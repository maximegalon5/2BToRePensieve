"""
Compare before/after snapshots from entity normalization.
Generates a markdown report suitable for an ADR.

Usage:
  python compare_snapshots.py
"""

import sys
import os
import json

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DIR = os.path.dirname(__file__)


def load(name: str) -> dict:
    path = os.path.join(DIR, f"snapshot_{name}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    before = load("before")
    after = load("after")

    bf = before["fragmentation"]
    af = after["fragmentation"]
    bt = before["type_distribution"]
    at_ = after["type_distribution"]
    bc = before["connectivity"]
    ac = after["connectivity"]
    bs = before["search_completeness"]
    as_ = after["search_completeness"]

    report = []
    report.append("# Entity Normalization: Before vs After\n")
    report.append(f"Before: {before['timestamp']}")
    report.append(f"After:  {after['timestamp']}\n")

    # --- Fragmentation ---
    report.append("## Entity Fragmentation\n")
    report.append("| Metric | Before | After | Change |")
    report.append("|--------|--------|-------|--------|")
    report.append(f"| Total entities | {bf['total_entities']} | {af['total_entities']} | {af['total_entities'] - bf['total_entities']:+d} |")
    report.append(f"| Unique names | {bf['unique_names']} | {af['unique_names']} | {af['unique_names'] - bf['unique_names']:+d} |")
    report.append(f"| Fragmented names | {bf['fragmented_names']} | {af['fragmented_names']} | {af['fragmented_names'] - bf['fragmented_names']:+d} |")
    report.append(f"| Fragmented entities | {bf['fragmented_entities']} | {af['fragmented_entities']} | {af['fragmented_entities'] - bf['fragmented_entities']:+d} |")
    report.append("")

    if bf["top_fragmented"]:
        report.append("### Top fragmented entities (before)\n")
        for item in bf["top_fragmented"][:10]:
            report.append(f"- **{item['name']}**: {item['count']}x as {', '.join(item['types'])}")
        report.append("")

    # --- Type Distribution ---
    report.append("## Entity Type Distribution\n")
    report.append(f"| Metric | Before | After |")
    report.append(f"|--------|--------|-------|")
    report.append(f"| Distinct types | {bt['distinct_types']} | {at_['distinct_types']} |")
    report.append("")

    if at_["distribution"]:
        report.append("### After distribution\n")
        report.append("| Type | Count |")
        report.append("|------|-------|")
        for t, c in at_["distribution"].items():
            report.append(f"| {t} | {c} |")
        report.append("")

    # --- Connectivity ---
    report.append("## Graph Connectivity\n")
    report.append("| Metric | Before | After | Change |")
    report.append("|--------|--------|-------|--------|")
    for key in ["entities", "relations", "observations", "avg_relations_per_entity", "avg_observations_per_entity"]:
        bv = bc[key]
        av = ac[key]
        if isinstance(bv, float):
            change = f"{av - bv:+.2f}"
        else:
            change = f"{av - bv:+d}"
        report.append(f"| {key} | {bv} | {av} | {change} |")
    report.append("")

    # --- Search Completeness ---
    report.append("## Search Completeness\n")
    report.append("| Query | Before results | After results | Before entities | After entities |")
    report.append("|-------|---------------|--------------|----------------|---------------|")
    for b_item, a_item in zip(bs, as_):
        report.append(
            f"| {b_item['query']} | {b_item['result_count']} | {a_item['result_count']} "
            f"| {b_item['unique_entities_referenced']} | {a_item['unique_entities_referenced']} |"
        )
    report.append("")

    output = "\n".join(report)
    print(output)

    output_file = os.path.join(DIR, "comparison_report.md")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"\nReport saved to {output_file}")


if __name__ == "__main__":
    main()
