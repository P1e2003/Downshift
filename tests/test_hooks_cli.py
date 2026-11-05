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
    raw_input = (
        json.dumps(payload, ensure_ascii=False)
        if isinstance(payload, dict)
        else payload
    )
    env = os.environ.copy()
    for codex_var in ("CODEX_CI", "CODEX_HOME", "PLUGIN_DATA"):
        env.pop(codex_var, None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=raw_input.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=5,
        check=False,
    )
    out = proc.stdout.decode("utf-8", errors="replace").strip()
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


def test_pre_tool_use_cli_fails_open_on_invalid_json(tmp_path: Path) -> None:
    ledger = tmp_path / "invalid_json_ledger.jsonl"
    env = {"DOWNSHIFT_LEDGER_PATH": str(ledger)}
    code, data = _run_hook(PRE_TOOL_USE, "{not-valid-json", extra_env=env)
    assert code == 0
    assert data == {"decision": "allow"}

    # Non-dict truthy nested fields must also fail open with exit code 0 without writing to ledger
    for malformed in (
        {"toolCall": "not-a-dict"},
        {"toolCall": {"name": "invoke_subagent", "args": [1, 2, 3]}},
        {"tool_name": "Task", "tool_input": 42},
    ):
        m_code, m_data = _run_hook(PRE_TOOL_USE, malformed, extra_env=env)
        assert m_code == 0
        assert isinstance(m_data, dict)
    assert not ledger.exists()


def test_pre_tool_use_skips_ledger_on_empty_or_non_dict_args(tmp_path: Path) -> None:
    ledger = tmp_path / "empty_args_ledger.jsonl"
    env = {"DOWNSHIFT_LEDGER_PATH": str(ledger)}
    for payload in (
        {"toolCall": {"name": "invoke_subagent", "args": {}}},
        {
            "toolCall": {
                "name": "invoke_subagent",
                "args": {"TypeName": "research", "Role": "   ", "Prompt": ""},
            }
        },
        {"tool_name": "Task", "tool_input": {}},
        {
            "tool_name": "spawn_agent",
            "tool_input": {"description": "", "message": "   "},
        },
    ):
        code, data = _run_hook(PRE_TOOL_USE, payload, extra_env=env)
        assert code == 0
        assert isinstance(data, dict)
    assert not ledger.exists()


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


def test_pre_tool_use_cli_benign_filename_vs_audit_and_prompt_chars(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "v020_cli_ledger.jsonl"
    env = {"DOWNSHIFT_LEDGER_PATH": str(ledger)}

    lookup_prompt = "Find the file where auth_service.py is defined"
    code_lookup, data_lookup = _run_hook(
        PRE_TOOL_USE,
        {
            "conversationId": "conv-benign-1",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "File Locator",
                    "Prompt": lookup_prompt,
                    "Model": "inherit",
                },
            },
        },
        extra_env=env,
    )
    assert code_lookup == 0
    assert data_lookup == {
        "decision": "allow",
        "overwrite": {"Model": "flash_lite"},
    }

    audit_prompt = "Audit auth_service.py for JWT token bypass"
    code_audit, data_audit = _run_hook(
        PRE_TOOL_USE,
        {
            "conversationId": "conv-audit-1",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "Security Auditor",
                    "Prompt": audit_prompt,
                    "Model": "inherit",
                },
            },
        },
        extra_env=env,
    )
    assert code_audit == 0
    assert data_audit == {"decision": "allow"}

    entries = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(entries) == 2
    assert entries[0]["tier"] == "lite"
    assert entries[0]["prompt_chars"] == len(lookup_prompt)
    assert lookup_prompt not in ledger.read_text(encoding="utf-8")
    assert entries[1]["tier"] == "frontier"
    assert entries[1]["prompt_chars"] == len(audit_prompt)


