# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# DevOps Agent Region: investigation end (EventBridge) -> caller Lambda -> StartOutboundVoiceContact.
# The Connect instance can be in another Region.

locals {
  event_types = ["Investigation Completed", "Investigation Failed", "Investigation Timed Out", "Investigation Cancelled"]
  sources     = merge({ real = "aws.aidevops" }, var.enable_replay ? { replay = local.replay_source } : {})
  # Only this Agent Space's investigations trigger a call (detail.metadata.agent_space_id,
  # the field the caller already reads to fetch the summary).
  agent_space_id = regex("agentspace/([^/]+)$", var.agent_space_arn)[0]
}

# The replay rule listens on a dedicated bus, not the default bus. Its policy denies
# events:PutEvents to every principal except the deployer principals (var.deployer_principal_arns),
# so another role of the account allowed PutEvents on "*" cannot place a call through the replay
# source. scripts/replay.py publishes to this bus (output oncall.replay_bus_name).
resource "aws_cloudwatch_event_bus" "replay" {
  count = var.enable_replay ? 1 : 0
  name  = "${var.prefix}-replay"
}

resource "aws_cloudwatch_event_bus_policy" "replay" {
  count          = var.enable_replay ? 1 : 0
  event_bus_name = aws_cloudwatch_event_bus.replay[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyPutEventsExceptDeployer"
      Effect    = "Deny"
      Principal = "*"
      Action    = "events:PutEvents"
      Resource  = aws_cloudwatch_event_bus.replay[0].arn
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = var.deployer_principal_arns } }
    }]
  })
}

# On-call number: SecureString created with a placeholder, real value set outside IaC (never in
# code or Terraform inputs). Terraform reads it back into state on refresh: treat the state as sensitive.
resource "aws_ssm_parameter" "oncall_number" {
  name        = "/${var.prefix}/oncall-number"
  type        = "SecureString"
  key_id      = var.kms_key_arn
  value       = "UNSET"
  description = "On-call phone number, E.164 (set outside IaC)"
  lifecycle { ignore_changes = [value] }
}

data "archive_file" "caller" {
  type        = "zip"
  output_path = "${path.root}/.build/${var.prefix}-oncall_caller.zip"
  # The function code plus the ONE shared untrusted-text sanitizer (src/common/sanitize.py), so
  # every package ships the same file and the copies cannot drift.
  source {
    content  = file("${var.src_root}/src/lambdas/oncall_caller/app.py")
    filename = "app.py"
  }
  source {
    content  = file("${var.src_root}/src/common/sanitize.py")
    filename = "sanitize.py"
  }
}

resource "aws_iam_role" "caller" {
  name               = "${var.prefix}-caller"
  assume_role_policy = local.lambda_assume
}

resource "aws_iam_role_policy_attachment" "caller_xray" {
  role       = aws_iam_role.caller.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_sqs_queue" "caller_failures" {
  name                      = "${var.prefix}-caller-failures"
  kms_master_key_id         = var.kms_key_arn
  message_retention_seconds = 345600
}

resource "aws_sqs_queue" "delivery_dlq" {
  name                      = "${var.prefix}-eventbridge-dlq"
  kms_master_key_id         = var.kms_key_arn
  message_retention_seconds = 345600
}

resource "aws_iam_role_policy" "caller" {
  role = aws_iam_role.caller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Query only on the index the caller reads, UpdateItem only on the base table.
      { Effect = "Allow", Action = "dynamodb:Query", Resource = "${var.tickets_table_arn}/index/byInvestigation" },
      { Effect = "Allow", Action = "dynamodb:UpdateItem", Resource = var.tickets_table_arn },
      { Effect = "Allow", Action = "aidevops:ListJournalRecords", Resource = var.agent_space_arn },
      # The Service Authorization Reference lists no resource type for StartOutboundVoiceContact; the
      # Connect IAM examples scope it to the instance's contacts, and live calls succeed with it.
      { Effect = "Allow", Action = "connect:StartOutboundVoiceContact", Resource = "${local.connect_arn}/contact/*" },
      { Effect = "Allow", Action = "ssm:GetParameter", Resource = aws_ssm_parameter.oncall_number.arn },
      { Effect = "Allow", Action = "sqs:SendMessage", Resource = aws_sqs_queue.caller_failures.arn },
      # Logs only to the function's own log group (created by Terraform, so no CreateLogGroup).
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.caller.arn}:*" },
      # SecureString, environment variables, encrypted ticket table and failure queue, only through
      # those services (kms:ViaService).
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = var.kms_key_arn,
        Condition = { StringEquals = { "kms:ViaService" = [
          "ssm.${local.region}.amazonaws.com",
          "lambda.${local.region}.amazonaws.com",
          "dynamodb.${local.region}.amazonaws.com",
          "sqs.${local.region}.amazonaws.com",
      ] } } },
    ]
  })
}

