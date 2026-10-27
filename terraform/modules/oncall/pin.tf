# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Optional recipient (PIN) gate. When enable_pin is true the flow asks for a DTMF PIN after
# "press 1" and before the AI session; connect-handler verifies it (constant-time) against this
# SecureString. The value is set OUTSIDE IaC (like the on-call number), so it is never a Terraform
# input or a Lambda environment variable. When enable_pin is false none of this is created and the
# rendered flow has no PIN step (the render tests prove both).
#
# PIN digits travel to the handler ONLY as a Lambda invocation parameter ($.StoredCustomerInput ->
# "pin"), never as a contact attribute. Amazon Connect flow logging, when enabled, records Invoke
# Lambda parameters, so a logged flow would capture the entered digits: keep enable_flow_logging =
# false whenever enable_pin = true. The handler never logs the PIN or the entered digits.

variable "enable_pin" {
  description = "Optional recipient gate: when true the flow asks the callee for a DTMF PIN after they press 1 and before the AI session, verified by connect-handler against the oncall_pin_param SecureString (set outside IaC). Default false: no PIN step. When true, keep enable_flow_logging = false so the entered digits are not recorded in flow logs."
  type        = bool
  default     = false
  # Flow logs record Invoke Lambda parameters, i.e. the entered PIN digits.
  validation {
    condition     = !(var.enable_pin && var.enable_flow_logging)
    error_message = "enable_pin = true and enable_flow_logging = true are refused together: flow logs would record the entered PIN digits."
  }
}

variable "pin_max_digits" {
  description = "Maximum number of DTMF digits accepted for the on-call PIN (enable_pin = true)"
  type        = number
  default     = 6
  validation {
    condition     = var.pin_max_digits >= 3 && var.pin_max_digits <= 20
    error_message = "pin_max_digits must be between 3 and 20."
  }
}

variable "pin_timeout_seconds" {
  description = "Seconds the flow waits for the PIN entry on each of the two attempts (enable_pin = true)"
  type        = number
  default     = 10
  validation {
    condition     = var.pin_timeout_seconds >= 1 && var.pin_timeout_seconds <= 180
    error_message = "Get customer input accepts 1 to 180 seconds."
  }
}

# On-call PIN: SecureString created with a placeholder only when enable_pin is true; real value set
# outside IaC, like the on-call number. Terraform reads it back into state on refresh: treat state as
# sensitive. The handler fails closed on the "UNSET" placeholder (verify_pin returns result=no).
resource "aws_ssm_parameter" "oncall_pin" {
  count       = var.enable_pin ? 1 : 0
  provider    = aws.connect
  name        = "/${var.prefix}/oncall-pin"
  type        = "SecureString"
  key_id      = var.connect_kms_key_arn
  value       = "UNSET"
  description = "On-call PIN for the optional recipient gate (set outside IaC)"
  lifecycle { ignore_changes = [value] }
}

output "oncall_pin_param" { value = var.enable_pin ? aws_ssm_parameter.oncall_pin[0].name : null }
