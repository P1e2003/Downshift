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


def test_shadow_mode_logs_event_without_overwriting_model(tmp_path: Path) -> None:
    ledger = tmp_path / "shadow_ledger.jsonl"
    env = {
        "DOWNSHIFT_MODE": "shadow",
        "DOWNSHIFT_LEDGER_PATH": str(ledger),
    }

    # 1. Antigravity PreToolUse in shadow mode -> pass-through without "overwrite", logs mode="shadow"
    agy_payload = {
        "conversationId": "conv-shadow-agy",
        "toolCall": {
            "name": "invoke_subagent",
            "args": {
                "TypeName": "research",
                "Role": "Codebase Researcher",
                "Prompt": "Survey all services across backend/app/services",
                "Model": "inherit",
            },
        },
    }
    code_agy, data_agy = _run_hook(PRE_TOOL_USE, agy_payload, extra_env=env)
    assert code_agy == 0
    assert data_agy == {"decision": "allow"}
    assert "overwrite" not in data_agy

    # 2. Claude Code PreToolUse in shadow mode -> pass-through {}, logs mode="shadow"
    claude_payload = {
        "session_id": "sess-shadow-claude",
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "Explore",
            "description": "Locate file",
            "prompt": "Find file where resolve_contact is defined",
        },
    }
    code_claude, data_claude = _run_hook(PRE_TOOL_USE, claude_payload, extra_env=env)
    assert code_claude == 0
    assert data_claude == {}

    lines = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(lines) == 2
    assert lines[0]["mode"] == "shadow"
    assert lines[0]["target_model"] == "flash"
    assert lines[0]["overwritten"] is True
    assert lines[1]["mode"] == "shadow"
    assert lines[1]["target_model"] == "haiku"
    assert lines[1]["overwritten"] is True

    # 3. PreInvocation in shadow mode -> suppresses hints (FR-006, FR-012)
    inv_code, inv_data = _run_hook(
        PRE_INVOCATION,
        {
            "conversationId": "conv-shadow-inv",
            "invocationNum": 1,
            "lastUserInput": "Search the logs and find all files referencing GlobalSearchDialog",
        },
        extra_env=env,
    )
    assert inv_code == 0
    assert inv_data == {}

    # 4. Off mode -> pass-through and zero ledger writes (FR-007)
    off_ledger = tmp_path / "off_ledger.jsonl"
    off_code, off_data = _run_hook(
        PRE_TOOL_USE,
        agy_payload,
        extra_env={
            "DOWNSHIFT_MODE": "off",
            "DOWNSHIFT_LEDGER_PATH": str(off_ledger),
        },
    )
    assert off_code == 0
    assert off_data == {"decision": "allow"}
    assert not off_ledger.exists()


def test_workspace_downshift_json_respected_by_hooks(tmp_path: Path) -> None:
    workspace = tmp_path / "chaxy_repo"
    sub_dir = workspace / "backend"
    sub_dir.mkdir(parents=True)
    (workspace / ".downshift.json").write_text(
        json.dumps(
            {
                "mode": "active",
                "hints": False,
                "high_risk_keywords": ["vietmoney"],
                "pinned_roles": {"Log Locator": "lite"},
            }
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "ws_ledger.jsonl"
    env = {"DOWNSHIFT_LEDGER_PATH": str(ledger)}

    # 1. Custom high_risk_keyword ("vietmoney") blocks downgrade even on research call
    code_hr, data_hr = _run_hook(
        PRE_TOOL_USE,
        {
            "conversationId": "conv-ws-1",
            "workspacePaths": [str(sub_dir)],
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "Log Locator",
                    "Prompt": "Inspect vietmoney webhook logs",
                    "Model": "inherit",
                },
            },
        },
        extra_env=env,
    )
    assert code_hr == 0
    assert data_hr == {"decision": "allow"}

    # 2. Pinned role ("Log Locator": "lite") routes general self call to flash_lite when safe
    code_pin, data_pin = _run_hook(
        PRE_TOOL_USE,
        {
            "conversationId": "conv-ws-2",
            "workspacePaths": [str(sub_dir)],
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "self",
                    "Role": "Log Locator",
                    "Prompt": "Read recent worker output lines",
                    "Model": "inherit",
                },
            },
        },
        extra_env=env,
    )
    assert code_pin == 0
    assert data_pin == {"decision": "allow", "overwrite": {"Model": "flash_lite"}}

    # 3. hints: false in .downshift.json suppresses PreInvocation hint
    code_inv, data_inv = _run_hook(
        PRE_INVOCATION,
        {
            "conversationId": "conv-ws-3",
            "workspacePaths": [str(sub_dir)],
            "invocationNum": 1,
            "lastUserInput": "Search the logs and find all files referencing GlobalSearchDialog",
        },
        extra_env=env,
    )
    assert code_inv == 0
    assert data_inv == {}


def test_portable_hook_launcher_resolves_from_any_cwd(tmp_path: Path) -> None:
    launcher_cmd = ROOT / "scripts" / "run_hook.cmd"
    assert launcher_cmd.exists()

    external_cwd = tmp_path / "external_dir"
    external_cwd.mkdir()
    ledger = tmp_path / "launcher_ledger.jsonl"

    payload = json.dumps(
        {
            "conversationId": "conv-launcher-1",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "File Locator",
                    "Prompt": "Find file where resolve_contact is defined",
                    "Model": "inherit",
                },
            },
        }
    )
    env = os.environ.copy()
    env["DOWNSHIFT_LEDGER_PATH"] = str(ledger)

    if os.name == "nt":
        cmd = [str(launcher_cmd), "./scripts/pre_tool_use.py"]
    else:
        cmd = ["sh", str(launcher_cmd), "./scripts/pre_tool_use.py"]

    proc = subprocess.run(
        cmd,
        input=payload,
        text=True,
        capture_output=True,
        cwd=external_cwd,
        env=env,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip())
    assert data == {"decision": "allow", "overwrite": {"Model": "flash_lite"}}


def test_portable_hook_launcher_fails_open_when_target_or_runtime_missing(
    tmp_path: Path,
) -> None:
    launcher_cmd = ROOT / "scripts" / "run_hook.cmd"
    if os.name == "nt":
        cmd_pre_tool = [str(launcher_cmd), "./scripts/missing_pre_tool_use.py"]
        cmd_pre_inv = [str(launcher_cmd), "./scripts/missing_pre_invocation.py"]
    else:
        cmd_pre_tool = ["sh", str(launcher_cmd), "./scripts/missing_pre_tool_use.py"]
        cmd_pre_inv = ["sh", str(launcher_cmd), "./scripts/missing_pre_invocation.py"]

    proc_tool = subprocess.run(
        cmd_pre_tool,
        input="{}",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=10,
        check=False,
    )
    assert proc_tool.returncode == 0
    assert json.loads(proc_tool.stdout.strip()) == {"decision": "allow"}

    proc_inv = subprocess.run(
        cmd_pre_inv,
        input="{}",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=10,
        check=False,
    )
    assert proc_inv.returncode == 0
    assert json.loads(proc_inv.stdout.strip()) == {}
