# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Foundation: demo workload (meter-api Lambda), alarm, ticket table standing in for the ITSM tool,
# automatic opening of an AWS DevOps Agent investigation, raw archive of aws.aidevops events.

terraform {
  required_providers {
    aws     = { source = "hashicorp/aws" }
    archive = { source = "hashicorp/archive" }
  }
}

variable "prefix" { type = string }
variable "agent_space_id" { type = string }
variable "src_root" { type = string }
variable "kms_key_arn" {
  description = "Customer managed KMS key of this Region (module kms)"
  type        = string
}
variable "log_retention_days" {
  description = "Retention of the log groups created by the sample"
  type        = number
}
variable "ticket_writer_role_names" {
  description = "Names of the IAM roles of the other modules that write the ticket table (on-call caller, Connect handler, MCP runtime). Only these roles and this module's incident opener may write it (table resource policy). Names, not ARNs, so the foundation does not depend on the modules that read its outputs."
  type        = list(string)
}
variable "deployer_principal_arns" {
  description = "IAM principal ARNs of the deployer and the operator's scripts, exempted from the ticket table write Deny (table administration, test scripts). At least one"
  type        = list(string)
  validation {
    condition     = length(var.deployer_principal_arns) > 0
    error_message = "deployer_principal_arns needs at least one ARN."
  }
}
variable "deletion_protection" {
  description = "Protect the ticket table from deletion (set true outside a throwaway demo)"
  type        = bool
  default     = false
}

data "aws_caller_identity" "me" {}
data "aws_region" "here" {}

locals {
  account         = data.aws_caller_identity.me.account_id
  region          = data.aws_region.here.region
  agent_space_arn = "arn:aws:aidevops:${local.region}:${local.account}:agentspace/${var.agent_space_id}"
  healthy_config  = jsonencode({ unit = "kWh", backend = "meter-data-v2" })
  # Lambda execution-role trust: no aws:SourceArn/SourceAccount condition. The Lambda
  # execution-role doc (lambda-intro-execution-role.html) documents none for lambda.amazonaws.com; the
  # role is only usable by a function whose creator holds iam:PassRole on it. Left as documented.
  lambda_assume = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }] })
}

# --- Ticket table (stands in for the ITSM tool) ---
resource "aws_dynamodb_table" "tickets" {
  name         = "${var.prefix}-tickets"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "ticketId"
  attribute {
    name = "ticketId"
    type = "S"
  }
  attribute {
    name = "investigationTaskId"
    type = "S"
  }
  # Correlates an "Investigation *" event (task_id) with its ticket without a table scan.
  global_secondary_index {
    name            = "byInvestigation"
    projection_type = "ALL"
    key_schema {
      attribute_name = "investigationTaskId"
      key_type       = "HASH"
    }
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
  deletion_protection_enabled = var.deletion_protection
}

# Resource policy on the ticket table: only the sample's own roles may write tickets.
# Without it any principal of the account allowed dynamodb:UpdateItem by an identity policy could close
# a ticket directly, skipping the DevOps Agent approval step (the KMS key delegates to IAM, so it does
# not stop this). Data-plane writes only: control-plane actions (DescribeTable, UpdateTable,
# PutResourcePolicy...) are not listed, so Terraform keeps managing the table and this policy. Writes
# never go through the byInvestigation index (reads only), so only the table ARN is covered.
# TransactWriteItems is authorized as the PutItem/UpdateItem/DeleteItem it contains, so it is covered.
# Role ARNs are built from names; the role path is the default "/". The deployer principals
# (var.deployer_principal_arns) are exempted too, so the operator's scripts can still write a ticket.
locals {
  ticket_writer_role_arns = [for n in concat([aws_iam_role.opener.name], var.ticket_writer_role_names) : "arn:aws:iam::${local.account}:role/${n}"]
  ticket_write_exempt     = concat(local.ticket_writer_role_arns, var.deployer_principal_arns)
}

resource "aws_dynamodb_resource_policy" "tickets" {
  resource_arn = aws_dynamodb_table.tickets.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyTicketWritesExceptSampleRoles"
      Effect    = "Deny"
      Principal = "*"
      Action = [
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:PartiQLInsert",
        "dynamodb:PartiQLUpdate",
        "dynamodb:PartiQLDelete",
      ]
      Resource  = aws_dynamodb_table.tickets.arn
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = local.ticket_write_exempt } }
    }]
  })
}

