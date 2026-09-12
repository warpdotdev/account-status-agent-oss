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

# The Grain API rate-limits aggressively, especially when several agents run
# concurrently. Every request goes through _request(), which retries on 429 and
# transient 5xx responses with exponential backoff.
MAX_ATTEMPTS = int(os.environ.get("GRAINIAC_GRAIN_MAX_ATTEMPTS", "6"))
INITIAL_BACKOFF_SECONDS = float(os.environ.get("GRAINIAC_GRAIN_INITIAL_BACKOFF", "5"))
MAX_BACKOFF_SECONDS = float(os.environ.get("GRAINIAC_GRAIN_MAX_BACKOFF", "60"))
RETRY_STATUSES = (429, 500, 502, 503, 504)


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _retry_delay(error, attempt):
    """Seconds to wait before the next attempt, honoring Retry-After when sent."""
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF_SECONDS)
        except ValueError:
            pass
    backoff = min(INITIAL_BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS)
    # Jitter so concurrent agents do not retry in lockstep.
    return backoff * (0.5 + random.random() / 2)


def _request(path, timeout, parse_json, description):
    url = f"{BASE_URL}{path}"
    for attempt in range(MAX_ATTEMPTS):
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body) if parse_json else body.decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                delay = _retry_delay(e, attempt)
                print(
                    f"  Grain API {e.code} on {description}; "
                    f"retrying in {delay:.1f}s (attempt {attempt + 2}/{MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"Grain API {e.code} on {description}: {e.reason}") from e
    raise RuntimeError(f"Grain API still rate-limiting {description} after {MAX_ATTEMPTS} attempts")


def _get(path, timeout=30):
    return _request(path, timeout, parse_json=True, description=f"GET {path}")


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
        f"/recordings/{recording_id}/transcript.txt",
        timeout=60,
        parse_json=False,
        description=f"transcript for {recording_id}",
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

    `stop_before_date` (a YYYY-MM-DD string) bounds the walk: the API returns
    recordings newest-first, so once a page contains only recordings older than
    that date there is nothing left to collect. This avoids walking the entire
    workspace history — and the rate limiting that comes with it — just to build
    a single day's list. The early exit only applies while the stream really is
    ordered newest-first; if it ever isn't, we fall back to a full walk.
    """
    all_recs = []
    cursor = None
    previous_oldest = None
    ordering_is_descending = True

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
            dates = [r.get("start_datetime", "")[:10] for r in recs]
            dates = [d for d in dates if d]
            if not dates:
                continue
            oldest = min(dates)
            if previous_oldest is not None and oldest > previous_oldest:
                ordering_is_descending = False
            previous_oldest = oldest
            if ordering_is_descending and oldest < stop_before_date:
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
