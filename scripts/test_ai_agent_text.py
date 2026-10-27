# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""End-to-end test of the AI agent call without a phone.

Same chain as a call except the voice: a real AI agent session, context injected by the deployed
connect handler (action=init, same parameters the flow sends), then conversation turns through
Lex RecognizeText with the same session attributes as the flow (ai-agent-arn + session-arn).
Passes if the agent calls the Acknowledge tool when the engineer takes the incident.

Uses the last "Investigation Completed" event of the archive, its ticket and its real summary.
Usage: python3 scripts/test_ai_agent_text.py [--profile NAME]
Output: build/ai_agent_text_transcript.json
"""

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from replay import ROOT, aws, last_completed_event, tf_outputs  # noqa: E402

# What the on-call engineer says, per deployed language (Terraform output "language").
TURNS = {
    "en-US": [
        "Yes, go ahead.",
        "What is the cause exactly, and since when?",
        "Can I reassign the ticket to another engineer?",
        "OK, I'll take the incident, I'm checking the Lambda configuration.",
    ],
    "fr-FR": [
        "Oui, je vous \u00e9coute.",
        "C'est quoi la cause exactement, et depuis quand ?",
        "Je peux réorienter le ticket vers un autre ingénieur ?",
        "D'accord, je prends l'incident, je regarde la configuration de la Lambda.",
    ],
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default=None)
    args = p.parse_args()
    out = tf_outputs()
    oncall = out["oncall"]
    region = out["foundation"]["agent_space_arn"].split(":")[3]
    connect_region = oncall["lex_alias_arn"].split(":")[3]
    language = out["language"]
    locale = json.loads((ROOT / "src" / "locales" / f"{language}.json").read_text())
    labels = locale["labels"]

    event = last_completed_event(args.profile, region, out["foundation"]["events_log_group"])
    meta, data = event["detail"]["metadata"], event["detail"]["data"]
    tickets = aws(
        args.profile,
        region,
        "dynamodb",
        "query",
        "--table-name",
        out["foundation"]["tickets_table_name"],
        "--index-name",
        "byInvestigation",
        "--key-condition-expression",
        "investigationTaskId = :t",
        "--expression-attribute-values",
        json.dumps({":t": {"S": meta["task_id"]}}),
    )["Items"]
    ticket_id = tickets[0]["ticketId"]["S"] if tickets else sys.exit("no ticket for the last investigation")
    records = aws(
        args.profile,
        region,
        "devops-agent",
        "list-journal-records",
        "--agent-space-id",
        meta["agent_space_id"],
        "--execution-id",
        meta["execution_id"],
        "--record-type",
        "investigation_summary_md",
    )["records"]
    summary = next((r["content"] for r in records if r.get("recordId") == data.get("summary_record_id")), "")

    session_arn = aws(
        args.profile,
        connect_region,
        "qconnect",
        "create-session",
        "--assistant-id",
        oncall["assistant_id"],
        "--name",
        f"text-test-{int(time.time())}",
    )["session"]["sessionArn"]
    init = {
        "Details": {
            "ContactData": {"ContactId": "text-test"},
            "Parameters": {
                "action": "init",
                "sessionArn": session_arn,
                "ticketId": ticket_id,
                "service": "meter-api",
                "priority": labels["priority"]["HIGH"],
                "statusText": labels["status"]["COMPLETED"],
                "summary": summary[:3500],
            },
        }
    }
    payload = ROOT / "build" / "ai_agent_init_event.json"
    payload.write_text(json.dumps(init))
    aws(
        args.profile,
        connect_region,
        "lambda",
        "invoke",
        "--function-name",
        oncall["handler_function_name"],
        "--cli-binary-format",
        "raw-in-base64-out",
        "--payload",
        f"file://{payload}",
        str(ROOT / "build" / "ai_agent_init_out.json"),
    )
    print("init:", (ROOT / "build" / "ai_agent_init_out.json").read_text(), "| summary chars:", len(summary))

    bot_id, alias_id = oncall["lex_alias_arn"].split("/")[-2:]
    attrs = {"x-amz-lex:q-in-connect:ai-agent-arn": oncall["ai_agent_arn"], "x-amz-lex:q-in-connect:session-arn": session_arn}
    lex_session, transcript, tool = f"text-{int(time.time())}", [], None
    for turn in TURNS[language]:
        resp = aws(
            args.profile,
            connect_region,
            "lexv2-runtime",
            "recognize-text",
            "--bot-id",
            bot_id,
            "--bot-alias-id",
            alias_id,
            "--locale-id",
            locale["lex_locale"],
            "--session-id",
            lex_session,
            "--text",
            turn,
            "--session-state",
            json.dumps({"sessionAttributes": attrs}),
        )
        said = " ".join(m.get("content", "").strip() for m in resp.get("messages", []))
        sess = resp.get("sessionState", {}).get("sessionAttributes", {})
        tool = sess.get("Tool")
        transcript.append({"oncall": turn, "agent": said, "Tool": tool, "note": sess.get("note")})
        print(f"\n> {turn}\n< {said}\n  Tool={tool} note={sess.get('note')}")
        if tool:
            break

    (ROOT / "build" / "ai_agent_text_transcript.json").write_text(json.dumps(transcript, indent=2, ensure_ascii=False))
    # Only the last turn takes the incident: an Acknowledge on an earlier turn is a false acknowledgment.
    ok = tool == "Acknowledge" and len(transcript) == len(TURNS[language])
    verdict = "PASS (Acknowledge on the ownership turn)" if ok else f"FAIL (tool = {tool} on turn {len(transcript)})"
    print("\nRESULT:", verdict)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
