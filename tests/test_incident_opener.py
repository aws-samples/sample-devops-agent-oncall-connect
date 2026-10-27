# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import pathlib

import pytest
from conftest import load
from fakes import FakeTable

opener = load("incident_opener")
ROOT = pathlib.Path(__file__).resolve().parent.parent


class FakeDevOps:
    def __init__(self, fail_first=False):
        self.calls, self.fail_first = [], fail_first

    def create_backlog_task(self, **kw):
        self.calls.append(kw)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("throttled")
        return {"task": {"taskId": "task-1", "status": "PENDING"}}


def tickets(table):
    return {k: v for k, v in table.items.items() if k.startswith("INC-")}


def alarm_event(state="ALARM", event_id="evt-1"):
    return {
        "id": event_id,
        "region": "eu-west-1",
        "account": "111122223333",
        "detail": {"alarmName": "meter-api-errors", "state": {"value": state, "reason": "Threshold crossed", "timestamp": "t"}},
    }


@pytest.fixture
def fakes(monkeypatch):
    def install(devops):
        table = FakeTable()
        monkeypatch.setattr(opener, "TABLE", table)
        monkeypatch.setattr(opener, "DEVOPS", devops)
        return table

    return install


def test_opens_ticket_and_investigation(fakes):
    devops = FakeDevOps()
    table = fakes(devops)
    out = opener.handler(alarm_event(), None)
    ticket = table.items[out["ticketId"]]
    assert ticket["status"] == "OPEN" and ticket["investigationTaskId"] == "task-1"
    assert devops.calls[0]["clientToken"] == out["ticketId"] and devops.calls[0]["taskType"] == "INVESTIGATION"


def test_ticket_id_is_stable_per_event():
    assert opener.ticket_id_for(alarm_event()) == opener.ticket_id_for(alarm_event())
    assert opener.ticket_id_for(alarm_event()) != opener.ticket_id_for(alarm_event(event_id="evt-2"))


def test_retry_after_failure_reuses_ticket_and_token(fakes):
    devops = FakeDevOps(fail_first=True)
    table = fakes(devops)
    with pytest.raises(RuntimeError):
        opener.handler(alarm_event(), None)
    out = opener.handler(alarm_event(), None)  # same event redelivered
    assert len(tickets(table)) == 1
    assert devops.calls[0]["clientToken"] == devops.calls[1]["clientToken"] == out["ticketId"]


def test_retry_after_success_does_not_open_twice(fakes):
    devops = FakeDevOps()
    table = fakes(devops)
    first = opener.handler(alarm_event(), None)
    second = opener.handler(alarm_event(), None)
    assert first == second and len(devops.calls) == 1 and len(tickets(table)) == 1


def test_ignores_ok_state(fakes):
    devops = FakeDevOps()
    fakes(devops)
    assert opener.handler(alarm_event(state="OK"), None) == {"skipped": "OK"}
    assert not devops.calls


# --- One incident per alarm until its ticket is CLOSED (lock item) -----------------------------


def test_second_alarm_while_open_comments_on_the_held_ticket(fakes):
    devops = FakeDevOps()
    table = fakes(devops)
    first = opener.handler(alarm_event(event_id="evt-1"), None)
    second = opener.handler(alarm_event(event_id="evt-2"), None)
    assert second == {"ticketId": first["ticketId"], "held": True}
    assert len(devops.calls) == 1 and len(tickets(table)) == 1  # no second investigation, so no second call
    lock = table.items["ALARM#meter-api-errors"]
    assert lock["openTicketId"] == first["ticketId"] and "until" not in lock
    comments = table.items[first["ticketId"]]["comments"]
    assert "entered ALARM again" in comments[-1]["text"] and comments[-1]["by"] == "incident-opener"


@pytest.mark.parametrize("status", ["OPEN", "ACKNOWLEDGED"])
def test_lock_is_held_while_the_ticket_is_not_closed_however_old(fakes, status):
    # No time-based expiry: an old OPEN/ACKNOWLEDGED incident still holds the alarm.
    devops = FakeDevOps()
    table = fakes(devops)
    first = opener.handler(alarm_event(event_id="evt-1"), None)
    table.items[first["ticketId"]]["status"] = status
    table.items["ALARM#meter-api-errors"]["lockedAt"] = "2000-01-01T00:00:00+00:00"
    second = opener.handler(alarm_event(event_id="evt-2"), None)
    assert second == {"ticketId": first["ticketId"], "held": True} and len(devops.calls) == 1


def test_alarm_after_the_ticket_was_closed_opens_a_new_incident(fakes):
    devops = FakeDevOps()
    table = fakes(devops)
    first = opener.handler(alarm_event(event_id="evt-1"), None)
    table.items[first["ticketId"]]["status"] = "CLOSED"
    second = opener.handler(alarm_event(event_id="evt-2"), None)
    assert second["ticketId"] != first["ticketId"] and len(devops.calls) == 2
    assert table.items["ALARM#meter-api-errors"]["openTicketId"] == second["ticketId"]


def test_lock_on_a_ticket_not_written_yet_retries_and_never_takes_over(fakes):
    # A concurrent winner took the lock but has not written its ticket yet.
    devops = FakeDevOps()
    table = fakes(devops)
    table.items["ALARM#meter-api-errors"] = {"ticketId": "ALARM#meter-api-errors", "openTicketId": "INC-0000A001"}
    with pytest.raises(RuntimeError, match="not written yet"):
        opener.handler(alarm_event(event_id="evt-2"), None)
    assert table.items["ALARM#meter-api-errors"]["openTicketId"] == "INC-0000A001"
    assert not tickets(table) and not devops.calls
    # Once the winner wrote its ticket, the redelivered event only comments it.
    table.items["INC-0000A001"] = {"ticketId": "INC-0000A001", "status": "OPEN", "comments": []}
    assert opener.handler(alarm_event(event_id="evt-2"), None) == {"ticketId": "INC-0000A001", "held": True}
    assert not devops.calls


def test_comment_on_open_distinguishes_missing_closed_and_open(fakes):
    table = fakes(FakeDevOps())
    table.items["INC-0000000A"] = {"ticketId": "INC-0000000A", "status": "OPEN"}
    table.items["INC-0000000C"] = {"ticketId": "INC-0000000C", "status": "CLOSED"}
    assert opener.comment_on_open("INC-0000000A", "a", "t", "now") == "commented"
    assert opener.comment_on_open("INC-0000000C", "a", "t", "now") == "closed"
    assert opener.comment_on_open("INC-0000F404", "a", "t", "now") == "missing"
    assert "comments" not in table.items["INC-0000000C"]


def test_no_cooldown_setting_left():
    assert not hasattr(opener, "COOLDOWN_SECONDS")
    for path in [
        "terraform/modules/foundation/main.tf",
        "terraform/envs/demo/main.tf",
        "terraform/envs/demo/variables.tf",
        "terraform/envs/demo/terraform.tfvars.example",
    ]:
        text = (ROOT / path).read_text()
        assert "alarm_cooldown_minutes" not in text and "ALARM_COOLDOWN_SECONDS" not in text, path
