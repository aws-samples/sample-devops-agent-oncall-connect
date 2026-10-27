# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Ticketing MCP server (stands in for an ITSM tool such as ServiceNow).

Hosted on Amazon Bedrock AgentCore Runtime (MCP protocol, Streamable HTTP, stateless) and
registered in AWS DevOps Agent with SigV4. Every write is logged (stdout -> CloudWatch) and
traced on the ticket (toolCalls attribute): proof that a tool actually ran, whatever the agent
says in the chat.

Tool classification in DevOps Agent (set by Terraform):
- list_open_tickets, get_ticket : READ_ONLY
- add_comment, close_ticket     : MUTATIVE (operator approval in chat, refused in autonomous runs)

Inputs come from the DevOps Agent (model-driven), so every tool validates its arguments before any
DynamoDB access: the ticket id must match the INC-<8 hex> format the opener issues, and free text is
length-capped (comments/resolutions). Invalid input returns a clear {"error": ...} the agent can read,
never an unhandled exception or an oversized DynamoDB item.

Untrusted text both ways: the comment and resolution the agent writes are passed
through the shared sanitizer (src/common/sanitize.py, packaged next to server.py by scripts/build.py)
before they are stored, and get_ticket sanitizes every string it returns (title, comments, the
voice note, older traces), so text written once by an alarm, a callee or a model is never handed
back raw to the DevOps Agent. Bounds: at most MAX_COMMENTS comments per ticket (add_comment is
refused beyond), toolCalls keeps only the length of each text argument, and list_open_tickets reads
at most MAX_SCAN_PAGES pages and returns at most MAX_LIST tickets (logged when truncated).

