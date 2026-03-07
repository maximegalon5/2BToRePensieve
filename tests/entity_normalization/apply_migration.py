"""
Apply SQL migrations to Supabase via the Management API.

Usage:
  python apply_migration.py 005
  python apply_migration.py 006
"""

import sys
import os

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import httpx

SUPABASE_ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
PROJECT_REF = "your-project-ref"

if not SUPABASE_ACCESS_TOKEN:
    print("Missing env var: SUPABASE_ACCESS_TOKEN")
    sys.exit(1)


def execute_sql(sql: str) -> dict:
    """Execute SQL via Supabase Management API."""
    res = httpx.post(
        f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query",
        headers={
            "Authorization": f"Bearer {SUPABASE_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"query": sql},
        timeout=300,
    )
    if res.status_code == 201 or res.status_code == 200:
        return {"ok": True, "data": res.json()}
    else:
        return {"ok": False, "status": res.status_code, "error": res.text[:500]}


def main():
    if len(sys.argv) < 2:
        print("Usage: python apply_migration.py <migration_number>")
        sys.exit(1)

    migration_num = sys.argv[1]
    migrations_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "supabase", "migrations"
    )

    # Find matching migration file
    migration_file = None
    for f in sorted(os.listdir(migrations_dir)):
        if f.startswith(migration_num) and f.endswith(".sql"):
            migration_file = os.path.join(migrations_dir, f)
            break

    if not migration_file:
        print(f"No migration file found starting with '{migration_num}'")
        sys.exit(1)

    print(f"Migration: {os.path.basename(migration_file)}")
    with open(migration_file, encoding="utf-8") as f:
        sql = f.read()

    print(f"SQL: {len(sql)} chars")
    print(f"Executing against project {PROJECT_REF}...\n")

    result = execute_sql(sql)

    if result["ok"]:
        print("Migration applied successfully!")
        data = result["data"]
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("rows"):
                    print(f"  Rows affected: {len(item['rows'])}")
        elif isinstance(data, dict):
            print(f"  Result: {str(data)[:200]}")
    else:
        print(f"Migration FAILED: {result.get('status')} {result.get('error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