# --- Recent boto3 layer (devops-agent client) ---
# Built by scripts/build.py (pinned boto3: the Lambda runtime one may not know devops-agent).
# The script zips with fixed timestamps, so a rebuild with the same pins keeps the same hash.
resource "aws_lambda_layer_version" "boto3" {
  layer_name          = "${var.prefix}-boto3"
  filename            = "${var.src_root}/build/layer_boto3.zip"
  source_code_hash    = filebase64sha256("${var.src_root}/build/layer_boto3.zip")
  compatible_runtimes = ["python3.13"]
  description         = "Pinned boto3 with the devops-agent client"
}

# --- Demo workload: meter-api Lambda, invoked directly (fault injected through CONFIG_JSON) ---
data "archive_file" "workload" {
  type        = "zip"
  source_dir  = "${var.src_root}/src/lambdas/meter_api"
  output_path = "${path.root}/.build/meter_api.zip"
  excludes    = ["__pycache__"]
}

resource "aws_iam_role" "workload" {
  name               = "${var.prefix}-meter-api"
  assume_role_policy = local.lambda_assume
}

resource "aws_iam_role_policy" "workload_kms" {
  role = aws_iam_role.workload.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Environment variables only (through Lambda, never a direct KMS call).
      { Effect = "Allow", Action = "kms:Decrypt", Resource = var.kms_key_arn,
      Condition = { StringEquals = { "kms:ViaService" = "lambda.${local.region}.amazonaws.com" } } },
      # Logs only to the function's own log group (created by Terraform, so no CreateLogGroup).
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.workload.arn}:*" },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "workload_xray" {
  role       = aws_iam_role.workload.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_cloudwatch_log_group" "workload" {
  name              = "/aws/lambda/${var.prefix}-meter-api"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_lambda_function" "workload" {
  #checkov:skip=CKV_AWS_117:No VPC resource is reached; a VPC would only add NAT cost
  #checkov:skip=CKV_AWS_272:Code signing belongs to the deployment pipeline, out of scope for a sample
  #checkov:skip=CKV_AWS_115:Reserved concurrency draws from the account pool and fails on new accounts
  #checkov:skip=CKV_AWS_116:Invoked synchronously (aws lambda invoke during the demo), a DLQ never receives anything
  function_name    = "${var.prefix}-meter-api"
  role             = aws_iam_role.workload.arn
  runtime          = "python3.13"
  handler          = "app.handler"
  filename         = data.archive_file.workload.output_path
  source_code_hash = data.archive_file.workload.output_base64sha256
  timeout          = 5
  kms_key_arn      = var.kms_key_arn
  tracing_config { mode = "Active" }
  environment { variables = { CONFIG_JSON = local.healthy_config } }
  depends_on = [aws_cloudwatch_log_group.workload, aws_iam_role_policy.workload_kms]
  # The fault is injected by hand during the demo: Terraform must not revert it.
  lifecycle { ignore_changes = [environment] }
}

# --- Alarm on the function errors ---
resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.prefix}-meter-api-errors"
  alarm_description   = "Errors of the meter-api Lambda function"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.workload.function_name }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}

# --- Automatic opening: alarm -> ticket + investigation ---
data "archive_file" "opener" {
  type        = "zip"
  output_path = "${path.root}/.build/incident_opener.zip"
  # The function code plus the ONE shared untrusted-text sanitizer (src/common/sanitize.py), so
  # every package ships the same file and the copies cannot drift.
  source {
    content  = file("${var.src_root}/src/lambdas/incident_opener/app.py")
    filename = "app.py"
  }
  source {
    content  = file("${var.src_root}/src/common/sanitize.py")
    filename = "sanitize.py"
  }
}

resource "aws_iam_role" "opener" {
  name               = "${var.prefix}-incident-opener"
  assume_role_policy = local.lambda_assume
}

