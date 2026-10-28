# Terraform guide

Everything in this sample is deployed by Terraform, with `hashicorp/aws` (~> 6.0) for the mature
services and `hashicorp/awscc` (~> 1.0, Cloud Control API) for AWS DevOps Agent and Amazon Connect
AI agents. Three things are not plain Terraform resources; they are listed under
[Provider gaps](#provider-gaps) with the workaround used.

## Layout

```
terraform/
├── envs/demo/            root module: providers, variables, module wiring
│   ├── terraform.tfvars.example
│   └── backend.tf.example   S3 remote state for a shared deployment
└── modules/
    ├── kms/              customer managed key with rotation (one per Region)
    ├── foundation/       demo workload, alarm, ticket table, incident opener, event archive
    ├── mcp-ticketing/    MCP server on AgentCore Runtime + DevOps Agent registration
    ├── connect-instance/ optional: new Connect instance, claimed number, flow log group, recordings
    └── oncall/           on-call call: caller, Connect AI agent, Lex bot, flow, connect handler
```

| Module | Region | Main resources |
|:--|:--|:--|
| `kms` | both | `aws_kms_key`, `aws_kms_alias` (alias suffix `-connect` on the Connect key, so both keys can live in the same Region) |
| `foundation` | DevOps Agent Region | `meter-api` Lambda, CloudWatch alarm, DynamoDB tickets table, `incident-opener` Lambda (one incident per alarm until its ticket is CLOSED: lock item `ALARM#<name>`), EventBridge rules, event archive log group |
| `mcp-ticketing` | DevOps Agent Region | `aws_bedrockagentcore_agent_runtime`, runtime and DEFAULT endpoint resource policies, IAM roles, `awscc_devopsagent_service`, `awscc_devopsagent_association`, `terraform_data` (classification, optional trust narrowing) |
| `connect-instance` | Connect Region | `aws_connect_instance`, `aws_connect_phone_number`, flow log group `/aws/connect/<alias>`, recordings S3 bucket + `aws_connect_instance_storage_config` (`CALL_RECORDINGS`). Created only with `create_connect_instance = true` |
| `oncall` | both | DevOps Agent Region (`caller.tf`): EventBridge rule on `aws.aidevops`, `caller` Lambda, target DLQ, on-failure queue, Parameter Store `SecureString`. Connect Region: `awscc_wisdom_assistant` + association (`assistant.tf`), `awscc_wisdom_ai_prompt` and `awscc_wisdom_ai_agent` with their versions (`agent.tf`), Lex bot as `aws_cloudformation_stack` (`lex.tf`), `connect-handler` Lambda, optional `/<prefix>/oncall-email` Parameter Store `SecureString`, EventBridge rule on Connect contact events for unanswered calls (`handler.tf`), `aws_connect_contact_flow` (`flow.tf`) |

## Inputs

Copy `terraform.tfvars.example` to `terraform.tfvars` (git-ignored). Required values:

| Variable | Description |
|:--|:--|
| `agent_space_id` | existing Agent Space, in the default provider Region |
| `agent_space_region`, `connect_region` | can differ. `connect_region` cannot be changed in place on a deployed stack (destroy, then deploy again) |
| `connect_instance_id`, `connect_source_number` | existing Connect instance and its claimed number (caller ID). Required when `create_connect_instance = false` (default) |
| `create_connect_instance`, `connect_instance_alias` | `true` creates the instance (`modules/connect-instance`) instead; the alias (access URL `<alias>.my.connect.aws`) is then required |

New instance options: `connect_phone_country_code` (default `US`), `connect_phone_type` (`DID` or
`TOLL_FREE`). The default Regions are `us-east-1`: pick the Connect Region for the on-call
country, a new instance only calls a short default list of countries (see the README).

Not-acknowledged notification (all optional, off by default): `sms_origination_identity_arn`
(End User Messaging, Connect Region), `notify_email_from` (SES verified sender address; the
recipient goes in the `/<prefix>/oncall-email` SecureString, set by hand), `gate_timeout_seconds`
(default 8) and `gate_retry_timeout_seconds` (default 5).

`unanswered_delay_seconds` sets the `DelaySeconds` of the SQS queue on the contact-event path
(default 60, validated `>= 30`). It must stay longer than the notification lease and the
acknowledgment grace, so a late press-1 or acknowledgment wins the race before the delayed handler
runs. The notification lease is not a
separate input: Terraform derives it from the handler timeout (`LEASE_SECONDS` is passed to
`connect-handler` as three times its function timeout) and passes it to the Lambda, so you tune the
timeout and the lease follows.

`enable_pin` (default `false`) adds a DTMF PIN check after press 1 and before the AI session.
The PIN lives in the SecureString of the `oncall.oncall_pin_param` output, set outside Terraform:
`aws ssm put-parameter --region <connect_region> --name /<prefix>/oncall-pin --type SecureString --value <digits> --overwrite`.
`pin_max_digits` (default 6, 3 to 20) and `pin_timeout_seconds` (default 10) tune the prompt. Keep
`enable_flow_logging = false` while the PIN is on: flow logs record the Lambda parameters.

A new instance created by the `connect-instance` module is outbound only (`inbound_calls_enabled =
false`).

`deployer_principal_arns` (default `null` = the role or user running Terraform) lists the principals
exempted from the explicit Denys on the MCP runtime and its DEFAULT endpoint, on ticket table writes
and on the replay bus. Set it explicitly when a CI role deploys and people run the scripts.
`caller_reserved_concurrency` (default `1`, `-1` = unreserved for accounts still at a concurrency
quota of 10) serializes on-call calls. `budget_alert_email` (default empty = none) creates an AWS
Budgets alert on the account's monthly cost at `budget_limit_usd` (default 50). The opener counts
every suppressed repeated ALARM in the `<prefix>/oncall` `AlarmSuppressed` metric (no alarm on it).
With `notify_email_from`, set the `oncall.ses_configuration_set` output (TLS required) as the
sender identity's default configuration set:
`aws sesv2 put-email-identity-configuration-set-attributes --region <connect_region> --email-identity <identity> --configuration-set-name <name>`.
`recordings_force_destroy` (default `false`) keeps recordings on `terraform destroy`.
`narrow_devops_trust` (default `false`): when `true`, after the MCP server registration and the
tool classification, `narrow_trust.py` rewrites the DevOps Agent SigV4 role trust from `service/*`
to this registration only (`service/<id>`); `false` keeps or writes back `service/*`, the trust AWS
documents. Set it to `true` only once you have confirmed the registration id in `aws:SourceArn`
is stable. With `service/*`, any DevOps Agent registration of the account and Region that is
given the role passes the trust: use a dedicated account for a pilot.

`enable_call_recording` (default `false`) turns call recording on. When `false`, no call is
recorded. When `true`, the flow plays a spoken recording notice first and then records the call; if
the notice fails to play, the flow skips recording (fail closed). Recording also needs a
`CALL_RECORDINGS` storage configuration on the instance (a new instance from
`modules/connect-instance` has one; an existing instance without one records nothing).

`enable_flow_logging` (default `false`) controls Connect flow logging for the flow this module
creates. It is `false` because flow logs can record the Lambda block parameters: ticket, service,
priority, status and, with `enable_pin`, the entered digits (the two are refused together). The
investigation summary and the destination number are read by the handler, never passed by the
flow. When `true`, the flow writes to the
instance flow log group `/aws/connect/<alias>`. The customer managed KMS key and `log_retention_days`
apply to that group only when the sample created the instance (`create_connect_instance = true`); on
an existing instance the sample does not manage `/aws/connect/<alias>`, so its encryption and
retention are whatever the instance owner set. Leave flow logging `false` unless you accept that
content in the logs.

`enable_ai_guardrail` (default `true`) attaches an `AWS::Wisdom::AIGuardrail` to the Connect
orchestration AI agent (`modules/oncall/guardrail.tf`). The guardrail runs a `PROMPT_ATTACK` filter
at `HIGH` on the callee's input (coverage of the summary, which sits in the prompt, is not
documented), the
harmful-content filters (hate, insults, sexual, violence, misconduct). It has no personal-data
policy: the `AWS::Wisdom::AIGuardrail` resource handler rejects one (see `guardrail.tf`), so the
summary is not filtered for personal data. Blocked turns speak a short line in the call's language.
Set it to `false` only when a guardrail is managed centrally for the Connect assistant.

