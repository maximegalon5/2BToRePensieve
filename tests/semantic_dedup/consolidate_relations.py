"""
Consolidate semantically duplicate relations using embedding similarity.

For each entity pair with >1 relation:
1. Embed all relation descriptions
2. Cluster by cosine similarity > 0.85
3. Keep the best relation per cluster (longest description), delete the rest

Usage:
  python consolidate_relations.py --dry-run   # Preview changes
  python consolidate_relations.py             # Apply changes
"""

import sys
import os
import json
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import httpx
import numpy as np

SUPABASE_ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
EMBED_MODEL = os.getenv("OPENROUTER_EMBED_MODEL", "openai/text-embedding-3-small")
PROJECT_REF = "your-project-ref"
SIMILARITY_THRESHOLD = 0.85

DRY_RUN = "--dry-run" in sys.argv


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


def embed_batch(texts):
    if not texts:
        return []
    res = httpx.post(
        "https://openrouter.ai/api/v1/embeddings",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": EMBED_MODEL, "input": texts},
        timeout=30,
    )
    data = res.json()
    return [d["embedding"] for d in data["data"]]


def cosine_sim(a, b):
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def cluster_and_pick_best(rels, embeddings):
    """
    Cluster relations by cosine > SIMILARITY_THRESHOLD.
    Keep the best (longest description) from each cluster.
    Returns (keep_ids, delete_ids).
    """
    n = len(rels)
    assigned = [False] * n
    clusters = []

    for i in range(n):
        if assigned[i]:
            continue
        cluster = [i]
        assigned[i] = True
        for j in range(i + 1, n):
            if assigned[j]:
                continue
            sim = cosine_sim(embeddings[i], embeddings[j])
            if sim >= SIMILARITY_THRESHOLD:
                cluster.append(j)
                assigned[j] = True
        clusters.append(cluster)

    keep_ids = []
    delete_ids = []

    for cluster in clusters:
        # Pick the one with the longest description as the best
        best_idx = max(cluster, key=lambda idx: len(rels[idx]["description"] or ""))
        keep_ids.append(rels[best_idx]["id"])
        for idx in cluster:
            if idx != best_idx:
                delete_ids.append(rels[idx]["id"])

    return keep_ids, delete_ids


