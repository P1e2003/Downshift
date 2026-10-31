"""Core deterministic subagent model routing engine & safety guardrails for Downshift."""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VALID_MODES = frozenset({"active", "shadow", "off"})
VALID_TIERS = frozenset({"lite", "fast", "frontier"})

READ_ONLY_SUBAGENTS = frozenset({"research", "explore"})
GENERAL_SUBAGENTS = frozenset(
    {"self", "general-purpose", "default", "agent", "task", ""}
)

HIGH_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "alembic_or_migration",
        re.compile(r"\b(alembic|migrations?|schema\s+change|ddl)\b", re.IGNORECASE),
    ),
    (
        "concurrency_or_locking",
        re.compile(
            r"\b(deadlocks?|race\s+conditions?|advisory[_\s]+locks?|for\s+update|savepoints?|rollbacks?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "security_or_auth",
        re.compile(
            r"\b(security|auth(?:entication|orization)?|permissions?|idor|cryptograph(?:y|ic)|jwt|csrf|xss|secrets?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "production_incident",
        re.compile(
            r"\b(production\s+incident|outage|data\s+corruption|billing\s+account)\b",
            re.IGNORECASE,
        ),
    ),
)

FRONTIER_TAG_RE = re.compile(r"(?:^|\s)@(pro|opus|frontier|inherit)\b", re.IGNORECASE)
LITE_TAG_RE = re.compile(r"(?:^|\s)@(flash_lite|lite|haiku)\b", re.IGNORECASE)
FAST_TAG_RE = re.compile(r"(?:^|\s)@(flash|sonnet|fast|downshift)\b", re.IGNORECASE)

SIMPLE_LOOKUP_RE = re.compile(
    r"\b(find\s+file|locate|where\s+is|git\s+status|git\s+branch|list\s+files|count\s+lines)\b",
    re.IGNORECASE,
)
COMPLEX_RESEARCH_RE = re.compile(
    r"\b(survey|trace\s+all|analyze|architecture|summarize\s+how|compare|audit|across\s+the\s+backend)\b",
    re.IGNORECASE,
)

VERIFICATION_RUNNER_RE = re.compile(
    r"\b(pytest|vitest|ruff|eslint|npm\s+run\s+(?:test|lint|build)|uv\s+run\s+(?:pytest|ruff)|run\s+tests?)\b",
    re.IGNORECASE,
)
CODE_MUTATION_RE = re.compile(
    r"\b(implement|refactor|fix\s+bug|edit\s+file|write\s+code|create\s+endpoint|modify|patch)\b",
    re.IGNORECASE,
)

ROUTINE_PROMPT_NUDGE_RE = re.compile(
    r"\b(search\s+the\s+(?:logs?|codebase)|find\s+all\s+(?:files|occurrences|callers)|list\s+all\s+callers|scan\s+logs?)\b",
    re.IGNORECASE,
)

HOST_MODEL_MAP: dict[str, dict[str, str]] = {
    "antigravity": {
        "lite": "flash_lite",
        "fast": "flash",
        "frontier": "inherit",
    },
    "claude": {
        "lite": "haiku",
        "fast": "sonnet",
        "frontier": "inherit",
    },
    "codex": {
        "lite": "flash_lite",
        "fast": "flash",
        "frontier": "inherit",
    },
}


@dataclass(frozen=True)
class DownshiftConfig:
    mode: str = "active"
    hints: bool = True
    high_risk_keywords: tuple[str, ...] = ()
    pinned_roles: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingDecision:
    host: str
    tier: str
    target_model: str
    should_overwrite: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromptAdvisory:
    host: str
    should_inject: bool
    message: str
    reason: str


def detect_host(payload: dict[str, Any], env: dict[str, str] | None = None) -> str:
    """Identify the calling agent host ('antigravity', 'claude', or 'codex')."""
    env_map = env if env is not None else os.environ
    if (
        "toolCall" in payload
        or "lastUserInput" in payload
        or "invocationNum" in payload
    ):
        return "antigravity"
    if env_map.get("CODEX_CI") or env_map.get("CODEX_HOME"):
        return "codex"
    if (
        "tool_name" in payload
        or "tool_input" in payload
        or "hook_event_name" in payload
    ):
        return "claude"
    if env_map.get("CLAUDE_PLUGIN_ROOT") or env_map.get("CLAUDE_PROJECT_DIR"):
        return "claude"
    return "antigravity"


def extract_workspace_dir(payload: dict[str, Any]) -> str | None:
    """Extract the workspace directory from an Antigravity or Claude/Codex hook payload."""
    if not isinstance(payload, dict):
        return None
    ws_paths = payload.get("workspacePaths")
    if isinstance(ws_paths, list):
        for item in ws_paths:
            if isinstance(item, str) and item.strip():
                return item.strip()
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    return None


def _find_config_file(
    workspace_dir: str | Path | None, env_map: Mapping[str, str]
) -> Path | None:
    explicit = (env_map.get("DOWNSHIFT_CONFIG_PATH") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None

    roots_to_walk: list[Path] = []
    if workspace_dir is not None:
        with contextlib.suppress(OSError, ValueError, TypeError):
            ws_path = Path(workspace_dir).expanduser().resolve()
            roots_to_walk.append(ws_path if ws_path.is_dir() else ws_path.parent)
    with contextlib.suppress(OSError):
        cwd_path = Path.cwd().resolve()
        if cwd_path not in roots_to_walk:
            roots_to_walk.append(cwd_path)

    for start in roots_to_walk:
        for directory in (start, *start.parents):
            candidate = directory / ".downshift.json"
            with contextlib.suppress(OSError):
                if candidate.is_file():
                    return candidate
    return None


def load_config(
    workspace_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> DownshiftConfig:
    """Load workspace .downshift.json and apply environment variable overrides (fail-open)."""
    env_map: Mapping[str, str] = env if env is not None else os.environ
    mode = "active"
    hints = True
    high_risk_keywords: tuple[str, ...] = ()
    pinned_roles: dict[str, str] = {}

    cfg_path = _find_config_file(workspace_dir, env_map)
    if cfg_path is not None:
        with contextlib.suppress(OSError, UnicodeDecodeError, ValueError, TypeError):
            raw_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw_data, dict):
                raw_mode = raw_data.get("mode")
                if (
                    isinstance(raw_mode, str)
                    and raw_mode.strip().lower() in VALID_MODES
                ):
                    mode = raw_mode.strip().lower()
                raw_hints = raw_data.get("hints")
                if isinstance(raw_hints, bool):
                    hints = raw_hints
                raw_keywords = raw_data.get("high_risk_keywords")
                if isinstance(raw_keywords, list):
                    high_risk_keywords = tuple(
                        kw.strip()
                        for kw in raw_keywords
                        if isinstance(kw, str) and kw.strip()
                    )
                raw_pinned = raw_data.get("pinned_roles")
                if isinstance(raw_pinned, dict):
                    for k, v in raw_pinned.items():
                        if (
                            isinstance(k, str)
                            and k.strip()
                            and isinstance(v, str)
                            and v.strip().lower() in VALID_TIERS
                        ):
                            pinned_roles[k.strip()] = v.strip().lower()

    env_mode = (env_map.get("DOWNSHIFT_MODE") or "").strip().lower()
    if env_mode in VALID_MODES:
        mode = env_mode
    env_enabled = (env_map.get("DOWNSHIFT_ENABLED") or "").strip().lower()
    if env_enabled in ("0", "false", "no", "off"):
        mode = "off"

    env_hints = (env_map.get("DOWNSHIFT_HINTS") or "").strip().lower()
    if env_hints in ("0", "false", "no", "off"):
        hints = False
    elif env_hints in ("1", "true", "yes", "on"):
        hints = True

    return DownshiftConfig(
        mode=mode,
        hints=hints,
        high_risk_keywords=high_risk_keywords,
        pinned_roles=pinned_roles,
    )


def _match_pinned_role(role: str, pinned_roles: Mapping[str, str]) -> str | None:
    norm_role = (role or "").strip().lower()
    if not norm_role or not pinned_roles:
        return None
    for pattern, tier in pinned_roles.items():
        if pattern.lower() in norm_role and tier in VALID_TIERS:
            return tier
    return None


def _match_custom_high_risk(
    combined_text: str, keywords: tuple[str, ...]
) -> str | None:
    if not combined_text or not keywords:
        return None
    lower_text = combined_text.lower()
    for kw in keywords:
        norm_kw = kw.strip().lower()
        if norm_kw and norm_kw in lower_text:
            return norm_kw
    return None


def _resolve_target_model(host: str, tier: str) -> str:
    normalized_host = host if host in HOST_MODEL_MAP else "antigravity"
    return HOST_MODEL_MAP[normalized_host].get(tier, "inherit")


def _match_high_risk(combined_text: str) -> str | None:
    for label, pattern in HIGH_RISK_PATTERNS:
        if pattern.search(combined_text):
            return label
    return None


def classify_subagent_call(
    type_name: str,
    role: str,
    prompt: str,
    current_model: str = "inherit",
    host: str = "antigravity",
    config: DownshiftConfig | None = None,
) -> RoutingDecision:
    """Classify a subagent tool call into lite, fast, or frontier with safety guardrails."""
    combined = f"{role or ''}\n{prompt or ''}".strip()
    norm_type = (type_name or "").strip().lower()
    norm_model = (current_model or "").strip()
    cfg = config if config is not None else DownshiftConfig()

    # 1. Explicit inline @tag overrides take highest precedence
    if FRONTIER_TAG_RE.search(combined):
        return RoutingDecision(
            host=host,
            tier="frontier",
            target_model="inherit",
            should_overwrite=False,
            reason="inline_override:frontier",
        )
    if LITE_TAG_RE.search(combined):
        target = _resolve_target_model(host, "lite")
        return RoutingDecision(
            host=host,
            tier="lite",
            target_model=target,
            should_overwrite=(norm_model != target),
            reason="inline_override:lite",
        )
    if FAST_TAG_RE.search(combined):
        target = _resolve_target_model(host, "fast")
        return RoutingDecision(
            host=host,
            tier="fast",
            target_model=target,
            should_overwrite=(norm_model != target),
            reason="inline_override:fast",
        )

    # 2. Preserve caller's explicit non-default model when no inline @tag is present
    if norm_model and norm_model.lower() != "inherit":
        return RoutingDecision(
            host=host,
            tier="frontier",
            target_model=norm_model,
            should_overwrite=False,
            reason="caller_explicit_model_preserved",
        )

    # 3. Workspace pinned_roles pinned to frontier (FR-010)
    pinned_tier = _match_pinned_role(role, cfg.pinned_roles)
    if pinned_tier == "frontier":
        return RoutingDecision(
            host=host,
            tier="frontier",
            target_model="inherit",
            should_overwrite=False,
            reason="workspace_config:pinned_role",
        )

    # 4. Workspace custom high_risk_keywords blocklist (FR-010)
    if _match_custom_high_risk(combined, cfg.high_risk_keywords) is not None:
        return RoutingDecision(
            host=host,
            tier="frontier",
            target_model="inherit",
            should_overwrite=False,
            reason="workspace_config:high_risk_keyword",
        )

    # 5. Hard safety guardrail blocklist — never downgrade high-risk engineering tasks
    risk_match = _match_high_risk(combined)
    if risk_match is not None:
        return RoutingDecision(
            host=host,
            tier="frontier",
            target_model="inherit",
            should_overwrite=False,
            reason=f"safety_guardrail:{risk_match}",
        )

    # 6. Lower-tier (lite / fast) workspace pinned_roles when prompt is safe (FR-010)
    if pinned_tier in ("lite", "fast"):
        target = _resolve_target_model(host, pinned_tier)
        return RoutingDecision(
            host=host,
            tier=pinned_tier,
            target_model=target,
            should_overwrite=True,
            reason="workspace_config:pinned_role",
        )

    # 7. Read-only subagents (research / Explore)
    if norm_type in READ_ONLY_SUBAGENTS:
        if SIMPLE_LOOKUP_RE.search(combined) and not COMPLEX_RESEARCH_RE.search(
            combined
        ):
            target = _resolve_target_model(host, "lite")
            return RoutingDecision(
                host=host,
                tier="lite",
                target_model=target,
                should_overwrite=True,
                reason="read_only_simple_lookup",
            )
        target = _resolve_target_model(host, "fast")
        return RoutingDecision(
            host=host,
            tier="fast",
            target_model=target,
            should_overwrite=True,
            reason="read_only_research_survey",
        )

    # 5. General / self subagents running pure test/lint/build verification without code mutation
    if (
        norm_type in GENERAL_SUBAGENTS
        and VERIFICATION_RUNNER_RE.search(combined)
        and not CODE_MUTATION_RE.search(combined)
    ):
        target = _resolve_target_model(host, "fast")
        return RoutingDecision(
            host=host,
            tier="fast",
            target_model=target,
            should_overwrite=True,
            reason="verification_test_or_lint_runner",
        )

    # 6. Default: keep frontier model for stateful code mutation & general engineering
    return RoutingDecision(
        host=host,
        tier="frontier",
        target_model="inherit",
        should_overwrite=False,
        reason="frontier_default_stateful_work",
    )


def classify_user_prompt(
    last_user_input: str,
    invocation_num: int = 1,
    host: str = "antigravity",
) -> PromptAdvisory:
    """Return an ephemeral delegation reminder on the first turn of routine search/log prompts."""
    text = (last_user_input or "").strip()
    if invocation_num > 1 or not text:
        return PromptAdvisory(
            host=host, should_inject=False, message="", reason="not_first_turn_or_empty"
        )

    if FRONTIER_TAG_RE.search(text) or _match_high_risk(text) is not None:
        return PromptAdvisory(
            host=host,
            should_inject=False,
            message="",
            reason="high_risk_or_frontier_tag",
        )

    if ROUTINE_PROMPT_NUDGE_RE.search(text):
        if host in ("claude", "codex"):
            msg = (
                "[Downshift] Routine multi-step search/log/verification task detected. "
                "Prefer delegating mechanical exploration via Task/subagent (which Downshift routes to Haiku/Sonnet) "
                "rather than looping on the frontier model."
            )
        else:
            msg = (
                "[Downshift] Routine multi-step search/log/verification task detected. "
                "Prefer delegating mechanical exploration via subagent (which Downshift routes to Flash/Haiku) "
                "rather than looping on the frontier model."
            )
        return PromptAdvisory(
            host=host,
            should_inject=True,
            message=msg,
            reason="routine_search_or_log_task",
        )

    return PromptAdvisory(
        host=host, should_inject=False, message="", reason="no_nudge_needed"
    )


def should_nudge_pre_invocation(
    last_user_input: str,
    invocation_num: int = 1,
    host: str = "antigravity",
) -> str | None:
    """Convenience wrapper returning the nudge string or None."""
    advisory = classify_user_prompt(
        last_user_input=last_user_input, invocation_num=invocation_num, host=host
    )
    return advisory.message if advisory.should_inject else None


def get_ledger_path(override: str | Path | None = None) -> Path:
    """Resolve the local JSONL telemetry ledger path."""
    if override is not None:
        return Path(override).expanduser()
    env_path = os.environ.get("DOWNSHIFT_LEDGER_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".gemini" / "downshift_ledger.jsonl"


def _append_line_locked(path: Path, line_bytes: bytes) -> None:
    """Append a line to path with a cross-process file lock on Windows and POSIX."""
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            for _ in range(250):
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.002)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True

        os.lseek(fd, 0, os.SEEK_END)
        os.write(fd, line_bytes)
    finally:
        if locked:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def record_routing_event(
    decision: RoutingDecision,
    conversation_id: str,
    subagent_type: str,
    role: str,
    original_model: str = "inherit",
    ledger_path: str | Path | None = None,
    mode: str = "active",
) -> bool:
    """Append a privacy-safe routing event (never storing raw Prompt text) to the JSONL ledger."""
    try:
        path = get_ledger_path(ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "conversation_id": str(conversation_id or ""),
            "host": decision.host,
            "mode": str(mode or "active"),
            "subagent_type": str(subagent_type or ""),
            "role": str(role or "")[:80],
            "original_model": str(original_model or "inherit"),
            "target_model": decision.target_model,
            "tier": decision.tier,
            "reason": decision.reason,
            "overwritten": decision.should_overwrite,
        }
        line_bytes = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
        _append_line_locked(path, line_bytes)
        return True
    except OSError:
        # Non-blocking fail-open if disk or directory is unwritable
        return False
