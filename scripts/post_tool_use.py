"""Multi-host PostToolUse lifecycle hook entrypoint for Downshift (Spec 012 / Issue #37)."""

from __future__ import annotations

import json
import sys
from typing import Any

from downshift_core import (
    classify_error_kind,
    compute_role_fingerprint,
    detect_host,
    extract_workspace_dir,
    load_config,
    record_outcome_event,
)

CLAUDE_SUBAGENT_TOOLS = frozenset(
    {"task", "agent", "spawn_agent", "multi_agent_v1__spawn_agent"}
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _extract_claude_or_codex_identity(
    tool_name: str, tool_input: dict[str, Any]
) -> tuple[str, str]:
    norm_tool = tool_name.strip().lower()
    if norm_tool in ("spawn_agent", "multi_agent_v1__spawn_agent"):
        type_name = str(tool_input.get("subagent_type") or "spawn_agent")
        role = str(
            tool_input.get("description") or tool_input.get("role") or "spawn_agent"
        )
        return type_name, role
    type_name = str(tool_input.get("subagent_type") or "general-purpose")
    role = str(tool_input.get("description") or "")
    return type_name, role


def _extract_claude_or_codex_failure(
    payload: dict[str, Any],
) -> tuple[bool, str | None]:
    raw_err = payload.get("error")
    if isinstance(raw_err, str) and raw_err.strip():
        return True, classify_error_kind(raw_err)
    if raw_err and not isinstance(raw_err, bool):
        return True, classify_error_kind(str(raw_err))
    if payload.get("is_error") is True:
        return True, "tool_error"

    tool_resp = payload.get("tool_response")
    if isinstance(tool_resp, dict):
        resp_err = tool_resp.get("error")
        if isinstance(resp_err, str) and resp_err.strip():
            return True, classify_error_kind(resp_err)
        if resp_err and not isinstance(resp_err, bool):
            return True, classify_error_kind(str(resp_err))
        if tool_resp.get("is_error") is True:
            msg = str(
                tool_resp.get("message")
                or tool_resp.get("content")
                or tool_resp.get("stderr")
                or ""
            )
            return True, classify_error_kind(msg) or "tool_error"
        if tool_resp.get("success") is False:
            msg = str(tool_resp.get("message") or tool_resp.get("stderr") or "")
            return True, classify_error_kind(msg) or "tool_error"
        exit_code = tool_resp.get("exit_code")
        if (
            isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and exit_code != 0
        ):
            msg = str(tool_resp.get("stderr") or tool_resp.get("message") or "")
            return True, classify_error_kind(msg) or "tool_error"
    return False, None


def handle_post_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    """Record a privacy-safe PostToolUse outcome event and always return an empty no-op dict."""
    host = detect_host(payload)
    config = load_config(workspace_dir=extract_workspace_dir(payload))
    if config.mode == "off":
        return {}

    if host == "antigravity":
        tool_call = _as_dict(payload.get("toolCall"))
        tool_name = str(tool_call.get("name") or "").strip()
        raw_args = tool_call.get("args")
        if not isinstance(raw_args, dict):
            return {}
        if tool_name == "swarm":
            action = str(raw_args.get("action") or "").strip().lower()
            if action != "add":
                return {}
            subagent_type = "swarm"
            role = str(raw_args.get("name") or "")
            if (
                not role.strip()
                and not str(raw_args.get("brief") or "").strip()
                and not isinstance(raw_args.get("tasks"), list)
            ):
                return {}
        elif tool_name == "invoke_subagent":
            subagent_type = str(raw_args.get("TypeName") or "")
            role = str(raw_args.get("Role") or "")
            if not role.strip() and not str(raw_args.get("Prompt") or "").strip():
                return {}
        else:
            return {}

        raw_err = payload.get("error")
        failed = (isinstance(raw_err, str) and bool(raw_err.strip())) or (
            raw_err is not None
            and not isinstance(raw_err, (str, bool))
            and bool(raw_err)
        )
        error_kind = classify_error_kind(str(raw_err)) if failed else None
        record_outcome_event(
            host="antigravity",
            conversation_id=str(payload.get("conversationId") or ""),
            tool=tool_name,
            tool_call_id=str(tool_call.get("id") or ""),
            role_fp=compute_role_fingerprint(subagent_type, role),
            failed=failed,
            error_kind=error_kind,
        )
        return {}

    # Claude Code / OpenAI Codex PostToolUse contract
    tool_name = str(payload.get("tool_name") or "").strip()
    if tool_name.lower() not in CLAUDE_SUBAGENT_TOOLS:
        return {}
    raw_tool_input = payload.get("tool_input")
    if not isinstance(raw_tool_input, dict):
        return {}
    subagent_type, role = _extract_claude_or_codex_identity(tool_name, raw_tool_input)
    failed, error_kind = _extract_claude_or_codex_failure(payload)
    record_outcome_event(
        host=host,
        conversation_id=str(payload.get("session_id") or ""),
        tool=tool_name,
        tool_call_id=str(payload.get("tool_use_id") or ""),
        role_fp=compute_role_fingerprint(subagent_type, role),
        failed=failed,
        error_kind=error_kind,
    )
    return {}


def main() -> int:
    try:
        raw_bytes = sys.stdin.buffer.read()
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw_bytes.decode(sys.stdin.encoding or "utf-8", errors="replace")
        if not raw or not raw.strip():
            sys.stdout.write("{}\n")
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            sys.stdout.write("{}\n")
            return 0
        response = handle_post_tool_use(payload)
        sys.stdout.write(json.dumps(response) + "\n")
        return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        # Strictly fail-open: never disrupt the host agent loop.
        sys.stdout.write("{}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
