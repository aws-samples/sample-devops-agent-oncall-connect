# Threat model

Scope: the resources this sample deploys (see the [architecture](architecture.png)). The AWS
DevOps Agent Agent Space and the Amazon Connect instance are inputs, secured by their owner. The
sample is for demonstration and evaluation, not for production use; a pilot with real on-call staff
needs a dedicated AWS account (see [Accepted risks](#accepted-risks)).

## Assets

| Asset | Where |
|:--|:--|
| On-call phone number, email and optional PIN | Parameter Store `SecureString` parameters, customer managed KMS key, set outside Terraform (read back into Terraform state on refresh) |
| Tickets and acknowledgments | DynamoDB table, customer managed KMS key, point-in-time recovery, resource policy that denies writes to every principal except the sample's roles and the deployer principals |
| Investigation summaries | AWS DevOps Agent journal; sanitized and stored on the ticket item (attribute `summary`, not a contact attribute or a flow parameter), copied into the Connect AI agent session on press 1, and sent in the no-answer email when email is configured |
| Call recordings | KMS-encrypted S3 bucket, created by `connect-instance` when `create_connect_instance = true`; on an existing instance it is the instance owner's bucket. Only written when `enable_call_recording = true` |
| Ticket write path from the DevOps Agent chat | MCP server on AgentCore Runtime, invoked only by the DevOps Agent SigV4 role and the deployer principals (runtime and DEFAULT endpoint resource policies) |

## Trust boundaries

The diagram marks them TB1 to TB3.

- **TB1**: AWS DevOps Agent to the account (EventBridge `aws.aidevops` events, SigV4 calls to the
  MCP server, operator chat and approvals in the AWS console).
- **TB2**: account to the public phone network (outbound call and voice input from the callee), and
  to the SMS and email networks for the optional notifications.
- **TB3**: untrusted text to models: log lines and ticket fields read by AWS DevOps Agent, the
  investigation summary and the callee's speech read by the Connect AI agent. One sanitizer
  (`src/common/sanitize.py`, packaged into every Lambda function and the MCP server) is applied to
  every untrusted text path: the alarm text written by `incident-opener`, the summary stored by
  `caller`, the context `connect-handler` copies into the AI agent session, the Acknowledge note,
  the values filled into the SMS and email templates, and the comments and resolutions the MCP
  server stores and returns. It drops invisible, control, private-use and unassigned code points,
  applies NFKC normalization, strips ASCII and Unicode bracket and brace look-alikes and back-ticks,
  removes dash-class and `= # ~ * _` runs and the prompt's fence phrase, repeats until the text is
  stable, then caps the length. The summary is passed to the AI agent as fenced untrusted data.

## Threats and mitigations

| # | Threat (STRIDE) | Mitigation in the sample | Residual risk / production action |
|:--|:--|:--|:--|
| T1 | **Toll fraud / spoofed trigger** (S): someone emits events to place calls | Calls and the no-answer SMS only go to the number stored in Parameter Store, never to a number from the event or the contact; the contact-event path acts only on the contact bound to the ticket (`oncallContactId`). At most one call per ticket (conditional `callState` claim). The rule on `aws.aidevops` matches only this Agent Space (`agent_space_id`); `PutEvents` on `aws.*` sources is refused by EventBridge. The replay rule is off by default (`enable_replay = false`); when on, it listens on a dedicated event bus whose policy denies `events:PutEvents` to every principal except the deployer principals | Enable the replay rule only for the `scripts/replay.py` demo and turn it back off afterwards |
| T2 | **Call storm** (D): a flapping alarm or replayed event calls repeatedly | Priority filter (HIGH, CRITICAL), `ClientToken` = execution id + status (idempotent for 7 days in Connect), EventBridge retries capped. One call per ticket: the caller claims the ticket (OPEN and no `callState`) before `StartOutboundVoiceContact`, released on a Connect error. The caller runs with a reserved concurrency of 1 by default, so calls are placed one at a time. One incident per alarm until its ticket is CLOSED: a lock item (`ALARM#<name>`) is taken before a ticket is opened; while the ticket is OPEN or ACKNOWLEDGED a repeated ALARM only comments it (up to the comment cap), however long the incident lasts, and each suppressed ALARM is counted in the `<prefix>/oncall` `AlarmSuppressed` metric. An optional AWS Budgets alert (`budget_alert_email`) warns on the account's monthly cost | A distinct alarm, or a new ALARM after the ticket is closed, still calls. A lock left pointing to a ticket that was never written (the first invocation failed for good after taking it) blocks new incidents for that alarm until the `ALARM#<name>` item is deleted; that failed invocation lands in the opener's on-failure queue. The `AlarmSuppressed` metric has no alarm: add one if a long suppression must page someone |
| T3 | **Prompt injection through logs or tickets** (T, E): crafted text steers AWS DevOps Agent to write tickets | Write tools are classified `MUTATIVE` on this server's DevOps Agent association: in the chat they need a human approval, and they are refused in autonomous investigations. The approval is a property of the association, not something the MCP server checks. The association is created fail-closed: the write tools (`add_comment`, `close_ticket`) are only enabled together with their `MUTATIVE` classification; the classification script reads the association back and rolls back to the read-only tools if the check fails. Ticket text returned by `get_ticket` and `list_open_tickets` goes through the sanitizer, and the stored investigation summary is never returned | Keep every write tool `MUTATIVE`. An unclassified write tool runs as `READ_ONLY`, without approval. Approval does not make the written text trustworthy: review what the agent proposes before approving |
| T3b | **Direct invocation of the ticket service by another caller** (S, E): a principal of the account calls the MCP runtime or writes the ticket table directly, closing a ticket without the DevOps Agent approval step (which silences the page) | The runtime and its DEFAULT endpoint carry resource policies with an explicit Deny on every invocation action for any principal other than the DevOps Agent SigV4 role and the deployer principals (`deployer_principal_arns`). The ticket table's resource policy denies data-plane writes to every principal except the `incident-opener`, `caller`, `connect-handler` and MCP runtime roles and the deployer principals. `close_ticket` only moves a ticket from OPEN or ACKNOWLEDGED; this is a consistency rule, not an access control (closing an OPEN ticket is allowed) | The deployer principals keep direct access by design: restrict who can assume them. The DevOps Agent SigV4 role trusts `service/*` in the account and Region (see [Accepted risks](#accepted-risks)) |
| T3c | **Ticket text re-read by the AI on every call** (T): text written once (alarm reason, a callee's note, a comment) is read again by AWS DevOps Agent each time it reads the ticket, so an injection persists | Every string stored or returned by the ticket service passes the sanitizer; ticket ids must match `INC-<8 hex>`; comments and resolutions are capped at 2000 characters and a ticket holds at most 50 comments; `toolCalls` keeps only the length of each text argument; `list_open_tickets` reads at most 5 scan pages and returns at most 100 tickets (truncation is logged). The stored investigation summary is never returned to AWS DevOps Agent | Plain-prose instructions pass the sanitizer. Writes still need approval (T3), so injected ticket text can at most influence what the agent proposes |
| T4 | **Prompt injection through the summary or the callee** (T): injected text in the investigation summary, or the callee, induces a false acknowledgment (which replaces the no-answer SMS and email by a receipt) or steers the spoken answers | The ticket ID comes from the contact attribute set by the `caller` Lambda, not from the model. The summary is sanitized and placed in the prompt as fenced untrusted data with an instruction never to follow instructions inside it; the Acknowledge note is sanitized the same way (200 characters). An `AWS::Wisdom::AIGuardrail` with a `PROMPT_ATTACK` input filter (HIGH) and harmful-content filters is attached to the AI agent by default. The prompt accepts as acknowledgment an explicit statement that the engineer takes the incident, or a short confirmation (a bare "yes") to the agent's direct question "can you take this incident?"; never a "yes" to another offer, and never in the turn where the agent asks. The model's `Acknowledge` alone does not acknowledge: the flow then asks the callee to press 1 to confirm ("to confirm you take incident X, press 1"). Only on 1 does `connect-handler` write ACKNOWLEDGED (`ackMethod: voice+dtmf`), and only from the call placed for that ticket (`oncallContactId`); without the 1 the ticket stays OPEN and the normal SMS/email goes out. A confirmed acknowledgment sends one written receipt ("you acknowledged INC-x at T") on the configured channels. `dry_run = false` requires `enable_pin = true` unless `allow_live_without_pin = true` | Sanitizing, fencing and capping reduce but do not remove prompt-injection risk: plain-prose instructions and homoglyphs NFKC does not fold pass through, and the model can still misstate the summary. The guardrail documentation does not say its prompt-attack filter scans the summary, which sits in the prompt, so that path relies on the sanitizer and the fence. An ACKNOWLEDGED ticket is never re-paged (no call, SMS or email after the receipt): a wrong confirmation is corrected out of band, from the receipt or in the DevOps Agent chat. There is no reopen path (status goes OPEN to ACKNOWLEDGED or CLOSED only) |
| T5 | **Disclosure of the incident to the wrong person** (I): voicemail or a reassigned number hears the summary; the no-answer email carries the full summary off-account through SES | The number is set outside Terraform and masked in logs. The SMS carries only ticket, service, priority and status. The email (`notify_email_from`) carries the full summary to the `/<prefix>/oncall-email` recipient, through an SES configuration set that requires TLS (assign it as the sender identity's default configuration set). Both channels are off by default. Optional `enable_pin`: a DTMF PIN, checked in constant time against a Parameter Store `SecureString`, gates the AI session, and the opening message drops the service name | Turn on `enable_pin` with real people, with flow logging off. The PIN is a bystander control, not authentication: two attempts per call, no lockout across calls, length chosen by the deployer (use 6 digits or more). Send the email to a team mailbox you control. The summary is written by a model from logs and is not filtered for personal data |
| T5b | **PII in logs** (I): phone numbers, email addresses or the summary leak into CloudWatch or flow logs | The handler logs error codes and types only, never phone numbers, email addresses, summaries or free text; the AI tool audit lines carry ids, outcomes and lengths only. The summary is not a contact attribute or a flow parameter, so flow logs do not record it; flow logs can record the remaining Lambda block parameters (ticket, service, priority, status, and with the PIN on the entered digits), so flow logging is off by default and refused together with `enable_pin`. Customer managed KMS keys and `log_retention_days` apply to the log groups the sample creates | Leave `enable_flow_logging = false` unless you accept the short incident context in the flow log group. The AgentCore Runtime log group is created by the service without the customer managed key or a retention period. Lower the retention default outside a demo, and review any custom log line before adding it |
| T5c | **Summary persisting in Connect records** (I): the incident content outlives the call in Connect | The summary is stored on the ticket, not in contact attributes, so Connect contact records do not carry it. Contact records keep the short context (ticket, service, priority, status) | The Amazon Q in Connect session holds the summary for the session lifetime, and what the AI agent speaks is derived from it; transcripts and recordings, when enabled on the instance, can keep that content. Retention of contact records, transcripts and sessions follows the instance, which this sample does not set: decide it before calling real people |
| T6 | **Secret or PII in state, code or logs** (I) | Number, email and optional PIN set with `aws ssm put-parameter` (not Terraform inputs, not Lambda environment variables), masked in logs; customer managed KMS keys on logs, Lambda environment, table, queues, the code and recordings buckets and the parameters. `ignore_changes = [value]` keeps the value out of code, not out of state: Terraform reads the decrypted parameter back into state on refresh. Identity policies use the keys only through the services that need them (`kms:ViaService`) | Use the encrypted remote state (`backend.tf.example`: SSE-KMS, versioning, restricted access) and never commit or share state; the default local backend writes state in clear text |
| T6b | **Account-root delegation on the shared key** (E): the key policies give the account root `kms:*` (the AWS default), so any principal of the account whose identity policy allows KMS actions can use the key that protects tickets, parameters and logs | The sample's own identity policies grant the key only through the services they use (`kms:ViaService`); the service-principal statements in the key policy are scoped to the sample's log groups and EventBridge rules | Review which principals of the account have KMS permissions on `*`; use a dedicated account, or replace the root delegation by explicit key administrators and users |
| T7 | **Unauthenticated access to the workload or MCP server** (S) | No public endpoint on the demo workload (Lambda API only). AgentCore Runtime requires SigV4; its resource policies let only the DevOps Agent SigV4 role (and the deployer principals) invoke it (T3b). The SigV4 role's identity policy allows only `bedrock-agentcore:InvokeAgentRuntime` on this runtime | Put AWS WAF in front of any public API you add |
| T7b | **Confused deputy on service principals** (E): another account or resource gets a service principal to act on the sample's resources | AgentCore runtime role trust: `aws:SourceAccount` plus `aws:SourceArn` `runtime/<runtime name>-*`. DevOps Agent SigV4 role trust: `aws:SourceAccount` plus `aws:SourceArn` `service/*`. Lex bot role trust: `aws:SourceAccount` only (the Lex documentation gives no source ARN form). Lambda execution roles: no condition, as documented for `lambda.amazonaws.com`. Event-archive log resource policy: `events.amazonaws.com` and `delivery.logs.amazonaws.com`, write actions only, on the sample's archive log group, with `aws:SourceAccount`. KMS key policy for EventBridge: `aws:SourceAccount` plus `aws:SourceArn` on the sample's rules. SQS queue policies: `aws:SourceArn` (the EventBridge rule) | Review the conditions if you add another writer. The `service/*` DevOps Agent trust is an accepted risk |
| T8 | **Lost or duplicate alerts** (D): the caller, the ticket opener or the notification fails and nobody is paged, or the same ticket is notified twice | Lambda on-failure destinations and EventBridge target DLQs (encrypted SQS, TLS-only queue policies). CloudWatch depth alarms on the five failure queues: the opener's DLQ and on-failure queue, the caller's target DLQ and on-failure queue, and the unanswered-call dead-letter queue (`maxReceiveCount` 3). The notification claims the ticket (`notifyState` CLAIMED/SENT/FAILED with an owner lease) so retries do not stack comments. Delivery is at-least-once, not exactly-once: a message accepted before the attempt records SENT can be resent. Amazon Connect contact events are delivered on a best-effort basis, so a never-answered call whose `DISCONNECTED` event is lost is not seen by any queue | Attach an SNS action to the depth alarms (they have none by default) to be paged, and treat a second notification as possible. The sample's own functions have no `Errors` alarm: their failures are visible through the failure queues |
| T8b | **Event versus acknowledgment race** (D): the contact-event notification fires while a late press-1 or acknowledgment is in flight | The `DISCONNECTED` event goes through an SQS queue with a delay longer than the lease and the acknowledgment grace, so an acknowledgment that lands first flips the ticket to ACKNOWLEDGED and the delayed handler sends the receipt instead of the not-acknowledged notification | Keep the delay above your worst-case acknowledgment latency if you change the lease |
| T9 | **Supply chain** (T) | Python dependencies of the Lambda layer, the MCP server and the development tools locked with hashes; `pip-audit`, a secret scan and a SAST scan in CI; boto3 layer and MCP package rebuilt from the lock; Terraform providers pinned in `.terraform.lock.hcl` | Sign Lambda code (AWS Signer) in your pipeline |
| T10 | **Repudiation of an acknowledgment** (R) | The ticket records the acknowledgment (`acknowledgedAt`, `acknowledgedContactId`, `ackMethod: voice+dtmf`) and a comment with the AI agent note; `connect-handler` and the MCP server write one structured audit line per AI tool call (`{"audit": "ai_tool_call", "tool", "ticketId", "result", ...lengths}`, no free text); the call is recorded when `enable_call_recording = true`; CloudTrail logs API calls | The AI agent turns are not exported by default: to investigate a disputed acknowledgment, turn on call recording (with notice), and enable CloudTrail data events on the table for per-item audit |
| T11 | **Recording without consent** (I, legal): the call is recorded without the callee knowing | Recording is opt-in (`enable_call_recording = false` by default); when on, the flow plays a spoken recording notice before recording and, if the notice fails to play, skips recording (fail closed). Recordings are KMS-encrypted and expire after `log_retention_days` | Confirm your recording-consent and retention obligations per jurisdiction before enabling it with real people |

## Public release threats

| # | Threat | Mitigation in the repository |
|:--|:--|:--|
| P1 | **Credential or account data leak** in the published code or history | No credentials, account IDs, phone numbers or resource IDs in the code: the account comes from the caller's credentials, IDs are Terraform inputs (`terraform.tfvars`, gitignored), the number is set with `aws ssm put-parameter`. State, local values and test evidence are gitignored. A secret scan runs in CI |
| P2 | **Supply chain / dependency confusion** | All Python packages pinned with hashes (`--require-hashes`), public PyPI only, no private package names. Terraform providers pinned in `.terraform.lock.hcl` |
| P3 | **Copy to production as-is** | README disclaimer and security considerations, `dry_run = true` by default, the live-call guard on the PIN, the replay switch, and a "residual risk / production action" column for every threat above |
| P4 | **Malicious fork or pull request** (backdoored copy, poisoned contribution) | Users deploy from the official repository; contributions go through review (`CONTRIBUTING.md`, PR template); security reports go to AWS Security (`SECURITY.md`), not public issues |
| P5 | **Toll fraud through a replicated deployment** | The number is never taken from an event (T1). Each deployment calls only the number its owner sets |

## Generative AI controls

| Model use | Input it reads | Control |
|:--|:--|:--|
| AWS DevOps Agent (managed service) | Logs, metrics, ticket fields through the MCP server | Write tools `MUTATIVE` on the association (human approval in the chat, refused in autonomous investigations); ticket text sanitized and bounded (T3c); the MCP runtime role only reaches the ticket table |
| Connect AI agent (Amazon Bedrock, Claude Haiku 4.5 through a cross-Region inference profile) | Investigation summary, callee speech | The summary is sanitized, injected as fenced untrusted data (never followed as instructions) and capped in length. Two tools: `Acknowledge` (writes a note on the ticket set by the caller and takes effect only after the callee presses 1, T4; the note is sanitized and capped at 200 characters) and `Complete` (ends the conversation). No tool reads other data |

An `AWS::Wisdom::AIGuardrail` is attached to the AI agent by default (`enable_ai_guardrail = true`,
`modules/oncall/guardrail.tf`): a `PROMPT_ATTACK` input filter (HIGH) and the harmful-content
filters (hate, insults, sexual, violence, misconduct) on input and output; blocked turns speak a
short line in the call's language. It has no sensitive-information, denied-topic or grounding
policy: the `AWS::Wisdom::AIGuardrail` resource handler rejects a sensitive-information policy. The
summary is therefore not filtered for personal data. Set `enable_ai_guardrail = false` only when a
guardrail is managed centrally for the Connect assistant.

## Accepted risks

- **DevOps Agent SigV4 role trust on `service/*`.** The role must exist before the MCP server
  registration, so its trust allows any DevOps Agent service registration of the account and Region
  (`aws:SourceArn` `service/*`), the form AWS documents for SigV4 MCP registration. The runtime
  resource policies limit who can call the server; the trust limits who can become the role, and
  any registration in the account that is given this role passes it. `narrow_devops_trust` (default
  `false`) can rewrite the trust to this registration only (`service/<id>`); turn it on only once
  you have confirmed the registration id in `aws:SourceArn` is stable in your Region.
- **Unconfirmed spoken intent.** The AI agent decides that the engineer took the incident; the
  key press confirms it, and an acknowledged ticket is never re-paged (T4).
- **No personal-data filter on the summary** (guardrail limitation above).
- **Account-root `kms:*` on the shared key** (T6b) and Lambda execution-role trusts without a
  condition (as documented by AWS).
- **AgentCore Runtime log group** without the customer managed key or a retention period, created
  by the service.
- **Default local Terraform state** holding the decrypted on-call number, email and PIN until you
  use the remote backend.

Because of these, use a dedicated AWS account for any pilot with real on-call staff.

## Production actions

- Use the encrypted remote state (`backend.tf.example`) before the first `terraform init`.
- Turn the PIN on (`enable_pin = true`, 6 digits or more) and keep flow logging off.
- Decide the retention of Connect contact records, transcripts and recordings on the instance.
- Place a test call to voicemail and to an unanswered phone, and check the ticket comments, the
  SMS/email and the receipt.
- Attach an SNS topic to the failure-queue alarms, and decide whether the `AlarmSuppressed` metric
  needs an alarm.
- Use a dedicated account, and review which principals are in `deployer_principal_arns`.

## Accepted scanner findings

Each accepted finding is justified next to the resource with a `#checkov:skip`, `# nosemgrep` or
`# nosec` comment, so any scanner reading the code sees the same reasons.
