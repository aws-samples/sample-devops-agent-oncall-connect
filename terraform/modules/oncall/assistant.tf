# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Connect AI agents assistant (formerly Amazon Q in Connect). One WISDOM_ASSISTANT integration per instance.

resource "awscc_wisdom_assistant" "this" {
  provider    = awscc.connect
  name        = "${var.prefix}-assistant"
  type        = "AGENT"
  description = "AI agents assistant for the DevOps Agent on-call call"
}

resource "awscc_connect_integration_association" "assistant" {
  provider         = awscc.connect
  instance_id      = local.connect_arn
  integration_type = "WISDOM_ASSISTANT"
  integration_arn  = awscc_wisdom_assistant.this.assistant_arn
}
