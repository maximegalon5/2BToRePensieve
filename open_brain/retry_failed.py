"""Retry extraction on sources that previously failed.

Re-runs LLM extraction + entity resolution on existing source content
without re-inserting the source or re-computing content embeddings.

Usage:
    # Retry all failed sources
    python -m open_brain.retry_failed

    # Retry only YouTube failures
    python -m open_brain.retry_failed --source-type youtube

    # Retry with limit
    python -m open_brain.retry_failed --source-type youtube --limit 10

    # Dry run
    python -m open_brain.retry_failed --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from openai import OpenAI

from open_brain.config import load_open_brain_config
from open_brain.db import get_client, get_failed_sources
from open_brain.embeddings import get_cloud_embedder
from open_brain.ingest import retry_extraction


def main() -> int:
    ap = argparse.ArgumentParser(description="Retry extraction on failed sources.")
    ap.add_argument("--source-type", default=None, help="Filter by source type (e.g. youtube, notion_page)")
    ap.add_argument("--limit", type=int, default=0, help="Max sources to retry (0 = all)")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between retries")
    ap.add_argument("--dry-run", action="store_true", help="List failed sources without retrying")
    args = ap.parse_args()

    cfg = load_open_brain_config()
    db_client = get_client(cfg)

    failed = get_failed_sources(db_client, args.source_type, args.limit)
    print(f"Found {len(failed)} failed sources{f' ({args.source_type})' if args.source_type else ''}")

    if not failed:
        return 0

    if args.dry_run:
        for i, src in enumerate(failed, 1):
            print(f"  [{i}] {src.get('title', '(untitled)')} — {src['source_type']} — {src.get('origin', '')[:80]}")
        return 0

    embed_client, embed_model = get_cloud_embedder(cfg)
    chat_client = OpenAI(base_url=cfg.openrouter.base_url, api_key=cfg.openrouter.api_key)

    n_success = 0
    n_failed = 0

    for i, src in enumerate(failed, 1):
        title = src.get("title", "(untitled)")
        print(f"  [{i}/{len(failed)}] {title}...", end=" ")

        result = retry_extraction(
            db_client, embed_client, embed_model,
            chat_client, cfg.openrouter.chat_model,
            src,
        )

        status = result.get("status", "unknown")
        if status == "success":
            n_success += 1
            print(f"OK — {result.get('entities_count', 0)} entities, {result.get('observations_count', 0)} obs")
        else:
            n_failed += 1
            print(f"FAIL — {result.get('error', '')[:80]}")

        if args.delay > 0:
            time.sleep(args.delay)

    print()
    print(f"=== Retry Complete ===")
    print(f"Total: {len(failed)}")
    print(f"Success: {n_success}")
    print(f"Failed: {n_failed}")

    # Send Telegram notification if configured
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_NOTIFY_CHAT_ID")
    if bot_token and chat_id:
        import httpx
        emoji = "OK" if n_failed == 0 else "WARN"
        msg = (
            f"Retry extraction {emoji}\n"
            f"Total: {len(failed)}, Success: {n_success}, Failed: {n_failed}"
        )
        if args.source_type:
            msg += f"\nSource type: {args.source_type}"
        try:
            httpx.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg},
                timeout=10,
            )
        except Exception:
            pass  # Don't fail the script over a notification

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
