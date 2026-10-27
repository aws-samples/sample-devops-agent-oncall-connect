# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Structural checks on the Terraform text (no provider, no plan): call budget, service-principal
scoping, encryption, state and IAM hardening.

Each test pins one hardening item so a later edit cannot drop it silently.
"""

import re

from conftest import ROOT

TF = ROOT / "terraform"
MODULES = TF / "modules"
FOUNDATION = (MODULES / "foundation" / "main.tf").read_text()
KMS = (MODULES / "kms" / "main.tf").read_text()
MCP = (MODULES / "mcp-ticketing" / "main.tf").read_text()
CALLER = (MODULES / "oncall" / "caller.tf").read_text()
HANDLER = (MODULES / "oncall" / "handler.tf").read_text()
ONCALL_MAIN = (MODULES / "oncall" / "main.tf").read_text()
ONCALL_VARS = (MODULES / "oncall" / "variables.tf").read_text()
LEX = (MODULES / "oncall" / "lex.tf").read_text()
CONNECT = (MODULES / "connect-instance" / "main.tf").read_text()
DEMO = (TF / "envs" / "demo" / "main.tf").read_text()
DEMO_VARS = (TF / "envs" / "demo" / "variables.tf").read_text()
BUDGET = (TF / "envs" / "demo" / "budget.tf").read_text()
BACKEND = (TF / "envs" / "demo" / "backend.tf.example").read_text()
REPLAY = (ROOT / "scripts" / "replay.py").read_text()
ALL_TF = {p.relative_to(TF).as_posix(): p.read_text() for p in TF.rglob("*.tf") if ".terraform" not in p.parts}


def _block(text: str, header: str) -> str:
    start = text.index(header)
    end = text.index("\n}\n", start)
    return text[start:end]


# --- Call budget ---


def test_caller_rule_is_scoped_to_the_agent_space():
    assert 'agent_space_id = regex("agentspace/([^/]+)$", var.agent_space_arn)[0]' in CALLER
    rule = _block(CALLER, 'resource "aws_cloudwatch_event_rule" "oncall"')
    assert "metadata = { agent_space_id = [local.agent_space_id] }" in rule
    assert 'data     = { priority = ["CRITICAL", "HIGH"] }' in rule


def test_caller_reserved_concurrency_defaults_to_one_and_is_configurable():
    fn = _block(CALLER, 'resource "aws_lambda_function" "caller"')
    assert "reserved_concurrent_executions = var.caller_reserved_concurrency" in fn
    assert "CKV_AWS_115" not in fn
    var = _block(ONCALL_VARS, 'variable "caller_reserved_concurrency"')
    assert "default     = 1" in var
    assert "var.caller_reserved_concurrency == -1 || var.caller_reserved_concurrency >= 1" in var
    assert "caller_reserved_concurrency  = var.caller_reserved_concurrency" in DEMO
    assert "default     = 1" in _block(DEMO_VARS, 'variable "caller_reserved_concurrency"')


def test_suppressed_alarms_are_counted_by_a_metric_without_alarm():
    mf = _block(FOUNDATION, 'resource "aws_cloudwatch_log_metric_filter" "alarm_suppressed"')
    assert "log_group_name = aws_cloudwatch_log_group.opener.name" in mf
    assert 'pattern        = "{ $.held IS TRUE }"' in mf
    assert 'name      = "AlarmSuppressed"' in mf
    # The opener really logs that line (src/lambdas/incident_opener/app.py).
    opener = (ROOT / "src" / "lambdas" / "incident_opener" / "app.py").read_text()
    assert '"held": True' in opener
    assert "AlarmSuppressed" not in "".join(re.findall(r'resource "aws_cloudwatch_metric_alarm"[^}]*', FOUNDATION))


def test_budget_alert_is_optional_and_off_by_default():
    assert 'count        = var.budget_alert_email == "" ? 0 : 1' in BUDGET
    assert 'budget_type  = "COST"' in BUDGET and 'time_unit    = "MONTHLY"' in BUDGET
    assert BUDGET.count("subscriber_email_addresses = [var.budget_alert_email]") == 2
    assert 'default     = ""' in _block(DEMO_VARS, 'variable "budget_alert_email"')


# --- Service-principal scoping ---


def test_kms_eventbridge_statement_is_scoped_to_the_sample_rules():
    stmt = KMS[KMS.index('Sid       = "EventBridgeToEncryptedSqs"') :]
    stmt = stmt[: stmt.index("},\n    ]")]
    assert '"aws:SourceAccount" = local.account' in stmt
    assert '"aws:SourceArn" = "arn:${local.partition}:events:${local.region}:${local.account}:rule/${var.prefix}-*"' in stmt


def test_every_sample_rule_matches_the_kms_rule_prefix():
    names = []
    for text in ALL_TF.values():
        for body in re.findall(r'resource "aws_cloudwatch_event_rule" "\w+" \{(.*?)\n\}', text, re.S):
            names.append(re.search(r'name\s*=\s*"([^"]+)"', body).group(1))
    assert names, "no EventBridge rule found"
    assert all(n.startswith("${var.prefix}-") for n in names), names


def test_unsupported_source_arn_trusts_are_documented():
    assert "Lex V2 service-role docs" in LEX
    assert "lambda-intro-execution-role.html" in FOUNDATION
    assert "lambda-intro-execution-role.html" in ONCALL_MAIN


# --- Encryption ---


def test_code_bucket_uses_the_sample_cmk():
    sse = _block(MCP, 'resource "aws_s3_bucket_server_side_encryption_configuration" "code"')
    assert 'sse_algorithm     = "aws:kms"' in sse
    assert "kms_master_key_id = var.tickets_kms_key_arn" in sse
    assert "bucket_key_enabled = true" in sse
    assert "AES256" not in MCP and "CKV_AWS_145" not in MCP


def test_every_queue_has_a_cmk_and_denies_insecure_transport():
    queues, policies = {}, {}
    for text in ALL_TF.values():
        for name, body in re.findall(r'resource "aws_sqs_queue" "(\w+)" \{(.*?)\n\}', text, re.S):
            queues[name] = body
        for name, body in re.findall(r'resource "aws_sqs_queue_policy" "(\w+)" \{(.*?)\n\}', text, re.S):
            policies[name] = body
    assert len(queues) == 6, sorted(queues)
    for name, body in queues.items():
        assert "kms_master_key_id" in body, name
    assert set(policies) == set(queues)
    for name, body in policies.items():
        assert f"aws_sqs_queue.{name}.id" in body, name
        assert 'Sid = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "sqs:*"' in body, name
        assert '"aws:SecureTransport" = "false"' in body, name


def test_ses_configuration_set_requires_tls_when_email_is_enabled():
    cs = _block(HANDLER, 'resource "aws_sesv2_configuration_set" "notify"')
    assert "count                  = local.email_enabled ? 1 : 0" in cs
    assert 'delivery_options { tls_policy = "REQUIRE" }' in cs
    assert "concat(local.ses_identity_arns, [aws_sesv2_configuration_set.notify[0].arn])" in HANDLER
    assert 'output "ses_configuration_set"' in ONCALL_MAIN


def test_recordings_bucket_is_not_force_destroyed_by_default():
    assert "force_destroy = var.force_destroy" in _block(CONNECT, 'resource "aws_s3_bucket" "recordings"')
    assert "default     = false" in _block(CONNECT, 'variable "force_destroy"')
    assert "force_destroy      = var.recordings_force_destroy" in DEMO
    assert "default     = false" in _block(DEMO_VARS, 'variable "recordings_force_destroy"')


# --- State ---


def test_no_secure_string_value_in_terraform():
    found = 0
    for path, text in ALL_TF.items():
        for body in re.findall(r'resource "aws_ssm_parameter" "\w+" \{(.*?)\n\}', text, re.S):
            assert 'type        = "SecureString"' in body, path
            assert 'value       = "UNSET"' in body, path
            assert "lifecycle { ignore_changes = [value] }" in body, path
            found += 1
    assert found == 3


def test_backend_example_comes_first():
    assert "set it up FIRST" in BACKEND
    assert "encrypt    = true" in BACKEND
    readme = (ROOT / "README.md").read_text()
    deploy = readme[readme.index("## Deploy") :]
    assert deploy.index("cp backend.tf.example backend.tf") < deploy.index("terraform init")


# --- IAM hardening ---


def test_replay_rule_is_on_a_dedicated_bus_only_the_deployer_can_publish_to():
    bus = _block(CALLER, 'resource "aws_cloudwatch_event_bus_policy" "replay"')
    assert 'Action    = "events:PutEvents"' in bus and 'Effect    = "Deny"' in bus
    assert 'ArnNotEquals = { "aws:PrincipalArn" = var.deployer_principal_arns }' in bus
    rule = _block(CALLER, 'resource "aws_cloudwatch_event_rule" "oncall"')
    target = _block(CALLER, 'resource "aws_cloudwatch_event_target" "oncall"')
    for body in (rule, target):
        assert 'event_bus_name = each.key == "replay" ? aws_cloudwatch_event_bus.replay[0].name : null' in body
    assert '"EventBusName": oncall["replay_bus_name"]' in REPLAY
    assert 'output "replay_bus_name"' in ONCALL_MAIN


def test_handler_kms_is_decrypt_only():
    policy = _block(HANDLER, 'resource "aws_iam_role_policy" "handler"')
    assert "kms:GenerateDataKey" not in policy
    assert 'Action = "kms:Decrypt", Resource = [var.connect_kms_key_arn, var.kms_key_arn]' in policy


def test_every_kms_identity_statement_has_via_service():
    statements = 0
    for path in ("modules/foundation/main.tf", "modules/mcp-ticketing/main.tf", "modules/oncall/caller.tf", "modules/oncall/handler.tf"):
        for line_start in [m.start() for m in re.finditer(r"\{ Effect = \"Allow\", Action = [^\n]*\"kms:Decrypt\"", ALL_TF[path])]:
            stmt = ALL_TF[path][line_start : ALL_TF[path].index("} },", line_start) + 4]
            assert '"kms:ViaService"' in stmt, (path, stmt[:120])
            statements += 1
    # meter-api, opener, runtime, caller, handler.
    assert statements == 5


def test_caller_query_is_scoped_to_the_index_it_reads():
    policy = _block(CALLER, 'resource "aws_iam_role_policy" "caller"')
    assert '{ Effect = "Allow", Action = "dynamodb:Query", Resource = "${var.tickets_table_arn}/index/byInvestigation" }' in policy
    assert '{ Effect = "Allow", Action = "dynamodb:UpdateItem", Resource = var.tickets_table_arn }' in policy
    assert "/index/*" not in policy
    caller_py = (ROOT / "src" / "lambdas" / "oncall_caller" / "app.py").read_text()
    assert set(re.findall(r'IndexName="(\w+)"', caller_py)) == {"byInvestigation"}
