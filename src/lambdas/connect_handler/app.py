# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda invoked by the contact flow (runs in the Amazon Connect Region).

Actions, selected by the `action` parameter of the Invoke Lambda block:
- init: copies the incident context into the AI agent session (UpdateSessionData, namespace
  Custom); the prompt reads {{$.Custom.<key>}}. The short fields come from the contact attributes the
  caller set; the summary is read from the ticket item (GetItem, ConsistentRead), never from the
  contact, so it never lands in contact records. Every field is re-sanitized (sanitize_untrusted) and
  the ticketId must match ^INC-[0-9A-F]{8}$ (else the invocation fails and the flow takes its
  fallback branch: no AI session for a forged or malformed id).
- verify_pin: optional recipient check (enable_pin). Constant-time compares the DTMF digits the
  caller entered against an on-call PIN read from an SSM SecureString (set outside IaC, like the
  on-call number). The PIN and the entered digits are never logged. Returns result=ok or result=no.
- ack: writes the acknowledgment in the ticket. The flow invokes it only after the AI agent returned
  the Acknowledge tool AND the callee pressed 1 to confirm (ackMethod voice+dtmf): the model alone
  never acknowledges. The ticket ID comes from the contact attributes set by the caller, never from
  the model. The write is bound to the call the caller placed (ticket.oncallContactId = this
  ContactId), so a direct invoke with a forged ContactId cannot acknowledge a ticket and bypass the
  DTMF confirmation. The model's `note` is sanitized (sanitize_untrusted, 200 characters) before it
  is stored in the ticket comment. Each Acknowledge writes ONE structured audit line
  ({"audit": "ai_tool_call", "tool": "Acknowledge", ticketId, contactId, result, noteLength}, no free
  text): the AI's tool calls are traceable without flow logging or call recording.
- notify: nobody acknowledged by voice (voicemail, no key pressed, declined, PIN not verified).
  On the flow path the claim is bound to the call the caller placed (oncallContactId = this
  ContactId); an unbound contact gets result=unbound, which the flow treats like its error path. The
  pre-claim reads (on-call number, email recipient, ticket summary; the summary only when email is
  configured) run in parallel. Claims the ticket with a short lease, then sends an SMS (AWS End User Messaging) and/or an email
  (Amazon SES), each only when configured. A failed channel does not stop the other. Both recipients
  are read at send time from SSM SecureStrings set outside IaC: the SMS goes to the configured
  on-call number (/<prefix>/oncall-number, DevOps Agent Region), never to a number taken from the
  contact; the email recipient lives in /<prefix>/oncall-email (Connect Region).

The same function is also the target of an EventBridge rule on Amazon Connect contact events,
delivered through an SQS queue (batch of 1, ReportBatchItemFailures). A call that is never
acknowledged still emits DISCONNECTED, so the event drives the same notify path. The event path
reads the ticket context back from the contact, derives `reason` from the contact event, and acts
only when the event's contactId is the ticket's oncallContactId (the call the caller placed). On an
ACKNOWLEDGED ticket it sends ONE acknowledgment receipt ("you acknowledged INC-x at T") over the
same channels, so a wrong acknowledgment is visible to the engineer in writing.

An ACKNOWLEDGED ticket is never re-paged (no call, SMS or email after the receipt), whatever happens
next. This is deliberate: the sample has no signal that tells "someone works on it" from "nobody
does" (progress lives in the engineer's tools, not in the ticket), and a timer-based re-page would
add a scheduler, a second state machine and a new toll and spam surface. The written receipt is the
control for a wrong acknowledgment: the engineer who did not take the incident sees it and reopens
the work out of band (DevOps Agent chat, the ITSM tool). Deployers who need escalation on no
progress should drive it from their ITSM tool's SLA timers.

Notification state machine on the ticket item (one claim per ticket, owner = this invocation):
- notifyState in {CLAIMED, SENT, FAILED}, leaseUntil (epoch seconds, number),
  notifyOwner (= context.aws_request_id), notifiedAt (ISO, set on SENT), notifiedContactId.
- Claim: status OPEN AND (no notifyState OR notifyState = FAILED OR an expired CLAIMED lease).
- Success: owner-conditioned update sets SENT and appends the ticket comment, atomically.
- Every channel failed: owner-conditioned update sets FAILED and appends ONE failure comment.
- No channel configured: comment only, counts as SENT (documented).

Acknowledgment receipt (same pattern, separate fields): receiptState in {CLAIMED, FAILED},
receiptOwner, receiptLeaseUntil, ackReceiptAt (ISO, set once on success: never sent twice).

Delivery is at-least-once: an SDK timeout after a provider accepted a message can cause a resend.

