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

# The Grain API is rate limited (observed: x-ratelimit-limit: 30). Paging plus
# per-recording hydration blows through that quickly, so every request goes
# through a retry/backoff wrapper that also proactively pauses when the
# remaining-request budget reported by the API gets low.
MAX_RETRIES = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}
RATE_LIMIT_COOLDOWN = float(os.environ.get("GRAINIAC_GRAIN_COOLDOWN", "20"))
MIN_REMAINING = 2

_last_remaining = None


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _note_rate_limit(headers):
    """Remember how much request budget the API says we have left."""
    global _last_remaining
    raw = headers.get("x-ratelimit-remaining")
    try:
        _last_remaining = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        _last_remaining = None


def _retry_delay(headers, attempt):
    """Honor Retry-After when present, otherwise exponential backoff."""
    raw = (headers or {}).get("Retry-After")
    if raw:
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            pass
    return min(60.0, 2.0 ** attempt)


def _request(path, timeout=30, raw=False):
    """GET a Grain API path with rate-limit aware retries."""
    global _last_remaining
    url = f"{BASE_URL}{path}"
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        # Proactively cool off before we run the budget to zero.
        if _last_remaining is not None and _last_remaining <= MIN_REMAINING:
            print(
                f"  Rate limit budget low ({_last_remaining} left); "
                f"sleeping {RATE_LIMIT_COOLDOWN:.0f}s",
                file=sys.stderr,
            )
            time.sleep(RATE_LIMIT_COOLDOWN)
            _last_remaining = None

        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _note_rate_limit(resp.headers)
                body = resp.read()
                return body.decode("utf-8") if raw else json.loads(body)
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                break
            delay = _retry_delay(e.headers, attempt)
            print(
                f"  Grain API {e.code} on GET {path}; "
                f"retrying in {delay:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            _last_remaining = None
            time.sleep(delay)
        except urllib.error.URLError as e:
            last_error = e
            if attempt == MAX_RETRIES:
                break
            delay = min(60.0, 2.0 ** attempt)
            print(
                f"  Grain API network error on GET {path} ({e.reason}); "
                f"retrying in {delay:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)

    if isinstance(last_error, urllib.error.HTTPError):
        raise RuntimeError(
            f"Grain API {last_error.code} on GET {path}: {last_error.reason}"
        ) from last_error
    raise RuntimeError(f"Grain API request failed on GET {path}: {last_error}") from last_error


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
    return _request(f"/recordings/{recording_id}/transcript.txt", timeout=60, raw=True)


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


def list_recordings_on_date(target_date, include_participants=True, max_pages=100):
    """Fetch recordings that start on `target_date` (YYYY-MM-DD string).

    The list endpoint returns recordings in reverse chronological order, so we
    can stop paging as soon as we walk past the target day instead of pulling
    the entire archive (which reliably trips the API rate limit).
    """
    target_str = str(target_date)
    matches = []
    cursor = None

    for page in range(max_pages):
        recs, cursor = list_recordings_page(
            cursor=cursor,
            include_participants=include_participants,
        )
        if not recs:
            break

        older_than_target = False
        for r in recs:
            day = (r.get("start_datetime") or "")[:10]
            if day == target_str:
                matches.append(r)
            elif day and day < target_str:
                older_than_target = True

        # Everything from here on is older than the target date.
        if older_than_target or not cursor:
            break

    return matches


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
