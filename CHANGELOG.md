# Changelog

All notable changes to this sample are documented in this file.

## 0.1.0: Initial release

- A CloudWatch alarm opens a ticket and an AWS DevOps Agent investigation; when the investigation
  ends, Amazon Connect calls the on-call engineer, and a Connect AI agent summarizes the findings
  and takes the acknowledgment (spoken intent confirmed with a key press).
- Ticketing MCP server on Amazon Bedrock AgentCore Runtime, registered in AWS DevOps Agent with
  SigV4, with write tools that need a human approval.
- No-answer notification by SMS and email, acknowledgment receipt, optional PIN gate, optional call
  recording with a spoken notice.
- Terraform modules for both Regions (`hashicorp/aws` and `hashicorp/awscc`), English and French
  locales.