resource "aws_iam_role_policy_attachment" "opener_xray" {
  role       = aws_iam_role.opener.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "opener" {
  role = aws_iam_role.opener.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # PutItem/GetItem: the ticket. UpdateItem: investigation id, the per-alarm lock item
      # (ALARM#<alarmName>, held until its ticket is CLOSED) and the repeated-ALARM comment on the held
      # ticket. GetItem also reads the lock and the held ticket's status.
      { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.tickets.arn },
      { Effect = "Allow", Action = "aidevops:CreateBacklogTask", Resource = local.agent_space_arn },
      # On-failure destination for the async invoke (opener_failures). Lambda delivers the record with
      # the function's execution role, so sqs:SendMessage must be on this role; the KMS actions below
      # cover the queue's customer managed key (per the Lambda async-destinations doc).
      { Effect = "Allow", Action = "sqs:SendMessage", Resource = aws_sqs_queue.opener_failures.arn },
      # Logs only to the function's own log group (created by Terraform, so no CreateLogGroup).
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.opener.arn}:*" },
      # Environment variables, encrypted ticket table, on-failure queue (via those services only).
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = var.kms_key_arn,
        Condition = { StringEquals = { "kms:ViaService" = [
          "lambda.${local.region}.amazonaws.com",
          "dynamodb.${local.region}.amazonaws.com",
          "sqs.${local.region}.amazonaws.com",
      ] } } },
    ]
  })
}

resource "aws_cloudwatch_log_group" "opener" {
  name              = "/aws/lambda/${var.prefix}-incident-opener"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_lambda_function" "opener" {
  #checkov:skip=CKV_AWS_117:No VPC resource is reached; a VPC would only add NAT cost
  #checkov:skip=CKV_AWS_272:Code signing belongs to the deployment pipeline, out of scope for a sample
  #checkov:skip=CKV_AWS_115:Reserved concurrency draws from the account pool and fails on new accounts
  #checkov:skip=CKV_AWS_116:Async invocation failures go to the on-failure destination opener_failures (the EventBridge target DLQ opener_dlq only catches delivery failures, not function errors)
  function_name    = "${var.prefix}-incident-opener"
  role             = aws_iam_role.opener.arn
  runtime          = "python3.13"
  handler          = "app.handler"
  filename         = data.archive_file.opener.output_path
  source_code_hash = data.archive_file.opener.output_base64sha256
  layers           = [aws_lambda_layer_version.boto3.arn]
  timeout          = 30
  kms_key_arn      = var.kms_key_arn
  tracing_config { mode = "Active" }
  environment {
    variables = {
      TICKETS_TABLE          = aws_dynamodb_table.tickets.name
      AGENT_SPACE_ID         = var.agent_space_id
      AGENT_SPACE_REGION     = local.region
      MONITORED_FUNCTION     = aws_lambda_function.workload.function_name
      INVESTIGATION_PRIORITY = "HIGH"
      SERVICE_NAME           = "meter-api"
    }
  }
  depends_on = [aws_cloudwatch_log_group.opener, aws_iam_role_policy.opener]
}

# EventBridge target DLQ: catches *delivery* failures (EventBridge could not invoke the function).
resource "aws_sqs_queue" "opener_dlq" {
  name                      = "${var.prefix}-opener-dlq"
  kms_master_key_id         = var.kms_key_arn
  message_retention_seconds = 345600
}

# Lambda on-failure destination: catches *function* errors (the invoke ran and the handler failed,
# e.g. CreateBacklogTask threw). Without it a failing opener drops the ticket + investigation + call
# silently; the EventBridge target DLQ never sees a function error.
resource "aws_sqs_queue" "opener_failures" {
  name                      = "${var.prefix}-opener-failures"
  kms_master_key_id         = var.kms_key_arn
  message_retention_seconds = 345600
}

# Function errors (async invoke) go to the on-failure destination. Bounded retries and a max event
# age cap how long Lambda keeps retrying before the record is written to opener_failures.
resource "aws_lambda_function_event_invoke_config" "opener" {
  function_name                = aws_lambda_function.opener.function_name
  maximum_retry_attempts       = 1
  maximum_event_age_in_seconds = 3600
  destination_config {
    on_failure { destination = aws_sqs_queue.opener_failures.arn }
  }
}

resource "aws_cloudwatch_event_rule" "alarm_to_opener" {
  name = "${var.prefix}-alarm-to-investigation"
  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    resources     = [aws_cloudwatch_metric_alarm.errors.arn]
    detail        = { state = { value = ["ALARM"] } }
  })
}

resource "aws_cloudwatch_event_target" "alarm_to_opener" {
  rule = aws_cloudwatch_event_rule.alarm_to_opener.name
  arn  = aws_lambda_function.opener.arn
  dead_letter_config { arn = aws_sqs_queue.opener_dlq.arn }
  retry_policy {
    maximum_retry_attempts       = 2
    maximum_event_age_in_seconds = 3600
  }
}

