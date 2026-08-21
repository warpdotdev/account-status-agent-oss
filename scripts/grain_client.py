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

# The Grain API rate limits aggressively when paginating. Retry 429s (and
# transient 5xxs) with exponential backoff, honoring Retry-After when present.
MAX_RETRIES = 6
INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 120
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Small pause between paginated requests to stay under the rate limit.
PAGE_DELAY_SECONDS = 1.0


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _retry_after_seconds(err, fallback):
    """Parse the Retry-After header, falling back to the computed backoff."""
    raw = None
    try:
        raw = err.headers.get("Retry-After") if err.headers else None
    except AttributeError:
        raw = None
    if raw:
        try:
            return max(float(raw), 0.0)
        except (TypeError, ValueError):
            pass
    return fallback


def _request_with_retries(req, timeout, describe):
    """Perform a request, retrying rate-limited and transient failures."""
    backoff = INITIAL_BACKOFF_SECONDS
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code not in RETRYABLE_STATUS or attempt == MAX_RETRIES:
                raise RuntimeError(f"Grain API {e.code} on {describe}: {e.reason}") from e
            delay = min(_retry_after_seconds(e, backoff), MAX_BACKOFF_SECONDS)
            print(
                f"  Grain API {e.code} on {describe}; retrying in {delay:.0f}s "
                f"(attempt {attempt}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        except urllib.error.URLError as e:
            last_error = e
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Grain API request failed on {describe}: {e.reason}") from e
            print(
                f"  Grain API network error on {describe}: {e.reason}; "
                f"retrying in {backoff:.0f}s (attempt {attempt}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    raise RuntimeError(f"Grain API retries exhausted on {describe}: {last_error}")


def _get(path, timeout=30):
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers=_headers())
    return json.loads(_request_with_retries(req, timeout, f"GET {path}"))


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
    req = urllib.request.Request(url, headers=_headers())
    raw = _request_with_retries(req, 60, f"transcript fetch for {recording_id}")
    return raw.decode("utf-8")


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


def list_all_recordings(
    include_participants=True,
    after_datetime=None,
    before_datetime=None,
    max_pages=100,
    stop_before_date=None,
):
    """Paginate through all recordings matching the filters.

    Results are returned newest-first. When `stop_before_date` (a `YYYY-MM-DD`
    string) is given, pagination stops as soon as a page contains only
    recordings older than that date. This avoids walking the entire archive
    -- and tripping the API rate limit -- when only one day is needed.
    """
    all_recs = []
    cursor = None
    for page in range(max_pages):
        if page > 0 and PAGE_DELAY_SECONDS:
            time.sleep(PAGE_DELAY_SECONDS)
        recs, cursor = list_recordings_page(
            cursor=cursor,
            include_participants=include_participants,
            after_datetime=after_datetime,
            before_datetime=before_datetime,
        )
        all_recs.extend(recs)
        if not cursor or not recs:
            break
        if stop_before_date and all(
            (r.get("start_datetime") or "")[:10] < stop_before_date for r in recs
        ):
            # Everything from here on is older than the target date.
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
