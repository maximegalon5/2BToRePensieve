"""Bulk ingest local files (markdown, text) into the knowledge graph."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from open_brain.config import load_open_brain_config
from open_brain.db import get_client
from open_brain.embeddings import get_cloud_embedder, get_local_embedder
from open_brain.ingest import ingest_content
from openai import OpenAI


def collect_files(
    paths: list[str],
    exclude: list[str],
    extensions: list[str],
    max_size_mb: int,
) -> list:
    """Collect files from directories matching given extensions."""
    from dataclasses import dataclass

    @dataclass
    class FileEntry:
        path: Path

    results: list[FileEntry] = []
    exclude_set = {Path(e).resolve() for e in exclude}
    max_bytes = max_size_mb * 1024 * 1024

    for root in paths:
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            continue
        for p in sorted(root_path.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() not in extensions:
                continue
            if p.stat().st_size > max_bytes:
                continue
            if any(p.resolve().is_relative_to(exc) for exc in exclude_set):
                continue
            results.append(FileEntry(path=p))
    return results


def read_text_file(path: Path) -> str:
    """Read a text file with fallback encoding."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description="Bulk ingest local files into Open Brain knowledge graph.")
    ap.add_argument("--paths", nargs="+", required=True, help="Directories to ingest")
    ap.add_argument("--extensions", nargs="+", default=[".md", ".txt"], help="File extensions to include")
    ap.add_argument("--exclude", nargs="*", default=[], help="Directories to exclude")
    ap.add_argument("--max-file-mb", type=int, default=5, help="Max file size in MB")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of files (0=all)")
    ap.add_argument("--use-local-embed", action="store_true", help="Use local LM Studio for embeddings")
    ap.add_argument("--dry-run", action="store_true", help="List files without ingesting")
    args = ap.parse_args()

    cfg = load_open_brain_config()
    files = collect_files(args.paths, args.exclude, args.extensions, args.max_file_mb)
    if args.limit > 0:
        files = files[:args.limit]

    print(f"Found {len(files)} files to ingest")

    if args.dry_run:
        for f in files:
            print(f"  {f.path}")
        return 0

    db_client = get_client(cfg)

    if args.use_local_embed:
        embed_client, embed_model = get_local_embedder(cfg)
    else:
        embed_client, embed_model = get_cloud_embedder(cfg)

    chat_client = OpenAI(
        base_url=cfg.openrouter.base_url,
        api_key=cfg.openrouter.api_key,
    )
    chat_model = cfg.openrouter.chat_model

    success = 0
    failed = 0
    duplicates = 0

    for idx, f in enumerate(files, start=1):
        print(f"[{idx}/{len(files)}] {f.path}", flush=True)

        text = read_text_file(f.path)
        if not text.strip():
            print("  - skipped (empty)", flush=True)
            continue

        ext = f.path.suffix.lower()
        source_type = "markdown" if ext == ".md" else "text"

        result = ingest_content(
            supabase_client=db_client,
            embed_client=embed_client,
            embed_model=embed_model,
            chat_client=chat_client,
            chat_model=chat_model,
            content=text,
            source_type=source_type,
            origin=str(f.path),
            title=f.path.stem,
        )

        status = result["status"]
        if status == "success":
            success += 1
            e = result["entities_count"]
            o = result["observations_count"]
            r = result["relations_count"]
            print(f"  - extracted {e} entities, {r} relations, {o} observations", flush=True)
        elif status == "duplicate":
            duplicates += 1
            print("  - skipped (duplicate)", flush=True)
        else:
            failed += 1
            print(f"  - FAILED: {result.get('error', 'unknown')}", flush=True)

    print(f"\nDone. {success} ingested, {duplicates} duplicates, {failed} failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
