"""Unit and CLI tests for Downshift local JSONL telemetry ledger & stats_cli.py (US-1.2 / Issue #5)."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from downshift_core import (
    RoutingDecision,
    _get_max_ledger_bytes,
    record_routing_event,
)
from stats_cli import (
    TIER_COST_WEIGHT,
    compute_summary,
    load_events,
    render_ascii_scoreboard,
)

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
    assert entry["mode"] == "active"
    assert "prompt" not in entry
    assert "Prompt" not in entry


def test_record_routing_event_supports_shadow_mode(tmp_path: Path) -> None:
    ledger = tmp_path / "shadow_ledger.jsonl"
    decision = RoutingDecision(
        host="antigravity",
        tier="lite",
        target_model="flash_lite",
        should_overwrite=True,
        reason="read_only_simple_lookup",
    )
    record_routing_event(
        decision=decision,
        conversation_id="conv-shadow-1",
        subagent_type="research",
        role="Locator",
        original_model="inherit",
        ledger_path=ledger,
        mode="shadow",
    )
    entry = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert entry["mode"] == "shadow"
    assert entry["overwritten"] is True
    assert entry["target_model"] == "flash_lite"


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


def test_cost_multiplier_savings_and_legacy_event_defaults(tmp_path: Path) -> None:
    assert TIER_COST_WEIGHT == {"frontier": 1.00, "fast": 0.20, "lite": 0.04}

    # Empty ledger state
    empty_summary = compute_summary([])
    assert empty_summary["est_cost_saved_pct"] == 0.0
    assert empty_summary["active_count"] == 0
    assert empty_summary["shadow_count"] == 0
    assert empty_summary["total_prompt_chars"] == 0

    ledger = tmp_path / "cost_roi.jsonl"
    # 1. Lite active call (120 chars) -> weight 0.04
    record_routing_event(
        decision=RoutingDecision(
            host="antigravity",
            tier="lite",
            target_model="flash_lite",
            should_overwrite=True,
            reason="read_only_simple_lookup",
        ),
        conversation_id="c-1",
        subagent_type="research",
        role="Locator",
        original_model="inherit",
        ledger_path=ledger,
        mode="active",
        prompt_chars=120,
    )
    # 2. Fast shadow call (380 chars) -> weight 0.20
    record_routing_event(
        decision=RoutingDecision(
            host="claude",
            tier="fast",
            target_model="sonnet",
            should_overwrite=True,
            reason="read_only_research_survey",
        ),
        conversation_id="c-2",
        subagent_type="Explore",
        role="Surveyor",
        original_model="inherit",
        ledger_path=ledger,
        mode="shadow",
        prompt_chars=380,
    )
    # 3. Append a legacy v0.1.0 line without mode or prompt_chars (frontier -> weight 1.00)
    legacy_line = json.dumps(
        {
            "timestamp": "2026-10-30T05:00:00Z",
            "conversation_id": "c-legacy",
            "host": "antigravity",
            "subagent_type": "self",
            "role": "Implementer",
            "original_model": "inherit",
            "target_model": "inherit",
            "tier": "frontier",
            "reason": "frontier_default_stateful_work",
            "overwritten": False,
        }
    )
    with ledger.open("a", encoding="utf-8") as f:
        f.write(legacy_line + "\n")

    loaded = load_events(ledger)
    summary = compute_summary(loaded)
    assert summary["total"] == 3
    assert summary["active_count"] == 2
    assert summary["shadow_count"] == 1
    assert summary["total_prompt_chars"] == 500
    # actual_weighted = 0.04 + 0.20 + 1.00 = 1.24; saved = 1.0 - (1.24 / 3) = 58.7%
    assert summary["est_cost_saved_pct"] == 58.7

    board = render_ascii_scoreboard(summary, ledger)
    assert "58.7%" in board
    assert "2 active, 1 shadow" in board
    assert "500 prompt chars" in board


def test_ledger_auto_rotation_keeps_newest_half_under_byte_limit(
    tmp_path: Path, monkeypatch
) -> None:
    # 1. Invalid or non-positive env values fall back to 5 MB (5_242_880)
    assert _get_max_ledger_bytes({}) == 5_242_880
    assert _get_max_ledger_bytes({"DOWNSHIFT_MAX_LEDGER_BYTES": "invalid"}) == 5_242_880
    assert _get_max_ledger_bytes({"DOWNSHIFT_MAX_LEDGER_BYTES": "0"}) == 5_242_880
    assert _get_max_ledger_bytes({"DOWNSHIFT_MAX_LEDGER_BYTES": "-500"}) == 5_242_880
    assert _get_max_ledger_bytes({"DOWNSHIFT_MAX_LEDGER_BYTES": "1024"}) == 1024

    # 2. Enforce 1024-byte cap across 20 writes
    monkeypatch.setenv("DOWNSHIFT_MAX_LEDGER_BYTES", "1024")
    ledger = tmp_path / "rotated_ledger.jsonl"

    for idx in range(20):
        ok = record_routing_event(
            decision=RoutingDecision(
                host="antigravity",
                tier="lite",
                target_model="flash_lite",
                should_overwrite=True,
                reason="read_only_simple_lookup",
            ),
            conversation_id=f"conv-rot-{idx}",
            subagent_type="research",
            role=f"Role {idx}",
            original_model="inherit",
            ledger_path=ledger,
            mode="active",
            prompt_chars=42,
        )
        assert ok is True
        assert ledger.stat().st_size <= 1024

    events = load_events(ledger)
    assert 1 <= len(events) < 10
    assert events[-1]["conversation_id"] == "conv-rot-19"


def test_stats_cli_aggregates_hosts_reasons_and_guardrail_count(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "scoreboard_v2.jsonl"
    raw_events = [
        {
            "host": "antigravity",
            "tier": "lite",
            "target_model": "flash_lite",
            "overwritten": True,
            "reason": "read_only_simple_lookup",
            "prompt_chars": 120,
        },
        {
            "host": "codex",
            "tier": "lite",
            "target_model": "gpt-6-luna",
            "overwritten": True,
            "reason": "burst_cap_downgraded_to_lite",
            "prompt_chars": 210,
        },
        {
            "host": "claude",
            "tier": "fast",
            "target_model": "sonnet",
            "overwritten": True,
            "reason": "read_only_research_survey",
            "prompt_chars": 340,
        },
        {
            "host": "antigravity",
            "tier": "frontier",
            "target_model": "inherit",
            "overwritten": False,
            "reason": "safety_guardrail:security_or_auth",
            "prompt_chars": 500,
        },
        {
            "host": "codex",
            "tier": "frontier",
            "target_model": "inherit",
            "overwritten": False,
            "reason": "workspace_config:high_risk_keyword",
            "prompt_chars": 400,
        },
        {
            "host": "claude",
            "tier": "frontier",
            "target_model": "inherit",
            "overwritten": False,
            "reason": "workspace_config:pinned_role",
            "prompt_chars": 300,
        },
        {
            "host": "claude",
            "tier": "lite",
            "target_model": "haiku",
            "overwritten": True,
            "reason": "workspace_config:pinned_role",
            "prompt_chars": 100,
        },
        # Legacy v0.1.0 row omitting host and reason
        {
            "tier": "frontier",
            "target_model": "inherit",
            "overwritten": False,
        },
    ]

    summary = compute_summary(raw_events)
    assert summary["hosts"] == {"antigravity": 3, "claude": 3, "codex": 2}
    assert summary["reasons"] == {
        "read_only_simple_lookup": 1,
        "burst_cap_downgraded_to_lite": 1,
        "read_only_research_survey": 1,
        "safety_guardrail:security_or_auth": 1,
        "workspace_config:high_risk_keyword": 1,
        "workspace_config:pinned_role": 2,
        "unknown": 1,
    }
    assert summary["guardrail_count"] == 3

    board = render_ascii_scoreboard(summary, ledger)
    assert "[flash_lite / haiku / gpt-6-luna · 0.04x cost]" in board
    assert "[flash / sonnet / gpt-6.1-sol · 0.20x cost]" in board
    assert (
        "  Hosts & guardrails          : antigravity: 3, claude: 3, codex: 2 · 3 high-risk call(s) protected by guardrails\n"
        in board
    )


def test_stats_cli_clean_removes_only_zero_char_roleless_and_corrupted_rows(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "dirty_ledger.jsonl"
    rows = [
        json.dumps(
            {
                "conversation_id": "c-valid-1",
                "role": "Codebase Researcher",
                "prompt_chars": 320,
                "tier": "fast",
            }
        ),
        json.dumps(
            {
                "conversation_id": "c-role-only",
                "role": "Git Status Checker",
                "prompt_chars": 0,
                "tier": "lite",
            }
        ),
        json.dumps(
            {
                "conversation_id": "c-prompt-only",
                "role": "",
                "prompt_chars": 45,
                "tier": "lite",
            }
        ),
        json.dumps(
            {
                "conversation_id": "c-synthetic-1",
                "role": "",
                "prompt_chars": 0,
                "tier": "frontier",
            }
        ),
        json.dumps(
            {
                "conversation_id": "c-synthetic-2",
                "role": "   ",
                "prompt_chars": 0,
                "tier": "frontier",
            }
        ),
        "CORRUPTED_NOT_JSON",
        "[1, 2, 3]",
    ]
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(STATS_CLI), "--ledger", str(ledger), "--clean"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0
    assert (
        f"Cleaned 4 synthetic/corrupted event(s) from {ledger} (3 valid event(s) retained)"
        in proc.stdout
    )
    remaining = load_events(ledger)
    assert [ev["conversation_id"] for ev in remaining] == [
        "c-valid-1",
        "c-role-only",
        "c-prompt-only",
    ]


def test_stats_cli_clean_handles_missing_ledger(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_ledger.jsonl"
    proc = subprocess.run(
        [sys.executable, str(STATS_CLI), "--ledger", str(missing), "--clean"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0
    assert (
        f"Cleaned 0 synthetic/corrupted event(s) from {missing} (0 valid event(s) retained)"
        in proc.stdout
    )
    assert not missing.exists()


def test_concurrent_clean_and_append_preserves_integrity(tmp_path: Path) -> None:
    from downshift_core import clean_ledger_file

    ledger = tmp_path / "concurrent_clean.jsonl"
    # Seed 5 synthetic rows + 5 valid rows
    seed_lines = []
    for i in range(5):
        seed_lines.append(
            json.dumps({"conversation_id": f"syn-{i}", "role": "", "prompt_chars": 0})
        )
        seed_lines.append(
            json.dumps(
                {"conversation_id": f"val-{i}", "role": "Finder", "prompt_chars": 80}
            )
        )
    ledger.write_text("\n".join(seed_lines) + "\n", encoding="utf-8")

    def _worker(idx: int) -> None:
        if idx % 5 == 0:
            clean_ledger_file(ledger)
        else:
            record_routing_event(
                decision=RoutingDecision(
                    host="antigravity",
                    tier="lite",
                    target_model="flash_lite",
                    should_overwrite=True,
                    reason="read_only_simple_lookup",
                ),
                conversation_id=f"live-{idx}",
                subagent_type="research",
                role="Locator",
                original_model="inherit",
                ledger_path=ledger,
                prompt_chars=64,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_worker, range(30)))

    raw_lines = [
        ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert len(raw_lines) >= 5
    for ln in raw_lines:
        parsed = json.loads(ln)
        assert isinstance(parsed, dict)
        assert (
            int(parsed.get("prompt_chars") or 0) > 0
            or str(parsed.get("role") or "").strip() != ""
        )
