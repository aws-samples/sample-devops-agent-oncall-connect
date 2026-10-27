# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Connect Region: the Lambda the contact flow invokes. "init" copies the incident context into the
# AI agent session, "verify_pin" checks the optional recipient PIN, "ack" writes the acknowledgment
# in the ticket (table in the DevOps Agent Region) once the callee confirmed it with key 1, "notify"
# sends the no-answer SMS (End User Messaging) and/or email (SES) from the Connect Region. The SMS
# goes to the on-call number SecureString (DevOps Agent Region), never to a number read from the contact.
# The same Lambda also drains an SQS queue fed by a contact-event rule: a call that is never
# acknowledged still emits DISCONNECTED, which drives the same notify path (only for the contact the
# caller bound to the ticket); after an acknowledgment it sends one written receipt instead. notify is idempotent per
# ticket (claim-with-lease state machine): delivery is at-least-once, a duplicate is possible when
# a message was accepted but the attempt failed before recording SENT (SDK timeout, final write error).

data "archive_file" "handler" {
  type        = "zip"
  output_path = "${path.root}/.build/${var.prefix}-connect_handler.zip"
  # The function code plus the ONE shared untrusted-text sanitizer (src/common/sanitize.py), so
  # every package ships the same file and the copies cannot drift.
  source {
    content  = file("${var.src_root}/src/lambdas/connect_handler/app.py")
    filename = "app.py"
  }
  source {
    content  = file("${var.src_root}/src/common/sanitize.py")
    filename = "sanitize.py"
  }
}

# Pinned boto3 in the Connect Region. The layer the foundation module publishes lives in the DevOps
# Agent Region and a Lambda layer is Regional, so it cannot be attached across Regions: publish an
# equivalent version here from the same build artifact (scripts/build.py, deterministic hash).
resource "aws_lambda_layer_version" "boto3_connect" {
  provider            = aws.connect
  layer_name          = "${var.prefix}-boto3"
  filename            = "${var.src_root}/build/layer_boto3.zip"
  source_code_hash    = filebase64sha256("${var.src_root}/build/layer_boto3.zip")
  compatible_runtimes = ["python3.13"]
  description         = "Pinned boto3 (Connect Region)"
}

# On-call email (recipient of the no-answer email): SecureString in the Connect Region, created with a
# placeholder only when email is enabled; real value set outside IaC, like the on-call number.
resource "aws_ssm_parameter" "oncall_email" {
  count       = local.email_enabled ? 1 : 0
  provider    = aws.connect
  name        = "/${var.prefix}/oncall-email"
  type        = "SecureString"
  key_id      = var.connect_kms_key_arn
  value       = "UNSET"
  description = "On-call email address for the no-answer notification (set outside IaC)"
  lifecycle { ignore_changes = [value] }
}

resource "aws_iam_role" "handler" {
  provider           = aws.connect
  name               = "${var.prefix}-connect-handler"
  assume_role_policy = local.lambda_assume
}

resource "aws_iam_role_policy_attachment" "handler_xray" {
  provider   = aws.connect
  role       = aws_iam_role.handler.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "handler" {
  provider = aws.connect
  role     = aws_iam_role.handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      # GetItem (ConsistentRead) backs _held_unexpired on a lost claim; UpdateItem is the whole state machine.
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = var.tickets_table_arn },
      # Logs only to the function's own log group (created by Terraform, so no CreateLogGroup).
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.handler.arn}:*" },
      # Decrypt only (the handler never encrypts: no SecureString write, no send to an
      # encrypted queue; DynamoDB encrypts on the service side), and only through the services it uses:
      # its environment (Lambda) and queue (SQS) in the Connect Region, the SecureStrings in both
      # Regions, the ticket table in the DevOps Agent Region.
      { Effect = "Allow", Action = "kms:Decrypt", Resource = [var.connect_kms_key_arn, var.kms_key_arn],
        Condition = { StringEquals = { "kms:ViaService" = [
          "lambda.${local.connect_region}.amazonaws.com",
          "sqs.${local.connect_region}.amazonaws.com",
          "ssm.${local.connect_region}.amazonaws.com",
          "ssm.${local.region}.amazonaws.com",
          "dynamodb.${local.region}.amazonaws.com",
      ] } } },
      { Effect = "Allow", Action = "wisdom:UpdateSessionData", Resource = "${replace(awscc_wisdom_assistant.this.assistant_arn, ":assistant/", ":session/")}/*" },
      # The event path reads the ticket context back from the contact. The SMS destination is NOT read
      # from the contact (no DescribeContact): it is the on-call number SecureString below.
      { Effect = "Allow", Action = "connect:GetContactAttributes", Resource = "${local.connect_arn}/contact/*" },
      # SMS destination: the configured on-call number (DevOps Agent Region; its key var.kms_key_arn is
      # in the kms:Decrypt statement above). Exactly that one parameter.
      { Effect = "Allow", Action = "ssm:GetParameter", Resource = [aws_ssm_parameter.oncall_number.arn] },
      # Drain the unanswered-call queue (event source mapping). Scoped to this queue only.
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.unanswered.arn },
      ],
      local.sms_enabled ? [{ Effect = "Allow", Action = "sms-voice:SendTextMessage", Resource = var.sms_origination_identity_arn }] : [],
      local.email_enabled ? [
        # The identity and the TLS-required configuration set (applied as the identity's default).
        { Effect = "Allow", Action = "ses:SendEmail", Resource = concat(local.ses_identity_arns, [aws_sesv2_configuration_set.notify[0].arn]) },
        { Effect = "Allow", Action = "ssm:GetParameter", Resource = [aws_ssm_parameter.oncall_email[0].arn] },
      ] : [],
      # Optional recipient (PIN) gate: read-only on the PIN SecureString, scoped to that one parameter.
      var.enable_pin ? [{ Effect = "Allow", Action = "ssm:GetParameter", Resource = [aws_ssm_parameter.oncall_pin[0].arn] }] : [],
    )
  })
}

