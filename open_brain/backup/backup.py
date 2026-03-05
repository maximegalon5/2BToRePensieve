"""Export Open Brain knowledge graph to local SQL dump and/or JSONL files."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import os

BACKUP_DIR = Path(__file__).parent / "dumps"


def pg_dump(db_url: str, output_path: Path) -> None:
    """Run pg_dump for the knowledge graph tables."""
    tables = ["sources", "entities", "relations", "observations"]
    table_args = []
    for t in tables:
        table_args.extend(["-t", t])

    cmd = ["pg_dump", db_url, *table_args, "-f", str(output_path)]
    subprocess.run(cmd, check=True)
    print(f"SQL dump saved to {output_path}")


def jsonl_export(output_dir: Path) -> None:
    """Export each table as a JSONL file via Supabase client."""
    from open_brain.config import load_open_brain_config
    from open_brain.db import get_client

    cfg = load_open_brain_config()
    client = get_client(cfg)

    tables = ["sources", "entities", "relations", "observations"]
    for table in tables:
        path = output_dir / f"{table}.jsonl"
        page_size = 1000
        offset = 0
        count = 0

        with open(path, "w", encoding="utf-8") as f:
            while True:
                result = client.table(table).select("*").range(offset, offset + page_size - 1).execute()
                rows = result.data or []
                if not rows:
                    break
                for row in rows:
                    f.write(json.dumps(row, default=str) + "\n")
                    count += 1
                offset += page_size

        print(f"  {table}: {count} rows -> {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Backup Open Brain knowledge graph.")
    ap.add_argument("--format", choices=["sql", "jsonl", "both"], default="both")
    ap.add_argument("--output-dir", default=str(BACKUP_DIR))
    args = ap.parse_args()

    db_url = os.getenv("SUPABASE_DB_URL", "")
    if not db_url and args.format in ("sql", "both"):
        print("Error: SUPABASE_DB_URL not set (needed for pg_dump)")
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.format in ("sql", "both"):
        sql_path = output_dir / f"open-brain-backup-{timestamp}.sql"
        pg_dump(db_url, sql_path)

    if args.format in ("jsonl", "both"):
        jsonl_dir = output_dir / f"open-brain-backup-{timestamp}"
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_export(jsonl_dir)

    print(f"\nBackup complete: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
