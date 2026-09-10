#!/usr/bin/env python3
"""Fetch all external customer meetings from Grain for a given date.

Usage:
    python scripts/fetch_daily_meetings.py 2026-03-20
    python scripts/fetch_daily_meetings.py today

Outputs JSON array of meetings to stdout. Requires GRAINIAC_GRAIN_TOKEN env var.
"""

import sys
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Allow importing from same directory
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from grain_client import list_recordings_page, filter_external_meetings, hydrate_with_participants

# Grain timestamps are UTC. When resolving "today", we need to use the
# business timezone so that an evening Pacific run captures the right day.
# Set via GRAINIAC_TIMEZONE env var. Default: America/Los_Angeles (Pacific).
#
# zoneinfo is stdlib (3.9+) and handles DST correctly, which a fixed UTC offset
# does not — a hardcoded -8 for Pacific is wrong for the ~8 months of PDT and
# shifts the resolved date for runs near midnight.
DEFAULT_TIMEZONE = "America/Los_Angeles"

# Fallback offsets, used only if the system has no IANA tz database.
TZ_OFFSETS = {
    "America/Los_Angeles": -8,
    "America/New_York": -5,
    "America/Chicago": -6,
    "America/Denver": -7,
    "US/Pacific": -8,
    "US/Eastern": -5,
    "UTC": 0,
}

# The recordings list endpoint returns newest-first, so a day's meetings sit at
# the front. Walking the entire history (the old behavior) issued thousands of
# requests and reliably tripped Grain's rate limiter.
MAX_PAGES = 100
PAGE_DELAY_SECONDS = 1
HYDRATE_DELAY_SECONDS = 0.5


def _timezone(tz_name):
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        offset_hours = TZ_OFFSETS.get(tz_name, -8)
        print(
            f"Warning: no tz database entry for {tz_name}; "
            f"falling back to fixed UTC{offset_hours:+d}",
            file=sys.stderr,
        )
        return timezone(timedelta(hours=offset_hours))


def _resolve_today():
    tz_name = os.environ.get("GRAINIAC_TIMEZONE", DEFAULT_TIMEZONE)
    return datetime.now(_timezone(tz_name)).date(), tz_name


def _recordings_on_date(target_str):
    """Page through recordings newest-first, stopping once we pass the target date."""
    matching = []
    cursor = None
    for page in range(MAX_PAGES):
        recs, cursor = list_recordings_page(cursor=cursor, include_participants=True)
        reached_older = False
        for r in recs:
            day = (r.get("start_datetime") or "")[:10]
            if day == target_str:
                matching.append(r)
            elif day and day < target_str:
                reached_older = True
        print(
            f"  page {page + 1}: {len(recs)} recordings, {len(matching)} on {target_str}",
            file=sys.stderr,
        )
        if reached_older or not cursor or not recs:
            break
        time.sleep(PAGE_DELAY_SECONDS)
    return matching


def main():
    if len(sys.argv) < 2:
        print("Usage: python fetch_daily_meetings.py <YYYY-MM-DD | today>", file=sys.stderr)
        sys.exit(1)

    date_arg = sys.argv[1]
    if date_arg == "today":
        target, tz_name = _resolve_today()
        print(f"Resolved 'today' to {target} (timezone: {tz_name})", file=sys.stderr)
    else:
        target = datetime.strptime(date_arg, "%Y-%m-%d").date()

    target_str = str(target)

    # Grain API date params are unreliable, so we page through recordings
    # (newest-first) and filter client-side, stopping as soon as we reach a
    # recording older than the target date.
    print(f"Fetching recordings for {target}...", file=sys.stderr)
    day_recordings = _recordings_on_date(target_str)
    print(f"Found {len(day_recordings)} recordings on {target}", file=sys.stderr)

    # List endpoint doesn't return participants — hydrate each recording individually
    print(f"Hydrating {len(day_recordings)} recordings with participant data...", file=sys.stderr)
    day_recordings = hydrate_with_participants(day_recordings)

    external = filter_external_meetings(day_recordings)
    print(f"Found {len(external)} external customer meetings", file=sys.stderr)

    for m in external:
        print(f"  [{m['company']}] {m['title']}", file=sys.stderr)

    # Output JSON to stdout for piping
    json.dump(external, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
