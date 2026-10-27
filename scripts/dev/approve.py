# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Approve (or reject) an AWS DevOps Agent approval request, then resume the conversation.

Follows the documented flow (Working with directed actions > The approval flow in the API):
UpdateApprovalAction, then SendMessage with userActionResponse=APPROVAL_ACTION. The resume message
must carry approvalAction.buttonText, otherwise SendMessage returns VALIDATION_ERROR.
Usage: python approve.py <label> <executionId> <approval-request.json> [APPROVED|REJECTED] [--resume-only]
"""

import json
import sys

from chat import AS, c, send

label, execution_id, req_path = sys.argv[1], sys.argv[2], sys.argv[3]
decision = next((a for a in sys.argv[4:] if a in ("APPROVED", "REJECTED")), "APPROVED")
req = json.load(open(req_path, encoding="utf-8"))
data = req["data"]

if "--resume-only" not in sys.argv:
    extra = {} if decision == "APPROVED" else {"reason": "test rejection"}
    upd = c.update_approval_action(
        agentSpaceId=AS,
        approvalId=data["approval_id"],
        action=decision,
        finalPattern={"tool": data["tool"], "argumentPins": data["argument_pins"]},
        singleUse=True,
        **extra,
    )
    upd.pop("ResponseMetadata", None)
    print("UpdateApprovalAction ->", json.dumps(upd, default=str))

button = data["options"][0]["display_text"] if decision == "APPROVED" else "Reject"
send(
    label,
    execution_id,
    button,
    context={
        "userActionResponse": "APPROVAL_ACTION",
        "approvalAction": {
            "toolUseId": data["tool_use_id"],
            "interruptId": req["interrupt_id"],
            "approvalId": data["approval_id"],
            "action": decision,
            "buttonText": button,
        },
    },
)
