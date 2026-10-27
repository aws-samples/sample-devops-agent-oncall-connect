# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Opens a ticket and an AWS DevOps Agent investigation when the alarm enters ALARM.

Triggered by an EventBridge rule on "CloudWatch Alarm State Change".
1. Creates a ticket in the DynamoDB table that stands in for the ITSM tool.
2. Calls CreateBacklogTask (INVESTIGATION) on the Agent Space.
3. Stores the task id on the ticket, to correlate the later "Investigation *" event with it.

Idempotent: the ticket ID is derived from the EventBridge event id, which stays the same when
EventBridge or Lambda retries the delivery. A retry reuses the ticket, and the same clientToken
stops DevOps Agent from opening a second investigation.

One incident per alarm until it is closed: a lock item per alarm (ticketId = "ALARM#<alarmName>",
fields openTicketId and lockedAt) is taken conditionally before a ticket is opened. A new ALARM
transition opens a new incident only when there is no lock or the held ticket is CLOSED. While the
held ticket is OPEN or ACKNOWLEDGED, the transition only adds a comment to it: no new ticket,
investigation or call, however long the incident stays open. If the held ticket does not exist yet
(a concurrent winner took the lock but has not written its ticket), the invocation raises and is
retried; it never takes the lock over on a missing ticket. The lock item has no INC- id
(get_ticket/add_comment/close_ticket reject it by format) and no status (the voice/notify conditions
on status = OPEN never match it).

