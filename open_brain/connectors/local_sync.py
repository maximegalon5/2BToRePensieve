"""Unified local folder sync -- scan a directory and ingest everything new.

Supports markdown, text, PDF, and ChatGPT/Claude conversation exports.
Safe to re-run: content hash dedup skips already-ingested files.

Usage:
    # Sync a folder (all supported file types)
    python -m open_brain.connectors.local_sync --watch-dir ~/Documents/brain-inbox

    # Dry run to see what would be processed
    python -m open_brain.connectors.local_sync --watch-dir ~/Documents/brain-inbox --dry-run

    # Sync with specific extensions only
    python -m open_brain.connectors.local_sync --watch-dir ./notes --extensions .md .txt

    # Include ChatGPT/Claude conversation exports
    python -m open_brain.connectors.local_sync --watch-dir ./exports --include-conversations
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from openai import OpenAI

from open_brain.chunking import chunk_text
from open_brain.config import load_open_brain_config
from open_brain.db import get_client
from open_brain.embeddings import get_cloud_embedder
from open_brain.ingest import ingest_content


# --- File discovery ---

SUPPORTED_TEXT_EXTS = {".md", ".txt", ".text", ".rst", ".org"}
SUPPORTED_PDF_EXTS = {".pdf"}
CONVERSATION_FILENAMES = {"conversations.json"}

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


def discover_files(
    watch_dir: Path,
    extensions: set[str],
    include_pdfs: bool = True,
    include_conversations: bool = False,
    exclude_dirs: list[str] | None = None,
) -> dict[str, list[Path]]:
    """Scan directory and categorize files by type.

    Returns {"text": [...], "pdf": [...], "conversations": [...]}.
    """
    exclude = {d.lower() for d in (exclude_dirs or [])}
    exclude.update({".git", "__pycache__", ".venv", "node_modules", ".claude"})

    result: dict[str, list[Path]] = {"text": [], "pdf": [], "conversations": []}

    if not watch_dir.exists():
        print(f"Error: {watch_dir} does not exist")
        return result

    for p in sorted(watch_dir.rglob("*")):
        if not p.is_file():
            continue

        # Skip excluded directories
        parts_lower = [part.lower() for part in p.relative_to(watch_dir).parts]
        if any(ex in parts_lower for ex in exclude):
            continue

        # Skip large files
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            if p.stat().st_size == 0:
                continue
        except OSError:
            continue

        # Categorize
        if include_conversations and p.name.lower() in CONVERSATION_FILENAMES:
            result["conversations"].append(p)
        elif p.suffix.lower() in extensions:
            result["text"].append(p)
        elif include_pdfs and p.suffix.lower() in SUPPORTED_PDF_EXTS:
            result["pdf"].append(p)

    return result


# --- Text file ingestion ---

def read_text_file(path: Path, max_chars: int = 2_000_000) -> str:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    return text[:max_chars]


def ingest_text_file(
    path: Path,
    db_client,
    embed_client,
    embed_model: str,
    chat_client,
    chat_model: str,
    watch_dir: Path,
) -> dict[str, Any]:
    text = read_text_file(path)
    if not text.strip():
        return {"status": "skipped", "reason": "empty"}

    ext = path.suffix.lower()
    source_type = "markdown" if ext == ".md" else "text"
    relative = path.relative_to(watch_dir)

    chunks = chunk_text(text)

    if len(chunks) == 1:
        return ingest_content(
            supabase_client=db_client,
            embed_client=embed_client,
            embed_model=embed_model,
            chat_client=chat_client,
            chat_model=chat_model,
            content=text,
            source_type=source_type,
            origin=f"file://{path.resolve()}",
            title=path.stem,
            metadata={"filename": path.name, "relative_path": str(relative)},
        )

    # Multi-chunk
    n_success = 0
    n_dup = 0
    total_entities = 0

    for i, chunk in enumerate(chunks):
        chunk_label = f" (chunk {i+1}/{len(chunks)})"
        chunk_origin = f"file://{path.resolve()}" if i == 0 else f"file://{path.resolve()}#chunk-{i+1}"

        result = ingest_content(
            supabase_client=db_client,
            embed_client=embed_client,
            embed_model=embed_model,
            chat_client=chat_client,
            chat_model=chat_model,
            content=chunk,
            source_type=source_type,
            origin=chunk_origin,
            title=f"{path.stem}{chunk_label}",
            metadata={
                "filename": path.name,
                "relative_path": str(relative),
                "chunk_index": i,
                "total_chunks": len(chunks),
            },
        )

        status = result.get("status", "unknown")
        if status == "success":
            n_success += 1
            total_entities += result.get("entities_count", 0)
        elif status == "duplicate":
            n_dup += 1

    if n_success == 0 and n_dup == len(chunks):
        return {"status": "duplicate"}

    return {
        "status": "success" if n_success > 0 else "failed",
        "chunks": len(chunks),
        "entities_count": total_entities,
    }


# --- PDF ingestion ---

def ingest_pdf_file(
    path: Path,
    db_client,
    embed_client,
    embed_model: str,
    chat_client,
    chat_model: str,
    watch_dir: Path,
) -> dict[str, Any]:
    from open_brain.connectors.pdf_ingest import extract_pdf_text, ingest_pdf

    text = extract_pdf_text(path)
    if not text.strip():
        return {"status": "skipped", "reason": "empty PDF"}

    relative = path.relative_to(watch_dir)
    return ingest_pdf(
        db_client=db_client,
        embed_client=embed_client,
        embed_model=embed_model,
        chat_client=chat_client,
        chat_model=chat_model,
        text=text,
        origin=f"file://{path.resolve()}",
        title=path.stem,
        metadata={"filename": path.name, "relative_path": str(relative)},
    )


# --- Conversation export ingestion ---

def detect_conversation_format(path: Path) -> str | None:
    """Detect if a conversations.json is ChatGPT or Claude format."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            # Read just enough to detect format
            chunk = f.read(5000)
            if '"mapping"' in chunk:
                return "chatgpt"
            elif '"chat_messages"' in chunk:
                return "claude"
    except Exception:
        pass
    return None


