"""
Semantic Dedup QA Snapshot

Measures precision and efficiency of the knowledge graph:
1. Relation uniqueness ratio
2. Semantic redundancy rate (embedding-based, sampled)
3. Observation cluster ratio (embedding-based, sampled)
4. Orphan rate
5. Retrieval noise ratio (search-based, sampled)
6. Storage efficiency

Usage:
  python run_qa_snapshot.py before
  python run_qa_snapshot.py after
"""

import sys
import os
import json
import time
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import httpx
from supabase import create_client
import numpy as np

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
EMBED_MODEL = os.getenv("OPENROUTER_EMBED_MODEL", "openai/text-embedding-3-small")
PROJECT_REF = "your-project-ref"

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


# --- Metric 1: Relation Uniqueness Ratio ---

def measure_relation_uniqueness():
    data = run_sql("""
        SELECT count(*) AS total_pairs,
               sum(cnt) AS total_relations,
               sum(CASE WHEN cnt > 1 THEN cnt ELSE 0 END) AS dup_relations,
               sum(CASE WHEN cnt > 1 THEN 1 ELSE 0 END) AS dup_pairs
        FROM (
            SELECT source_entity, target_entity, count(*) AS cnt
            FROM relations GROUP BY source_entity, target_entity
        ) sub;
    """)
    row = data[0]
    total_pairs = int(row["total_pairs"])
    total_rels = int(row["total_relations"])
    dup_rels = int(row["dup_relations"])
    dup_pairs = int(row["dup_pairs"])

    return {
        "total_relations": total_rels,
        "unique_pairs": total_pairs,
        "duplicate_pairs": dup_pairs,
        "duplicate_relations": dup_rels,
        "uniqueness_ratio": round(total_pairs / max(total_rels, 1), 4),
    }


# --- Metric 2: Semantic Redundancy Rate (sampled) ---

def measure_semantic_redundancy(sample_size=50):
    """For entity pairs with >1 relation, embed descriptions and check cosine similarity."""
    # Get top duplicate pairs with their descriptions
    data = run_sql(f"""
        SELECT s.name AS source_name, t.name AS target_name,
               r.relation_type, r.description
        FROM relations r
        JOIN entities s ON s.id = r.source_entity
        JOIN entities t ON t.id = r.target_entity
        WHERE (r.source_entity, r.target_entity) IN (
            SELECT source_entity, target_entity
            FROM relations
            GROUP BY source_entity, target_entity
            HAVING count(*) > 1
            ORDER BY count(*) DESC
            LIMIT {sample_size}
        )
        ORDER BY s.name, t.name, r.relation_type;
    """)

    # Group by pair
    pairs = {}
    for row in data:
        key = f"{row['source_name']}-->{row['target_name']}"
        pairs.setdefault(key, []).append(row)

    redundant_count = 0
    total_multi_rels = 0
    pair_details = []

    for pair_key, rels in pairs.items():
        if len(rels) < 2:
            continue

        # Embed the descriptions
        texts = [
            f"[{r['relation_type']}] {r['description'] or r['relation_type']}"
            for r in rels
        ]
        try:
            embeddings = embed_batch(texts)
        except Exception as e:
            print(f"    Embed failed for {pair_key}: {e}")
            continue

        # Find max pairwise cosine similarity
        max_sim = 0
        redundant_in_pair = 0
        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                sim = cosine_sim(embeddings[i], embeddings[j])
                max_sim = max(max_sim, sim)
                if sim > 0.85:
                    redundant_in_pair += 1

        total_multi_rels += len(rels)
        redundant_count += redundant_in_pair

        pair_details.append({
            "pair": pair_key,
            "relation_count": len(rels),
            "max_similarity": round(max_sim, 4),
            "redundant_pairs": redundant_in_pair,
            "types": [r["relation_type"] for r in rels],
        })

    # Sort by max similarity descending
    pair_details.sort(key=lambda x: -x["max_similarity"])

    return {
        "sampled_pairs": len(pairs),
        "total_multi_relations": total_multi_rels,
        "redundant_relation_pairs": redundant_count,
        "redundancy_rate": round(redundant_count / max(total_multi_rels, 1), 4),
        "top_redundant": pair_details[:15],
    }


# --- Metric 3: Observation Cluster Ratio (sampled) ---

