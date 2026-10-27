# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Lex V2 bot that carries the AI agent (built-in intent AMAZON.QInConnectIntent).
# Provider gap: neither hashicorp/aws nor awscc 1.104 expose QInConnectIntentConfiguration, while
# the CloudFormation type AWS::Lex::Bot does. Terraform therefore drives a minimal CloudFormation
# stack (same plan/apply/destroy lifecycle).

# Trust: aws:SourceAccount only. The Lex V2 service-role docs (using-service-roles.html,
# create-iam-test-set.html) show a trust with no aws:SourceArn and do not document which ARN (bot or
# bot alias) Lex passes when it assumes the role, so a SourceArn condition could stop the bot from
# assuming it at runtime. Left at SourceAccount until AWS documents the key.
resource "aws_iam_role" "lex" {
  provider = aws.connect
  name     = "${var.prefix}-lex-bot"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lexv2.amazonaws.com" },
    Condition = { StringEquals = { "aws:SourceAccount" = local.account } } }]
  })
}

locals {
  # Minimal Amazon Q in Connect (wisdom:) actions the AMAZON.QInConnectIntent runtime exercises.
  # Grouped by the resource type each one acts on, so each stays scoped to this assistant's ARNs. Action names and resource types are from the AWS Service
  # Authorization Reference for Amazon Q in Connect ("Actions defined by Amazon Q in Connect").
  #
  # On the assistant ARN:
  #   GetAssistant                 - Lex reads the assistant config when it opens the intent;
  #                                  the missing-permission error names "your Q In Connect Assistant".
  #   QueryAssistant               - the agent queries the assistant during the turn.
  #   GetRecommendations           - the AI agent reads its recommendations/responses.
  #   NotifyRecommendationsReceived- acknowledges recommendations so they are not re-served.
  #   SendMessage / GetNextMessage - the message-based turn API the Connect AI agent uses.
  # On the session ARN:
  #   GetSession / UpdateSessionData - read and update the per-contact assistant session
  #                                    (CreateSession itself has no resource type, see below).
  # On the ai-agent / ai-prompt / ai-guardrail ARNs:
  #   GetAIAgent / GetAIPrompt / GetAIGuardrail - resolve the AI agent and the prompt/guardrail it
  #                                    references so the configured orchestration runs.
  wisdom_actions_by_resource = {
    assistant = ["wisdom:GetAssistant", "wisdom:QueryAssistant"]
    # GetRecommendations, NotifyRecommendationsReceived, SendMessage and GetNextMessage have the
    # Session resource type (Service Authorization Reference), so they are granted on sessions.
    session      = ["wisdom:GetSession", "wisdom:UpdateSessionData", "wisdom:GetRecommendations", "wisdom:NotifyRecommendationsReceived", "wisdom:SendMessage", "wisdom:GetNextMessage"]
    ai-agent     = ["wisdom:GetAIAgent"]
    ai-prompt    = ["wisdom:GetAIPrompt"]
    ai-guardrail = ["wisdom:GetAIGuardrail"]
  }
}

resource "aws_iam_role_policy" "lex" {
  provider = aws.connect
  role     = aws_iam_role.lex.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      # polly:SynthesizeSpeech has no resource type in the Service Authorization Reference (it acts on
      # a voice, not an account resource), so Resource "*" is required. Lex speaks the bot's replies.
      [{ Effect = "Allow", Action = ["polly:SynthesizeSpeech"], Resource = "*" }],
      # One statement per resource type, each scoped to this assistant's ARN(s). The assistant ARN
      # is used directly; the dependent resources hang under the assistant id.
      [for t, actions in local.wisdom_actions_by_resource : {
        Effect   = "Allow"
        Action   = actions
        Resource = t == "assistant" ? awscc_wisdom_assistant.this.assistant_arn : "arn:aws:wisdom:${local.connect_region}:${local.account}:${t}/${awscc_wisdom_assistant.this.assistant_id}/*"
      }],
      # Actions without a resource type (Service Authorization Reference): Resource "*" is required.
      # List* lets the runtime discover the assistant's agents/prompts/guardrails; CreateSession and
      # SearchSessions open and find the per-contact session.
      [{ Effect = "Allow", Action = ["wisdom:ListAssistants", "wisdom:ListAIAgents", "wisdom:ListAIPrompts", "wisdom:ListAIGuardrails", "wisdom:ListAssistantAssociations", "wisdom:CreateSession", "wisdom:SearchSessions"], Resource = "*" }],
    )
  })
}