def ingest_conversations(
    path: Path,
    format_type: str,
    db_client,
    embed_client,
    embed_model: str,
    chat_client,
    chat_model: str,
    delay: float = 1.0,
    limit: int = 0,
) -> dict[str, int]:
    """Ingest a conversations.json file using the appropriate connector."""
    if format_type == "chatgpt":
        from open_brain.connectors.chatgpt_conversations import (
            iter_conversations,
            build_conversation_text,
            chunk_conversation,
        )
        source_type = "chatgpt_conversation"
        origin_prefix = "chatgpt://"
        id_field = lambda c: c.get("conversation_id") or c.get("id") or ""
    else:
        from open_brain.connectors.claude_conversations import (
            iter_conversations,
            build_conversation_text,
            chunk_conversation,
        )
        source_type = "claude_conversation"
        origin_prefix = "claude://"
        id_field = lambda c: c.get("uuid") or ""

    n_convos = 0
    n_success = 0
    n_dup = 0
    n_failed = 0
    n_skipped = 0

    for convo in iter_conversations(str(path)):
        n_convos += 1
        if limit and n_convos > limit:
            break

        convo_id = id_field(convo)

        if format_type == "chatgpt":
            title, text, msgs = build_conversation_text(convo)
            msg_count = len(msgs)
        else:
            title, text, msg_count = build_conversation_text(convo)

        if len(text) < 100:
            n_skipped += 1
            continue

        chunks = chunk_conversation(text)

        for i, chunk in enumerate(chunks):
            chunk_label = f" (chunk {i+1}/{len(chunks)})" if len(chunks) > 1 else ""
            chunk_suffix = f"/chunk-{i+1}" if len(chunks) > 1 else ""

            try:
                result = ingest_content(
                    supabase_client=db_client,
                    embed_client=embed_client,
                    embed_model=embed_model,
                    chat_client=chat_client,
                    chat_model=chat_model,
                    content=chunk,
                    source_type=source_type,
                    origin=f"{origin_prefix}{convo_id}{chunk_suffix}",
                    title=f"{title}{chunk_label}",
                    metadata={
                        "platform": format_type,
                        "conversation_id": convo_id,
                        "message_count": msg_count,
                        "chunk_index": i,
                        "total_chunks": len(chunks),
                    },
                )

                status = result.get("status", "unknown")
                if status == "success":
                    n_success += 1
                    ec = result.get("entities_count", 0)
                    print(f"  [{n_convos}] OK {title}{chunk_label} -> {ec} entities")
                elif status == "duplicate":
                    n_dup += 1
                else:
                    n_failed += 1
                    print(f"  [{n_convos}] FAIL {title}{chunk_label} -> {result.get('error', 'unknown')[:80]}")

                if delay > 0:
                    time.sleep(delay)

            except Exception as e:
                n_failed += 1
                print(f"  [{n_convos}] ERROR {title}{chunk_label} -> {e}")

        if n_convos % 50 == 0:
            print(f"  --- {n_convos} convos: {n_success} ok, {n_dup} dup, {n_failed} fail ---")

    return {
        "conversations": n_convos,
        "skipped": n_skipped,
        "success": n_success,
        "duplicates": n_dup,
        "failed": n_failed,
    }


