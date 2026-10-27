# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Connect Region: contact flow. Optional flow logging and recording (with a spoken notice), "press 1" gate (asked twice), an optional DTMF PIN gate (enable_pin), then AI agent session,
# init Lambda, Lex bot carrying the AI agent, and acknowledgment in the ticket when the agent returns
# the Acknowledge tool AND the callee presses 1 to confirm (otherwise the call ends, the ticket stays
# OPEN and the contact-event path sends the normal SMS/email). 1 not pressed (voicemail, answered without pressing 1): connect-handler
# "notify" marks the ticket and sends the configured SMS/email; the closing message depends on what
# was actually sent ($.External.sent). A call that is never answered never enters the flow: the
# contact-event rule in handler.tf covers it.

resource "aws_connect_contact_flow" "oncall" {
  provider    = aws.connect
  instance_id = var.connect_instance_id
  name        = "${var.prefix}-ai-agent"
  type        = "CONTACT_FLOW"
  description = "On-call call for AWS DevOps Agent investigations: Connect AI agent (voice)"
  content = templatefile("${var.src_root}/src/connect_flows/ai_agent.json.tftpl", {
    messages                   = var.locale.messages
    voice                      = var.locale.voice
    connect_language           = var.locale.connect_language
    handler_arn                = aws_lambda_function.handler.arn
    assistant_arn              = awscc_wisdom_assistant.this.assistant_arn
    lex_alias_arn              = aws_cloudformation_stack.lex.outputs["AliasArn"]
    ai_agent_arn               = local.ai_agent_versioned_arn
    gate_timeout_seconds       = var.gate_timeout_seconds
    gate_retry_timeout_seconds = var.gate_retry_timeout_seconds
    enable_call_recording      = var.enable_call_recording
    enable_flow_logging        = var.enable_flow_logging
    enable_pin                 = var.enable_pin
    pin_max_digits             = var.pin_max_digits
    pin_timeout_seconds        = var.pin_timeout_seconds
    # DTMF confirmation after the AI agent's Acknowledge.
    ack_confirm_timeout_seconds = var.ack_confirm_timeout_seconds
  })
  depends_on = [
    aws_connect_lambda_function_association.handler,
    awscc_connect_integration_association.lex,
    awscc_connect_integration_association.assistant,
  ]
}
