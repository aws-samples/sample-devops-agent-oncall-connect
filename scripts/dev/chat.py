# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Drive an AWS DevOps Agent chat through the API and archive the raw event stream.

Usage (AGENT_SPACE_ID, AWS_REGION and optionally AWS_PROFILE in the environment):
  python chat.py new <label> "<message>"                  -> new conversation
  python chat.py send <label> <executionId> "<message>"   -> next turn
Every stream event (text, tool blocks, approval requests) is written to
evidence/chat-<label>.jsonl and the final agent text is printed.
"""

import json
import os
import sys
from datetime import UTC, datetime

import boto3

AS = os.environ["AGENT_SPACE_ID"]
EV = os.path.join(os.path.dirname(__file__), "..", "..", "evidence")
SESSION = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=os.environ["AWS_REGION"])
c = SESSION.client("devops-agent")


def send(label: str, execution_id: str, message: str, context: dict | None = None) -> None:
    path = os.path.join(EV, f"chat-{label}.jsonl")
    kwargs = {"agentSpaceId": AS, "executionId": execution_id, "content": message}
    if context:
        kwargs["context"] = context
    resp = c.send_message(**kwargs)
    text_parts = []
    approvals = {}  # block index -> [block id, json parts] for action_approval blocks
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(UTC).isoformat(), "sent": message, "executionId": execution_id, "context": context}) + "\n")
        for ev in resp["events"]:
            f.write(json.dumps(ev, default=str) + "\n")
            start = ev.get("contentBlockStart", {})
            if start.get("type") == "action_approval":
                approvals[start["index"]] = [start["id"], []]
            if "contentBlockDelta" in ev:
                b = ev["contentBlockDelta"]
                d = b.get("delta", {})
                if "jsonDelta" in d:
                    pj = d["jsonDelta"].get("partialJson", "")
                    if b.get("index") in approvals:
                        approvals[b["index"]][1].append(pj)
                    if '"tool_call"' in pj or '"tool_result"' in pj or "approval" in pj.lower():
                        print(f"[block {b.get('index')} json] {pj[:900]}")
                td = d.get("textDelta")
                if isinstance(td, dict):
                    text_parts.append(td.get("text", ""))
            for k in ("contentBlockStart", "contentBlockStop", "responseFailed", "summary"):
                if k in ev and k != "contentBlockDelta":
                    b = ev[k]
                    if k == "contentBlockStop" and b.get("type") in (None, "text"):
                        continue
                    print(f"[{k}] {json.dumps(b, default=str)[:600]}")
    for block_id, parts in approvals.values():
        # approve.py input. The payload carries its own interrupt_id; the block id is only a fallback.
        req = json.loads("".join(parts))
        req.setdefault("interrupt_id", block_id)
        req_path = os.path.join(EV, f"approval-{label}.json")
        with open(req_path, "w", encoding="utf-8") as out:
            json.dump(req, out, indent=1)
        print(f"---- approval request: {os.path.abspath(req_path)}")
    print("---- agent text ----")
    print("".join(text_parts)[:4000])
    print(f"---- archive: {os.path.abspath(path)}")


if __name__ == "__main__":
    mode, label = sys.argv[1], sys.argv[2]
    if mode == "new":
        ex = c.create_chat(agentSpaceId=AS, userType="IAM")["executionId"]
        print("executionId:", ex)
        send(label, ex, sys.argv[3])
    else:
        send(label, sys.argv[3], sys.argv[4])
