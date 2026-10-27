# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# On-call call at the end of an AWS DevOps Agent investigation, handled by an Amazon Connect AI agent.
#
#   caller.tf     DevOps Agent Region: EventBridge rule -> caller Lambda -> StartOutboundVoiceContact
#   assistant.tf  Connect Region: AI agents assistant, associated with the instance
#   agent.tf      Connect Region: orchestration prompt + AI agent (voice) with Acknowledge / Complete tools
#   lex.tf        Connect Region: Lex bot that carries the AI agent (CloudFormation, provider gap)
#   handler.tf    Connect Region: Lambda the flow invokes (session init, acknowledgment in the ticket)
#   flow.tf       Connect Region: contact flow

terraform {
  # Cross-variable validations (pin.tf, variables.tf) need Terraform >= 1.9.
  required_version = ">= 1.9"
  required_providers {
    aws     = { source = "hashicorp/aws", configuration_aliases = [aws.connect] }
    awscc   = { source = "hashicorp/awscc", configuration_aliases = [awscc.connect] }
    archive = { source = "hashicorp/archive" }
  }
}

variable "prefix" { type = string }
variable "src_root" { type = string }
variable "agent_space_arn" { type = string }
variable "tickets_table_name" { type = string }
variable "tickets_table_arn" { type = string }
variable "boto3_layer_arn" { type = string }
variable "connect_instance_id" { type = string }
variable "connect_source_number" { type = string }
variable "dry_run" {
  description = "true: the caller logs the StartOutboundVoiceContact request without placing the call"
  type        = bool
  default     = true
}
variable "enable_replay" {
  description = "Also listen on <prefix>.replay (scripts/replay.py). Any principal with events:PutEvents can then trigger a call"
  type        = bool
}
variable "kms_key_arn" {
  description = "KMS key of the AWS DevOps Agent Region (ticket table, caller)"
  type        = string
}
variable "connect_kms_key_arn" {
  description = "KMS key of the Amazon Connect Region (connect handler)"
  type        = string
}
variable "log_retention_days" {
  type = number
  validation {
    condition     = contains([0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be a CloudWatch Logs retention value (0 = never expire, or 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653)."
  }
}
variable "locale" {
  description = "Decoded src/locales/<language>.json (spoken texts, voice, language)"
  type        = any
}
variable "model_id" {
  description = "Bedrock model of the AI agent prompt (default: Claude Haiku 4.5 profile of the Connect Region's geography)"
  type        = string
  default     = null
}
variable "gate_timeout_seconds" {
  description = "Seconds the flow waits for the engineer to press 1 after the opening announcement"
  type        = number
  default     = 8
  validation {
    condition     = var.gate_timeout_seconds >= 1 && var.gate_timeout_seconds <= 180
    error_message = "Get customer input accepts 1 to 180 seconds."
  }
}
variable "gate_retry_timeout_seconds" {
  description = "Seconds the flow waits for 1 after the short second prompt. Kept short: a voicemail that cuts on silence hangs up before the notification otherwise"
  type        = number
  default     = 5
  validation {
    condition     = var.gate_retry_timeout_seconds >= 1 && var.gate_retry_timeout_seconds <= 180
    error_message = "Get customer input accepts 1 to 180 seconds."
  }
}
variable "unanswered_delay_seconds" {
  description = "SQS delay before the unanswered-call event reaches connect-handler. Must exceed the claim lease (3x the 8 s handler timeout = 24 s) and the ACK grace, so the in-flow notify on a connected call always runs and releases the claim before the event fires. Minimum 30, default 60"
  type        = number
  default     = 60
  validation {
    # >= 30 keeps it above the 24 s lease plus a few seconds of ACK grace; <= 900 is the SQS cap.
    condition     = var.unanswered_delay_seconds >= 30 && var.unanswered_delay_seconds <= 900
    error_message = "unanswered_delay_seconds must be between 30 and 900: it has to exceed the 24 s claim lease (3x the handler timeout) and the ACK grace, and SQS DelaySeconds caps at 900."
  }
}
variable "sms_origination_identity_arn" {
  description = "AWS End User Messaging SMS phone number, pool or sender ID ARN (Connect Region). null = no SMS when nobody presses 1"
  type        = string
  default     = null
}
variable "notify_email_from" {
  description = "Amazon SES verified sender address (the address itself or its domain is verified, Connect Region). null = no email. The recipient goes in the oncall_email_param SecureString, set outside IaC"
  type        = string
  default     = null
  validation {
    condition     = var.notify_email_from == null || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.notify_email_from))
    error_message = "notify_email_from must be an email address (SES FromEmailAddress), not a bare domain."
  }
}

