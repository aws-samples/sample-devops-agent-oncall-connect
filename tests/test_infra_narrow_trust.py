# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Optional second step that narrows the DevOps Agent SigV4 role trust to one registration."""

import importlib.util
import json

import pytest
from conftest import ROOT

_PATH = ROOT / "terraform" / "modules" / "mcp-ticketing" / "narrow_trust.py"
_SPEC = importlib.util.spec_from_file_location("narrow_trust", _PATH)
nt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(nt)

MCP = (ROOT / "terraform" / "modules" / "mcp-ticketing" / "main.tf").read_text()
ENV = {
    "PARTITION": "aws",
    "REGION": "us-east-1",
    "ACCOUNT": "111122223333",
    "SERVICE_ID": "svc-123",
    "ROLE_NAME": "devops-oncall-devopsagent-mcp-sigv4",
    "AWS_PROFILE_NAME": "",
    "NARROW": "true",
}


class FakeIam:
    def __init__(self, echo=True):
        self.calls, self.doc, self.echo = [], None, echo

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "update-assume-role-policy":
            self.doc = json.loads(args[args.index("--policy-document") + 1])
            return {}
        doc = self.doc if self.echo else nt.trust_policy("aws", "us-east-1", "111122223333", None)
        return {"Role": {"AssumeRolePolicyDocument": doc}}


def _source_arn(doc):
    return doc["Statement"][0]["Condition"]["ArnLike"]["aws:SourceArn"]


def test_narrowed_trust_names_the_one_registration():
    doc = nt.trust_policy("aws", "us-east-1", "111122223333", "svc-123")
    stmt = doc["Statement"][0]
    assert stmt["Principal"] == {"Service": "aidevops.amazonaws.com"}
    assert stmt["Condition"]["StringEquals"] == {"aws:SourceAccount": "111122223333"}
    assert _source_arn(doc) == "arn:aws:aidevops:us-east-1:111122223333:service/svc-123"
    assert _source_arn(nt.trust_policy("aws", "us-east-1", "111122223333", None)).endswith(":service/*")


def test_apply_writes_and_reads_back():
    iam = FakeIam()
    wanted = nt.trust_policy("aws", "us-east-1", "111122223333", "svc-123")
    nt.apply(iam, "r", wanted)
    assert [c[0] for c in iam.calls] == ["update-assume-role-policy", "get-role"]


def test_apply_fails_when_the_read_back_differs():
    with pytest.raises(RuntimeError, match="read-back mismatch"):
        nt.apply(FakeIam(echo=False), "r", nt.trust_policy("aws", "us-east-1", "111122223333", "svc-123"))


@pytest.mark.parametrize(
    "key,value", [("SERVICE_ID", "a b"), ("ACCOUNT", "123"), ("ROLE_NAME", "x;y"), ("NARROW", "yes"), ("REGION", "us east")]
)
def test_invalid_input_is_refused(key, value):
    with pytest.raises(ValueError):
        nt.validate({**ENV, key: value})


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-3", "ap-southeast-2", "us-gov-west-1", "eu-central-2"])
@pytest.mark.parametrize("partition", ["aws", "aws-cn", "aws-us-gov", "aws-iso-b"])
def test_valid_regions_and_partitions_are_accepted(region, partition):
    nt.validate({**ENV, "REGION": region, "PARTITION": partition})


@pytest.mark.parametrize(
    "key,value",
    [
        ("REGION", "us-east-1\n"),
        ("REGION", "useast-1"),
        ("REGION", "us-east-"),
        ("REGION", "us-" + "a" * 17 + "-1"),
        ("REGION", "us-a-b-c-d-1"),
        ("PARTITION", "aws\n"),
        ("PARTITION", "aws-"),
        ("PARTITION", "amazon"),
        ("PARTITION", "aws-a-b-c"),
    ],
)
def test_malformed_regions_and_partitions_are_refused(key, value):
    with pytest.raises(ValueError):
        nt.validate({**ENV, key: value})


def test_region_and_partition_patterns_do_not_backtrack():
    """Adversarial near-misses return fast: bounded, anchored patterns (Probe regex_dos)."""
    import time

    start = time.monotonic()
    for value in ("us" + "-a" * 5000 + "!", "aws" + "-a" * 5000 + "!"):
        assert not nt._REGION.match(value)
        assert not nt._PARTITION.match(value)
    assert time.monotonic() - start < 0.5


def test_terraform_wires_the_second_step_after_classification():
    assert "lifecycle {\n    ignore_changes = [assume_role_policy]\n  }" in MCP
    start = MCP.index('resource "terraform_data" "narrow_trust"')
    body = MCP[start : MCP.index("\n}\n", start)]
    assert "depends_on = [terraform_data.classify_tools]" in body
    assert "SERVICE_ID       = awscc_devopsagent_service.mcp.service_id" in body
    assert "NARROW           = tostring(var.narrow_devops_trust)" in body
