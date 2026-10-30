"""Unit and CLI tests for Downshift local JSONL telemetry ledger & stats_cli.py (US-1.2 / Issue #5)."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from downshift_core import RoutingDecision, record_routing_event
from stats_cli import compute_summary, load_events, render_ascii_scoreboard

STATS_CLI = ROOT / "scripts" / "stats_cli.py"


def test_record_routing_event_writes_valid_jsonl_without_prompt(tmp_path: Path) -> None:
    ledger = tmp_path / "downshift_ledger.jsonl"
    decision = RoutingDecision(
        host="antigravity",
        tier="fast",
        target_model="flash",
        should_overwrite=True,
        reason="read_only_research_survey",
    )
    record_routing_event(
        decision=decision,
        conversation_id="conv-secret-test",
        subagent_type="research",
        role="Codebase Researcher",
        original_model="inherit",
        ledger_path=ledger,
    )
    assert ledger.exists()
    lines = ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["conversation_id"] == "conv-secret-test"
    assert entry["subagent_type"] == "research"
    assert entry["role"] == "Codebase Researcher"
    assert entry["original_model"] == "inherit"
    assert entry["target_model"] == "flash"
    assert entry["tier"] == "fast"
    assert entry["reason"] == "read_only_research_survey"
    assert entry["overwritten"] is True
    assert "prompt" not in entry
    assert "Prompt" not in entry


def test_stats_cli_empty_ledger_renders_zero_state(tmp_path: Path) -> None:
    missing_ledger = tmp_path / "does_not_exist.jsonl"
    proc = subprocess.run(
        [sys.executable, str(STATS_CLI), "--ledger", str(missing_ledger)],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0
    assert "0 subagents intercepted yet" in proc.stdout


def test_stats_cli_aggregates_tiers_and_percentages(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    events = [
        ("lite", "flash_lite", True, "read_only_simple_lookup"),
        ("lite", "haiku", True, "read_only_simple_lookup"),
        ("fast", "flash", True, "read_only_research_survey"),
        ("fast", "sonnet", True, "verification_test_or_lint_runner"),
        ("frontier", "inherit", False, "safety_guardrail:alembic_or_migration"),
    ]
    for idx, (tier, model, overwritten, reason) in enumerate(events):
        record_routing_event(
            decision=RoutingDecision(
                host="antigravity",
                tier=tier,
                target_model=model,
                should_overwrite=overwritten,
                reason=reason,
            ),
            conversation_id=f"c-{idx}",
            subagent_type="research",
            role="Tester",
            original_model="inherit",
            ledger_path=ledger,
        )

    loaded = load_events(ledger)
    summary = compute_summary(loaded)
    assert summary["total"] == 5
    assert summary["saved"] == 4
    assert summary["saved_pct"] == 80.0
    assert summary["tiers"]["lite"] == 2
    assert summary["tiers"]["fast"] == 2
    assert summary["tiers"]["frontier"] == 1

    board = render_ascii_scoreboard(summary, ledger)
    assert "Total subagents intercepted : 5" in board
    assert "Frontier calls saved        : 4 (80.0%)" in board


def test_stats_cli_skips_corrupted_lines(tmp_path: Path) -> None:
    ledger = tmp_path / "corrupt.jsonl"
    valid_line = json.dumps(
        {
            "timestamp": "2026-10-30T05:00:00Z",
            "conversation_id": "c-1",
            "subagent_type": "research",
            "role": "Finder",
            "original_model": "inherit",
            "target_model": "flash_lite",
            "tier": "lite",
            "reason": "read_only_simple_lookup",
            "overwritten": True,
        }
    )
    ledger.write_text(
        f"{valid_line}\nNOT_VALID_JSON_LINE\n{{broken\n", encoding="utf-8"
    )
    loaded = load_events(ledger)
    assert len(loaded) == 1
    assert loaded[0]["tier"] == "lite"


def test_stats_cli_reset_clears_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "reset_me.jsonl"
    ledger.write_text('{"tier": "lite", "overwritten": true}\n', encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(STATS_CLI), "--ledger", str(ledger), "--reset"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0
    assert ledger.read_text(encoding="utf-8") == ""


def test_concurrent_appends_preserve_all_events_without_corruption(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "concurrent.jsonl"

    def _write_one(idx: int) -> bool:
        return record_routing_event(
            decision=RoutingDecision(
                host="antigravity",
                tier="lite",
                target_model="flash_lite",
                should_overwrite=True,
                reason="read_only_simple_lookup",
            ),
            conversation_id=f"conv-{idx}",
            subagent_type="research",
            role=f"Worker {idx}",
            original_model="inherit",
            ledger_path=ledger,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_write_one, range(40)))

    assert all(results)
    events = load_events(ledger)
    assert len(events) == 40
