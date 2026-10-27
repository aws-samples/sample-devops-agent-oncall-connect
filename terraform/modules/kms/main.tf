# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Customer managed KMS key for one Region: CloudWatch log groups, Lambda environment variables,
# the ticket table, SQS queues and the on-call number SecureString.
# IAM principals get key permissions through their own identity policies (root statement below).

terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "prefix" { type = string }
variable "alias_suffix" {
  type    = string
  default = ""
}
variable "extra_log_groups" {
  description = "Log group names outside /aws/lambda/<prefix>-* and /aws/events/<prefix>-* that CloudWatch Logs may encrypt with this key"
  type        = list(string)
  default     = []
}

data "aws_caller_identity" "me" {}
data "aws_region" "here" {}
data "aws_partition" "here" {}

locals {
  account   = data.aws_caller_identity.me.account_id
  region    = data.aws_region.here.region
  partition = data.aws_partition.here.partition
  log_arn   = "arn:${local.partition}:logs:${local.region}:${local.account}:log-group"
}

resource "aws_kms_key" "this" {
  description             = "${var.prefix}: logs, Lambda environment, tickets, queues, on-call number"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # AWS default key policy: the account root delegates key use to IAM. In a key policy,
        # Resource "*" means this key. The account's identity policies decide who else can use it;
        # the sample's own roles use it only through the services they call (kms:ViaService).
        Sid       = "AccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        # Only the log groups this sample creates (Resource "*" in a key policy means this key).
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = { "kms:EncryptionContext:aws:logs:arn" = concat([
            "${local.log_arn}:/aws/lambda/${var.prefix}-*",
            "${local.log_arn}:/aws/events/${var.prefix}-*",
          ], [for g in var.extra_log_groups : "${local.log_arn}:${g}"]) }
        }
      },
      {
        # EventBridge encrypts messages it delivers to this account's SQS queues with this key:
        # the unanswered-call target queue and the dead-letter queues. Scoped to the sample's own
        # rules: the SQS key-management doc lists CloudWatch Events among the sources that
        # support aws:SourceArn for SSE queues. Every rule of the sample is named <prefix>-* (on the
        # replay bus the rule ARN is rule/<prefix>-replay/<prefix>-..., matched by the same pattern).
        Sid       = "EventBridgeToEncryptedSqs"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource  = "*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account }
          ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:events:${local.region}:${local.account}:rule/${var.prefix}-*" }
        }
      },
    ]
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.prefix}-${local.region}${var.alias_suffix}"
  target_key_id = aws_kms_key.this.key_id
}

output "key_arn" { value = aws_kms_key.this.arn }