Useful options: `prefix` (default `devops-oncall`), `language` (`en-US`, `fr-FR`), `dry_run`
(default `true`, builds the call request without placing it), `enable_replay` (default `false`;
set `true` only for the `scripts/replay.py` demo and back to `false` afterwards: once
`dry_run = false`, an event on the dedicated `<prefix>-replay` bus places a real call, and the
bus policy lets only the deployer principals publish to it), `deletion_protection`, `log_retention_days`, `aws_profile` (used
by the local scripts). The
`oncall` module also takes `model_id` (default: Claude Haiku 4.5 profile of the Connect Region).

The on-call number and email are never Terraform inputs: the Parameter Store parameters are created with a
placeholder and `ignore_changes = [value]`, and you set the real values with `aws ssm put-parameter`.
`ignore_changes = [value]` stops Terraform overwriting the externally-set value; it does not keep
the value out of state. On refresh the AWS provider reads the parameter and persists the decrypted
value in state, so treat the state file as sensitive: use the encrypted S3 backend
(`backend.tf.example`: SSE-KMS, versioning, restricted access) and never commit or share state.

## What lives outside Terraform

| Item | Why | Where |
|:--|:--|:--|
| Agent Space and its account association | created in the DevOps Agent console | input |
| Connect instance, phone number | shared, usually owned by another team; or created by `modules/connect-instance` | inputs or `create_connect_instance` |
| On-call number and email values | keep them out of code and out of Terraform inputs; set by hand so a redeploy does not overwrite them (Terraform still reads them back into state on refresh, so treat state as sensitive) | Parameter Store, set by hand |
| DevOps Agent investigation | created at runtime by the `incident-opener` Lambda (`CreateBacklogTask`) | Lambda code |
| AgentCore Runtime log groups | created by the runtime on first invocation | see gotchas |
| Connect flow log group of an existing instance | created by Connect (`/aws/connect/<alias>`); managed by Terraform only for a new instance | delete by hand |

