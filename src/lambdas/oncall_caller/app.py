# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Calls the on-call engineer through Amazon Connect when an AWS DevOps Agent investigation ends.

Triggered by EventBridge (source aws.aidevops, detail-type Investigation Completed / Failed /
Timed Out / Cancelled; the rule filters on priority).

1. Finds the ticket linked to the investigation (GSI on investigationTaskId).
2. Reads the investigation summary (ListJournalRecords, record investigation_summary_md). The
   event only carries its id (summary_record_id).
3. Claims the ticket and writes the sanitized summary on it (attribute `summary`), then calls
   StartOutboundVoiceContact with the short incident context as contact attributes (ticketId,
   ticketSpoken, service, priority, statusText). The summary is NOT a contact attribute, so it never
   lands in contact records; the contact flow's handler reads it from the ticket.

Safeguards:
- DRY_RUN=true (default): logs the request (both numbers masked) without calling.
- No linked ticket found: the handler raises so the async invocation retries (the GSI is
  eventually consistent, so a just-created ticket can be missing on the first read). It never
  calls Connect with an "unknown" ticket.
- The on-call number comes from an SSM SecureString parameter, never from code or Terraform inputs.
- ClientToken = execution + status: EventBridge redeliveries never call twice. Test replays
  (REPLAY_SOURCE) get a per-event token, because Connect keeps a token for 7 days.
- One call per incident: before StartOutboundVoiceContact the ticket is claimed (status OPEN and no
  callState yet). A second investigation event for the same ticket, or a ticket already acknowledged
  or closed, places no call. On a Connect error the claim is released and the error re-raised, so
  the async retry can call again. The contact id is then bound to the ticket (oncallContactId).
- Logs never carry phone numbers or free text; both numbers are masked and only error
  codes/types are logged.
- The investigation summary is UNTRUSTED (an LLM wrote it over logs/alarm text an attacker may
  influence). It is reduced to plain text, stripped of invisible/control code points, NFKC-normalized,
  stripped of prompt-delimiter look-alikes (ASCII and Unicode), and capped before it is stored on
  the ticket and later read into the AI agent prompt (prompt-injection defence, see
  sanitize_untrusted, the shared module src/common/sanitize.py). The prompt fences it as data and
  tells the model never to follow instructions found inside it.
