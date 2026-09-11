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
# zoneinfo is stdlib (3.9+) and handles DST correctly. A fixed-offset table
# would silently pick the wrong calendar date for late-evening runs during
# daylight saving time.
DEFAULT_TIMEZONE = "America/Los_Angeles"

# Fallback offsets, used only if the system lacks a tz database.
TZ_OFFSETS = {
    "America/Los_Angeles": -8,
    "America/New_York": -5,
    "America/Chicago": -6,
    "America/Denver": -7,
    "US/Pacific": -8,
    "US/Eastern": -5,
    "UTC": 0,
}


def _resolve_today():
    tz_name = os.environ.get("GRAINIAC_TIMEZONE", DEFAULT_TIMEZONE)
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:
        offset_hours = TZ_OFFSETS.get(tz_name, -8)
        tz = timezone(timedelta(hours=offset_hours))
        print(
            f"Warning: no tz database entry for {tz_name}; "
            f"falling back to fixed UTC{offset_hours:+d} offset",
            file=sys.stderr,
        )
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

    # The Grain API accepts afterDatetime/beforeDatetime but ignores them, so we
    # fetch recent recordings and filter client-side by date. Results come back
    # newest-first, so `stop_before_date` lets us stop paginating as soon as we
    # pass the target day instead of walking the whole history (which blows the
    # API's small rate-limit budget).
    print(f"Fetching recordings for {target}...", file=sys.stderr)
    recordings = list_all_recordings(include_participants=True, stop_before_date=target_str)
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
