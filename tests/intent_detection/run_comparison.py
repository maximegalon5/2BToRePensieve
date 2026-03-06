"""Compare Approach A (single-call) vs Approach B (two-stage) intent classification."""

import json
import os
import sys
import time
from pathlib import Path

# Force UTF-8 output on Windows
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
from dotenv import load_dotenv

from prompts import (
    APPROACH_A_SYSTEM,
    APPROACH_B_CLASSIFY_SYSTEM,
    APPROACH_B_EXTRACT_SYSTEMS,
    VALID_INTENTS,
)

load_dotenv()

API_KEY = os.environ["OPENROUTER_API_KEY"]
MODEL = "openai/gpt-4o-mini"
API_URL = "https://openrouter.ai/api/v1/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def call_llm(system: str, user: str, max_tokens: int = 150) -> tuple[str, float, int]:
    """Call OpenRouter. Returns (response_text, latency_seconds, total_tokens)."""
    start = time.time()
    resp = httpx.post(
        API_URL,
        headers=HEADERS,
        json={
            "model": MODEL,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=30,
    )
    latency = time.time() - start
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()
    tokens = data.get("usage", {}).get("total_tokens", 0)
    return text, latency, tokens


def parse_json_safe(text: str) -> dict | None:
    """Try to parse JSON from LLM response, handling markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def run_approach_a(message: str) -> dict:
    """Single-call: classify + extract params in one shot."""
    raw, latency, tokens = call_llm(APPROACH_A_SYSTEM, message)
    result = parse_json_safe(raw)
    if result and result.get("intent") in VALID_INTENTS:
        return {
            "intent": result["intent"],
            "params": result.get("params", {}),
            "latency": latency,
            "tokens": tokens,
            "raw": raw,
        }
    return {
        "intent": "ambiguous",
        "params": {},
        "latency": latency,
        "tokens": tokens,
        "raw": raw,
    }


def run_approach_b(message: str) -> dict:
    """Two-stage: classify first, then extract params if needed."""
    # Stage 1: classify
    raw_intent, lat1, tok1 = call_llm(APPROACH_B_CLASSIFY_SYSTEM, message, max_tokens=20)
    intent = raw_intent.strip().lower().replace('"', "").replace("'", "")

    if intent not in VALID_INTENTS:
        return {
            "intent": "ambiguous",
            "params": {},
            "latency": lat1,
            "tokens": tok1,
            "raw": raw_intent,
        }

    # Stage 2: extract params (only for intents that need them)
    if intent in APPROACH_B_EXTRACT_SYSTEMS:
        raw_params, lat2, tok2 = call_llm(
            APPROACH_B_EXTRACT_SYSTEMS[intent], message, max_tokens=100
        )
        params = parse_json_safe(raw_params) or {}
        return {
            "intent": intent,
            "params": params,
            "latency": lat1 + lat2,
            "tokens": tok1 + tok2,
            "raw": f"stage1: {raw_intent} | stage2: {raw_params}",
        }

    return {
        "intent": intent,
        "params": {},
        "latency": lat1,
        "tokens": tok1,
        "raw": raw_intent,
    }


def check_params(expected: dict, actual: dict) -> bool:
    """Check if extracted params are acceptable.

    For params with string values, check if the expected value appears
    as a substring of the actual value (case-insensitive) to allow for
    minor phrasing differences.
    """
    if not expected:
        return True
    for key, exp_val in expected.items():
        act_val = actual.get(key, "")
        if isinstance(exp_val, str) and isinstance(act_val, str):
            if exp_val.lower() not in act_val.lower():
                return False
        elif exp_val != act_val:
            return False
    return True


def main():
    test_file = Path(__file__).parent / "test_cases.json"
    with open(test_file, encoding="utf-8") as f:
        cases = json.load(f)["test_cases"]

    print(f"Running {len(cases)} test cases through both approaches...\n")

    results_a = []
    results_b = []

    for i, case in enumerate(cases):
        msg = case["input"]
        expected_intent = case["expected_intent"]
        expected_params = case["expected_params"]
        label = msg[:50] + "..." if len(msg) > 50 else msg

        print(f"[{i+1:2d}/{len(cases)}] {label}")

        # Run both approaches
        res_a = run_approach_a(msg)
        res_b = run_approach_b(msg)

        # Score
        a_intent_ok = res_a["intent"] == expected_intent
        b_intent_ok = res_b["intent"] == expected_intent
        a_params_ok = check_params(expected_params, res_a["params"])
        b_params_ok = check_params(expected_params, res_b["params"])

        results_a.append({
            **res_a,
            "input": msg,
            "expected_intent": expected_intent,
            "expected_params": expected_params,
            "intent_correct": a_intent_ok,
            "params_correct": a_params_ok,
        })
        results_b.append({
            **res_b,
            "input": msg,
            "expected_intent": expected_intent,
            "expected_params": expected_params,
            "intent_correct": b_intent_ok,
            "params_correct": b_params_ok,
        })

        a_mark = "OK" if a_intent_ok else "MISS"
        b_mark = "OK" if b_intent_ok else "MISS"
        print(f"       A: {res_a['intent']:20s} [{a_mark}]  B: {res_b['intent']:20s} [{b_mark}]")

    # --- Summary ---
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}\n")

    a_acc = sum(r["intent_correct"] for r in results_a)
    b_acc = sum(r["intent_correct"] for r in results_b)
    a_param = sum(r["params_correct"] for r in results_a)
    b_param = sum(r["params_correct"] for r in results_b)
    a_lat = sum(r["latency"] for r in results_a) / len(results_a)
    b_lat = sum(r["latency"] for r in results_b) / len(results_b)
    a_tok = sum(r["tokens"] for r in results_a) / len(results_a)
    b_tok = sum(r["tokens"] for r in results_b) / len(results_b)

    total = len(cases)
    print(f"{'Metric':<25} {'Approach A':>15} {'Approach B':>15}")
    print(f"{'─'*25} {'─'*15} {'─'*15}")
    print(f"{'Intent accuracy':<25} {a_acc}/{total} ({a_acc/total*100:.1f}%){'':<4} {b_acc}/{total} ({b_acc/total*100:.1f}%)")
    print(f"{'Param accuracy':<25} {a_param}/{total} ({a_param/total*100:.1f}%){'':<4} {b_param}/{total} ({b_param/total*100:.1f}%)")
    print(f"{'Avg latency (s)':<25} {a_lat:>15.3f} {b_lat:>15.3f}")
    print(f"{'Avg tokens/call':<25} {a_tok:>15.1f} {b_tok:>15.1f}")

    # Per-category breakdown
    categories = {}
    for r in results_a:
        cat = r["expected_intent"]
        if cat not in categories:
            categories[cat] = {"a_ok": 0, "b_ok": 0, "total": 0}
        categories[cat]["total"] += 1
        categories[cat]["a_ok"] += r["intent_correct"]
    for r in results_b:
        cat = r["expected_intent"]
        categories[cat]["b_ok"] += r["intent_correct"]

    print(f"\n{'Category':<25} {'A correct':>15} {'B correct':>15}")
    print(f"{'─'*25} {'─'*15} {'─'*15}")
    for cat, counts in sorted(categories.items()):
        t = counts["total"]
        print(f"{cat:<25} {counts['a_ok']}/{t:>12} {counts['b_ok']}/{t:>12}")

    # Disagreements
    disagreements = [
        (results_a[i], results_b[i])
        for i in range(total)
        if results_a[i]["intent"] != results_b[i]["intent"]
    ]
    if disagreements:
        print(f"\nDISAGREEMENTS ({len(disagreements)}):")
        for ra, rb in disagreements:
            msg = ra["input"][:60]
            print(f"  \"{msg}\"")
            print(f"    Expected: {ra['expected_intent']}")
            print(f"    A: {ra['intent']} {'OK' if ra['intent_correct'] else 'MISS'}")
            print(f"    B: {rb['intent']} {'OK' if rb['intent_correct'] else 'MISS'}")

    # Failures detail
    failures_a = [r for r in results_a if not r["intent_correct"]]
    failures_b = [r for r in results_b if not r["intent_correct"]]
    if failures_a:
        print(f"\nAPPROACH A FAILURES ({len(failures_a)}):")
        for r in failures_a:
            print(f"  \"{r['input'][:60]}\" -> {r['intent']} (expected {r['expected_intent']})")
    if failures_b:
        print(f"\nAPPROACH B FAILURES ({len(failures_b)}):")
        for r in failures_b:
            print(f"  \"{r['input'][:60]}\" -> {r['intent']} (expected {r['expected_intent']})")

    # Save raw results
    output_file = Path(__file__).parent / "comparison_results.json"
    with open(output_file, "w") as f:
        json.dump({"approach_a": results_a, "approach_b": results_b}, f, indent=2)
    print(f"\nRaw results saved to {output_file}")

    # Winner
    print(f"\n{'='*80}")
    if a_acc > b_acc:
        print("WINNER: Approach A (higher accuracy)")
    elif b_acc > a_acc:
        print("WINNER: Approach B (higher accuracy)")
    elif a_lat < b_lat:
        print("WINNER: Approach A (same accuracy, lower latency)")
    else:
        print("WINNER: Approach B (same accuracy, lower latency)")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
