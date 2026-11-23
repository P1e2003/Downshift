"""Unit and CLI tests for Downshift dry-run explain diagnostics and read-only ledger audits (US-4.5 / Issue #40)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "benchmarks"))

from downshift_core import (
    DownshiftConfig,
    classify_subagent_call,
    explain_subagent_call,
)
from run_benchmark import SCENARIOS
from stats_cli import render_ascii_explain

STATS_CLI = ROOT / "scripts" / "stats_cli.py"
RUN_HOOK_CMD = ROOT / "scripts" / "run_hook.cmd"
RUN_HOOK_SH = ROOT / "scripts" / "run_hook.sh"


def _launcher_args(*extra: str) -> list[str]:
    if os.name == "nt":
        return [str(RUN_HOOK_CMD), "./scripts/stats_cli.py", *extra]
    return ["sh", str(RUN_HOOK_SH), "./scripts/stats_cli.py", *extra]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "DOWNSHIFT_MODE",
        "DOWNSHIFT_ENABLED",
        "DOWNSHIFT_HINTS",
        "DOWNSHIFT_CONFIG_PATH",
        "DOWNSHIFT_LEDGER_PATH",
        "DOWNSHIFT_MAX_BURST",
    ):
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def test_explain_matches_classify_subagent_call_across_all_benchmark_cases() -> None:
    assert len(SCENARIOS) == 30
    for sc in SCENARIOS:
        raw_cfg = sc.get("config")
        cfg_obj = (
            DownshiftConfig(
                high_risk_keywords=tuple(raw_cfg.get("high_risk_keywords", ())),
                pinned_roles=dict(raw_cfg.get("pinned_roles", {})),
            )
            if isinstance(raw_cfg, dict)
            else DownshiftConfig()
        )
        current_model = "inherit" if sc["host"] == "antigravity" else ""
        expected = classify_subagent_call(
            type_name=sc["type_name"],
            role=sc["role"],
            prompt=sc["prompt"],
            current_model=current_model,
            host=sc["host"],
            config=cfg_obj,
        )
        trace = explain_subagent_call(
            type_name=sc["type_name"],
            role=sc["role"],
            prompt=sc["prompt"],
            current_model=current_model,
            host=sc["host"],
            config=cfg_obj,
        )
        assert trace.decision.tier == expected.tier, sc["id"]
        assert trace.decision.target_model == expected.target_model, sc["id"]
        assert trace.decision.should_overwrite == expected.should_overwrite, sc["id"]
        assert trace.decision.reason == expected.reason, sc["id"]
        assert 1 <= trace.winning_stage <= 8, sc["id"]


def test_explain_reports_masked_tokens_and_guardrail_match(tmp_path: Path) -> None:
    # 1. Benign read-only slice mapper mentioning src/migrations/ and PATCH /api/users
    benign = explain_subagent_call(
        type_name="research",
        role="Slice 1 Migrations & Auth Mapper @medium",
        prompt="Inspect src/migrations/ and PATCH /api/users for contract fields",
        host="antigravity",
        workspace_dir=tmp_path,
        env={},
    )
    assert benign.decision.tier == "fast"
    assert benign.decision.reason == "inline_override:fast"
    assert benign.winning_stage == 5
    assert "@medium" in [t.lower() for t in benign.matched_tags]
    assert "PATCH /api/users" in benign.masked_tokens
    assert "src/migrations/" in benign.masked_tokens
    assert benign.matched_guardrail_label == ""
    assert benign.is_readonly_subagent is True
    assert benign.is_readonly_role is True
    assert benign.is_write_capable is False
    assert benign.config_source == "defaults"

    # 2. Genuine security audit hits Stage 4 safety guardrail with literal substring
    audit = explain_subagent_call(
        type_name="research",
        role="Security Auditor @medium",
        prompt="Audit auth middleware for IDOR bypass vulnerability",
        host="antigravity",
        workspace_dir=tmp_path,
        env={},
    )
    assert audit.decision.tier == "frontier"
    assert audit.decision.reason == "safety_guardrail:security_or_auth"
    assert audit.winning_stage == 4
    assert audit.matched_guardrail_label == "security_or_auth"
    assert audit.matched_guardrail_text.lower() in (
        "security",
        "auth",
        "idor",
        "bypass",
        "vulnerability",
    )

    # 3. Workspace .downshift.json provenance + custom high_risk_keywords
    cfg_file = tmp_path / ".downshift.json"
    cfg_file.write_text(
        json.dumps(
            {
                "mode": "shadow",
                "max_burst_subagents": 6,
                "high_risk_keywords": ["pawn_vault"],
                "pinned_roles": {"Release Lead": "frontier"},
            }
        ),
        encoding="utf-8",
    )
    custom_hit = explain_subagent_call(
        type_name="research",
        role="Mapper @medium",
        prompt="Inspect pawn_vault settlement summary",
        host="antigravity",
        workspace_dir=tmp_path,
        env={},
    )
    assert custom_hit.config_source == str(cfg_file)
    assert custom_hit.effective_mode == "shadow"
    assert custom_hit.max_burst_subagents == 6
    assert custom_hit.winning_stage == 3
    assert custom_hit.decision.reason == "workspace_config:high_risk_keyword"
    assert custom_hit.matched_guardrail_label == "custom_high_risk_keyword"
    assert custom_hit.matched_guardrail_text == "pawn_vault"


def test_explain_shows_all_three_hosts_and_codex_reasoning_effort(
    tmp_path: Path,
) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(STATS_CLI),
            "explain",
            "--type",
            "research",
            "--role",
            "Slice 1 Migrations & Auth Mapper @medium",
            "--prompt",
            "Inspect src/migrations/ and PATCH /api/users",
            "--workspace",
            str(tmp_path),
            "--host",
            "all",
            "--json",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=_clean_env(),
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert set(data["hosts"].keys()) == {"antigravity", "claude", "codex"}
    assert data["hosts"]["antigravity"]["target_model"] == "flash"
    assert data["hosts"]["antigravity"]["reasoning_effort"] is None
    assert data["hosts"]["claude"]["target_model"] == "sonnet"
    assert data["hosts"]["codex"]["target_model"] == "gpt-6.1-sol"
    assert data["hosts"]["codex"]["reasoning_effort"] == "medium"

    # Single-host filter (--host codex) + ASCII <= 88-col width check
    proc_codex = subprocess.run(
        [
            sys.executable,
            str(STATS_CLI),
            "explain",
            "--type",
            "research",
            "--role",
            "Locator @low",
            "--prompt",
            "Find where load_config is defined",
            "--workspace",
            str(tmp_path),
            "--host",
            "codex",
            "--json",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=_clean_env(),
        timeout=15,
        check=False,
    )
    assert proc_codex.returncode == 0, proc_codex.stderr
    codex_data = json.loads(proc_codex.stdout)
    assert list(codex_data["hosts"].keys()) == ["codex"]
    assert codex_data["hosts"]["codex"]["target_model"] == "gpt-6-luna"
    assert codex_data["hosts"]["codex"]["reasoning_effort"] == "low"

    trace = explain_subagent_call(
        type_name="research",
        role="Slice 1 Migrations & Auth Mapper @medium",
        prompt="Inspect src/migrations/ and PATCH /api/users",
        workspace_dir=tmp_path,
        env={},
    )
    ascii_out = render_ascii_explain(trace, host_filter="all")
    assert "downshift explain" in ascii_out
    assert "effort=medium" in ascii_out
    for line in ascii_out.splitlines():
        if not line.startswith("  ╭─"):
            assert len(line) <= 88, f"Line exceeded 88 cols ({len(line)}): {line!r}"


def test_explain_and_audit_ledger_never_modify_ledger_file(tmp_path: Path) -> None:
    ledger = tmp_path / "immutable_ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "timestamp": "2026-11-23T03:00:00Z",
                "event_type": "route",
                "host": "antigravity",
                "subagent_type": "research",
                "role": "Slice Mapper",
                "tier": "frontier",
                "reason": "safety_guardrail:alembic_or_migration",
                "overwritten": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    before_hash = _sha256(ledger)
    missing_ledger = tmp_path / "should_not_be_created.jsonl"

    proc_explain = subprocess.run(
        [
            sys.executable,
            str(STATS_CLI),
            "explain",
            "--type",
            "research",
            "--role",
            "Locator",
            "--prompt",
            "Find stats_cli.py",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=_clean_env({"DOWNSHIFT_LEDGER_PATH": str(missing_ledger)}),
        timeout=15,
        check=False,
    )
    assert proc_explain.returncode == 0, proc_explain.stderr
    assert not missing_ledger.exists()

    proc_audit = subprocess.run(
        [
            sys.executable,
            str(STATS_CLI),
            "--ledger",
            str(ledger),
            "--audit-ledger",
            "--json",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=_clean_env({"DOWNSHIFT_LEDGER_PATH": str(ledger)}),
        timeout=15,
        check=False,
    )
    assert proc_audit.returncode == 0, proc_audit.stderr
    assert _sha256(ledger) == before_hash

    proc_audit_missing = subprocess.run(
        [
            sys.executable,
            str(STATS_CLI),
            "--ledger",
            str(missing_ledger),
            "--audit-ledger",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=_clean_env({"DOWNSHIFT_LEDGER_PATH": str(missing_ledger)}),
        timeout=15,
        check=False,
    )
    assert proc_audit_missing.returncode == 0, proc_audit_missing.stderr
    assert not missing_ledger.exists()


def test_run_hook_cmd_forwards_explain_flags_on_windows(tmp_path: Path) -> None:
    ws_dir = tmp_path / "space workspace"
    ws_dir.mkdir()
    cfg_file = ws_dir / ".downshift.json"
    cfg_file.write_text(json.dumps({"max_burst_subagents": 7}), encoding="utf-8")
    isolated_ledger = tmp_path / "launcher_explain_unused.jsonl"

    # 12 args after script path (exceeds Windows %2..%9 cap) plus spaces, '&', and '!'
    prompt_with_bang = "Inspect src/migrations/ and PATCH /api/users now!"
    cmd = _launcher_args(
        "explain",
        "--type",
        "research",
        "--role",
        "Slice 1 Migrations & Auth Mapper @medium",
        "--prompt",
        prompt_with_bang,
        "--workspace",
        str(ws_dir),
        "--host",
        "all",
        "--json",
    )
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=_clean_env({"DOWNSHIFT_LEDGER_PATH": str(isolated_ledger)}),
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r}, stdout={proc.stdout!r}"
    data = json.loads(proc.stdout)
    assert data["decision"]["tier"] == "fast"
    assert data["decision"]["reason"] == "inline_override:fast"
    assert data["config_source"] == str(cfg_file)
    assert data["max_burst_subagents"] == 7
    assert data["prompt_chars"] == len(prompt_with_bang)
    assert "PATCH /api/users" in data["masked_tokens"]
    assert "src/migrations/" in data["masked_tokens"]
    assert set(data["hosts"].keys()) == {"antigravity", "claude", "codex"}
    assert data["hosts"]["codex"]["reasoning_effort"] == "medium"
    assert not isolated_ledger.exists()