## Provider gaps

| Gap | Effect | Workaround in this sample |
|:--|:--|:--|
| AWS DevOps Agent exists only in `awscc` | no `hashicorp/aws` resource | `awscc_devopsagent_service` and `awscc_devopsagent_association` |
| `awscc_devopsagent_association` has no `toolDetails` | every MCP tool is `READ_ONLY`, write tools run without approval | `terraform_data` + `local-exec` runs `classify_tools.py` (AWS CLI 2.36.47 or later) after each change, then checks the result; later applies keep it |
| `AMAZON.QInConnectIntent` (Lex bot for the Connect AI agent) is in neither provider | no native Lex bot for the AI agent | `aws_cloudformation_stack` driven by Terraform, removed by `terraform destroy` |

If your platform forbids `local-exec` or CloudFormation stacks, the two workarounds are isolated:
the classification is one `update-association` call to run from your pipeline, and the Lex bot is
one CloudFormation template (`modules/oncall/lex.tf`).

## Gotchas

- **IAM propagation before the MCP registration**: DevOps Agent initializes the MCP server when
  the service is registered. With a fresh role it gets a 403. The `time_sleep` waits 30 s and is
  re-triggered when the role, its policy or the runtime resource policies change, so a rename
  waits too.
- **One assistant per Connect instance**: the `WISDOM_ASSISTANT` integration slot is unique.
  Replacing the assistant means destroy first, then create. When you move the assistant to another
  module address, add a `moved` block, otherwise Terraform creates the new one in parallel and the
  association fails.