Audit: one structured line per tool call ({"audit": "ai_tool_call", tool, ticketId, result, lengths}),
never the free text.
"""

import json
import os
from datetime import UTC, datetime

import boto3
from boto3.dynamodb.conditions import Attr
from mcp.server.fastmcp import FastMCP

# The ONE shared untrusted-text sanitizer and ticket bounds (src/common/sanitize.py, packaged next to server.py).
from sanitize import MAX_COMMENTS, TICKET_ID_RE, sanitize_untrusted

TABLE = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION")).Table(os.environ["TICKETS_TABLE"])

# AgentCore Runtime reaches the container on all interfaces (port 8000, path /mcp).
mcp = FastMCP("ticketing", host="0.0.0.0", stateless_http=True)  # noqa: S104  # nosec B104

# Ticket attributes the tools never return: `summary` is the untrusted investigation summary the
# caller stores for the voice call; the DevOps Agent already has the investigation.
HIDDEN_FIELDS = frozenset(("summary",))
# Caps keep the DynamoDB item well under its 400 KB
# limit. A real ITSM API would enforce its own field limits; these are the sample's.
MAX_COMMENT = 2000
MAX_RESOLUTION = 2000
MAX_SCAN_PAGES = 5  # list_open_tickets: at most 5 Scan pages (1 MB each)
MAX_LIST = 100  # list_open_tickets: at most 100 tickets returned


def _audit(tool: str, ticket_id: str, result: str, **lengths: int) -> None:
    """One structured line per tool call: ids, outcome and lengths, never free text."""
    print(json.dumps({"audit": "ai_tool_call", "tool": tool, "ticketId": ticket_id, "result": result, **lengths}), flush=True)


def _clean(value):
    """Every string in a ticket, sanitized before it is handed back to the DevOps Agent."""
    if isinstance(value, str):
        return sanitize_untrusted(value, MAX_COMMENT)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _validate_ticket_id(ticket_id: str) -> dict | None:
    if not isinstance(ticket_id, str) or not TICKET_ID_RE.match(ticket_id):
        return {"error": "invalid ticket id: expected format INC-XXXXXXXX (8 uppercase hex characters)"}
    return None


def _validate_text(field: str, value: str, limit: int) -> dict | None:
    if not isinstance(value, str) or not value.strip():
        return {"error": f"{field} must be a non-empty string"}
    if len(value) > limit:
        return {"error": f"{field} too long: {len(value)} characters, maximum {limit}"}
    if not sanitize_untrusted(value, limit):
        return {"error": f"{field} has no readable text once delimiters and control characters are removed"}
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat()


# add_comment: the ticket exists, is not closed and is below its comment cap (same expression as the
# incident opener's repeated-ALARM comment).
COMMENTABLE = (
    "attribute_exists(ticketId) AND #s <> :closedStatus AND (attribute_not_exists(comments) OR size(comments) < :maxComments)",
    {":closedStatus": "CLOSED", ":maxComments": MAX_COMMENTS},
    f"not found, already closed, or at its limit of {MAX_COMMENTS} comments",
)
# close_ticket only moves a ticket from a known live state. Any other value (CLOSED, or a status this
# server does not know) is refused, so a close can never overwrite an unexpected state.
CLOSABLE = (
    "attribute_exists(ticketId) AND #s IN (:openStatus, :ackStatus)",
    {":openStatus": "OPEN", ":ackStatus": "ACKNOWLEDGED"},
    "not found or not in OPEN or ACKNOWLEDGED status",
)


def _write(tool: str, ticket_id: str, args: dict, update: str, values: dict, condition: tuple = COMMENTABLE) -> dict:
    """Apply a ticket change and its toolCalls trace in ONE conditional update.

    The trace can never say a tool ran when the change did not happen. Tickets that fail the
    condition (missing, closed, or for close_ticket not OPEN/ACKNOWLEDGED) are refused with an
    error the agent can read.
    """
    expression, condition_values, refusal = condition
    # Logs (stdout -> CloudWatch) and the toolCalls trace never carry the comment or resolution free
    # text, which can hold incident detail: only the length of each text argument. The (sanitized) text
    # itself is stored once, in comments / resolution.
    lengths = {f"{k}Length": len(str(v)) for k, v in args.items()}
    try:
        TABLE.update_item(
            Key={"ticketId": ticket_id},
            UpdateExpression=f"{update}, toolCalls = list_append(if_not_exists(toolCalls, :e), :trace)",
            ConditionExpression=expression,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                **values,
                ":e": [],
                ":trace": [{"at": _now(), "tool": tool, "argLengths": lengths}],
                **condition_values,
            },
        )
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        _audit(tool, ticket_id, "refused", **lengths)
        return {"error": f"ticket {ticket_id} {refusal}"}
    _audit(tool, ticket_id, "ok", **lengths)
    return {}


@mcp.tool()
def list_open_tickets() -> list[dict]:
    """List open incident tickets (id, title, service, priority, status, investigation task id). Read-only.

    At most MAX_LIST tickets (the first ones found): a real ITSM API would query by status server side.
    """
    keys = ("ticketId", "title", "service", "priority", "status", "investigationTaskId")
    # Server-side filter (not CLOSED, real tickets only: the incident opener's per-alarm lock items
    # "ALARM#..." share the table), paginated and bounded: never a silent one-page cut, never an
    # unbounded read. The same filter is re-applied below (defence in depth).
    kw = {"FilterExpression": Attr("status").ne("CLOSED") & Attr("ticketId").begins_with("INC-")}
    tickets, truncated = [], False
    for page in range(MAX_SCAN_PAGES):
        resp = TABLE.scan(**kw)
        tickets += [it for it in resp.get("Items", []) if TICKET_ID_RE.match(str(it.get("ticketId", ""))) and it.get("status") != "CLOSED"]
        last = resp.get("LastEvaluatedKey")
        if len(tickets) > MAX_LIST or (last and page == MAX_SCAN_PAGES - 1):
            truncated = True
            break
        if not last:
            break
        kw["ExclusiveStartKey"] = last
    _audit("list_open_tickets", "-", "truncated" if truncated else "ok", count=min(len(tickets), MAX_LIST))
    return [_clean({k: it.get(k) for k in keys}) for it in tickets[:MAX_LIST]]


@mcp.tool()
def get_ticket(ticket_id: str) -> dict:
    """Get the content of an incident ticket, including comments (not the investigation summary). Read-only."""
    if err := _validate_ticket_id(ticket_id):
        _audit("get_ticket", "-", "invalid")
        return err
    item = TABLE.get_item(Key={"ticketId": ticket_id}).get("Item")
    if not item:
        _audit("get_ticket", ticket_id, "not-found")
        return {"error": f"ticket {ticket_id} not found"}
    _audit("get_ticket", ticket_id, "ok", comments=len(item.get("comments", [])))
    # The investigation summary the caller stored for the voice call is untrusted LLM output: never
    # hand it back to the DevOps Agent, which would re-read injectable text.
    item = {k: v for k, v in item.items() if k not in HIDDEN_FIELDS}
    # Every string (title, comments incl. the alarm reason and the voice note, older traces) sanitized.
    return _clean(json.loads(json.dumps(item, default=str)))


@mcp.tool()
def add_comment(ticket_id: str, text: str) -> dict:
    """Add a work note to an open incident ticket. Modifies the ticket."""
    if err := (_validate_ticket_id(ticket_id) or _validate_text("comment text", text, MAX_COMMENT)):
        _audit("add_comment", ticket_id if TICKET_ID_RE.match(str(ticket_id)) else "-", "invalid")
        return err
    text = sanitize_untrusted(text, MAX_COMMENT)
    error = _write(
        "add_comment",
        ticket_id,
        {"text": text},
        "SET comments = list_append(if_not_exists(comments, :e), :c)",
        {":c": [{"at": _now(), "by": "devops-agent", "text": text}]},
    )
    return error or {"ticketId": ticket_id, "result": "comment added"}


@mcp.tool()
def close_ticket(ticket_id: str, resolution: str) -> dict:
    """Close an incident ticket in OPEN or ACKNOWLEDGED status with a resolution note. Modifies the ticket."""
    if err := (_validate_ticket_id(ticket_id) or _validate_text("resolution", resolution, MAX_RESOLUTION)):
        _audit("close_ticket", ticket_id if TICKET_ID_RE.match(str(ticket_id)) else "-", "invalid")
        return err
    resolution = sanitize_untrusted(resolution, MAX_RESOLUTION)
    error = _write(
        "close_ticket",
        ticket_id,
        {"resolution": resolution},
        "SET #s = :closed, resolution = :r, closedAt = :t",
        {":closed": "CLOSED", ":r": resolution, ":t": _now()},
        condition=CLOSABLE,
    )
    return error or {"ticketId": ticket_id, "result": "ticket closed"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