resource "aws_cloudwatch_log_group" "caller" {
  name              = "/aws/lambda/${var.prefix}-caller"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_lambda_function" "caller" {
  #checkov:skip=CKV_AWS_117:No VPC resource is reached; a VPC would only add NAT cost
  #checkov:skip=CKV_AWS_272:Code signing belongs to the deployment pipeline, out of scope for a sample
  #checkov:skip=CKV_AWS_116:Async invocation failures go to the on-failure destination caller_failures
  function_name    = "${var.prefix}-caller"
  role             = aws_iam_role.caller.arn
  runtime          = "python3.13"
  handler          = "app.handler"
  filename         = data.archive_file.caller.output_path
  source_code_hash = data.archive_file.caller.output_base64sha256
  layers           = [var.boto3_layer_arn]
  timeout          = 30
  kms_key_arn      = var.kms_key_arn
  # One call placed at a time (default 1; -1 = unreserved). Throttled async invokes are
  # retried by Lambda within the event age, so a burst of investigation ends is serialized, not lost.
  reserved_concurrent_executions = var.caller_reserved_concurrency
  tracing_config { mode = "Active" }
  environment {
    variables = {
      TICKETS_TABLE         = var.tickets_table_name
      AGENT_SPACE_REGION    = local.region
      CONNECT_REGION        = local.connect_region
      CONNECT_INSTANCE_ID   = var.connect_instance_id
      CONNECT_FLOW_ID       = aws_connect_contact_flow.oncall.contact_flow_id
      CONNECT_SOURCE_NUMBER = var.connect_source_number
      ONCALL_NUMBER_PARAM   = aws_ssm_parameter.oncall_number.name
      REPLAY_SOURCE         = local.replay_source
      DRY_RUN               = tostring(var.dry_run)
      SPEECH_LABELS         = jsonencode(var.locale.labels)
    }
  }
  depends_on = [aws_cloudwatch_log_group.caller, aws_iam_role_policy.caller]
}

# Function errors (async invoke) go to an on-failure destination. The EventBridge target DLQ
# below only catches delivery failures, not function errors.
resource "aws_lambda_function_event_invoke_config" "caller" {
  function_name          = aws_lambda_function.caller.function_name
  maximum_retry_attempts = 1
  destination_config {
    on_failure { destination = aws_sqs_queue.caller_failures.arn }
  }
}

resource "aws_cloudwatch_event_rule" "oncall" {
  for_each = local.sources
  name     = "${var.prefix}-investigation-${each.key}"
  # The replay rule is on the replay bus; the real one on the default bus (null).
  event_bus_name = each.key == "replay" ? aws_cloudwatch_event_bus.replay[0].name : null
  # Filter on detail-type: the aws.aidevops source also carries "AWS API Call via CloudTrail" events.
  event_pattern = jsonencode({
    source        = [each.value]
    "detail-type" = local.event_types
    detail = {
      metadata = { agent_space_id = [local.agent_space_id] }
      data     = { priority = ["CRITICAL", "HIGH"] }
    }
  })
}

resource "aws_cloudwatch_event_target" "oncall" {
  for_each       = aws_cloudwatch_event_rule.oncall
  rule           = each.value.name
  event_bus_name = each.key == "replay" ? aws_cloudwatch_event_bus.replay[0].name : null
  arn            = aws_lambda_function.caller.arn
  dead_letter_config { arn = aws_sqs_queue.delivery_dlq.arn }
  retry_policy {
    maximum_retry_attempts       = 2
    maximum_event_age_in_seconds = 3600
  }
}

resource "aws_lambda_permission" "caller_events" {
  for_each      = aws_cloudwatch_event_rule.oncall
  statement_id  = "EventBridge-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.caller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = each.value.arn
}

resource "aws_sqs_queue_policy" "delivery_dlq" {
  queue_url = aws_sqs_queue.delivery_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sqs:SendMessage", Resource = aws_sqs_queue.delivery_dlq.arn,
      Condition = { ArnEquals = { "aws:SourceArn" = [for r in aws_cloudwatch_event_rule.oncall : r.arn] } } },
      { Sid = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.delivery_dlq.arn,
      Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    ]
  })
}

# TLS only on the on-failure queue too. Lambda writes to it with the function role.
resource "aws_sqs_queue_policy" "caller_failures" {
  queue_url = aws_sqs_queue.caller_failures.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Sid = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.caller_failures.arn,
    Condition = { Bool = { "aws:SecureTransport" = "false" } } }]
  })
}

# Failures of the caller (no linked ticket, Connect error) and EventBridge delivery failures land in
# these queues; alarm on depth so they are visible. No action by default: attach your SNS topic.
resource "aws_cloudwatch_metric_alarm" "caller_queues" {
  for_each = {
    caller-failures = aws_sqs_queue.caller_failures.name
    eventbridge-dlq = aws_sqs_queue.delivery_dlq.name
  }
  alarm_name          = "${var.prefix}-${each.key}-not-empty"
  alarm_description   = "Messages in ${each.value}: an on-call call was not placed. Inspect the queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = each.value }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}
