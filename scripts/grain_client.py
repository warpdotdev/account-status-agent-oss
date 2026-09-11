#!/usr/bin/env python3
"""Grain API client. Requires GRAINIAC_GRAIN_TOKEN environment variable."""

import urllib.request
import urllib.error
import json
import os
import sys
import time
from datetime import datetime

BASE_URL = "https://api.grain.com/_/public-api"

# The Grain public API enforces a small request budget (observed: 27 requests
# per rolling window, reported via the x-ratelimit-* response headers). A single
# daily run can easily exceed it because every recording needs its own
# hydration call, so we pace requests proactively and retry on 429 rather than
# crashing the whole run.
RATE_LIMIT_MAX_RETRIES = 6
RATE_LIMIT_BASE_BACKOFF = 5  # seconds; doubled on each retry
RATE_LIMIT_LOW_WATERMARK = 2  # pause when this few requests remain in the window
RATE_LIMIT_COOLDOWN = 30  # seconds to wait once the budget is nearly exhausted


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _throttle(headers):
    """Sleep if the remaining rate-limit budget is nearly exhausted."""
    raw = headers.get("x-ratelimit-remaining")
    if raw is None:
        return
    try:
        remaining = int(raw)
    except (TypeError, ValueError):
        return
    if remaining <= RATE_LIMIT_LOW_WATERMARK:
        print(
            f"  Rate limit nearly exhausted ({remaining} left); "
            f"cooling down {RATE_LIMIT_COOLDOWN}s...",
            file=sys.stderr,
        )
        time.sleep(RATE_LIMIT_COOLDOWN)