Every value filled into an SMS or email template (_fill) is passed through sanitize_untrusted, whatever
its origin. Ticket ids are checked against ^INC-[0-9A-F]{8}$ (is_ticket_id) on every path.

Returns a STRING_MAP to the flow (values only [A-Za-z0-9_-]). Logs error codes/types only, never
phone numbers, email addresses, PINs or free text.
"""

import hmac
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import boto3
from botocore.config import Config

# The ONE shared untrusted-text sanitizer and ticket bounds (src/common/sanitize.py, packaged next to app.py).
from sanitize import MAX_FIELD, MAX_SUMMARY, is_ticket_id, sanitize_untrusted

# Short timeouts and a single attempt on every client (DynamoDB and Q Connect included): one slow
# dependency never starves the others inside the flow's 8 s budget.
FAST = Config(connect_timeout=1, read_timeout=2, retries={"total_max_attempts": 1})
TABLE = boto3.resource("dynamodb", region_name=os.environ["TICKETS_REGION"], config=FAST).Table(os.environ["TICKETS_TABLE"])
QCONNECT = boto3.client("qconnect", config=FAST)
CONNECT = boto3.client("connect", config=FAST)
SMS = boto3.client("pinpoint-sms-voice-v2", config=FAST)
SES = boto3.client("sesv2", config=FAST)
SSM = boto3.client("ssm", config=FAST)
# The on-call number lives in the DevOps Agent Region (same Region as the ticket table).
ONCALL_SSM = boto3.client("ssm", region_name=os.environ["TICKETS_REGION"], config=FAST)
ONCALL_NUMBER_PARAM = os.environ.get("ONCALL_NUMBER_PARAM", "")
# Short context fields carried as contact attributes. The summary is NOT one of them:
# it lives on the ticket item and is read with GetItem.
CONTEXT_KEYS = ("ticketId", "service", "priority", "statusText")
ACK_TEXT = os.environ.get("ACK_TEXT", "acknowledged by voice through the Connect AI agent")
# The flow reaches ack only after the AI agent's Acknowledge AND a DTMF "1" confirmation.
ACK_METHOD = "voice+dtmf"
# Labels of src/locales/<language>.json used by notify (passed by Terraform).
LABELS = json.loads(os.environ.get("NOTIFY_LABELS") or "{}")
SMS_ORIGINATION_IDENTITY = os.environ.get("SMS_ORIGINATION_IDENTITY", "")
EMAIL_FROM = os.environ.get("NOTIFY_EMAIL_FROM", "")
EMAIL_TO_PARAM = os.environ.get("NOTIFY_EMAIL_TO_PARAM", "")
# On-call PIN (optional recipient gate): SSM SecureString name, set only when enable_pin is true.
# Empty means PIN verification is off; the action is then never invoked by the flow.
PIN_PARAM = os.environ.get("ONCALL_PIN_PARAM", "")
# Claim lease: Terraform passes 3x the Lambda timeout so a slow invocation keeps the claim long
# enough to finish, and a dead invocation releases it for the next attempt.
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "24"))
MAX_SMS = 600  # a few segments at most
MAX_NOTE = 200  # the AI agent's Acknowledge note (model output) stored in the ticket comment
# Keep this much of the Lambda budget for the final DynamoDB write that records the outcome.
FINAL_WRITE_MS = 1500
# One send can take a full connect+read round-trip (FAST: 1 s connect + 2 s read) before it returns.
# Reserve that plus the final write, so the guard never lets a send start that would overrun the
# timeout mid-flight and leave the ticket CLAIMED with the outcome unrecorded.
SEND_BUDGET_MS = (1 + 2) * 1000
MIN_BUDGET_MS = SEND_BUDGET_MS + FINAL_WRITE_MS
# Only DTMF digits are ever a valid PIN entry: everything else fails closed without a provider call.
_DIGITS = re.compile(r"\A[0-9]{1,32}\Z")


def _remaining_ms(context) -> int:
    try:
        return context.get_remaining_time_in_millis()
    except AttributeError:
        return FINAL_WRITE_MS * 10  # no context (unit tests): never trip the guard


def _session_ids(session_arn: str) -> tuple[str, str]:
    # arn:aws:wisdom:<region>:<account>:session/<assistantId>/<sessionId>
    parts = session_arn.split("/")
    if len(parts) < 3 or ":session" not in parts[0]:
        raise ValueError(f"invalid AI agent session ARN: {session_arn!r}")
    return parts[-2], parts[-1]


def spoken_id(value: str) -> str:
    # Text-to-speech reads "INC-E3974F0B" as words ("F0B" -> "byte"): one character per token, a comma
    # (spoken pause) in place of each separator, so the prompt can say it verbatim in any locale.
    groups = [" ".join(g) for g in value.upper().replace("_", "-").split("-") if g]
    return ", ".join(groups) or "-"


def spoken_name(value: str) -> str:
    # "meter-api" is read "meterapy": separators become spaces.
    return value.replace("-", " ").replace("_", " ").strip() or "-"


def _context(attrs: dict) -> dict:
    """The short context fields from contact attributes, re-sanitized (any attribute could be set out of
    band on another contact; the summary is never taken from here)."""
    return {k: sanitize_untrusted(attrs.get(k) or "", MAX_FIELD) for k in CONTEXT_KEYS}


def _summary_text(summary: str) -> str:
    return sanitize_untrusted(summary or "") or LABELS.get("summary_unavailable", "Summary unavailable.")


def _ticket_summary(ticket_id: str) -> str:
    """The sanitized summary the caller stored on the ticket, or the "summary unavailable" label.

    A read error never blocks the call or the notification: it is logged by type and the label is used.
    """
    if not is_ticket_id(ticket_id):
        return _summary_text("")
    try:
        item = TABLE.get_item(
            Key={"ticketId": ticket_id},
            ConsistentRead=True,
            ProjectionExpression="#sum",
            ExpressionAttributeNames={"#sum": "summary"},
        ).get("Item", {})
    except Exception as err:  # noqa: BLE001 - without the summary the call still goes on
        print({"action": "summary", "ticketId": ticket_id, "error": type(err).__name__})
        return _summary_text("")
    return _summary_text(item.get("summary", ""))


def init(params: dict) -> dict:
    assistant_id, session_id = _session_ids(params.get("sessionArn", ""))
    fields = _context(params)
    if not is_ticket_id(fields["ticketId"]):
        # A forged or malformed id never opens the AI session: the flow takes its fallback branch.
        print({"action": "init", "sessionId": session_id, "error": "invalid_ticket"})
        raise ValueError("invalid ticketId")
    # The summary is the only free-text, attacker-influenceable field: read from the ticket (never from
    # the flow parameters) and sanitized again before it is written into the session the prompt reads.
    fields["summary"] = _ticket_summary(fields["ticketId"])
    data = [{"key": k, "value": {"stringValue": v or "-"}} for k, v in fields.items()]
    data += [
        {"key": "ticketSpoken", "value": {"stringValue": spoken_id(fields["ticketId"])}},
        {"key": "serviceSpoken", "value": {"stringValue": spoken_name(fields["service"])}},
    ]
    QCONNECT.update_session_data(assistantId=assistant_id, sessionId=session_id, namespace="Custom", data=data)
    print({"action": "init", "sessionId": session_id, "ticketId": fields["ticketId"]})
    return {"result": "ok"}


def verify_pin(params: dict) -> dict:
    """Constant-time compare the entered DTMF digits with the on-call PIN (SSM SecureString).

    The PIN and the entered digits are never logged (only ok/no and an error type on a read failure).
    A missing parameter, an unset value, a stored value that is not 1-32 ASCII digits, a read error or
    non-digit input all fail closed (result=no), so the flow routes to its retry/notify path rather
    than opening the AI session. Limits: two attempts per call (the flow), no lockout
    across calls, strength chosen by the deployer (use 6 digits or more): it is a bystander control
    only, not authentication.
    """
    entered = (params.get("pin") or "").strip()
    if not PIN_PARAM or not _DIGITS.match(entered):
        print({"action": "verify_pin", "result": "no", "reason": "no_input"})
        return {"result": "no"}
    try:
        expected = SSM.get_parameter(Name=PIN_PARAM, WithDecryption=True)["Parameter"]["Value"].strip()
    except Exception as err:  # noqa: BLE001 - a read error must fail closed, not open the session
        print({"action": "verify_pin", "result": "no", "error": type(err).__name__})
        return {"result": "no"}
    if not expected or expected == "UNSET":
        print({"action": "verify_pin", "result": "no", "reason": "unset"})
        return {"result": "no"}
    if not _DIGITS.match(expected):
        # A stored value that is not DTMF digits can never be entered (and a non-ASCII one would make
        # compare_digest raise): refuse explicitly, logged by reason only.
        print({"action": "verify_pin", "result": "no", "reason": "invalid_stored_pin"})
        return {"result": "no"}
    ok = hmac.compare_digest(entered, expected)
    print({"action": "verify_pin", "result": "ok" if ok else "no"})
    return {"result": "ok" if ok else "no"}


ACK_CONDITION = "#s = :open AND oncallContactId = :c"


def _audit(tool: str, ticket_id: str, contact_id: str, result: str, **counts: int) -> None:
    """One structured line per AI agent tool call: ids, outcome and lengths, never free text."""
    line = {"audit": "ai_tool_call", "tool": tool, "ticketId": ticket_id, "contactId": contact_id or "-", "result": result, **counts}
    print(json.dumps(line))


def ack(params: dict, contact_id: str) -> dict:
    ticket_id = params.get("ticketId", "")
    if not is_ticket_id(ticket_id):
        _audit("Acknowledge", "-", contact_id, "invalid_ticket")
        raise ValueError("invalid ticketId")
    if not contact_id:
        _audit("Acknowledge", ticket_id, "-", "missing_contact")
        raise ValueError("missing ContactId")
    # The note is model output (it can echo injected text): same sanitizer as the summary, shorter cap.
    note = sanitize_untrusted(params.get("note") or "", MAX_NOTE)
    now = datetime.now(UTC).isoformat()
    text = f"{ACK_TEXT}: {note}" if note else ACK_TEXT
    try:
        # Only an OPEN ticket can be acknowledged: a second Acknowledge in the same call, or a ticket
        # already closed from the DevOps Agent chat, is a no-op instead of another comment. And only
        # from the call the caller placed for it (oncallContactId): a direct invoke with any other
        # ContactId cannot acknowledge (and so cannot skip the DTMF confirmation or the SMS/email).
        TABLE.update_item(
            Key={"ticketId": ticket_id},
            ConditionExpression=ACK_CONDITION,
            UpdateExpression=(
                "SET #s = :s, acknowledgedAt = :t, acknowledgedContactId = :c, ackMethod = :m,"
                " comments = list_append(if_not_exists(comments, :e), :cm)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":open": "OPEN",
                ":s": "ACKNOWLEDGED",
                ":t": now,
                ":c": contact_id,
                ":m": ACK_METHOD,
                ":e": [],
                ":cm": [{"at": now, "by": "connect-ai-agent", "text": f"{text} (ackMethod: {ACK_METHOD})"}],
            },
        )
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        _audit("Acknowledge", ticket_id, contact_id, "not-open", noteLength=len(note))
        return {"result": "not-open", "ticketId": ticket_id}
    _audit("Acknowledge", ticket_id, contact_id, "ok", noteLength=len(note))
    return {"result": "ok", "ticketId": ticket_id}


class _Blank(dict):
    def __missing__(self, key):
        return "-"


def _fill(template: str, params: dict) -> str:
    # Missing or unknown keys become "-" so a partial context or a locale typo never raises. Every value
    # goes through the shared sanitizer, whatever its origin: the template (locale file) is
    # trusted, the values never are.
    values = {k: sanitize_untrusted(v, MAX_SUMMARY) or "-" for k, v in params.items() if isinstance(v, str)}
    return template.format_map(_Blank(values))


def _oncall_number() -> str:
    """The configured on-call number (SSM SecureString, DevOps Agent Region), never the contact's.

    "UNSET", a non-E.164 value or a read error means no SMS (logged by error type only); the email
    channel still goes out.
    """
    if not ONCALL_NUMBER_PARAM:
        return ""
    try:
        value = ONCALL_SSM.get_parameter(Name=ONCALL_NUMBER_PARAM, WithDecryption=True)["Parameter"]["Value"].strip()
    except Exception as err:  # noqa: BLE001 - without the number the email still goes out
        print({"action": "notify", "channel": "sms", "error": type(err).__name__})
        return ""
    return value if value.startswith("+") else ""


def _email_to() -> str:
    # "UNSET" (the IaC placeholder), an empty value or a read error means no email.
    if not EMAIL_TO_PARAM:
        return ""
    try:
        value = SSM.get_parameter(Name=EMAIL_TO_PARAM, WithDecryption=True)["Parameter"]["Value"].strip()
    except Exception as err:  # noqa: BLE001 - a missing recipient must not stop the SMS
        print({"action": "notify", "channel": "email", "error": type(err).__name__})
        return ""
    return value if "@" in value else ""


def _send_sms(phone: str, body: str) -> bool:
    if not (SMS_ORIGINATION_IDENTITY and phone.startswith("+")):
        return False
    try:
        SMS.send_text_message(
            DestinationPhoneNumber=phone,
            OriginationIdentity=SMS_ORIGINATION_IDENTITY,
            MessageBody=body[:MAX_SMS],
            MessageType="TRANSACTIONAL",
        )
        return True
    except Exception as err:  # noqa: BLE001 - one channel failing must not stop the other
        print({"action": "notify", "channel": "sms", "error": type(err).__name__})
        return False


def _send_email(email_to: str, subject: str, body: str) -> bool:
    if not email_to:
        return False
    try:
        SES.send_email(
            FromEmailAddress=EMAIL_FROM,
            Destination={"ToAddresses": [email_to]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject[:200]},
                    "Body": {"Text": {"Data": body}},
                }
            },
        )
        return True
    except Exception as err:  # noqa: BLE001
        print({"action": "notify", "channel": "email", "error": type(err).__name__})
        return False


def _send(params: dict, phone: str, email_to: str, kind: str = "notify") -> list[str]:
    # SMS and email run in parallel: a slow channel never pushes the other past the time budget.
    # kind selects the locale texts: "notify" (not acknowledged) or "ack_receipt" (receipt).
    defaults = {"sms": "On-call incident {ticketId}", "email_subject": "On-call incident {ticketId}", "email_body": "{summary}"}
    sms_body = _fill(LABELS.get(f"{kind}_sms", defaults["sms"]), params)
    subject = _fill(LABELS.get(f"{kind}_email_subject", defaults["email_subject"]), params)
    body = _fill(LABELS.get(f"{kind}_email_body", defaults["email_body"]), params)
    sent = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        sms_future = pool.submit(_send_sms, phone, sms_body)
        email_future = pool.submit(_send_email, email_to, subject, body)
        if sms_future.result():
            sent.append("sms")
        if email_future.result():
            sent.append("email")
    return sent


NOTIFY_CLAIM = "#s = :open AND (attribute_not_exists(notifyState) OR notifyState = :failed OR (notifyState = :claimed AND leaseUntil < :now))"
# Flow path: the claim also requires the ticket to be bound to this contact (the call the caller placed).
NOTIFY_CLAIM_BOUND = NOTIFY_CLAIM + " AND oncallContactId = :c"


def _claim(ticket_id: str, owner: str, now_epoch: int, bound_contact: str = "") -> bool:
    values = {
        ":open": "OPEN",
        ":failed": "FAILED",
        ":claimed": "CLAIMED",
        ":now": now_epoch,
        ":me": owner,
        ":lease": now_epoch + LEASE_SECONDS,
    }
    if bound_contact:
        values[":c"] = bound_contact
    try:
        TABLE.update_item(
            Key={"ticketId": ticket_id},
            ConditionExpression=NOTIFY_CLAIM_BOUND if bound_contact else NOTIFY_CLAIM,
            UpdateExpression="SET notifyState = :claimed, notifyOwner = :me, leaseUntil = :lease",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
        return True
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _claim_item(ticket_id: str) -> dict:
    # The claim could not be taken: read why. ConsistentRead so the state reflects the claim the
    # winner just wrote (needs dynamodb:GetItem).
    return TABLE.get_item(Key={"ticketId": ticket_id}, ConsistentRead=True).get("Item", {})


def _held_unexpired(item: dict, now_epoch: int) -> bool:
    # If another invocation holds an unexpired CLAIMED lease the event path must retry later (not
    # skip): it may be the loser of a race the winner has not finished.
    return item.get("notifyState") == "CLAIMED" and int(item.get("leaseUntil", 0)) >= now_epoch


def _mark_sent(ticket_id: str, owner: str, now_iso: str, contact_id: str, text: str) -> None:
    TABLE.update_item(
        Key={"ticketId": ticket_id},
        ConditionExpression="notifyOwner = :me",
        UpdateExpression=(
            "SET notifyState = :sent, notifiedAt = :t, notifiedContactId = :c,"
            " comments = list_append(if_not_exists(comments, :e), :cm) REMOVE leaseUntil"
        ),
        ExpressionAttributeValues={
            ":me": owner,
            ":sent": "SENT",
            ":t": now_iso,
            ":c": contact_id,
            ":e": [],
            ":cm": [{"at": now_iso, "by": "connect-flow", "text": text}],
        },
    )


def _mark_failed(ticket_id: str, owner: str, now_iso: str, label: str = "notify_failed") -> None:
    # Owner-conditioned release to FAILED. The failure comment is written at most once per ticket
    # (dedupe on notifyFailedAt) so repeated attempts never stack it; a wrong owner still raises.
    defaults = {
        "notify_failed": "notification could not be sent (SMS and email both failed)",
        "notify_no_budget": "notification not attempted (no time left in this attempt), it will be retried",
    }
    text = LABELS.get(label, defaults.get(label, defaults["notify_failed"]))
    try:
        TABLE.update_item(
            Key={"ticketId": ticket_id},
            ConditionExpression="notifyOwner = :me AND attribute_not_exists(notifyFailedAt)",
            UpdateExpression=(
                "SET notifyState = :failed, notifyFailedAt = :t, comments = list_append(if_not_exists(comments, :e), :cm) REMOVE leaseUntil"
            ),
            ExpressionAttributeValues={
                ":me": owner,
                ":failed": "FAILED",
                ":t": now_iso,
                ":e": [],
                ":cm": [{"at": now_iso, "by": "connect-flow", "text": text}],
            },
        )
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        # The comment already exists (an earlier attempt failed): release to FAILED without a second
        # comment. Still owner-conditioned, so a wrong owner raises here instead of silently winning.
        TABLE.update_item(
            Key={"ticketId": ticket_id},
            ConditionExpression="notifyOwner = :me",
            UpdateExpression="SET notifyState = :failed, notifyFailedAt = :t REMOVE leaseUntil",
            ExpressionAttributeValues={":me": owner, ":failed": "FAILED", ":t": now_iso},
        )


def notify(
    params: dict,
    contact_id: str,
    phone: str,
    owner: str,
    context=None,
    reason: str = "no_answer",
    email_to: str | None = None,
    bound_contact: str = "",
) -> dict:
    """Claim the ticket, send on the configured channels, record the outcome.

    On the flow path this runs inside the Invoke Lambda block: any exception that escapes (a
    non-conditional DynamoDB error on claim or mark, a GetItem failure) is caught by the flow's
    Error branch, which plays the plain closing message. On the event path the SQS record is then
    retried. The return value is the normal (non-exception) outcome.

    email_to: the recipient already read by the caller (flow path, parallel reads); None reads it here.
    bound_contact: flow path only, the claim also requires ticket.oncallContactId = bound_contact; a
    contact that is not the placed call gets result=unbound (the flow plays its error-path message).
    The event path checks the binding itself before calling notify.
    """
    ticket_id = params.get("ticketId", "")
    if not is_ticket_id(ticket_id):
        print({"action": "notify", "result": "skipped", "error": "invalid_ticket"})
        return {"result": "skipped", "sent": "none"}
    # Read the recipient before the claim: the SSM round-trip stays outside the lease window and a
    # read error never leaves a claimed ticket.
    if email_to is None:
        email_to = _email_to() if EMAIL_FROM else ""
    now_epoch = int(time.time())
    if not _claim(ticket_id, owner, now_epoch, bound_contact):
        item = _claim_item(ticket_id)
        if bound_contact and item.get("oncallContactId") != bound_contact:
            print({"action": "notify", "ticketId": ticket_id, "contactId": contact_id, "result": "unbound"})
            return {"result": "unbound", "sent": "none"}
        held = _held_unexpired(item, now_epoch)
        print({"action": "notify", "ticketId": ticket_id, "contactId": contact_id, "result": "skipped", "held": held})
        # `held` lets the event path report an item failure (retry later) rather than skip for good.
        return {"result": "skipped", "sent": "none", "held": held}

    now_iso = datetime.now(UTC).isoformat()
    # No channel configured: comment only, counts as SENT (nothing to send, nothing to retry).
    if not (SMS_ORIGINATION_IDENTITY or EMAIL_FROM):
        text = LABELS.get(reason, LABELS.get("no_answer", "not acknowledged by voice"))
        _mark_sent(ticket_id, owner, now_iso, contact_id, text)
        print({"action": "notify", "ticketId": ticket_id, "contactId": contact_id, "reason": reason, "sent": []})
        return {"result": "ok", "sent": "none"}

    # Guard the time budget: reserve one full send round-trip plus the final DynamoDB write before
    # touching the channels. Below that, mark FAILED now (reclaimable) so a retry sends later.
    if _remaining_ms(context) < MIN_BUDGET_MS:
        _mark_failed(ticket_id, owner, now_iso, label="notify_no_budget")
        print({"action": "notify", "ticketId": ticket_id, "contactId": contact_id, "result": "failed", "error": "no_time_budget"})
        return {"result": "failed", "sent": "none"}

    sent = _send(params, phone.strip(), email_to)
    now_iso = datetime.now(UTC).isoformat()
    if not sent:
        _mark_failed(ticket_id, owner, now_iso)
        print({"action": "notify", "ticketId": ticket_id, "contactId": contact_id, "result": "failed", "sent": []})
        return {"result": "failed", "sent": "none"}

    text = LABELS.get(reason, LABELS.get("no_answer", "not acknowledged by voice"))
    text += f"; {LABELS.get('notified', 'notification sent by')} {', '.join(sent)}"
    _mark_sent(ticket_id, owner, now_iso, contact_id, text)
    print({"action": "notify", "ticketId": ticket_id, "contactId": contact_id, "reason": reason, "sent": sent})
    return {"result": "ok", "sent": "notified"}


def _notify_reads(ticket_id: str) -> tuple[str, str, str]:
    """Flow-path pre-claim reads, in parallel so their worst case stays one FAST round-trip.

    - on-call number: the configured SSM number, never a number passed by the flow or read from the
      contact, so it never travels through flow logs and cannot be redirected;
    - email recipient (SSM);
    - ticket summary (the flow passes only the short context): only the email body uses it, so it is
      not read at all when email is not configured.
    """
    email_on = bool(EMAIL_FROM and EMAIL_TO_PARAM)
    with ThreadPoolExecutor(max_workers=3) as pool:
        phone = pool.submit(_oncall_number) if SMS_ORIGINATION_IDENTITY else None
        email_to = pool.submit(_email_to) if email_on else None
        summary = pool.submit(_ticket_summary, ticket_id) if email_on else None
        return (
            phone.result() if phone else "",
            email_to.result() if email_to else "",
            summary.result() if summary else _summary_text(""),
        )


def _reason(detail: dict) -> str:
    # For an API-initiated call connectedToSystemTimestamp is set when the customer endpoint answers
    # (voicemail counts as answered). Present -> answered but not acknowledged (hang-up, decline,
    # fallback); absent -> never answered.
    return "not_acknowledged" if detail.get("connectedToSystemTimestamp") else "not_connected"


# --- Acknowledgment receipt (event path) ----------------------------------------------------------

RECEIPT_CLAIM = (
    "#s = :acked AND attribute_not_exists(ackReceiptAt) AND (attribute_not_exists(receiptState)"
    " OR receiptState = :failed OR (receiptState = :claimed AND receiptLeaseUntil < :now))"
)


def _receipt_claim(ticket_id: str, owner: str, now_epoch: int) -> bool:
    try:
        TABLE.update_item(
            Key={"ticketId": ticket_id},
            ConditionExpression=RECEIPT_CLAIM,
            UpdateExpression="SET receiptState = :claimed, receiptOwner = :me, receiptLeaseUntil = :lease",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":acked": "ACKNOWLEDGED",
                ":failed": "FAILED",
                ":claimed": "CLAIMED",
                ":now": now_epoch,
                ":me": owner,
                ":lease": now_epoch + LEASE_SECONDS,
            },
        )
        return True
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _receipt_release(ticket_id: str, owner: str, now_iso: str, sent: list[str], text: str = "") -> None:
    """Owner-conditioned outcome: ackReceiptAt on success (never sent again), FAILED otherwise."""
    if sent or text:
        text = text or f"{LABELS.get('ack_receipt_sent', 'acknowledgment receipt sent by')} {', '.join(sent)}"
        TABLE.update_item(
            Key={"ticketId": ticket_id},
            ConditionExpression="receiptOwner = :me",
            UpdateExpression=(
                "SET ackReceiptAt = :t, comments = list_append(if_not_exists(comments, :e), :cm) REMOVE receiptState, receiptLeaseUntil"
            ),
            ExpressionAttributeValues={":me": owner, ":t": now_iso, ":e": [], ":cm": [{"at": now_iso, "by": "connect-handler", "text": text}]},
        )
        return
    TABLE.update_item(
        Key={"ticketId": ticket_id},
        ConditionExpression="receiptOwner = :me",
        UpdateExpression="SET receiptState = :failed REMOVE receiptLeaseUntil",
        ExpressionAttributeValues={":me": owner, ":failed": "FAILED"},
    )


def _ack_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return "-"


def receipt(params: dict, ticket: dict, contact_id: str, owner: str, context=None) -> dict:
    """ONE written receipt for a voice acknowledgment, so a wrong "yes" is visible to the engineer.

    Same claim/lease/retry pattern as notify, on separate fields. A held lease or every channel
    failing reports an item failure (SQS retry, then the DLQ alarm); ackReceiptAt set means done.
    """
    ticket_id = ticket["ticketId"]
    if ticket.get("ackReceiptAt"):
        return {"result": "skipped", "sent": "none"}
    email_to = _email_to() if EMAIL_FROM else ""
    phone = _oncall_number() if SMS_ORIGINATION_IDENTITY else ""
    now_epoch = int(time.time())
    if not _receipt_claim(ticket_id, owner, now_epoch):
        item = TABLE.get_item(Key={"ticketId": ticket_id}, ConsistentRead=True).get("Item", {})
        held = item.get("receiptState") == "CLAIMED" and int(item.get("receiptLeaseUntil", 0)) >= now_epoch
        print({"action": "receipt", "ticketId": ticket_id, "contactId": contact_id, "result": "skipped", "held": held})
        return {"result": "skipped", "sent": "none", "held": held}
    if not (SMS_ORIGINATION_IDENTITY or EMAIL_FROM):
        # No channel configured: nothing to send, nothing to retry (same rule as notify).
        _receipt_release(ticket_id, owner, datetime.now(UTC).isoformat(), [], text="acknowledgment receipt: no channel configured")
        return {"result": "ok", "sent": "none"}
    if _remaining_ms(context) < MIN_BUDGET_MS:
        _receipt_release(ticket_id, owner, datetime.now(UTC).isoformat(), [])
        print({"action": "receipt", "ticketId": ticket_id, "result": "failed", "error": "no_time_budget"})
        return {"result": "failed", "sent": "none"}
    sent = _send({**params, "ackAt": _ack_time(ticket.get("acknowledgedAt", ""))}, phone, email_to, kind="ack_receipt")
    _receipt_release(ticket_id, owner, datetime.now(UTC).isoformat(), sent)
    print({"action": "receipt", "ticketId": ticket_id, "contactId": contact_id, "sent": sent})
    return {"result": "ok" if sent else "failed", "sent": "notified" if sent else "none"}


def unanswered(detail: dict, owner: str, context=None) -> dict:
    """Amazon Connect contact event DISCONNECTED on an API-initiated call.

    OPEN ticket: notify (not acknowledged). ACKNOWLEDGED ticket: one acknowledgment receipt. Only the
    call the caller placed for this ticket counts: the event's contactId must equal
    ticket.oncallContactId, so another API contact carrying a forged ticketId attribute is ignored.
    """
    contact_id = detail.get("contactId", "")
    instance_id = detail.get("instanceArn", "").rsplit("/", 1)[-1]
    if not contact_id or not instance_id:
        return {"result": "ignored"}
    # The incident context travels as contact attributes (set by the caller Lambda); other API calls
    # on the same instance have no ticketId and are ignored.
    attrs = CONNECT.get_contact_attributes(InstanceId=instance_id, InitialContactId=contact_id).get("Attributes", {})
    ticket_id = attrs.get("ticketId", "")
    if not is_ticket_id(ticket_id):
        return {"result": "ignored", "contactId": contact_id}
    ticket = TABLE.get_item(Key={"ticketId": ticket_id}, ConsistentRead=True).get("Item")
    if not ticket:
        return {"result": "ignored", "contactId": contact_id}
    bound = ticket.get("oncallContactId")
    if bound != contact_id:
        # A call claimed (callState) but not yet bound: the caller may still be writing the contact id,
        # or died after placing the call. Retry (then DLQ alarm) instead of dropping the notification.
        pending = not bound and bool(ticket.get("callState"))
        print({"action": "unanswered", "ticketId": ticket_id, "contactId": contact_id, "result": "unbound", "pending": pending})
        return {"result": "failed" if pending else "ignored", "contactId": contact_id}
    # Short context from the contact attributes (sanitized); the summary from the ticket item itself.
    params = {**_context(attrs), "summary": _summary_text(ticket.get("summary", ""))}
    if ticket.get("status") == "ACKNOWLEDGED":
        return receipt(params, ticket, contact_id, owner, context)
    phone = _oncall_number() if SMS_ORIGINATION_IDENTITY else ""
    return notify(params, contact_id, phone, owner, context, reason=_reason(detail))


def _event_records(event, context) -> dict:
    """SQS batch (one Connect contact event per record). Report item failures so SQS retries them."""
    failures = []
    for record in event["Records"]:
        message_id = record.get("messageId", "")
        try:
            detail = json.loads(record.get("body") or "{}").get("detail", {})
            result = unanswered(detail, context.aws_request_id, context)
            # Report an item failure so SQS makes the record visible again rather than deleting it:
            #  - held: another invocation holds an unexpired claim (it may be mid-send) -> retry later.
            #  - failed: every channel failed (notify or receipt), or a placed call is not yet bound.
            #    FAILED is reclaimable and the failure comment is
            #    deduped (notifyFailedAt), so a retry re-sends without stacking a second comment.
            # After maxReceiveCount (3) the record redrives to the DLQ and the DLQ-depth alarm fires,
            # instead of a no-answer notification being lost silently. "ok", "skipped" (SENT/not OPEN)
            # and "ignored" are terminal: nothing to retry, so the record is deleted.
            if result.get("held") or result.get("result") == "failed":
                failures.append({"itemIdentifier": message_id})
        except Exception as err:  # noqa: BLE001 - one bad record must not fail the batch
            print({"action": "unanswered", "messageId": message_id, "error": type(err).__name__})
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def handler(event, context):
    if isinstance(event, dict) and event.get("Records"):
        return _event_records(event, context)
    owner = getattr(context, "aws_request_id", "local")
    details = event.get("Details", {})
    params = details.get("Parameters", {})
    contact_data = details.get("ContactData", {})
    contact_id = contact_data.get("ContactId", "")
    action = params.get("action", "")
    if action == "init":
        return init(params)
    if action == "verify_pin":
        return verify_pin(params)
    if action == "ack":
        return ack(params, contact_id)
    if action == "notify":
        ctx = _context(params)
        phone, email_to, ctx["summary"] = _notify_reads(ctx["ticketId"])
        # Bound to the call the caller placed: another contact cannot drive this ticket's notification.
        out = notify(ctx, contact_id, phone, owner, context, email_to=email_to, bound_contact=contact_id or "-")
        # The flow only reads result/sent; `held` is internal to the event path.
        return {"result": out["result"], "sent": out["sent"]}
    raise ValueError(f"unknown action: {action!r}")
