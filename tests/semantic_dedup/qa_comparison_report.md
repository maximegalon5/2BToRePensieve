# Semantic Dedup: QA Matrix Before vs After

Before: 2026-03-06T19:39:52.892637+00:00
After:  2026-03-06T20:42:31.912149+00:00

## QA Matrix Summary

| Metric | Before | After | Change | Direction |
|--------|--------|-------|--------|-----------|
| Relation uniqueness ratio | 0.9109 | 0.9286 | +1.9% | higher is better |
| Semantic redundancy rate | 0.3583 | 0.0049 | -98.6% | lower is better |
| Observation cluster ratio | 0.994 | 0.994 | +0.0% | higher is better |
| Orphan rate | 0.2729 | 0.1612 | -40.9% | lower is better |
| Retrieval noise ratio | 0.0 | 0.0 | n/a | lower is better |
| Storage efficiency | 0.785 | 0.8667 | +10.4% | higher is better |

## Relation Consolidation

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Total relations | 11514 | 11294 | -220 |
| Unique pairs | 10488 | 10488 | +0 |
| Duplicate pairs | 770 | 649 | -121 |
| Duplicate relations | 1796 | 1455 | -341 |

## Orphan Re-linking

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Total observations | 25047 | 25047 | +0 |
| Linked | 18211 | 21009 | +2798 |
| Orphaned | 6836 | 4038 | -2798 |

## Retrieval Quality

| Query | Before results | After results |
|-------|---------------|--------------|
| What does ExampleOrg do? | timeout | 10 (10 unique) |
| tell me about SATS | 10 (10 unique) | 10 (10 unique) |
| what supplements does ExampleOrg offer? | timeout | 10 (10 unique) |
| cortisol and stress | timeout | 10 (10 unique) |
| Python programming tools | timeout | 10 (10 unique) |