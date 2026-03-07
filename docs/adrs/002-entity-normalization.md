# ADR-002: Entity Type Normalization and Deduplication

**Status:** Accepted
**Date:** 2026-03-06
**Decision makers:** User, Claude (AI pair programmer)

## Context

The knowledge graph's ingestion pipeline allowed LLMs to freely assign entity types, resulting in 517 distinct types across 11,769 entities. This caused:

1. **Entity fragmentation** — 1,306 entity names appeared multiple times with different types (e.g., "SATS" existed 16 times as project, feature, system, framework, concept, tool, etc.)
2. **Weak graph connectivity** — average 1.02 relations per entity, diluted across fragments
3. **Search timeouts** — vector similarity search over 11K+ entities hit Supabase's 8s statement timeout on some queries
4. **Inconsistent type filtering** — filtering by entity type was unreliable with 517 ad-hoc categories

## Decision

### 1. Constrain entity types to 6 canonical categories

| Type | Covers |
|------|--------|
| person | humans — real, fictional, clients, team members |
| organization | companies, brands, teams, institutions |
| project | products, repos, applications, initiatives |
| concept | ideas, theories, patterns, events, places, substances — broad catch-all |
| tool | software, libraries, frameworks, APIs, platforms, languages |
| content | books, articles, videos, papers, courses |

**Rationale:** 6 types is the sweet spot — enough to meaningfully filter, few enough that an LLM can reliably classify without ambiguity. "Will the LLM know the difference between a Person and a Character?" — no, so they're the same type.

### 2. Merge duplicate entities

For each set of entities sharing a name (case-insensitive), keep the one with the most relations and merge all others into it: move relations, observations, tasks, and aliases from loser to winner, then delete the loser.

### 3. Enforce constraints going forward

- CHECK constraint: `entity_type IN ('person', 'organization', 'project', 'concept', 'tool', 'content')`
- Unique index: `lower(name)` — one entity per canonical name
- Updated extraction prompt explicitly lists the 6 types with examples
- 3-tier entity resolution in ingestion: exact name match → fuzzy embedding (cosine > 0.8) with LLM confirmation → create new

## Results

### Entity Fragmentation

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Total entities | 11,769 | 9,576 | -2,193 (-18.6%) |
| Unique names | 9,576 | 9,576 | 0 |
| Fragmented names | 1,306 | 0 | -1,306 (100% fixed) |
| Fragmented entities | 3,499 | 0 | -3,499 (100% fixed) |

### Entity Type Distribution

| Metric | Before | After |
|--------|--------|-------|
| Distinct types | 517 | 6 |

After distribution: concept (6,306), tool (1,447), project (760), organization (567), person (480), content (16).

### Graph Connectivity

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Entities | 11,769 | 9,576 | -2,193 |
| Relations | 12,056 | 11,514 | -542 |
| Observations | 25,047 | 25,047 | 0 |
| Avg relations/entity | 1.02 | 1.20 | +0.18 (+17.6%) |
| Avg observations/entity | 2.13 | 2.62 | +0.49 (+23.0%) |

### Search Completeness (10 benchmark queries)

| Query | Before | After |
|-------|--------|-------|
| ExampleOrg | 10 results | timeout* |
| SATS | timeout | 10 results, 9 entities |
| User | 10 results | 10 results |
| tell me about SATS | timeout | 10 results, 9 entities |
| Python programming | timeout | 10 results, 9 entities |
| React framework | 10 results | 10 results |
| PersonB | 10 results | 10 results |

*ExampleOrg has 500+ relations making its neighborhood scan heavy — a separate optimization target.

**Net search improvement:** 4 queries that previously timed out now return results. 1 query that previously worked now times out (ExampleOrg — dense node).

## Migrations

- `005_normalize_entity_types.sql` — maps 517 types to 6 canonical types, adds CHECK constraint
- `006_merge_duplicate_entities.sql` — creates `merge_entities()` function, merges all name-based duplicates, adds `lower(name)` unique index

## Open Items

- ExampleOrg search timeout — dense entity with 500+ relations. Consider: statement timeout tuning, pre-computed entity summaries, or HNSW index parameter tuning.
- `content` type has only 16 entities — the extraction prompt may need stronger examples to classify books/articles correctly (many likely landed in `concept`).
- Graph-aware search expansion (1-hop neighborhood after top results) — planned but not yet implemented.
