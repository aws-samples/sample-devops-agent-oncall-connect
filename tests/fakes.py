# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""In-memory stand-ins for the DynamoDB table, with just enough condition handling for the tests."""

from types import SimpleNamespace


class ConditionalCheckFailed(Exception):
    pass


def _claim_ok(item, v):
    # #s = OPEN AND (no notifyState OR notifyState = FAILED OR (CLAIMED AND leaseUntil < now))
    if item is None or item.get("status") != v[":open"]:
        return False
    state = item.get("notifyState")
    if state is None or state == v[":failed"]:
        return True
    return state == v[":claimed"] and item.get("leaseUntil", 0) < v[":now"]


def _receipt_claim_ok(item, v):
    # #s = ACKNOWLEDGED AND no ackReceiptAt AND (no receiptState OR FAILED OR expired CLAIMED lease)
    if item is None or item.get("status") != v[":acked"] or "ackReceiptAt" in item:
        return False
    state = item.get("receiptState")
    if state is None or state == v[":failed"]:
        return True
    return state == v[":claimed"] and item.get("receiptLeaseUntil", 0) < v[":now"]


def _lock_ok(item, v):
    # attribute_not_exists(ticketId) OR openTicketId = :tid
    return item is None or item.get("openTicketId") == v[":tid"]


def _split_top(text, sep=","):
    """Split on `sep` outside parentheses (list_append(a, b) stays one clause)."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


class FakeTable:
    """Stores items by ticketId. `CONDITIONS` maps a ConditionExpression to a predicate on the item."""

    CONDITIONS = {
        "attribute_not_exists(ticketId)": lambda item, v: item is None,
        "attribute_exists(ticketId)": lambda item, v: item is not None,
        "#s = :open": lambda item, v: item is not None and item.get("status") == v[":open"],
        # Acknowledgment bound to the placed call (connect_handler ack).
        "#s = :open AND oncallContactId = :c": lambda item, v: (
            item is not None and item.get("status") == v[":open"] and item.get("oncallContactId") == v[":c"]
        ),
        "attribute_exists(ticketId) AND #s <> :closedStatus": lambda item, v: item is not None and item.get("status") != v[":closedStatus"],
        # Comment cap (incident_opener repeated ALARM, MCP add_comment).
        ("attribute_exists(ticketId) AND #s <> :closedStatus AND (attribute_not_exists(comments) OR size(comments) < :maxComments)"): (
            lambda item, v: (item is not None and item.get("status") != v[":closedStatus"] and len(item.get("comments", [])) < v[":maxComments"])
        ),
        # Notification state machine (connect_handler).
        (
            "#s = :open AND (attribute_not_exists(notifyState) OR notifyState = :failed OR (notifyState = :claimed AND leaseUntil < :now))"
        ): _claim_ok,
        # Flow-path claim, also bound to the placed call.
        (
            "#s = :open AND (attribute_not_exists(notifyState) OR notifyState = :failed OR (notifyState = :claimed AND leaseUntil < :now))"
            " AND oncallContactId = :c"
        ): lambda item, v: _claim_ok(item, v) and item.get("oncallContactId") == v[":c"],
        "notifyOwner = :me": lambda item, v: item is not None and item.get("notifyOwner") == v[":me"],
        "notifyOwner = :me AND attribute_not_exists(notifyFailedAt)": lambda item, v: (
            item is not None and item.get("notifyOwner") == v[":me"] and "notifyFailedAt" not in item
        ),
        # Acknowledgment receipt (connect_handler).
        (
            "#s = :acked AND attribute_not_exists(ackReceiptAt) AND (attribute_not_exists(receiptState)"
            " OR receiptState = :failed OR (receiptState = :claimed AND receiptLeaseUntil < :now))"
        ): _receipt_claim_ok,
        "receiptOwner = :me": lambda item, v: item is not None and item.get("receiptOwner") == v[":me"],
        # One call per incident (oncall_caller).
        "#s = :open AND attribute_not_exists(callState)": lambda item, v: (
            item is not None and item.get("status") == v[":open"] and "callState" not in item
        ),
        "callState = :calling": lambda item, v: item is not None and item.get("callState") == v[":calling"],
        # Per-alarm lock, held until its ticket is CLOSED (incident_opener).
        "attribute_not_exists(ticketId) OR openTicketId = :tid": _lock_ok,
        "openTicketId = :old": lambda item, v: item is not None and item.get("openTicketId") == v[":old"],
    }

    def __init__(self, items=None):
        self.items = {i["ticketId"]: dict(i) for i in (items or [])}
        self.updates = []
        self.meta = SimpleNamespace(client=SimpleNamespace(exceptions=SimpleNamespace(ConditionalCheckFailedException=ConditionalCheckFailed)))

    def _check(self, kw, item):
        cond = kw.get("ConditionExpression")
        if cond and not self.CONDITIONS[cond](item, kw.get("ExpressionAttributeValues", {})):
            raise ConditionalCheckFailed(cond)

    def put_item(self, Item, **kw):
        self._check(kw, self.items.get(Item["ticketId"]))
        self.items[Item["ticketId"]] = dict(Item)

    def get_item(self, Key, **kw):
        item = self.items.get(Key["ticketId"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, **kw):
        self._check(kw, self.items.get(Key["ticketId"]))
        self.updates.append({"Key": Key, **kw})
        item = self.items.setdefault(Key["ticketId"], {"ticketId": Key["ticketId"]})
        expr = kw.get("UpdateExpression", "")
        values = kw.get("ExpressionAttributeValues", {})
        names = kw.get("ExpressionAttributeNames", {})
        # Enough of the SET/REMOVE grammar: `a = :v`, `#n = :v`, `a = list_append(if_not_exists(a, :e), :v)`.
        set_part, _, remove_part = expr.partition(" REMOVE ")
        if expr.startswith("REMOVE "):
            set_part, remove_part = "", expr[len("REMOVE ") :]
        set_part = set_part.removeprefix("SET ").strip()
        for clause in _split_top(set_part):
            lhs, _, rhs = (s.strip() for s in clause.partition("="))
            attr = names.get(lhs, lhs)
            if rhs.startswith("list_append("):
                item.setdefault(attr, []).extend(values[_split_top(rhs[len("list_append(") : -1])[-1]])
            else:
                item[attr] = values[rhs]
        for attr in _split_top(remove_part):
            item.pop(names.get(attr, attr), None)

    def query(self, **kw):
        return {"Items": [dict(i) for i in self.items.values()]}

    def scan(self, **kw):
        # One page, unfiltered (the code under test re-applies its filter); tests that need several
        # pages set `scan_pages`, a list of (items, last_evaluated_key) returned in order.
        self.scans = getattr(self, "scans", []) + [kw]
        pages = getattr(self, "scan_pages", None)
        if pages:
            items, last = pages[len(self.scans) - 1]
            return {"Items": [dict(i) for i in items], **({"LastEvaluatedKey": last} if last else {})}
        return {"Items": [dict(i) for i in self.items.values()]}
