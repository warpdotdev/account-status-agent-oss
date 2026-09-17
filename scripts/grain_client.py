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

# The Grain API rate limits at ~21-30 requests/minute (see the x-ratelimit-limit
# response header). Space requests out and retry on 429/5xx with backoff so a
# busy day's pagination doesn't blow up mid-run.
#
# That budget is per token, not per process, so a daily run that fans out one
# child agent per meeting shares it across every worker. A 429 there means the
# whole fleet is over budget, so retries have to wait out a real slice of the
# limit window and be jittered -- otherwise the workers retry in lockstep and
# keep colliding.
MIN_REQUEST_INTERVAL = float(os.environ.get("GRAINIAC_MIN_REQUEST_INTERVAL", "2.1"))
MAX_RETRIES = int(os.environ.get("GRAINIAC_MAX_RETRIES", "8"))
RATE_LIMIT_WINDOW = 60.0

_last_request_at = 0.0


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _throttle():
    """Sleep as needed so consecutive requests stay under the rate limit."""
    global _last_request_at
    wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _backoff_seconds(err, attempt):
    """How long to wait before retrying, always with jitter.

    Jitter matters because several agents may be hitting the same token at
    once; without it they all wake up together and re-collide.
    """
    retry_after = err.headers.get("Retry-After") if err.headers else None
    try:
        return float(retry_after) + random.uniform(0, 5)
    except (TypeError, ValueError):
        pass
    if err.code == 429:
        # Wait out a growing slice of the rate-limit window rather than a few
        # seconds -- the budget only replenishes on the window, so short retries
        # just burn attempts.
        return min(RATE_LIMIT_WINDOW * 1.5, (RATE_LIMIT_WINDOW / 3) * (attempt + 1)) + random.uniform(0, 10)
    return MIN_REQUEST_INTERVAL * (2 ** attempt) + random.uniform(0, 2)


def _request(url, timeout):
    """Perform a throttled GET with retries on rate limits and server errors.
    Returns the raw response body as bytes.
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code != 429 and e.code < 500:
                raise
            if attempt == MAX_RETRIES - 1:
                break
            delay = _backoff_seconds(e, attempt)
            print(
                f"  Grain API {e.code}; retrying in {delay:.1f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise last_error


def _get(path, timeout=30):
    url = f"{BASE_URL}{path}"
    try:
        return json.loads(_request(url, timeout))
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
        return _request(url, timeout=60).decode("utf-8")
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


def list_all_recordings(include_participants=True, after_datetime=None, before_datetime=None,
                        max_pages=100, stop_before_date=None):
    """Paginate through all recordings matching the filters.

    Recordings come back newest-first. When `stop_before_date` (a YYYY-MM-DD
    string) is given, pagination stops as soon as a page contains only
    recordings older than that date — there is no point burning rate limit on
    pages we would discard anyway.
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
                (r.get("start_datetime", "")[:10] for r in recs if r.get("start_datetime")),
                default="",
            )
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
