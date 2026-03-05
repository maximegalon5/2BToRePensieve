"""Ingest ChatGPT conversation exports into the knowledge graph.

Reads the official ChatGPT data export (conversations.json),
groups messages by conversation, and ingests each conversation
through the Open Brain pipeline for entity/relation/observation extraction.

Usage:
    python -m open_brain.connectors.chatgpt_conversations --in "ChatGPT/ChatGPT History/conversations.json"
    python -m open_brain.connectors.chatgpt_conversations --in "ChatGPT/ChatGPT History/conversations.json" --limit 10 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import ijson
from openai import OpenAI

from open_brain.config import load_open_brain_config
from open_brain.db import get_client
from open_brain.embeddings import get_cloud_embedder
from open_brain.ingest import ingest_content


def iso_from_unix(ts) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat()
    except Exception:
        return None


def extract_text(content_obj: dict) -> str:
    """Extract text from a ChatGPT message content object."""
    if not content_obj:
        return ""
    parts = content_obj.get("parts")
    if isinstance(parts, list):
        out = []
        for p in parts:
            if p is None:
                continue
            if isinstance(p, str):
                out.append(p)
            # Skip non-text parts (images, etc.)
        return "\n".join(out).strip()
    if isinstance(parts, str):
        return parts.strip()
    return ""


def build_conversation_text(convo: dict) -> tuple[str, str, list[dict]]:
    """Build a single text block from a ChatGPT conversation.

    Returns (title, formatted_text, message_metadata).
    """
    title = convo.get("title") or "(untitled)"
    mapping = convo.get("mapping") or {}

    msgs = []
    for node_id, node in mapping.items():
        msg = (node or {}).get("message")
        if not msg:
            continue

        author = msg.get("author") or {}
        role = author.get("role", "unknown")
        create_time = msg.get("create_time")
        content = msg.get("content") or {}
        text = extract_text(content)

        # Skip empty system messages
        if not text and role == "system":
            continue
        if not text:
            continue

        msgs.append({
            "role": role,
            "text": text,
            "created_at": create_time,
            "iso_time": iso_from_unix(create_time),
        })

    # Sort by timestamp
    msgs.sort(key=lambda m: m.get("created_at") or 0)

    # Format as conversation
    lines = []
    for m in msgs:
        role_label = {"user": "User", "assistant": "Assistant", "system": "System", "tool": "Tool"}.get(m["role"], m["role"])
        lines.append(f"[{role_label}]: {m['text']}")

    return title, "\n\n".join(lines), msgs


def chunk_conversation(text: str, max_chars: int = 10000) -> list[str]:
    """Split a long conversation into chunks at message boundaries.

    Each chunk is up to max_chars, split at double-newline boundaries.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")
    current_chunk = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2  # +2 for the \n\n separator
        if current_len + para_len > max_chars and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = []
            current_len = 0
        current_chunk.append(para)
        current_len += para_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def iter_conversations(path: str):
    """Stream conversations from a ChatGPT export JSON file."""
    with open(path, "rb") as f:
        for convo in ijson.items(f, "item"):
            yield convo


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest ChatGPT conversations into Open Brain.")
    ap.add_argument("--in", dest="in_path", required=True, help="Path to conversations.json")
    ap.add_argument("--limit", type=int, default=0, help="Max conversations to process (0 = all)")
    ap.add_argument("--min-chars", type=int, default=100, help="Skip conversations shorter than this")
    ap.add_argument("--max-chunk-chars", type=int, default=10000, help="Max chars per chunk for long conversations")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be ingested without ingesting")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between API calls (rate limiting)")
    args = ap.parse_args()

    in_path = Path(args.in_path)
    if not in_path.exists():
        print(f"Error: {in_path} not found")
        return 1

    cfg = load_open_brain_config()

    if not args.dry_run:
        db_client = get_client(cfg)
        embed_client, embed_model = get_cloud_embedder(cfg)
        chat_client = OpenAI(base_url=cfg.openrouter.base_url, api_key=cfg.openrouter.api_key)

    n_convos = 0
    n_chunks = 0
    n_skipped = 0
    n_success = 0
    n_dup = 0
    n_failed = 0
    total_entities = 0

    print(f"Reading: {in_path}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE INGEST'}")
    print(f"Min chars: {args.min_chars}, Max chunk: {args.max_chunk_chars}")
    print()

    for convo in iter_conversations(str(in_path)):
        n_convos += 1

        if args.limit and n_convos > args.limit:
            break

        convo_id = convo.get("conversation_id") or convo.get("id") or ""
        title, text, msgs = build_conversation_text(convo)

        if len(text) < args.min_chars:
            n_skipped += 1
            continue

        chunks = chunk_conversation(text, args.max_chunk_chars)

        for i, chunk in enumerate(chunks):
            n_chunks += 1
            chunk_label = f" (chunk {i+1}/{len(chunks)})" if len(chunks) > 1 else ""

            if args.dry_run:
                print(f"[{n_convos}] {title}{chunk_label} — {len(chunk)} chars, {len(msgs)} msgs")
                continue

            try:
                result = ingest_content(
                    supabase_client=db_client,
                    embed_client=embed_client,
                    embed_model=embed_model,
                    chat_client=chat_client,
                    chat_model=cfg.openrouter.chat_model,
                    content=chunk,
                    source_type="chatgpt_conversation",
                    origin=f"chatgpt://{convo_id}{f'/chunk-{i+1}' if len(chunks) > 1 else ''}",
                    title=f"{title}{chunk_label}",
                    metadata={
                        "platform": "chatgpt",
                        "conversation_id": convo_id,
                        "message_count": len(msgs),
                        "chunk_index": i,
                        "total_chunks": len(chunks),
                    },
                )

                status = result.get("status", "unknown")
                if status == "success":
                    n_success += 1
                    ec = result.get("entities_count", 0)
                    total_entities += ec
                    print(f"  [{n_convos}] OK {title}{chunk_label} — {ec} entities, {result.get('relations_count', 0)} rels, {result.get('observations_count', 0)} obs")
                elif status == "duplicate":
                    n_dup += 1
                    print(f"  [{n_convos}] DUP {title}{chunk_label}")
                else:
                    n_failed += 1
                    print(f"  [{n_convos}] FAIL {title}{chunk_label} — {result.get('error', 'unknown')}")

                if args.delay > 0:
                    time.sleep(args.delay)

            except Exception as e:
                n_failed += 1
                print(f"  [{n_convos}] ERROR {title}{chunk_label} — {e}")

        if n_convos % 25 == 0:
            print(f"--- Progress: {n_convos} convos, {n_chunks} chunks, {n_success} ok, {n_dup} dup, {n_failed} fail ---")

    print()
    print(f"=== ChatGPT Ingestion Complete ===")
    print(f"Conversations: {n_convos}")
    print(f"Chunks processed: {n_chunks}")
    print(f"Skipped (too short): {n_skipped}")
    print(f"Success: {n_success}")
    print(f"Duplicates: {n_dup}")
    print(f"Failed: {n_failed}")
    print(f"Total entities extracted: {total_entities}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
