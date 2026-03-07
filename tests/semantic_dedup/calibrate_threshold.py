"""
Threshold Calibration for Semantic Dedup

Samples relation pairs at different cosine similarity bands and
shows the actual text so we can visually verify where
"semantically same" vs "semantically different" falls.

Also analyzes orphaned observations and how to re-link them.

Usage:
  python calibrate_threshold.py
"""

import sys
import os
import json
import random

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import httpx
import numpy as np

SUPABASE_ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
EMBED_MODEL = os.getenv("OPENROUTER_EMBED_MODEL", "openai/text-embedding-3-small")
PROJECT_REF = "your-project-ref"


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
        return res.json()
    return [{"error": res.text[:300]}]


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


def main():
    # ============================================
    # PART 1: Relation Threshold Calibration
    # ============================================
    print("=" * 60)
    print("PART 1: RELATION SIMILARITY THRESHOLD CALIBRATION")
    print("=" * 60)

    # Get ALL duplicate pairs with descriptions
    data = run_sql("""
        SELECT r.id, r.relation_type, r.description,
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
        ORDER BY s.name, t.name;
    """)

    # Group by pair
    pairs = {}
    for row in data:
        key = f"{row['source_name']} --> {row['target_name']}"
        pairs.setdefault(key, []).append(row)

    # Sample up to 100 pairs randomly
    pair_items = list(pairs.items())
    random.seed(42)
    sampled = random.sample(pair_items, min(100, len(pair_items)))

    # Compute all pairwise similarities within each pair
    all_comparisons = []

    for pair_name, rels in sampled:
        if len(rels) < 2:
            continue

        texts = [
            f"[{r['relation_type']}] {r['description'] or r['relation_type']}"
            for r in rels
        ]
        try:
            embeddings = embed_batch(texts)
        except Exception as e:
            print(f"  Embed failed for {pair_name}: {e}")
            continue

        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                sim = cosine_sim(embeddings[i], embeddings[j])
                all_comparisons.append({
                    "pair": pair_name,
                    "a_type": rels[i]["relation_type"],
                    "a_desc": (rels[i]["description"] or "")[:120],
                    "b_type": rels[j]["relation_type"],
                    "b_desc": (rels[j]["description"] or "")[:120],
                    "similarity": round(sim, 4),
                })

    # Sort by similarity
    all_comparisons.sort(key=lambda x: x["similarity"])

    # Bin into similarity bands
    bands = {
        "0.50-0.60": [],
        "0.60-0.70": [],
        "0.70-0.75": [],
        "0.75-0.80": [],
        "0.80-0.85": [],
        "0.85-0.90": [],
        "0.90-0.95": [],
        "0.95-1.00": [],
    }

    for comp in all_comparisons:
        sim = comp["similarity"]
        if sim < 0.60:
            bands["0.50-0.60"].append(comp)
        elif sim < 0.70:
            bands["0.60-0.70"].append(comp)
        elif sim < 0.75:
            bands["0.70-0.75"].append(comp)
        elif sim < 0.80:
            bands["0.75-0.80"].append(comp)
        elif sim < 0.85:
            bands["0.80-0.85"].append(comp)
        elif sim < 0.90:
            bands["0.85-0.90"].append(comp)
        elif sim < 0.95:
            bands["0.90-0.95"].append(comp)
        else:
            bands["0.95-1.00"].append(comp)

    print(f"\nTotal pairwise comparisons: {len(all_comparisons)}")
    print(f"Sampled {len(sampled)} entity pairs\n")

    print("DISTRIBUTION:")
    print(f"{'Band':<12} {'Count':>6} {'% of total':>10}")
    print("-" * 30)
    for band, items in bands.items():
        pct = len(items) / max(len(all_comparisons), 1) * 100
        print(f"{band:<12} {len(items):>6} {pct:>9.1f}%")

    # Show 3 random examples from each band
    print("\n" + "=" * 60)
    print("EXAMPLES PER BAND (3 random samples each)")
    print("=" * 60)

    for band, items in bands.items():
        if not items:
            continue
        print(f"\n--- Band {band} ({len(items)} pairs) ---")
        examples = random.sample(items, min(3, len(items)))
        for ex in examples:
            print(f"\n  {ex['pair']} (sim={ex['similarity']})")
            print(f"    A: [{ex['a_type']}] {ex['a_desc']}")
            print(f"    B: [{ex['b_type']}] {ex['b_desc']}")

    # ============================================
    # PART 2: Orphaned Observation Analysis
    # ============================================
    print("\n\n" + "=" * 60)
    print("PART 2: ORPHANED OBSERVATION ANALYSIS")
    print("=" * 60)

    orphan_data = run_sql("""
        SELECT id, content, observation_type, source_id
        FROM observations
        WHERE entity_ids IS NULL OR array_length(entity_ids, 1) IS NULL
        ORDER BY random()
        LIMIT 30;
    """)

    print(f"\nSample of 30 orphaned observations:\n")
    for i, obs in enumerate(orphan_data):
        print(f"  {i+1}. [{obs['observation_type']}] {obs['content'][:150]}")

    # Check if orphans have sources
    orphan_source_stats = run_sql("""
        SELECT
            count(*) AS total_orphans,
            count(source_id) AS has_source,
            count(*) - count(source_id) AS no_source
        FROM observations
        WHERE entity_ids IS NULL OR array_length(entity_ids, 1) IS NULL;
    """)
    print(f"\nOrphan source stats:")
    for row in orphan_source_stats:
        for k, v in row.items():
            print(f"  {k}: {v}")

    # Save full results
    output = {
        "total_comparisons": len(all_comparisons),
        "distribution": {band: len(items) for band, items in bands.items()},
        "examples_per_band": {
            band: random.sample(items, min(5, len(items)))
            for band, items in bands.items()
            if items
        },
        "orphan_sample": orphan_data[:30],
    }

    output_file = os.path.join(os.path.dirname(__file__), "threshold_calibration.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nFull results saved to {output_file}")


if __name__ == "__main__":
    main()
