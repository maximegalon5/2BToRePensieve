"""
Apply migration 006 in batches to avoid Supabase/Cloudflare timeouts.

1. Creates the merge_entities function
2. Runs merges in batches of 50 duplicate groups at a time
3. Creates the unique index at the end

Usage:
  python apply_migration_batched.py
"""

import sys
import os
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import httpx

SUPABASE_ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
PROJECT_REF = "your-project-ref"

if not SUPABASE_ACCESS_TOKEN:
    print("Missing env var: SUPABASE_ACCESS_TOKEN")
    sys.exit(1)


def execute_sql(sql: str, timeout: int = 120) -> dict:
    res = httpx.post(
        f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query",
        headers={
            "Authorization": f"Bearer {SUPABASE_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"query": sql},
        timeout=timeout,
    )
    if res.status_code in (200, 201):
        return {"ok": True, "data": res.json()}
    else:
        return {"ok": False, "status": res.status_code, "error": res.text[:500]}


def main():
    # Step 1: Create the merge function
    print("Step 1: Creating merge_entities function...")
    create_fn_sql = """
SET statement_timeout = '120s';

CREATE OR REPLACE FUNCTION merge_entities(winner_id uuid, loser_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE relations SET source_entity = winner_id
  WHERE source_entity = loser_id
    AND NOT EXISTS (
      SELECT 1 FROM relations r2
      WHERE r2.source_entity = winner_id
        AND r2.target_entity = relations.target_entity
        AND r2.relation_type = relations.relation_type
    );

  UPDATE relations SET target_entity = winner_id
  WHERE target_entity = loser_id
    AND NOT EXISTS (
      SELECT 1 FROM relations r2
      WHERE r2.target_entity = winner_id
        AND r2.source_entity = relations.source_entity
        AND r2.relation_type = relations.relation_type
    );

  DELETE FROM relations WHERE source_entity = loser_id OR target_entity = loser_id;

  UPDATE observations
  SET entity_ids = array_replace(entity_ids, loser_id, winner_id)
  WHERE loser_id = ANY(entity_ids);

  UPDATE tasks
  SET entity_ids = array_replace(entity_ids, loser_id, winner_id)
  WHERE loser_id = ANY(entity_ids);

  UPDATE entities
  SET aliases = (
    SELECT array_agg(DISTINCT a)
    FROM (
      SELECT unnest(aliases) AS a FROM entities WHERE id = winner_id
      UNION
      SELECT unnest(aliases) FROM entities WHERE id = loser_id
      UNION
      SELECT name FROM entities WHERE id = loser_id
    ) sub
    WHERE a IS NOT NULL
  )
  WHERE id = winner_id;

  DELETE FROM entities WHERE id = loser_id;
END;
$$;
"""
    result = execute_sql(create_fn_sql)
    if not result["ok"]:
        print(f"  FAILED: {result}")
        sys.exit(1)
    print("  Done.")

    # Step 2: Get all duplicate groups
    print("\nStep 2: Finding duplicate entity groups...")
    find_dupes_sql = """
SELECT lower(name) AS lname, count(*) AS cnt
FROM entities
GROUP BY lower(name)
HAVING count(*) > 1
ORDER BY count(*) DESC;
"""
    result = execute_sql(find_dupes_sql)
    if not result["ok"]:
        print(f"  FAILED: {result}")
        sys.exit(1)

    dupes = result["data"]
    total_groups = len(dupes)
    print(f"  Found {total_groups} duplicate groups to merge.")

    # Step 3: Merge in batches
    BATCH_SIZE = 50
    merged = 0

    for batch_start in range(0, total_groups, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, total_groups)
        batch = dupes[batch_start:batch_end]
        batch_names = [d["lname"] for d in batch]

        # Escape single quotes in names
        escaped = [n.replace("'", "''") for n in batch_names]
        name_list = ", ".join(f"'{n}'" for n in escaped)

        merge_sql = f"""
SET statement_timeout = '120s';

DO $$
DECLARE
  rec RECORD;
  winner_id uuid;
  loser RECORD;
BEGIN
  FOR rec IN
    SELECT lower(name) AS lname, array_agg(id ORDER BY id) AS ids
    FROM entities
    WHERE lower(name) IN ({name_list})
    GROUP BY lower(name)
    HAVING count(*) > 1
  LOOP
    SELECT e.id INTO winner_id
    FROM entities e
    LEFT JOIN (
      SELECT source_entity AS eid, count(*) AS cnt FROM relations GROUP BY source_entity
      UNION ALL
      SELECT target_entity AS eid, count(*) AS cnt FROM relations GROUP BY target_entity
    ) r ON r.eid = e.id
    WHERE e.id = ANY(rec.ids)
    GROUP BY e.id
    ORDER BY coalesce(sum(r.cnt), 0) DESC
    LIMIT 1;

    FOR loser IN
      SELECT id FROM entities WHERE id = ANY(rec.ids) AND id != winner_id
    LOOP
      PERFORM merge_entities(winner_id, loser.id);
    END LOOP;
  END LOOP;
END;
$$;
"""
        print(f"  Merging batch {batch_start+1}-{batch_end} of {total_groups}...", end=" ", flush=True)
        start = time.time()
        result = execute_sql(merge_sql, timeout=180)
        elapsed = time.time() - start

        if result["ok"]:
            merged += len(batch)
            print(f"OK ({elapsed:.1f}s)")
        else:
            print(f"FAILED ({elapsed:.1f}s)")
            print(f"    Error: {result.get('error', '')[:200]}")
            # Continue with next batch rather than aborting
            continue

    print(f"\n  Merged {merged}/{total_groups} groups.")

    # Step 4: Drop old index (already dropped in 005, but be safe)
    print("\nStep 4: Dropping old index if exists...")
    result = execute_sql("DROP INDEX IF EXISTS idx_entities_name_type_unique;")
    print(f"  {'Done' if result['ok'] else 'FAILED: ' + str(result)}")

    # Step 5: Create name-only unique index
    print("\nStep 5: Creating name-only unique index...")
    result = execute_sql("""
SET statement_timeout = '60s';
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_name_unique ON entities (lower(name));
""")
    if result["ok"]:
        print("  Done.")
    else:
        print(f"  FAILED: {result.get('error', '')[:300]}")
        print("  (May need manual cleanup of remaining duplicates)")

    # Step 6: Add CHECK constraint
    print("\nStep 6: Adding entity type CHECK constraint...")
    result = execute_sql("""
ALTER TABLE entities DROP CONSTRAINT IF EXISTS entities_type_check;
ALTER TABLE entities ADD CONSTRAINT entities_type_check
  CHECK (entity_type IN ('person', 'organization', 'project', 'concept', 'tool', 'content'));
""")
    if result["ok"]:
        print("  Done.")
    else:
        print(f"  FAILED: {result.get('error', '')[:300]}")

    print("\nMigration 006 complete!")


if __name__ == "__main__":
    main()