- **AI prompt and agent versions**: do not set `modified_time_seconds` on the `awscc` version
  resources; `replace_triggered_by` alone creates the new version in the same apply.
- **`awscc` returns the AI agent ARN with `:$LATEST`**: strip it before appending a version.
- **Lex bot version**: a hash of the bot configuration forces a new version, otherwise the alias
  stays on the old one when the assistant changes.
- **Stale computed attributes**: after an in-place update of `awscc_devopsagent_service`,
  `additional_service_details` can keep old values in state while the service is correct. Run
  `terraform apply -refresh-only`.
- **AgentCore Runtime log group**: the runtime creates `/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`
  itself, without the customer managed key or a retention period, and `terraform destroy` does not
  remove it. Delete it after a destroy or a rename of the runtime.
- **S3 object of the MCP package**: use `source_hash`, not `etag`, because the package is uploaded
  in multipart and the ETag never matches.
- **`AWS::Lex::Bot` tags**: the property is `BotTags`, not `Tags` (the stack update fails
  otherwise). The `AmazonConnectEnabled = True` tag is what lets the Connect admin website manage
  the bot.
- **`coalesce` with a null variable**: `coalesce(var.x, "")` fails when `var.x` is null (all
  arguments null or empty). Use `local.enabled ? var.x : ""`.
- **Conditional IAM statements**: every statement in a `concat` of conditionals must have the same
  shape; `Resource` as a string in one and a list in another is refused. Use lists everywhere.
- **KMS aliases**: with both Regions set to the same value, `kms` and `kms_connect` would create
  the same `alias/<prefix>-<region>` (`AlreadyExistsException`); the Connect key gets `-connect`.
  On an existing deployment the fix replaces the alias only, the key stays.
- **Connect flow log group KMS**: the key policy only lets CloudWatch Logs use the key for the
  log groups the sample creates; a new instance's `/aws/connect/<alias>` is added through the kms
  module's `extra_log_groups`.

- **boto3 Lambda layer in the Connect Region**: `connect-handler` runs in the Connect Region and
  needs a pinned boto3 newer than the Lambda runtime's built-in SDK. The layer is built from the
  hash-locked `src/layers/boto3/requirements.txt` and published with the Connect-Region provider
  (`provider = aws.connect`), then attached to `connect-handler`. A layer is Region-local, so it is
  published again if you change the Connect Region.
- **Importing a pre-existing Connect flow log group**: when `create_connect_instance = true` and a
  log group `/aws/connect/<alias>` already exists (Connect created it on an earlier run), the apply
  fails on a name collision. Import it instead of deleting it:

  ```bash
  cd terraform/envs/demo
  terraform import 'module.connect[0].aws_cloudwatch_log_group.flows' /aws/connect/<alias>
  ```

  The module address is `module.connect[0]` because the `connect` module is called with `count`
  (`terraform/envs/demo/main.tf`), and the resource is `aws_cloudwatch_log_group.flows` in
  `terraform/modules/connect-instance/main.tf` (its name is `/aws/connect/${var.instance_alias}`).
  After the import, the flow log group is encrypted by the Connect-Region KMS key: the key policy
  lets CloudWatch Logs use the key only for the log groups the sample manages, and a new instance's
  `/aws/connect/<alias>` is added to that policy through the `kms` module's `extra_log_groups`, so a
  group imported this way is covered too.

## Day-2 operations

```bash
cd terraform/envs/demo
terraform plan                          # expect "No changes" after an apply
terraform apply -var dry_run=false -var enable_pin=true   # allow real calls (or allow_live_without_pin=true, sandbox only)
terraform apply                         # back to dry-run
terraform destroy                       # removes everything this project created
```

For a shared deployment, enable the S3 backend (`backend.tf.example`), run `terraform plan` in
your pipeline, and give the pipeline role the AWS CLI so the classification step can run.
