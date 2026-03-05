"""Parse WhatsApp chat export (.txt) and ingest into knowledge graph."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from open_brain.config import load_open_brain_config
from open_brain.db import get_client
from open_brain.embeddings import get_cloud_embedder
from open_brain.ingest import ingest_content
from openai import OpenAI

# WhatsApp export line: [DD/MM/YYYY, HH:MM:SS] Name: Message
WA_LINE = re.compile(
    r'^\[(\d{1,2}/\d{1,2}/\d{2,4}),?\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)\]\s*(.+?):\s*(.+)$'
)


def parse_whatsapp_export(text: str) -> list[dict]:
    """Parse a WhatsApp .txt export into message dicts."""
    messages: list[dict] = []
    current: dict | None = None

    for line in text.splitlines():
        match = WA_LINE.match(line)
        if match:
            if current:
                messages.append(current)
            current = {
                "date": match.group(1),
                "time": match.group(2),
                "sender": match.group(3),
                "text": match.group(4),
            }
        elif current:
            current["text"] += "\n" + line

    if current:
        messages.append(current)

    return messages


def group_messages(messages: list[dict], group_size: int = 20) -> list[str]:
    """Group sequential messages to preserve conversational context."""
    groups: list[str] = []
    for i in range(0, len(messages), group_size):
        batch = messages[i : i + group_size]
        text = "\n".join(
            f"[{m['date']} {m['time']}] {m['sender']}: {m['text']}"
            for m in batch
        )
        groups.append(text)
    return groups


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest WhatsApp export into Open Brain.")
    ap.add_argument("file", help="Path to WhatsApp export .txt file")
    ap.add_argument("--group-size", type=int, default=20, help="Messages per ingestion group")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of groups (0=all)")
    args = ap.parse_args()

    cfg = load_open_brain_config()
    path = Path(args.file)

    print(f"Parsing {path}...")
    text = path.read_text(encoding="utf-8", errors="replace")
    messages = parse_whatsapp_export(text)
    print(f"Parsed {len(messages)} messages")

    groups = group_messages(messages, args.group_size)
    if args.limit > 0:
        groups = groups[:args.limit]
    print(f"Grouped into {len(groups)} chunks")

    db_client = get_client(cfg)
    embed_client, embed_model = get_cloud_embedder(cfg)
    chat_client = OpenAI(base_url=cfg.openrouter.base_url, api_key=cfg.openrouter.api_key)

    success = 0
    for idx, group_text in enumerate(groups, start=1):
        print(f"[{idx}/{len(groups)}] Ingesting message group...", flush=True)

        result = ingest_content(
            supabase_client=db_client,
            embed_client=embed_client,
            embed_model=embed_model,
            chat_client=chat_client,
            chat_model=cfg.openrouter.chat_model,
            content=group_text,
            source_type="whatsapp",
            origin=str(path),
            title=f"WhatsApp: {path.stem} (group {idx})",
        )

        if result["status"] == "success":
            success += 1

    print(f"\nDone. {success}/{len(groups)} groups ingested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