resource "aws_cloudwatch_log_group" "handler" {
  provider          = aws.connect
  name              = "/aws/lambda/${var.prefix}-connect-handler"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.connect_kms_key_arn
}

resource "aws_lambda_function" "handler" {
  #checkov:skip=CKV_AWS_117:No VPC resource is reached; a VPC would only add NAT cost
  #checkov:skip=CKV_AWS_272:Code signing belongs to the deployment pipeline, out of scope for a sample
  #checkov:skip=CKV_AWS_115:Reserved concurrency draws from the account pool and fails on new accounts
  #checkov:skip=CKV_AWS_116:Invoked by the contact flow (sync) and by the SQS poller (sync); poison messages go to the SQS redrive DLQ unanswered_dlq, not a Lambda async DLQ
  provider         = aws.connect
  function_name    = "${var.prefix}-connect-handler"
  role             = aws_iam_role.handler.arn
  runtime          = "python3.13"
  handler          = "app.handler"
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  layers           = [aws_lambda_layer_version.boto3_connect.arn]
  timeout          = local.handler_timeout
  kms_key_arn      = var.connect_kms_key_arn
  tracing_config { mode = "Active" }
  environment {
    variables = {
      TICKETS_TABLE            = var.tickets_table_name
      TICKETS_REGION           = local.region
      ACK_TEXT                 = var.locale.labels.ack
      NOTIFY_LABELS            = jsonencode({ for k in local.notify_label_keys : k => var.locale.labels[k] if contains(keys(var.locale.labels), k) })
      SMS_ORIGINATION_IDENTITY = local.sms_enabled ? var.sms_origination_identity_arn : ""
      NOTIFY_EMAIL_FROM        = local.email_enabled ? var.notify_email_from : ""
      NOTIFY_EMAIL_TO_PARAM    = local.email_enabled ? aws_ssm_parameter.oncall_email[0].name : ""
      # SMS destination: on-call number SecureString (DevOps Agent Region = TICKETS_REGION).
      ONCALL_NUMBER_PARAM = aws_ssm_parameter.oncall_number.name
      # Optional recipient (PIN) gate: empty when disabled, so verify_pin fails closed and is never
      # invoked by the flow. The entered digits reach the handler only as a Lambda parameter.
      ONCALL_PIN_PARAM = var.enable_pin ? aws_ssm_parameter.oncall_pin[0].name : ""
      # Claim lease: 3x the Lambda timeout, so a slow invocation keeps the claim long enough to
      # finish and a dead invocation releases it for the next attempt.
      LEASE_SECONDS = tostring(3 * local.handler_timeout)
    }
  }
  depends_on = [aws_cloudwatch_log_group.handler, aws_iam_role_policy.handler]
}

resource "aws_lambda_permission" "connect" {
  provider       = aws.connect
  statement_id   = "ConnectInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.handler.function_name
  principal      = "connect.amazonaws.com"
  source_account = local.account
  source_arn     = local.connect_arn
}

