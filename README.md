# Grainiac

Automated pipeline that syncs customer meeting recordings from [Grain](https://grain.com) into a structured [Notion](https://notion.so) Account Tracking database. Built to run as [Oz](https://docs.warp.dev) cloud agents.

## What It Does

After each day of customer meetings, Grainiac:

1. Pulls all external meeting recordings from Grain for a given date
2. Identifies the customer company from participant email domains
3. Fetches the full transcript for each meeting
4. Analyzes the transcript against a structured enterprise customer template
5. Creates or updates the company's page in the Notion Account Tracking database

The result is a living record per customer that tracks stakeholders, tech stack, requirements, use cases, deal status, follow-ups, and more — all extracted directly from meeting transcripts.

## How to Deploy

### With Warp (recommended)

Copy and paste this prompt into [Warp](https://www.warp.dev) and follow along:

```text
Set up Grainiac: clone https://github.com/warpdotdev/account-status-agent-oss.git if it isn't already cloned, then read the grainiac-setup skill in the repo and follow its instructions.
```

The [setup skill](.agents/skills/grainiac-setup/SKILL.md) walks through everything in the manual steps below interactively: cloning, Oz CLI auth, Notion configuration, secrets, environment creation, a test run, and an optional daily schedule.

### Manual setup

The same steps, as a human-readable reference:

1. **Set up Notion.**
   - Create a database in Notion (or use an existing one) for account tracking.
   - The title property name is detected automatically from the database schema, so no renaming is required. Every database has exactly one title property, whatever it is called (`Name`, `Company`, `Account name`, ...). Set `GRAINIAC_NOTION_TITLE_PROPERTY` if you want to pin it explicitly; if it disagrees with the database, the actual property wins and a warning is printed.
   - Create an internal integration at https://www.notion.so/my-integrations and copy its token into `GRAINIAC_NOTION_TOKEN`.
   - Share the database with the integration (database `⋯` menu → *Connections* → add your integration). Without this, the Notion API returns 404s.
   - Copy the database ID from the URL — `notion.so/<workspace>/<DATABASE_ID>?v=...` — into `GRAINIAC_NOTION_DATABASE_ID`.

   No other database properties are required; everything else lives in the page body, which follows the [Notion template](.agents/skills/grainiac-meeting-processor/references/notion-template.md).

2. **Install and authenticate the Oz CLI.** The CLI ships with the [Warp app](https://docs.warp.dev/getting-started/installation-and-setup); otherwise see [Installing the CLI](https://docs.warp.dev/reference/cli). Then sign in:
   ```sh
   oz login
   ```
   For CI or headless environments, export an API key instead: `export WARP_API_KEY="wk-..."`.

3. **Add secrets.** Warp injects team secrets into cloud runs as environment variables. Each command prompts for the value securely:
   ```sh
   oz secret create --team GRAINIAC_GRAIN_TOKEN
   oz secret create --team GRAINIAC_NOTION_TOKEN
   oz secret create --team GRAINIAC_NOTION_DATABASE_ID
   oz secret create --team GRAINIAC_INTERNAL_DOMAIN
   ```
   Add `GRAINIAC_SLACK_TOKEN` and `GRAINIAC_SLACK_CHANNEL` as well if you use the Slack summary skill.

4. **Create an Oz environment named `grainiac`** with this repo checked out. The orchestrator discovers the environment by this exact name, so it must match:
   ```sh
   oz environment create --team \
     --name grainiac \
     --repo <owner/repo> \
     --docker-image warpdotdev/dev-base:latest
   ```
   The scripts only need the Python standard library, so the base image is enough. Copy the environment ID from the output (or `oz environment list`) for the next steps.

5. **Run manually** (replace `<ENV_ID>` with your `grainiac` environment ID):
   ```sh
   oz agent run-cloud \
     --environment <ENV_ID> \
     --prompt "Read the grainiac-orchestrator skill for instructions. Process today's meetings."
   ```

6. **Schedule a daily run:**
   ```sh
   oz schedule create \
     --name "Grainiac Daily" \
     --cron "0 5 * * *" \
     --environment <ENV_ID> \
     --prompt "Read the grainiac-orchestrator skill for instructions. Process today's meetings."
   ```
   This runs at 05:00 UTC (roughly 9–10pm Pacific depending on DST).

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GRAINIAC_GRAIN_TOKEN` | Yes | Grain personal access token |
| `GRAINIAC_NOTION_TOKEN` | Yes | Notion integration token |
| `GRAINIAC_NOTION_DATABASE_ID` | Yes | Notion database ID for the Account Tracking database |
| `GRAINIAC_NOTION_TITLE_PROPERTY` | No | Name of the database's title property that holds the company name. Auto-detected from the database schema; set only to pin it explicitly |
| `GRAINIAC_SLACK_TOKEN` | Slack skill only | Slack Bot OAuth token (xoxb-...) |
| `GRAINIAC_SLACK_CHANNEL` | Slack skill only | Slack channel to post summaries to (e.g. `#meeting-summaries`) |
| `GRAINIAC_INTERNAL_DOMAIN` | Recommended | Your company's email domain for filtering internal participants (e.g. `yourcompany.com`) |
| `GRAINIAC_TIMEZONE` | No | IANA timezone for resolving "today", DST-aware (defaults to `America/Los_Angeles`) |
| `GRAINIAC_GRAIN_MIN_INTERVAL` | No | Minimum seconds between Grain API requests (default: `2.5`) |
| `GRAINIAC_GRAIN_MAX_RETRIES` | No | Attempts per Grain request before giving up on rate limiting (default: `6`) |

## Repo Structure

```
grainiac/
├── .agents/
│   └── skills/
│       ├── grainiac-setup/
│       │   └── SKILL.md               # Oz skill: guided end-to-end deployment
│       ├── grainiac-orchestrator/
│       │   └── SKILL.md               # Oz skill for the main orchestrator agent
│       ├── grainiac-meeting-processor/
│       │   ├── SKILL.md               # Oz skill for per-meeting child agents
│       │   └── references/
│       │       └── notion-template.md # Notion page template (single source of truth)
│       └── grainiac-slack-summary/
│           └── SKILL.md               # Oz skill: summarize a recording to Slack
├── scripts/
│   ├── grain_client.py                # Grain API client (list, get, transcript, hydrate)
│   ├── notion_client.py               # Notion API client (CRUD, block builders, section finders)
│   └── fetch_daily_meetings.py        # CLI: fetch external meetings for a date
├── .env.example                       # Sample environment variables for local testing
├── LICENSE
├── README.md
└── SECURITY.md
```

## How It Works

### Skill architecture

**Orchestrator agent** (`.agents/skills/grainiac-orchestrator/`) — the main agent that runs once per batch:
- Runs `scripts/fetch_daily_meetings.py` to get the day's external meetings
- For a single meeting, processes it inline
- For multiple meetings, spawns one child cloud agent per meeting via `oz agent run-cloud`

**Meeting processor agent** (`.agents/skills/grainiac-meeting-processor/`) — one per meeting:
- Fetches the full transcript from Grain
- Reads and analyzes the entire transcript (not just the AI summary)
- Checks if the company already has a Notion page
- Creates a new page or surgically updates the existing one

**Slack summary skill** (`.agents/skills/grainiac-slack-summary/`) — optional, one recording at a time:
- Fetches a recording and its transcript from Grain
- Generates a concise meeting summary
- Posts a formatted summary to a Slack channel

### Input modes

The orchestrator supports three modes based on the prompt:

| Prompt | Behavior |
|--------|----------|
| *(no parameters)* | Process today's meetings (Pacific time default) |
| "Process meetings from 2026-03-10" | Process all external meetings on that date |
| "Process Acme Corp meetings from 2026-03-10" | Process only that company's meetings on that date |

## Scripts

All scripts use the Python 3.9+ standard library only — no pip dependencies required. (3.9 is the floor for `zoneinfo`, used for DST-correct date resolution.)

For local testing, copy `.env.example` to `.env`, fill in your values, and export them before running (e.g. `set -a; source .env; set +a`). The scripts read configuration directly from environment variables.

### Grain API rate limits

The Grain API allows roughly 30 requests per rate-limit window, and a daily batch fans out one child agent per meeting that all call the API concurrently. `grain_client.py` therefore paces requests (`GRAINIAC_GRAIN_MIN_INTERVAL`) and retries on HTTP 429 with exponential backoff, honoring the `retry-after` header.

`list_all_recordings()` also accepts `stop_before_date="YYYY-MM-DD"`. Recordings are returned newest-first, so this stops pagination once a page predates the target day. Callers that only need a single day should always pass it — walking the full history costs one request per 20 recordings and will exhaust the rate limit.

### `scripts/fetch_daily_meetings.py`

```sh
python scripts/fetch_daily_meetings.py <YYYY-MM-DD | today>
```

Outputs a JSON array of external customer meetings to stdout. Stderr shows progress.

### `scripts/grain_client.py`

Importable Grain API client:
- `get_recording(id)` — fetch recording metadata and participants
- `get_transcript_text(id)` — fetch full plain-text transcript
- `list_all_recordings(stop_before_date=)` — paginate through recordings, stopping early once past a target date
- `hydrate_with_participants(recordings)` — enrich list results with per-recording participant data
- `filter_external_meetings(recordings)` — filter to external-only, identify company names

### `scripts/notion_client.py`

Importable Notion API client:
- `find_company_page(name)` — check if a company page exists
- `resolve_title_property()` — detect the database's actual title property name
- `create_page(name, blocks)` — create a new page in the database
- `get_all_blocks(page_id)` — get all blocks from a page
- `append_blocks(parent_id, blocks, after=)` — insert blocks at a specific position
- `update_block(block_id, data)` — update a block's content
- `find_heading_block()`, `get_last_block_in_section()` — locate insertion points
- Block builders: `heading1()`, `heading2()`, `bullet()`, `todo()`, `paragraph()`, `divider()`, `rich()`

## Notion Template

The template that defines the structure of each customer's Notion page lives at `.agents/skills/grainiac-meeting-processor/references/notion-template.md`. This is the single source of truth for what information is tracked per customer. Key sections:

- Follow-Up Items (checkbox list)
- Key Activities & Decisions (reverse-chronological timeline)
- Meeting Log (hyperlinked Grain recordings)
- Company Overview, People & Org
- Tech Stack & Environment
- Current Tooling & Competitive Landscape
- Requirements (deployment, inference/LLM, security, governance, integrations, blockers)
- Use Cases (primary and secondary workflows)
- Commercial (pricing, procurement/legal)
- POC / Pilot Tracker
- Communication preferences
- Sentiment & Relationship Notes
