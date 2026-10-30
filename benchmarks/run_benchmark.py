"""Reproducible 20-scenario routing accuracy, safety guardrail, and latency benchmark suite."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from downshift_core import classify_subagent_call

PRE_TOOL_USE = ROOT / "scripts" / "pre_tool_use.py"

SCENARIOS: list[dict[str, Any]] = [
    # Category 1: Tier 1 (lite) — Simple bounded file/symbol lookups (5 scenarios)
    {
        "id": "S01",
        "category": "simple_lookup",
        "host": "antigravity",
        "type_name": "research",
        "role": "File Locator",
        "prompt": "Find file where resolve_contact is defined",
        "expected_tier": "lite",
        "expected_model": "flash_lite",
        "high_risk": False,
    },
    {
        "id": "S02",
        "category": "simple_lookup",
        "host": "claude",
        "type_name": "Explore",
        "role": "Symbol Locator",
        "prompt": "Where is TenantMemberORM located in the repository?",
        "expected_tier": "lite",
        "expected_model": "haiku",
        "high_risk": False,
    },
    {
        "id": "S03",
        "category": "simple_lookup",
        "host": "antigravity",
        "type_name": "research",
        "role": "Directory Reader",
        "prompt": "List files in backend/app/services and count lines",
        "expected_tier": "lite",
        "expected_model": "flash_lite",
        "high_risk": False,
    },
    {
        "id": "S04",
        "category": "simple_lookup",
        "host": "claude",
        "type_name": "Explore",
        "role": "Git Helper",
        "prompt": "Check git status and git branch in the workspace",
        "expected_tier": "lite",
        "expected_model": "haiku",
        "high_risk": False,
    },
    {
        "id": "S05",
        "category": "simple_lookup",
        "host": "antigravity",
        "type_name": "self",
        "role": "Quick Formatter",
        "prompt": "Format this JSON object @lite",
        "expected_tier": "lite",
        "expected_model": "flash_lite",
        "high_risk": False,
    },
    # Category 2: Tier 2 (fast) — Multi-file codebase surveys & log scans (5 scenarios)
    {
        "id": "S06",
        "category": "research_survey",
        "host": "antigravity",
        "type_name": "research",
        "role": "Codebase Researcher",
        "prompt": "Survey all webhook handlers across backend/app/api and summarize how inbound messages are processed",
        "expected_tier": "fast",
        "expected_model": "flash",
        "high_risk": False,
    },
    {
        "id": "S07",
        "category": "research_survey",
        "host": "claude",
        "type_name": "Explore",
        "role": "Caller Tracer",
        "prompt": "Trace all callers of NotificationService across the backend and summarize the flow",
        "expected_tier": "fast",
        "expected_model": "sonnet",
        "high_risk": False,
    },
    {
        "id": "S08",
        "category": "research_survey",
        "host": "antigravity",
        "type_name": "research",
        "role": "Log Analyzer",
        "prompt": "Scan the worker logs for timeout errors and group by endpoint",
        "expected_tier": "fast",
        "expected_model": "flash",
        "high_risk": False,
    },
    {
        "id": "S09",
        "category": "research_survey",
        "host": "claude",
        "type_name": "Explore",
        "role": "Frontend Inspector",
        "prompt": "Find all React components that consume useWorkspaceStore and summarize their state usage",
        "expected_tier": "fast",
        "expected_model": "sonnet",
        "high_risk": False,
    },
    {
        "id": "S10",
        "category": "research_survey",
        "host": "antigravity",
        "type_name": "self",
        "role": "Helper",
        "prompt": "Check import graph across services @downshift",
        "expected_tier": "fast",
        "expected_model": "flash",
        "high_risk": False,
    },
    # Category 3: Tier 2 (fast) — Pure test/lint/build verification loops (4 scenarios)
    {
        "id": "S11",
        "category": "verification_loop",
        "host": "antigravity",
        "type_name": "self",
        "role": "Test Runner",
        "prompt": "Run pytest backend/tests/unit -v and ruff check backend/ and report test results",
        "expected_tier": "fast",
        "expected_model": "flash",
        "high_risk": False,
    },
    {
        "id": "S12",
        "category": "verification_loop",
        "host": "claude",
        "type_name": "general-purpose",
        "role": "Frontend Verification",
        "prompt": "Run vitest and eslint on frontend/src and report any failures",
        "expected_tier": "fast",
        "expected_model": "sonnet",
        "high_risk": False,
    },
    {
        "id": "S13",
        "category": "verification_loop",
        "host": "antigravity",
        "type_name": "self",
        "role": "Build Verifier",
        "prompt": "Execute npm run build and npm run test to verify the bundle compiles cleanly",
        "expected_tier": "fast",
        "expected_model": "flash",
        "high_risk": False,
    },
    {
        "id": "S14",
        "category": "verification_loop",
        "host": "claude",
        "type_name": "general-purpose",
        "role": "Lint Runner",
        "prompt": "Run uv run ruff check and uv run pytest on the repository",
        "expected_tier": "fast",
        "expected_model": "sonnet",
        "high_risk": False,
    },
    # Category 4: Tier 3 (frontier) — High-risk guardrails & stateful mutations (6 scenarios)
    {
        "id": "S15",
        "category": "high_risk_or_mutation",
        "host": "antigravity",
        "type_name": "research",
        "role": "Migration Auditor",
        "prompt": "Check alembic migration head conflicts and schema change DDL",
        "expected_tier": "frontier",
        "expected_model": "inherit",
        "high_risk": True,
    },
    {
        "id": "S16",
        "category": "high_risk_or_mutation",
        "host": "antigravity",
        "type_name": "research",
        "role": "Concurrency Debugger",
        "prompt": "Investigate PostgreSQL deadlock, advisory lock ordering, and savepoint rollback in webhook_service.py",
        "expected_tier": "frontier",
        "expected_model": "inherit",
        "high_risk": True,
    },
    {
        "id": "S17",
        "category": "high_risk_or_mutation",
        "host": "claude",
        "type_name": "Explore",
        "role": "Security Reviewer",
        "prompt": "Audit authentication, permission checks, and IDOR boundaries in tenants.py",
        "expected_tier": "frontier",
        "expected_model": "inherit",
        "high_risk": True,
    },
    {
        "id": "S18",
        "category": "high_risk_or_mutation",
        "host": "antigravity",
        "type_name": "self",
        "role": "Backend Engineer",
        "prompt": "Implement the new webhook retry handler and refactor message_service.py",
        "expected_tier": "frontier",
        "expected_model": "inherit",
        "high_risk": True,
    },
    {
        "id": "S19",
        "category": "high_risk_or_mutation",
        "host": "claude",
        "type_name": "general-purpose",
        "role": "Concurrency Engineer",
        "prompt": "Run pytest on race condition and for update row lock contention",
        "expected_tier": "frontier",
        "expected_model": "inherit",
        "high_risk": True,
    },
    {
        "id": "S20",
        "category": "high_risk_or_mutation",
        "host": "antigravity",
        "type_name": "research",
        "role": "Deep Researcher",
        "prompt": "Find all files in backend/ @pro",
        "expected_tier": "frontier",
        "expected_model": "inherit",
        "high_risk": True,
    },
]


def _build_hook_payload(scenario: dict[str, Any]) -> dict[str, Any]:
    if scenario["host"] == "antigravity":
        return {
            "conversationId": f"bench-{scenario['id']}",
            "modelName": "argon",
            "stepIdx": 1,
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": scenario["type_name"],
                    "Role": scenario["role"],
                    "Prompt": scenario["prompt"],
                    "Model": "inherit",
                },
            },
        }
    return {
        "session_id": f"bench-{scenario['id']}",
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": scenario["type_name"],
            "description": scenario["role"],
            "prompt": scenario["prompt"],
        },
    }


def run_benchmark() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    latencies_ms: list[float] = []
    in_memory_us: list[float] = []
    correct = 0
    false_downgrades = 0
    saved_frontier = 0

    with tempfile.TemporaryDirectory() as tmp_dir:
        ledger_path = Path(tmp_dir) / "bench_ledger.jsonl"
        env = os.environ.copy()
        env["DOWNSHIFT_LEDGER_PATH"] = str(ledger_path)

        for sc in SCENARIOS:
            t0_mem = time.perf_counter()
            decision = classify_subagent_call(
                type_name=sc["type_name"],
                role=sc["role"],
                prompt=sc["prompt"],
                current_model="inherit" if sc["host"] == "antigravity" else "",
                host=sc["host"],
            )
            in_memory_us.append((time.perf_counter() - t0_mem) * 1_000_000.0)

            payload = _build_hook_payload(sc)
            t0_cli = time.perf_counter()
            proc = subprocess.run(
                [sys.executable, str(PRE_TOOL_USE)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=env,
                timeout=5,
                check=False,
            )
            elapsed_ms = (time.perf_counter() - t0_cli) * 1000.0
            latencies_ms.append(elapsed_ms)

            tier_ok = decision.tier == sc["expected_tier"]
            model_ok = decision.target_model == sc["expected_model"]
            cli_ok = proc.returncode == 0
            passed = tier_ok and model_ok and cli_ok
            if passed:
                correct += 1
            if sc["high_risk"] and decision.tier != "frontier":
                false_downgrades += 1
            if decision.tier in ("lite", "fast") and decision.should_overwrite:
                saved_frontier += 1

            results.append(
                {
                    "id": sc["id"],
                    "category": sc["category"],
                    "host": sc["host"],
                    "expected_tier": sc["expected_tier"],
                    "actual_tier": decision.tier,
                    "expected_model": sc["expected_model"],
                    "actual_model": decision.target_model,
                    "reason": decision.reason,
                    "passed": passed,
                    "latency_ms": round(elapsed_ms, 2),
                }
            )

    sorted_lat = sorted(latencies_ms)
    p50_ms = round(statistics.median(sorted_lat), 2)
    p95_idx = min(len(sorted_lat) - 1, int(len(sorted_lat) * 0.95))
    p95_ms = round(sorted_lat[p95_idx], 2)
    median_mem_us = round(statistics.median(in_memory_us), 2)
    total = len(SCENARIOS)

    return {
        "total_scenarios": total,
        "passed_scenarios": correct,
        "accuracy_pct": round((correct / total) * 100.0, 1),
        "false_downgrades_on_high_risk": false_downgrades,
        "frontier_calls_saved": saved_frontier,
        "frontier_reduction_pct": round((saved_frontier / total) * 100.0, 1),
        "in_memory_median_us": median_mem_us,
        "p50_latency_ms": p50_ms,
        "p95_latency_ms": p95_ms,
        "scenarios": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Downshift 20-scenario routing benchmark"
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args(argv)

    report = run_benchmark()
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(
            f"Downshift Benchmark ({report['passed_scenarios']}/{report['total_scenarios']} passed -- "
            f"{report['accuracy_pct']}% accuracy)\n"
            f"  High-risk false downgrades : {report['false_downgrades_on_high_risk']}\n"
            f"  Frontier calls saved       : {report['frontier_calls_saved']}/{report['total_scenarios']} "
            f"({report['frontier_reduction_pct']}%)\n"
            f"  In-memory classifier p50   : {report['in_memory_median_us']} us\n"
            f"  Subprocess CLI latency     : p50={report['p50_latency_ms']}ms, p95={report['p95_latency_ms']}ms\n"
        )
    return (
        0
        if report["accuracy_pct"] == 100.0
        and report["false_downgrades_on_high_risk"] == 0
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