def _request(url, timeout, parse_json=True):
    """Perform a GET with retry/backoff on rate limiting and transient errors."""
    backoff = RATE_LIMIT_BASE_BACKOFF
    last_error = None

    for attempt in range(RATE_LIMIT_MAX_RETRIES):
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                _throttle(resp.headers)
                return json.loads(body) if parse_json else body.decode("utf-8")
        except urllib.error.HTTPError as e:
            last_error = e
            # 429 = rate limited, 5xx = transient server error. Both are worth retrying.
            if e.code != 429 and e.code < 500:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                delay = int(retry_after) if retry_after else backoff
            except (TypeError, ValueError):
                delay = backoff
            if attempt < RATE_LIMIT_MAX_RETRIES - 1:
                print(
                    f"  Grain API {e.code}; retrying in {delay}s "
                    f"(attempt {attempt + 1}/{RATE_LIMIT_MAX_RETRIES})...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                backoff = min(backoff * 2, 120)

    raise RuntimeError(
        f"Grain API {last_error.code} on GET {url} after "
        f"{RATE_LIMIT_MAX_RETRIES} attempts: {last_error.reason}"
    ) from last_error


def _get(path, timeout=30):
    return _request(f"{BASE_URL}{path}", timeout=timeout, parse_json=True)


def get_recording(recording_id, include_participants=True, include_ai_summary=False):
    """Fetch a single recording by ID."""
    params = []
    if include_participants:
        params.append("includeParticipants=true")
    if include_ai_summary:
        params.append("includeAiSummary=true")
    qs = f"?{'&'.join(params)}" if params else ""
    return _get(f"/recordings/{recording_id}{qs}")


def get_transcript_text(recording_id):
    """Fetch the full plain-text transcript for a recording."""
    url = f"{BASE_URL}/recordings/{recording_id}/transcript.txt"
    return _request(url, timeout=60, parse_json=False)


def list_recordings_page(cursor=None, include_participants=True, after_datetime=None, before_datetime=None):
    """Fetch one page of recordings. Returns (recordings_list, next_cursor)."""
    params = []
    if include_participants:
        params.append("includeParticipants=true")
    if after_datetime:
        params.append(f"afterDatetime={after_datetime}")
    if before_datetime:
        params.append(f"beforeDatetime={before_datetime}")
    if cursor:
        params.append(f"cursor={cursor}")
    qs = f"?{'&'.join(params)}" if params else ""
    data = _get(f"/recordings{qs}")
    return data.get("recordings", []), data.get("cursor")


def list_all_recordings(include_participants=True, after_datetime=None, before_datetime=None,
                        max_pages=100, stop_before_date=None):
    """Paginate through all recordings matching the filters.

    NOTE: the API's afterDatetime/beforeDatetime params are accepted but not
    actually applied server-side, so callers must still filter client-side.
    Recordings are returned in reverse-chronological order, which lets us stop
    paginating early.

    `stop_before_date` is a 'YYYY-MM-DD' string. Once a page contains only
    recordings that start before that date, pagination stops. This keeps a
    daily run to a couple of requests instead of walking the entire history
    and burning the rate-limit budget.
    """
    all_recs = []
    cursor = None
    for _ in range(max_pages):
        recs, cursor = list_recordings_page(
            cursor=cursor,
            include_participants=include_participants,
            after_datetime=after_datetime,
            before_datetime=before_datetime,
        )
        all_recs.extend(recs)
        if not cursor or not recs:
            break
        if stop_before_date:
            oldest = min(
                (r.get("start_datetime", "") or "")[:10] for r in recs
            )
            # Results are newest-first, so once the oldest entry on this page
            # predates the target we've already seen everything we need.
            if oldest and oldest < stop_before_date:
                break
    return all_recs


def identify_company_from_participants(participants):
    """Determine the external company from participant email domains.
    Returns (company_name, external_participants) or (None, []) if no external found.
    """
    internal_domain = os.environ.get("GRAINIAC_INTERNAL_DOMAIN", "").lower().strip()
    internal_domains = {internal_domain} if internal_domain else set()
    external = []
    domains = {}

    for p in participants:
        email = p.get("email") or ""
        scope = p.get("scope", "")
        if not email:
            continue
        domain = email.split("@")[-1].lower()
        if domain in internal_domains or scope == "internal":
            continue
        external.append(p)
        domains[domain] = domains.get(domain, 0) + 1

    if not domains:
        return None, []

    # Return the most common external domain, strip TLD for company name
    top_domain = max(domains, key=domains.get)
    company = top_domain.split(".")[0].capitalize()
    return company, external


def hydrate_with_participants(recordings):
    """The list endpoint doesn't return participants reliably.
    Fetch each recording individually to get participant data.
    """
    hydrated = []
    for r in recordings:
        try:
            full = get_recording(r["id"], include_participants=True)
            # Merge participant data into the original record
            r["participants"] = full.get("participants", [])
            # Also grab public_url and summary if missing
            if not r.get("public_url"):
                r["public_url"] = full.get("public_url") or full.get("url", "")
            if not r.get("summary"):
                r["summary"] = full.get("summary", "")
        except Exception as e:
            print(f"  Warning: failed to hydrate {r['id']}: {e}", file=__import__('sys').stderr)
            r["participants"] = []
        hydrated.append(r)
    return hydrated


def filter_external_meetings(recordings):
    """Filter recordings to only those with external participants. Returns enriched list."""
    results = []
    for r in recordings:
        participants = r.get("participants", [])
        company, externals = identify_company_from_participants(participants)
        if company:
            results.append({
                "recording_id": r["id"],
                "title": r.get("title", ""),
                "date": r.get("start_datetime", "")[:10],
                "start_datetime": r.get("start_datetime", ""),
                "company": company,
                "grain_url": r.get("public_url") or r.get("url", ""),
                "summary": r.get("summary", ""),
                "participants": [
                    {"name": p.get("name", ""), "email": p.get("email", ""), "scope": p.get("scope", "")}
                    for p in participants
                ],
                "external_participants": [
                    {"name": p.get("name", ""), "email": p.get("email", "")}
                    for p in externals
                ],
            })
    return results
