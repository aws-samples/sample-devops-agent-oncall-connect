# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Caller/privacy additions. Other oncall variables are declared in main.tf.

variable "deployer_principal_arns" {
  description = "IAM principal ARNs of the deployer and the operator's scripts. Only they may publish to the replay bus (enable_replay = true). At least one"
  type        = list(string)
  validation {
    condition     = length(var.deployer_principal_arns) > 0
    error_message = "deployer_principal_arns needs at least one ARN."
  }
}

variable "caller_reserved_concurrency" {
  description = "Reserved concurrency of the caller Lambda: 1 = one call placed at a time. -1 = unreserved, for accounts whose concurrency quota leaves no room to reserve (new accounts often have 10, and Lambda keeps 10 unreserved, so any reservation fails there until the quota is raised)"
  type        = number
  default     = 1
  validation {
    condition     = var.caller_reserved_concurrency == -1 || var.caller_reserved_concurrency >= 1
    error_message = "caller_reserved_concurrency must be -1 (unreserved) or >= 1 (0 would block every call)."
  }
}

variable "enable_call_recording" {
  description = "Record the on-call call (IVR recording in the contact flow). Default false: recording is opt-in. When true the flow plays a short recording notice (locale key recording_notice) before it starts recording. Recording also needs a CALL_RECORDINGS storage config on the instance (a new instance gets one). Check your recording and retention obligations before enabling it."
  type        = bool
  default     = false
}

variable "enable_flow_logging" {
  description = "Enable Amazon Connect flow logging for the on-call contact flow. Default false: flow logs can record the parameters passed to Invoke Lambda blocks (ticket id, service, priority, status; the investigation summary is not passed, the handler reads it from the ticket), so logging is opt-in for privacy (threat model T5b). When true the flow turns logging on at its start; when false the flow starts straight at the language step and never enables logging."
  type        = bool
  default     = false
}

variable "enable_ai_guardrail" {
  description = "Attach an AWS::Wisdom::AIGuardrail (awscc_wisdom_ai_guardrail) to the orchestration AI agent: a PROMPT_ATTACK input filter (HIGH), the harmful-content filters (threat model T4). Default true. Set false only if a guardrail is managed centrally for the Connect assistant."
  type        = bool
  default     = true
}

variable "ack_confirm_timeout_seconds" {
  description = "Seconds the flow waits for key 1 after the AI agent's Acknowledge (to confirm you take incident X, press 1). No 1 in time: not acknowledged, the ticket stays OPEN and the normal SMS/email goes out."
  type        = number
  default     = 5
  validation {
    condition     = var.ack_confirm_timeout_seconds >= 1 && var.ack_confirm_timeout_seconds <= 180
    error_message = "Get customer input accepts 1 to 180 seconds."
  }
}

# Live calls to real staff need the recipient (PIN) gate, and the PIN must never meet
# flow logging (flow logs record Invoke Lambda parameters, i.e. the entered digits). Cross-variable
# validations (Terraform >= 1.9; the PIN + flow logging one is on enable_pin in pin.tf).
variable "allow_live_without_pin" {
  description = "Explicit override: allow dry_run = false with enable_pin = false (live calls without the recipient PIN gate). Default false. Set true only for a sandbox demo where you are the only callee."
  type        = bool
  default     = false
  validation {
    condition     = var.dry_run || var.enable_pin || var.allow_live_without_pin
    error_message = "dry_run = false needs enable_pin = true (recipient gate). Set allow_live_without_pin = true to override for a sandbox demo."
  }
}