"""

import json
import os
import re
from datetime import UTC, datetime

import boto3
from boto3.dynamodb.conditions import Key

# The ONE shared untrusted-text sanitizer (src/common/sanitize.py, packaged next to app.py).
from sanitize import MAX_FIELD, MAX_SUMMARY, sanitize_untrusted

TABLE = boto3.resource("dynamodb").Table(os.environ["TICKETS_TABLE"])
DEVOPS = boto3.client("devops-agent", region_name=os.environ["AGENT_SPACE_REGION"])
CONNECT = boto3.client("connect", region_name=os.environ["CONNECT_REGION"])
SSM = boto3.client("ssm")

# Strict E.164: + then 8 to 15 ASCII digits, no leading zero (defence in depth; the Connect
# country allow-list is the primary toll-fraud control).
E164 = re.compile(r"\+[1-9][0-9]{7,14}")
# Spoken labels: "labels" block of src/locales/<language>.json, passed by Terraform.
LABELS = json.loads(os.environ.get("SPEECH_LABELS") or "{}")


def spoken_id(value: str) -> str:
    # Same rule as connect_handler.spoken_id: one character per token so TTS reads "INC-E3974F0B"
    # letter by letter (used by the flow's DTMF confirmation prompt, $.Attributes.ticketSpoken).
    groups = [" ".join(g) for g in value.upper().replace("_", "-").split("-") if g]
    return ", ".join(groups) or "-"


class TicketNotFoundError(Exception):
    """No ticket is linked to the investigation yet. Raising lets the async invocation retry."""


def _mask(number: str) -> str:
    return number[:3] + "*" * max(len(number) - 5, 0) + number[-2:] if number else "<absent>"


def plain_text(md: str) -> str:
    """Markdown -> text that reads well when spoken."""
    md = re.sub(r"`([^`]*)`", r"\1", md)
    md = re.sub(r"[*#>|]", " ", md)
    md = md.replace("_", " ")  # CONFIG_JSON -> "CONFIG JSON"
    return re.sub(r"\s+", " ", md).strip()


def fetch_summary(agent_space_id: str, execution_id: str, record_id: str) -> str:
    kw = {"agentSpaceId": agent_space_id, "executionId": execution_id, "recordType": "investigation_summary_md"}
    while True:
        resp = DEVOPS.list_journal_records(**kw)
        for rec in resp.get("records", []):
            if rec.get("recordId") == record_id:
                return rec.get("content", "")
        if not resp.get("nextToken"):
            return ""
        kw["nextToken"] = resp["nextToken"]


def find_ticket(task_id: str) -> dict | None:
    items = TABLE.query(IndexName="byInvestigation", KeyConditionExpression=Key("investigationTaskId").eq(task_id)).get("Items", [])
    return items[0] if items else None


def build_request(event: dict) -> tuple[dict, dict, str]:
    meta, data = event["detail"]["metadata"], event["detail"]["data"]
    status, priority = data["status"], data.get("priority", "")
    task_id, execution_id = meta["task_id"], meta.get("execution_id", "")

    ticket = find_ticket(task_id)
    if not ticket:
        # The GSI read is eventually consistent: a ticket created moments ago may be missing.
        # Raise so the async invocation retries instead of calling about an unknown incident.
        raise TicketNotFoundError

    summary = ""
    if data.get("summary_record_id") and execution_id:
        summary = plain_text(fetch_summary(meta["agent_space_id"], execution_id, data["summary_record_id"]))

    # Untrusted LLM output: sanitized and capped here, stored on the ticket by claim_call, never a
    # contact attribute (contact records keep only the short context below).
    summary = sanitize_untrusted(summary, MAX_SUMMARY)
    attributes = {
        "ticketId": ticket["ticketId"],
        "ticketSpoken": spoken_id(ticket["ticketId"]),
        "service": sanitize_untrusted(ticket.get("service", ""), MAX_FIELD) or LABELS.get("unknown", "unknown"),
        # Only known values reach the prompt context; anything else (a replayed or forged event) is
        # spoken as "unknown". The event says CANCELLED, UpdateBacklogTask says CANCELED: both mapped.
        "priority": LABELS.get("priority", {}).get(priority, LABELS.get("unknown", "unknown")),
        "statusText": LABELS.get("status", {}).get(status, LABELS.get("unknown", "unknown")),
    }
    token = f"{execution_id or task_id}-{status}"
    if event.get("source") == os.environ.get("REPLAY_SOURCE"):
        token += f"-replay-{event.get('id', '')}"
    request = {
        "InstanceId": os.environ["CONNECT_INSTANCE_ID"],
        "ContactFlowId": os.environ["CONNECT_FLOW_ID"],
        "SourcePhoneNumber": os.environ["CONNECT_SOURCE_NUMBER"],
        "Attributes": attributes,
        "ClientToken": token[:500],
    }
    return request, ticket, summary


def claim_call(ticket_id: str, summary: str = "") -> bool:
    """Conditional claim: only an OPEN ticket that was never called gets a call.

    The same update stores the sanitized summary on the ticket (attribute `summary`), where the
    contact flow's handler reads it; an empty summary is not written (the handler then says
    "summary unavailable").
    """
    update = "SET callState = :calling, callClaimedAt = :t"
    values = {":open": "OPEN", ":calling": "CALLING", ":t": datetime.now(UTC).isoformat()}
    if summary:
        update += ", summary = :sum"
        values[":sum"] = summary
    try:
        TABLE.update_item(
            Key={"ticketId": ticket_id},
            ConditionExpression="#s = :open AND attribute_not_exists(callState)",
            UpdateExpression=update,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
        return True
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def release_call(ticket_id: str) -> None:
    """Undo our claim after a Connect error (only while still CALLING, never after PLACED)."""
    try:
        TABLE.update_item(
            Key={"ticketId": ticket_id},
            ConditionExpression="callState = :calling",
            UpdateExpression="REMOVE callState, callClaimedAt",
            ExpressionAttributeValues={":calling": "CALLING"},
        )
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        pass


def handler(event, context):
    request, ticket, summary = build_request(event)
    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
    number = "" if dry_run else SSM.get_parameter(Name=os.environ["ONCALL_NUMBER_PARAM"], WithDecryption=True)["Parameter"]["Value"].strip()
    # Never log phone numbers or the summary (it is not in the request at all). Both numbers are masked.
    log = {**request, "SourcePhoneNumber": _mask(request["SourcePhoneNumber"]), "DestinationPhoneNumber": _mask(number)}

    if dry_run:
        print(json.dumps({"action": "DRY_RUN_START_OUTBOUND_VOICE_CONTACT", **log}, ensure_ascii=False))
        return {"dryRun": True, "ticketId": request["Attributes"]["ticketId"]}

    if not E164.fullmatch(number):
        raise RuntimeError("on-call number missing or not in E.164 format")
    ticket_id = ticket["ticketId"]
    if not claim_call(ticket_id, summary):
        # Already called for this incident (another investigation event, a redelivery), or the
        # ticket is no longer OPEN (acknowledged or closed): no second call.
        print(json.dumps({"action": "SKIP_CALL", "ticketId": ticket_id, "reason": "not-open-or-already-called"}))
        return {"skipped": "not-open-or-already-called", "ticketId": ticket_id}
    try:
        contact_id = CONNECT.start_outbound_voice_contact(**request, DestinationPhoneNumber=number)["ContactId"]
    except Exception:
        release_call(ticket_id)  # let the async retry place the call
        raise
    print(json.dumps({"action": "START_OUTBOUND_VOICE_CONTACT", "contactId": contact_id, **log}, ensure_ascii=False))
    TABLE.update_item(
        Key={"ticketId": ticket_id},
        UpdateExpression="SET oncallContactId = :c, callState = :placed",
        ExpressionAttributeValues={":c": contact_id, ":placed": "PLACED"},
    )
    return {"contactId": contact_id, "ticketId": ticket_id}
