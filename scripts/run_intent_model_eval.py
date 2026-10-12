"""Evaluate semantic intent recognition without executing commerce tools."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time

from app.commerce.config import Settings
from app.commerce.intent import IntentRecognizer


def rate(values):
    return round(sum(values) / len(values), 4) if values else None


async def run(dataset, output):
    cases = json.loads(Path(dataset).read_text(encoding="utf-8"))
    settings = Settings(intent_enabled=True)
    if not (settings.intent_api_key or settings.api_key):
        raise RuntimeError("Set COMMERCE_INTENT_API_KEY or DASHSCOPE_API_KEY before running this model-focused evaluation.")
    recognizer = IntentRecognizer(settings)
    results = []
    intent_all, intent_model, slot_checks, latencies = [], [], [], []
    source_counts = Counter()
    try:
        for case in cases:
            start = time.perf_counter()
            recognition = await recognizer.recognize(case["query"], case.get("context", {}))
            latency_ms = (time.perf_counter() - start) * 1000
            result = recognition.result
            source_counts[recognition.source] += 1
            expected = case["expected_intent"]
            intent_ok = result.intent == expected
            intent_all.append(intent_ok)
            if recognition.source == "model":
                intent_model.append(intent_ok)
            slots = result.slots.model_dump(exclude_none=True)
            expected_slots = case.get("expected_slots", {})
            case_slot_checks = {}
            for name, value in expected_slots.items():
                actual = slots.get(name)
                ok = actual == value
                case_slot_checks[name] = {"expected": value, "actual": actual, "correct": ok}
                slot_checks.append(ok)
            latencies.append(latency_ms)
            results.append({
                "id": case["id"],
                "expected_intent": expected,
                "actual_intent": result.intent,
                "intent_correct": intent_ok,
                "expected_source": case.get("expected_source", "model"),
                "actual_source": recognition.source,
                "source_correct": recognition.source == case.get("expected_source", "model"),
                "needs_clarification": result.needs_clarification,
                "slot_checks": case_slot_checks,
                "latency_ms": round(latency_ms, 2),
            })
    finally:
        await recognizer.aclose()

    attempted = source_counts["model"] + source_counts["model_fallback"]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "model_focused_intent_only",
        "dataset": str(dataset),
        "summary": {
            "total_cases": len(cases),
            "intent_accuracy_e2e": rate(intent_all),
            "intent_accuracy_when_model_returned": rate(intent_model),
            "model_attempted": attempted,
            "model_returned": source_counts["model"],
            "model_success_rate": round(source_counts["model"] / attempted, 4) if attempted else None,
            "expected_model_source_rate": rate([r["source_correct"] for r in results]),
            "slot_parameter_accuracy": rate(slot_checks),
            "slot_parameter_points": len(slot_checks),
            "source_counts": dict(source_counts),
            "average_latency_ms": round(statistics.mean(latencies), 2) if latencies else None,
            "p95_latency_ms": round(sorted(latencies)[max(0, math.ceil(len(latencies) * .95) - 1)], 2) if latencies else None,
        },
        "results": results,
    }
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return all(r["intent_correct"] and r["source_correct"] for r in results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="eval/commerce/intent_model_focused.json")
    parser.add_argument("--output", default="eval/commerce/report_intent_model_focused.json")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.dataset, args.output)) else 1)
