# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Caller/privacy additions. Other demo variables are declared in main.tf.

variable "deployer_principal_arns" {
  description = "IAM principal ARNs (role ARN without session, or IAM user ARN) exempted from the explicit Denys on the MCP runtime and its DEFAULT endpoint, the ticket table writes and the replay bus, so table and runtime administration and the operator's scripts keep working. null (default) = the role or user running Terraform (aws_iam_session_context issuer). Set it explicitly when a CI role deploys and people run the scripts, or the policies follow whoever ran the last apply."
  type        = list(string)
  default     = null
  validation {
    condition     = try(length(var.deployer_principal_arns) > 0, true)
    error_message = "deployer_principal_arns must be null or list at least one ARN."
  }
}

variable "narrow_devops_trust" {
  description = "Narrow the DevOps Agent SigV4 role trust to this MCP server registration (service/<id>) after it is created and classified. Default false (the documented service/* trust, accepted risk). Set true only after a spike confirms the service id in aws:SourceArn is stable"
  type        = bool
  default     = false
}

variable "caller_reserved_concurrency" {
  description = "Reserved concurrency of the caller Lambda: 1 (default) = one on-call call placed at a time. -1 = unreserved, needed on accounts whose Lambda concurrency quota is still 10 (new accounts): Lambda keeps 10 unreserved, so any reservation fails there until the quota is raised."
  type        = number
  default     = 1
}

variable "budget_alert_email" {
  description = "Email that receives an AWS Budgets alert on the account's monthly cost (actual > 80% or forecast > 100% of budget_limit_usd). Empty (default) = no budget created."
  type        = string
  default     = ""
  validation {
    condition     = var.budget_alert_email == "" || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be empty or an email address."
  }
}

variable "budget_limit_usd" {
  description = "Monthly cost budget in USD for the budget alert (budget_alert_email set)"
  type        = number
  default     = 50
}

variable "recordings_force_destroy" {
  description = "create_connect_instance = true: let terraform destroy delete the recordings bucket with its recordings. Default false: empty the bucket before destroy, or set true for a throwaway demo."
  type        = bool
  default     = false
}

variable "enable_call_recording" {
  description = "Record the on-call call. Default false: recording is opt-in. When true the flow plays a short recording notice before recording starts. Tell participants and check your recording and retention obligations before enabling it outside a demo."
  type        = bool
  default     = false
}

variable "enable_flow_logging" {
  description = "Enable Amazon Connect flow logging for the on-call contact flow. Default false: flow logs can record the parameters passed to Invoke Lambda blocks (ticket id, service, priority, status, and the entered PIN when enable_pin is true; the investigation summary is not passed, the handler reads it from the ticket), so logging is opt-in for privacy. Enable it only when you need to debug the flow and your logging destination and retention are acceptable, and keep it off while enable_pin is true."
  type        = bool
  default     = false
}

variable "enable_ai_guardrail" {
  description = "Attach an AWS::Wisdom::AIGuardrail to the Connect orchestration AI agent (prompt-attack filter, harmful-content filters). Default true. Set false only if a guardrail is managed centrally for the Connect assistant."
  type        = bool
  default     = true
}

variable "enable_pin" {
  description = "Optional recipient gate: when true the on-call call asks the callee for a DTMF PIN after they press 1 and before the AI session. The PIN is a SecureString (output oncall.oncall_pin_param) set outside IaC, verified by connect-handler. Default false: no PIN step (whoever answers hears the summary after pressing 1). Keep enable_flow_logging = false whenever this is true, so the entered digits are not recorded in flow logs."
  type        = bool
  default     = false
}

variable "pin_max_digits" {
  description = "Maximum DTMF digits accepted for the on-call PIN (enable_pin = true)"
  type        = number
  default     = 6
}

variable "pin_timeout_seconds" {
  description = "Seconds the call waits for the PIN entry on each of the two attempts (enable_pin = true)"
  type        = number
  default     = 10
}

variable "allow_live_without_pin" {
  description = "Override of the live-call guard. By default dry_run = false requires enable_pin = true (recipient PIN gate before the AI agent), and enable_pin = true refuses enable_flow_logging = true. Set true to place live calls WITHOUT the PIN gate, only for a sandbox demo where you are the only callee. Default false."
  type        = bool
  default     = false
}

variable "ack_confirm_timeout_seconds" {
  description = "Seconds the call waits for key 1 after the AI agent's acknowledgment (to confirm you take incident X, press 1). No 1: not acknowledged, the ticket stays open and the written notification goes out."
  type        = number
  default     = 5
}
