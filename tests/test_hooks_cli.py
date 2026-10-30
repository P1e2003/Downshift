"""Subprocess CLI integration tests for pre_tool_use.py and pre_invocation.py (US-1.1 / Issue #4)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRE_TOOL_USE = ROOT / "scripts" / "pre_tool_use.py"
PRE_INVOCATION = ROOT / "scripts" / "pre_invocation.py"


def _run_hook(
    script: Path, payload: dict | str, extra_env: dict[str, str] | None = None
) -> tuple[int, dict]:
    raw_input = json.dumps(payload) if isinstance(payload, dict) else payload
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=raw_input,
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=False,
    )
    out = proc.stdout.strip()
    return proc.returncode, (json.loads(out) if out else {})


def test_antigravity_pre_tool_use_cli_emits_overwrite_json(tmp_path: Path) -> None:
    payload = {
        "conversationId": "conv-123",
        "modelName": "argon",
        "stepIdx": 4,
        "toolCall": {
            "name": "invoke_subagent",
            "args": {
                "TypeName": "research",
                "Role": "Codebase Researcher",
                "Prompt": "Survey all callers of resolve_contact across backend/app/services",
                "Model": "inherit",
            },
        },
    }
    code, data = _run_hook(
        PRE_TOOL_USE,
        payload,
        extra_env={"DOWNSHIFT_LEDGER_PATH": str(tmp_path / "ledger.jsonl")},
    )
    assert code == 0
    assert data == {
        "decision": "allow",
        "overwrite": {
            "Model": "flash",
        },
    }


def test_claude_pre_tool_use_cli_emits_updated_input_json(tmp_path: Path) -> None:
    payload = {
        "session_id": "sess-456",
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "Explore",
            "description": "Locate resolve_contact",
            "prompt": "Find file where resolve_contact is defined",
        },
    }
    code, data = _run_hook(
        PRE_TOOL_USE,
        payload,
        extra_env={"DOWNSHIFT_LEDGER_PATH": str(tmp_path / "ledger.jsonl")},
    )
    assert code == 0
    assert data == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "subagent_type": "Explore",
                "description": "Locate resolve_contact",
                "prompt": "Find file where resolve_contact is defined",
                "model": "haiku",
            },
        }
    }


def test_pre_tool_use_cli_preserves_frontier_on_high_risk_task(tmp_path: Path) -> None:
    payload = {
        "conversationId": "conv-789",
        "modelName": "argon",
        "stepIdx": 2,
        "toolCall": {
            "name": "invoke_subagent",
            "args": {
                "TypeName": "research",
                "Role": "Database Reviewer",
                "Prompt": "Check alembic migration and savepoint rollback locking in postgres.py",
                "Model": "inherit",
            },
        },
    }
    code, data = _run_hook(
        PRE_TOOL_USE,
        payload,
        extra_env={"DOWNSHIFT_LEDGER_PATH": str(tmp_path / "ledger.jsonl")},
    )
    assert code == 0
    assert data == {"decision": "allow"}


def test_pre_tool_use_cli_fails_open_on_invalid_json() -> None:
    code, data = _run_hook(PRE_TOOL_USE, "{not-valid-json")
    assert code == 0
    assert data == {"decision": "allow"}

    # Non-dict truthy nested fields must also fail open with exit code 0
    for malformed in (
        {"toolCall": "not-a-dict"},
        {"toolCall": {"name": "invoke_subagent", "args": [1, 2, 3]}},
        {"tool_name": "Task", "tool_input": 42},
    ):
        m_code, m_data = _run_hook(PRE_TOOL_USE, malformed)
        assert m_code == 0
        assert isinstance(m_data, dict)


def test_pre_invocation_cli_supports_antigravity_and_claude_codex() -> None:
    agy_payload = {
        "conversationId": "conv-100",
        "invocationNum": 1,
        "lastUserInput": "Search the logs and find all files referencing GlobalSearchDialog",
    }
    code_agy, data_agy = _run_hook(PRE_INVOCATION, agy_payload)
    assert code_agy == 0
    assert "injectSteps" in data_agy
    assert "[Downshift]" in data_agy["injectSteps"][0]["ephemeralMessage"]

    # Turn 2 must emit {}
    agy_turn2 = {
        "conversationId": "conv-100",
        "invocationNum": 2,
        "lastUserInput": "Search the logs and find all files referencing GlobalSearchDialog",
    }
    code_turn2, data_turn2 = _run_hook(PRE_INVOCATION, agy_turn2)
    assert code_turn2 == 0
    assert data_turn2 == {}

    # Claude Code / Codex UserPromptSubmit
    claude_payload = {
        "session_id": "sess-200",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Search the codebase and list all callers of NotificationService",
    }
    code_claude, data_claude = _run_hook(PRE_INVOCATION, claude_payload)
    assert code_claude == 0
    assert data_claude["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "[Downshift]" in data_claude["hookSpecificOutput"]["additionalContext"]


def test_pre_tool_use_populates_ledger_and_stats_cli_reads_it(tmp_path: Path) -> None:
    ledger = tmp_path / "e2e_ledger.jsonl"
    stats_cli = ROOT / "scripts" / "stats_cli.py"
    env = {"DOWNSHIFT_LEDGER_PATH": str(ledger)}

    # 1. Lite research call
    _run_hook(
        PRE_TOOL_USE,
        {
            "conversationId": "conv-1",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "Locator",
                    "Prompt": "Find file where resolve_contact is defined",
                    "Model": "inherit",
                },
            },
        },
        extra_env=env,
    )
    # 2. Frontier high-risk call
    _run_hook(
        PRE_TOOL_USE,
        {
            "conversationId": "conv-2",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "Auditor",
                    "Prompt": "Audit alembic migration and savepoint deadlock",
                    "Model": "inherit",
                },
            },
        },
        extra_env=env,
    )

    proc = subprocess.run(
        [sys.executable, str(stats_cli), "--ledger", str(ledger), "--json"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0
    summary = json.loads(proc.stdout)
    assert summary["total"] == 2
    assert summary["saved"] == 1
    assert summary["saved_pct"] == 50.0
    assert summary["tiers"]["lite"] == 1
    assert summary["tiers"]["frontier"] == 1
