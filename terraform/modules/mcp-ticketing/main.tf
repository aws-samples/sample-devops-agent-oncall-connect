# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Ticketing MCP server on Amazon Bedrock AgentCore Runtime, registered in AWS DevOps Agent (SigV4).

terraform {
  required_providers {
    aws   = { source = "hashicorp/aws" }
    awscc = { source = "hashicorp/awscc" }
    time  = { source = "hashicorp/time" }
  }
}

variable "prefix" { type = string }
variable "agent_space_id" { type = string }
variable "tickets_table_name" { type = string }
variable "tickets_table_arn" { type = string }
variable "mcp_zip_path" { type = string }
variable "tickets_kms_key_arn" { type = string }
variable "aws_profile" {
  type    = string
  default = null
}
variable "deployer_principal_arns" {
  description = "IAM principal ARNs (role ARN without session, or user ARN) of the deployer and the operator's scripts. Exempted from the explicit Deny of the runtime and DEFAULT endpoint resource policies, besides the DevOps Agent SigV4 role. At least one"
  type        = list(string)
  validation {
    condition     = length(var.deployer_principal_arns) > 0
    error_message = "deployer_principal_arns needs at least one ARN (an empty list would leave the Deny condition without a value)."
  }
}
variable "narrow_devops_trust" {
  description = "Second step after the MCP server registration: narrow the DevOps Agent SigV4 role trust from service/* to this registration (service/<id>) with narrow_trust.py. Default false (documented service/* trust, accepted risk); true only after a spike confirms the service id in aws:SourceArn is stable"
  type        = bool
  default     = false
}
variable "tool_classification" {
  description = "READ_ONLY or MUTATIVE per tool. The association is created with the READ_ONLY tools only; classify_tools.py adds the MUTATIVE tools together with their classification (awscc has no toolDetails)."
  type        = map(string)
}

data "aws_caller_identity" "me" {}
data "aws_region" "here" {}
data "aws_partition" "here" {}

locals {
  account      = data.aws_caller_identity.me.account_id
  region       = data.aws_region.here.region
  runtime_name = replace("${var.prefix}_mcp_ticketing", "-", "_")

  # Fail-closed split: the association is created with the READ_ONLY tools only. The MUTATIVE tools
  # (add_comment, close_ticket) are not even enabled until classify_tools.py adds them together with
  # their MUTATIVE classification in one update-association call. If that script is skipped or fails,
  # no write tool is exposed, so none can run unclassified (i.e. without approval).
  readonly_tools = sort([for t, c in var.tool_classification : t if c == "READ_ONLY"])
}

