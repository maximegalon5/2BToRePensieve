"""
Entity Normalization Before/After Test Harness

Captures a snapshot of entity health metrics and search quality.
Run once before migration, once after, then compare with compare_snapshots.py.

Usage:
  python run_snapshot.py before
  python run_snapshot.py after
"""

import sys
import os
import json
import time
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
EMBED_MODEL = os.getenv("OPENROUTER_EMBED_MODEL", "openai/text-embedding-3-small")

if not all([SUPABASE_URL, SUPABASE_KEY, OPENROUTER_KEY]):
    print("Missing env vars: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, OPENROUTER_API_KEY")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

import httpx

def embed_query(text: str) -> list[float]:
    res = httpx.post(
        "https://openrouter.ai/api/v1/embeddings",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
        json={"model": EMBED_MODEL, "input": [text]},
        timeout=30,
    )
    return res.json()["data"][0]["embedding"]


def search_brain(query: str, limit: int = 10) -> list[dict]:
    """Raw similarity search (no reranking) to get consistent baseline."""
    embedding = embed_query(query)
    try:
        result = supabase.rpc("search_knowledge", {
            "query_embedding": embedding,
            "match_count": limit,
            "filter_entity_type": None,
            "filter_observation_type": None,
        }).execute()
        return result.data or []
    except Exception as e:
        print(f"    Search failed: {e}")
        return []


# --- Metric 1: Entity fragmentation ---

def measure_fragmentation() -> dict:
    """Count entities that share a name (case-insensitive) across multiple rows."""
    # Get all entities with name and type
    all_entities = []
    offset = 0
    batch = 1000
    while True:
        result = supabase.from_("entities").select("id, name, entity_type").range(offset, offset + batch - 1).execute()
        if not result.data:
            break
        all_entities.extend(result.data)
        if len(result.data) < batch:
            break
        offset += batch

    # Group by lowercase name
    by_name: dict[str, list[dict]] = {}
    for e in all_entities:
        key = e["name"].lower().strip()
        by_name.setdefault(key, []).append(e)

    duplicates = {name: entries for name, entries in by_name.items() if len(entries) > 1}

    return {
        "total_entities": len(all_entities),
        "unique_names": len(by_name),
        "fragmented_names": len(duplicates),
        "fragmented_entities": sum(len(v) for v in duplicates.values()),
        "top_fragmented": sorted(
            [{"name": name, "count": len(entries), "types": [e["entity_type"] for e in entries]}
             for name, entries in duplicates.items()],
            key=lambda x: -x["count"]
        )[:20],
    }


# --- Metric 2: Entity type distribution ---

def measure_type_distribution() -> dict:
    all_entities = []
    offset = 0
    batch = 1000
    while True:
        result = supabase.from_("entities").select("entity_type").range(offset, offset + batch - 1).execute()
        if not result.data:
            break
        all_entities.extend(result.data)
        if len(result.data) < batch:
            break
        offset += batch

    types: dict[str, int] = {}
    for e in all_entities:
        t = e["entity_type"]
        types[t] = types.get(t, 0) + 1

    return {
        "distinct_types": len(types),
        "distribution": dict(sorted(types.items(), key=lambda x: -x[1])),
    }


# --- Metric 3: Graph connectivity ---

def measure_connectivity() -> dict:
    ent_count = supabase.from_("entities").select("id", count="exact", head=True).execute().count or 0
    rel_count = supabase.from_("relations").select("id", count="exact", head=True).execute().count or 0
    obs_count = supabase.from_("observations").select("id", count="exact", head=True).execute().count or 0

    return {
        "entities": ent_count,
        "relations": rel_count,
        "observations": obs_count,
        "avg_relations_per_entity": round(rel_count / max(ent_count, 1), 2),
        "avg_observations_per_entity": round(obs_count / max(ent_count, 1), 2),
    }


# --- Metric 4: Search completeness for known fragmented entities ---

SEARCH_QUERIES = [
    {"query": "ExampleOrg", "description": "Fragmented across organization/project/brand"},
    {"query": "SATS", "description": "Fragmented across concept/project"},
    {"query": "User", "description": "Fragmented as User/User"},
    {"query": "ProductA", "description": "Product entity"},
    {"query": "ProductB", "description": "Product entity"},
    {"query": "what is ExampleOrg and what do I know about it", "description": "Natural language query about fragmented entity"},
    {"query": "tell me about SATS", "description": "Natural language query about fragmented entity"},
    {"query": "Python programming", "description": "Likely split across tool/concept types"},
    {"query": "React framework", "description": "Likely split across tool/concept types"},
    {"query": "PersonB", "description": "Person entity - check for fragments"},
]

def measure_search_completeness() -> list[dict]:
    results = []
    for sq in SEARCH_QUERIES:
        start = time.time()
        search_results = search_brain(sq["query"], limit=10)
        elapsed = time.time() - start

        # Count unique entity IDs referenced in results
        entity_ids_seen = set()
        for r in search_results:
            if r.get("result_type") == "entity":
                entity_ids_seen.add(r["result_id"])
            if r.get("entity_ids"):
                for eid in r["entity_ids"]:
                    entity_ids_seen.add(eid)

        results.append({
            "query": sq["query"],
            "description": sq["description"],
            "result_count": len(search_results),
            "unique_entities_referenced": len(entity_ids_seen),
            "avg_similarity": round(sum(r.get("similarity", 0) for r in search_results) / max(len(search_results), 1), 4),
            "top_3": [
                {
                    "type": r.get("result_type"),
                    "name": r.get("name"),
                    "content": (r.get("content") or "")[:100],
                    "similarity": round(r.get("similarity", 0), 4),
                }
                for r in search_results[:3]
            ],
            "latency_s": round(elapsed, 2),
        })
        print(f"  {sq['query']}: {len(search_results)} results, {len(entity_ids_seen)} unique entities, {elapsed:.1f}s")

    return results


# --- Main ---

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("before", "after"):
        print("Usage: python run_snapshot.py before|after")
        sys.exit(1)

    label = sys.argv[1]
    output_file = os.path.join(os.path.dirname(__file__), f"snapshot_{label}.json")

    print(f"\n=== Capturing '{label}' snapshot ===\n")

    print("1. Measuring entity fragmentation...")
    fragmentation = measure_fragmentation()
    print(f"   {fragmentation['fragmented_names']} fragmented names ({fragmentation['fragmented_entities']} entities)")

    print("2. Measuring type distribution...")
    types = measure_type_distribution()
    print(f"   {types['distinct_types']} distinct entity types")

    print("3. Measuring graph connectivity...")
    connectivity = measure_connectivity()
    print(f"   {connectivity['entities']} entities, {connectivity['relations']} relations, {connectivity['avg_relations_per_entity']} rels/entity")

    print("4. Running search completeness queries...")
    search = measure_search_completeness()

    snapshot = {
        "label": label,
        "timestamp": datetime.utcnow().isoformat(),
        "fragmentation": fragmentation,
        "type_distribution": types,
        "connectivity": connectivity,
        "search_completeness": search,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    print(f"\nSnapshot saved to {output_file}")


if __name__ == "__main__":
    main()