Alarm text is UNTRUSTED (an alarm name, and above all its state reason, can carry text a metric or
log author chose): the name, reason and timestamp go through the shared sanitizer
(src/common/sanitize.py) before they reach the ticket (title, comments) or the DevOps Agent task
(title, description). The raw alarm name is kept only as the lock key. Repeated ALARM comments stop
at the per-ticket comment cap (MAX_COMMENTS): a flapping alarm cannot grow the item without
limit; once capped the transition is only logged.
"""

import hashlib
import json
import os
from datetime import UTC, datetime

import boto3

# The ONE shared untrusted-text sanitizer and ticket bounds (src/common/sanitize.py, packaged next to app.py).
from sanitize import MAX_COMMENTS, MAX_FIELD, sanitize_untrusted

TABLE = boto3.resource("dynamodb").Table(os.environ["TICKETS_TABLE"])
DEVOPS = boto3.client("devops-agent", region_name=os.environ["AGENT_SPACE_REGION"])
AGENT_SPACE_ID = os.environ["AGENT_SPACE_ID"]
PRIORITY = os.environ.get("INVESTIGATION_PRIORITY", "HIGH")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "meter-api")
LOCK_CLAIM = "attribute_not_exists(ticketId) OR openTicketId = :tid"
LOCK_TAKEOVER = "openTicketId = :old"
# A repeated-ALARM comment is added only while the ticket is not CLOSED and below the comment cap.
COMMENTABLE = "attribute_exists(ticketId) AND #s <> :closedStatus AND (attribute_not_exists(comments) OR size(comments) < :maxComments)"
MAX_ALARM_NAME = 255  # CloudWatch alarm name limit
MAX_REASON = 1000  # alarm state reason kept on the ticket and in the task description


def ticket_id_for(event: dict) -> str:
    return "INC-" + hashlib.sha256(event["id"].encode()).hexdigest()[:8].upper()


def open_ticket(ticket_id: str, alarm_name: str, reason: str, now: str) -> dict:
    """Create the ticket once; on a retry, return the ticket written by the first attempt."""
    try:
        TABLE.put_item(
            Item={
                "ticketId": ticket_id,
                "status": "OPEN",
                "title": f"Alarm {alarm_name}",
                "service": SERVICE_NAME,
                "priority": PRIORITY,
                "createdAt": now,
                "comments": [{"at": now, "by": "incident-opener", "text": reason}],
            },
            ConditionExpression="attribute_not_exists(ticketId)",
        )
        return {}
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return TABLE.get_item(Key={"ticketId": ticket_id}, ConsistentRead=True).get("Item", {})


def _lock_key(alarm_name: str) -> dict:
    return {"ticketId": f"ALARM#{alarm_name}"}


def take_lock(alarm_name: str, ticket_id: str, now: str, condition: str = LOCK_CLAIM, old: str = "") -> bool:
    """Conditionally bind the alarm to ticket_id. False: another ticket holds it."""
    values = {":tid": ticket_id, ":t": now}
    if condition == LOCK_TAKEOVER:
        values[":old"] = old
    try:
        TABLE.update_item(
            Key=_lock_key(alarm_name),
            ConditionExpression=condition,
            UpdateExpression="SET openTicketId = :tid, lockedAt = :t",
            ExpressionAttributeValues=values,
        )
        return True
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def comment_on_open(open_ticket_id: str, alarm_name: str, timestamp: str, now: str) -> str:
    """Record the repeated ALARM on the held ticket.

    Returns "commented" (the ticket is not CLOSED), "capped" (not CLOSED but at its comment cap: no
    comment, same outcome as commented), "closed" (the held ticket is CLOSED: the caller may take the
    lock over) or "missing" (no item yet: a concurrent winner took the lock and has not written its
    ticket, so the caller must retry, never take over). alarm_name and timestamp are already sanitized.
    """
    text = f"alarm {alarm_name} entered ALARM again at {timestamp} (incident still open: no new investigation or call)"
    try:
        TABLE.update_item(
            Key={"ticketId": open_ticket_id},
            ConditionExpression=COMMENTABLE,
            UpdateExpression="SET comments = list_append(if_not_exists(comments, :e), :cm)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":closedStatus": "CLOSED",
                ":maxComments": MAX_COMMENTS,
                ":e": [],
                ":cm": [{"at": now, "by": "incident-opener", "text": text}],
            },
        )
        return "commented"
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        # Missing vs CLOSED: a consistent read sees the item a concurrent winner just wrote.
        item = TABLE.get_item(Key={"ticketId": open_ticket_id}, ConsistentRead=True).get("Item")
        if item is None:
            return "missing"
        if item.get("status") == "CLOSED":
            return "closed"
        return "capped" if len(item.get("comments", [])) >= MAX_COMMENTS else "commented-race"


def handler(event, context):
    detail = event["detail"]
    if detail["state"]["value"] != "ALARM":
        return {"skipped": detail["state"]["value"]}

    alarm_name = detail["alarmName"]  # raw: the lock key only
    now = datetime.now(UTC).isoformat()
    # Untrusted alarm text, sanitized once for every place it is written (ticket, task).
    alarm_text = sanitize_untrusted(alarm_name, MAX_ALARM_NAME) or "-"
    reason = sanitize_untrusted(detail["state"].get("reason", ""), MAX_REASON)
    timestamp = sanitize_untrusted(detail["state"].get("timestamp", ""), MAX_FIELD) or now
    ticket_id = ticket_id_for(event)

    if not take_lock(alarm_name, ticket_id, now):
        # Another ticket holds the alarm (ConsistentRead: the winner's write).
        lock = TABLE.get_item(Key=_lock_key(alarm_name), ConsistentRead=True).get("Item", {})
        open_id = lock.get("openTicketId", "")
        if not open_id:
            raise RuntimeError("alarm lock without a ticket, retrying")
        outcome = comment_on_open(open_id, alarm_text, timestamp, now)
        if outcome == "missing":
            # The winner took the lock but its ticket is not written yet: retry, never take over.
            raise RuntimeError("held ticket not written yet, retrying")
        if outcome != "closed":
            # OPEN or ACKNOWLEDGED (commented or capped; or it reopened between the two reads, retry then).
            if outcome not in ("commented", "capped"):
                raise RuntimeError("held ticket changed concurrently, retrying")
            print(json.dumps({"ticketId": open_id, "alarm": alarm_text, "held": True, "capped": outcome == "capped"}))
            return {"ticketId": open_id, "held": True}
        # The held ticket is CLOSED: take the lock over (conditional on the same holder) and open a
        # new incident. Losing this race means another invocation just did it: retry from the top.
        if not take_lock(alarm_name, ticket_id, now, condition=LOCK_TAKEOVER, old=open_id):
            raise RuntimeError("alarm lock changed concurrently, retrying")

    existing = open_ticket(ticket_id, alarm_text, reason or "-", now)
    if existing.get("investigationTaskId"):
        print(json.dumps({"ticketId": ticket_id, "taskId": existing["investigationTaskId"], "retry": True}))
        return {"ticketId": ticket_id, "taskId": existing["investigationTaskId"]}

    description = (
        f"CloudWatch alarm {alarm_text} entered ALARM at {timestamp}.\n"
        f"Reason (untrusted alarm text): {reason or 'n/a'}\n"
        f"Affected service: Lambda function {os.environ['MONITORED_FUNCTION']} ({SERVICE_NAME}), "
        f"region {event['region']}, account {event['account']}.\n"
        f"Related ticket: {ticket_id}.\n"
        "Find the root cause of the errors and propose a mitigation."
    )
    task = DEVOPS.create_backlog_task(
        agentSpaceId=AGENT_SPACE_ID,
        taskType="INVESTIGATION",
        priority=PRIORITY,
        title=f"[{ticket_id}] {SERVICE_NAME} errors ({alarm_text})",
        description=description,
        clientToken=ticket_id,
    )["task"]

    TABLE.update_item(
        Key={"ticketId": ticket_id},
        UpdateExpression="SET investigationTaskId = :t",
        ExpressionAttributeValues={":t": task["taskId"]},
    )
    print(json.dumps({"ticketId": ticket_id, "taskId": task["taskId"], "status": task.get("status")}))
    return {"ticketId": ticket_id, "taskId": task["taskId"]}
