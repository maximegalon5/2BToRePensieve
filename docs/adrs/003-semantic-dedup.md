# ADR-003: Semantic Deduplication and Orphan Re-linking

**Status:** Accepted
**Date:** 2026-03-06
**Decision makers:** [Author], Claude (AI pair programmer)

## Context

After normalizing entity types (ADR-002), the knowledge graph still had two efficiency problems:

1. **Relation duplication** — 770 entity pairs had multiple relations (1,796 total), many semantically identical. For example, [Entity-X]→SATS had 9 relations: "creates", "evaluates", "implements", "manages", "offers", "provides", "related_to", "uses", "works_on" — all saying roughly the same thing.

2. **Orphaned observations** — 6,836 observations (27.3%) had no entity links, making them unreachable via graph traversal. These contained real knowledge but were orphaned during ingestion when entity resolution failed.

### Threshold Calibration

Before choosing a dedup threshold, we sampled 100 entity pairs and computed pairwise cosine similarity across 174 relation pairs at every band:

| Band | Count | Verdict |
|------|-------|---------|
| 0.95-1.00 | 4 | Clearly same — "co_founded" vs "co-founded" |
| 0.90-0.95 | 13 | Same — "worked_on" vs "works_on" |
| 0.85-0.90 | 21 | Mostly same — "depends_on" vs "uses" |
| 0.80-0.85 | 32 | Mixed — gray zone |
| 0.75-0.80 | 37 | Mostly different — "founded" vs "works_on" |
| <0.75 | 67 | Clearly different |

**Chosen threshold: 0.85** — catches clear duplicates, preserves distinct relationships. Below 0.80 we consistently saw genuinely different semantics.

## Decision

### 1. Consolidate duplicate relations (batch cleanup)

For each entity pair with >1 relation:
- Embed all relation descriptions
- Cluster by cosine similarity > 0.85
- Keep the best relation per cluster (longest description)
- Delete the rest

### 2. Re-link orphaned observations

For each orphaned observation (no entity_ids):
- Use its stored embedding to find top-3 matching entities via `search_similar_entities` RPC
- If any entity matches with cosine > 0.5, link the observation
- Skip if no match (observation too generic)

### 3. Prevent future relation duplicates (ingestion fix)

Changed the ingest function from checking exact `(source, target, type)` match to checking if ANY relation exists between the entity pair. If a relation already exists between A→B, skip new ones — this is the root cause of the duplication.

## Results

### QA Matrix

| Metric | Before | After | Change | Direction |
|--------|--------|-------|--------|-----------|
| Relation uniqueness ratio | 0.9109 | 0.9286 | +1.9% | higher is better |
| Semantic redundancy rate | 0.3583 | 0.0049 | **-98.6%** | lower is better |
| Observation cluster ratio | 0.994 | 0.994 | +0.0% | higher is better |
| Orphan rate | 0.2729 | 0.1612 | **-40.9%** | lower is better |
| Retrieval noise ratio | 0.0 | 0.0 | stable | lower is better |
| Storage efficiency | 0.785 | 0.8667 | **+10.4%** | higher is better |

### Relation Consolidation

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Total relations | 11,514 | 11,294 | -220 |
| Duplicate pairs | 770 | 649 | -121 |
| Duplicate relations | 1,796 | 1,455 | -341 |

Remaining 649 duplicate pairs have relations below the 0.85 threshold — these are genuinely distinct relationships (e.g., "created" vs "competes_with").

### Orphan Re-linking

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Linked observations | 18,211 | 21,009 | +2,798 |
| Orphaned observations | 6,836 | 4,038 | -2,798 |

Remaining 4,038 orphans either had no entity match above 0.5, no stored embedding, or were skipped due to a pagination limitation (offset-based query on a mutating result set).

### Retrieval Quality

| Query | Before | After |
|-------|--------|-------|
| What does [Entity-X] do? | timeout | 10 results |
| tell me about SATS | 10 results | 10 results |
| what supplements does [Entity-X] offer? | timeout | 10 results |
| cortisol and stress | timeout | 10 results |
| Python programming tools | timeout | 10 results |

All 5 queries now return results with zero noise (no near-duplicate results in top-10). The search timeouts resolved after the combined effect of entity merging (ADR-002), relation cleanup, and HNSW index rebuild.

## QA Pipeline Design

The QA matrix measures 6 dimensions of graph health:

1. **Relation uniqueness ratio** — unique entity pairs / total relations (structural dedup)
2. **Semantic redundancy rate** — % of multi-relation pairs with cosine > 0.85 (semantic dedup)
3. **Observation cluster ratio** — semantically distinct observations / total (sampled, cosine > 0.90)
4. **Orphan rate** — unlinked observations / total
5. **Retrieval noise ratio** — near-duplicate search results / total results (sampled)
6. **Storage efficiency** — useful rows / total rows

These can be run periodically via `python tests/semantic_dedup/run_qa_snapshot.py` to monitor graph health over time.

## Files Changed

- `supabase/functions/ingest/index.ts` — relation dedup: check any existing pair, not just exact type match
- `tests/semantic_dedup/run_qa_snapshot.py` — QA matrix snapshot tool
- `tests/semantic_dedup/calibrate_threshold.py` — threshold calibration with examples per band
- `tests/semantic_dedup/consolidate_relations.py` — embedding-based relation cleanup
- `tests/semantic_dedup/relink_orphans.py` — orphan observation re-linker
- `tests/semantic_dedup/compare_qa.py` — before/after comparison report

## Open Items

- **Remaining 649 duplicate pairs** — these have genuinely different relation types below 0.85 cosine. Some may still be semantically redundant at a higher level (e.g., "creates" vs "offers" for the same pair). A future LLM-based pass could consolidate these further.
- **Remaining 4,038 orphans** — re-run the re-linker (pagination fix) or accept these as truly generic observations.
- **Relation type normalization** — 30+ distinct relation types exist (uses, related_to, depends_on, works_on, etc.). Constraining to a canonical set (like we did for entity types) could further reduce redundancy.
