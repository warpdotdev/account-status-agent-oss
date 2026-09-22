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

# The Grain API rate limits aggressively on busy accounts. Retry transient
# failures with exponential backoff instead of aborting the whole run.
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
MAX_RETRIES = int(os.environ.get("GRAINIAC_MAX_RETRIES", "6"))
MAX_BACKOFF_SECONDS = 90


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _backoff_seconds(error, attempt):
    """Honour Retry-After when the server sends it, else exponential backoff."""
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after and retry_after.strip().isdigit():
        return min(MAX_BACKOFF_SECONDS, int(retry_after))
    return min(MAX_BACKOFF_SECONDS, 10 * (2 ** attempt))


def _request(path, timeout, parse_json):
    """Perform a GET against the Grain API, retrying on transient failures."""
    url = f"{BASE_URL}{path}"
    attempt = 0
    while True:
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            return json.loads(body) if parse_json else body.decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in RETRY_STATUS_CODES and attempt < MAX_RETRIES:
                wait = _backoff_seconds(e, attempt)
                print(
                    f"  Grain API {e.code} on GET {path} — retrying in {wait}s "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(wait)
                attempt += 1
                continue
            raise RuntimeError(f"Grain API {e.code} on GET {path}: {e.reason}") from e
        except urllib.error.URLError as e:
            if attempt < MAX_RETRIES:
                wait = min(MAX_BACKOFF_SECONDS, 5 * (2 ** attempt))
                print(
                    f"  Network error on GET {path} ({e.reason}) — retrying in {wait}s "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(wait)
                attempt += 1
                continue
            raise


def _get(path, timeout=30):
    return _request(path, timeout, parse_json=True)


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
    """Fetch the full plain-text transcript for a recording.

    Returns an empty string for recordings that have no transcript (for example a
    no-show that produced a few seconds of video).
    """
    return _request(f"/recordings/{recording_id}/transcript.txt", 60, parse_json=False)


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

    Recordings come back newest first. Pass ``stop_before_date`` (a 'YYYY-MM-DD'
    string) to stop paginating as soon as a page contains only recordings older
    than that date. Without it, a daily run walks the account's entire history,
    which is slow and reliably trips the Grain rate limiter.
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
            oldest = min((r.get("start_datetime") or "" for r in recs), default="")
            if oldest and oldest[:10] < stop_before_date:
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
