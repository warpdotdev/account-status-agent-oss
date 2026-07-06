---
name: grainiac-meeting-processor
description: >
  Analyze a single customer meeting transcript from Grain and create or update that
  company's page in the Account Tracking Notion database. Use this skill when given a
  Grain recording ID, company name, and meeting details. The skill covers fetching the
  transcript, extracting information against a structured template, and writing to Notion
  via its API. Requires GRAINIAC_GRAIN_TOKEN, GRAINIAC_NOTION_TOKEN, and GRAINIAC_NOTION_DATABASE_ID env vars.
---

# Grainiac Meeting Processor

Process a single meeting: fetch transcript from Grain, analyze it, create or update the company's Notion page.

## Environment Requirements

- `GRAINIAC_GRAIN_TOKEN` — Grain personal access token
- `GRAINIAC_NOTION_TOKEN` — Notion integration token
- `GRAINIAC_NOTION_DATABASE_ID` — Account Tracking database ID (**required**)
- `GRAINIAC_NOTION_TITLE_PROPERTY` — (optional) name of the database's title property holding the company name (default: `Company`)

## Input

The orchestrator passes these values in the prompt:
- `RECORDING_ID` — Grain recording UUID
- `COMPANY` — Company name (may need correction based on transcript content)
- `GRAIN_URL` — Link to the Grain recording
- `MEETING_TITLE` — Title of the meeting
- `MEETING_DATE` — Date string (YYYY-MM-DD)

## Procedure

### Step 1: Fetch the transcript

Use the Grain client to get the full transcript and recording metadata:

```python
import sys
sys.path.insert(0, "scripts")
from grain_client import get_recording, get_transcript_text

recording = get_recording("<RECORDING_ID>", include_participants=True)
transcript = get_transcript_text("<RECORDING_ID>")
```

Also extract: `title`, `date`, `participants`, `summary`, `public_url` from the recording object.

### Step 2: Read the full transcript

Read the transcript text in its entirety. This is the primary source of truth — not the AI summary. Extract all details that map to the Notion template categories. See [references/notion-template.md](references/notion-template.md) for the full template.

### Step 3: Analyze the transcript

Extract information for every applicable section of the template. For each category, capture verbatim quotes and specific details where possible. Key categories:

**Always extract (every meeting):**
- Follow-up items (action items with owner and date)
- Key activity / decision summary (one bullet for the timeline)
- People mentioned (names, titles, roles, emails, personal details)
- Sentiment and engagement signals

**Extract when discussed:**
- Tech stack details (specific tools, platforms, infra mentioned)
- Deployment model preferences (self-hosted, hybrid, cloud, air-gapped)
- Inference/LLM requirements (vendor-managed, BYO-LLM, gateway, air-gapped) — for AI products
- Security and compliance requirements
- Governance and admin control needs
- Integration requirements
- Feature blockers or requests
- Product use cases and target workflows
- Pricing or commercial discussions
- Procurement/legal status (NDA, legal review, vendor onboarding)
- Communication preferences (Slack, email, cadence)

**Important analysis guidelines:**
- Distinguish between what the customer said vs. what our team said.
- Capture customer concerns and objections verbatim where possible.
- Note buying signals (proactive next steps, urgency, bringing in stakeholders).
- Flag corrections to previously recorded information.
- Mark sections as "Not discussed" when a topic did not come up — do not fabricate.

### Step 4: Check for existing Notion page

```python
from notion_client import find_company_page
page = find_company_page("<COMPANY>")
```

If `page` is `None`, proceed to Step 5a (create). Otherwise, proceed to Step 5b (update).

### Step 5a: Create a new page

Build the full page structure following the template in [references/notion-template.md](references/notion-template.md). Use the block builders from `scripts/notion_client.py`:

```python
from notion_client import (
    create_page, append_blocks, rich, heading1, heading2, bullet, todo, divider, paragraph
)
```

The page sections, in order:
1. **Follow-Up Items** — checkbox list of action items from our team
2. **Key Activities & Decisions** — reverse-chronological milestones
3. **Meeting Log** — hyperlinked meeting titles, newest first
4. **Company Overview** — name, industry, size, deal stage, sentiment
5. **People & Organization** — stakeholders, org structure
6. **Tech Stack & Environment** — dev env, source control, infra, auth
7. **Current Tooling & Competitive Landscape**
8. **Requirements** — deployment model, inference/LLM, security, governance, integrations, blockers
9. **Use Cases** — primary and secondary use cases, priority ranking
10. **Commercial** — pricing, procurement/legal, contract
11. **POC / Pilot Tracker**
12. **Communication** — channels, cadence, scheduling
13. **Sentiment & Relationship Notes**

Separate each H1 section with a divider block. Notion limits 100 blocks per API call — use multiple `append_blocks` calls for the remaining blocks after the initial `create_page`.

### Step 5b: Update an existing page

This is the more common case. Fetch existing blocks and surgically update:

```python
from notion_client import (
    get_all_blocks, append_blocks, update_block,
    find_heading_block, get_last_block_in_section,
    rich, bullet, todo
)

page_id = page["id"]
blocks = get_all_blocks(page_id)
```

**Update strategy for each section:**

**Follow-Up Items:** Add new `todo` blocks after the last existing todo in the section. If a previous follow-up was completed (confirmed in transcript), update its block with `checked: True`.

**Key Activities & Decisions:** Insert a new bullet after the last existing activity bullet (before the divider). Use format: `**YYYY-MM-DD** — Summary of key decision or milestone`.

**Meeting Log:** Insert the new meeting as the first bullet after the "Meeting Log" heading (newest first). Format: hyperlinked title.

**All other sections:** For each piece of new information:
- If it supplements existing content, append new bullets after the last block in that section.
- If it corrects existing content, use `update_block` to modify the specific block.
- If a section previously said "Not discussed" and now has data, replace that block.

Use `find_heading_block` and `get_last_block_in_section` to locate insertion points. Use `find_block_by_text` to find specific blocks to update.

### Step 6: Verify

After all writes, fetch the blocks again and do a sanity check:
- Confirm the new meeting appears in the Meeting Log
- Confirm new follow-up items were added
- Confirm the key activity was logged

Report completion with: company name, meeting title, whether page was created or updated, and the Notion page URL.

## Important Notes

- Notion API limits: 100 blocks per append call, 3 requests/second. Batch blocks and add brief delays if needed.
- All block text content has a 2000-character limit per rich text segment. Split long content across multiple segments.
- When the transcript is ambiguous or the company name from email domain seems wrong (e.g., "Gmail"), use context from the transcript to determine the correct company name.
- For the Meeting Log entry, the Grain URL should come from the recording's `public_url` field.
