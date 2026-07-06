---
name: grainiac-orchestrator
description: >
  Orchestrate daily processing of customer meeting recordings from Grain into Notion.
  Use this skill when tasked with processing a day's meetings, syncing Grain recordings
  to the Account Tracking Notion database, or running the daily customer intel pipeline.
  This skill fetches all external meetings for a given date and spawns one child cloud
  agent per meeting to analyze the transcript and update the Notion page.
---

# Grainiac Orchestrator

## Environment Requirements

These environment variables must be set:
- `GRAINIAC_GRAIN_TOKEN` — Grain personal access token
- `GRAINIAC_NOTION_TOKEN` — Notion integration token
- `GRAINIAC_NOTION_DATABASE_ID` — ID of the Account Tracking database (**required**)

## Input

The user's prompt may specify:
- **No parameters** → process today's meetings (default)
- **A date** (e.g., "process meetings from 2026-03-10") → process that date's meetings
- **A date and company** (e.g., "process Acme Corp meetings from 2026-03-10") → process only that company's meetings from that date

Determine the target date and optional company filter from the prompt. If no date is given, use `today`.

## Procedure

### 1. Fetch the day's meetings

Run the fetch script with the target date:

```sh
python scripts/fetch_daily_meetings.py <YYYY-MM-DD | today>
```

This outputs a JSON array to stdout. Each entry contains:
- `recording_id`, `title`, `date`, `company`, `grain_url`, `participants`, `summary`

Capture the JSON output and parse it.

If a company filter was specified, filter the results to only meetings where the `company` field matches (case-insensitive).

### 2. Check for meetings

If the output array is empty (no external meetings matching the criteria), report that there are no meetings to process and stop. Do not spawn any child agents.

If meetings exist, every meeting gets its own child agent — even if multiple meetings are with the same company.

### 3. Process or spawn

If there is only **one meeting** to process, handle it inline instead of spawning a child agent — read the `grainiac-meeting-processor` skill and follow its procedure directly. This avoids the overhead of spinning up a cloud agent for a single meeting.

If there are **multiple meetings**, spawn a child cloud agent for each one.

First, discover the environment ID by looking up the "grainiac" environment:

```sh
oz environment list --output-format json | python3 -c "import sys,json; envs=json.load(sys.stdin); print(next(e['id'] for e in envs if e['name']=='grainiac'))"
```

Then spawn each child agent using that environment ID:

```sh
oz agent run-cloud \
  --prompt 'Read the grainiac-meeting-processor skill in this repo for instructions. Process this meeting:
RECORDING_ID: <recording_id>
COMPANY: <company_name>
GRAIN_URL: <grain_url>
MEETING_TITLE: <title>
MEETING_DATE: <date>' \
  --environment <ENV_ID>
```

### 4. Monitor

After spawning all child agents, list their run IDs and report a summary:
- Number of meetings found
- Number of child agents spawned
- Company names and meeting titles for each

Check run status in the Oz web app at https://oz.warp.dev/runs (each `oz agent run-cloud` invocation also prints its run ID).

## Notes

- **Timezone handling:** Grain timestamps are UTC. When the script resolves `today`, it uses the `GRAINIAC_TIMEZONE` env var (default: `America/Los_Angeles`) to determine the correct calendar date. This matters for evening Pacific runs where UTC has already rolled to the next day. You can override with `GRAINIAC_TIMEZONE=UTC` or any supported timezone. When a specific `YYYY-MM-DD` date is provided, the script matches recordings whose UTC `start_datetime` falls on that calendar date.
- The Grain API's `title_search` parameter does not filter server-side. Filtering happens client-side in `grain_client.py`.
- Company names are inferred from external participant email domains. If the inferred name is wrong (e.g., "Gmail" for a personal email), the child agent should correct it during analysis.
- The Grain API paginates at 20 recordings per page. For busy days, the fetch script handles up to 100 pages automatically.