# Versioning is enabled by aws_s3_bucket_versioning.code below.
# nosemgrep: aws-s3-bucket-versioning-not-enabled
resource "aws_s3_bucket" "code" {
  #checkov:skip=CKV_AWS_18:Holds only a package rebuilt from public sources, written by Terraform and read by AgentCore; enable CloudTrail S3 data events for object-level audit
  #checkov:skip=CKV2_AWS_62:No consumer for events on the code bucket
  #checkov:skip=CKV_AWS_144:The package is rebuilt from source; cross-Region replication is not needed
  bucket_prefix = "${var.prefix}-mcp-code-"
  # Kept true on purpose: the bucket only holds the package rebuilt from this repository's sources
  # (scripts/build.py), so terraform destroy may empty it. The recordings bucket defaults to false.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "code" {
  bucket                  = aws_s3_bucket.code.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "code" {
  bucket = aws_s3_bucket.code.id
  versioning_configuration { status = "Enabled" }
}

# Code packages are keyed by content hash: old versions only need to live for a rollback.
resource "aws_s3_bucket_lifecycle_configuration" "code" {
  bucket = aws_s3_bucket.code.id
  rule {
    id     = "expire-old-packages"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  depends_on = [aws_s3_bucket_versioning.code]
}

# SSE-KMS with the sample's customer managed key, bucket key on. The runtime role has
# kms:Decrypt on this key (via S3) to read the package.
resource "aws_s3_bucket_server_side_encryption_configuration" "code" {
  #checkov:skip=CKV2_AWS_67:Customer managed key from modules/kms, which has rotation on (enable_key_rotation = true); the key arrives as a variable so the check cannot follow it
  bucket = aws_s3_bucket.code.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.tickets_kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_policy" "code_tls_only" {
  bucket = aws_s3_bucket.code.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.code.arn, "${aws_s3_bucket.code.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.code]
}

resource "aws_s3_object" "code" {
  bucket      = aws_s3_bucket.code.id
  key         = "mcp_ticketing-${filemd5(var.mcp_zip_path)}.zip"
  source      = var.mcp_zip_path
  source_hash = filemd5(var.mcp_zip_path)
}

resource "aws_iam_role" "runtime" {
  name = "${var.prefix}-mcp-runtime"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      # Confused-deputy scoping to THIS runtime only. The documented example uses
      # "<region>:<account>:*"; the runtime id is generated by the service and starts with the runtime
      # name (the log-group scoping below relies on the same fact), so "runtime/<name>-*" matches this
      # runtime and no other AgentCore resource of the account.
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock-agentcore:${local.region}:${local.account}:runtime/${local.runtime_name}-*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "runtime" {
  role = aws_iam_role.runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan"], Resource = var.tickets_table_arn },
      # Encrypted ticket table and code package only, never a direct KMS call.
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = var.tickets_kms_key_arn,
      Condition = { StringEquals = { "kms:ViaService" = ["dynamodb.${local.region}.amazonaws.com", "s3.${local.region}.amazonaws.com"] } } },
      # Read of this sample's own code bucket (the AgentCore package), not a data store.
      # nosemgrep: no-iam-data-exfiltration
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.code.arn}/*" },
      # DescribeLogGroups has no resource type, so it stays on "*". The write/stream actions are
      # scoped to this runtime's log-group naming pattern (/aws/bedrock-agentcore/runtimes/<runtime-id>-*,
      # the runtime id starts with the runtime name) instead of every AgentCore runtime in the account.
      { Effect = "Allow", Action = ["logs:DescribeLogGroups"], Resource = "*" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
        Resource = [
          "arn:aws:logs:${local.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/${local.runtime_name}-*",
          "arn:aws:logs:${local.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/${local.runtime_name}-*:log-stream:*",
      ] },
    ]
  })
}

resource "aws_bedrockagentcore_agent_runtime" "mcp" {
  agent_runtime_name    = local.runtime_name
  description           = "Ticketing MCP server (stands in for an ITSM tool)"
  role_arn              = aws_iam_role.runtime.arn
  environment_variables = { TICKETS_TABLE = var.tickets_table_name }
  agent_runtime_artifact {
    code_configuration {
      entry_point = ["server.py"]
      runtime     = "PYTHON_3_13"
      code {
        s3 {
          bucket = aws_s3_bucket.code.id
          prefix = aws_s3_object.code.key
        }
      }
    }
  }
  network_configuration { network_mode = "PUBLIC" }
  protocol_configuration { server_protocol = "MCP" }
  depends_on = [aws_iam_role_policy.runtime]
}

# Role assumed by AWS DevOps Agent to sign (SigV4) its calls to the runtime.
resource "aws_iam_role" "devops_mcp" {
  name = "${var.prefix}-devopsagent-mcp-sigv4"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole"
      Principal = { Service = "aidevops.amazonaws.com" }
      # "service/*" is the AWS documented trust for a SigV4 MCP server registration: the role must
      # exist before the service registration (awscc_devopsagent_service below), so the service id
      # is not known yet. Accepted risk: any DevOps Agent registration of this account and Region that
      # is given this role can assume it; it can only call InvokeAgentRuntime on this runtime, and the
      # runtime resource policies below keep every other caller out. Optional second step
      # (var.narrow_devops_trust, default false): terraform_data.narrow_trust rewrites the trust to
      # service/<this registration id> after the registration and the classification.
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account }
        ArnLike      = { "aws:SourceArn" = "arn:aws:aidevops:${local.region}:${local.account}:service/*" }
      }
    }]
  })
  # narrow_trust.py owns the trust after creation (otherwise each plan would widen it back).
  lifecycle {
    ignore_changes = [assume_role_policy]
  }
}

resource "aws_iam_role_policy" "devops_mcp" {
  role = aws_iam_role.devops_mcp.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "bedrock-agentcore:InvokeAgentRuntime",
    Resource = [aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn, "${aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn}/*"] }]
  })
}

# Resource policies on the runtime AND its DEFAULT endpoint: only the DevOps Agent SigV4
# role (and the deployer principals, var.deployer_principal_arns) may invoke them. Without them, any
# principal of the account with an identity policy allowing InvokeAgentRuntime could call close_ticket
# directly and skip the DevOps Agent approval step.
# - AgentCore evaluates identity and resource policies on BOTH the runtime and the endpoint being
#   invoked (resource-based-policies.html), so each carries the same explicit Deny.
# - Resource must be the exact ARN of the resource the policy is attached to (same doc).
# - DEFAULT endpoint ARN: the provider does not expose it (aws_bedrockagentcore_agent_runtime has no
#   endpoint attribute; aws_bedrockagentcore_agent_runtime_endpoint only manages extra endpoints), so it
#   is built from the documented format <runtime-arn>/runtime-endpoint/<name>: Service Authorization
#   Reference (list_bedrock-agentcore.html, resource type runtime-endpoint) and the agentRuntimeEndpointArn
#   pattern of the control-plane API (GetAgentRuntimeEndpoint). The devguide example shows
#   runtime/<id>/endpoint/<id>; the API pattern is the one the service returns.
# - The Deny lists every runtime invocation action the guide supports for resource policies, so a
#   shell/command or WebSocket entry point is closed as well.
locals {
  runtime_invoke_actions = [
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeAgentRuntimeForUser",
    "bedrock-agentcore:InvokeAgentRuntimeCommand",
    "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
    "bedrock-agentcore:StopRuntimeSession",
  ]
  # Principals exempted from the Deny: the DevOps Agent SigV4 role, then the deployer principals.
  runtime_invokers     = concat([aws_iam_role.devops_mcp.arn], var.deployer_principal_arns)
  default_endpoint_arn = "${aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn}/runtime-endpoint/DEFAULT"
}

resource "aws_bedrockagentcore_resource_policy" "mcp" {
  resource_arn = aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowDevOpsAgentSigV4Role"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.devops_mcp.arn }
        Action    = "bedrock-agentcore:InvokeAgentRuntime"
        Resource  = aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn
      },
      {
        Sid       = "DenyEveryOtherPrincipal"
        Effect    = "Deny"
        Principal = "*"
        Action    = local.runtime_invoke_actions
        Resource  = aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = local.runtime_invokers } }
      },
    ]
  })
}

resource "aws_bedrockagentcore_resource_policy" "mcp_default_endpoint" {
  resource_arn = local.default_endpoint_arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowDevOpsAgentSigV4Role"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.devops_mcp.arn }
        Action    = "bedrock-agentcore:InvokeAgentRuntime"
        Resource  = local.default_endpoint_arn
      },
      {
        Sid       = "DenyEveryOtherPrincipal"
        Effect    = "Deny"
        Principal = "*"
        Action    = local.runtime_invoke_actions
        Resource  = local.default_endpoint_arn
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = local.runtime_invokers } }
      },
    ]
  })
}

locals {
  endpoint = "https://bedrock-agentcore.${local.region}.amazonaws.com/runtimes/${urlencode(aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn)}/invocations?qualifier=DEFAULT"
}

# IAM propagation: without this wait the registration fails with a 403. The triggers make the wait
# run again whenever the role or its policy is replaced (a rename, for example), not only on the
# first apply.
# The runtime resource policy is in depends_on/triggers too: the registration validates the runtime
# with the role, so the policy that keeps the role as the only caller is in place (and settled)
# before the service is registered.
resource "time_sleep" "iam_propagation" {
  depends_on      = [aws_iam_role_policy.devops_mcp, aws_bedrockagentcore_resource_policy.mcp, aws_bedrockagentcore_resource_policy.mcp_default_endpoint]
  create_duration = "30s"
  triggers = {
    role            = aws_iam_role.devops_mcp.arn
    policy          = aws_iam_role_policy.devops_mcp.policy
    resource_policy = aws_bedrockagentcore_resource_policy.mcp.policy
    endpoint_policy = aws_bedrockagentcore_resource_policy.mcp_default_endpoint.policy
  }
}

resource "awscc_devopsagent_service" "mcp" {
  service_type = "mcpserversigv4"
  service_details = {
    mcp_server_sig_v4 = {
      name        = "${var.prefix}-ticketing"
      endpoint    = local.endpoint
      description = "Ticketing MCP server on AgentCore Runtime"
      authorization_config = {
        region       = local.region
        service      = "bedrock-agentcore"
        mcp_role_arn = aws_iam_role.devops_mcp.arn
      }
    }
  }
  depends_on = [time_sleep.iam_propagation]
}

# Fail-closed: create the association with the READ_ONLY tools only. A write tool is never enabled
# here, so it cannot be invoked while unclassified (the service treats a tool with no classification
# as READ_ONLY, which would be "a write tool runs without approval"). classify_tools.py then expands
# the tool list to the full set and attaches toolDetails in one update-association call; a skipped or
# failed script leaves only the read tools exposed.
resource "awscc_devopsagent_association" "mcp" {
  agent_space_id = var.agent_space_id
  service_id     = awscc_devopsagent_service.mcp.service_id
  configuration = {
    mcp_server_sig_v4 = { tools = local.readonly_tools }
  }

  # classify_tools.py grows the tools list (adds the MUTATIVE tools) and sets toolDetails, neither of
  # which this awscc resource models. Without this, every plan after the first would try to shrink the
  # association back to the READ_ONLY-only set and drop the classification (perpetual drift, and a
  # window where the write tools are unclassified). Scoped to `configuration` so a change to the
  # agent space or service is still detected.
  lifecycle {
    ignore_changes = [configuration]
  }
}

# awscc gap: the resource models neither toolDetails nor the full (read + write) tool list. This step
# adds the MUTATIVE tools and sets every tool's classification in a single update-association call,
# then reads the association back and fails if any MUTATIVE tool is not classified MUTATIVE.
resource "terraform_data" "classify_tools" {
  triggers_replace = {
    association = awscc_devopsagent_association.mcp.association_id
    tools       = jsonencode(var.tool_classification)
  }
  provisioner "local-exec" {
    command = "python3 ${path.module}/classify_tools.py"
    environment = {
      AGENT_SPACE_ID   = var.agent_space_id
      ASSOCIATION_ID   = awscc_devopsagent_association.mcp.association_id
      SERVICE_ID       = awscc_devopsagent_service.mcp.service_id
      TOOLS_JSON       = jsonencode(var.tool_classification)
      REGION           = local.region
      AWS_PROFILE_NAME = var.aws_profile == null ? "" : var.aws_profile
    }
  }
}

# Second step of the SigV4 role trust: service/* -> service/<id> (NARROW=false writes service/* back).
# Runs after the classification, so the association was created and classified with the documented
# trust; triggered again when the registration, the role or the switch changes.
resource "terraform_data" "narrow_trust" {
  triggers_replace = {
    service = awscc_devopsagent_service.mcp.service_id
    role    = aws_iam_role.devops_mcp.arn
    narrow  = var.narrow_devops_trust
  }
  provisioner "local-exec" {
    command = "python3 ${path.module}/narrow_trust.py"
    environment = {
      ROLE_NAME        = aws_iam_role.devops_mcp.name
      SERVICE_ID       = awscc_devopsagent_service.mcp.service_id
      ACCOUNT          = local.account
      REGION           = local.region
      PARTITION        = data.aws_partition.here.partition
      NARROW           = tostring(var.narrow_devops_trust)
      AWS_PROFILE_NAME = var.aws_profile == null ? "" : var.aws_profile
    }
  }
  depends_on = [terraform_data.classify_tools]
}

output "runtime_arn" { value = aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn }
output "service_id" { value = awscc_devopsagent_service.mcp.service_id }
output "association_id" { value = awscc_devopsagent_association.mcp.association_id }