def measure_observation_clusters(sample_entity_count=10):
    """For top entities by observation count, check how many observations are near-duplicates."""
    # Get top entities
    top_entities = run_sql(f"""
        SELECT e.id, e.name, count(*) AS obs_count
        FROM observations o, unnest(o.entity_ids) AS eid
        JOIN entities e ON e.id = eid
        GROUP BY e.id, e.name
        ORDER BY count(*) DESC
        LIMIT {sample_entity_count};
    """)

    results = []
    total_obs = 0
    total_unique = 0

    for entity in top_entities:
        eid = entity["id"]
        ename = entity["name"]

        # Get observations for this entity (limit to 50 for cost)
        obs_data = (
            supabase.from_("observations")
            .select("id, content, embedding")
            .contains("entity_ids", [eid])
            .limit(50)
            .execute()
        )
        obs_list = obs_data.data or []
        if len(obs_list) < 2:
            continue

        # Use stored embeddings to find clusters
        embeddings = []
        contents = []
        for o in obs_list:
            if o.get("embedding"):
                emb = o["embedding"]
                # Supabase may return embeddings as strings — convert to float list
                if isinstance(emb, str):
                    emb = json.loads(emb)
                if isinstance(emb, list) and len(emb) > 0:
                    embeddings.append([float(x) for x in emb])
                    contents.append(o["content"][:100])

        if len(embeddings) < 2:
            continue

        # Count unique observations (not cosine > 0.90 with any other)
        unique_mask = [True] * len(embeddings)
        dup_examples = []
        for i in range(len(embeddings)):
            if not unique_mask[i]:
                continue
            for j in range(i + 1, len(embeddings)):
                if not unique_mask[j]:
                    continue
                sim = cosine_sim(embeddings[i], embeddings[j])
                if sim > 0.90:
                    unique_mask[j] = False
                    if len(dup_examples) < 3:
                        dup_examples.append({
                            "a": contents[i],
                            "b": contents[j],
                            "similarity": round(sim, 4),
                        })

        unique_count = sum(unique_mask)
        total_obs += len(embeddings)
        total_unique += unique_count

        results.append({
            "entity": ename,
            "observations_sampled": len(embeddings),
            "semantically_unique": unique_count,
            "cluster_ratio": round(unique_count / len(embeddings), 4),
            "duplicate_examples": dup_examples,
        })

    return {
        "entities_sampled": len(results),
        "total_observations_sampled": total_obs,
        "total_unique": total_unique,
        "overall_cluster_ratio": round(total_unique / max(total_obs, 1), 4),
        "per_entity": results,
    }


# --- Metric 4: Orphan Rate ---