data "aws_caller_identity" "me" {}
data "aws_region" "here" {}
data "aws_region" "connect" { provider = aws.connect }

locals {
  account        = data.aws_caller_identity.me.account_id
  region         = data.aws_region.here.region
  connect_region = data.aws_region.connect.region
  connect_arn    = "arn:aws:connect:${local.connect_region}:${local.account}:instance/${var.connect_instance_id}"
  # Lambda execution-role trust: no SourceArn/SourceAccount condition, none documented
  # for lambda.amazonaws.com (lambda-intro-execution-role.html). Left as documented.
  lambda_assume = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }] })
  replay_source = "${var.prefix}.replay"
  # Cross-Region inference profile of the Connect Region's geography (eu., us., apac.).
  geo      = startswith(local.connect_region, "eu-") ? "eu" : startswith(local.connect_region, "ap-") ? "apac" : "us"
  model_id = coalesce(var.model_id, "${local.geo}.anthropic.claude-haiku-4-5-20251001-v1:0")
  # No-answer notifications (connect-handler action "notify").
  sms_enabled   = var.sms_origination_identity_arn != null
  email_enabled = var.notify_email_from != null
  # connect-handler timeout (seconds). The claim lease is 3x this value.
  handler_timeout = 8
  # Locale labels passed to connect-handler (only the keys that exist in the locale are forwarded).
  notify_label_keys = [
    "no_answer", "not_connected", "not_acknowledged", "notified", "notify_failed", "notify_no_budget", "notify_sms", "notify_email_subject", "notify_email_body",
    "ack_receipt_sms", "ack_receipt_email_subject", "ack_receipt_email_body", "ack_receipt_sent",
    # The handler reads the summary from the ticket; missing -> this label.
    "summary_unavailable",
  ]
  # SES authorizes the identity that was verified: the address itself or its domain.
  ses_identity_arns = local.email_enabled ? [
    "arn:aws:ses:${local.connect_region}:${local.account}:identity/${var.notify_email_from}",
    "arn:aws:ses:${local.connect_region}:${local.account}:identity/${split("@", var.notify_email_from)[1]}",
  ] : []
}

output "caller_function_name" { value = aws_lambda_function.caller.function_name }
output "handler_function_name" { value = aws_lambda_function.handler.function_name }
output "flow_id" { value = aws_connect_contact_flow.oncall.contact_flow_id }
output "oncall_number_param" { value = aws_ssm_parameter.oncall_number.name }
output "oncall_email_param" { value = local.email_enabled ? aws_ssm_parameter.oncall_email[0].name : null }
output "replay_source" { value = local.replay_source }
output "replay_bus_name" { value = var.enable_replay ? aws_cloudwatch_event_bus.replay[0].name : null }
output "ses_configuration_set" { value = local.email_enabled ? aws_sesv2_configuration_set.notify[0].configuration_set_name : null }
output "assistant_id" { value = awscc_wisdom_assistant.this.assistant_id }
output "ai_agent_arn" { value = local.ai_agent_versioned_arn }
output "lex_alias_arn" { value = aws_cloudformation_stack.lex.outputs["AliasArn"] }
output "failures_queue_url" { value = aws_sqs_queue.caller_failures.url }
output "unanswered_queue_url" { value = aws_sqs_queue.unanswered.url }
output "unanswered_dlq_url" { value = aws_sqs_queue.unanswered_dlq.url }
