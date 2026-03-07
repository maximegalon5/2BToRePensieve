"""
Audit semantic duplication in the knowledge graph.

Measures:
1. Relation duplication: how many entity pairs have multiple relations?
2. Observation density: how many observations per entity?
3. Semantic similarity within same-pair relations and same-entity observations

Usage:
  python audit_semantic_dupes.py
"""

import sys
import os
import json

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import httpx

SUPABASE_ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN")
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


def main():
    print("=== RELATION DUPLICATION (global) ===\n")

    data = run_sql("""
        SELECT count(*) AS total_pairs,
               sum(cnt) AS total_relations,
               sum(CASE WHEN cnt > 1 THEN cnt ELSE 0 END) AS dup_relations,
               sum(CASE WHEN cnt > 1 THEN 1 ELSE 0 END) AS dup_pairs
        FROM (
            SELECT source_entity, target_entity, count(*) AS cnt
            FROM relations
            GROUP BY source_entity, target_entity
        ) sub;
    """)
    for row in data:
        for k, v in row.items():
            print(f"  {k}: {v}")

    print("\n=== RELATIONS PER ENTITY PAIR DISTRIBUTION ===\n")

    data = run_sql("""
        SELECT cnt AS rels_per_pair, count(*) AS num_pairs
        FROM (
            SELECT source_entity, target_entity, count(*) AS cnt
            FROM relations
            GROUP BY source_entity, target_entity
        ) sub
        GROUP BY cnt
        ORDER BY cnt;
    """)
    for row in data:
        print(f"  {row['rels_per_pair']} relations: {row['num_pairs']} pairs")

    print("\n=== OBSERVATION STATS ===\n")

    data = run_sql("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE array_length(entity_ids, 1) > 0) AS linked,
               count(*) FILTER (WHERE entity_ids IS NULL OR array_length(entity_ids, 1) IS NULL) AS orphaned
        FROM observations;
    """)
    for row in data:
        for k, v in row.items():
            print(f"  {k}: {v}")

    print("\n=== TOP ENTITIES BY RELATION COUNT ===\n")

    data = run_sql("""
        SELECT e.name, coalesce(o.cnt, 0) + coalesce(i.cnt, 0) AS total_rels,
               coalesce(o.cnt, 0) AS outgoing, coalesce(i.cnt, 0) AS incoming
        FROM entities e
        LEFT JOIN (SELECT source_entity, count(*) AS cnt FROM relations GROUP BY source_entity) o ON o.source_entity = e.id
        LEFT JOIN (SELECT target_entity, count(*) AS cnt FROM relations GROUP BY target_entity) i ON i.target_entity = e.id
        ORDER BY coalesce(o.cnt, 0) + coalesce(i.cnt, 0) DESC
        LIMIT 20;
    """)
    for row in data:
        print(f"  {row['name']}: {row['total_rels']} ({row['outgoing']} out, {row['incoming']} in)")

    print("\n=== TOP ENTITIES BY OBSERVATION COUNT ===\n")

    data = run_sql("""
        SELECT e.name, count(*) AS obs_count
        FROM observations o, unnest(o.entity_ids) AS eid
        JOIN entities e ON e.id = eid
        GROUP BY e.name
        ORDER BY count(*) DESC
        LIMIT 20;
    """)
    for row in data:
        print(f"  {row['name']}: {row['obs_count']} observations")

    print("\n=== ENTITY PAIRS WITH MOST DUPLICATE RELATIONS ===\n")

    data = run_sql("""
        SELECT s.name AS source, t.name AS target, count(*) AS rel_count,
               array_agg(DISTINCT r.relation_type) AS types
        FROM relations r
        JOIN entities s ON s.id = r.source_entity
        JOIN entities t ON t.id = r.target_entity
        GROUP BY s.name, t.name
        HAVING count(*) > 2
        ORDER BY count(*) DESC
        LIMIT 20;
    """)
    for row in data:
        types = row["types"] if isinstance(row["types"], list) else row["types"]
        print(f"  {row['source']} --> {row['target']}: {row['rel_count']}x ({types})")

    print("\n=== RELATION TYPE DISTRIBUTION ===\n")

    data = run_sql("""
        SELECT relation_type, count(*) AS cnt
        FROM relations
        GROUP BY relation_type
        ORDER BY count(*) DESC
        LIMIT 30;
    """)
    for row in data:
        print(f"  {row['relation_type']}: {row['cnt']}")


if __name__ == "__main__":
    main()