resource "aws_lambda_permission" "opener_events" {
  statement_id  = "EventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.opener.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.alarm_to_opener.arn
}

resource "aws_sqs_queue_policy" "opener_dlq" {
  queue_url = aws_sqs_queue.opener_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sqs:SendMessage", Resource = aws_sqs_queue.opener_dlq.arn,
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.alarm_to_opener.arn } } },
      { Sid = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.opener_dlq.arn,
      Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    ]
  })
}

# TLS only on the on-failure queue too. Lambda writes to it with the function role.
resource "aws_sqs_queue_policy" "opener_failures" {
  queue_url = aws_sqs_queue.opener_failures.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Sid = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.opener_failures.arn,
    Condition = { Bool = { "aws:SecureTransport" = "false" } } }]
  })
}

# Every repeated ALARM the opener suppresses (no new ticket, investigation or call) logs
# {"ticketId": ..., "alarm": ..., "held": true}. This metric counts those lines; no alarm on it by
# design (a flapping alarm is information, not a page). Namespace <prefix>/oncall, no dimension.
resource "aws_cloudwatch_log_metric_filter" "alarm_suppressed" {
  name           = "${var.prefix}-alarm-suppressed"
  log_group_name = aws_cloudwatch_log_group.opener.name
  pattern        = "{ $.held IS TRUE }"
  metric_transformation {
    name      = "AlarmSuppressed"
    namespace = "${var.prefix}/oncall"
    value     = "1"
    unit      = "Count"
  }
}

# Both opener queues hold evidence that an investigation was not opened: alarm on depth so they are
# visible (same pattern as the caller queues). No action by default: attach your SNS topic.
resource "aws_cloudwatch_metric_alarm" "opener_queues" {
  for_each = {
    opener-dlq      = aws_sqs_queue.opener_dlq.name
    opener-failures = aws_sqs_queue.opener_failures.name
  }
  alarm_name          = "${var.prefix}-${each.key}-not-empty"
  alarm_description   = "Messages in ${each.value}: an incident was not opened. Inspect the queue."
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

# --- Raw archive of every AWS DevOps Agent event (to read real payloads) ---
resource "aws_cloudwatch_log_group" "events" {
  name              = "/aws/events/${var.prefix}-aidevops"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_cloudwatch_log_resource_policy" "events" {
  policy_name = "${var.prefix}-eventbridge-to-logs"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      # Both principals are the documented policy for an EventBridge rule -> CloudWatch Logs target
      # (the console creates exactly this pair); delivery.logs.amazonaws.com is the vended-log
      # delivery principal, so it is kept as documented, not dropped.
      # https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-use-resource-based.html (CloudWatch Logs permissions)
      Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.events.arn}:*"
      # Confused-deputy account scoping: only this account's EventBridge/delivery can write here.
      # aws:SourceArn (the archive rule) is NOT added: the documented CloudWatch Logs example omits
      # it (unlike the Lambda/SNS/SQS examples that show it), and whether the delivery.logs principal
      # passes the rule ARN as aws:SourceArn is UNVERIFIED, so adding it could silently break delivery.
      Condition = { StringEquals = { "aws:SourceAccount" = local.account } }
    }]
  })
}

resource "aws_cloudwatch_event_rule" "archive" {
  name          = "${var.prefix}-aidevops-archive"
  event_pattern = jsonencode({ source = ["aws.aidevops"] })
}

resource "aws_cloudwatch_event_target" "archive" {
  rule = aws_cloudwatch_event_rule.archive.name
  arn  = aws_cloudwatch_log_group.events.arn
}

output "tickets_table_name" { value = aws_dynamodb_table.tickets.name }
output "tickets_table_arn" { value = aws_dynamodb_table.tickets.arn }
output "boto3_layer_arn" { value = aws_lambda_layer_version.boto3.arn }
output "workload_function_name" { value = aws_lambda_function.workload.function_name }
output "alarm_name" { value = aws_cloudwatch_metric_alarm.errors.alarm_name }
output "events_log_group" { value = aws_cloudwatch_log_group.events.name }
output "agent_space_arn" { value = local.agent_space_arn }
output "alarm_suppressed_metric" { value = { namespace = "${var.prefix}/oncall", name = "AlarmSuppressed" } }
