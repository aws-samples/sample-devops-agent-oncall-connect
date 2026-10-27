# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the MCP tool-classification script (terraform/modules/mcp-ticketing/classify_tools.py).

The script is exercised through a fake DevOps Agent CLI client, so no AWS access is needed. The focus
is the fail-closed guarantee: a run that cannot classify a MUTATIVE tool must fail, so Terraform never
reports success while a write tool is exposed without a MUTATIVE classification.
"""

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("classify_tools", ROOT / "terraform" / "modules" / "mcp-ticketing" / "classify_tools.py")
ct = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ct)

SPACE = "space-1"
ASSOC = "assoc-1"
WANTED = {"list_open_tickets": "READ_ONLY", "get_ticket": "READ_ONLY", "add_comment": "MUTATIVE", "close_ticket": "MUTATIVE"}


class FakeDevOpsAgent:
    """Stands in for `aws devops-agent ...`. Stores the classification set by update-association and
    returns it on get-association. `read_sequence` lets a test force eventual-consistency reads."""

    def __init__(self, read_sequence=None):
        self.updates = []
        self.stored = {}  # name -> classification, as applied by update-association
        self.read_sequence = list(read_sequence) if read_sequence is not None else None

    def __call__(self, *args):
        if args[0] == "update-association":
            payload = json.loads(args[args.index("--cli-input-json") + 1])
            self.updates.append(payload)
            details = payload["configuration"]["mcpserversigv4"].get("toolDetails", [])
            self.stored = {d["name"]: d["toolClassification"] for d in details}
            return {}
        if args[0] == "get-association":
            details = self._next_read()
            return {"association": {"configuration": {"mcpserversigv4": {"toolDetails": details}}}}
        raise AssertionError(f"unexpected call: {args}")

    def _next_read(self):
        if self.read_sequence is not None:
            state = self.read_sequence.pop(0) if len(self.read_sequence) > 1 else self.read_sequence[0]
            return [{"name": n, "toolClassification": c} for n, c in state.items()]
        return [{"name": n, "toolClassification": c} for n, c in self.stored.items()]


def test_update_payload_enables_write_tools_with_mutative_classification():
    """Safe state first (READ_ONLY tools only), then one call with the full list AND toolDetails."""
    fake = FakeDevOpsAgent()
    ct.classify(fake, SPACE, ASSOC, WANTED, sleep=0)
    assert len(fake.updates) == 2
    first = fake.updates[0]["configuration"]["mcpserversigv4"]
    assert sorted(first["tools"]) == ["get_ticket", "list_open_tickets"]
    assert {d["toolClassification"] for d in first["toolDetails"]} == {"READ_ONLY"}
    cfg = fake.updates[1]["configuration"]["mcpserversigv4"]
    assert sorted(cfg["tools"]) == sorted(WANTED)  # write tools are added here, not left out
    got = {d["name"]: d["toolClassification"] for d in cfg["toolDetails"]}
    assert got == WANTED
    assert got["add_comment"] == "MUTATIVE" and got["close_ticket"] == "MUTATIVE"


def test_happy_path_returns_classification():
    fake = FakeDevOpsAgent()
    assert ct.classify(fake, SPACE, ASSOC, WANTED, sleep=0) == WANTED


def test_eventual_consistency_read_retries_then_succeeds():
    """First read misses the write tools; a later read is correct. The script must not fail early."""
    partial = {"list_open_tickets": "READ_ONLY", "get_ticket": "READ_ONLY"}
    fake = FakeDevOpsAgent(read_sequence=[partial, WANTED])
    assert ct.classify(fake, SPACE, ASSOC, WANTED, attempts=3, sleep=0) == WANTED


def test_fails_closed_when_mutative_tool_misclassified_readonly():
    """If the read-back shows a write tool as READ_ONLY, the script must raise (never silently pass)."""
    bad = dict(WANTED, close_ticket="READ_ONLY")
    fake = FakeDevOpsAgent(read_sequence=[bad])
    with pytest.raises(RuntimeError) as exc:
        ct.classify(fake, SPACE, ASSOC, WANTED, attempts=2, sleep=0)
    assert "close_ticket" in str(exc.value)
    assert "MUTATIVE" in str(exc.value)


def test_fails_closed_when_mutative_tool_absent_from_readback():
    """A missing write tool in the read-back is also a fail-closed error naming the tool."""
    missing = {"list_open_tickets": "READ_ONLY", "get_ticket": "READ_ONLY", "add_comment": "MUTATIVE"}
    fake = FakeDevOpsAgent(read_sequence=[missing])
    with pytest.raises(RuntimeError) as exc:
        ct.classify(fake, SPACE, ASSOC, WANTED, attempts=2, sleep=0)
    assert "close_ticket" in str(exc.value)


def test_mismatch_without_mutative_gap_reports_generic_mismatch():
    """A drift that leaves every MUTATIVE-wanted tool MUTATIVE still fails, with the generic message."""
    # Only a READ_ONLY-wanted tool drifts (reads back MUTATIVE); both write tools stay MUTATIVE, so
    # the MUTATIVE-gap branch does not fire and the generic mismatch message is used.
    read = dict(WANTED, get_ticket="MUTATIVE")
    fake = FakeDevOpsAgent(read_sequence=[read])
    with pytest.raises(RuntimeError) as exc:
        ct.classify(fake, SPACE, ASSOC, WANTED, attempts=2, sleep=0)
    assert "mismatch" in str(exc.value)


@pytest.mark.parametrize(
    "space,assoc,region,profile,wanted",
    [
        ("bad space", ASSOC, "eu-west-1", "", WANTED),  # space with a space char
        (SPACE, ASSOC, "not-a-region", "", WANTED),  # bad region
        (SPACE, ASSOC, "eu-west-1", "bad profile!", WANTED),  # bad profile
        (SPACE, ASSOC, "eu-west-1", "", {"close_ticket": "DESTRUCTIVE"}),  # classification not R/O or MUTATIVE
        (SPACE, ASSOC, "eu-west-1", "", {"bad name": "MUTATIVE"}),  # tool name with a space
    ],
)
def test_validate_rejects_bad_input(space, assoc, region, profile, wanted):
    with pytest.raises(ValueError):
        ct.validate(space, assoc, region, profile, wanted)


def test_validate_accepts_known_good_input():
    ct.validate(SPACE, ASSOC, "eu-west-1", "my-profile", WANTED)


def test_failed_readback_rolls_back_to_readonly_tools():
    """If the read-back never matches, the last update must remove the write tools again."""
    bad = dict(WANTED, close_ticket="READ_ONLY")
    fake = FakeDevOpsAgent(read_sequence=[bad])
    with pytest.raises(RuntimeError) as exc:
        ct.classify(fake, SPACE, ASSOC, WANTED, attempts=2, sleep=0)
    assert "rolled back" in str(exc.value)
    last = fake.updates[-1]["configuration"]["mcpserversigv4"]
    assert sorted(last["tools"]) == ["get_ticket", "list_open_tickets"]


def test_reclassified_tool_is_removed_before_it_is_readded():
    """A tool going READ_ONLY -> MUTATIVE is absent from the first (safe) update."""
    fake = FakeDevOpsAgent()
    ct.classify(fake, SPACE, ASSOC, WANTED, sleep=0)
    first_tools = fake.updates[0]["configuration"]["mcpserversigv4"]["tools"]
    assert "close_ticket" not in first_tools and "add_comment" not in first_tools


def test_readback_exception_still_rolls_back():
    """An unexpected get-association failure must not skip the rollback."""

    class Broken(FakeDevOpsAgent):
        def __call__(self, *args):
            if args[0] == "get-association":
                raise KeyError("association")
            return super().__call__(*args)

    fake = Broken()
    with pytest.raises(RuntimeError) as exc:
        ct.classify(fake, SPACE, ASSOC, WANTED, attempts=2, sleep=0)
    assert "read-back error" in str(exc.value) and "rolled back" in str(exc.value)
    last = fake.updates[-1]["configuration"]["mcpserversigv4"]
    assert sorted(last["tools"]) == ["get_ticket", "list_open_tickets"]
