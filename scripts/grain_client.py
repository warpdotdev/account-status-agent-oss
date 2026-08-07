#!/usr/bin/env python3
"""Grain API client. Requires GRAINIAC_GRAIN_TOKEN environment variable."""

import urllib.request
import urllib.error
import json
import os
import random
import sys
import time
from datetime import datetime

BASE_URL = "https://api.grain.com/_/public-api"

# The Grain API enforces a request budget (x-ratelimit-limit, currently 30 per
# window). Blowing through it returns HTTP 429, so every request retries with
# exponential backoff and we proactively pause when the budget runs low.
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 5
LOW_QUOTA_THRESHOLD = 2
LOW_QUOTA_PAUSE_SECONDS = 20


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _throttle(headers):
    """Pause when the remaining request budget is nearly exhausted."""
    try:
        remaining = int(headers.get("x-ratelimit-remaining", ""))
    except (TypeError, ValueError):
        return
    if remaining <= LOW_QUOTA_THRESHOLD:
        print(
            f"  Grain rate limit nearly exhausted (remaining={remaining}); "
            f"pausing {LOW_QUOTA_PAUSE_SECONDS}s",
            file=sys.stderr,
        )
        time.sleep(LOW_QUOTA_PAUSE_SECONDS)


def _request(url, timeout, decode_json=True):
    """Perform a GET with retry/backoff on rate limiting and transient errors."""
    backoff = INITIAL_BACKOFF_SECONDS
    last_error = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                _throttle(resp.headers)
                return json.loads(payload) if decode_json else payload.decode("utf-8")
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                delay = int(retry_after)
            except (TypeError, ValueError):
                # Jitter keeps the orchestrator's per-meeting agents, which all
                # share one token, from retrying in lockstep and re-colliding.
                delay = backoff + random.uniform(0, 1)
            if attempt == MAX_RETRIES - 1:
                break
            print(
                f"  Grain API {e.code}; retrying in {delay:.1f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)
            backoff *= 2
    raise RuntimeError(
        f"Grain API {last_error.code} on GET {url} after {MAX_RETRIES} attempts: "
        f"{last_error.reason}"
    ) from last_error


def _get(path, timeout=30):
    return _request(f"{BASE_URL}{path}", timeout)


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
    return _request(url, timeout=60, decode_json=False)


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

    The API returns recordings in reverse-chronological order and ignores the
    date parameters server-side, so `stop_before_date` (a `YYYY-MM-DD` string)
    lets callers stop paging once an entire page predates the target day. This
    keeps a single-day fetch to a couple of requests instead of exhausting the
    rate limit walking the full history.
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
            newest_on_page = max(
                (r.get("start_datetime", "")[:10] for r in recs), default=""
            )
            if newest_on_page and newest_on_page < stop_before_date:
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
