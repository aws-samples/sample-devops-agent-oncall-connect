# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sets the READ_ONLY / MUTATIVE classification of the MCP tools of an AWS DevOps Agent association.

Works around an awscc provider gap (awscc_devopsagent_association exposes neither toolDetails nor a
tool list that mixes read and write tools). Terraform creates the association with the READ_ONLY
tools only; this script, called by terraform_data.classify_tools after each creation or change of
the association, adds the MUTATIVE tools *together with* their MUTATIVE classification in one
update-association call. A skipped or failed run therefore never leaves a write tool enabled without
a MUTATIVE classification (fail-closed).

Uses the AWS CLI: same identity resolution as Terraform (named profile or CI role). The
devops-agent update-association / get-association commands and the mcpserversigv4 toolDetails member
are present in aws-cli 2.36.47 (verified via `aws devops-agent update-association help`).

Reads the association back and fails explicitly if the classification does not match what was asked,
in particular if any MUTATIVE tool is missing or not classified MUTATIVE.
"""

import json
import os
import re
import subprocess  # nosec B404 - fixed argument list, no shell
import sys
import time

# Validated before anything reaches the command line: an ID, a Region, a profile or a tool name.
_ID = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_PROFILE = re.compile(r"^[A-Za-z0-9_+=,.@-]{0,128}$")
_TOOL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate(space: str, assoc: str, region: str, profile: str, wanted: dict) -> None:
    """Reject invalid inputs. Raises ValueError with a message on the first problem."""
    if not (_ID.match(space) and _ID.match(assoc) and _REGION.match(region) and _PROFILE.match(profile)):
        raise ValueError("classification: invalid AGENT_SPACE_ID, ASSOCIATION_ID, REGION or AWS_PROFILE_NAME")
    if not all(_TOOL.match(n) and c in ("READ_ONLY", "MUTATIVE") for n, c in wanted.items()):
        raise ValueError("classification: invalid tool name or classification in TOOLS_JSON")


def make_cli(region: str, profile: str):
    """Return an `aws devops-agent ...` caller. The default calls the real CLI; tests pass a fake."""

    def aws(*args: str) -> dict:
        cmd = ["aws", "devops-agent", *args, "--region", region, "--output", "json"]
        if profile:
            cmd += ["--profile", profile]
        # Argument list without a shell; every value is validated above.
        # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use-tainted-env-args
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout  # nosec B603
        return json.loads(out) if out.strip() else {}

    return aws


def read_classification(aws, space: str, assoc: str) -> dict:
    """Return {tool_name: classification} currently on the association."""
    cfg = aws("get-association", "--agent-space-id", space, "--association-id", assoc)["association"]["configuration"]
    return {d["name"]: d["toolClassification"] for d in cfg["mcpserversigv4"].get("toolDetails", [])}


def classify(aws, space: str, assoc: str, wanted: dict, *, attempts: int = 10, sleep: float = 3.0) -> dict:
    """Set the full tool list + classification in one call, then verify by reading it back.

    Returns the classification read back on success. Raises RuntimeError if, after `attempts` reads,
    any MUTATIVE tool is missing or not classified MUTATIVE, or the classification otherwise drifts
    from `wanted`.
    """
    readonly = {n: c for n, c in wanted.items() if c == "READ_ONLY"}

    def put(tools: dict) -> None:
        # One call sets the tool list and a classification for every listed tool, so a tool is never
        # enabled without its classification.
        details = [{"name": n, "toolClassification": c} for n, c in sorted(tools.items())]
        payload = {
            "agentSpaceId": space,
            "associationId": assoc,
            "configuration": {"mcpserversigv4": {"tools": sorted(tools), "toolDetails": details}},
        }
        aws("update-association", "--cli-input-json", json.dumps(payload))

    # Step 1, safe state first: only the READ_ONLY tools. This also covers a re-run where a tool that
    # was READ_ONLY becomes MUTATIVE: it is removed before it is re-added with its new classification.
    put(readonly)
    # Step 2: the full list, write tools together with their MUTATIVE classification.
    put(wanted)

    mutative_wanted = {n for n, c in wanted.items() if c == "MUTATIVE"}
    got: dict = {}
    read_error = ""
    try:
        for i in range(attempts):
            # The association update is eventually consistent.
            got = read_classification(aws, space, assoc)
            if got == wanted:
                return got
            if i < attempts - 1:
                time.sleep(sleep)  # nosemgrep: arbitrary-sleep
    except Exception as exc:  # noqa: BLE001 - any read-back failure must still roll back
        read_error = f"read-back error ({exc}); "

    # Read-back failed: roll back to the READ_ONLY tool set so no write tool stays exposed.
    try:
        put(readonly)
        rolled_back = read_error + "rolled back to the READ_ONLY tools"
    except Exception as exc:  # noqa: BLE001 - report the rollback failure with the original error
        rolled_back = read_error + f"ROLLBACK FAILED ({exc}): remove the write tools by hand"
    unprotected = sorted(n for n in mutative_wanted if got.get(n) != "MUTATIVE")
    if unprotected:
        raise RuntimeError(f"classification FAILED: MUTATIVE tool(s) not classified MUTATIVE: {unprotected} (read {got}); {rolled_back}")
    raise RuntimeError(f"classification mismatch: expected {wanted}, read {got}; {rolled_back}")


def main() -> int:
    region = os.environ["REGION"]
    profile = os.environ.get("AWS_PROFILE_NAME", "")
    space, assoc = os.environ["AGENT_SPACE_ID"], os.environ["ASSOCIATION_ID"]
    wanted = json.loads(os.environ["TOOLS_JSON"])
    try:
        validate(space, assoc, region, profile, wanted)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    aws = make_cli(region, profile)
    try:
        got = classify(aws, space, assoc, wanted)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"classification OK: {json.dumps(got, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