resource "aws_connect_lambda_function_association" "handler" {
  provider     = aws.connect
  instance_id  = var.connect_instance_id
  function_arn = aws_lambda_function.handler.arn
}

# --- Unanswered-call path: contact event -> SQS (delay) -> connect-handler (event source mapping) ---
# Calls that are never acknowledged still emit DISCONNECTED. The rule routes those to an SQS queue
# with a delay (longer than the claim lease and the ACK grace), so the in-flow notify runs first on
# a connected call and the event is a backstop. The handler drains the queue one message at a time
# and reports item failures (a claim still held, or every channel failed) so SQS retries them; poison messages redrive to the
# DLQ after maxReceiveCount, watched by a CloudWatch alarm.

resource "aws_sqs_queue" "unanswered_dlq" {
  provider                  = aws.connect
  name                      = "${var.prefix}-unanswered-dlq"
  kms_master_key_id         = var.connect_kms_key_arn
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "unanswered" {
  provider                   = aws.connect
  name                       = "${var.prefix}-unanswered"
  kms_master_key_id          = var.connect_kms_key_arn
  delay_seconds              = var.unanswered_delay_seconds
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 6 * local.handler_timeout # >= 6x the Lambda timeout, so a retry never overlaps a run
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.unanswered_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "unanswered" {
  provider  = aws.connect
  queue_url = aws_sqs_queue.unanswered.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.unanswered.arn
        Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.unanswered.arn } }
      },
      { Sid = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.unanswered.arn,
      Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    ]
  })
}

# TLS only on the DLQ too. The redrive from the unanswered queue is internal to SQS.
resource "aws_sqs_queue_policy" "unanswered_dlq" {
  provider  = aws.connect
  queue_url = aws_sqs_queue.unanswered_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Sid = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.unanswered_dlq.arn,
    Condition = { Bool = { "aws:SecureTransport" = "false" } } }]
  })
}

# The no-answer email carries the summary, so SES must not fall back to plaintext SMTP.
# A configuration set with TlsPolicy REQUIRE, created only when email is enabled. The sender identity
# is verified outside Terraform, so assign this set as the identity's DEFAULT configuration set
# (aws sesv2 put-email-identity-configuration-set-attributes, see docs/terraform.md): SES then applies
# it to every SendEmail from the identity, no code change. Do not destroy it while it is still the
# identity's default: SES then rejects sends from that identity (SES default configuration sets doc).
resource "aws_sesv2_configuration_set" "notify" {
  count                  = local.email_enabled ? 1 : 0
  provider               = aws.connect
  configuration_set_name = "${var.prefix}-notify"
  delivery_options { tls_policy = "REQUIRE" }
}

resource "aws_lambda_event_source_mapping" "unanswered" {
  provider                           = aws.connect
  event_source_arn                   = aws_sqs_queue.unanswered.arn
  function_name                      = aws_lambda_function.handler.arn
  batch_size                         = 1
  function_response_types            = ["ReportBatchItemFailures"]
  maximum_batching_window_in_seconds = 0
}

# Contact events (default bus, Connect Region): DISCONNECTED on an API-initiated VOICE contact of
# this instance. No connectedToSystemTimestamp filter: the handler decides from ticket state and
# derives the reason (answered-but-not-acknowledged vs never-answered) from the timestamp.
resource "aws_cloudwatch_event_rule" "unanswered" {
  provider    = aws.connect
  name        = "${var.prefix}-unanswered-call"
  description = "On-call call not acknowledged: notify by SMS/email through connect-handler"
  event_pattern = jsonencode({
    source = ["aws.connect"]
    # The contact events page shows both detail-type strings and both channel casings in its
    # samples (the DISCONNECTED sample uses "Connect Customer Contact Event" and "Voice"), so the
    # rule accepts both: EventBridge matches any value in each list.
    detail-type = ["Amazon Connect Contact Event", "Connect Customer Contact Event"]
    detail = {
      eventType        = ["DISCONNECTED"]
      initiationMethod = ["API"]
      instanceArn      = [local.connect_arn]
      channel          = ["VOICE", "Voice"]
    }
  })
}

resource "aws_cloudwatch_event_target" "unanswered" {
  provider = aws.connect
  rule     = aws_cloudwatch_event_rule.unanswered.name
  arn      = aws_sqs_queue.unanswered.arn
}

resource "aws_cloudwatch_metric_alarm" "unanswered_dlq" {
  provider            = aws.connect
  alarm_name          = "${var.prefix}-unanswered-dlq"
  alarm_description   = "A no-answer notification could not be processed and landed in the DLQ"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.unanswered_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}
