"""Extract text from PDF files and ingest into knowledge graph.

Supports both local files and raw bytes (for use by other connectors).
Long PDFs are automatically chunked (~10k chars) so each chunk gets full
LLM extraction. Every chunk links back to the source file via origin.

Usage:
    # Single PDF
    python -m open_brain.connectors.pdf_ingest path/to/file.pdf

    # Multiple PDFs
    python -m open_brain.connectors.pdf_ingest *.pdf

    # With custom title
    python -m open_brain.connectors.pdf_ingest report.pdf --title "Q4 Revenue Report"

    # Dry run
    python -m open_brain.connectors.pdf_ingest *.pdf --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import fitz  # PyMuPDF

from open_brain.chunking import chunk_text
from open_brain.config import load_open_brain_config
from open_brain.db import get_client
from open_brain.embeddings import get_cloud_embedder
from open_brain.ingest import ingest_content
from openai import OpenAI


def extract_pdf_text(path: Path) -> str:
    """Extract all text from a local PDF file."""
    doc = fitz.open(str(path))
    pages = []
    for page in doc:
        text = page.get_text()
        if text.strip():
            pages.append(text)
    doc.close()
    return "\n\n".join(pages)


def extract_pdf_text_from_bytes(data: bytes) -> str:
    """Extract all text from PDF bytes (for use by email/Notion connectors)."""
    doc = fitz.open(stream=data, filetype="pdf")
    pages = []
    for page in doc:
        text = page.get_text()
        if text.strip():
            pages.append(text)
    doc.close()
    return "\n\n".join(pages)


def ingest_pdf(
    db_client,
    embed_client,
    embed_model: str,
    chat_client,
    chat_model: str,
    text: str,
    origin: str,
    title: str,
    source_type: str = "pdf",
    metadata: dict[str, Any] | None = None,
    max_chunk_chars: int = 10000,
) -> dict[str, Any]:
    """Ingest PDF text with automatic chunking.

    Returns aggregated stats across all chunks, or a single result for short PDFs.
    """
    chunks = chunk_text(text, max_chars=max_chunk_chars)

    total_entities = 0
    total_relations = 0
    total_observations = 0
    n_success = 0
    n_dup = 0
    errors: list[str] = []

    for i, chunk in enumerate(chunks):
        chunk_label = f" (chunk {i+1}/{len(chunks)})" if len(chunks) > 1 else ""
        chunk_origin = origin if i == 0 else f"{origin}#chunk-{i+1}"
        chunk_title = f"{title}{chunk_label}"

        chunk_meta = dict(metadata or {})
        chunk_meta.update({
            "chunk_index": i,
            "total_chunks": len(chunks),
            "chunk_chars": len(chunk),
            "total_chars": len(text),
        })

        result = ingest_content(
            supabase_client=db_client,
            embed_client=embed_client,
            embed_model=embed_model,
            chat_client=chat_client,
            chat_model=chat_model,
            content=chunk,
            source_type=source_type,
            origin=chunk_origin,
            title=chunk_title,
            metadata=chunk_meta,
        )

        status = result.get("status", "unknown")
        if status == "success":
            n_success += 1
            total_entities += result.get("entities_count", 0)
            total_relations += result.get("relations_count", 0)
            total_observations += result.get("observations_count", 0)
            print(f"    chunk {i+1}/{len(chunks)}: {result.get('entities_count', 0)} entities, "
                  f"{result.get('relations_count', 0)} rels, {result.get('observations_count', 0)} obs")
        elif status == "duplicate":
            n_dup += 1
            print(f"    chunk {i+1}/{len(chunks)}: duplicate")
        else:
            errors.append(result.get("error", "unknown"))
            print(f"    chunk {i+1}/{len(chunks)}: FAILED — {result.get('error', 'unknown')[:100]}")

    if n_success == 0 and n_dup == len(chunks):
        return {"status": "duplicate", "message": "All chunks already ingested"}

    return {
        "status": "success" if n_success > 0 else "failed",
        "chunks": len(chunks),
        "chunks_success": n_success,
        "chunks_duplicate": n_dup,
        "chunks_failed": len(errors),
        "entities_count": total_entities,
        "relations_count": total_relations,
        "observations_count": total_observations,
        "errors": errors if errors else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest PDF files into Open Brain.")
    ap.add_argument("files", nargs="+", help="PDF file paths")
    ap.add_argument("--title", default="", help="Override title (single file only)")
    ap.add_argument("--dry-run", action="store_true", help="Show PDF info without ingesting")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between chunks")
    args = ap.parse_args()

    cfg = load_open_brain_config()

    if not args.dry_run:
        db_client = get_client(cfg)
        embed_client, embed_model = get_cloud_embedder(cfg)
        chat_client = OpenAI(base_url=cfg.openrouter.base_url, api_key=cfg.openrouter.api_key)

    n_total = 0
    n_success = 0
    total_entities = 0
    total_chunks = 0

    for filepath in args.files:
        n_total += 1
        path = Path(filepath)
        print(f"[{n_total}] Extracting text from {path}...")
        text = extract_pdf_text(path)
        chunks = chunk_text(text)
        print(f"  {len(text)} chars -> {len(chunks)} chunk(s)")

        if not text.strip():
            print("  Empty PDF, skipping.")
            continue

        if args.dry_run:
            continue

        title = args.title or path.stem

        result = ingest_pdf(
            db_client=db_client,
            embed_client=embed_client,
            embed_model=embed_model,
            chat_client=chat_client,
            chat_model=cfg.openrouter.chat_model,
            text=text,
            origin=f"file://{path.resolve()}",
            title=title,
            metadata={"filename": path.name, "filepath": str(path.resolve())},
        )

        status = result.get("status", "unknown")
        if status == "success":
            n_success += 1
            ec = result.get("entities_count", 0)
            total_entities += ec
            total_chunks += result.get("chunks", 0)
            print(f"  OK — {result.get('chunks', 0)} chunks, {ec} entities, "
                  f"{result.get('relations_count', 0)} rels, {result.get('observations_count', 0)} obs")
        elif status == "duplicate":
            print(f"  DUP — already ingested")
        else:
            print(f"  FAILED — {result.get('errors', ['unknown'])}")

        if args.delay > 0 and n_total < len(args.files):
            time.sleep(args.delay)

    print()
    print(f"=== PDF Ingestion Complete ===")
    print(f"Files: {n_total}")
    print(f"Success: {n_success}")
    print(f"Total chunks: {total_chunks}")
    print(f"Total entities: {total_entities}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
