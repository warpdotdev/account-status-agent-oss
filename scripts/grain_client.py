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

# The Grain API rate limits aggressively (observed: x-ratelimit-limit: 23 per
# rolling window) and the token is typically shared across concurrently running
# agents, so 429s are routine rather than exceptional. Retry them with
# exponential backoff + jitter instead of failing the whole run.
MAX_RETRIES = 6
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _backoff_seconds(attempt, retry_after=None):
    """Honor Retry-After when the server sends it, else exponential + jitter."""
    if retry_after:
        try:
            return min(120.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    return min(60.0, (2 ** attempt) * 2.0) + random.uniform(0, 1.5)


def _request(path, timeout=30, parse_json=True):
    """Issue a GET against the Grain API, retrying transient failures."""
    url = f"{BASE_URL}{path}"
    last_error = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body) if parse_json else body.decode("utf-8")
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code not in RETRYABLE_STATUS or attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"Grain API {e.code} on GET {path}: {e.reason}") from e
            delay = _backoff_seconds(attempt, e.headers.get("retry-after"))
            print(
                f"  Grain API {e.code} on GET {path}; retrying in {delay:.1f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)
        except urllib.error.URLError as e:
            last_error = e
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"Grain API unreachable on GET {path}: {e.reason}") from e
            delay = _backoff_seconds(attempt)
            print(
                f"  Grain API connection error on GET {path}; retrying in {delay:.1f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise RuntimeError(f"Grain API retries exhausted on GET {path}: {last_error}")


def _get(path, timeout=30):
    return _request(path, timeout=timeout)


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
        f"/recordings/{recording_id}/transcript.txt", timeout=60, parse_json=False
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


def list_all_recordings(include_participants=True, after_datetime=None, before_datetime=None,
                        max_pages=100, stop_before_date=None):
    """Paginate through all recordings matching the filters.

    Recordings are returned newest-first. Pass `stop_before_date` ('YYYY-MM-DD')
    to stop as soon as a page ends older than that date. Without it, fetching a
    single day walks the entire account history, which burns the rate limit
    (~23 requests per window) and usually ends in a 429.
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
            oldest = (recs[-1].get("start_datetime") or "")[:10]
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
