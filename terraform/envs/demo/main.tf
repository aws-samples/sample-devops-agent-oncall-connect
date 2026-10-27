# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

locals {
  src_root = abspath("${path.root}/../../..")
  locale   = jsondecode(file("${local.src_root}/src/locales/${var.language}.json"))
  # Principals exempted from the runtime, DEFAULT endpoint, ticket table and replay bus Denys. Default:
  # the IAM role (or user) that runs Terraform, i.e. the session's issuer, not the session ARN.
  deployer_principal_arns = var.deployer_principal_arns != null ? var.deployer_principal_arns : [data.aws_iam_session_context.deployer.issuer_arn]
}

data "aws_caller_identity" "deployer" {}

# Resolves an assumed-role session ARN (sts::...:assumed-role/<role>/<session>) to the role ARN,
# with its path (SSO roles live under /aws-reserved/...), which is what aws:PrincipalArn carries.
data "aws_iam_session_context" "deployer" {
  arn = data.aws_caller_identity.deployer.arn
}

variable "language" {
  description = "Spoken language of the call: a file name in src/locales (en-US, fr-FR)"
  type        = string
  default     = "en-US"
  validation {
    condition     = fileexists("${path.root}/../../../src/locales/${var.language}.json")
    error_message = "No src/locales/<language>.json file for this language."
  }
}
variable "log_retention_days" {
  description = "Retention of every log group the sample creates (days)"
  type        = number
  default     = 365
}
variable "deletion_protection" {
  description = "Protect the ticket table from deletion"
  type        = bool
  default     = false
}

variable "prefix" {
  description = "Prefix of every resource name"
  type        = string
  default     = "devops-oncall"
}
variable "agent_space_id" {
  description = "Existing AWS DevOps Agent Agent Space (same Region as the default provider)"
  type        = string
}
variable "create_connect_instance" {
  description = "true: create a new Connect instance and claim a number (modules/connect-instance). false: use connect_instance_id and connect_source_number"
  type        = bool
  default     = false
}
variable "connect_instance_id" {
  description = "Existing Amazon Connect instance (Region of the connect provider). Required when create_connect_instance = false"
  type        = string
  default     = null
  validation {
    condition     = var.create_connect_instance || var.connect_instance_id != null
    error_message = "Set connect_instance_id, or set create_connect_instance = true."
  }
}
variable "connect_source_number" {
  description = "Phone number claimed on the existing Connect instance, used as caller ID (E.164). Required when create_connect_instance = false"
  type        = string
  default     = null
  validation {
    condition     = var.create_connect_instance || var.connect_source_number != null
    error_message = "Set connect_source_number, or set create_connect_instance = true."
  }
}
variable "connect_instance_alias" {
  description = "Alias of the new instance (create_connect_instance = true), unique across AWS"
  type        = string
  default     = null
  validation {
    condition     = !var.create_connect_instance || var.connect_instance_alias != null
    error_message = "Set connect_instance_alias when create_connect_instance = true."
  }
}
variable "connect_phone_country_code" {
  description = "Country of the number claimed on the new instance (create_connect_instance = true)"
  type        = string
  default     = "US"
}
variable "connect_phone_type" {
  description = "DID or TOLL_FREE, number claimed on the new instance (create_connect_instance = true)"
  type        = string
  default     = "DID"
}
variable "enable_replay" {
  description = "Create the replay rules for scripts/replay.py. Default false. When true, any principal with events:PutEvents on the default bus can trigger a real call once dry_run = false, so enable it only for a demo and turn it back off afterwards."
  type        = bool
  default     = false
}
variable "dry_run" {
  description = "true: the caller logs the StartOutboundVoiceContact request without placing the call"
  type        = bool
  default     = true
}
variable "sms_origination_identity_arn" {
  description = "End User Messaging SMS phone number, pool or sender ID ARN for the no-answer SMS. null = no SMS"
  type        = string
  default     = null
}
variable "notify_email_from" {
  description = "SES verified sender address (not a bare domain) for the no-answer email. null = no email. Recipient: SSM SecureString output oncall.oncall_email_param, set outside IaC"
  type        = string
  default     = null
  validation {
    condition     = var.notify_email_from == null || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.notify_email_from))
    error_message = "notify_email_from must be an email address (SES FromEmailAddress), not a bare domain."
  }
}
variable "gate_timeout_seconds" {
  description = "Seconds the call waits for 1 after the opening announcement"
  type        = number
  default     = 8
}
variable "gate_retry_timeout_seconds" {
  description = "Seconds the call waits for 1 after the short second prompt"
  type        = number
  default     = 5
}

# One customer managed key per Region (rotation on).
module "kms" {
  source = "../../modules/kms"
  prefix = var.prefix
}

