# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Terraform test (mock providers, plan only) run by tests/test_terraform_guards.py on a temp copy of
# modules/oncall. SRC_ROOT is replaced by a temp src root (src symlink + dummy build/layer_boto3.zip).

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_data "aws_region" {
    defaults = { region = "eu-west-1" }
  }
}
mock_provider "aws" {
  alias = "connect"
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_data "aws_region" {
    defaults = { region = "eu-central-1" }
  }
}
mock_provider "awscc" {}
mock_provider "awscc" { alias = "connect" }
mock_provider "archive" {}

variables {
  prefix                = "t"
  src_root              = "SRC_ROOT"
  agent_space_arn       = "arn:aws:aidevops:eu-west-1:111122223333:agentspace/s"
  tickets_table_name    = "t"
  tickets_table_arn     = "arn:aws:dynamodb:eu-west-1:111122223333:table/t"
  boto3_layer_arn       = "arn:aws:lambda:eu-west-1:111122223333:layer:l:1"
  connect_instance_id   = "i"
  connect_source_number = "+15555550100"
  enable_replay         = false
  locale                = jsondecode(file("SRC_ROOT/src/locales/en-US.json"))
  kms_key_arn           = "arn:aws:kms:eu-west-1:111122223333:key/k"
  connect_kms_key_arn   = "arn:aws:kms:eu-central-1:111122223333:key/k"
  log_retention_days    = 7
  deployer_principal_arns = ["arn:aws:iam::111122223333:role/deployer"]
}

run "live_without_pin_refused" {
  command = plan
  variables {
    dry_run = false
  }
  expect_failures = [var.allow_live_without_pin]
}

run "live_with_override_ok" {
  command = plan
  variables {
    dry_run                = false
    allow_live_without_pin = true
  }
}

run "live_with_pin_ok" {
  command = plan
  variables {
    dry_run    = false
    enable_pin = true
  }
}

run "pin_with_flow_logging_refused" {
  command = plan
  variables {
    enable_pin          = true
    enable_flow_logging = true
  }
  expect_failures = [var.enable_pin]
}

run "dry_run_default_ok" {
  command = plan
}

# Replay on its own bus, rule and target on that bus, scoped to the agent space.
run "replay_bus_ok" {
  command = plan
  variables {
    enable_replay = true
  }
  assert {
    condition     = aws_cloudwatch_event_rule.oncall["replay"].event_bus_name == "t-replay"
    error_message = "the replay rule must be on the replay bus"
  }
  assert {
    condition     = jsondecode(aws_cloudwatch_event_rule.oncall["real"].event_pattern).detail.metadata.agent_space_id[0] == "s"
    error_message = "the rule must be scoped to the agent space"
  }
  assert {
    condition     = aws_lambda_function.caller.reserved_concurrent_executions == 1
    error_message = "caller reserved concurrency must default to 1"
  }
}
