"""Multi-host PreInvocation (Antigravity) & UserPromptSubmit (Claude/Codex) hook entrypoint."""

from __future__ import annotations

import json
import sys
from typing import Any

from downshift_core import classify_user_prompt, detect_host


def handle_pre_invocation(payload: dict[str, Any]) -> dict[str, Any]:
    host = detect_host(payload)

    if host == "antigravity":
        invocation_num = int(payload.get("invocationNum") or 1)
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
    except (ValueError, TypeError, KeyError, OSError):
        sys.stdout.write("{}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
