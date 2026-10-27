# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Optional: a new Amazon Connect instance with a claimed phone number, for deployers who do not
# already have one. Pass the Connect Region provider as `aws`. The oncall module only needs the
# two outputs (instance_id, phone_number), so an existing instance works the same way.
#
# Also created: the flow log group (/aws/connect/<alias>, with retention; Connect would otherwise
# create it without retention and leave it behind after destroy) and the call recording storage
# (S3 bucket, KMS), so the recording the flow turns on is kept. Not configured: users, queues and
# routing profiles, which the on-call call does not need. AI agents are enabled by the oncall module
# (assistant + WISDOM_ASSISTANT integration).

terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "instance_alias" {
  description = "Instance alias, also the access URL (<alias>.my.connect.aws): unique across AWS"
  type        = string
  validation {
    condition     = can(regex("^[a-zA-Z0-9]([a-zA-Z0-9-]{0,43}[a-zA-Z0-9])?$", var.instance_alias))
    error_message = "1 to 45 letters, digits or hyphens, not starting or ending with a hyphen."
  }
}
variable "phone_country_code" {
  description = "ISO country code of the claimed number (US, GB, FR...). Some countries require regulatory documents first"
  type        = string
  default     = "US"
}
variable "phone_type" {
  description = "Type of the claimed number: DID or TOLL_FREE"
  type        = string
  default     = "DID"
  validation {
    condition     = contains(["DID", "TOLL_FREE"], var.phone_type)
    error_message = "DID or TOLL_FREE."
  }
}
variable "prefix" {
  description = "Resource name prefix (recording bucket)"
  type        = string
}
variable "kms_key_arn" {
  description = "KMS key of the Connect Region: flow log group and call recordings"
  type        = string
}
variable "log_retention_days" {
  description = "Retention of the flow log group (also the recordings bucket lifecycle, so >= 1)"
  type        = number
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be a CloudWatch Logs retention value and >= 1 (the recordings bucket lifecycle needs a positive expiration): 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653."
  }
}
variable "force_destroy" {
  description = "Let terraform destroy delete the recordings bucket with the recordings in it. Default false: empty the bucket first, or set true for a throwaway demo"
  type        = bool
  default     = false
}
variable "phone_prefix" {
  description = "Optional E.164 prefix of the claimed number (for example +1206), null for any"
  type        = string
  default     = null
}

# Connect writes flow logs to /aws/connect/<alias>: created here first so it has a retention and a
# key, and goes away with terraform destroy.
resource "aws_cloudwatch_log_group" "flows" {
  name              = "/aws/connect/${var.instance_alias}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_connect_instance" "this" {
  instance_alias           = var.instance_alias
  identity_management_type = "CONNECT_MANAGED"
  # Outbound only: the sample never receives inbound calls (the on-call call is placed with
  # StartOutboundVoiceContact and the backstop reacts to API-initiated contact events, not to
  # inbound traffic). Leaving inbound on would expose the claimed number to inbound calls and cost.
  # inbound_calls_enabled and outbound_calls_enabled are independent booleans on the AWS
  # CreateInstance API; an outbound-only instance (inbound false, outbound true) is a supported
  # configuration (AWS CreateInstance API reference, InboundCallsEnabled/OutboundCallsEnabled).
  inbound_calls_enabled     = false
  outbound_calls_enabled    = true # StartOutboundVoiceContact
  contact_flow_logs_enabled = true # lets the flow log when enable_flow_logging = true
  contact_lens_enabled      = false
  depends_on                = [aws_cloudwatch_log_group.flows]
}

# Call recordings (used when enable_call_recording = true). Without a CALL_RECORDINGS storage
# config the call goes on but no recording is stored.
# Versioning is enabled by aws_s3_bucket_versioning.recordings below.
# nosemgrep: aws-s3-bucket-versioning-not-enabled
resource "aws_s3_bucket" "recordings" {
  #checkov:skip=CKV_AWS_18:Demo recordings; enable CloudTrail S3 data events for object-level audit
  #checkov:skip=CKV2_AWS_62:No consumer for events on the recordings bucket
  #checkov:skip=CKV_AWS_144:Demo recordings, cross-Region replication is not needed
  bucket_prefix = "${var.prefix}-recordings-"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_public_access_block" "recordings" {
  bucket                  = aws_s3_bucket.recordings.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "recordings" {
  bucket = aws_s3_bucket.recordings.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "recordings" {
  bucket = aws_s3_bucket.recordings.id
  rule {
    id     = "expire-recordings"
    status = "Enabled"
    filter {}
    expiration { days = var.log_retention_days }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  depends_on = [aws_s3_bucket_versioning.recordings]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "recordings" {
  #checkov:skip=CKV2_AWS_67:Customer managed key from modules/kms, which has rotation on (enable_key_rotation = true); the key arrives as a variable so the check cannot follow it
  bucket = aws_s3_bucket.recordings.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_policy" "recordings_tls_only" {
  bucket = aws_s3_bucket.recordings.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.recordings.arn, "${aws_s3_bucket.recordings.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.recordings]
}

resource "aws_connect_instance_storage_config" "recordings" {
  #checkov:skip=CKV_AWS_269:The check targets Kinesis Video Streams storage; recordings go to S3 encrypted with the sample's CMK (encryption_config below)
  instance_id   = aws_connect_instance.this.id
  resource_type = "CALL_RECORDINGS"
  storage_config {
    storage_type = "S3"
    s3_config {
      bucket_name   = aws_s3_bucket.recordings.id
      bucket_prefix = "recordings"
      encryption_config {
        encryption_type = "KMS"
        key_id          = var.kms_key_arn
      }
    }
  }
  depends_on = [aws_s3_bucket_policy.recordings_tls_only, aws_s3_bucket_server_side_encryption_configuration.recordings]
}

# Claimed numbers are billed per day until released (terraform destroy releases it).
resource "aws_connect_phone_number" "this" {
  target_arn   = aws_connect_instance.this.arn
  country_code = var.phone_country_code
  type         = var.phone_type
  prefix       = var.phone_prefix
  description  = "On-call caller ID (${var.instance_alias})"
}

output "instance_id" { value = aws_connect_instance.this.id }
output "instance_arn" { value = aws_connect_instance.this.arn }
output "phone_number" { value = aws_connect_phone_number.this.phone_number }
output "recordings_bucket" { value = aws_s3_bucket.recordings.id }
