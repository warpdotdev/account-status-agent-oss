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

# Grain rate-limits the public API. Paginating through a busy workspace can
# trip a 429 partway through, so every request retries with backoff.
MAX_ATTEMPTS = int(os.environ.get("GRAINIAC_MAX_RETRIES", "6"))
BASE_BACKOFF_SECONDS = float(os.environ.get("GRAINIAC_BACKOFF_SECONDS", "5"))
MAX_BACKOFF_SECONDS = 60.0
# Small pause between paginated requests to stay under the rate limit.
PAGE_DELAY_SECONDS = float(os.environ.get("GRAINIAC_PAGE_DELAY_SECONDS", "1"))
RETRY_STATUSES = {429, 500, 502, 503, 504}


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _retry_delay(attempt, error):
    """Seconds to wait before the next attempt, honoring Retry-After when present."""
    retry_after = None
    headers = getattr(error, "headers", None)
    if headers is not None:
        raw = headers.get("Retry-After")
        if raw:
            try:
                retry_after = float(raw)
            except ValueError:
                retry_after = None
    if retry_after is None:
        retry_after = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
    return min(retry_after, MAX_BACKOFF_SECONDS) + random.uniform(0, 1)


def _urlopen_with_retry(req, timeout, description):
    """Open a request, retrying on rate limits and transient server errors."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"Grain API {e.code} on {description}: {e.reason}") from e
            delay = _retry_delay(attempt, e)
            print(
                f"  Grain API {e.code} on {description}; retrying in {delay:.1f}s "
                f"(attempt {attempt}/{MAX_ATTEMPTS - 1})",
                file=sys.stderr,
            )
            time.sleep(delay)
        except urllib.error.URLError as e:
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"Grain API request failed on {description}: {e.reason}") from e
            delay = _retry_delay(attempt, e)
            print(
                f"  Grain API network error on {description}: {e.reason}; "
                f"retrying in {delay:.1f}s (attempt {attempt}/{MAX_ATTEMPTS - 1})",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise RuntimeError(f"Grain API retries exhausted on {description}")


def _get(path, timeout=30):
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers=_headers())
    return json.loads(_urlopen_with_retry(req, timeout, f"GET {path}"))


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
    body = _urlopen_with_retry(req, 60, f"transcript fetch for {recording_id}")
    return body.decode("utf-8")


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
    stop_before_datetime=None,
):
    """Paginate through all recordings matching the filters.

    Grain ignores the afterDatetime/beforeDatetime query params, but it does
    return recordings in reverse-chronological order. `stop_before_datetime`
    (an ISO-8601 UTC string) uses that ordering to stop paging as soon as a
    page contains a recording older than the bound, which keeps a single-day
    fetch from walking the entire workspace history and tripping rate limits.
    """
    all_recs = []
    cursor = None
    for page in range(max_pages):
        if page and PAGE_DELAY_SECONDS:
            time.sleep(PAGE_DELAY_SECONDS)
        recs, cursor = list_recordings_page(
            cursor=cursor,
            include_participants=include_participants,
            after_datetime=after_datetime,
            before_datetime=before_datetime,
        )
        all_recs.extend(recs)
        if stop_before_datetime and any(
            (r.get("start_datetime") or "") < stop_before_datetime for r in recs
        ):
            break
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
