"""Offline business regression, explicitly distinct from LLM semantic evaluation."""
import argparse
from collections import Counter
import json
import logging
import math
import os
import re
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from app.commerce.api import create_app
from app.commerce.config import Settings


class _IntentFailureCounter(logging.Handler):
    """Count safe exception type names from intent fallback warnings."""

    def __init__(self, counts):
        super().__init__(level=logging.WARNING)
        self.counts = counts

    def emit(self, record):
        match = re.search(r"Semantic intent recognition failed \(([^)]+)\)", record.getMessage())
        if match:
            self.counts[match.group(1)] += 1


def run(dataset, output, *, with_model=False, with_rag=False, embedding_provider="local", parser="pdfplumber"):
    cases = json.loads(Path(dataset).read_text(encoding="utf-8"))
    results, intents, parameters, latencies, intercepts = [], [], [], [], []
    task_completion_cases, slot_parameter_points = [], []
    tagged_model_required = []
    tool_parameter_points, tool_parameter_case_checks, tool_parameter_guard_points = [], [], []
    model_intents, model_fallbacks, model_conditional_intents = [], [], []
    intent_source_counts = Counter()
    rag_calls, rag_hits = 0, 0
    parameters_reached, parameters_correct_reached = [], []
    parameter_not_reached_after_model_fallback = 0
    unauthorized_reached, unauthorized_reached_intercepts, unauthorized_no_leak = [], [], []
    fallback_reason_counts = Counter()
    rag_evidence_checks, rag_no_result_checks = [], []
    normalize_evidence = lambda value: re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value).casefold())
    rag_temp = tempfile.TemporaryDirectory(prefix="commerce-rag-eval-") if with_rag else None
    env_overrides = {}
    if with_rag:
        rag_root = Path(rag_temp.name)
        env_overrides = {
            "COMMERCE_RAG_DATA_DIR": str(Path("raw_data/pdf").resolve()),
            "COMMERCE_RAG_DATABASE_PATH": str(rag_root / "rag.sqlite"),
            "COMMERCE_RAG_QDRANT_PATH": str(rag_root / "qdrant"),
            "COMMERCE_RAG_EMBEDDING_PROVIDER": embedding_provider,
            "COMMERCE_RAG_PARSER": parser,
        }
        for key, value in env_overrides.items():
            os.environ[key] = value
        # Build one isolated index up front, then share it across scenario apps.
        rag_settings = Settings(rag_enabled=True, rag_data_dir=env_overrides["COMMERCE_RAG_DATA_DIR"],
                                rag_database_path=env_overrides["COMMERCE_RAG_DATABASE_PATH"],
                                rag_qdrant_path=env_overrides["COMMERCE_RAG_QDRANT_PATH"])
        from app.commerce.rag.service import RAGService
        indexer = RAGService(None, rag_settings)
        indexer.build()
        indexer.qdrant.client.close()
        indexer.engine.dispose()
    intent_logger = logging.getLogger("app.commerce.intent")
    intent_failure_handler = _IntentFailureCounter(fallback_reason_counts)
    intent_logger.addHandler(intent_failure_handler)
    base_settings = Settings()
    effective_intent_enabled = with_model or base_settings.intent_enabled
    effective_llm_enabled = base_settings.llm_enabled
    try:
      for case in cases:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(database_url="sqlite:///" + str(Path(directory) / "eval.db"),
                                tool_transport="local", redis_url="", llm_enabled=effective_llm_enabled,
                                intent_enabled=effective_intent_enabled, rag_enabled=with_rag, demo_enabled=True)
            with TestClient(create_app(settings)) as client:
                def account(name):
                    body = {"user_id": name, "password": "eval-password-only"}
                    client.post("/auth/register", json=body)
                    token = client.post("/auth/login", json=body).json()["access_token"]
                    return {"Authorization": "Bearer " + token}
                headers, other = account("buyer"), account("other")
                orders = client.get("/orders", headers=headers).json()["orders"]
                ids = {o["status"]: o["order_id"] for o in orders}
                if case.get("fixture") == "delivered_customized":
                    from app.commerce.store import Order
                    with client.app.state.store.session.begin() as db:
                        db.get(Order, ids["delivered"]).product_id = "P300"
                ids["foreign"] = client.get("/orders", headers=other).json()["orders"][0]["order_id"]
                checks, steps = [], []
                for step in case["steps"]:
                    result = client.post("/chat/", headers=headers,
                        json={"query": step["query"].format(**ids), "conversation_id": "eval"}).json()
                    intent_ok = result.get("intent") == step["intent"]
                    intents.append(intent_ok)
                    intent_source = result.get("intent_source", "rule_fallback")
                    if "model_required" in case.get("eval_tags", []):
                        tagged_model_required.append(intent_source == "model")
                    intent_source_counts[intent_source] += 1
                    model_intents.append(intent_source == "model")
                    model_fallbacks.append(intent_source == "model_fallback")
                    if intent_source == "model":
                        model_conditional_intents.append(intent_ok)
                    tools = result.get("tool_trace", [])
                    expected_tools = step.get("tools_model_rag")
                    if expected_tools is None and "tools" in step:
                        expected_tools = list(step["tools"])
                        if with_rag and step["intent"] in {"product_consult", "other"}:
                            expected_tools = ["rag_search" if name == "search_knowledge" else name
                                              for name in expected_tools]
                    actual_tools = [t["tool"] for t in tools]
                    expected_parameters = list(step.get("expected_tool_parameters", []))
                    if "order_id" in step:
                        expected_order_id = step["order_id"].format(**ids)
                        for tool_name in set(step.get("tools", [])) & {
                                "query_order", "query_logistics", "query_after_sale", "check_after_sales",
                                "check_business_rule", "prepare_operation"}:
                            if not any(p["tool"] == tool_name and p["name"] == "order_id"
                                       for p in expected_parameters):
                                expected_parameters.append({"tool": tool_name, "name": "order_id",
                                                            "value": expected_order_id})
                    current_parameter_checks = []
                    for expected_parameter in expected_parameters:
                        tool_name = expected_parameter["tool"]
                        key = expected_parameter["name"]
                        expected_value = expected_parameter.get("value")
                        if isinstance(expected_value, str):
                            expected_value = expected_value.format(**ids)
                        matching_calls = [t for t in tools if t["tool"] == tool_name]
                        matches = (len(matching_calls) == 1
                                   and matching_calls[0].get("arguments", {}).get(key) == expected_value)
                        current_parameter_checks.append(matches)
                        tool_parameter_points.append(matches)
                    if expected_parameters:
                        tool_parameter_case_checks.append(all(current_parameter_checks))
                    for slot_name, expected_slot in step.get("expected_slots", {}).items():
                        actual_slot = (result.get("intent_slots") or {}).get(slot_name)
                        slot_parameter_points.append(actual_slot == expected_slot)
                    for forbidden_tool in step.get("forbidden_parameterized_tools", []):
                        tool_parameter_guard_points.append(not any(t["tool"] == forbidden_tool for t in tools))
                    tool_ok = actual_tools == expected_tools if expected_tools is not None else True
                    rag_calls += sum(t["tool"] == "rag_search" for t in tools)
                    data = result.get("data", {})
                    used_rag = any(t["tool"] == "rag_search" for t in tools)
                    if used_rag and data.get("hits"):
                        rag_hits += 1
                    if used_rag:
                        retrieved_text = " ".join(hit.get("text", "") for hit in data.get("hits", []))
                        if step.get("contains"):
                            expected_evidence = normalize_evidence(step["contains"])
                            rag_evidence_checks.append(expected_evidence in normalize_evidence(retrieved_text))
                        if step.get("status") == "no_results":
                            rag_no_result_checks.append(result.get("status") == "no_results" and not data.get("hits"))
                    checks.append(intent_ok)
                    if expected_tools is not None:
                        checks.append(tool_ok)
                    if "status" in step:
                        checks.append(result.get("status") == step["status"])
                    if "contains" in step:
                        checks.append(step["contains"] in result.get("answer", ""))
                    if "not_contains" in step:
                        checks.extend(value not in result.get("answer", "") for value in step["not_contains"])
                    if "order_id" in step:
                        expected = step["order_id"].format(**ids)
                        actual = [t["arguments"]["order_id"] for t in tools if "order_id" in t["arguments"]]
                        matches = bool(actual) and all(v == expected for v in actual)
                        parameters.append(matches)
                        if actual:
                            parameters_reached.append(True)
                            parameters_correct_reached.append(matches)
                        elif intent_source == "model_fallback":
                            parameter_not_reached_after_model_fallback += 1
                        checks.append(matches)
                    latencies.append(result.get("latency_ms", 0))
                    steps.append(result)
                if case.get("confirm"):
                    confirmation = steps[-1].get("data", {}).get("confirmation_id")
                    response = client.post("/operations/confirm", headers=headers, json={"confirmation_id": confirmation}).json() if confirmation else {}
                    checks.append(response.get("status") == case["confirm"])
                if case.get("risk"):
                    expected_tools_by_step = [set(item.get("tools", [])) for item in case["steps"]]
                    checked = []
                    no_leak = []
                    for test_step, response_step, expected_tools_for_step in zip(case["steps"], steps, expected_tools_by_step):
                        actual_tool_names = {tool.get("tool") for tool in response_step.get("tool_trace", [])}
                        reached_business = (response_step.get("status") == "order_access_denied"
                                            or bool(actual_tool_names & expected_tools_for_step))
                        if reached_business:
                            checked.append(response_step.get("status") == "order_access_denied")
                        data_keys = set((response_step.get("data") or {}).keys())
                        no_leak.append(not data_keys.intersection({"order", "orders", "address", "phone", "contact", "receiver"}))
                    intercepted = all(step.get("status") == "order_access_denied" and "order" not in step.get("data", {}) for step in steps)
                    intercepts.append(intercepted)
                    unauthorized_no_leak.append(all(no_leak))
                    if checked:
                        unauthorized_reached.append(True)
                        unauthorized_reached_intercepts.append(all(checked))
                    else:
                        unauthorized_reached.append(False)
                    checks.append(intercepted)
                if "task_completion" in case.get("eval_tags", []):
                    task_completion_cases.append(all(checks))
                results.append({"case_id": case["id"], "passed": all(checks), "steps": steps})
            if with_rag:
                rag_runtime = client.app.state.service._rag
                if rag_runtime not in (None, False):
                    rag_runtime.qdrant.client.close()
                    rag_runtime.engine.dispose()
      rate = lambda values: round(sum(values) / len(values), 4) if values else None
      mode = f"local_tools_{'llm' if effective_llm_enabled else 'no_llm'}_intent_{'model' if effective_intent_enabled else 'rules'}_{'rag' if with_rag else 'no_rag'}"
      report = {"generated_at": datetime.now(timezone.utc).isoformat(),
          "mode": mode,
          "metric_notes": ("Task completion uses expected status/tool sequence and confirmation outcomes; order parameter accuracy checks explicit order_id expectations. "
              "Tool parameter accuracy checks only explicitly annotated tool/name/value points in the dataset. "
              f"Authored local regression scenarios, not production benchmarks. RAG embedding provider: {embedding_provider if with_rag else 'not used'}; "
              f"RAG PDF parser: {parser if with_rag else 'not used'}; MinerU production ingestion automatically falls back to pdfplumber. "
              "RAG evidence coverage is a normalized lexical check against each case's expected answer fragment, not an LLM relevance judgment."),
          "summary": {"total_cases": len(results), "passed_cases": sum(r["passed"] for r in results),
              "task_completion_rate": rate(task_completion_cases),
              "intent_accuracy": rate(intents), "intent_accuracy_e2e": rate(intents),
              "intent_accuracy_when_model_returned": rate(model_conditional_intents) if effective_intent_enabled else None,
              "intent_source_counts": dict(intent_source_counts),
              "model_intent_fallback_rate": (round(model_fallbacks.count(True) /
                  max(1, model_intents.count(True) + model_fallbacks.count(True)), 4) if with_model else None),
              "order_parameter_accuracy": rate(parameters), "order_parameter_accuracy_e2e": rate(parameters),
              "tool_parameter_accuracy": rate(tool_parameter_points),
              "tool_parameter_case_accuracy": rate(tool_parameter_case_checks),
              "tool_parameter_guard_accuracy": rate(tool_parameter_guard_points),
              "intent_slot_parameter_accuracy": rate(slot_parameter_points),
              "model_required_source_rate": rate(tagged_model_required),
              "order_parameter_accuracy_when_tool_reached": rate(parameters_correct_reached),
              "order_parameter_tool_reach_rate": (round(len(parameters_reached) / len(parameters), 4) if parameters else None),
              "order_parameter_cases_not_reached_after_model_fallback": (parameter_not_reached_after_model_fallback if effective_intent_enabled else None),
              "unauthorized_intercept_rate": rate(intercepts), "unauthorized_intercept_rate_e2e": rate(intercepts),
              "unauthorized_business_reach_rate": rate(unauthorized_reached),
              "unauthorized_intercept_rate_when_business_reached": rate(unauthorized_reached_intercepts),
              "unauthorized_no_leak_rate": rate(unauthorized_no_leak),
              "average_latency_ms": round(statistics.mean(latencies), 2),
              "p95_latency_ms": round(sorted(latencies)[max(0, math.ceil(len(latencies) * .95) - 1)], 2),
              "model_intent_call_rate": (round((model_intents.count(True) + model_fallbacks.count(True)) / len(intents), 4) if effective_intent_enabled and intents else None),
              "model_intent_success_rate": (round(model_intents.count(True) /
                  max(1, model_intents.count(True) + model_fallbacks.count(True)), 4) if effective_intent_enabled else None),
              "model_fallback_reason_counts": dict(fallback_reason_counts) if effective_intent_enabled else None,
              "metric_denominators": {
                  "intent_all_steps": len(intents),
                  "intent_model_returned": len(model_conditional_intents),
                  "intent_model_attempted": model_intents.count(True) + model_fallbacks.count(True),
                  "intent_model_fallbacks": model_fallbacks.count(True),
                  "order_parameter_expected": len(parameters),
                  "tool_parameter_points_expected": len(tool_parameter_points),
                  "tool_parameter_cases_expected": len(tool_parameter_case_checks),
                  "tool_parameter_guard_cases_expected": len(tool_parameter_guard_points),
                  "intent_slot_parameter_points_expected": len(slot_parameter_points),
                  "task_completion_cases": len(task_completion_cases),
                  "model_required_cases": len(tagged_model_required),
                  "order_parameter_tool_reached": len(parameters_reached),
                  "unauthorized_cases": len(intercepts),
                  "unauthorized_business_reached": sum(unauthorized_reached),
              },
              "metric_counts": {
                  "intent_all_correct": sum(intents),
                  "intent_model_returned_correct": sum(model_conditional_intents),
                  "order_parameter_e2e_correct": sum(parameters),
                  "tool_parameter_points_correct": sum(tool_parameter_points),
                  "tool_parameter_cases_correct": sum(tool_parameter_case_checks),
                  "tool_parameter_guard_cases_correct": sum(tool_parameter_guard_points),
                  "intent_slot_parameter_points_correct": sum(slot_parameter_points),
                  "task_completion_cases_correct": sum(task_completion_cases),
                  "model_required_cases_using_model": sum(tagged_model_required),
                  "order_parameter_correct_when_tool_reached": sum(parameters_correct_reached),
                  "unauthorized_e2e_intercepted": sum(intercepts),
                  "unauthorized_intercepted_when_business_reached": sum(unauthorized_reached_intercepts),
                  "unauthorized_cases_without_data_leak": sum(unauthorized_no_leak),
              },
              "rag_tool_calls": rag_calls if with_rag else None,
              "rag_nonempty_result_rate": round(rag_hits / rag_calls, 4) if with_rag and rag_calls else None,
              "rag_expected_evidence_coverage": rate(rag_evidence_checks) if with_rag and rag_evidence_checks else None,
              "rag_no_result_rejection_rate": rate(rag_no_result_checks) if with_rag and rag_no_result_checks else None},
          "results": results}
      Path(output).parent.mkdir(parents=True, exist_ok=True)
      Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
      print(json.dumps(report["summary"], ensure_ascii=False))
      return all(r["passed"] for r in results)
    finally:
      intent_logger.removeHandler(intent_failure_handler)
      for key in env_overrides:
          os.environ.pop(key, None)
      if rag_temp:
          rag_temp.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="eval/commerce/cases.json")
    parser.add_argument("--output", default="eval/commerce/report.json")
    parser.add_argument("--with-model", action="store_true", help="Enable semantic intent and answer LLMs")
    parser.add_argument("--with-rag", action="store_true", help="Build and use an isolated local RAG index")
    parser.add_argument("--embedding-provider", choices=("local", "auto", "remote"), default="local",
                        help="Embedding backend for RAG evaluation (default: local, deterministic)")
    parser.add_argument("--pdf-parser", choices=("pdfplumber", "mineru_api"), default="pdfplumber",
                        help="PDF parser used to build the evaluation index (default: pdfplumber)")
    args = parser.parse_args()
    logging.disable(logging.INFO)
    raise SystemExit(0 if run(args.dataset, args.output, with_model=args.with_model,
                              with_rag=args.with_rag, embedding_provider=args.embedding_provider,
                              parser=args.pdf_parser) else 1)