def main():
    print(f"{'[DRY RUN] ' if DRY_RUN else ''}Consolidating duplicate relations (threshold={SIMILARITY_THRESHOLD})...\n")

    # Step 1: Get all duplicate pairs with full relation details
    print("Step 1: Finding duplicate entity pairs...")
    result = run_sql("""
        SELECT r.id, r.source_entity, r.target_entity, r.relation_type, r.description,
               s.name AS source_name, t.name AS target_name
        FROM relations r
        JOIN entities s ON s.id = r.source_entity
        JOIN entities t ON t.id = r.target_entity
        WHERE (r.source_entity, r.target_entity) IN (
            SELECT source_entity, target_entity
            FROM relations
            GROUP BY source_entity, target_entity
            HAVING count(*) > 1
        )
        ORDER BY s.name, t.name, r.relation_type;
    """)

    if not result["ok"]:
        print(f"FAILED: {result['error']}")
        sys.exit(1)

    rows = result["data"]
    print(f"  Found {len(rows)} relations across duplicate pairs")

    # Group by pair
    pairs = {}
    for row in rows:
        key = (row["source_entity"], row["target_entity"])
        pairs.setdefault(key, []).append(row)

    print(f"  {len(pairs)} entity pairs to process\n")

    # Step 2: Process each pair — embed, cluster, pick best
    all_delete_ids = []
    total_before = 0
    total_after = 0
    consolidation_log = []

    # Sort by count descending for most impactful first
    sorted_pairs = sorted(pairs.items(), key=lambda x: -len(x[1]))

    # Process in embedding batches (batch texts across multiple pairs)
    EMBED_BATCH_SIZE = 50
    text_buffer = []
    pair_buffer = []  # (pair_key, rels, text_indices)

    def flush_buffer():
        nonlocal total_before, total_after
        if not text_buffer:
            return

        try:
            embeddings = embed_batch(text_buffer)
        except Exception as e:
            print(f"  Embed batch failed: {e}")
            text_buffer.clear()
            pair_buffer.clear()
            return

        for pair_key, rels, text_indices in pair_buffer:
            pair_embeddings = [embeddings[idx] for idx in text_indices]
            keep_ids, delete_ids = cluster_and_pick_best(rels, pair_embeddings)

            total_before += len(rels)
            total_after += len(keep_ids)
            all_delete_ids.extend(delete_ids)

            if delete_ids:
                pair_name = f"{rels[0]['source_name']} --> {rels[0]['target_name']}"
                kept_types = [r["relation_type"] for r in rels if r["id"] in keep_ids]
                deleted_types = [r["relation_type"] for r in rels if r["id"] in delete_ids]
                print(f"  {pair_name}: {len(rels)} -> {len(keep_ids)} (keep: {kept_types}, drop: {deleted_types})")

                consolidation_log.append({
                    "pair": pair_name,
                    "before": len(rels),
                    "after": len(keep_ids),
                    "kept_types": kept_types,
                    "dropped_types": deleted_types,
                })

        text_buffer.clear()
        pair_buffer.clear()

    for i, (pair_key, rels) in enumerate(sorted_pairs):
        # Build texts for this pair
        texts = [
            f"[{r['relation_type']}] {r['description'] or r['relation_type']}"
            for r in rels
        ]
        text_indices = list(range(len(text_buffer), len(text_buffer) + len(texts)))
        text_buffer.extend(texts)
        pair_buffer.append((pair_key, rels, text_indices))

        # Flush when buffer is full
        if len(text_buffer) >= EMBED_BATCH_SIZE:
            flush_buffer()

        # Progress
        if (i + 1) % 100 == 0:
            print(f"  --- Processed {i + 1}/{len(sorted_pairs)} pairs ---")

    # Flush remaining
    flush_buffer()

    print(f"\n{'='*50}")
    print(f"SUMMARY")
    print(f"{'='*50}")
    print(f"  Relations in duplicate pairs (before): {total_before}")
    print(f"  Relations after consolidation:         {total_after}")
    print(f"  Relations to delete:                   {len(all_delete_ids)}")
    print(f"  Reduction:                             {len(all_delete_ids)}/{total_before} ({len(all_delete_ids)/max(total_before,1)*100:.1f}%)")

    if DRY_RUN:
        print(f"\n[DRY RUN] Would delete {len(all_delete_ids)} relations. Run without --dry-run to apply.")
        plan_file = os.path.join(os.path.dirname(__file__), "consolidation_plan.json")
        with open(plan_file, "w", encoding="utf-8") as f:
            json.dump({
                "threshold": SIMILARITY_THRESHOLD,
                "delete_ids": all_delete_ids,
                "total_deleted": len(all_delete_ids),
                "total_before": total_before,
                "total_after": total_after,
                "log": consolidation_log,
            }, f, indent=2, ensure_ascii=False)
        print(f"Plan saved to {plan_file}")
        return

    # Step 3: Delete redundant relations in batches
    if all_delete_ids:
        print(f"\nStep 3: Deleting {len(all_delete_ids)} redundant relations...")
        BATCH = 100
        for i in range(0, len(all_delete_ids), BATCH):
            batch_ids = all_delete_ids[i:i + BATCH]
            id_list = ", ".join(f"'{uid}'" for uid in batch_ids)
            result = run_sql(f"DELETE FROM relations WHERE id IN ({id_list});")
            if result["ok"]:
                print(f"  Deleted batch {i // BATCH + 1} ({len(batch_ids)} relations)")
            else:
                print(f"  FAILED batch {i // BATCH + 1}: {result['error'][:200]}")
            time.sleep(0.2)

        print(f"\nDone! Deleted {len(all_delete_ids)} redundant relations.")
    else:
        print("\nNo redundant relations to delete.")


if __name__ == "__main__":
    main()
