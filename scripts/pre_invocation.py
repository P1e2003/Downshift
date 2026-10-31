"""Multi-host PreInvocation (Antigravity) & UserPromptSubmit (Claude/Codex) hook entrypoint."""

from __future__ import annotations

import json
import sys
from typing import Any

from downshift_core import (
    classify_user_prompt,
    detect_host,
    extract_workspace_dir,
    load_config,
)


def _safe_int(value: Any, default: int = 1) -> int:
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def handle_pre_invocation(payload: dict[str, Any]) -> dict[str, Any]:
    host = detect_host(payload)
    config = load_config(workspace_dir=extract_workspace_dir(payload))
    if config.mode != "active" or not config.hints:
        return {}

    if host == "antigravity":
        invocation_num = _safe_int(payload.get("invocationNum"), default=1)
        last_user_input = str(payload.get("lastUserInput") or "")
        advisory = classify_user_prompt(
            last_user_input=last_user_input,
            invocation_num=invocation_num,
            host="antigravity",
        )
        if advisory.should_inject:
            return {
                "injectSteps": [
                    {
                        "ephemeralMessage": advisory.message,
                    }
                ]
            }
        return {}

    # Claude Code / OpenAI Codex UserPromptSubmit contract
    prompt = str(payload.get("prompt") or "")
    advisory = classify_user_prompt(
        last_user_input=prompt,
        invocation_num=1,
        host=host,
    )
    if advisory.should_inject:
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": advisory.message,
            }
        }
    return {}


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw or not raw.strip():
            sys.stdout.write("{}\n")
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            sys.stdout.write("{}\n")
            return 0
        response = handle_pre_invocation(payload)
        sys.stdout.write(json.dumps(response) + "\n")
        return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        sys.stdout.write("{}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
