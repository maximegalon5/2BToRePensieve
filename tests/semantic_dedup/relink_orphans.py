"""
Re-link orphaned observations to entities.

Orphaned observations have content and embeddings but no entity_ids.
For each orphan:
1. Use its stored embedding to find the top-3 matching entities
2. If any entity matches with cosine > 0.5, link the observation to it
3. Skip if no match (observation may be too generic)

Uses cursor-based pagination (id > last_id) to avoid offset drift
when rows are updated mid-iteration.

Usage:
  python relink_orphans.py --dry-run   # Preview changes
  python relink_orphans.py             # Apply changes
"""

import sys
import os
import json
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import httpx
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
PROJECT_REF = "your-project-ref"

ENTITY_MATCH_THRESHOLD = 0.5
TOP_K_ENTITIES = 3

DRY_RUN = "--dry-run" in sys.argv

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def run_sql(sql, timeout=60):
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
    return {"ok": False, "error": res.text[:500]}


def main():
    print(f"{'[DRY RUN] ' if DRY_RUN else ''}Re-linking orphaned observations...\n")

    # Step 1: Count orphans
    result = run_sql("""
        SELECT count(*) AS cnt
        FROM observations
        WHERE entity_ids IS NULL OR array_length(entity_ids, 1) IS NULL;
    """)
    total_orphans = int(result["data"][0]["cnt"])
    print(f"Total orphaned observations: {total_orphans}\n")

    if total_orphans == 0:
        print("No orphans to process.")
        return

    # Step 2: Process using cursor-based pagination (id > last_id)
    BATCH_SIZE = 50
    linked = 0
    skipped = 0
    failed = 0
    processed = 0
    link_log = []
    last_id = "00000000-0000-0000-0000-000000000000"  # UUID min

    while True:
        # Fetch batch of orphans ordered by id, cursor after last_id
        try:
            batch = (
                supabase.from_("observations")
                .select("id, content, embedding")
                .or_("entity_ids.is.null,entity_ids.eq.{}")
                .gt("id", last_id)
                .order("id")
                .limit(BATCH_SIZE)
                .execute()
            )
        except Exception as e:
            print(f"  Fetch failed after id {last_id}: {e}")
            failed += BATCH_SIZE
            break

        rows = batch.data or []
        if not rows:
            break  # No more orphans

        for obs in rows:
            last_id = obs["id"]  # Advance cursor
            processed += 1

            emb = obs.get("embedding")
            if not emb:
                skipped += 1
                continue

            # Convert embedding if string
            if isinstance(emb, str):
                emb = json.loads(emb)
            if not isinstance(emb, list) or len(emb) == 0:
                skipped += 1
                continue

            # Search for matching entities
            try:
                result = supabase.rpc("search_similar_entities", {
                    "query_embedding": emb,
                    "match_count": TOP_K_ENTITIES,
                    "similarity_threshold": ENTITY_MATCH_THRESHOLD,
                }).execute()
                matches = result.data or []
            except Exception as e:
                # Timeout or other error
                skipped += 1
                continue

            if not matches:
                skipped += 1
                continue

            # Link to all matching entities above threshold
            entity_ids = [m["id"] for m in matches]
            entity_names = [m["name"] for m in matches]
            best_sim = matches[0].get("similarity", 0)

            if DRY_RUN:
                if linked < 20:  # Show first 20
                    print(f"  Would link: \"{obs['content'][:80]}...\"")
                    print(f"    -> {entity_names} (best sim={best_sim:.3f})")
                linked += 1
            else:
                try:
                    supabase.from_("observations").update(
                        {"entity_ids": entity_ids}
                    ).eq("id", obs["id"]).execute()
                    linked += 1
                except Exception as e:
                    failed += 1
                    continue

                if linked % 100 == 0:
                    print(f"  Linked {linked} observations so far...")

            link_log.append({
                "obs_id": obs["id"],
                "content_preview": obs["content"][:80],
                "matched_entities": entity_names,
                "best_similarity": round(best_sim, 4),
            })

        # Brief pause between batches
        time.sleep(0.2)

        # Progress
        print(f"  Processed {processed} "
              f"(linked={linked}, skipped={skipped}, failed={failed})")

    print(f"\n{'='*50}")
    print(f"SUMMARY")
    print(f"{'='*50}")
    print(f"  Total orphans:   {total_orphans}")
    print(f"  Processed:       {processed}")
    print(f"  Linked:          {linked}")
    print(f"  Skipped:         {skipped}")
    print(f"  Failed:          {failed}")
    print(f"  Link rate:       {linked/max(processed,1)*100:.1f}%")

    if DRY_RUN:
        print(f"\n[DRY RUN] Would link {linked} observations. Run without --dry-run to apply.")

    # Save log
    log_file = os.path.join(os.path.dirname(__file__), "relink_log.json")
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump({
            "total_orphans": total_orphans,
            "processed": processed,
            "linked": linked,
            "skipped": skipped,
            "failed": failed,
            "sample": link_log[:100],
        }, f, indent=2, ensure_ascii=False)
    print(f"Log saved to {log_file}")


if __name__ == "__main__":
    main()
