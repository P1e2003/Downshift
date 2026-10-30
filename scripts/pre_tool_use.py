"""Multi-host PreToolUse lifecycle hook entrypoint for Downshift."""

from __future__ import annotations

import json
import sys
from typing import Any

from downshift_core import classify_subagent_call, detect_host, record_routing_event

CLAUDE_SUBAGENT_TOOLS = frozenset({"task", "agent"})


def handle_pre_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    host = detect_host(payload)

    if host == "antigravity":
        tool_call = payload.get("toolCall") or {}
        tool_name = str(tool_call.get("name") or "")
        if tool_name != "invoke_subagent":
            return {"decision": "allow"}

        args = tool_call.get("args") or {}
        type_name = str(args.get("TypeName") or "")
        role = str(args.get("Role") or "")
        orig_model = str(args.get("Model") or "inherit")
        decision = classify_subagent_call(
            type_name=type_name,
            role=role,
            prompt=str(args.get("Prompt") or ""),
            current_model=orig_model,
            host="antigravity",
        )
        record_routing_event(
            decision=decision,
            conversation_id=str(payload.get("conversationId") or ""),
            subagent_type=type_name,
            role=role,
            original_model=orig_model,
        )
        if decision.should_overwrite:
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

    tool_input = dict(payload.get("tool_input") or {})
    type_name = str(tool_input.get("subagent_type") or "general-purpose")
    role = str(tool_input.get("description") or "")
    orig_model = str(tool_input.get("model") or "")
    decision = classify_subagent_call(
        type_name=type_name,
        role=role,
        prompt=str(tool_input.get("prompt") or ""),
        current_model=orig_model,
        host=host,
    )
    record_routing_event(
        decision=decision,
        conversation_id=str(payload.get("session_id") or ""),
        subagent_type=type_name,
        role=role,
        original_model=orig_model or "inherit",
    )
    if decision.should_overwrite:
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
        raw = sys.stdin.read()
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
    except (ValueError, TypeError, KeyError, OSError):
        # Fail open so the host agent loop is never blocked
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
