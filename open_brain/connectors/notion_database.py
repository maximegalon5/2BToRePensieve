"""Ingest all pages from a Notion database into the knowledge graph.

Supports incremental sync — only fetches pages modified since last sync.
Pulls page properties (title, AI summary, URL, etc.) alongside block content.

Usage:
    # Full ingest
    python -m open_brain.connectors.notion_database --database-id DB_ID

    # Incremental sync (only new/modified pages)
    python -m open_brain.connectors.notion_database --database-id DB_ID --sync

    # Dry run
    python -m open_brain.connectors.notion_database --database-id DB_ID --dry-run

    # With limit
    python -m open_brain.connectors.notion_database --database-id DB_ID --limit 5 --sync
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import tempfile
import httpx
from openai import OpenAI

from open_brain.chunking import chunk_text
from open_brain.config import load_open_brain_config
from open_brain.db import get_client
from open_brain.embeddings import get_cloud_embedder
from open_brain.ingest import ingest_content


NOTION_API = "https://api.notion.com/v1"


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def query_database_pages(
    database_id: str, token: str, last_edited_after: str | None = None
) -> list[dict]:
    """Query pages in a Notion database. Optionally filter by last_edited_time."""
    headers = get_headers(token)
    pages = []
    cursor = None

    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        # Incremental sync: only pages edited after the given timestamp
        if last_edited_after:
            body["filter"] = {
                "timestamp": "last_edited_time",
                "last_edited_time": {
                    "after": last_edited_after,
                },
            }

        # Sort by last_edited_time descending for most recent first
        body["sorts"] = [
            {"timestamp": "last_edited_time", "direction": "descending"}
        ]

        resp = httpx.post(
            f"{NOTION_API}/databases/{database_id}/query",
            headers=headers,
            json=body,
            timeout=30,
        )
        data = resp.json()

        if "results" not in data:
            print(f"  Notion API error: {data.get('message', data)}")
            break

        for page in data.get("results", []):
            pages.append(page)

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return pages


def extract_page_title(page: dict) -> str:
    """Extract the title from a Notion page's properties."""
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return "(untitled)"


def extract_page_properties(page: dict) -> dict[str, str]:
    """Extract key properties from a Notion page as text."""
    props = {}
    for name, prop in page.get("properties", {}).items():
        ptype = prop.get("type", "")

        if ptype == "title":
            props[name] = "".join(t.get("plain_text", "") for t in prop.get("title", []))
        elif ptype == "rich_text":
            props[name] = "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
        elif ptype == "url":
            props[name] = prop.get("url") or ""
        elif ptype == "created_time":
            props[name] = prop.get("created_time") or ""
        elif ptype == "last_edited_time":
            props[name] = prop.get("last_edited_time") or ""
        elif ptype == "select":
            sel = prop.get("select")
            if sel:
                props[name] = sel.get("name", "")
        elif ptype == "multi_select":
            props[name] = ", ".join(s.get("name", "") for s in prop.get("multi_select", []))
        elif ptype == "number":
            val = prop.get("number")
            if val is not None:
                props[name] = str(val)
        elif ptype == "checkbox":
            props[name] = "Yes" if prop.get("checkbox") else "No"
        elif ptype == "date":
            date_obj = prop.get("date")
            if date_obj:
                props[name] = date_obj.get("start", "")

    return {k: v for k, v in props.items() if v}


def _get_file_url(block_data: dict) -> str | None:
    """Extract download URL from a Notion file/pdf block.

    Notion file objects have either type "file" (hosted, with expiring URL)
    or type "external" (user-provided URL).
    """
    file_type = block_data.get("type")
    if file_type == "file":
        return block_data.get("file", {}).get("url")
    elif file_type == "external":
        return block_data.get("external", {}).get("url")
    return None


