---
name: grainiac-orchestrator
description: >
  Orchestrate daily processing of customer meeting recordings from Grain into Notion.
  Use this skill when tasked with processing a day's meetings, syncing Grain recordings
  to the Account Tracking Notion database, or running the daily customer intel pipeline.
  This skill fetches all external meetings for a given date and processes each one,
  analyzing the transcript and updating that company's Notion page.
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

If the output array is empty (no external meetings matching the criteria), report that there are no meetings to process and stop.

If several recordings belong to the same conversation or the same company, process them **together** against a single Notion page rather than independently. Two writers racing on one page produce interleaved, duplicated content. Back-to-back recordings of one meeting are common: read all of their transcripts first, then make one coherent set of edits and add each recording to the Meeting Log.

### 3. Process the meetings

> **IMPORTANT — read this before trying to fan out to child agents.**
>
> Warp injects team secrets into a cloud run via that run's *agent config*
> (`agent_config.secrets`). A run started by a schedule that has the
> `GRAINIAC_*` secrets attached receives them; **child runs spawned from it do
> not.** They are created with `agent_config.secrets: []` and there is no way to
> attach secrets at spawn time — `oz agent run-cloud` has no `--secret` flag, its
> `-f` config file accepts only `name`, `environment_id`, `runner_id`,
> `model_id`, `base_prompt`, `mcp_servers`, `host` and `computer_use_enabled`,
> `oz schedule create` has no secret flag either, and the REST
> `AmbientAgentConfig` has no secrets field.
>
> The practical consequence: **children fail immediately** with
> `GRAINIAC_GRAIN_TOKEN environment variable is required`. Never work around
> this by passing token values in a child's prompt or in a message — that puts
> live credentials into conversation logs.

**Default: process the meetings inline, in this run.** Read the
`grainiac-meeting-processor` skill and follow its procedure once per meeting.
This run already has the credentials, so it always works. It is serial and
therefore slower, so if the day is busy, prioritise the highest-value meetings
(POC kickoffs and active-account working sessions) first and report anything you
did not get to.

**Only fan out to child agents if the secrets problem has actually been solved**
— that is, if a named agent or factory config exists that has the `GRAINIAC_*`
secrets attached and you can dispatch children as that agent. If you attempt it,
verify before dispatching the full batch:

```sh
# Confirm a spawned child would actually receive the secrets.
oz run get <CHILD_RUN_ID> --output-format json | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['agent_config'].get('secrets', []))"
```

An empty list means the child cannot authenticate; fall back to inline
processing. To discover the environment ID for a child run:

```sh
oz environment list --output-format json | python3 -c "import sys,json; envs=json.load(sys.stdin); print(next(e['id'] for e in envs if e['name']=='grainiac'))"
```

### 4. Report

Report a summary covering:
- Number of meetings found, and how many were processed
- For each: company name, meeting title, whether the Notion page was created or updated, and the page URL
- Any company names you corrected, and why
- Anything you could not process, and why

If you did spawn child agents, also list their run IDs; check status in the Oz
web app at https://oz.warp.dev/runs.

## Notes

- **Timezone handling:** Grain timestamps are UTC. When the script resolves `today`, it uses the `GRAINIAC_TIMEZONE` env var (default: `America/Los_Angeles`) to determine the correct calendar date. This matters for evening Pacific runs where UTC has already rolled to the next day. You can override with `GRAINIAC_TIMEZONE=UTC` or any supported timezone. When a specific `YYYY-MM-DD` date is provided, the script matches recordings whose UTC `start_datetime` falls on that calendar date.
- The Grain API's `title_search` parameter does not filter server-side. Filtering happens client-side in `grain_client.py`.
- **Rate limiting:** the Grain API returns HTTP 429 readily. `grain_client.py` paces every request and retries on 429 with backoff, honouring `Retry-After`. `fetch_daily_meetings.py` bounds its listing to a window around the target date instead of walking the whole recording history — do not remove that bound, it is what keeps the run under the limit.
- **Empty transcripts:** Grain returns an empty body for recordings that were started and abandoned. Treat these as false starts — note them and move on. Never fabricate content for a recording with no transcript.
- **Company names must be corrected before writing.** The `company` field is only the capitalised first label of the most common external email domain, so it is frequently wrong in two distinct ways, and both create junk pages:
  - *Consumer email domains* — "Gmail", "Yahoo", "Me" (iCloud), "Hotmail", "Outlook". Never create a page named after an email provider. Read the transcript to find the person's real employer. If they are genuinely independent, title the page `<Full Name> (individual - company unknown)`, matching the existing convention in the database.
  - *Domain slugs* — "Thebrowser" for The Browser Company, "Endurancedirect" for Endurance, "Joinstatus" for Statusphere, "Linqapp" for Linq, "Kickdrumtech" for Kickdrum. Always search the database for an existing correctly-named page before creating one.
- **Check for an existing page under the corrected name, not the inferred one.** Past runs created duplicate pages by skipping this, splitting account history across two or three records. If you find duplicates, do not delete anything — write to the canonical page and flag the duplicates for a human to merge.
- **Transcript speaker labels are not reliable.** Grain's diarization sometimes swaps speakers, including attributing vendor statements to the customer. Attribute by role and context, and note the caveat on the page when labels are clearly unreliable.
