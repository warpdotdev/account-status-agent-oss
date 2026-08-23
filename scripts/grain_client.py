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

# Grain rate-limits the public API (observed: x-ratelimit-limit: 30 per window).
# Retry 429s with exponential backoff instead of failing the whole run.
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 5

# Retrying on 429 recovers, but a busy day (one hydration request per recording)
# can outrun the budget and pay a failed request plus backoff for each overage.
# Responses carry the remaining budget, so pause before spending the last of it.
RATE_LIMIT_FLOOR = 2
RATE_LIMIT_COOLDOWN_SECONDS = 20  # observed window refill time


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _throttle_if_budget_low(headers):
    """Pause when the remaining rate-limit budget is nearly spent."""
    try:
        remaining = int(headers.get("x-ratelimit-remaining"))
    except (TypeError, ValueError):
        return  # header absent or malformed; rely on 429 retries instead
    if remaining <= RATE_LIMIT_FLOOR:
        print(
            f"  Grain rate limit nearly exhausted ({remaining} left); "
            f"pausing {RATE_LIMIT_COOLDOWN_SECONDS}s",
            file=sys.stderr,
        )
        time.sleep(RATE_LIMIT_COOLDOWN_SECONDS)


def _request_with_retry(url, timeout):
    """Perform a GET, retrying on 429 (and 5xx) with exponential backoff.

    Honors the Retry-After header when the server provides one.
    Returns the raw response body as bytes.
    """
    backoff = INITIAL_BACKOFF_SECONDS
    last_error = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                _throttle_if_budget_low(resp.headers)
                return body
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code != 429 and e.code < 500:
                raise
            if attempt == MAX_RETRIES - 1:
                break
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                delay = float(retry_after) if retry_after else backoff
            except (TypeError, ValueError):
                delay = backoff
            print(
                f"  Grain API {e.code}; retrying in {delay:.0f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)
            backoff *= 2
    raise last_error


def _get(path, timeout=30):
    url = f"{BASE_URL}{path}"
    try:
        return json.loads(_request_with_retry(url, timeout))
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
        return _request_with_retry(url, timeout=60).decode("utf-8")
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


def list_all_recordings(
    include_participants=True,
    after_datetime=None,
    before_datetime=None,
    max_pages=100,
    stop_before_date=None,
):
    """Paginate through all recordings matching the filters.

    The list endpoint returns recordings in reverse-chronological order, so when
    the caller only cares about a specific day we can stop paginating as soon as
    a page is entirely older than that day. This matters because the Grain public
    API is rate limited (~30 requests per window) and walking full history will
    reliably 429.

    Args:
        stop_before_date: 'YYYY-MM-DD' string. Stop paginating once a page
            contains only recordings that started before this date.
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
            page_dates = [r.get("start_datetime", "")[:10] for r in recs]
            page_dates = [d for d in page_dates if d]
            # Every recording on this page predates the target day, and results
            # are newest-first, so nothing newer remains.
            if page_dates and max(page_dates) < stop_before_date:
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
