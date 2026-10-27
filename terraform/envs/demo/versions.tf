# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    awscc = {
      source  = "hashicorp/awscc"
      version = "~> 1.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

# AWS DevOps Agent Region: Agent Space, foundation, caller Lambdas
provider "aws" {
  region  = var.agent_space_region
  profile = var.aws_profile
  default_tags { tags = { project = "devops-agent-oncall-connect", iac = "terraform" } }
}

provider "awscc" {
  region  = var.agent_space_region
  profile = var.aws_profile
}

# Amazon Connect Region
provider "aws" {
  alias   = "connect"
  region  = var.connect_region
  profile = var.aws_profile
  default_tags { tags = { project = "devops-agent-oncall-connect", iac = "terraform" } }
}

provider "awscc" {
  alias   = "connect"
  region  = var.connect_region
  profile = var.aws_profile
}

variable "agent_space_region" {
  description = "Region of the AWS DevOps Agent Agent Space (foundation, MCP server, caller Lambdas)"
  type        = string
}
variable "connect_region" {
  description = "Region of the Amazon Connect instance (can differ from agent_space_region)"
  type        = string
}

variable "aws_profile" {
  description = "Named AWS profile, or null for the default credential chain"
  type        = string
  default     = null
}
