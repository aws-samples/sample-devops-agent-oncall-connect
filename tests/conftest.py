# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared test setup: fake environment so the Lambda modules import without AWS access."""

import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The shared sanitizer is packaged at the root of every Lambda/MCP zip (`import sanitize`): same here.
sys.path.insert(0, str(ROOT / "src" / "common"))
os.environ.update(
    {
        "AWS_DEFAULT_REGION": "eu-west-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",  # pragma: allowlist secret - fake value for boto3 in unit tests
        "TICKETS_TABLE": "tickets",
        "TICKETS_REGION": "eu-west-1",
        "AGENT_SPACE_REGION": "eu-west-1",
        "AGENT_SPACE_ID": "space-1",
        "CONNECT_REGION": "eu-central-1",
        "CONNECT_INSTANCE_ID": "instance-1",
        "CONNECT_FLOW_ID": "flow-1",
        "CONNECT_SOURCE_NUMBER": "+15555550199",
        "ONCALL_NUMBER_PARAM": "/test/oncall-number",
        "REPLAY_SOURCE": "test.replay",
        "MONITORED_FUNCTION": "meter-api",
        "SPEECH_LABELS": json.dumps(json.loads((ROOT / "src" / "locales" / "en-US.json").read_text())["labels"]),
        "NOTIFY_LABELS": json.dumps(json.loads((ROOT / "src" / "locales" / "en-US.json").read_text())["labels"]),
    }
)


def load(name: str):
    """Import src/lambdas/<name>/app.py under a unique module name."""
    path = ROOT / "src" / "lambdas" / name / "app.py"
    spec = importlib.util.spec_from_file_location(f"{name}_app", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod
