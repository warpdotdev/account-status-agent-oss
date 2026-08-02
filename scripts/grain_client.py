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

# The Grain public API allows roughly 30 requests per rolling minute
# (advertised via the x-ratelimit-limit response header). Exceeding it returns
# HTTP 429. We retry with exponential backoff and keep a small delay between
# paginated requests so a busy day doesn't blow through the budget.
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 5
PAGE_DELAY_SECONDS = 2.5
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _retry_after_seconds(err, attempt):
    """Honor a Retry-After header when present, else exponential backoff."""
    raw = err.headers.get("Retry-After") if err.headers else None
    if raw:
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            pass
    return INITIAL_BACKOFF_SECONDS * (2 ** attempt)


def _request(url, timeout, parse_json, description):
    req = urllib.request.Request(url, headers=_headers())
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body) if parse_json else body.decode("utf-8")
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code not in RETRYABLE_STATUS or attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"Grain API {e.code} {description}: {e.reason}") from e
            delay = _retry_after_seconds(e, attempt)
            print(
                f"  Grain API {e.code} {description}; retrying in {delay:.0f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)
        except urllib.error.URLError as e:
            last_error = e
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"Grain API unreachable {description}: {e.reason}") from e
            time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** attempt))
    raise RuntimeError(f"Grain API request failed {description}: {last_error}")


def _get(path, timeout=30):
    return _request(f"{BASE_URL}{path}", timeout, True, f"on GET {path}")


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
    return _request(
        f"{BASE_URL}/recordings/{recording_id}/transcript.txt",
        60,
        False,
        f"fetching transcript for {recording_id}",
    )


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

    The API ignores afterDatetime/beforeDatetime server-side, but it does return
    recordings in reverse chronological order (newest first). `stop_before_date`
    (a "YYYY-MM-DD" string) exploits that: pagination halts once a full page is
    older than the given date, so a daily run costs a couple of requests instead
    of walking the entire archive and tripping the rate limit.
    """
    all_recs = []
    cursor = None
    for page in range(max_pages):
        if page > 0:
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
        if stop_before_date:
            dates = [r.get("start_datetime", "")[:10] for r in recs if r.get("start_datetime")]
            # Newest-first ordering means once the whole page predates the
            # target day, every later page does too.
            if dates and max(dates) < stop_before_date:
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
