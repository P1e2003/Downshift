"""Subprocess CLI and privacy tests for multi-host PostToolUse hook (Spec 012 / Issue #37)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRE_TOOL_USE = ROOT / "scripts" / "pre_tool_use.py"
POST_TOOL_USE = ROOT / "scripts" / "post_tool_use.py"


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


def test_post_tool_use_records_error_outcome_and_returns_empty_json(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "post_outcomes.jsonl"
    env = {"DOWNSHIFT_LEDGER_PATH": str(ledger)}
    secret_prompt = "SECRET_PROMPT_API_KEY_998877"
    secret_err = "Bearer sk-live-secret-token deadline exceeded while calling model"

    # 1. Antigravity PreToolUse (lite) -> PostToolUse (timeout error)
    c_pre, _ = _run_hook(
        PRE_TOOL_USE,
        {
            "conversationId": "conv-post-agy",
            "toolCall": {
                "id": "toolu_agy_1",
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "File Locator",
                    "Prompt": f"Locate login.py ({secret_prompt})",
                },
            },
        },
        extra_env=env,
    )
    assert c_pre == 0

    c_post, d_post = _run_hook(
        POST_TOOL_USE,
        {
            "hookEventName": "PostToolUse",
            "conversationId": "conv-post-agy",
            "stepIdx": 8,
            "toolCall": {
                "id": "toolu_agy_1",
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "File Locator",
                    "Prompt": f"Locate login.py ({secret_prompt})",
                },
            },
            "error": secret_err,
        },
        extra_env=env,
    )
    assert c_post == 0
    assert d_post == {}

    # 2. Claude Code Task PreToolUse (fast) -> PostToolUse (rate_limit via tool_response)
    c_cl_pre, _ = _run_hook(
        PRE_TOOL_USE,
        {
            "session_id": "sess-post-cl",
            "tool_use_id": "toolu_cl_1",
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "Explore",
                "description": "Survey API routes",
                "prompt": "Survey all API routes across backend/app/api",
            },
        },
        extra_env=env,
    )
    assert c_cl_pre == 0
    c_cl_post, d_cl_post = _run_hook(
        POST_TOOL_USE,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "sess-post-cl",
            "tool_use_id": "toolu_cl_1",
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "Explore",
                "description": "Survey API routes",
                "prompt": secret_prompt,
            },
            "tool_response": {
                "is_error": True,
                "error": f"429 rate limit exceeded ({secret_prompt})",
            },
        },
        extra_env=env,
    )
    assert c_cl_post == 0
    assert d_cl_post == {}

    # 3. OpenAI Codex spawn_agent PreToolUse (lite) -> PostToolUse (exit_code=1 error)
    c_cx_pre, _ = _run_hook(
        PRE_TOOL_USE,
        {
            "session_id": "sess-post-cx",
            "tool_use_id": "toolu_cx_1",
            "tool_name": "spawn_agent",
            "tool_input": {
                "role": "Locator",
                "message": "Locate main.py",
            },
        },
        extra_env=env,
    )
    assert c_cx_pre == 0
    c_cx_post, d_cx_post = _run_hook(
        POST_TOOL_USE,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "sess-post-cx",
            "tool_use_id": "toolu_cx_1",
            "tool_name": "spawn_agent",
            "tool_input": {
                "role": "Locator",
                "message": secret_prompt,
            },
            "tool_response": {
                "exit_code": 1,
                "stderr": f"permission denied on {secret_prompt}",
            },
        },
        extra_env=env,
    )
    assert c_cx_post == 0
    assert d_cx_post == {}

    raw_text = ledger.read_text(encoding="utf-8")
    assert secret_prompt not in raw_text
    assert "sk-live-secret-token" not in raw_text
    rows = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
    outcomes = [r for r in rows if r.get("event_type") == "outcome"]
    assert len(outcomes) == 3

    agy_out, cl_out, cx_out = outcomes
    assert agy_out["host"] == "antigravity"
    assert agy_out["tool"] == "invoke_subagent"
    assert agy_out["tool_call_id"] == "toolu_agy_1"
    assert agy_out["failed"] is True
    assert agy_out["error_kind"] == "timeout"
    assert agy_out["routed_tier"] == "lite"
    assert len(agy_out["role_fp"]) == 12

    assert cl_out["host"] == "claude"
    assert cl_out["failed"] is True
    assert cl_out["error_kind"] == "rate_limit"
    assert cl_out["routed_tier"] == "fast"

    assert cx_out["host"] == "codex"
    assert cx_out["failed"] is True
    assert cx_out["error_kind"] == "permission_denied"
    assert cx_out["routed_tier"] == "lite"


def test_post_tool_use_records_success_and_ignores_non_add_swarm_and_off_mode(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "post_success.jsonl"
    env = {"DOWNSHIFT_LEDGER_PATH": str(ledger)}

    # Non-add swarm actions produce zero ledger writes
    for action in ("status", "models", "model", "kill", "title"):
        code, data = _run_hook(
            POST_TOOL_USE,
            {
                "conversationId": "conv-swarm-mgmt",
                "toolCall": {
                    "id": f"toolu_{action}",
                    "name": "swarm",
                    "args": {"action": action, "name": "worker-1"},
                },
            },
            extra_env=env,
        )
        assert code == 0
        assert data == {}
    assert not ledger.exists()

    # Clean swarm(action="add") writes failed=False, error_kind=None
    code_ok, data_ok = _run_hook(
        POST_TOOL_USE,
        {
            "conversationId": "conv-swarm-add",
            "toolCall": {
                "id": "toolu_sw_1",
                "name": "swarm",
                "args": {
                    "action": "add",
                    "name": "scanner",
                    "brief": "Survey modules",
                },
            },
        },
        extra_env=env,
    )
    assert code_ok == 0
    assert data_ok == {}
    rows = [
        json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["event_type"] == "outcome"
    assert rows[0]["tool"] == "swarm"
    assert rows[0]["failed"] is False
    assert rows[0]["error_kind"] is None

    # DOWNSHIFT_MODE=off produces zero additional writes
    code_off, data_off = _run_hook(
        POST_TOOL_USE,
        {
            "conversationId": "conv-off",
            "toolCall": {
                "id": "toolu_off",
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "Locator",
                    "Prompt": "Locate a.py",
                },
            },
            "error": "timeout",
        },
        extra_env={**env, "DOWNSHIFT_MODE": "off"},
    )
    assert code_off == 0
    assert data_off == {}
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1


def test_post_tool_use_fails_open_on_malformed_stdin_and_unwritable_ledger(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "fail_open.jsonl"
    for bad_input in ("", "not-json{", "[]", '"hello"'):
        code, data = _run_hook(
            POST_TOOL_USE,
            bad_input,
            extra_env={"DOWNSHIFT_LEDGER_PATH": str(ledger)},
        )
        assert code == 0
        assert data == {}
    assert not ledger.exists()

    # Point ledger path at an existing directory so os.open fails with IsADirectoryError/PermissionError
    blocked_dir = tmp_path / "dir_as_ledger"
    blocked_dir.mkdir()
    code, data = _run_hook(
        POST_TOOL_USE,
        {
            "conversationId": "conv-unwritable",
            "toolCall": {
                "id": "toolu_bad_disk",
                "name": "invoke_subagent",
                "args": {
                    "TypeName": "research",
                    "Role": "Locator",
                    "Prompt": "Find a.py",
                },
            },
            "error": "timeout",
        },
        extra_env={"DOWNSHIFT_LEDGER_PATH": str(blocked_dir)},
    )
    assert code == 0
    assert data == {}
