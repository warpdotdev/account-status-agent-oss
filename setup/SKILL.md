---
name: grainiac-setup
description: >
  Set up a working Grainiac deployment from scratch. Use this skill when asked to
  set up, install, deploy, or onboard Grainiac. It covers cloning the repo (or using
  an existing clone), verifying the Oz CLI, walking the user through Grain and Notion
  prerequisites, creating team secrets, creating the `grainiac` Oz environment, running
  a test agent, and optionally scheduling the daily run.
---

# Grainiac Setup

Goal: a working Grainiac deployment — an Oz environment named `grainiac` with this
repo checked out, all secrets configured, and (optionally) a daily scheduled run.

Work through the steps in order. Ask the user for input where noted; never ask the
user to paste secret values into the chat.

## 1. Get the repo

Check whether you are already inside a Grainiac checkout (look for
`.agents/skills/grainiac-orchestrator/SKILL.md` relative to the repo root). If so,
use it and skip cloning.

Otherwise clone and enter it:

```sh
git clone https://github.com/warpdotdev/account-status-agent-oss.git
cd account-status-agent-oss
```

If the user has a fork, clone that instead — the fork's `owner/repo` is used again in
step 5.

## 2. Verify the Oz CLI

Run:

```sh
oz environment list
```

- **Command not found** → the CLI ships with the [Warp app](https://docs.warp.dev/getting-started/installation-and-setup); otherwise see [Installing the CLI](https://docs.warp.dev/reference/cli).
- **Not authenticated** → run `oz login` (interactive), or for CI/headless environments have the user export `WARP_API_KEY`.
- **Succeeds** → note whether an environment named `grainiac` already exists (used in step 5).

## 3. Gather prerequisites

Confirm with the user that they have (or help them create):

1. **Grain personal access token** — from their Grain workspace settings.
2. **Notion database** for account tracking (new or existing).
3. **Notion integration** — created at https://www.notion.so/my-integrations; they need its token.
4. **Database shared with the integration** — database `⋯` menu → *Connections* → add the integration. Without this the Notion API returns 404s.
5. **Database ID** — from the database URL: `notion.so/<workspace>/<DATABASE_ID>?v=...`.
6. **Title property name** — every Notion database has exactly one title property. Grainiac assumes it is named `Company`; new Notion databases default to `Name`. Ask the user which name theirs uses: either they rename it to `Company`, or set `GRAINIAC_NOTION_TITLE_PROPERTY` in step 4.
7. **Internal email domain** (recommended) — e.g. `yourcompany.com`, used to separate internal vs. external meeting participants.
8. **Slack bot token and channel** (optional) — only if they want the `grainiac-slack-summary` skill.

No database properties are required beyond the title; page content follows
`.agents/skills/grainiac-meeting-processor/references/notion-template.md`.

## 4. Create secrets

Warp injects team secrets into cloud runs as environment variables. Check what
already exists:

```sh
oz secret list --output-format json
```

For each missing variable, run the create command and let the user type the value at
the CLI's secure prompt — do not echo values or accept them via chat:

```sh
oz secret create --team GRAINIAC_GRAIN_TOKEN
oz secret create --team GRAINIAC_NOTION_TOKEN
oz secret create --team GRAINIAC_NOTION_DATABASE_ID
oz secret create --team GRAINIAC_INTERNAL_DOMAIN   # recommended
```

Conditionally:

```sh
oz secret create --team GRAINIAC_NOTION_TITLE_PROPERTY  # only if the title property is not "Company"
oz secret create --team GRAINIAC_TIMEZONE               # only to override the America/Los_Angeles default
oz secret create --team GRAINIAC_SLACK_TOKEN            # Slack summary skill only
oz secret create --team GRAINIAC_SLACK_CHANNEL          # Slack summary skill only
```

Some of these (database ID, domain, channel) are not sensitive, but they are stored
as secrets because that is how values get injected into cloud runs.

## 5. Create the `grainiac` environment

The orchestrator discovers the environment by the exact name `grainiac`, so the name
must match.

If step 2 showed an existing `grainiac` environment, reuse it and capture its ID:

```sh
oz environment list --output-format json
```

Otherwise, determine the repo slug from the clone (accounts for forks). Extract
`owner/repo` from the remote URL (e.g. `https://github.com/<owner>/<repo>.git`):

```sh
git remote get-url origin
```

Then create the environment. The scripts only need Python 3.8+ stdlib, so the base
image is sufficient:

```sh
oz environment create --team \
  --name grainiac \
  --repo <owner/repo> \
  --docker-image warpdotdev/dev-base:latest
```

Capture the environment ID from the output for the next steps.

## 6. Test run

Offer to kick off a manual run to verify everything works end to end:

```sh
oz agent run-cloud \
  --environment <ENV_ID> \
  --prompt "Read the grainiac-orchestrator skill for instructions. Process today's meetings."
```

Monitor with `oz run get <run-id>` or at https://oz.warp.dev/runs. A day with no
external meetings is still a successful test — the agent should report "no meetings
to process".

## 7. Schedule the daily run (optional)

Ask whether the user wants a daily schedule and what local time they prefer, then
convert to a UTC cron expression. The default below is 05:00 UTC (roughly 9–10pm
Pacific depending on DST):

```sh
oz schedule create \
  --name "Grainiac Daily" \
  --cron "0 5 * * *" \
  --environment <ENV_ID> \
  --prompt "Read the grainiac-orchestrator skill for instructions. Process today's meetings."
```

Verify with `oz schedule list`.

## Troubleshooting

- **Notion API returns 404** → the database was not shared with the integration (step 3.4).
- **Company pages not created / title errors** → the database's title property name does not match; set `GRAINIAC_NOTION_TITLE_PROPERTY` (step 3.6).
- **"Today" resolves to the wrong date** → set `GRAINIAC_TIMEZONE` (default `America/Los_Angeles`).
- **Local script testing** → copy `.env.example` to `.env`, fill it in, and export with `set -a; source .env; set +a`.
