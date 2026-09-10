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

# Grain rate-limits bursts of requests. Retry 429s (and transient 5xx) with
# exponential backoff rather than aborting the whole run.
MAX_RETRIES = 6
INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 120
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _retry_after_seconds(err, fallback):
    """Honor a Retry-After header when the server sends one."""
    raw = err.headers.get("Retry-After") if err.headers else None
    if not raw:
        return fallback
    try:
        return max(float(raw), fallback)
    except (TypeError, ValueError):
        return fallback


def _request(url, timeout, decode):
    """Issue a GET with retry/backoff on rate limits and transient errors."""
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return decode(resp.read())
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS_CODES or attempt == MAX_RETRIES:
                raise
            delay = _retry_after_seconds(e, backoff)
            print(
                f"  Grain API {e.code}; retrying in {delay:.0f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


def _get(path, timeout=30):
    try:
        return _request(f"{BASE_URL}{path}", timeout, json.loads)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Grain API {e.code} on GET {path}: {e.reason}") from e


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
    try:
        return _request(url, 60, lambda raw: raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Grain API {e.code} fetching transcript for {recording_id}: {e.reason}") from e


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


def list_all_recordings(include_participants=True, after_datetime=None, before_datetime=None, max_pages=100):
    """Paginate through all recordings matching the filters."""
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


def hydrate_with_participants(recordings, delay_seconds=0.5):
    """The list endpoint doesn't return participants reliably.
    Fetch each recording individually to get participant data.

    Requests are spaced out by `delay_seconds` to stay under Grain's rate limit.
    """
    hydrated = []
    for i, r in enumerate(recordings):
        if i and delay_seconds:
            time.sleep(delay_seconds)
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
            print(f"  Warning: failed to hydrate {r['id']}: {e}", file=sys.stderr)
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
