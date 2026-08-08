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
from datetime import datetime, timedelta, timezone

# Allow importing from same directory
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from grain_client import list_all_recordings, filter_external_meetings, hydrate_with_participants

# Grain timestamps are UTC. When resolving "today", we need to use the
# business timezone so that an evening Pacific run captures the right day.
# Set via GRAINIAC_TIMEZONE env var. Default: America/Los_Angeles (Pacific).
#
# We use a simple UTC offset lookup instead of pytz to stay stdlib-only.
# This handles PST/PDT well enough for daily batch runs.
TZ_OFFSETS = {
    "America/Los_Angeles": -8,  # PST (close enough; PDT is -7)
    "America/New_York": -5,
    "America/Chicago": -6,
    "America/Denver": -7,
    "US/Pacific": -8,
    "US/Eastern": -5,
    "UTC": 0,
}


def _resolve_today():
    tz_name = os.environ.get("GRAINIAC_TIMEZONE", "America/Los_Angeles")
    offset_hours = TZ_OFFSETS.get(tz_name, -8)
    tz = timezone(timedelta(hours=offset_hours))
    return datetime.now(tz).date(), tz_name


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

    # Grain API date params are unreliable, so we fetch recent recordings and
    # filter client-side by date. Results come back newest-first, so we stop
    # paginating once we've walked past the target day — the public API is rate
    # limited (~30 requests/window) and paging full history will 429.
    print(f"Fetching recordings for {target}...", file=sys.stderr)
    recordings = list_all_recordings(
        include_participants=True,
        stop_before_date=target_str,
    )
    print(f"Fetched {len(recordings)} total recordings", file=sys.stderr)

    # Filter to target date
    day_recordings = [
        r for r in recordings
        if r.get("start_datetime", "")[:10] == target_str
    ]
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
