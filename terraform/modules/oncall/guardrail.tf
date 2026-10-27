# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# AWS::Wisdom::AIGuardrail for the orchestration AI agent (awscc_wisdom_ai_guardrail).
#
# Attached to the agent through orchestration_ai_agent_configuration.orchestration_ai_guardrail_id
# (see agent.tf). Toggleable with var.enable_ai_guardrail (default true) so an environment that
# manages a guardrail centrally can turn the sample's own off.
#
# Config chosen for a voice on-call assistant that reads an untrusted investigation summary and
# speaks short answers over the phone (threat model T4):
#   - content policy: PROMPT_ATTACK filter at HIGH on the callee's input (prompt injection by the
#     callee; the summary sits in the prompt, scanning it is not documented), and the harmful-content filters (hate, insults, sexual, violence,
#     misconduct) on both directions so a steered model cannot speak abusive content.
#     PROMPT_ATTACK output strength is NONE: the filter only classifies model input, so the schema
#     and Bedrock reject a non-NONE output strength for that type.
#   - no sensitive information policy (see the note in the resource below).
#   - blocked input/output messaging: a short line in the call's language, so a blocked turn still
#     sounds like the assistant rather than a raw service error.
# Docs:
#   https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-wisdom-aiguardrail.html
#   https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-wisdom-aiguardrail-guardrailcontentfilterconfig.html
#   https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-wisdom-aiguardrail-guardrailpiientityconfig.html

locals {
  # Blocked messages keyed by language. agent.tf already receives the decoded locale object; the
  # locale JSON has no guardrail key and src/locales is owned elsewhere, so the two short strings
  # live here keyed on var.locale.connect_language, with en-US as the fallback.
  guardrail_blocked_messaging = {
    "en-US" = {
      input  = "Sorry, I can't help with that. The incident details are in the ticket."
      output = "Sorry, I can't say that. The incident details are in the ticket."
    }
    "fr-FR" = {
      input  = "Désolé, je ne peux pas traiter cette demande. Les détails de l'incident sont dans le ticket."
      output = "Désolé, je ne peux pas répondre à cela. Les détails de l'incident sont dans le ticket."
    }
  }
  guardrail_messaging = lookup(local.guardrail_blocked_messaging, var.locale.connect_language, local.guardrail_blocked_messaging["en-US"])
}

resource "awscc_wisdom_ai_guardrail" "orchestration" {
  count        = var.enable_ai_guardrail ? 1 : 0
  provider     = awscc.connect
  assistant_id = awscc_wisdom_assistant.this.assistant_id
  name         = "${var.prefix}-orchestration"
  description  = "Prompt-attack and harmful-content guardrail for the voice on-call AI agent"

  blocked_input_messaging   = local.guardrail_messaging.input
  blocked_outputs_messaging = local.guardrail_messaging.output

  content_policy_config = {
    filters_config = [
      # Prompt injection by the callee (scanning the summary, which sits in the prompt, is not
      # documented). Output strength NONE: the PROMPT_ATTACK filter applies to input only.
      { type = "PROMPT_ATTACK", input_strength = "HIGH", output_strength = "NONE" },
      { type = "HATE", input_strength = "MEDIUM", output_strength = "MEDIUM" },
      { type = "INSULTS", input_strength = "MEDIUM", output_strength = "MEDIUM" },
      { type = "SEXUAL", input_strength = "MEDIUM", output_strength = "MEDIUM" },
      { type = "VIOLENCE", input_strength = "MEDIUM", output_strength = "MEDIUM" },
      { type = "MISCONDUCT", input_strength = "MEDIUM", output_strength = "MEDIUM" },
    ]
  }

  # No sensitive_information_policy_config: the AWS::Wisdom::AIGuardrail resource handler (Cloud
  # Control) fails with GeneralServiceException as soon as a PII entity is set, while the same config
  # is accepted by the QConnect CreateAIGuardrail API. So the guardrail has content filters only, and
  # the investigation summary is NOT filtered for personal data: it is written by the DevOps Agent
  # model from logs and metrics, and the sanitizer only strips delimiters, normalizes and caps it.

}

# Pin a version so the agent references an immutable guardrail (same pattern as the AI prompt). No
# modified_time_seconds: awscc plans the version with the pre-update value and QConnect rejects it
# ("Modified time did not match"). Without it the version snapshots $LATEST at creation, and
# replace_triggered_by cuts a new version in the same apply whenever the guardrail changes.
resource "awscc_wisdom_ai_guardrail_version" "orchestration" {
  count           = var.enable_ai_guardrail ? 1 : 0
  provider        = awscc.connect
  assistant_id    = awscc_wisdom_assistant.this.assistant_id
  ai_guardrail_id = awscc_wisdom_ai_guardrail.orchestration[0].ai_guardrail_id
  lifecycle { replace_triggered_by = [awscc_wisdom_ai_guardrail.orchestration[0]] }
}

locals {
  # The agent field takes "<guardrailId>:<versionNumber>" to pin an immutable version, mirroring the
  # prompt id. null when the guardrail is disabled, which omits the optional agent field.
  orchestration_ai_guardrail_id = var.enable_ai_guardrail ? "${awscc_wisdom_ai_guardrail.orchestration[0].ai_guardrail_id}:${awscc_wisdom_ai_guardrail_version.orchestration[0].version_number}" : null
}