module "kms_connect" {
  source       = "../../modules/kms"
  providers    = { aws = aws.connect }
  prefix       = var.prefix
  alias_suffix = "-connect"
  # Flow log group of a new instance (modules/connect-instance).
  extra_log_groups = var.create_connect_instance ? ["/aws/connect/${var.connect_instance_alias}"] : []
}

# Optional new Connect instance. With an existing instance, this module is skipped and the oncall
# module gets connect_instance_id / connect_source_number directly.
module "connect" {
  count              = var.create_connect_instance ? 1 : 0
  source             = "../../modules/connect-instance"
  providers          = { aws = aws.connect }
  prefix             = var.prefix
  kms_key_arn        = module.kms_connect.key_arn
  log_retention_days = var.log_retention_days
  instance_alias     = var.connect_instance_alias
  phone_country_code = var.connect_phone_country_code
  phone_type         = var.connect_phone_type
  force_destroy      = var.recordings_force_destroy
}

locals {
  connect_instance_id   = var.create_connect_instance ? module.connect[0].instance_id : var.connect_instance_id
  connect_source_number = var.create_connect_instance ? module.connect[0].phone_number : var.connect_source_number
}

module "foundation" {
  source              = "../../modules/foundation"
  prefix              = var.prefix
  agent_space_id      = var.agent_space_id
  src_root            = local.src_root
  deletion_protection = var.deletion_protection
  kms_key_arn         = module.kms.key_arn
  log_retention_days  = var.log_retention_days
  # Roles created by modules oncall (caller.tf, handler.tf) and mcp-ticketing: the only ticket writers
  # besides the opener. Keep in sync with their `name`; tests/test_resource_policies.py checks it.
  ticket_writer_role_names = ["${var.prefix}-caller", "${var.prefix}-connect-handler", "${var.prefix}-mcp-runtime"]
  deployer_principal_arns  = local.deployer_principal_arns
}

module "mcp" {
  source              = "../../modules/mcp-ticketing"
  prefix              = var.prefix
  agent_space_id      = var.agent_space_id
  tickets_table_name  = module.foundation.tickets_table_name
  tickets_table_arn   = module.foundation.tickets_table_arn
  mcp_zip_path        = "${local.src_root}/build/mcp_server.zip"
  aws_profile         = var.aws_profile
  tickets_kms_key_arn = module.kms.key_arn
  # Exempted from the runtime and DEFAULT endpoint Deny, besides the DevOps Agent SigV4 role.
  deployer_principal_arns = local.deployer_principal_arns
  narrow_devops_trust     = var.narrow_devops_trust
  tool_classification = {
    list_open_tickets = "READ_ONLY"
    get_ticket        = "READ_ONLY"
    add_comment       = "MUTATIVE"
    close_ticket      = "MUTATIVE"
  }
}

module "oncall" {
  source                = "../../modules/oncall"
  providers             = { aws = aws, aws.connect = aws.connect, awscc = awscc, awscc.connect = awscc.connect }
  prefix                = var.prefix
  src_root              = local.src_root
  agent_space_arn       = module.foundation.agent_space_arn
  tickets_table_name    = module.foundation.tickets_table_name
  tickets_table_arn     = module.foundation.tickets_table_arn
  boto3_layer_arn       = module.foundation.boto3_layer_arn
  connect_instance_id   = local.connect_instance_id
  connect_source_number = local.connect_source_number
  dry_run               = var.dry_run
  enable_replay         = var.enable_replay
  locale                = local.locale
  kms_key_arn           = module.kms.key_arn
  connect_kms_key_arn   = module.kms_connect.key_arn
  log_retention_days    = var.log_retention_days

  sms_origination_identity_arn = var.sms_origination_identity_arn
  notify_email_from            = var.notify_email_from
  gate_timeout_seconds         = var.gate_timeout_seconds
  gate_retry_timeout_seconds   = var.gate_retry_timeout_seconds
  enable_call_recording        = var.enable_call_recording
  enable_flow_logging          = var.enable_flow_logging
  enable_ai_guardrail          = var.enable_ai_guardrail
  enable_pin                   = var.enable_pin
  pin_max_digits               = var.pin_max_digits
  pin_timeout_seconds          = var.pin_timeout_seconds
  allow_live_without_pin       = var.allow_live_without_pin
  ack_confirm_timeout_seconds  = var.ack_confirm_timeout_seconds
  deployer_principal_arns      = local.deployer_principal_arns
  caller_reserved_concurrency  = var.caller_reserved_concurrency
}

output "connect" {
  # Hides the claimed source number from routine CLI output. It is not the destination secret,
  # but there is no reason to print a phone number on every apply.
  sensitive = true
  value = {
    created       = var.create_connect_instance
    instance_id   = local.connect_instance_id
    source_number = local.connect_source_number
  }
}
output "foundation" { value = module.foundation }
output "mcp" { value = module.mcp }
output "oncall" { value = module.oncall }

output "language" { value = var.language }
