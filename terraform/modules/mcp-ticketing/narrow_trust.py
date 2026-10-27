# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Optional second step of the DevOps Agent SigV4 role trust (var.narrow_devops_trust, default false).

The role must exist before the MCP server registration (awscc_devopsagent_service), so Terraform
creates it with the documented wide trust (aws:SourceArn service/*). Once the registration exists and
the association is classified, terraform_data.narrow_trust calls this script; with NARROW=true it rewrites the
trust to this one registration (service/<SERVICE_ID>) and reads it back. NARROW=false writes the wide
trust back (off switch). Terraform ignores the role's assume_role_policy after creation, so this
script is the only writer of the trust.

Uses the AWS CLI (same identity resolution as Terraform), argument list without a shell, every value
validated first.
"""

from __future__ import annotations  # runs under the deployer's python3, which may be older than 3.10

import json
import os
import re
import subprocess  # nosec B404 - fixed argument list, no shell
import sys

_ACCOUNT = re.compile(r"^\d{12}$")
# Anchored, bounded, no nested unbounded quantifier (no catastrophic backtracking):
# us-east-1, ap-southeast-2, us-gov-west-1, eu-central-2 ...
_REGION = re.compile(r"\A[a-z]{2}(?:-[a-z]{1,16}){1,3}-[0-9]{1,2}\Z")
_PARTITION = re.compile(r"\Aaws(?:-[a-z]{1,16}){0,2}\Z")  # aws, aws-cn, aws-us-gov, aws-iso-b
_ID = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_ROLE = re.compile(r"^[\w+=,.@-]{1,64}$")
_PROFILE = re.compile(r"^[A-Za-z0-9_+=,.@-]{0,128}$")


def trust_policy(partition: str, region: str, account: str, service_id: str | None) -> dict:
    """Trust of the SigV4 role: this registration when service_id is set, else every one (service/*)."""
    scope = service_id if service_id else "*"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "aidevops.amazonaws.com"},
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": f"arn:{partition}:aidevops:{region}:{account}:service/{scope}"},
                },
            }
        ],
    }


def validate(env: dict) -> None:
    ok = (
        _PARTITION.match(env["PARTITION"])
        and _REGION.match(env["REGION"])
        and _ACCOUNT.match(env["ACCOUNT"])
        and _ID.match(env["SERVICE_ID"])
        and _ROLE.match(env["ROLE_NAME"])
        and _PROFILE.match(env.get("AWS_PROFILE_NAME", ""))
        and env.get("NARROW", "true") in ("true", "false")
    )
    if not ok:
        raise ValueError("narrow_trust: invalid PARTITION, REGION, ACCOUNT, SERVICE_ID, ROLE_NAME, AWS_PROFILE_NAME or NARROW")


def make_cli(profile: str):
    def aws(*args: str) -> dict:
        cmd = ["aws", "iam", *args, "--output", "json"]
        if profile:
            cmd += ["--profile", profile]
        # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use-tainted-env-args
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout  # nosec B603
        return json.loads(out) if out.strip() else {}

    return aws


def apply(aws, role: str, wanted: dict) -> None:
    """Write the trust, read it back, raise RuntimeError on a mismatch."""
    aws("update-assume-role-policy", "--role-name", role, "--policy-document", json.dumps(wanted))
    got = aws("get-role", "--role-name", role)["Role"]["AssumeRolePolicyDocument"]
    if isinstance(got, str):  # some CLI versions return the URL-encoded JSON string
        got = json.loads(got)
    if got.get("Statement") != wanted["Statement"]:
        raise RuntimeError(f"narrow_trust: read-back mismatch on {role}: {json.dumps(got)}")


def main() -> int:
    env = dict(os.environ)
    try:
        validate(env)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    narrow = env.get("NARROW", "true") == "true"
    wanted = trust_policy(env["PARTITION"], env["REGION"], env["ACCOUNT"], env["SERVICE_ID"] if narrow else None)
    try:
        apply(make_cli(env.get("AWS_PROFILE_NAME", "")), env["ROLE_NAME"], wanted)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"trust of {env['ROLE_NAME']}: {wanted['Statement'][0]['Condition']['ArnLike']['aws:SourceArn']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