locals {
  lex_intents = [
    {
      Name                  = "OncallAIAgent"
      ParentIntentSignature = "AMAZON.QInConnectIntent"
      QInConnectIntentConfiguration = {
        QInConnectAssistantConfiguration = { AssistantArn = awscc_wisdom_assistant.this.assistant_arn }
      }
    },
    { Name = "FallbackIntent", ParentIntentSignature = "AMAZON.FallbackIntent" },
  ]
}

resource "aws_cloudformation_stack" "lex" {
  #checkov:skip=CKV_AWS_124:Terraform owns the stack lifecycle; SNS notifications would duplicate it
  provider = aws.connect
  name     = "${var.prefix}-lex-bot"
  template_body = jsonencode({
    AWSTemplateFormatVersion = "2010-09-09"
    Description              = "Lex V2 bot carrying the Connect AI agent (DevOps Agent on-call)"
    Resources = {
      Bot = {
        Type = "AWS::Lex::Bot"
        Properties = {
          Name                    = "${var.prefix}-bot"
          RoleArn                 = aws_iam_role.lex.arn
          DataPrivacy             = { ChildDirected = false }
          IdleSessionTTLInSeconds = 300
          AutoBuildBotLocales     = true
          # Connect only lists and manages a bot in its admin website (Conversational AI, flow designer)
          # when the bot carries this tag. Bots created in the Connect console get it automatically; bots
          # created by API, CloudFormation or Terraform do not.
          BotTags = [{ Key = "AmazonConnectEnabled", Value = "True" }]
          BotLocales = [{
            LocaleId               = var.locale.lex_locale
            NluConfidenceThreshold = 0.4
            VoiceSettings          = { VoiceId = var.locale.voice, Engine = "neural" }
            Intents                = local.lex_intents
          }]
        }
      }
      Version = {
        Type = "AWS::Lex::BotVersion"
        Properties = {
          BotId = { Ref = "Bot" }
          # A bot update only changes DRAFT: without a new version the alias keeps serving the old one
          # (seen when the assistant was recreated). The config hash forces a new version on change.
          Description = "config ${substr(sha1(jsonencode([awscc_wisdom_assistant.this.assistant_arn, aws_iam_role.lex.arn, local.lex_intents, var.locale.lex_locale, var.locale.voice])), 0, 12)}"
          BotVersionLocaleSpecification = [{
            LocaleId                = var.locale.lex_locale
            BotVersionLocaleDetails = { SourceBotVersion = "DRAFT" }
          }]
        }
      }
      Alias = {
        Type = "AWS::Lex::BotAlias"
        Properties = {
          BotId                  = { Ref = "Bot" }
          BotAliasName           = "live"
          BotVersion             = { "Fn::GetAtt" = ["Version", "BotVersion"] }
          BotAliasLocaleSettings = [{ LocaleId = var.locale.lex_locale, BotAliasLocaleSetting = { Enabled = true } }]
        }
      }
    }
    Outputs = {
      BotId    = { Value = { Ref = "Bot" } }
      AliasArn = { Value = { "Fn::GetAtt" = ["Alias", "Arn"] } }
    }
  })
  depends_on = [aws_iam_role_policy.lex]
}

resource "awscc_connect_integration_association" "lex" {
  provider         = awscc.connect
  instance_id      = local.connect_arn
  integration_type = "LEX_BOT"
  integration_arn  = aws_cloudformation_stack.lex.outputs["AliasArn"]
}