# --- Main ---

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sync local files into Open Brain. Safe to re-run -- dedup skips already-ingested content."
    )
    ap.add_argument("--watch-dir", required=True, help="Directory to scan for new files")
    ap.add_argument("--extensions", nargs="*", default=None,
                    help="Text file extensions to include (default: .md .txt)")
    ap.add_argument("--include-conversations", action="store_true",
                    help="Also process ChatGPT/Claude conversations.json files")
    ap.add_argument("--no-pdf", action="store_true", help="Skip PDF files")
    ap.add_argument("--exclude-dirs", nargs="*", default=[], help="Directories to exclude")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max conversations to process per export file (0=all)")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between API calls")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be synced")
    args = ap.parse_args()

    watch_dir = Path(args.watch_dir).resolve()
    extensions = set(args.extensions) if args.extensions else SUPPORTED_TEXT_EXTS

    print(f"=== Open Brain Local Sync ===")
    print(f"Watch dir: {watch_dir}")
    print(f"Extensions: {', '.join(sorted(extensions))}")
    print(f"PDFs: {'no' if args.no_pdf else 'yes'}")
    print(f"Conversations: {'yes' if args.include_conversations else 'no'}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()

    files = discover_files(
        watch_dir,
        extensions=extensions,
        include_pdfs=not args.no_pdf,
        include_conversations=args.include_conversations,
        exclude_dirs=args.exclude_dirs,
    )

    total_files = len(files["text"]) + len(files["pdf"]) + len(files["conversations"])
    print(f"Found: {len(files['text'])} text, {len(files['pdf'])} PDF, "
          f"{len(files['conversations'])} conversation exports ({total_files} total)")
    print()

    if total_files == 0:
        print("Nothing to sync.")
        return 0

    if args.dry_run:
        for category, paths in files.items():
            if paths:
                print(f"--- {category} ---")
                for p in paths:
                    size_kb = p.stat().st_size / 1024
                    print(f"  {p.relative_to(watch_dir)} ({size_kb:.1f} KB)")
        return 0

    # Initialize clients
    cfg = load_open_brain_config()
    db_client = get_client(cfg)
    embed_client, embed_model = get_cloud_embedder(cfg)
    chat_client = OpenAI(base_url=cfg.openrouter.base_url, api_key=cfg.openrouter.api_key)
    chat_model = cfg.openrouter.chat_model

    n_success = 0
    n_dup = 0
    n_failed = 0
    n_skipped = 0
    total_entities = 0

    # 1. Text files
    for idx, path in enumerate(files["text"], 1):
        relative = path.relative_to(watch_dir)
        print(f"[text {idx}/{len(files['text'])}] {relative}")

        result = ingest_text_file(
            path, db_client, embed_client, embed_model, chat_client, chat_model, watch_dir
        )

        status = result.get("status", "unknown")
        if status == "success":
            n_success += 1
            ec = result.get("entities_count", 0)
            total_entities += ec
            print(f"  OK -> {ec} entities")
        elif status == "duplicate":
            n_dup += 1
            print(f"  DUP (already ingested)")
        elif status == "skipped":
            n_skipped += 1
            print(f"  SKIP ({result.get('reason', '')})")
        else:
            n_failed += 1
            print(f"  FAIL -> {result.get('error', 'unknown')[:80]}")

        if args.delay > 0 and idx < len(files["text"]):
            time.sleep(args.delay)

    # 2. PDF files
    for idx, path in enumerate(files["pdf"], 1):
        relative = path.relative_to(watch_dir)
        print(f"[pdf {idx}/{len(files['pdf'])}] {relative}")

        result = ingest_pdf_file(
            path, db_client, embed_client, embed_model, chat_client, chat_model, watch_dir
        )

        status = result.get("status", "unknown")
        if status == "success":
            n_success += 1
            ec = result.get("entities_count", 0)
            total_entities += ec
            chunks = result.get("chunks", 1)
            print(f"  OK -> {chunks} chunks, {ec} entities")
        elif status == "duplicate":
            n_dup += 1
            print(f"  DUP (already ingested)")
        elif status == "skipped":
            n_skipped += 1
            print(f"  SKIP ({result.get('reason', '')})")
        else:
            n_failed += 1
            print(f"  FAIL -> {result.get('errors', result.get('error', 'unknown'))}")

        if args.delay > 0 and idx < len(files["pdf"]):
            time.sleep(args.delay)

    # 3. Conversation exports
    for path in files["conversations"]:
        format_type = detect_conversation_format(path)
        if not format_type:
            print(f"[conversations] {path.name} -> unknown format, skipping")
            continue

        print(f"[conversations] {path.relative_to(watch_dir)} ({format_type} format)")
        stats = ingest_conversations(
            path, format_type,
            db_client, embed_client, embed_model, chat_client, chat_model,
            delay=args.delay,
            limit=args.limit,
        )
        n_success += stats["success"]
        n_dup += stats["duplicates"]
        n_failed += stats["failed"]
        n_skipped += stats["skipped"]
        print(f"  Done: {stats['conversations']} convos, {stats['success']} ok, "
              f"{stats['duplicates']} dup, {stats['failed']} fail")

    print()
    print(f"=== Local Sync Complete ===")
    print(f"Success: {n_success}")
    print(f"Duplicates: {n_dup}")
    print(f"Skipped: {n_skipped}")
    print(f"Failed: {n_failed}")
    print(f"Total entities: {total_entities}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
