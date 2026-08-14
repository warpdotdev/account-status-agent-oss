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

# The Grain API rate limits aggressively when paginating. Requests are paced and
# retried with exponential backoff on 429/5xx responses.
REQUEST_INTERVAL_SECONDS = float(os.environ.get("GRAINIAC_REQUEST_INTERVAL", "1.5"))
MAX_RETRIES = int(os.environ.get("GRAINIAC_MAX_RETRIES", "6"))
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_last_request_time = 0.0


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _throttle():
    """Space out requests so we stay under the Grain API rate limit."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < REQUEST_INTERVAL_SECONDS:
        time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)
    _last_request_time = time.monotonic()


def _retry_after_seconds(error, attempt):
    """Honor a Retry-After header when present, else exponential backoff."""
    header = None
    if getattr(error, "headers", None):
        header = error.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 120.0)
        except ValueError:
            pass
    return min(2.0 ** attempt, 60.0)


def _request(url, timeout, decode_json=True, description=None):
    """Perform a GET with throttling and retries on transient failures."""
    label = description or url
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body) if decode_json else body.decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                delay = _retry_after_seconds(e, attempt)
                print(
                    f"  Grain API {e.code} on {label}; retrying in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"Grain API {e.code} on GET {label}: {e.reason}") from e
        except urllib.error.URLError as e:
            if attempt < MAX_RETRIES:
                delay = min(2.0 ** attempt, 60.0)
                print(
                    f"  Network error on {label}: {e.reason}; retrying in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"Network error on GET {label}: {e.reason}") from e


def _get(path, timeout=30):
    return _request(f"{BASE_URL}{path}", timeout, decode_json=True, description=path)


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
    return _request(
        url,
        timeout=60,
        decode_json=False,
        description=f"/recordings/{recording_id}/transcript.txt",
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
