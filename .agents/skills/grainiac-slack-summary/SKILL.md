---
name: grainiac-slack-summary
description: >
  Fetch a Grain recording, summarize the meeting, and post a structured summary
  to Slack. Use this skill when given a Grain recording ID and a Slack channel.
  The skill fetches the transcript, generates a concise summary, and posts a
  formatted Slack message with the meeting title, participants, recording link,
  and summary. Requires GRAINIAC_GRAIN_TOKEN and GRAINIAC_SLACK_TOKEN env vars.
---

# Grainiac Slack Summary

Fetch a single Grain recording, summarize the meeting, and post a nicely formatted message to Slack.

## Environment Requirements

- `GRAINIAC_GRAIN_TOKEN` — Grain personal access token
- `GRAINIAC_SLACK_TOKEN` — Slack Bot OAuth token (xoxb-...)
- `GRAINIAC_SLACK_CHANNEL` — Slack channel to post summaries to (e.g. `#meeting-summaries`)
- `GRAINIAC_INTERNAL_DOMAIN` — (optional) your company's email domain, used to label internal vs. external attendees

## Input

The prompt should include:
- `RECORDING_ID` — Grain recording UUID

Optional (will be fetched from Grain if not provided):
- `COMPANY` — Company name
- `MEETING_TITLE` — Title of the meeting

The Slack channel is read from the `GRAINIAC_SLACK_CHANNEL` environment variable.

## Procedure

### Step 1: Fetch the recording and transcript

Use the Grain client to get recording metadata and the full transcript:

```python
import sys
sys.path.insert(0, "scripts")
from grain_client import get_recording, get_transcript_text

recording = get_recording("<RECORDING_ID>", include_participants=True, include_ai_summary=True)
transcript = get_transcript_text("<RECORDING_ID>")
```

Extract from the recording object:
- `title` — meeting title
- `start_datetime` — meeting date/time
- `participants` — list of attendees (each has `name`, `email`, `scope`)
- `public_url` — link to the Grain recording
- `summary` — Grain's AI summary (use as a starting point, but refine from the transcript)

### Step 2: Identify participants

Separate participants into internal (your company's domain) and external. The internal domain is read from the `GRAINIAC_INTERNAL_DOMAIN` environment variable:

```python
from grain_client import identify_company_from_participants

participants = recording.get("participants", [])
company, external_participants = identify_company_from_participants(participants)
```

If `COMPANY` was provided in the prompt, use that. Otherwise, use the inferred company name.

Build participant lists:
- **External attendees**: names of external participants
- **Internal attendees**: names of participants with `scope == "internal"` or an email on your internal domain (`GRAINIAC_INTERNAL_DOMAIN`)

### Step 3: Summarize the meeting

Read the full transcript and produce a concise summary with these sections:
- **Overview** — 2-3 sentence high-level summary of what the meeting was about
- **Key Discussion Points** — 3-6 bullet points covering the most important topics discussed
- **Action Items** — any follow-ups, next steps, or commitments made (with owners if mentioned)

Guidelines:
- Keep the total summary under 400 words — this is for a Slack message, not a document.
- Focus on outcomes and decisions, not a play-by-play.
- Distinguish between what the customer said vs. what our team said.
- If no clear action items were discussed, omit that section rather than fabricating.

### Step 4: Post to Slack

Post a structured message to the specified Slack channel using the Slack Web API `chat.postMessage` endpoint.

Build the message using Block Kit for rich formatting:

```python
import urllib.request
import json
import os

slack_token = os.environ["GRAINIAC_SLACK_TOKEN"]
slack_channel = os.environ.get("GRAINIAC_SLACK_CHANNEL")
if not slack_channel:
    raise RuntimeError("GRAINIAC_SLACK_CHANNEL environment variable is required")

# Format the date nicely
from datetime import datetime
meeting_date = recording.get("start_datetime", "")
if meeting_date:
    dt = datetime.fromisoformat(meeting_date.replace("Z", "+00:00"))
    date_display = dt.strftime("%B %d, %Y")
else:
    date_display = "Unknown date"

# Build participant strings
external_names = ", ".join(p["name"] for p in external_participants if p.get("name"))
internal_domain = os.environ.get("GRAINIAC_INTERNAL_DOMAIN", "").lower().strip()
internal_names = ", ".join(
    p.get("name", "")
    for p in participants
    if (p.get("scope") == "internal"
        or (internal_domain and p.get("email", "").lower().endswith("@" + internal_domain)))
    and p.get("name")
)

grain_url = recording.get("public_url") or recording.get("url", "")
title = recording.get("title", "Untitled Meeting")

# Build Block Kit blocks
blocks = [
    {
        "type": "header",
        "text": {"type": "plain_text", "text": f"📋 Meeting Summary: {title}", "emoji": True}
    },
    {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*Company:*\n{company or 'Unknown'}"},
            {"type": "mrkdwn", "text": f"*Date:*\n{date_display}"},
            {"type": "mrkdwn", "text": f"*External:*\n{external_names or 'N/A'}"},
            {"type": "mrkdwn", "text": f"*Internal:*\n{internal_names or 'N/A'}"}
        ]
    },
    {"type": "divider"},
    {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "<SUMMARY_TEXT>"}
    },
    {"type": "divider"},
    {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🎥 View Recording", "emoji": True},
                "url": grain_url
            }
        ]
    }
]

# Post to Slack
payload = json.dumps({
    "channel": slack_channel,
    "blocks": blocks,
    "text": f"Meeting Summary: {title}"  # fallback for notifications
}).encode()

req = urllib.request.Request(
    "https://slack.com/api/chat.postMessage",
    data=payload,
    headers={
        "Authorization": f"Bearer {slack_token}",
        "Content-Type": "application/json"
    }
)

with urllib.request.urlopen(req, timeout=15) as resp:
    result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Slack API error: {result.get('error')}")
```

Replace `<SUMMARY_TEXT>` with the formatted summary from Step 3. Format it as Slack mrkdwn:

```
*Overview*
<2-3 sentence summary>

*Key Discussion Points*
• Point one
• Point two
• Point three

*Action Items*
• Action item one (Owner)
• Action item two (Owner)
```

### Step 5: Confirm

After a successful post, report:
- Company name
- Meeting title
- Slack channel posted to
- A note that the summary was posted successfully

## Important Notes

- Slack Block Kit `header` blocks have a 150-character limit for text. If the meeting title is long, truncate it.
- Slack `section` block text has a 3000-character limit. If the summary is long, split it across multiple section blocks.
- The `text` field at the top level of the payload is the notification fallback — always include it.
- If the Slack API returns an error like `channel_not_found` or `not_in_channel`, report the error clearly so the user can fix the channel configuration.
- Use only stdlib (`urllib`, `json`) — no external dependencies.
