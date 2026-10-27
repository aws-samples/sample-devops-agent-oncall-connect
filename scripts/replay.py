# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Replays a recorded "Investigation *" event to the caller, without waiting for a new investigation.

EventBridge refuses PutEvents on aws.* sources, so the caller also listens on a replay source
(Terraform output `oncall.replay_source`). The replay rules exist only when enable_replay = true:
otherwise the event is accepted by EventBridge but no rule matches and nothing is called. The
ticket linked to the recorded task id must exist in the ticket table, otherwise no call is placed.

Usage:
  python3 scripts/replay.py [--event FILE] [--profile NAME]
  FILE defaults to the last "Investigation Completed" event found in the archive log group.
"""

from __future__ import annotations  # also runs under an older python3

import argparse
import json
import pathlib
import subprocess  # nosec B404 - terraform and aws CLI with argument lists, no shell
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TF_DIR = ROOT / "terraform" / "envs" / "demo"


def run(*cmd: str) -> str:
    # Local operator tool: argument list without a shell, values from the CLI user and Terraform outputs.
    # nosemgrep: dangerous-subprocess-use-audit
    return subprocess.run(list(cmd), check=True, capture_output=True, text=True).stdout  # nosec B603


def tf_outputs() -> dict:
    return {k: v["value"] for k, v in json.loads(run("terraform", f"-chdir={TF_DIR}", "output", "-json")).items()}


def aws(profile: str | None, region: str, *args: str) -> dict:
    cmd = ["aws", *args, "--region", region, "--output", "json"] + (["--profile", profile] if profile else [])
    out = run(*cmd)
    return json.loads(out) if out.strip() else {}


def last_completed_event(profile, region, log_group) -> dict:
    events = aws(profile, region, "logs", "filter-log-events", "--log-group-name", log_group, "--filter-pattern", '"Investigation Completed"')[
        "events"
    ]
    if not events:
        sys.exit(f"no 'Investigation Completed' event in {log_group}: run an investigation first")
    return json.loads(events[-1]["message"])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--event", type=pathlib.Path, help="recorded EventBridge event (JSON)")
    p.add_argument("--profile", default=None)
    p.add_argument("--region", default=None, help="AWS DevOps Agent Region (default: from the Agent Space ARN)")
    args = p.parse_args()

    out = tf_outputs()
    region = args.region or out["foundation"]["agent_space_arn"].split(":")[3]
    oncall = out["oncall"]
    event = (
        json.loads(args.event.read_text(encoding="utf-8"))
        if args.event
        else last_completed_event(args.profile, region, out["foundation"]["events_log_group"])
    )

    if not oncall.get("replay_bus_name"):
        sys.exit("replay is off: set enable_replay = true and apply first")
    # The replay rule listens on its own bus, whose policy only lets the deployer principals publish.
    entry = [
        {
            "Source": oncall["replay_source"],
            "DetailType": event["detail-type"],
            "Detail": json.dumps(event["detail"]),
            "EventBusName": oncall["replay_bus_name"],
        }
    ]
    res = aws(args.profile, region, "events", "put-events", "--entries", json.dumps(entry))
    if res.get("FailedEntryCount"):
        sys.exit(f"put-events failed: {res}")
    print(
        f"replayed task {event['detail']['metadata']['task_id']} "
        f"(source {oncall['replay_source']}); caller logs: /aws/lambda/{oncall['caller_function_name']}"
    )


if __name__ == "__main__":
    main()
