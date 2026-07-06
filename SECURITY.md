# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Grainiac, please **do not** open a public GitHub issue.

Instead, report it privately by emailing **security@warp.dev**. Include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept (if applicable)
- Any suggested mitigations

You should receive an acknowledgment within 2 business days. We will work with you to understand and address the issue, and will coordinate disclosure timing with you.

## Scope

This project is a pipeline tool that:
- Reads meeting recordings from the Grain API
- Writes structured notes to a Notion database
- Optionally posts summaries to Slack

All credentials (API tokens) are provided at runtime via environment variables and are never stored by this codebase.

## Out of Scope

- Vulnerabilities in Grain, Notion, or Slack themselves
- Social engineering attacks
- Issues requiring physical access to infrastructure
