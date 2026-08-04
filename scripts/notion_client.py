#!/usr/bin/env python3
"""Notion API client. Requires GRAINIAC_NOTION_TOKEN environment variable."""

import urllib.request
import urllib.error
import json
import os
import sys

BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Resolved (database_id, title_property), cached for the life of the process.
_RESOLVED_DB = None


def _plain_text(rich_text):
    """Flatten a Notion rich text array to a plain string."""
    return "".join(t.get("plain_text", "") for t in (rich_text or []))


def _search(body):
    return _api("POST", "/search", body)


def _discover_database():
    """Find a database the integration can actually see, via /search.

    Prefers a name match against GRAINIAC_NOTION_DATABASE_NAME when set;
    otherwise returns the only shared database. Returns the database object.
    """
    results = _search({
        "filter": {"value": "database", "property": "object"},
        "page_size": 100,
    }).get("results", [])

    wanted = os.environ.get("GRAINIAC_NOTION_DATABASE_NAME", "").strip().casefold()
    if wanted:
        named = [d for d in results if _plain_text(d.get("title")).casefold() == wanted]
        if named:
            return named[0]

    if not results:
        raise RuntimeError(
            "No Notion database is shared with this integration. Share the Account "
            "Tracking database with the integration, or set a reachable "
            "GRAINIAC_NOTION_DATABASE_ID."
        )
    if len(results) > 1 and not wanted:
        names = ", ".join(_plain_text(d.get("title")) or d["id"] for d in results)
        print(
            f"  Warning: multiple Notion databases are shared with this integration "
            f"({names}); using the first. Set GRAINIAC_NOTION_DATABASE_NAME to "
            f"disambiguate.",
            file=sys.stderr,
        )
    return results[0]


def _detect_title_property(db_obj):
    """Determine the database's title property, trusting the schema over config."""
    configured = os.environ.get("GRAINIAC_NOTION_TITLE_PROPERTY", "").strip()
    props = db_obj.get("properties") or {}
    actual = [name for name, spec in props.items() if spec.get("type") == "title"]

    if configured and configured in props:
        return configured
    if actual:
        if configured:
            print(
                f"  Warning: GRAINIAC_NOTION_TITLE_PROPERTY={configured!r} is not a "
                f"property of this database; using the actual title property "
                f"{actual[0]!r}.",
                file=sys.stderr,
            )
        return actual[0]
    return configured or "Company"


def resolve_database():
    """Resolve (database_id, title_property) for the Account Tracking database.

    Uses GRAINIAC_NOTION_DATABASE_ID when the integration can actually read it.
    A stale or unshared ID (Notion answers 404) falls back to discovery via
    /search rather than failing the run. The title property is read from the
    database schema so a misconfigured GRAINIAC_NOTION_TITLE_PROPERTY does not
    silently produce empty query results.
    """
    global _RESOLVED_DB
    if _RESOLVED_DB is not None:
        return _RESOLVED_DB

    configured = os.environ.get("GRAINIAC_NOTION_DATABASE_ID", "").strip()
    db_obj = None
    if configured:
        try:
            db_obj = _api("GET", f"/databases/{configured}")
        except RuntimeError as e:
            print(
                f"  Warning: configured GRAINIAC_NOTION_DATABASE_ID ({configured}) is "
                f"not readable by this integration ({e}); discovering the shared "
                f"database instead.",
                file=sys.stderr,
            )

    if db_obj is None:
        db_obj = _discover_database()
        print(
            f"  Using Notion database {_plain_text(db_obj.get('title'))!r} "
            f"({db_obj['id']}) discovered via /search.",
            file=sys.stderr,
        )

    _RESOLVED_DB = (db_obj["id"], _detect_title_property(db_obj))
    return _RESOLVED_DB


def _database_id():
    return resolve_database()[0]


def _title_property():
    """Name of the database's title property (the column that holds the company name).
    Detected from the database schema; override via GRAINIAC_NOTION_TITLE_PROPERTY.
    """
    return resolve_database()[1]


