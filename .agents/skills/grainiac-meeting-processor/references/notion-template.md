# Enterprise Customer Notion Page Template

This document defines the structure and expected content for each enterprise customer's living Notion page. The goal is to create a single source of truth for every customer conversation.

This is a generic, product-agnostic template. Replace the bracketed placeholders — `[Your Company]` (your company/vendor name) and `[Product]` (the product or platform you sell) — and swap any example tool, provider, or use-case names for ones that fit your own sales motion. The sections and guidance are meant to work out of the box for B2B/enterprise sales tracking.

Each customer gets one Notion page. It is updated after every call.

---

## Follow-Up Items

> Checkbox list of action items owned by our team. Add new items after each call, check them off as completed.

- [ ] Example: Send security questionnaire responses
- [ ] Example: Follow up on deployment architecture question from 3/10 call
- [ ] Example: Share Trust Center / security documentation access
- [ ] Example: Schedule technical deep-dive with their platform team
- [ ] Example: Send proposal for their planned deployment
- [ ] Example: Answer open integration question from the last call

Items should include enough context to be actionable without re-watching the call. Include the date the item was raised.

---

## Key Activities & Decisions

> Reverse-chronological log of significant milestones and decisions. This is the "timeline of the deal" — someone should be able to skim this section and understand where things stand.

Examples of what belongs here:
- **2026-03-10** — Kicked off POC with 25 developers, 2-week initial period
- **2026-03-05** — Customer confirmed they want hybrid deployment (self-hosted workers + vendor-managed control plane)
- **2026-02-24** — Set up shared Slack channel for POC support
- **2026-02-10** — NDA fully executed
- **2026-01-22** — Customer decided to proceed with Bedrock as inference provider
- **2026-01-09** — Scheduled weekly sync cadence (Tuesdays 12:30 ET)
- **2025-12-05** — Initial discovery call; strong interest in cloud agents for migration workflows

This is not a place for granular call notes — it's for decisions and inflection points that move the deal forward or change direction.

---

## Meeting Log

> List of all meetings, newest first. Each entry is the meeting title hyperlinked to the Grain recording.

