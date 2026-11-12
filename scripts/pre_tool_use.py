"""Multi-host PreToolUse lifecycle hook entrypoint for Downshift."""

from __future__ import annotations

import json
import sys
from typing import Any

from downshift_core import (
    CODEX_TIER_REASONING_EFFORT,
    classify_subagent_call,
    detect_host,
    extract_workspace_dir,
    load_config,
    record_and_apply_burst_cap,
)

CLAUDE_SUBAGENT_TOOLS = frozenset(
    {"task", "agent", "spawn_agent", "multi_agent_v1__spawn_agent"}
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _extract_claude_or_codex_fields(
    tool_name: str, tool_input: dict[str, Any]
) -> tuple[str, str, str]:
    norm_tool = tool_name.strip().lower()
    if norm_tool in ("spawn_agent", "multi_agent_v1__spawn_agent"):
        type_name = str(tool_input.get("subagent_type") or "spawn_agent")
        role = str(
            tool_input.get("description") or tool_input.get("role") or "spawn_agent"
        )
        msg = str(tool_input.get("message") or tool_input.get("prompt") or "")
        items = tool_input.get("items")
        if isinstance(items, list):
            item_texts = [
                str(item.get("text") or "")
                for item in items
                if isinstance(item, dict) and item.get("text")
            ]
            if item_texts:
                joined = "\n".join(item_texts)
                msg = f"{msg}\n{joined}" if msg else joined
        return type_name, role, msg
    type_name = str(tool_input.get("subagent_type") or "general-purpose")
    role = str(tool_input.get("description") or "")
    prompt_text = str(tool_input.get("prompt") or "")
    return type_name, role, prompt_text


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

        raw_args = tool_call.get("args")
        if not isinstance(raw_args, dict):
            return {"decision": "allow"}
        args = raw_args
        type_name = str(args.get("TypeName") or "")
        role = str(args.get("Role") or "")
        prompt_text = str(args.get("Prompt") or "")
        if not role.strip() and not prompt_text.strip():
            return {"decision": "allow"}
        orig_model = str(args.get("Model") or "inherit")
        decision = classify_subagent_call(
            type_name=type_name,
            role=role,
            prompt=prompt_text,
            current_model=orig_model,
            host="antigravity",
            config=config,
        )
        decision = record_and_apply_burst_cap(
            decision=decision,
            conversation_id=str(payload.get("conversationId") or ""),
            subagent_type=type_name,
            role=role,
            original_model=orig_model,
            mode=config.mode,
            prompt_chars=len(prompt_text),
            config=config,
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

    raw_tool_input = payload.get("tool_input")
    if not isinstance(raw_tool_input, dict):
        return {}
    tool_input = dict(raw_tool_input)
    type_name, role, prompt_text = _extract_claude_or_codex_fields(
        tool_name, tool_input
    )
    is_codex_spawn = tool_name.strip().lower() in (
        "spawn_agent",
        "multi_agent_v1__spawn_agent",
    )
    has_user_role = bool(
        str(tool_input.get("description") or tool_input.get("role") or "").strip()
    )
    if not prompt_text.strip() and (
        not has_user_role if is_codex_spawn else not role.strip()
    ):
        return {}
    orig_model = str(tool_input.get("model") or "")
    decision = classify_subagent_call(
        type_name=type_name,
        role=role,
        prompt=prompt_text,
        current_model=orig_model,
        host=host,
        config=config,
    )
    decision = record_and_apply_burst_cap(
        decision=decision,
        conversation_id=str(payload.get("session_id") or ""),
        subagent_type=type_name,
        role=role,
        original_model=orig_model or "inherit",
        mode=config.mode,
        prompt_chars=len(prompt_text),
        config=config,
    )
    if config.mode == "active":
        updated_input: dict[str, Any] | None = None
        if decision.should_overwrite:
            updated_input = dict(tool_input)
            updated_input["model"] = decision.target_model
        if host == "codex" and not (
            decision.tier == "frontier"
            and decision.reason == "caller_explicit_model_preserved"
        ):
            target_effort = CODEX_TIER_REASONING_EFFORT.get(decision.tier)
            cur_effort = str(tool_input.get("reasoning_effort") or "").strip().lower()
            if target_effort and (
                updated_input is not None or cur_effort != target_effort
            ):
                if updated_input is None:
                    updated_input = dict(tool_input)
                updated_input["reasoning_effort"] = target_effort
        if updated_input is not None:
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