def _headers():
    token = os.environ.get("GRAINIAC_NOTION_TOKEN")
    if not token:
        raise RuntimeError("GRAINIAC_NOTION_TOKEN environment variable is required")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _api(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Notion API {e.code} on {method} {path}: {e.reason}") from e


# ── Database operations ──

def _page_title(page):
    """Extract a page's title text from its properties."""
    for spec in (page.get("properties") or {}).values():
        if spec.get("type") == "title":
            return _plain_text(spec.get("title"))
    return ""


def _query_database(db, body):
    """Query a database, following pagination. Returns all matching pages."""
    pages = []
    cursor = None
    while True:
        payload = dict(body)
        payload["page_size"] = 100
        if cursor:
            payload["start_cursor"] = cursor
        resp = _api("POST", f"/databases/{db}/query", payload)
        pages.extend(resp.get("results", []))
        if not resp.get("has_more"):
            return pages
        cursor = resp["next_cursor"]


def _find_page_by_search(company_name):
    """Last-resort lookup: find a page by title via /search (needs no DB access)."""
    results = _search({
        "query": company_name,
        "filter": {"value": "page", "property": "object"},
        "page_size": 100,
    }).get("results", [])
    target = company_name.casefold()
    for page in results:
        if _page_title(page).casefold() == target:
            return page
    return None


def find_company_page(company_name, database_id=None, title_property=None):
    """Search the database for an existing page with the given company name.
    Returns the page object or None.

    Matching is exact first, then case-insensitive, so a company name inferred
    from an email domain (e.g. "Chargepoint") still finds the existing page
    ("ChargePoint"). If the database itself is unreachable, falls back to a
    /search lookup so a stale database ID does not create duplicate pages.
    """
    try:
        db = database_id or _database_id()
        prop = title_property or _title_property()
        exact = _api("POST", f"/databases/{db}/query", {
            "filter": {"property": prop, "title": {"equals": company_name}}
        }).get("results", [])
        if exact:
            return exact[0]

        target = company_name.casefold()
        for page in _query_database(db, {}):
            if _page_title(page).casefold() == target:
                return page
        return None
    except RuntimeError as e:
        print(
            f"  Warning: database lookup for {company_name!r} failed ({e}); "
            f"falling back to /search.",
            file=sys.stderr,
        )
        return _find_page_by_search(company_name)


def create_page(company_name, children_blocks, database_id=None, title_property=None):
    """Create a new page in the database with the given company name and initial blocks.
    Notion limits to 100 children per request. Returns the created page object.
    """
    db = database_id or _database_id()
    prop = title_property or _title_property()
    return _api("POST", "/pages", {
        "parent": {"database_id": db},
        "properties": {prop: {"title": [{"text": {"content": company_name}}]}},
        "children": children_blocks[:100],
    })


# ── Block operations ──

def get_all_blocks(page_id):
    """Get all child blocks of a page, handling pagination."""
    blocks = []
    cursor = None
    while True:
        url = f"/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        resp = _api("GET", url)
        blocks.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return blocks


def append_blocks(parent_id, children, after_block_id=None):
    """Append blocks to a parent. Optionally insert after a specific block."""
    body = {"children": children}
    if after_block_id:
        body["after"] = after_block_id
    return _api("PATCH", f"/blocks/{parent_id}/children", body)


def update_block(block_id, block_data):
    """Update a block's content."""
    return _api("PATCH", f"/blocks/{block_id}", block_data)


def delete_block(block_id):
    """Delete a block."""
    return _api("DELETE", f"/blocks/{block_id}")


def find_block_by_text(blocks, text_prefix, block_type=None):
    """Find a block whose text starts with a given prefix. Returns (index, block) or (None, None)."""
    for i, b in enumerate(blocks):
        btype = b["type"]
        if block_type and btype != block_type:
            continue
        rt = b.get(btype, {}).get("rich_text", [])
        full_text = "".join(t.get("plain_text", "") for t in rt)
        if full_text.startswith(text_prefix):
            return i, b
    return None, None


def find_heading_block(blocks, heading_text, level=1):
    """Find a heading block by its text. Returns (index, block) or (None, None)."""
    htype = f"heading_{level}"
    return find_block_by_text(blocks, heading_text, block_type=htype)


def find_section_range(blocks, heading_text, heading_level=1):
    """Find the index range of a section (from heading to next same-level heading or divider).
    Returns (heading_index, last_content_index) or (None, None).
    """
    htype = f"heading_{heading_level}"
    start_idx, _ = find_heading_block(blocks, heading_text, level=heading_level)
    if start_idx is None:
        return None, None

    end_idx = start_idx
    for i in range(start_idx + 1, len(blocks)):
        btype = blocks[i]["type"]
        if btype == htype or btype == "divider":
            break
        end_idx = i
    return start_idx, end_idx


def get_last_block_in_section(blocks, heading_text, heading_level=1):
    """Get the ID of the last block before the divider after a section heading.
    Useful for appending to the end of a section.
    """
    _, end_idx = find_section_range(blocks, heading_text, heading_level)
    if end_idx is not None:
        return blocks[end_idx]["id"]
    return None


# ── Block builders ──

def rich(content, bold=False, link=None):
    """Create a rich text object."""
    obj = {"type": "text", "text": {"content": content}}
    if link:
        obj["text"]["link"] = {"url": link}
    if bold:
        obj["annotations"] = {"bold": True}
    return obj


def heading1(text):
    return {"object": "block", "type": "heading_1", "heading_1": {"rich_text": [rich(text)]}}


def heading2(text):
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [rich(text)]}}


def heading3(text):
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [rich(text)]}}


def paragraph(parts):
    """parts is a list of rich text objects from rich()."""
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": parts}}


def bullet(parts):
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": parts}}


def todo(parts, checked=False):
    return {"object": "block", "type": "to_do", "to_do": {"rich_text": parts, "checked": checked}}


def divider():
    return {"object": "block", "type": "divider", "divider": {}}
