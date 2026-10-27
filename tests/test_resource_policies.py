# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The runtime, its DEFAULT endpoint and the ticket table carry resource policies that keep
the approval step, with the deployer principals as the only exemption besides the sample's roles.

Reads the Terraform text (no provider, no plan): enough to catch a dropped policy or a writer role
list that drifts from the role names the modules actually create.
"""

import re

from conftest import ROOT

TF = ROOT / "terraform"
MCP = (TF / "modules" / "mcp-ticketing" / "main.tf").read_text()
FOUNDATION = (TF / "modules" / "foundation" / "main.tf").read_text()
DEMO = (TF / "envs" / "demo" / "main.tf").read_text()
DEMO_VARS = (TF / "envs" / "demo" / "variables.tf").read_text()

INVOKE_ACTIONS = {
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeAgentRuntimeForUser",
    "bedrock-agentcore:InvokeAgentRuntimeCommand",
    "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
    "bedrock-agentcore:StopRuntimeSession",
}


def _block(text: str, header: str) -> str:
    """Body of the top-level block that starts with `header` (up to the next unindented `}`)."""
    start = text.index(header)
    end = text.index("\n}\n", start)
    return text[start:end]


def _role_name(path: str, resource: str) -> str:
    body = _block((TF / path).read_text(), f'resource "aws_iam_role" "{resource}"')
    return re.search(r'name\s*=\s*"([^"]+)"', body).group(1)


def _assert_invoke_deny(body: str, resource_ref: str) -> None:
    assert f"resource_arn = {resource_ref}" in body
    assert "Principal = { AWS = aws_iam_role.devops_mcp.arn }" in body
    assert 'Effect    = "Deny"' in body and 'Principal = "*"' in body
    assert "Action    = local.runtime_invoke_actions" in body
    assert f"Resource  = {resource_ref}" in body
    assert 'ArnNotEquals = { "aws:PrincipalArn" = local.runtime_invokers } }' in body


def test_invoke_deny_lists_every_runtime_action_and_exempts_devops_role_and_deployer():
    locals_body = _block(MCP, "locals {\n  runtime_invoke_actions")
    assert set(re.findall(r'"(bedrock-agentcore:\w+)"', locals_body)) == INVOKE_ACTIONS
    assert "runtime_invokers     = concat([aws_iam_role.devops_mcp.arn], var.deployer_principal_arns)" in locals_body


def test_runtime_resource_policy_allows_only_the_devops_agent_role():
    body = _block(MCP, 'resource "aws_bedrockagentcore_resource_policy" "mcp"')
    _assert_invoke_deny(body, "aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn")


def test_default_endpoint_carries_the_same_deny():
    # AgentCore evaluates the runtime AND the endpoint; the DEFAULT endpoint ARN is not exposed by the
    # provider and follows the documented <runtime-arn>/runtime-endpoint/<name> format.
    assert 'default_endpoint_arn = "${aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn}/runtime-endpoint/DEFAULT"' in MCP
    body = _block(MCP, 'resource "aws_bedrockagentcore_resource_policy" "mcp_default_endpoint"')
    _assert_invoke_deny(body, "local.default_endpoint_arn")
    # The service registration waits for both policies.
    sleep = _block(MCP, 'resource "time_sleep" "iam_propagation"')
    assert "aws_bedrockagentcore_resource_policy.mcp," in sleep
    assert "aws_bedrockagentcore_resource_policy.mcp_default_endpoint" in sleep
    assert "aws_bedrockagentcore_resource_policy.mcp_default_endpoint.policy" in sleep


def test_runtime_role_trust_is_scoped_to_this_runtime():
    body = _block(MCP, 'resource "aws_iam_role" "runtime"')
    assert ':runtime/${local.runtime_name}-*"' in body
    assert ':${local.account}:*"' not in body


def test_table_resource_policy_denies_writes_except_the_sample_roles_and_deployer():
    body = _block(FOUNDATION, 'resource "aws_dynamodb_resource_policy" "tickets"')
    assert "resource_arn = aws_dynamodb_table.tickets.arn" in body
    assert 'Effect    = "Deny"' in body and 'Principal = "*"' in body
    actions = set(re.findall(r'"(dynamodb:\w+)"', body))
    # Data-plane writes only: Terraform keeps managing the table and the policy.
    assert actions == {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:PartiQLInsert",
        "dynamodb:PartiQLUpdate",
        "dynamodb:PartiQLDelete",
    }
    assert 'ArnNotEquals = { "aws:PrincipalArn" = local.ticket_write_exempt } }' in body
    assert "concat([aws_iam_role.opener.name], var.ticket_writer_role_names)" in FOUNDATION
    assert "ticket_write_exempt     = concat(local.ticket_writer_role_arns, var.deployer_principal_arns)" in FOUNDATION


def test_table_writer_roles_are_exactly_the_sample_roles():
    passed = re.search(r"ticket_writer_role_names = \[([^\]]*)\]", DEMO).group(1)
    names = sorted(re.findall(r'"([^"]+)"', passed))
    expected = sorted(
        [
            _role_name("modules/oncall/caller.tf", "caller"),
            _role_name("modules/oncall/handler.tf", "handler"),
            _role_name("modules/mcp-ticketing/main.tf", "runtime"),
        ]
    )
    assert names == expected
    # The opener is added inside the foundation module (its own role).
    assert _role_name("modules/foundation/main.tf", "opener") == "${var.prefix}-incident-opener"


def test_deployer_principals_default_to_the_caller_role_and_reach_every_module():
    assert 'variable "deployer_principal_arns"' in DEMO_VARS
    assert 'data "aws_iam_session_context" "deployer"' in DEMO
    assert "arn = data.aws_caller_identity.deployer.arn" in DEMO
    assert "[data.aws_iam_session_context.deployer.issuer_arn]" in DEMO
    # foundation, mcp and oncall all receive it.
    assert len(re.findall(r"deployer_principal_arns\s*=\s*local\.deployer_principal_arns", DEMO)) == 3
    for path in ("modules/foundation/main.tf", "modules/mcp-ticketing/main.tf", "modules/oncall/variables.tf"):
        body = _block((TF / path).read_text(), 'variable "deployer_principal_arns"')
        assert "length(var.deployer_principal_arns) > 0" in body, path
