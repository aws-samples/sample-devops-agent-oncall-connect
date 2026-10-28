# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate docs/architecture.drawio, with the trust boundaries of docs/threat-model.md.

Export the PNG with:
  /Applications/draw.io.app/Contents/MacOS/draw.io -x -f png -s 1.5 -o docs/architecture.png docs/architecture.drawio
or, without draw.io desktop:
  docker run --rm -v "$PWD/docs":/data rlespinasse/drawio-export --format png --scale 1.5 architecture.drawio
"""

import html
import pathlib

OUT = pathlib.Path(__file__).resolve().parents[2] / "docs" / "architecture.drawio"
cells, n = [], [100]
DX = 160  # the AWS Cloud starts after the external operator zone
TB_RED = "#D13212"


def nid():
    n[0] += 1
    return f"c{n[0]}"


def esc(label):
    return html.escape(label.replace(chr(10), "<br>"))


def vertex(label, style, x, y, w, h):
    i = nid()
    cells.append(
        f'<mxCell id="{i}" value="{esc(label)}" style="{style}" vertex="1" parent="1">'
        f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
    )
    return i


def icon(label, res, color, x, y, side="bottom", resource=False):
    """AWS service icon, or resource icon when resource=True (draw.io shape mxgraph.aws4.<res>).
    side='top', 'left' or 'right' keeps the label off the arrows."""
    if side == "bottom":
        pos = "verticalLabelPosition=bottom;verticalAlign=top;align=center;"
    elif side == "top":
        pos = "verticalLabelPosition=top;verticalAlign=bottom;align=center;"
    elif side == "left":
        pos = "labelPosition=left;verticalLabelPosition=middle;align=right;verticalAlign=middle;spacingRight=4;"
    else:
        pos = "labelPosition=right;verticalLabelPosition=middle;align=left;verticalAlign=middle;spacingLeft=4;"
    shape = f"shape=mxgraph.aws4.{res};pointerEvents=1;strokeColor=none;" if resource else (
        f"shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{res};strokeColor=#ffffff;"
    )
    style = (
        f"sketch=0;outlineConnect=0;fontColor=#232F3E;fillColor={color};dashed=0;"
        f"{pos}html=1;fontSize=11;fontStyle=0;aspect=fixed;{shape}"
    )
    return vertex(label, style, x, y, 48, 48)


def box(label, x, y, w=120, h=48, fill="#FFFFFF", stroke="#545B64"):
    style = f"rounded=1;whiteSpace=wrap;html=1;fontSize=11;fillColor={fill};strokeColor={stroke};fontColor=#232F3E;"
    return vertex(label, style, x, y, w, h)


def group(label, x, y, w, h, kind):
    base = (
        "points=[];outlineConnect=0;gradientColor=none;html=1;whiteSpace=wrap;fontSize=12;fontStyle=0;"
        "container=0;pointerEvents=0;collapsible=0;recursiveResize=0;verticalAlign=top;align=left;"
    )
    if kind == "cloud":
        style = base + (
            "shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_aws_cloud_alt;strokeColor=#232F3E;"
            "fillColor=none;spacingLeft=30;fontColor=#232F3E;dashed=0;"
        )
    elif kind == "region":
        style = base + (
            "shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_region;strokeColor=#00A4A6;fillColor=none;"
            "spacingLeft=30;fontColor=#147EBA;dashed=1;"
        )
    else:  # trust boundary
        style = base + (
            f"rounded=0;strokeColor={TB_RED};strokeWidth=2;dashed=1;dashPattern=8 4;fillColor=none;"
            f"fontColor={TB_RED};fontStyle=1;spacingLeft=6;spacingTop=2;"
        )
    return vertex(label, style, x, y, w, h)


def badge(label, x, y):
    style = f"rounded=1;arcSize=40;whiteSpace=wrap;html=1;fontSize=10;fontStyle=1;fillColor={TB_RED};strokeColor=none;fontColor=#FFFFFF;"
    return vertex(label, style, x, y, 34, 18)


def edge(a, b, label="", dashed=False, color="#545B64", extra=""):
    i = nid()
    style = (
        f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=block;endFill=1;fontSize=10;"
        f"strokeColor={color};fontColor=#232F3E;labelBackgroundColor=#FFFFFF;" + ("dashed=1;" if dashed else "") + extra
    )
    cells.append(
        f'<mxCell id="{i}" value="{esc(label)}" style="{style}" edge="1" parent="1" source="{a}" target="{b}">'
        f'<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


def text(label, x, y, w, h, size=11, bold=False, color="#232F3E"):
    style = f"text;html=1;align=left;verticalAlign=top;whiteSpace=wrap;fontSize={size};fontColor={color};" + ("fontStyle=1;" if bold else "")
    return vertex(label, style, x, y, w, h)


ORANGE, PINK, PURPLE, RED, GREEN, BLUE = "#ED7100", "#E7157B", "#8C4FFF", "#DD344C", "#7AA116", "#C925D1"

text("AWS DevOps Agent on-call calls with Amazon Connect", 20, 10, 700, 24, 16, True)

# External actors, outside the AWS Cloud
group("Operator\n(signed in to the\nAWS console)", 20, 250, 130, 170, "boundary")
group("TB2: public phone network\n(untrusted)", 1560, 560, 200, 170, "boundary")

group("AWS Cloud (your account)", DX + 20, 50, 1360, 700, "cloud")
group("Region A: AWS DevOps Agent Region", DX + 40, 90, 840, 640, "region")
group("Region B: Amazon Connect Region", DX + 900, 90, 460, 640, "region")
group("TB1", DX + 440, 262, 260, 140, "boundary")

# Region A, row 1: workload -> alarm -> rule -> opener -> tickets
workload = icon("meter-api Lambda\n(demo workload)", "lambda", ORANGE, DX + 80, 150)
alarm = icon("CloudWatch alarm", "cloudwatch_2", PINK, DX + 230, 150)
eb1 = icon("EventBridge rule\n(alarm state)", "eventbridge", PINK, DX + 380, 150)
opener = icon("incident-opener\nLambda", "lambda", ORANGE, DX + 541, 150, side="top")
tickets = icon("DynamoDB tickets\n(stands in for ITSM,\nKMS encrypted)", "dynamodb", BLUE, DX + 751, 150, side="top")
# Row 2: operator -> DevOps Agent -> MCP server
operator = icon("", "user", "#232F3E", 61, 320)
agent = box("AWS DevOps Agent\n(Agent Space)", DX + 490, 305, 150, 60, "#E6F2F8", "#147EBA")
mcp = box("Ticketing MCP server\nAgentCore Runtime (SigV4)", DX + 700, 305, 150, 60, "#F2EAFE", PURPLE)
# Rows 3-4: investigation events -> caller
eb2 = icon("EventBridge rule\n(investigation end,\nHIGH / CRITICAL)", "eventbridge", PINK, DX + 541, 450, side="right")
caller = icon("caller Lambda\n(reads the summary)", "lambda", ORANGE, DX + 541, 620)
ssm = icon("Parameter Store SecureString\n(on-call number, also\nthe SMS destination)", "parameter_store", PINK, DX + 380, 620, resource=True)
dlq = icon("SQS on-failure\nqueue + DLQ", "sqs", PINK, DX + 380, 470)
# Optional replay path (enable_replay): its own bus, PutEvents denied to all but the deployer.
replay = icon("EventBridge replay bus\n(optional, deployer-only\nPutEvents)", "eventbridge", PINK, DX + 760, 560, side="top")
# Resource policies: only the DevOps Agent SigV4 role (runtime) / the sample's roles (table) and the deployer.
text("Resource policies on the runtime and its\nDEFAULT endpoint: DevOps Agent role\n+ deployer only", DX + 700, 368, 180, 44, 9)
text("Resource policy: ticket writes only\nfrom the sample's roles + deployer", DX + 600, 205, 145, 30, 9)

edge(workload, alarm, "errors")
edge(alarm, eb1)
edge(eb1, opener)
edge(opener, tickets, "open ticket")
edge(opener, agent, "CreateBacklogTask")
edge(operator, agent, "chat, approvals")
edge(agent, mcp, "tools")
edge(mcp, tickets, "read / comment / close")
edge(agent, eb2, "aws.aidevops events")
edge(eb2, caller)
edge(caller, ssm, "number", dashed=True, extra="exitX=0;exitY=0.75;exitDx=0;exitDy=0;entryX=1;entryY=0.75;entryDx=0;entryDy=0;")
edge(caller, dlq, "on failure", dashed=True, extra="exitX=0;exitY=0.25;exitDx=0;exitDy=0;entryX=1;entryY=0.5;entryDx=0;entryDy=0;")
edge(replay, caller, "replay", dashed=True, extra="exitX=0;exitY=0.5;exitDx=0;exitDy=0;entryX=1;entryY=0.25;entryDx=0;entryDy=0;")

# Region B
handler = icon("connect-handler Lambda\n(init, PIN, ack, receipt, notify)", "lambda", ORANGE, DX + 1090, 150, side="top")
flow = box("Contact flow: Lex bot +\nConnect AI agent (voice),\npress 1 to confirm", DX + 1030, 400, 170, 56, "#FDEBEC", RED)
bedrock = icon("Amazon Bedrock", "bedrock", GREEN, DX + 1300, 340)
polly = icon("Amazon Polly\n(Lex voice)", "polly", GREEN, DX + 1300, 460)
sms = icon("End User Messaging\nSMS (optional)", "pinpoint", RED, DX + 1220, 150)
ses = icon("Amazon SES\n(optional, TLS)", "simple_email_service", RED, DX + 1300, 230)
ssm_b = icon("Parameter Store SecureString\n(email, PIN; optional)", "parameter_store", PINK, DX + 940, 215, resource=True)
eb3 = icon("EventBridge rule\n(contact DISCONNECTED)", "eventbridge", PINK, DX + 940, 500)
unanswered = icon("SQS unanswered queue\n(delay) + DLQ", "sqs", PINK, DX + 940, 335, side="top")
connect = icon("Amazon Connect\n(outbound call, recording)", "connect", RED, DX + 1081, 620)
phone = icon("On-call engineer", "mobile_client", "#232F3E", 1636, 620)

edge(caller, connect, "StartOutboundVoiceContact")
edge(connect, phone, "voice")
edge(connect, flow)
edge(flow, bedrock, "", dashed=True, extra="entryX=0;entryY=0.5;entryDx=0;entryDy=0;")
edge(flow, polly, "", dashed=True, extra="entryX=0;entryY=0.5;entryDx=0;entryDy=0;")
edge(connect, eb3, "contact events", extra="exitX=0.5;exitY=0;exitDx=0;exitDy=0;entryX=1;entryY=0.5;entryDx=0;entryDy=0;")
edge(eb3, unanswered)
edge(unanswered, handler, "no answer")
edge(handler, ssm_b, "", dashed=True)
edge(handler, sms, "notify", dashed=True)
edge(handler, ses, "", dashed=True)
edge(sms, phone, "SMS", dashed=True)
edge(ses, phone, "email", dashed=True)
edge(flow, handler, "init + ack")
edge(handler, tickets, "ACKNOWLEDGED")

# TB3: untrusted text read by models (logs and tickets, summary and speech)
badge("TB3", DX + 446, 376)
badge("TB3", DX + 1200, 392)
# TB1 also sits on the events and SigV4 calls that leave the managed service
badge("TB1", DX + 655, 376)

text(
    "Trust boundaries (see docs/threat-model.md)\n"
    "TB1: AWS DevOps Agent (managed service) to your account (aws.aidevops events, SigV4 calls to the MCP server, operator chat)\n"
    "TB2: your account to the public phone network (outbound call, voice input from the callee)\n"
    "TB3: untrusted text read by models (log lines and ticket fields read by DevOps Agent; "
    "investigation summary and callee speech read by the Connect AI agent)",
    DX + 20,
    760,
    1360,
    70,
    11,
    color=TB_RED,
)

xml = (
    '<mxfile host="generator"><diagram name="architecture" id="arch"><mxGraphModel dx="1800" dy="860" grid="1" gridSize="10" '
    'guides="1" page="1" pageWidth="1780" pageHeight="840" math="0" shadow="0"><root><mxCell id="0"/>'
    '<mxCell id="1" parent="0"/>' + "".join(cells) + "</root></mxGraphModel></diagram></mxfile>\n"
)
OUT.parent.mkdir(exist_ok=True)
OUT.write_text(xml, encoding="utf-8")
print(OUT)
