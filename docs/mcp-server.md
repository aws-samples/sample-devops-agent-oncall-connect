# Ticketing MCP server

The ticketing MCP server lets the AWS DevOps Agent operator read and update the incident ticket
from the chat, with a human approval on every write. In this sample it stands in for an ITSM tool
such as ServiceNow: the tickets live in the DynamoDB table that the alarm writes to and that
Amazon Connect updates on acknowledgment, so one ticket follows the whole incident.

```
DevOps Agent chat ──SigV4──> AgentCore Runtime (src/mcp_server/server.py) ──> DynamoDB tickets table
```

## Tools

| Tool | What it does | Classification in DevOps Agent |
|:--|:--|:--|
| `list_open_tickets` | lists tickets that are not closed | `READ_ONLY`: runs without approval |
| `get_ticket` | returns a ticket with its comments (never the stored investigation summary) | `READ_ONLY` |
| `add_comment` | adds a work note | `MUTATIVE`: approval in the chat |
| `close_ticket` | closes the ticket with a resolution | `MUTATIVE` |

Every tool call writes one audit line to stdout (CloudWatch Logs):
`{"audit": "ai_tool_call", "tool": ..., "ticketId": ..., "result": ..., <field>Length: ...}`,
ids, outcome and lengths only, never the free text. Every write is also appended to the ticket
(`toolCalls` attribute, with the time, the tool and the length of each text argument). That trace
shows whether a tool really ran, whatever the agent says in the chat. The change and its trace go
in one conditional `UpdateItem`, so the trace can never record a write that did not happen.
Writes on a missing or closed ticket, or on a ticket at its limit of 50 comments, return an
`error` the agent can read. `close_ticket` only closes a ticket in `OPEN` or `ACKNOWLEDGED`
status; this keeps the ticket consistent, it is not an access control (see below).

Inputs are validated before any DynamoDB access: the ticket id must match `INC-<8 hex>`, and a
comment or resolution is at most 2000 characters. Every string the server stores or returns goes
through the shared sanitizer (`src/common/sanitize.py`, packaged next to `server.py`), so text
written once by an alarm, a callee or a model is never handed back raw to the DevOps Agent.

`list_open_tickets` filters on the server side, reads at most 5 scan pages and returns at most
100 tickets (a truncated list is logged). Against a real ITSM tool, query by status with
pagination.

## What the classification does

Observed on this sample's deployment (not only read in the documentation):

| Situation | Behavior |
|:--|:--|
| `READ_ONLY` tool, chat or autonomous investigation | runs |
| `MUTATIVE` tool in the chat, directed actions disabled on the Agent Space | refused |
| `MUTATIVE` tool in the chat, directed actions enabled | the agent asks for approval (summary, risk, blast radius, rollback), runs once approved |
| `MUTATIVE` tool in an autonomous investigation, directed actions enabled | refused ("An approval-ref header is required"), reported as an investigation gap |
| Tool with no classification | treated as `READ_ONLY`: **a write tool runs without approval** |
| Classification that covers only some of the enabled tools | rejected by the API |

Consequences:
- The classification, and so the approval, is a property of the DevOps Agent association, not
  something the MCP server checks. Another client that can invoke the server would not be asked
  for an approval: that is what the runtime resource policies prevent (see below).
- Classify every tool that changes something as `MUTATIVE`. Leaving the classification empty is
  the dangerous default, which is why the module never enables a write tool without its
  classification (see Terraform below).
- An investigation cannot update the ticket by itself through MCP. To write the outcome to the
  ticket automatically, react to the `aws.aidevops` EventBridge events, as the on-call chain does.

## Why AgentCore Runtime

AWS DevOps Agent only calls remote MCP servers over Streamable HTTP, authenticated with OAuth 2.0,
an API key, custom headers or SigV4. Servers in a private network are reached through a DevOps
Agent private connection.

[Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp.html)
hosts the server with `server_protocol = "MCP"` and gives:
- **SigV4 natively**: DevOps Agent assumes `<prefix>-devopsagent-mcp-sigv4`, a role that trusts
  `aidevops.amazonaws.com` with `aws:SourceAccount` and `aws:SourceArn` (`service/*` of the
  account and Region) and can only call `bedrock-agentcore:InvokeAgentRuntime` on this runtime.
  No API key to store or rotate. `service/*` is the trust AWS documents for a SigV4 MCP
  registration, because the role must exist before the registration: any DevOps Agent
  registration of the account that is given this role passes it, which is why a pilot needs a
  dedicated account. `narrow_devops_trust` (default `false`) can rewrite it to this registration
  (`service/<id>`) once you have confirmed that id is stable in `aws:SourceArn`.
  Resource policies on the runtime and on its DEFAULT endpoint deny every invocation action to
  every principal except that role and the deployer principals (`deployer_principal_arns`), so
  the approval step in DevOps Agent cannot be skipped by invoking the runtime directly. The ticket
  table has a resource policy too: it denies writes to every principal except the sample's roles
  (opener, caller, Connect handler, MCP runtime) and the deployer principals.
