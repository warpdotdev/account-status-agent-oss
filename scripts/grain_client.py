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

# The Grain public API enforces a small request budget (observed: 17 requests
# per rolling minute, reported via x-ratelimit-* response headers). Paginating
# the recordings list and then hydrating each recording individually blows
# through that budget easily, so every request goes through a throttle that
# watches the remaining budget and backs off before we get a 429.
RATE_LIMIT_FLOOR = int(os.environ.get("GRAINIAC_RATE_LIMIT_FLOOR", "2"))
RATE_LIMIT_SLEEP = float(os.environ.get("GRAINIAC_RATE_LIMIT_SLEEP", "62"))
MAX_RETRIES = int(os.environ.get("GRAINIAC_MAX_RETRIES", "5"))

_RATE_STATE = {"limit": None, "remaining": None}


def _headers():
    token = os.environ.get("GRAINIAC_GRAIN_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_GRAIN_TOKEN environment variable is required")
    return {"Authorization": f"Bearer {token}"}


def _record_rate_headers(headers):
    """Track the API's advertised rate-limit budget from response headers."""
    for header, key in (("x-ratelimit-limit", "limit"), ("x-ratelimit-remaining", "remaining")):
        value = headers.get(header)
        if value is None:
            continue
        try:
            _RATE_STATE[key] = int(value)
        except (TypeError, ValueError):
            pass


def _throttle():
    """Pause if the remaining request budget is nearly exhausted."""
    remaining = _RATE_STATE.get("remaining")
    if remaining is not None and remaining <= RATE_LIMIT_FLOOR:
        print(
            f"  Rate limit nearly exhausted ({remaining} of "
            f"{_RATE_STATE.get('limit')} left); sleeping {RATE_LIMIT_SLEEP:.0f}s",
            file=sys.stderr,
        )
        time.sleep(RATE_LIMIT_SLEEP)
        # Budget is unknown until the next response tells us otherwise.
        _RATE_STATE["remaining"] = None


def _retry_delay(error, attempt):
    """Seconds to wait before retrying, honouring Retry-After when provided."""
    retry_after = error.headers.get("retry-after") if error.headers else None
    if retry_after:
        try:
            return max(1.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    return min(30.0 * (2 ** attempt), 120.0)


def _open(url, context, timeout):
    """GET a URL with rate-limit throttling and retries on 429/5xx.
    Returns the raw response body as bytes.
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _record_rate_headers(resp.headers)
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.headers:
                _record_rate_headers(e.headers)
            if e.code != 429 and not (500 <= e.code < 600):
                raise RuntimeError(f"Grain API {e.code} on {context}: {e.reason}") from e
            last_error = e
            _RATE_STATE["remaining"] = None
            if attempt == MAX_RETRIES - 1:
                break
            delay = _retry_delay(e, attempt)
            print(
                f"  Grain API {e.code} on {context}; retrying in {delay:.0f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Grain API {last_error.code} on {context} after {MAX_RETRIES} attempts: {last_error.reason}"
    ) from last_error


def _get(path, timeout=30):
    return json.loads(_open(f"{BASE_URL}{path}", f"GET {path}", timeout))


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
    body = _open(url, f"fetching transcript for {recording_id}", timeout=60)
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


def list_all_recordings(include_participants=True, after_datetime=None, before_datetime=None,
                        max_pages=100, stop_before_date=None):
    """Paginate through all recordings matching the filters.

    The API ignores afterDatetime/beforeDatetime (filtering is client-side), and
    the request budget is small, so pass `stop_before_date` ("YYYY-MM-DD") to
    stop paging as soon as we reach recordings older than the date of interest.
    The list is returned newest-first, so everything after that point is older.
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
        if stop_before_date and any(
            (r.get("start_datetime") or "")[:10] < stop_before_date for r in recs
        ):
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
