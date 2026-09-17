#!/usr/bin/env python3
"""Notion API client. Requires GRAINIAC_NOTION_TOKEN environment variable."""

import urllib.request
import urllib.error
import json
import os

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
        if e.code == 404 and "/databases/" in path:
            raise RuntimeError(_database_404_help(path)) from e
        raise RuntimeError(f"Notion API {e.code} on {method} {path}: {e.reason}") from e


def accessible_databases():
    """List databases the integration can actually see, as (id, title, title_property)."""
    result = _api("POST", "/search", {
        "filter": {"value": "database", "property": "object"},
        "page_size": 20,
    })
    found = []
    for db in result.get("results", []):
        title = "".join(t.get("plain_text", "") for t in db.get("title", []))
        title_prop = next(
            (k for k, v in db.get("properties", {}).items() if v.get("type") == "title"),
            None,
        )
        found.append((db.get("id"), title, title_prop))
    return found


def _database_404_help(path):
    """A 404 on a database almost always means GRAINIAC_NOTION_DATABASE_ID is stale or
    the database was never shared with the integration. Say so, and show what IS visible.
    """
    msg = [
        f"Notion API 404 on {path}.",
        f"GRAINIAC_NOTION_DATABASE_ID={_database_id()!r} is not visible to this integration "
        "(wrong ID, or the database was never shared with it).",
    ]
    try:
        visible = accessible_databases()
    except Exception:
        visible = []
    if visible:
        msg.append("Databases this integration CAN see:")
        for db_id, title, title_prop in visible:
            msg.append(f"  - {title!r} id={db_id} title property={title_prop!r}")
        msg.append(
            "Set GRAINIAC_NOTION_DATABASE_ID (and GRAINIAC_NOTION_TITLE_PROPERTY) to match one of these."
        )
    else:
        msg.append(
            "This integration cannot see any databases. Share the Account Tracking database "
            "with it via the Notion UI (Connections → add your integration)."
        )
    return "\n".join(msg)


# ── Database operations ──

def find_company_page(company_name, database_id=None, title_property=None):
    """Search the database for an existing page with the given company name.
    Returns the page object or None.
    """
    db = database_id or _database_id()
    prop = title_property or _title_property()
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