def _download_and_extract_pdf(url: str) -> str:
    """Download a PDF from URL and extract text using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("    PyMuPDF not installed, skipping PDF extraction")
        return ""

    try:
        resp = httpx.get(url, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        pdf_bytes = resp.content

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        for page in doc:
            text = page.get_text()
            if text.strip():
                pages.append(text)
        doc.close()
        return "\n\n".join(pages)
    except Exception as e:
        print(f"    PDF extraction failed: {e}")
        return ""


def fetch_page_blocks(page_id: str, token: str) -> str:
    """Fetch all text content from a Notion page's blocks.

    Handles text blocks (paragraphs, headings, lists, etc.) and PDF/file
    blocks (downloads and extracts text from attached PDFs).
    """
    headers = get_headers(token)
    all_texts: list[str] = []
    cursor = None

    while True:
        params: dict = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor

        resp = httpx.get(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=headers,
            params=params,
            timeout=30,
        )
        data = resp.json()

        for block in data.get("results", []):
            block_type = block.get("type", "")
            block_data = block.get(block_type, {})

            # PDF and file blocks: download and extract text
            if block_type in ("pdf", "file"):
                url = _get_file_url(block_data)
                if url and (url.lower().endswith(".pdf") or block_type == "pdf"):
                    caption = ""
                    captions = block_data.get("caption", [])
                    if captions:
                        caption = "".join(c.get("plain_text", "") for c in captions)

                    print(f"    Downloading PDF block{': ' + caption if caption else ''}...")
                    pdf_text = _download_and_extract_pdf(url)
                    if pdf_text:
                        header = f"[PDF: {caption}]" if caption else "[Attached PDF]"
                        all_texts.append(f"{header}\n{pdf_text}")
                        print(f"    Extracted {len(pdf_text)} chars from PDF")
                continue

            # Standard text blocks
            rich_texts = block_data.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_texts)
            if text:
                all_texts.append(text)

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return "\n\n".join(all_texts)


def build_content(title: str, properties: dict[str, str], body: str) -> str:
    """Combine title, properties, and body into a single content string."""
    parts = [f"Title: {title}"]

    for key, value in properties.items():
        if key.lower() not in ("name",):  # Skip title property (already included)
            parts.append(f"{key}: {value}")

    if body:
        parts.append("")
        parts.append(body)

    return "\n".join(parts)


def get_sync_state(db_client, sync_id: str) -> str | None:
    """Get last sync timestamp from sync_state table."""
    result = db_client.table("sync_state").select("last_synced_at").eq("id", sync_id).execute()
    if result.data:
        return result.data[0]["last_synced_at"]
    return None


def get_already_ingested_notion(db_client) -> set[str]:
    """Get set of Notion page IDs already ingested."""
    result = db_client.table("sources") \
        .select("origin") \
        .eq("source_type", "notion_page") \
        .like("origin", "notion://%") \
        .execute()

    ingested = set()
    for row in result.data or []:
        origin = row.get("origin", "")
        # Extract page ID from notion://PAGE_ID or notion://PAGE_ID#chunk-N
        page_id = origin.replace("notion://", "").split("#")[0]
        if page_id:
            ingested.add(page_id)
    return ingested


def update_sync_state(db_client, sync_id: str, metadata: dict | None = None):
    """Update sync state cursor."""
    now = datetime.now(timezone.utc).isoformat()
    existing = db_client.table("sync_state").select("id").eq("id", sync_id).execute()
    if existing.data:
        db_client.table("sync_state").update({
            "last_synced_at": now,
            "metadata": metadata or {},
        }).eq("id", sync_id).execute()
    else:
        db_client.table("sync_state").insert({
            "id": sync_id,
            "last_synced_at": now,
            "metadata": metadata or {},
        }).execute()


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest Notion database pages into Open Brain.")
    ap.add_argument("--database-id", required=True, help="Notion database ID")
    ap.add_argument("--token", default=None, help="Notion API token (or set NOTION_API_TOKEN env)")
    ap.add_argument("--limit", type=int, default=0, help="Max pages to process (0 = all)")
    ap.add_argument("--min-chars", type=int, default=50, help="Skip pages shorter than this")
    ap.add_argument("--dry-run", action="store_true", help="List pages without ingesting")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between API calls")
    ap.add_argument("--sync", action="store_true", help="Incremental sync — only new/modified pages")
    args = ap.parse_args()

    token = args.token or os.getenv("NOTION_API_TOKEN", "")
    if not token:
        print("Error: Provide --token or set NOTION_API_TOKEN env variable")
        return 1

    cfg = load_open_brain_config()
    sync_id = f"notion_db_{args.database_id.replace('-', '')}"

    if not args.dry_run:
        db_client = get_client(cfg)
        embed_client, embed_model = get_cloud_embedder(cfg)
        chat_client = OpenAI(base_url=cfg.openrouter.base_url, api_key=cfg.openrouter.api_key)

    # Sync strategy: fetch ALL pages from Notion, then filter locally to find
    # (a) pages modified since last sync (re-ingest) and (b) pages never ingested (backfill).
    # This ensures the backlog is worked through even if pages haven't been edited recently.
    already_ingested: set[str] = set()
    last_edited_after = None
    if args.sync and not args.dry_run:
        last_edited_after = get_sync_state(db_client, sync_id)
        already_ingested = get_already_ingested_notion(db_client)
        if last_edited_after:
            print(f"Sync cursor: {last_edited_after}")
            print(f"Already ingested: {len(already_ingested)} pages")
        else:
            print("First sync: fetching all pages")

    print(f"Querying Notion database {args.database_id}...")
    all_pages = query_database_pages(args.database_id, token)  # Always fetch all
    print(f"Total pages in database: {len(all_pages)}")

    if args.sync and not args.dry_run and already_ingested:
        # Split into modified (re-ingest) and new (backfill)
        modified_pages = []
        new_pages = []
        for page in all_pages:
            page_id = page["id"]
            last_edited = page.get("last_edited_time", "")
            if page_id in already_ingested:
                # Re-ingest if modified since last sync
                if last_edited_after and last_edited > last_edited_after:
                    modified_pages.append(page)
            else:
                new_pages.append(page)

        print(f"Modified since last sync: {len(modified_pages)}")
        print(f"Never ingested (backfill): {len(new_pages)}")
        # Prioritize modified pages, then backfill
        pages = modified_pages + new_pages
    else:
        pages = all_pages

    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE INGEST'}")
    print()

    n_processed = 0
    n_skipped = 0
    n_success = 0
    n_dup = 0
    n_failed = 0
    total_entities = 0
    total_chunks = 0

    for page in pages:
        n_processed += 1
        if args.limit and n_processed > args.limit:
            break

        page_id = page["id"]
        title = extract_page_title(page)
        properties = extract_page_properties(page)
        notion_url = page.get("url", "")
        source_url = properties.get("URL", "")

        if args.dry_run:
            last_edited = page.get("last_edited_time", "")
            print(f"  [{n_processed}] {title} (edited: {last_edited})")
            for k, v in properties.items():
                if k.lower() not in ("name",) and v:
                    print(f"    {k}: {v[:100]}")
            continue

        # Fetch page content (includes PDF text extraction)
        try:
            body = fetch_page_blocks(page_id, token)
        except Exception as e:
            print(f"  [{n_processed}] FETCH ERROR {title} — {e}")
            n_failed += 1
            continue

        content = build_content(title, properties, body)

        if len(content) < args.min_chars:
            print(f"  [{n_processed}] SKIP {title} — only {len(content)} chars")
            n_skipped += 1
            continue

        try:
            # Chunk large pages (especially those with PDF content)
            chunks = chunk_text(content)
            page_entities = 0
            page_rels = 0
            page_obs = 0
            page_success = 0
            page_dup = 0

            base_metadata = {
                "page_id": page_id,
                "source_url": source_url,
                "notion_url": notion_url,
                "properties": properties,
            }

            for ci, chunk in enumerate(chunks):
                chunk_label = f" (chunk {ci+1}/{len(chunks)})" if len(chunks) > 1 else ""
                chunk_origin = f"notion://{page_id}" if ci == 0 else f"notion://{page_id}#chunk-{ci+1}"
                chunk_title = f"{title}{chunk_label}"

                chunk_meta = dict(base_metadata)
                if len(chunks) > 1:
                    chunk_meta.update({
                        "chunk_index": ci,
                        "total_chunks": len(chunks),
                        "chunk_chars": len(chunk),
                        "total_chars": len(content),
                    })

                result = ingest_content(
                    supabase_client=db_client,
                    embed_client=embed_client,
                    embed_model=embed_model,
                    chat_client=chat_client,
                    chat_model=cfg.openrouter.chat_model,
                    content=chunk,
                    source_type="notion_page",
                    origin=chunk_origin,
                    title=chunk_title,
                    metadata=chunk_meta,
                )

                status = result.get("status", "unknown")
                if status == "success":
                    page_success += 1
                    page_entities += result.get("entities_count", 0)
                    page_rels += result.get("relations_count", 0)
                    page_obs += result.get("observations_count", 0)
                elif status == "duplicate":
                    page_dup += 1

                if args.delay > 0:
                    time.sleep(args.delay)

            total_chunks += len(chunks)

            if page_success > 0:
                n_success += 1
                total_entities += page_entities
                chunk_info = f" ({len(chunks)} chunks)" if len(chunks) > 1 else ""
                print(f"  [{n_processed}] OK {title}{chunk_info} — {page_entities} entities, {page_rels} rels, {page_obs} obs")
            elif page_dup == len(chunks):
                n_dup += 1
                print(f"  [{n_processed}] DUP {title}")
            else:
                n_failed += 1
                print(f"  [{n_processed}] FAIL {title}")

        except Exception as e:
            n_failed += 1
            print(f"  [{n_processed}] ERROR {title} — {e}")

    # Update sync state on success
    if not args.dry_run and args.sync:
        update_sync_state(db_client, sync_id, {
            "database_id": args.database_id,
            "pages_found": len(pages),
            "ingested_this_run": n_success,
        })

    print()
    print(f"=== Notion Database Ingestion Complete ===")
    print(f"Pages found: {len(pages)}")
    print(f"Processed: {n_processed}")
    print(f"Skipped (too short): {n_skipped}")
    print(f"Success: {n_success}")
    print(f"Duplicates: {n_dup}")
    print(f"Failed: {n_failed}")
    print(f"Total chunks: {total_chunks}")
    print(f"Total entities extracted: {total_entities}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