Format:
- [Meeting Title (YYYY-MM-DD)](https://grain.com/share/recording/...)

Example:
- [[Your Company] & Acme | Product Discussion with Zach (2026-03-10)](https://grain.com/share/recording/abc123/xyz)
- [[Your Company] & Acme | Weekly Sync on POC (2026-03-05)](https://grain.com/share/recording/def456/xyz)
- [[Your Company] & Acme | POC Scheduling (2026-01-22)](https://grain.com/share/recording/ghi789/xyz)

---

## Company Overview

| Field | Expected Content |
|---|---|
| Company Name | |
| Industry | |
| Company Size | Total employees and number of developers/engineers |
| Number of Repos | Approximate count; mono-repo vs. multi-repo |
| CRM Deal Link | |
| Deal Stage | e.g. Discovery, POC, Negotiation, Closed Won |
| Account Owner | |
| SE Assigned | |
| Overall Sentiment | Updated after each call: Strong positive / Positive / Neutral / Cautious / At risk |

---

## People & Organization

### Key Stakeholders

For each person, capture:
- **Name**
- **Title / Role**
- **Email**
- **Stakeholder Type**: Economic Buyer, Technical Champion, Evaluator, Legal/Procurement, End User
- **Notes**: Anything relevant — what they care about, how they communicate, personal details they've shared (e.g. kids, hobbies, location, background)

### Organizational Structure

- Which teams are involved (e.g. CTO Office, Platform Engineering, DevEx, InfoSec, Procurement)
- Reporting lines relevant to the deal
- Who is the internal champion driving adoption
- Who has budget authority

---

## Tech Stack & Environment

### Developer Environment
- **OS**: Windows, Mac, Linux, or mixed (e.g. "primarily Windows, migrating to Linux containers")
- **IDE / Editor**: VS Code, JetBrains, Vim, etc.
- **Terminal**: Current terminal usage and any pain points
- **Container tooling**: Docker Desktop, Podman, dev containers, etc.
- **Dev environment model**: Local machines, remote dev boxes, cloud workspaces, VDI

### Source Control & CI/CD
- **Source control**: GitHub, GitLab (cloud or on-prem), Bitbucket, etc.
- **CI/CD**: Jenkins, GitHub Actions, GitLab CI, Buildkite, CircleCI, etc.
- **Code review process**: PR-based, trunk-based, etc.

### Infrastructure
- **Cloud provider(s)**: AWS, GCP, Azure, on-prem, hybrid
- **Container orchestration**: Kubernetes (EKS, GKE, AKS), ECS, Nomad, etc.
- **Internal package registries**: Artifactory, internal Docker registries, etc.

### Authentication & Secrets
- **SSO provider**: Okta, Azure AD, etc.
- **Secrets management**: HashiCorp Vault, AWS Secrets Manager, etc.
- **Other auth requirements**: LDAP, MCP-based auth, certificate-based, etc.

---

## Current Tooling & Competitive Landscape

- **Competing / incumbent tools in use**: Which products in your category they already use
- **Tools evaluated and rejected**: What they tried and why they passed
- **Alternatives currently being evaluated**: Other vendors in the running for this deal
- **Internal / homegrown efforts**: Any in-house solutions they've built to solve this problem
- **Selection process**: How they evaluate and approve new tools (e.g. narrowing a shortlist based on internal studies and user feedback)
- **Vendor consolidation stance**: Do they want fewer vendors or best-of-breed per category?

---

## Requirements

### Deployment Model

What deployment model fits their needs? For many enterprise deals this is the most important technical question.

- [ ] **Vendor-hosted** (default SaaS — the vendor manages infrastructure)
- [ ] **Hybrid** (vendor-managed control plane + self-hosted components in the customer environment)
- [ ] **Fully self-hosted** (all components run in customer infrastructure)
- [ ] **Airgapped** (no external network access)

Notes on deployment constraints (e.g. "must access internal Docker registries", "code cannot leave their network"):

### Inference & LLM Requirements

Which inference model fits their needs? *(Applies to AI-powered products; skip if not relevant to what you sell.)*

- [ ] **Vendor-managed** (default)
- [ ] **BYO-LLM via AWS Bedrock**
- [ ] **BYO-LLM via Microsoft Azure AI Foundry**
- [ ] **BYO-LLM via Google Vertex AI**
- [ ] **Fully airgapped / on-prem model hosting**

Additional inference details:
- **LLM gateway requirements**: Do they route through an internal gateway/proxy? If so, which one and for what purpose (logging, policy enforcement, rate limiting)?
- **Approved models**: Which LLM providers/models have passed their governance review?
- **Data retention stance on LLM calls**: Are zero-retention guarantees sufficient, or must inference never leave their network?
- **Multi-model needs**: Different models for different tasks? Fallback routing?

### Security & Compliance

- **Data sovereignty**: Where must data reside? Any geographic restrictions?
- **Data retention**: Zero-retention requirements? Maximum retention periods?
- **Auditability**: What level of audit logging is required for agent actions?
- **Sandboxing**: What isolation requirements exist for agent execution?
- **Compliance frameworks**: SOC 2, FedRAMP, ISO 27001, internal frameworks, etc.
- **Security review artifacts needed**: Trust center access, architecture diagrams, pen test reports, security questionnaires
- **Network controls**: Proxy requirements, egress restrictions, VPN requirements

### Governance & Admin Controls

- **Model access governance**: Who approves which LLMs can be used?
- **Admin controls needed**: Usage limits, credit caps, feature gating per team
- **Permission scoping**: What can agents access vs. what is off-limits?
- **Approval workflows**: Do agent actions (e.g. PRs, deployments) require human approval?
- **Governance dashboard**: Do they need visibility into agent activity, costs, model usage?

### Integration Must-Haves

List specific integrations that are required for the deal to proceed:
- Source control integration (GitHub / GitLab / Bitbucket — cloud or on-prem)
- CI/CD integration
- Slack / Teams integration
- Monitoring / observability tools
- Internal tools or APIs
- SSO / SCIM provisioning

### Feature Blockers

List any features that are hard blockers — the deal cannot proceed without them:
- Feature:
- Current status (shipped / on roadmap with ETA / not planned):
- Workaround available?:

---

## Use Cases

### Primary Use Cases

For each use case, capture what it is, how important it is to the customer, and any specifics discussed.

Track the concrete workflows the customer wants your product to solve. Replace the examples below with the use cases that matter for your product; these are illustrative (from a developer-tooling context) to show the level of detail to capture:
- Large-scale repository migrations (e.g. framework upgrades across many repos)
- Flaky test triage and fixes
- PR triage and automated code review with CI integration
- Ticket-to-PR automation (end-to-end from a Jira/Linear ticket to a pull request)
- Background / long-running automation tasks
- DevOps / infrastructure automation

### Secondary Use Cases

Additional or adjacent workflows the customer mentioned that aren't the primary driver but could expand the account:
- Broader team enablement or knowledge sharing
- Non-core or non-technical use cases

### Priority Ranking

Which use cases are they most interested in starting with? What would a successful first project look like?

---

## Commercial

### Pricing

- **Pricing model discussed**: Seat-based, consumption-based, or hybrid
- **Estimates shared**: What specific numbers were quoted
- **Customer's budget expectations**: What they've indicated they're willing to spend
- **Per-user cost projections**: Based on expected usage patterns
- **Number of seats**: Initial deployment size and potential expansion

### Procurement & Legal

- **NDA status**: Not started / In progress / Executed
- **Legal review status**: Not started / Redlines in progress / Approved
- **Vendor onboarding system**: Zip, Coupa, internal process, etc.
- **Procurement process**: What steps remain and who owns them
- **Security review status**: Not started / In progress / Approved
- **Timeline to close**: Customer's stated timeline or our estimate
- **Budget approval status**: Approved / Pending / Unknown

### Contract Details

- **Contract type**: Enterprise agreement, POC agreement, pilot terms
- **Term length**: Annual, multi-year, month-to-month during POC
- **Renewal date**: If existing contract

---

## POC / Pilot Tracker

> Only relevant if a POC/pilot is active or planned.

| Field | Expected Content |
|---|---|
| POC Status | Not started / Active / Completed / Extended |
| Kickoff Date | |
| End Date | |
| Extension Date (if applicable) | |
| Number of Users | |
| Success Criteria | What was agreed upon to evaluate the POC |

### Milestones

- Kickoff session date and format (overview + deep dive)
- Mid-cycle check-in date
- Final review date
- Report/deliverable due dates

### Adoption & Usage Metrics

- Active users / total invited
- AI credit consumption (total and per-user)
- Power user identification (who is using it most)
- Usage patterns and anomalies (e.g. credit spikes)
- Sentiment survey results if collected

### Enablement Plan

- Kickoff session(s) planned
- Lunch-and-learns or office hours
- User documentation or guides shared
- Slack/email support channel established
- Phased rollout plan (e.g. sandbox → internal beta → broader pilot)

---

## Rollout & Adoption Strategy

> How the customer plans to roll out the product internally beyond the POC.

- **Target user groups**: Which teams first, expansion plan
- **Rollout approach**: Big bang, phased by team, opt-in, mandated
- **Enablement needs**: Training sessions, documentation, champions program
- **Internal communication plan**: How they'll announce to their developers
- **Adoption targets**: What metrics they care about for successful adoption

---

## Feature Requests & Product Feedback

> Track every feature request and piece of product feedback. This feeds back to the product team and is critical for roadmap prioritization.

For each item:
- **Request**: What they asked for
- **Context**: Why they need it, which call it came from
- **Priority for the deal**: Blocker / High / Medium / Nice-to-have
- **Status**: Shipped / On roadmap (ETA) / Under consideration / Not planned

---

## Partnership Opportunities

> Not relevant for every customer, but capture when applicable.

- Co-marketing opportunities
- Integration partnerships (e.g. building a joint plugin or MCP integration)
- Joint case studies or testimonials
- Referral potential to other companies

---

## Communication

- **Primary communication channel**: Slack Connect, email, Teams
- **Shared Slack channel**: Channel name and link if established
- **Meeting cadence**: Weekly, biweekly, ad-hoc
- **Preferred meeting times**: Timezone and scheduling preferences
- **Key contacts for scheduling**: Who to reach out to for booking meetings

---

## Sentiment & Relationship Notes

> Qualitative notes updated after each call. This is where you capture the "feel" of the relationship.

- How engaged are they in calls?
- Are they bringing new stakeholders in (good sign) or shrinking attendance?
- Any concerns or objections raised?
- Notable positive reactions to demos or features?
- Any personal context that helps build the relationship?
- Are they responsive between calls or do we have to chase?