def measure_orphan_rate():
    data = run_sql("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE array_length(entity_ids, 1) > 0) AS linked,
               count(*) FILTER (WHERE entity_ids IS NULL OR array_length(entity_ids, 1) IS NULL) AS orphaned
        FROM observations;
    """)
    row = data[0]
    total = int(row["total"])
    orphaned = int(row["orphaned"])
    return {
        "total_observations": total,
        "linked": int(row["linked"]),
        "orphaned": orphaned,
        "orphan_rate": round(orphaned / max(total, 1), 4),
    }


# --- Metric 5: Retrieval Noise Ratio (sampled) ---

SEARCH_QUERIES = [
    "What does ExampleOrg do?",
    "tell me about SATS",
    "what supplements does ExampleOrg offer?",
    "cortisol and stress",
    "Python programming tools",
]


def embed_query(text):
    res = httpx.post(
        "https://openrouter.ai/api/v1/embeddings",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": EMBED_MODEL, "input": [text]},
        timeout=30,
    )
    return res.json()["data"][0]["embedding"]


def measure_retrieval_noise():
    """Search for known queries, check how many top-10 results are near-duplicates of each other."""
    results = []

    for query in SEARCH_QUERIES:
        try:
            embedding = embed_query(query)
            search = supabase.rpc("search_knowledge", {
                "query_embedding": embedding,
                "match_count": 10,
                "filter_entity_type": None,
                "filter_observation_type": None,
            }).execute()
            rows = search.data or []
        except Exception as e:
            print(f"    Search failed for '{query}': {e}")
            results.append({
                "query": query,
                "results": 0,
                "unique_results": 0,
                "noise_ratio": 0,
                "error": str(e),
            })
            continue

        if len(rows) < 2:
            results.append({
                "query": query,
                "results": len(rows),
                "unique_results": len(rows),
                "noise_ratio": 0,
            })
            continue

        # Embed result contents and check pairwise similarity
        texts = []
        for r in rows:
            if r.get("result_type") == "entity":
                texts.append(f"{r.get('name', '')}: {r.get('description', '')}")
            else:
                texts.append(r.get("content", "")[:200])

        try:
            embeddings = embed_batch(texts)
        except Exception:
            results.append({
                "query": query,
                "results": len(rows),
                "unique_results": len(rows),
                "noise_ratio": 0,
            })
            continue

        unique_mask = [True] * len(embeddings)
        for i in range(len(embeddings)):
            if not unique_mask[i]:
                continue
            for j in range(i + 1, len(embeddings)):
                if not unique_mask[j]:
                    continue
                sim = cosine_sim(embeddings[i], embeddings[j])
                if sim > 0.90:
                    unique_mask[j] = False

        unique_count = sum(unique_mask)
        results.append({
            "query": query,
            "results": len(rows),
            "unique_results": unique_count,
            "noise_ratio": round(1 - unique_count / len(rows), 4),
        })
        print(f"  '{query}': {len(rows)} results, {unique_count} unique, noise={1 - unique_count / len(rows):.1%}")

    avg_noise = sum(r["noise_ratio"] for r in results) / max(len(results), 1)
    return {
        "queries": results,
        "avg_noise_ratio": round(avg_noise, 4),
    }


# --- Metric 6: Storage Efficiency ---

def measure_storage_efficiency(relation_uniqueness, orphan_rate):
    total_stored = relation_uniqueness["total_relations"] + orphan_rate["total_observations"]
    estimated_unique = relation_uniqueness["unique_pairs"] + orphan_rate["linked"]
    return {
        "total_stored_rows": total_stored,
        "estimated_useful_rows": estimated_unique,
        "storage_efficiency": round(estimated_unique / max(total_stored, 1), 4),
    }


# --- Main ---

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("before", "after"):
        print("Usage: python run_qa_snapshot.py before|after")
        sys.exit(1)

    label = sys.argv[1]
    output_file = os.path.join(os.path.dirname(__file__), f"qa_snapshot_{label}.json")

    print(f"\n=== Capturing '{label}' QA snapshot ===\n")

    print("1. Measuring relation uniqueness...")
    rel_unique = measure_relation_uniqueness()
    print(f"   Ratio: {rel_unique['uniqueness_ratio']} ({rel_unique['duplicate_pairs']} duplicate pairs)")

    print("2. Measuring semantic redundancy (sampled, top 50 pairs)...")
    sem_redundancy = measure_semantic_redundancy(sample_size=50)
    print(f"   Redundancy rate: {sem_redundancy['redundancy_rate']} across {sem_redundancy['sampled_pairs']} pairs")

    print("3. Measuring observation clusters (sampled, top 10 entities)...")
    obs_clusters = measure_observation_clusters(sample_entity_count=10)
    print(f"   Cluster ratio: {obs_clusters['overall_cluster_ratio']} ({obs_clusters['total_unique']}/{obs_clusters['total_observations_sampled']} unique)")

    print("4. Measuring orphan rate...")
    orphans = measure_orphan_rate()
    print(f"   Orphan rate: {orphans['orphan_rate']} ({orphans['orphaned']}/{orphans['total_observations']})")

    print("5. Measuring retrieval noise (5 queries)...")
    noise = measure_retrieval_noise()
    print(f"   Avg noise ratio: {noise['avg_noise_ratio']}")

    print("6. Computing storage efficiency...")
    efficiency = measure_storage_efficiency(rel_unique, orphans)
    print(f"   Efficiency: {efficiency['storage_efficiency']}")

    snapshot = {
        "label": label,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "relation_uniqueness": rel_unique,
        "semantic_redundancy": sem_redundancy,
        "observation_clusters": obs_clusters,
        "orphan_rate": orphans,
        "retrieval_noise": noise,
        "storage_efficiency": efficiency,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    print(f"\nSnapshot saved to {output_file}")

    # Print summary table
    print(f"\n{'='*50}")
    print(f"QA MATRIX SUMMARY ({label})")
    print(f"{'='*50}")
    print(f"  Relation uniqueness ratio:   {rel_unique['uniqueness_ratio']}")
    print(f"  Semantic redundancy rate:    {sem_redundancy['redundancy_rate']}")
    print(f"  Observation cluster ratio:   {obs_clusters['overall_cluster_ratio']}")
    print(f"  Orphan rate:                 {orphans['orphan_rate']}")
    print(f"  Retrieval noise ratio:       {noise['avg_noise_ratio']}")
    print(f"  Storage efficiency:          {efficiency['storage_efficiency']}")


if __name__ == "__main__":
    main()