def test_bilingual_vietnamese_hooks_cli(tmp_path: Path) -> None:
    ledger = tmp_path / "vi_ledger.jsonl"
    env = {"DOWNSHIFT_LEDGER_PATH": str(ledger)}

    # 1. Vietnamese simple lookup -> flash_lite
    code_vi_lite, data_vi_lite = _run_hook(
        PRE_TOOL_USE,
        {
            "conversationId": "conv-vi-1",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "Tìm kiếm",
                    "Prompt": "Tìm file định nghĩa hàm resolve_contact",
                    "Model": "inherit",
                },
            },
        },
        extra_env=env,
    )
    assert code_vi_lite == 0
    assert data_vi_lite == {
        "decision": "allow",
        "overwrite": {"Model": "flash_lite"},
    }

    # 2. Vietnamese high-risk prompt -> frontier pass-through
    code_vi_hr, data_vi_hr = _run_hook(
        PRE_TOOL_USE,
        {
            "conversationId": "conv-vi-2",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "Kiểm toán bảo mật",
                    "Prompt": "Kiểm tra phân quyền và xác thực trong tenants.py",
                    "Model": "inherit",
                },
            },
        },
        extra_env=env,
    )
    assert code_vi_hr == 0
    assert data_vi_hr == {"decision": "allow"}

    # 3. Vietnamese routine prompt on turn 1 -> injects delegation hint
    code_vi_inv, data_vi_inv = _run_hook(
        PRE_INVOCATION,
        {
            "conversationId": "conv-vi-3",
            "invocationNum": 1,
            "lastUserInput": "Quét log và tìm tất cả các file gọi NotificationService",
        },
        extra_env=env,
    )
    assert code_vi_inv == 0
    assert "injectSteps" in data_vi_inv


