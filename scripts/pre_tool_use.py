"""Multi-host PreToolUse lifecycle hook entrypoint for Downshift."""

from __future__ import annotations

import json
import sys
from typing import Any

from downshift_core import (
    classify_subagent_call,
    detect_host,
    extract_workspace_dir,
    load_config,
    record_routing_event,
)

CLAUDE_SUBAGENT_TOOLS = frozenset({"task", "agent"})


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def handle_pre_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    host = detect_host(payload)
    config = load_config(workspace_dir=extract_workspace_dir(payload))
    if config.mode == "off":
        return {"decision": "allow"} if host == "antigravity" else {}

    if host == "antigravity":
        tool_call = _as_dict(payload.get("toolCall"))
        tool_name = str(tool_call.get("name") or "")
        if tool_name != "invoke_subagent":
            return {"decision": "allow"}

        args = _as_dict(tool_call.get("args"))
        type_name = str(args.get("TypeName") or "")
        role = str(args.get("Role") or "")
        prompt_text = str(args.get("Prompt") or "")
        orig_model = str(args.get("Model") or "inherit")
        decision = classify_subagent_call(
            type_name=type_name,
            role=role,
            prompt=prompt_text,
            current_model=orig_model,
            host="antigravity",
            config=config,
        )
        record_routing_event(
            decision=decision,
            conversation_id=str(payload.get("conversationId") or ""),
            subagent_type=type_name,
            role=role,
            original_model=orig_model,
            mode=config.mode,
            prompt_chars=len(prompt_text),
        )
        if config.mode == "active" and decision.should_overwrite:
            return {
                "decision": "allow",
                "overwrite": {
                    "Model": decision.target_model,
                },
            }
        return {"decision": "allow"}

    # Claude Code / OpenAI Codex PreToolUse contract
    tool_name = str(payload.get("tool_name") or "")
    if tool_name.lower() not in CLAUDE_SUBAGENT_TOOLS:
        return {}

    tool_input = dict(_as_dict(payload.get("tool_input")))
    type_name = str(tool_input.get("subagent_type") or "general-purpose")
    role = str(tool_input.get("description") or "")
    prompt_text = str(tool_input.get("prompt") or "")
    orig_model = str(tool_input.get("model") or "")
    decision = classify_subagent_call(
        type_name=type_name,
        role=role,
        prompt=prompt_text,
        current_model=orig_model,
        host=host,
        config=config,
    )
    record_routing_event(
        decision=decision,
        conversation_id=str(payload.get("session_id") or ""),
        subagent_type=type_name,
        role=role,
        original_model=orig_model or "inherit",
        mode=config.mode,
        prompt_chars=len(prompt_text),
    )
    if config.mode == "active" and decision.should_overwrite:
        updated_input = dict(tool_input)
        updated_input["model"] = decision.target_model
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated_input,
            }
        }
    return {}


def main() -> int:
    try:
        raw_bytes = sys.stdin.buffer.read()
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw_bytes.decode(sys.stdin.encoding or "utf-8", errors="replace")
        if not raw or not raw.strip():
            sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
            return 0
        response = handle_pre_tool_use(payload)
        sys.stdout.write(json.dumps(response) + "\n")
        return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        # Fail open so the host agent loop is never blocked
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
