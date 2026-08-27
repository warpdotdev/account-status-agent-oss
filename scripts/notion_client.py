#!/usr/bin/env python3
"""Notion API client. Requires GRAINIAC_NOTION_TOKEN environment variable."""

import urllib.request
import urllib.error
import json
import os
import sys

BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _database_id():
    db = os.environ.get("GRAINIAC_NOTION_DATABASE_ID", "").strip()
    if not db:
        raise RuntimeError("GRAINIAC_NOTION_DATABASE_ID environment variable is required")
    return db


def _title_property():
    """Name of the database's title property (the column that holds the company name).
    New Notion databases call this 'Name'; override via GRAINIAC_NOTION_TITLE_PROPERTY.
    Defaults to 'Company'.
    """
    return os.environ.get("GRAINIAC_NOTION_TITLE_PROPERTY", "Company").strip() or "Company"


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
        detail = ""
        try:
            detail = json.loads(e.read() or b"{}").get("message", "")
        except (ValueError, OSError):
            pass
        hint = ""
        if e.code == 404:
            hint = (
                " — the ID is wrong, or the page/database has not been shared with "
                "the Notion integration. Check GRAINIAC_NOTION_DATABASE_ID and the "
                "integration's access (Notion: ... menu > Connections)."
            )
        raise RuntimeError(
            f"Notion API {e.code} on {method} {path}: {e.reason}"
            f"{f' ({detail})' if detail else ''}{hint}"
        ) from e


# ── Database operations ──

def get_database(database_id=None):
    """Fetch a database object (schema included)."""
    return _api("GET", f"/databases/{database_id or _database_id()}")


_resolved_title_props = {}


def resolve_title_property(database_id=None, title_property=None):
    """Return the database's actual title property name.

    Every Notion database has exactly one title property, but its name varies
    ('Company', 'Name', 'Account name', ...). Rather than failing with an opaque
    validation error when the configured name is wrong, look up the real one and
    warn. Results are cached per database.
    """
    db = database_id or _database_id()
    configured = title_property or _title_property()

    cached = _resolved_title_props.get(db)
    if cached:
        return cached

    try:
        schema = get_database(db).get("properties", {})
    except RuntimeError as e:
        # Can't introspect (e.g. 404/permissions) — let the caller's own request
        # surface the real error instead of masking it here.
        print(f"  Warning: could not read database schema: {e}", file=sys.stderr)
        return configured

    if schema.get(configured, {}).get("type") == "title":
        _resolved_title_props[db] = configured
        return configured

    actual = next((n for n, p in schema.items() if p.get("type") == "title"), None)
    if not actual:
        raise RuntimeError(f"Notion database {db} has no title property")

    print(
        f"  Warning: configured title property {configured!r} is not this "
        f"database's title property; using {actual!r} instead. Set "
        f"GRAINIAC_NOTION_TITLE_PROPERTY={actual!r} to silence this.",
        file=sys.stderr,
    )
    _resolved_title_props[db] = actual
    return actual


def find_company_page(company_name, database_id=None, title_property=None):
    """Search the database for an existing page with the given company name.
    Returns the page object or None.
    """
    db = database_id or _database_id()
    prop = resolve_title_property(db, title_property)
    result = _api("POST", f"/databases/{db}/query", {
        "filter": {"property": prop, "title": {"equals": company_name}}
    })
    pages = result.get("results", [])
    return pages[0] if pages else None


def create_page(company_name, children_blocks, database_id=None, title_property=None):
    """Create a new page in the database with the given company name and initial blocks.
    Notion limits to 100 children per request. Returns the created page object.
    """
    db = database_id or _database_id()
    prop = resolve_title_property(db, title_property)
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