def test_codex_spawn_agent_and_pre_invocation_cli(tmp_path: Path) -> None:
    ledger = tmp_path / "codex_ledger.jsonl"
    env = {"DOWNSHIFT_LEDGER_PATH": str(ledger)}

    # 1. spawn_agent with message -> Tier 1 gpt-6-luna + reasoning_effort="low"
    code_lite, data_lite = _run_hook(
        PRE_TOOL_USE,
        {
            "session_id": "sess-codex-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {
                "message": "Locate backend/app/api/auth.py and count lines",
            },
        },
        extra_env=env,
    )
    assert code_lite == 0
    assert data_lite == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "message": "Locate backend/app/api/auth.py and count lines",
                "model": "gpt-6-luna",
                "reasoning_effort": "low",
            },
        }
    }

    # 2. multi_agent_v1__spawn_agent with items list -> Tier 2 gpt-6.1-sol + reasoning_effort="medium"
    code_fast, data_fast = _run_hook(
        PRE_TOOL_USE,
        {
            "session_id": "sess-codex-2",
            "hook_event_name": "PreToolUse",
            "tool_name": "multi_agent_v1__spawn_agent",
            "tool_input": {
                "items": [
                    {
                        "type": "text",
                        "text": "Survey all callers of NotificationService across the backend and summarize the flow",
                    }
                ],
                "reasoning_effort": "high",
            },
        },
        extra_env=env,
    )
    assert code_fast == 0
    assert data_fast == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "items": [
                    {
                        "type": "text",
                        "text": "Survey all callers of NotificationService across the backend and summarize the flow",
                    }
                ],
                "model": "gpt-6.1-sol",
                "reasoning_effort": "medium",
            },
        }
    }

    # 3. High-risk spawn_agent call with explicit gpt-6-luna is upgraded to inherit + reasoning_effort="high"
    code_hr, data_hr = _run_hook(
        PRE_TOOL_USE,
        {
            "session_id": "sess-codex-3",
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {
                "message": "Audit alembic migration and savepoint rollback contention",
                "model": "gpt-6-luna",
                "reasoning_effort": "low",
            },
        },
        extra_env=env,
    )
    assert code_hr == 0
    assert data_hr == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "message": "Audit alembic migration and savepoint rollback contention",
                "model": "inherit",
                "reasoning_effort": "high",
            },
        }
    }

    # 4. Codex UserPromptSubmit via PLUGIN_DATA emits hookSpecificOutput.additionalContext
    code_inv, data_inv = _run_hook(
        PRE_INVOCATION,
        {
            "session_id": "sess-codex-4",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Search the codebase and list all callers of NotificationService",
        },
        extra_env={**env, "PLUGIN_DATA": str(tmp_path)},
    )
    assert code_inv == 0
    assert data_inv["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "spawn_agent" in data_inv["hookSpecificOutput"]["additionalContext"]
    assert "systemMessage" not in data_inv


def test_pre_invocation_cli_parallel_fan_out_advisory(tmp_path: Path) -> None:
    # 1. Antigravity PreInvocation on turn 1 parallel mapping/audit prompt
    code_agy, data_agy = _run_hook(
        PRE_INVOCATION,
        {
            "conversationId": "conv-parallel-1",
            "invocationNum": 1,
            "lastUserInput": "Check the new plugin downshift for me, all of it",
        },
    )
    assert code_agy == 0
    msg_agy = data_agy["injectSteps"][0]["ephemeralMessage"]
    assert "4-step parallel fan-out" in msg_agy
    assert "invoke_subagent" in msg_agy
    assert "flash_lite/flash" in msg_agy

    # 2. Claude Code UserPromptSubmit on Vietnamese parallel issue map/compare prompt
    code_claude, data_claude = _run_hook(
        PRE_INVOCATION,
        {
            "session_id": "sess-parallel-claude",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Phân tích issue và đối chiếu spec với codebase",
        },
    )
    assert code_claude == 0
    msg_claude = data_claude["hookSpecificOutput"]["additionalContext"]
    assert "4-step parallel fan-out" in msg_claude
    assert "Task/Explore" in msg_claude
    assert "Haiku/Sonnet" in msg_claude

    # 3. OpenAI Codex UserPromptSubmit via PLUGIN_DATA on parallel issue map prompt
    code_codex, data_codex = _run_hook(
        PRE_INVOCATION,
        {
            "session_id": "sess-parallel-codex",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Map out the issue across backend and frontend",
        },
        extra_env={"PLUGIN_DATA": str(tmp_path)},
    )
    assert code_codex == 0
    msg_codex = data_codex["hookSpecificOutput"]["additionalContext"]
    assert "4-step parallel fan-out" in msg_codex
    assert "spawn_agent" in msg_codex
    assert "gpt-6-luna/gpt-6.1-sol" in msg_codex


def test_parallel_pre_tool_use_concurrent_fan_out(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    ledger = tmp_path / "parallel_fanout_ledger.jsonl"
    env = {"DOWNSHIFT_LEDGER_PATH": str(ledger)}

    slice_payloads = [
        # Slice 1: Antigravity bounded file/symbol lookup (<600 chars, <3 paths) -> Tier 1 lite (flash_lite)
        {
            "conversationId": "conv-fanout-1",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "Slice 1 File Locator",
                    "Prompt": "Locate scripts/downshift_core.py and check where classify_user_prompt is defined",
                    "Model": "inherit",
                },
            },
        },
        # Slice 2: Claude Code bounded file lookup (<600 chars, <3 paths) -> Tier 1 lite (haiku)
        {
            "session_id": "sess-fanout-2",
            "hook_event_name": "PreToolUse",
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "Explore",
                "description": "Slice 2 Manifest Locator",
                "prompt": "Find the file where hooks/claude-codex-hooks.json is located and count lines",
            },
        },
        # Slice 3: Antigravity module-level comparison survey -> Tier 2 fast (flash)
        {
            "conversationId": "conv-fanout-3",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "Slice 3 Backend Mapper",
                    "Prompt": "Survey all callers across scripts/ and tests/ and compare the issue spec against main",
                    "Model": "inherit",
                },
            },
        },
        # Slice 4: OpenAI Codex module-level comparison survey -> Tier 2 fast (gpt-6.1-sol)
        {
            "session_id": "sess-fanout-4",
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {
                "message": "Survey all skill files across skills/ and rules/ and summarize the routing flow",
                "reasoning_effort": "high",
            },
        },
    ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda p: _run_hook(PRE_TOOL_USE, p, extra_env=env), slice_payloads
            )
        )

    assert results[0] == (
        0,
        {"decision": "allow", "overwrite": {"Model": "flash_lite"}},
    )
    assert results[1][0] == 0
    assert results[1][1]["hookSpecificOutput"]["updatedInput"]["model"] == "haiku"
    assert results[2] == (
        0,
        {"decision": "allow", "overwrite": {"Model": "flash"}},
    )
    assert results[3][0] == 0
    assert results[3][1]["hookSpecificOutput"]["updatedInput"]["model"] == "gpt-6.1-sol"
    assert (
        results[3][1]["hookSpecificOutput"]["updatedInput"]["reasoning_effort"]
        == "medium"
    )

    lines = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 4
    tiers = sorted(entry["tier"] for entry in lines)
    assert tiers == ["fast", "fast", "lite", "lite"]