- **No server to run**: code deployment from an S3 zip (`PYTHON_3_13`, arm64), stateless sessions,
  per-session isolation.
- **Small code**: the server is about 220 lines of [FastMCP](https://github.com/modelcontextprotocol/python-sdk),
  most of it input validation and comments.

This is pattern 1 of the AWS Prescriptive Guidance
[MCP deployment patterns](https://docs.aws.amazon.com/prescriptive-guidance/latest/mcp-deployment-patterns-on-aws/deployment-pattern-1-amazon-bedrock-agent-core.html);
see also [sample-mcp-deployment-patterns](https://github.com/aws-samples/sample-mcp-deployment-patterns/tree/main/deploy-agentcore-iam).

`network_mode = "PUBLIC"` means the endpoint is the public AgentCore endpoint, still protected by
SigV4. If the server must reach an ITSM that is only on a private network, run the runtime in VPC
mode so its outbound calls go through your VPC.

### Other hosting options (not tested here)

- **The MCP server of your ITSM vendor**, if it has one. Check that it supports Streamable HTTP and
  one of the authentication methods above, and that you can classify its tools.
- **AgentCore Gateway** with AWS Lambda targets: each Lambda function becomes an MCP tool.
- **A container** (Amazon ECS on AWS Fargate) behind a load balancer with OAuth 2.0, or private
  behind a DevOps Agent private connection.

## Connect it to a real ITSM tool

Keep the runtime, the SigV4 role and the classification. Replace the DynamoDB calls in the four
functions of `src/mcp_server/server.py` with calls to your ITSM API:
- Store the ITSM credentials in AWS Secrets Manager and grant the runtime role
  `secretsmanager:GetSecretValue` on that secret only. Do not put them in environment variables.
- Use an ITSM account restricted to the incident table and to the fields these tools change.
- Keep the docstrings precise: the agent picks tools from them.
- Keep the write trace (`toolCalls` or an ITSM work note): it is your audit trail.

## Terraform

Module `terraform/modules/mcp-ticketing`. The tool list and classification come from the
`tool_classification` map in `terraform/envs/demo/main.tf`.

The `awscc_devopsagent_association` resource does not expose `toolDetails`. The module is
fail-closed: Terraform creates the association with the `READ_ONLY` tools only, then
`classify_tools.py` (AWS CLI `update-association`) adds the `MUTATIVE` tools together with their
classification in one call, and reads the association back to check it. If the script fails or is
skipped, the write tools are not enabled. If the read-back check fails, the script rolls back to
the read-only tools; if that rollback also fails, it reports `ROLLBACK FAILED` and the write tools
must be removed by hand. The association ignores later changes to its
`configuration` in Terraform, so an apply does not remove the tools the script added. See
[terraform.md](terraform.md) for the other provider gaps.

## Test it

Enable elevated (directed) actions on the Agent Space for the write tests, and disable them
afterwards. `--preferences` replaces the full preference set:

```bash
aws devops-agent update-agent-space --agent-space-id <id> --preferences elevatedActionsEnabled=true
# ... write tests ...
aws devops-agent update-agent-space --agent-space-id <id> --preferences elevatedActionsEnabled=false
```

Open a new chat after enabling them. A chat started while they were off keeps refusing the
`MUTATIVE` tool ("An approval-ref header is required") and never shows the approval request.
Before the approval request, the agent may also ask a plain confirmation question (`ask_user`):
answer it with `chat.py send`, the approval request comes next.

`scripts/dev/chat.py` reads `AGENT_SPACE_ID`, `AWS_REGION` and optionally `AWS_PROFILE`.

```bash
cd scripts/dev
python3 chat.py new read "Read ticket INC-XXXX with the ticketing tools"          # READ_ONLY, no approval
python3 chat.py new write "Add the comment 'checked' to ticket INC-XXXX"          # MUTATIVE, approval request
python3 approve.py write <executionId> ../../evidence/approval-write.json APPROVED
```

`chat.py` archives every stream event (text, tool blocks, approval requests) to
`evidence/chat-<label>.jsonl`, and writes any approval request to `evidence/approval-<label>.json`.
`approve.py` follows the documented approval flow:
`UpdateApprovalAction`, then `SendMessage` with `approvalAction.buttonText` (without it the resume
returns `VALIDATION_ERROR`).

Check the result in the ticket (`comments`, `toolCalls`) and in the runtime log group
`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`.
