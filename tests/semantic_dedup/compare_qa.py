"""
Compare before/after QA snapshots for semantic dedup.

Usage:
  python compare_qa.py
"""

import sys
import os
import json

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DIR = os.path.dirname(__file__)


def load(name):
    with open(os.path.join(DIR, f"qa_snapshot_{name}.json"), encoding="utf-8") as f:
        return json.load(f)


def pct_change(before, after):
    if before == 0:
        return "n/a"
    change = (after - before) / before * 100
    return f"{change:+.1f}%"


def main():
    before = load("before")
    after = load("after")

    report = []
    report.append("# Semantic Dedup: QA Matrix Before vs After\n")
    report.append(f"Before: {before['timestamp']}")
    report.append(f"After:  {after['timestamp']}\n")

    # Summary table
    report.append("## QA Matrix Summary\n")
    report.append("| Metric | Before | After | Change | Direction |")
    report.append("|--------|--------|-------|--------|-----------|")

    metrics = [
        ("Relation uniqueness ratio", before["relation_uniqueness"]["uniqueness_ratio"],
         after["relation_uniqueness"]["uniqueness_ratio"], "higher is better"),
        ("Semantic redundancy rate", before["semantic_redundancy"]["redundancy_rate"],
         after["semantic_redundancy"]["redundancy_rate"], "lower is better"),
        ("Observation cluster ratio", before["observation_clusters"]["overall_cluster_ratio"],
         after["observation_clusters"]["overall_cluster_ratio"], "higher is better"),
        ("Orphan rate", before["orphan_rate"]["orphan_rate"],
         after["orphan_rate"]["orphan_rate"], "lower is better"),
        ("Retrieval noise ratio", before["retrieval_noise"]["avg_noise_ratio"],
         after["retrieval_noise"]["avg_noise_ratio"], "lower is better"),
        ("Storage efficiency", before["storage_efficiency"]["storage_efficiency"],
         after["storage_efficiency"]["storage_efficiency"], "higher is better"),
    ]

    for name, bv, av, direction in metrics:
        change = pct_change(bv, av)
        report.append(f"| {name} | {bv} | {av} | {change} | {direction} |")

    # Relation details
    report.append("\n## Relation Consolidation\n")
    br = before["relation_uniqueness"]
    ar = after["relation_uniqueness"]
    report.append(f"| Metric | Before | After | Change |")
    report.append(f"|--------|--------|-------|--------|")
    report.append(f"| Total relations | {br['total_relations']} | {ar['total_relations']} | {ar['total_relations'] - br['total_relations']:+d} |")
    report.append(f"| Unique pairs | {br['unique_pairs']} | {ar['unique_pairs']} | {ar['unique_pairs'] - br['unique_pairs']:+d} |")
    report.append(f"| Duplicate pairs | {br['duplicate_pairs']} | {ar['duplicate_pairs']} | {ar['duplicate_pairs'] - br['duplicate_pairs']:+d} |")
    report.append(f"| Duplicate relations | {br['duplicate_relations']} | {ar['duplicate_relations']} | {ar['duplicate_relations'] - br['duplicate_relations']:+d} |")

    # Orphan details
    report.append("\n## Orphan Re-linking\n")
    bo = before["orphan_rate"]
    ao = after["orphan_rate"]
    report.append(f"| Metric | Before | After | Change |")
    report.append(f"|--------|--------|-------|--------|")
    report.append(f"| Total observations | {bo['total_observations']} | {ao['total_observations']} | {ao['total_observations'] - bo['total_observations']:+d} |")
    report.append(f"| Linked | {bo['linked']} | {ao['linked']} | {ao['linked'] - bo['linked']:+d} |")
    report.append(f"| Orphaned | {bo['orphaned']} | {ao['orphaned']} | {ao['orphaned'] - bo['orphaned']:+d} |")

    # Search results
    report.append("\n## Retrieval Quality\n")
    report.append("| Query | Before results | After results |")
    report.append("|-------|---------------|--------------|")
    for bq, aq in zip(before["retrieval_noise"]["queries"], after["retrieval_noise"]["queries"]):
        b_res = f"{bq['results']} ({bq['unique_results']} unique)" if not bq.get("error") else "timeout"
        a_res = f"{aq['results']} ({aq['unique_results']} unique)" if not aq.get("error") else "timeout"
        report.append(f"| {bq['query']} | {b_res} | {a_res} |")

    output = "\n".join(report)
    print(output)

    output_file = os.path.join(DIR, "qa_comparison_report.md")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"\nReport saved to {output_file}")


if __name__ == "__main__":
    main()
