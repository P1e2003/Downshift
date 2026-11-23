"""Core deterministic subagent model routing engine & safety guardrails for Downshift."""

from __future__ import annotations

import contextlib
import hashlib
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
VALID_ISOLATE_PARALLEL_WRITES = frozenset({"share", "branch", "warn", "off"})
DEFAULT_ISOLATE_PARALLEL_WRITES = "share"

READ_ONLY_SUBAGENTS = frozenset({"research", "explore"})
NON_WRITING_SUBAGENTS = frozenset({"research", "explore", "browser", "image-generator"})
LOWER_TIER_MODEL_TIERS: dict[str, str] = {
    "flash_lite": "lite",
    "haiku": "lite",
    "gpt-6-luna": "lite",
    "flash": "fast",
    "sonnet": "fast",
    "gpt-6.1-sol": "fast",
}
LOWER_TIER_MODELS = frozenset(LOWER_TIER_MODEL_TIERS)
GENERAL_SUBAGENTS = frozenset(
    {
        "self",
        "general-purpose",
        "default",
        "agent",
        "task",
        "spawn_agent",
        "multi_agent_v1__spawn_agent",
        "",
    }
)

HIGH_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "alembic_or_migration",
        re.compile(
            r"\b(alembic|migrations?|schema\s+change|ddl|di\s+trú\s+dữ\s+liệu|di\s+tru\s+du\s+lieu)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "concurrency_or_locking",
        re.compile(
            r"\b(deadlocks?|race\s+conditions?|advisory[_\s]+locks?|for\s+update|savepoints?|rollbacks?|"
            r"khóa\s+bảng|khoa\s+bang|tranh\s+chấp\s+đồng\s+thời|tranh\s+chap\s+dong\s+thoi)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "security_or_auth",
        re.compile(
            r"\b(security|auth(?:entication|orization)?|permissions?|idor|cryptograph(?:y|ic)|jwt|csrf|xss|secrets?|"
            r"malware|backdoors?|obfuscat\w*|steganograph\w*|vulnerabilit(?:y|ies)|exploits?|"
            r"lỗ\s+hổng|lo\s+hong|mã\s+độc|ma\s+doc|"
            r"phân\s+quyền|phan\s+quyen|bảo\s+mật|bao\s+mat|xác\s+thực|xac\s+thuc)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "production_incident",
        re.compile(
            r"\b(production\s+incident|outage|data\s+corruption|billing\s+account|"
            r"lỗi\s+mất\s+tin\s+nhắn|loi\s+mat\s+tin\s+nhan)\b",
            re.IGNORECASE,
        ),
    ),
)

FRONTIER_TAG_RE = re.compile(
    r"(?:^|\s)@(pro|opus|frontier|inherit|high)\b", re.IGNORECASE
)
LITE_TAG_RE = re.compile(r"(?:^|\s)@(flash_lite|lite|haiku|low)\b", re.IGNORECASE)
FAST_TAG_RE = re.compile(
    r"(?:^|\s)@(flash|sonnet|fast|downshift|medium)\b", re.IGNORECASE
)

READ_ONLY_ROLE_RE = re.compile(
    r"\b(research(?:er)?|explor(?:e|er)|map(?:per|ping)|slice\s+\d+|"
    r"inspect(?:or)?|scan(?:ner)?|compar(?:ator)|extract(?:or)|"
    r"locator|lookup|reader|survey(?:or)?|"
    r"nghiên\s+cứu|nghien\s+cuu|khảo\s+sát|khao\s+sat|đối\s+chiếu|doi\s+chieu)\b",
    re.IGNORECASE,
)

SIMPLE_LOOKUP_RE = re.compile(
    r"\b(find\s+(?:the\s+)?file|locate|where\s+is|git\s+status|git\s+branch|list\s+files|count\s+lines|"
    r"read\s+(?:the\s+)?(?:file|files|slice|module|[a-zA-Z0-9_./\\-]+\.[a-zA-Z0-9]+)s?|"
    r"inspect\s+(?:the\s+)?(?:file|files|slice|module|[a-zA-Z0-9_./\\-]+\.[a-zA-Z0-9]+)s?|"
    r"extract\s+(?:the\s+)?(?:route|field|schema|type|signature|boundary|contract)s?|"
    r"tìm\s+file|tim\s+file|tìm\s+hàm|tim\s+ham|tìm\s+vị\s+trí|tim\s+vi\s+tri|"
    r"ở\s+file\s+nào|o\s+file\s+nao|định\s+nghĩa\s+ở\s+đâu|dinh\s+nghia\s+o\s+dau|"
    r"liệt\s+kê\s+tên\s+file|liet\s+ke\s+ten\s+file|"
    r"đọc\s+file|doc\s+file|"
    r"trích\s+xuất\s+(?:route|field|schema|type|chữ\s+ký|chu\s+ky)|"
    r"trich\s+xuat\s+(?:route|field|schema|type|chu\s+ky))\b",
    re.IGNORECASE,
)
COMPLEX_RESEARCH_RE = re.compile(
    r"\b(survey|trace\s+all|analyze|architecture|summarize\s+how|compare|audit|across\s+the\s+backend|"
    r"map\s+(?:out\s+)?slice|extract\s+boundary\s+contracts?|"
    r"what\s+already\s+exists\s+vs|check\s+what\s+(?:already\s+)?exists|"
    r"tóm\s+tắt\s+luồng|tom\s+tat\s+luong|kiểm\s+tra\s+toàn\s+bộ|kiem\s+tra\s+toan\s+bo|"
    r"tìm\s+tất\s+cả|tim\s+tat\s+ca|quét\s+log|quet\s+log|"
    r"phân\s+tích\s+slice|phan\s+tich\s+slice)\b",
    re.IGNORECASE,
)

VERIFICATION_RUNNER_RE = re.compile(
    r"\b(pytest|vitest|ruff|eslint|npm\s+run\s+(?:test|lint|build)|uv\s+run\s+(?:pytest|ruff)|run\s+tests?|"
    r"chạy\s+tests?|chay\s+tests?|chạy\s+pytest|chay\s+pytest|kiểm\s+tra\s+lint|kiem\s+tra\s+lint)\b",
    re.IGNORECASE,
)
CODE_MUTATION_RE = re.compile(
    r"\b(implement|refactor|fix\s+bug|edit\s+file|write\s+code|create\s+endpoint|modify|patch|"
    r"update\s+(?:the\s+)?(?:file|code|function|endpoint|model|test|logic)|"
    r"add\s+(?:a\s+)?(?:new\s+)?(?:field|column|method|route|endpoint|test)|"
    r"(?:delete|remove)\s+(?:the\s+)?(?:file|function|method|route|endpoint|code)|"
    r"apply\s+(?:the\s+)?fix|"
    r"sửa\s+lỗi|sua\s+loi|cập\s+nhật\s+code|cap\s+nhat\s+code|viết\s+thêm|viet\s+them|"
    r"chỉnh\s+sửa|chinh\s+sua|thêm\s+mới|them\s+moi|"
    r"triển\s+khai|trien\s+khai|tái\s+cấu\s+trúc|tai\s+cau\s+truc)\b",
    re.IGNORECASE,
)
WRITE_INTENT_EXTRA_RE = re.compile(
    r"\b(create\s+(?:a\s+)?(?:new\s+)?file|write\s+(?:unit\s+)?tests?|"
    r"fix\s+(?:the\s+)?(?:lint|errors?|types?|bugs?)|"
    r"edit\s+[a-zA-Z0-9_./\\-]+\.[a-zA-Z0-9]+|save\s+changes)\b",
    re.IGNORECASE,
)
READ_ONLY_ROUTING_REASONS: frozenset[str] = frozenset(
    {
        "read_only_simple_lookup",
        "read_only_research_survey",
        "read_only_role_survey",
        "promoted_to_fast:multi_file_or_long_prompt",
        "verification_test_or_lint_runner",
        "burst_cap_downgraded_to_lite",
    }
)

PARALLEL_MAP_NUDGE_RE = re.compile(
    r"\b("
    r"draft\s+(?:out\s+)?(?:an?\s+|the\s+|this\s+|these\s+)?(?:new\s+)?(?:issues?|specs?)|"
    r"plan\s+(?:out\s+)?(?:an?\s+|the\s+|this\s+|these\s+)?(?:new\s+)?(?:issues?|features?|specs?|implementations?|workflows?)|"
    r"break\s+down\s+(?:an?\s+|the\s+|this\s+|these\s+)?(?:new\s+)?(?:issues?|features?|specs?|tasks?)|"
    r"(?:allocate|assign)\s+subagents?|(?:split|divide)\s+(?:into\s+)?(?:slices?|subagents?)|"
    r"map\s+out\s+(?:an?\s+|the\s+|this\s+|these\s+)?(?:new\s+)?(?:issues?|specs?|features?|prs?|tasks?)|"
    r"issue\s+map(?:-out)?|"
    r"compare\s+(?:the\s+)?(?:issue|spec|pr|branch|codebase|against\s+(?:main|develop))|"
    r"check\s+.*\ball\s+of\s+it|"
    r"audit\s+(?:the\s+)?(?:entire|whole|full|new\s+)?(?:plugin|repo|repository|codebase|workspace)|"
    r"across\s+backend\s+and\s+frontend|"
    r"soạn\s+(?:thảo\s+)?issue|soan\s+(?:thao\s+)?issue|"
    r"(?:viết|tạo)\s+đặc\s+tả|(?:viet|tao)\s+dac\s+ta|"
    r"(?:lên|lập)\s+kế\s+hoạch|(?:len|lap)\s+ke\s+hoach|"
    r"chia\s+(?:nhỏ\s+)?(?:tasks?|việc|công\s+việc|slices?|subagents?)|"
    r"chia\s+(?:nho\s+)?(?:tasks?|viec|cong\s+viec|slices?|subagents?)|"
    r"phân\s+bổ\s+(?:subagents?|tasks?|công\s+việc)|"
    r"phan\s+bo\s+(?:subagents?|tasks?|cong\s+viec)|"
    r"đối\s+chiếu\s+(?:issue|spec|code|nhánh)|doi\s+chieu\s+(?:issue|spec|code|nhanh)|"
    r"so\s+sánh\s+(?:issue|spec|code|nhánh)|so\s+sanh\s+(?:issue|spec|code|nhanh)|"
    r"phân\s+tích\s+issue|phan\s+tich\s+issue|"
    r"kiểm\s+tra\s+tất\s+cả|kiem\s+tra\s+tat\s+ca|"
    r"rà\s+soát\s+toàn\s+bộ|ra\s+soat\s+toan\s+bo"
    r")\b",
    re.IGNORECASE,
)

ROUTINE_PROMPT_NUDGE_RE = re.compile(
    r"\b(search\s+the\s+(?:logs?|codebase)|find\s+all\s+(?:files|occurrences|callers)|list\s+all\s+callers|scan\s+logs?|"
    r"tìm\s+tất\s+cả|tim\s+tat\s+ca|quét\s+log|quet\s+log|kiểm\s+tra\s+toàn\s+bộ|kiem\s+tra\s+toan\s+bo|"
    r"tóm\s+tắt\s+luồng|tom\s+tat\s+luong)\b",
    re.IGNORECASE,
)

BATCH_WORKFLOW_COUNT_RE = re.compile(
    r"\b(?:[5-9]|[1-9]\d+)\+?\s*(?:api\s+|backend\s+|frontend\s+|file\s+)?"
    r"(?:files?|endpoints?|controllers?|routes?|services?|modules?|components?|tests?|"
    r"pages?|handlers?|models?|repositories|views?|scripts?|tables?|schemas?)\b",
    re.IGNORECASE,
)
BATCH_WORKFLOW_NUDGE_RE = re.compile(
    r"(?:"
    r"\b(?:audit|migrate|refactor|update|check|review|lint|convert|rename|scan|inspect|fix|"
    r"sweep|process|validate|analyze)\s+"
    r"(?:(?:across\s+)?all\s+(?:the\s+)?(?:(?:[5-9]|[1-9]\d+)\+?\s+)?|"
    r"every\s+|"
    r"across\s+(?:(?:[5-9]|[1-9]\d+)\+?\s+))"
    r"(?:api\s+|backend\s+|frontend\s+)?"
    r"(?:controllers?|endpoints?|routes?|services?|modules?|components?|handlers?|models?|"
    r"repositories|views?|pages?|tests?|files?|scripts?)\b"
    r"|"
    r"\b(?:kiểm\s+tra|kiem\s+tra|rà\s+soát|ra\s+soat|cập\s+nhật|cap\s+nhat|"
    r"chuyển\s+đổi|chuyen\s+doi|sửa|sua|quét|quet)\s+"
    r"(?:tất\s+cả|tat\s+ca|toàn\s+bộ|toan\s+bo)\s+(?:các\s+|cac\s+)?"
    r"(?:(?:[5-9]|[1-9]\d+)\+?\s+)?"
    r"(?:file\s+)?(?:controllers?|endpoints?|routes?|services?|modules?|components?|handlers?|models?|pages?)\b"
    r"|"
    r"\b(?:across|over)\s+(?:all\s+)?(?:[5-9]|[1-9]\d+)\+?\s+"
    r"(?:api\s+|backend\s+|frontend\s+)?"
    r"(?:files?|endpoints?|controllers?|routes?|services?|modules?|components?|tests?|handlers?)\b"
    r")",
    re.IGNORECASE,
)

FILE_PATH_TOKEN_RE = re.compile(
    r"\b[\w./\\-]+?\.(?:py|ts|tsx|js|jsx|json|sql|md|yml|yaml|sh|cmd|toml|ini|cfg|env|lock)\b",
    re.IGNORECASE,
)
DIR_OR_ROUTE_PATH_RE = re.compile(
    r"(?<!\w)/[\w.-]+(?:/[\w.:*-]+)+/?|\b[\w.-]+(?:/[\w.:*-]+){2,}/?|\b[\w.-]+/[\w.:*-]+/"
)
HTTP_ROUTE_METHOD_RE = re.compile(
    r"(?:\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+`?/\S*|\b(?:GET|POST|PUT)/PATCH(?:/DELETE)?\b)"
)
GLOB_PATH_RE = re.compile(r"\b[\w.-]+(?:/[\w.*-]+)+\b")
CODE_IDENTIFIER_RE = re.compile(
    r"\b[a-zA-Z0-9]+(?:_[a-zA-Z0-9]+)+\b|\b[a-z]+(?:[A-Z][a-z0-9]+)+\b|\b(?:[A-Z]+[a-z0-9]+){2,}[A-Z]*\b"
)
STACK_TRACE_RE = re.compile(
    r"(?:Traceback \(most recent call last\):|File \"[^\"]+\", line \d+|\bat [\w.$]+ \([\w./\\-]+:\d+:\d+\))"
)
AUDIT_OR_MUTATION_INTENT_RE = re.compile(
    r"\b(audit|investigate|debug|fix|patch|refactor|implement|vulnerability|exploit|bypass|review|"
    r"kiểm\s+toán|kiem\s+toan|kiểm\s+tra\s+lỗi|kiem\s+tra\s+loi|phân\s+tích\s+lỗi|phan\s+tich\s+loi|"
    r"điều\s+tra|dieu\s+tra|rà\s+soát|ra\s+soat|sửa\s+lỗi|sua\s+loi)\b",
    re.IGNORECASE,
)
READONLY_AUDIT_RISK_RE = re.compile(
    r"\b(malware|obfuscat\w*|steganograph\w*|vulnerabilit(?:y|ies)|exploits?|bypass(?:es|ing)?|"
    r"idor|csrf|xss|cryptograph(?:y|ic)|penetration|audit(?:or|ing|s)?|security\s+review|"
    r"head\s+conflicts?|ddl\s+rollback|lỗ\s+hổng|lo\s+hong|mã\s+độc|ma\s+doc|"
    r"kiểm\s+toán|kiem\s+toan|rà\s+soát\s+bảo\s+mật|ra\s+soat\s+bao\s+mat)\b",
    re.IGNORECASE,
)

DEFAULT_MAX_LEDGER_BYTES = 5_242_880

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
        "lite": "gpt-6-luna",
        "fast": "gpt-6.1-sol",
        "frontier": "inherit",
    },
}

CODEX_TIER_REASONING_EFFORT: dict[str, str] = {
    "lite": "low",
    "fast": "medium",
    "frontier": "high",
}

DEFAULT_MAX_BURST_SUBAGENTS = 4
DEFAULT_BURST_WINDOW_SECONDS = 5
BURST_TAIL_READ_BYTES = 65_536

DEFAULT_AUTO_UPSHIFT = True
DEFAULT_RETRY_WINDOW_SECONDS = 120
DEFAULT_MAX_AUTO_RETRIES = 2

DEFAULT_ADVISORY_COOLDOWN_TURNS = 4
DEFAULT_ADVISORY_COOLDOWN_SECONDS = 120
ADVISORY_STATE_MAX_CONVERSATIONS = 128
ADVISORY_STATE_TTL_SECONDS = 86_400

TIER_RANK: dict[str, int] = {"lite": 0, "fast": 1, "frontier": 2}
HIGH_RISK_SAFETY_REASONS_PREFIX = (
    "safety_guardrail:",
    "swarm_isolated_workspace:",
)
HIGH_RISK_SAFETY_REASONS_EXACT = frozenset(
    {
        "workspace_config:high_risk_keyword",
        "swarm_forked_worker",
    }
)

INLINE_TAG_STRIP_RE = re.compile(
    r"(?:^|\s)@(?:pro|opus|frontier|inherit|high|flash_lite|lite|haiku|low|flash|sonnet|fast|downshift|medium)\b",
    re.IGNORECASE,
)
ROLE_RETRY_SUFFIX_RE = re.compile(
    r"(?:\s*(?:\(|\[)\s*(?:retry|attempt|run|pass|try)\s*#?\s*\d*\s*(?:\)|\])"
    r"|\s+(?:retry|attempt)\s*#?\s*\d*"
    r"|\s*#\s*\d+)\s*$",
    re.IGNORECASE,
)
RETRY_CUE_RE = re.compile(
    r"\b(?:retry(?:ing)?|re-?run|try\s+again|run\s+again|again|"
    r"second\s+attempt|attempt\s*#?\s*[2-9]|"
    r"previous\s+attempt\s+failed|failed\s+earlier|"
    r"fix\s+(?:the\s+)?(?:previous|prior|failed|failing|broken|error|regression|test|lint|build|check|it)|"
    r"thử\s+lại|thu\s+lai|chạy\s+lại|chay\s+lai|sửa\s+lại|sua\s+lai|làm\s+lại|lam\s+lai)\b",
    re.IGNORECASE,
)
ERROR_KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "timeout",
        re.compile(r"\b(timeout|timed?\s*out|deadline\s+exceeded)\b", re.IGNORECASE),
    ),
    (
        "rate_limit",
        re.compile(
            r"\b(rate\s*limit|too\s+many\s+requests|429|quota\s+exceeded)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "auth_error",
        re.compile(
            r"\b(unauthorized|unauthenticated|invalid\s+token|401|auth(?:entication)?\s+fail\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "permission_denied",
        re.compile(
            r"\b(permission\s+denied|forbidden|403|access\s+denied|eacces)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "invalid_args",
        re.compile(
            r"\b(invalid\s+(?:arg|argument|param|parameter|input|model|schema)|validation\s+error|bad\s+request|400)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "not_found",
        re.compile(r"\b(not\s+found|404|enoent|no\s+such\s+file)\b", re.IGNORECASE),
    ),
)

WORKFLOW_AGENT_CALL_RE = re.compile(r"\bagent\s*\(")
WORKFLOW_PARALLEL_CALL_RE = re.compile(r"\bparallel\s*\(")
WORKFLOW_MODEL_ARG_RE = re.compile(r"""\bmodel\s*=\s*["']([^"']+)["']""")

BURST_ELIGIBLE_REASONS: frozenset[str] = frozenset(
    {
        "read_only_research_survey",
        "promoted_to_fast:multi_file_or_long_prompt",
        "verification_test_or_lint_runner",
    }
)


@dataclass(frozen=True)
class DownshiftConfig:
    mode: str = "active"
    hints: bool = True
    high_risk_keywords: tuple[str, ...] = ()
    pinned_roles: dict[str, str] = field(default_factory=dict)
    max_burst_subagents: int = DEFAULT_MAX_BURST_SUBAGENTS
    burst_window_seconds: int = DEFAULT_BURST_WINDOW_SECONDS
    swarm_models: dict[str, str] = field(default_factory=dict)
    auto_upshift: bool = DEFAULT_AUTO_UPSHIFT
    retry_window_seconds: int = DEFAULT_RETRY_WINDOW_SECONDS
    max_auto_retries: int = DEFAULT_MAX_AUTO_RETRIES
    isolate_parallel_writes: str = DEFAULT_ISOLATE_PARALLEL_WRITES
    advisory_cooldown_turns: int = DEFAULT_ADVISORY_COOLDOWN_TURNS
    advisory_cooldown_seconds: int = DEFAULT_ADVISORY_COOLDOWN_SECONDS


@dataclass(frozen=True)
class RoutingDecision:
    host: str
    tier: str
    target_model: str
    should_overwrite: bool
    reason: str
    target_workspace: str = ""
    workspace_guardrail_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoutingTrace:
    """Pure dry-run diagnostic trace of a single subagent routing decision."""

    decision: RoutingDecision
    config_source: str
    effective_mode: str
    max_burst_subagents: int
    winning_stage: int
    winning_stage_name: str
    matched_tags: tuple[str, ...]
    matched_guardrail_label: str
    matched_guardrail_text: str
    matched_pinned_role: str
    masked_tokens: tuple[str, ...]
    prompt_chars: int
    path_count: int
    line_count: int
    has_code_fence: bool
    has_stack_trace: bool
    is_readonly_subagent: bool
    is_readonly_role: bool
    is_simple_lookup: bool
    is_verification_runner: bool
    is_code_mutation: bool
    is_write_capable: bool
    hosts: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["matched_tags"] = list(self.matched_tags)
        data["masked_tokens"] = list(self.masked_tokens)
        return data


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
    tool_name = str(payload.get("tool_name") or "").strip().lower()
    if tool_name in ("spawn_agent", "multi_agent_v1__spawn_agent"):
        return "codex"
    tool_input = payload.get("tool_input")
    if (
        tool_name in ("task", "agent")
        and isinstance(tool_input, dict)
        and "subagent_type" in tool_input
    ):
        return "claude"
    if (
        env_map.get("CODEX_CI")
        or env_map.get("CODEX_HOME")
        or env_map.get("PLUGIN_DATA")
    ):
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
    max_burst_subagents = DEFAULT_MAX_BURST_SUBAGENTS
    burst_window_seconds = DEFAULT_BURST_WINDOW_SECONDS
    swarm_models: dict[str, str] = {}
    auto_upshift = DEFAULT_AUTO_UPSHIFT
    retry_window_seconds = DEFAULT_RETRY_WINDOW_SECONDS
    max_auto_retries = DEFAULT_MAX_AUTO_RETRIES
    isolate_parallel_writes = DEFAULT_ISOLATE_PARALLEL_WRITES
    advisory_cooldown_turns = DEFAULT_ADVISORY_COOLDOWN_TURNS
    advisory_cooldown_seconds = DEFAULT_ADVISORY_COOLDOWN_SECONDS

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
                raw_max_burst = raw_data.get("max_burst_subagents")
                if (
                    isinstance(raw_max_burst, int)
                    and not isinstance(raw_max_burst, bool)
                    and raw_max_burst >= 0
                ):
                    max_burst_subagents = raw_max_burst
                raw_burst_window = raw_data.get("burst_window_seconds")
                if (
                    isinstance(raw_burst_window, int)
                    and not isinstance(raw_burst_window, bool)
                    and raw_burst_window >= 1
                ):
                    burst_window_seconds = raw_burst_window
                raw_swarm_models = raw_data.get("swarm_models")
                if isinstance(raw_swarm_models, dict):
                    for tier_key in ("lite", "fast"):
                        raw_val = raw_swarm_models.get(tier_key)
                        if isinstance(raw_val, str) and raw_val.strip():
                            swarm_models[tier_key] = raw_val.strip()
                raw_auto_upshift = raw_data.get("auto_upshift")
                if isinstance(raw_auto_upshift, bool):
                    auto_upshift = raw_auto_upshift
                raw_retry_window = raw_data.get("retry_window_seconds")
                if (
                    isinstance(raw_retry_window, int)
                    and not isinstance(raw_retry_window, bool)
                    and raw_retry_window >= 1
                ):
                    retry_window_seconds = raw_retry_window
                raw_max_retries = raw_data.get("max_auto_retries")
                if (
                    isinstance(raw_max_retries, int)
                    and not isinstance(raw_max_retries, bool)
                    and raw_max_retries >= 1
                ):
                    max_auto_retries = raw_max_retries
                raw_isolate_writes = raw_data.get("isolate_parallel_writes")
                if isinstance(raw_isolate_writes, bool):
                    isolate_parallel_writes = "share" if raw_isolate_writes else "off"
                elif (
                    isinstance(raw_isolate_writes, str)
                    and raw_isolate_writes.strip().lower()
                    in VALID_ISOLATE_PARALLEL_WRITES
                ):
                    isolate_parallel_writes = raw_isolate_writes.strip().lower()
                raw_cd_turns = raw_data.get("advisory_cooldown_turns")
                if (
                    isinstance(raw_cd_turns, int)
                    and not isinstance(raw_cd_turns, bool)
                    and raw_cd_turns >= 0
                ):
                    advisory_cooldown_turns = raw_cd_turns
                raw_cd_secs = raw_data.get("advisory_cooldown_seconds")
                if (
                    isinstance(raw_cd_secs, int)
                    and not isinstance(raw_cd_secs, bool)
                    and raw_cd_secs >= 0
                ):
                    advisory_cooldown_seconds = raw_cd_secs

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

    env_max_burst = (env_map.get("DOWNSHIFT_MAX_BURST") or "").strip()
    if env_max_burst:
        with contextlib.suppress(ValueError):
            parsed_burst = int(env_max_burst)
            if parsed_burst >= 0:
                max_burst_subagents = parsed_burst

    env_swarm_lite = (env_map.get("DOWNSHIFT_SWARM_LITE_MODEL") or "").strip()
    if env_swarm_lite:
        swarm_models["lite"] = env_swarm_lite
    env_swarm_fast = (env_map.get("DOWNSHIFT_SWARM_FAST_MODEL") or "").strip()
    if env_swarm_fast:
        swarm_models["fast"] = env_swarm_fast

    env_auto_upshift = (env_map.get("DOWNSHIFT_AUTO_UPSHIFT") or "").strip().lower()
    if env_auto_upshift in ("0", "false", "no", "off"):
        auto_upshift = False
    elif env_auto_upshift in ("1", "true", "yes", "on"):
        auto_upshift = True

    env_retry_window = (env_map.get("DOWNSHIFT_RETRY_WINDOW") or "").strip()
    if env_retry_window:
        with contextlib.suppress(ValueError):
            parsed_rw = int(env_retry_window)
            if parsed_rw >= 1:
                retry_window_seconds = parsed_rw

    env_max_retries = (env_map.get("DOWNSHIFT_MAX_AUTO_RETRIES") or "").strip()
    if env_max_retries:
        with contextlib.suppress(ValueError):
            parsed_mr = int(env_max_retries)
            if parsed_mr >= 1:
                max_auto_retries = parsed_mr

    env_isolate_writes = (
        (env_map.get("DOWNSHIFT_ISOLATE_PARALLEL_WRITES") or "").strip().lower()
    )
    if env_isolate_writes in ("0", "false", "no", "off"):
        isolate_parallel_writes = "off"
    elif env_isolate_writes in ("1", "true", "yes", "share"):
        isolate_parallel_writes = "share"
    elif env_isolate_writes in VALID_ISOLATE_PARALLEL_WRITES:
        isolate_parallel_writes = env_isolate_writes

    env_cd_turns = (env_map.get("DOWNSHIFT_ADVISORY_COOLDOWN_TURNS") or "").strip()
    if env_cd_turns:
        with contextlib.suppress(ValueError):
            parsed_ct = int(env_cd_turns)
            if parsed_ct >= 0:
                advisory_cooldown_turns = parsed_ct

    env_cd_secs = (env_map.get("DOWNSHIFT_ADVISORY_COOLDOWN_SECONDS") or "").strip()
    if env_cd_secs:
        with contextlib.suppress(ValueError):
            parsed_cs = int(env_cd_secs)
            if parsed_cs >= 0:
                advisory_cooldown_seconds = parsed_cs

    return DownshiftConfig(
        mode=mode,
        hints=hints,
        high_risk_keywords=high_risk_keywords,
        pinned_roles=pinned_roles,
        max_burst_subagents=max_burst_subagents,
        burst_window_seconds=burst_window_seconds,
        swarm_models=swarm_models,
        auto_upshift=auto_upshift,
        retry_window_seconds=retry_window_seconds,
        max_auto_retries=max_auto_retries,
        isolate_parallel_writes=isolate_parallel_writes,
        advisory_cooldown_turns=advisory_cooldown_turns,
        advisory_cooldown_seconds=advisory_cooldown_seconds,
    )


def resolve_config_source(
    workspace_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the resolved .downshift.json path string, or 'defaults' if none found."""
    env_map: Mapping[str, str] = env if env is not None else os.environ
    cfg_path = _find_config_file(workspace_dir, env_map)
    return str(cfg_path) if cfg_path is not None else "defaults"


def _match_pinned_role_detail(
    role: str, pinned_roles: Mapping[str, str]
) -> tuple[str, str] | None:
    norm_role = (role or "").strip().lower()
    if not norm_role or not pinned_roles:
        return None
    for pattern, tier in pinned_roles.items():
        if pattern.lower() in norm_role and tier in VALID_TIERS:
            return pattern, tier
    return None


def _match_pinned_role(role: str, pinned_roles: Mapping[str, str]) -> str | None:
    match = _match_pinned_role_detail(role, pinned_roles)
    return match[1] if match is not None else None


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


def _match_high_risk_detail(combined_text: str) -> tuple[str, str] | None:
    for label, pattern in HIGH_RISK_PATTERNS:
        m = pattern.search(combined_text)
        if m is not None:
            return label, m.group(0)
    return None


def _match_high_risk(combined_text: str) -> str | None:
    match = _match_high_risk_detail(combined_text)
    return match[0] if match is not None else None


def _match_strict_readonly_risk_detail(text: str) -> tuple[str, str] | None:
    for label, pattern in HIGH_RISK_PATTERNS:
        if label in ("concurrency_or_locking", "production_incident"):
            m = pattern.search(text)
            if m is not None:
                return label, m.group(0)
    return None


def _match_strict_readonly_risk(text: str) -> str | None:
    match = _match_strict_readonly_risk_detail(text)
    return match[0] if match is not None else None


def strip_benign_code_identifiers(text: str) -> str:
    """Mask explicit file/directory/route path tokens and compound code identifiers during read-only lookups."""
    if not text:
        return ""
    masked = FILE_PATH_TOKEN_RE.sub(" <path> ", text)
    masked = DIR_OR_ROUTE_PATH_RE.sub(" <path> ", masked)
    return CODE_IDENTIFIER_RE.sub(" <id> ", masked)


def _extract_masked_tokens(
    text: str, include_benign_identifiers: bool
) -> tuple[str, ...]:
    """Return deduplicated tokens masked by route and/or benign identifier sanitizers."""
    if not text:
        return ()
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        cleaned = tok.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            tokens.append(cleaned)

    working = text
    if "/" in working:
        for m in HTTP_ROUTE_METHOD_RE.finditer(working):
            _add(m.group(0))
        working = HTTP_ROUTE_METHOD_RE.sub(" <route> ", working)

    if include_benign_identifiers:
        for m in FILE_PATH_TOKEN_RE.finditer(working):
            _add(m.group(0))
        working = FILE_PATH_TOKEN_RE.sub(" <path> ", working)
        for m in DIR_OR_ROUTE_PATH_RE.finditer(working):
            _add(m.group(0))
        working = DIR_OR_ROUTE_PATH_RE.sub(" <path> ", working)
        for m in CODE_IDENTIFIER_RE.finditer(working):
            _add(m.group(0))
    return tuple(tokens)


def _extract_inline_tags(text: str) -> tuple[str, ...]:
    """Return deduplicated inline @tag overrides detected in text in tier-precedence order."""
    if not text:
        return ()
    tags: list[str] = []
    seen: set[str] = set()
    for pattern in (FRONTIER_TAG_RE, FAST_TAG_RE, LITE_TAG_RE):
        for m in pattern.finditer(text):
            tok = m.group(0).strip()
            key = tok.lower()
            if tok and key not in seen:
                seen.add(key)
                tags.append(tok)
    return tuple(tags)


def normalize_workspace(workspace: str | None) -> str:
    """Normalize a workspace mode string into 'inherit', 'share', or 'branch'."""
    norm = str(workspace or "").strip().lower()
    if norm in ("share", "branch"):
        return norm
    return "inherit"


def is_write_capable(
    type_name: str,
    role: str,
    prompt: str,
    decision: RoutingDecision | None = None,
    tool: str = "",
) -> bool:
    """Classify whether a subagent/swarm worker may mutate repository files on disk."""
    if (tool or "").strip().lower() == "run_workflow":
        return False
    norm_type = (type_name or "").strip().lower()
    if norm_type in NON_WRITING_SUBAGENTS:
        return False

    raw_combined = f"{role or ''}\n{prompt or ''}"
    route_masked = HTTP_ROUTE_METHOD_RE.sub(" <route> ", raw_combined)
    cleaned = strip_benign_code_identifiers(route_masked)
    has_mutation = bool(
        CODE_MUTATION_RE.search(cleaned) or WRITE_INTENT_EXTRA_RE.search(route_masked)
    )
    if VERIFICATION_RUNNER_RE.search(cleaned) and not has_mutation:
        return False

    has_frontier_tag = bool(FRONTIER_TAG_RE.search(raw_combined))
    if not has_mutation:
        if decision is not None and (
            decision.reason in READ_ONLY_ROUTING_REASONS
            or decision.reason.startswith("swarm_models_unconfigured:")
        ):
            return False
        if READ_ONLY_ROLE_RE.search(role or "") and not has_frontier_tag:
            return False
        if (
            SIMPLE_LOOKUP_RE.search(cleaned) or COMPLEX_RESEARCH_RE.search(cleaned)
        ) and not has_frontier_tag:
            return False

    if has_mutation:
        return True
    if decision is not None and decision.tier == "frontier":
        return True
    return has_frontier_tag


def score_prompt_complexity(prompt: str) -> dict[str, int | bool]:
    """Return structural complexity signals for a subagent prompt."""
    text = prompt or ""
    paths: set[str] = set()
    for match in FILE_PATH_TOKEN_RE.finditer(text):
        paths.add(match.group(0).strip().replace("\\", "/").lower())
    for match in GLOB_PATH_RE.finditer(text):
        paths.add(match.group(0).strip().replace("\\", "/").lower())
    return {
        "char_len": len(text),
        "path_count": len(paths),
        "has_code_fence": "```" in text,
        "has_stack_trace": bool(STACK_TRACE_RE.search(text)),
        "line_count": len(text.splitlines()),
    }


def _classify_subagent_internal(
    type_name: str,
    role: str,
    prompt: str,
    current_model: str = "inherit",
    host: str = "antigravity",
    config: DownshiftConfig | None = None,
    trace_out: dict[str, Any] | None = None,
) -> RoutingDecision:
    """Shared single-source-of-truth classifier for both hot-path routing and dry-run explain traces."""
    combined = f"{role or ''}\n{prompt or ''}".strip()
    norm_type = (type_name or "").strip().lower()
    norm_model = (current_model or "").strip()
    cfg = config if config is not None else DownshiftConfig()

    is_explicit_non_default = bool(
        norm_model and norm_model.lower() not in ("inherit", "default")
    )
    explicit_lower_tier = (
        LOWER_TIER_MODEL_TIERS.get(norm_model.lower())
        if is_explicit_non_default
        else None
    )
    is_explicit_lower_tier = explicit_lower_tier is not None

    def _finish(
        decision: RoutingDecision,
        stage: int,
        stage_name: str,
        *,
        guardrail_label: str = "",
        guardrail_text: str = "",
        pinned_detail: str = "",
    ) -> RoutingDecision:
        if trace_out is not None:
            trace_out["winning_stage"] = stage
            trace_out["winning_stage_name"] = stage_name
            trace_out["matched_guardrail_label"] = guardrail_label
            trace_out["matched_guardrail_text"] = guardrail_text
            trace_out["matched_pinned_role"] = pinned_detail
        return decision

    # 1. Explicit frontier inline @tag overrides take highest precedence
    if FRONTIER_TAG_RE.search(combined):
        return _finish(
            RoutingDecision(
                host=host,
                tier="frontier",
                target_model="inherit",
                should_overwrite=is_explicit_lower_tier,
                reason="inline_override:frontier",
            ),
            1,
            "frontier_inline_tag",
        )

    # 2. Preserve caller's explicit non-default model unless a safety guardrail upgrades a lower-tier model
    if is_explicit_non_default and not is_explicit_lower_tier:
        return _finish(
            RoutingDecision(
                host=host,
                tier="frontier",
                target_model=norm_model,
                should_overwrite=False,
                reason="caller_explicit_model_preserved",
            ),
            2,
            "caller_explicit_frontier_model",
        )

    # 3. Workspace pinned_roles pinned to frontier (FR-010) & custom high_risk_keywords (FR-003)
    pinned_match = _match_pinned_role_detail(role, cfg.pinned_roles)
    pinned_tier = pinned_match[1] if pinned_match is not None else None
    pinned_str = f"{pinned_match[0]} -> {pinned_match[1]}" if pinned_match else ""
    if pinned_tier == "frontier":
        return _finish(
            RoutingDecision(
                host=host,
                tier="frontier",
                target_model="inherit",
                should_overwrite=is_explicit_lower_tier,
                reason="workspace_config:pinned_role",
            ),
            3,
            "workspace_config_guardrail",
            guardrail_label="pinned_role:frontier",
            guardrail_text=pinned_match[0] if pinned_match else "",
            pinned_detail=pinned_str,
        )

    custom_kw = _match_custom_high_risk(combined, cfg.high_risk_keywords)
    if custom_kw is not None:
        return _finish(
            RoutingDecision(
                host=host,
                tier="frontier",
                target_model="inherit",
                should_overwrite=is_explicit_lower_tier,
                reason="workspace_config:high_risk_keyword",
            ),
            3,
            "workspace_config_guardrail",
            guardrail_label="custom_high_risk_keyword",
            guardrail_text=custom_kw,
            pinned_detail=pinned_str,
        )

    # 4. Hard safety guardrail blocklist (with benign identifier & effort-tagged read-only slice disambiguation)
    intent_text = (
        HTTP_ROUTE_METHOD_RE.sub(" <route> ", combined) if "/" in combined else combined
    )
    has_mutation_intent = bool(CODE_MUTATION_RE.search(intent_text))
    has_audit_or_mutation_intent = bool(AUDIT_OR_MUTATION_INTENT_RE.search(intent_text))
    is_sandboxed_readonly = norm_type in READ_ONLY_SUBAGENTS
    has_lower_effort_tag = bool(
        FAST_TAG_RE.search(combined) or LITE_TAG_RE.search(combined)
    )
    is_readonly = is_sandboxed_readonly or (
        norm_type in GENERAL_SUBAGENTS
        and not has_mutation_intent
        and not has_audit_or_mutation_intent
        and not VERIFICATION_RUNNER_RE.search(combined)
        and bool(
            SIMPLE_LOOKUP_RE.search(combined)
            or COMPLEX_RESEARCH_RE.search(combined)
            or READ_ONLY_ROLE_RE.search(role or "")
        )
    )
    simple_lookup_checked = False
    is_simple_lookup = False
    risk_detail = _match_high_risk_detail(combined)
    if risk_detail is not None and is_readonly:
        simple_lookup_checked = True
        is_simple_lookup = bool(SIMPLE_LOOKUP_RE.search(combined)) and not bool(
            COMPLEX_RESEARCH_RE.search(combined)
        )
        if (
            is_simple_lookup
            and not has_audit_or_mutation_intent
            and not has_mutation_intent
        ):
            risk_detail = _match_high_risk_detail(
                strip_benign_code_identifiers(intent_text)
            )
        if (
            risk_detail is not None
            and is_sandboxed_readonly
            and has_lower_effort_tag
            and not has_mutation_intent
            and not READONLY_AUDIT_RISK_RE.search(intent_text)
        ):
            risk_detail = _match_strict_readonly_risk_detail(intent_text)
    if risk_detail is not None:
        return _finish(
            RoutingDecision(
                host=host,
                tier="frontier",
                target_model="inherit",
                should_overwrite=is_explicit_lower_tier,
                reason=f"safety_guardrail:{risk_detail[0]}",
            ),
            4,
            "safety_guardrail",
            guardrail_label=risk_detail[0],
            guardrail_text=risk_detail[1],
            pinned_detail=pinned_str,
        )

    # 5. Lower-tier inline @tag overrides & explicit lower-tier models on safe prompts
    if FAST_TAG_RE.search(combined):
        target = _resolve_target_model(host, "fast")
        return _finish(
            RoutingDecision(
                host=host,
                tier="fast",
                target_model=target,
                should_overwrite=(norm_model.lower() != target.lower()),
                reason="inline_override:fast",
            ),
            5,
            "lower_inline_tag",
            pinned_detail=pinned_str,
        )
    if LITE_TAG_RE.search(combined):
        target = _resolve_target_model(host, "lite")
        return _finish(
            RoutingDecision(
                host=host,
                tier="lite",
                target_model=target,
                should_overwrite=(norm_model.lower() != target.lower()),
                reason="inline_override:lite",
            ),
            5,
            "lower_inline_tag",
            pinned_detail=pinned_str,
        )

    if explicit_lower_tier is not None:
        return _finish(
            RoutingDecision(
                host=host,
                tier=explicit_lower_tier,
                target_model=norm_model,
                should_overwrite=False,
                reason="caller_explicit_model_preserved",
            ),
            5,
            "caller_explicit_lower_model",
            pinned_detail=pinned_str,
        )

    # 6. Lower-tier (lite / fast) workspace pinned_roles when prompt is safe (FR-010)
    if pinned_tier in ("lite", "fast"):
        target = _resolve_target_model(host, pinned_tier)
        return _finish(
            RoutingDecision(
                host=host,
                tier=pinned_tier,
                target_model=target,
                should_overwrite=True,
                reason="workspace_config:pinned_role",
            ),
            6,
            "workspace_pinned_lower_role",
            pinned_detail=pinned_str,
        )

    # 7. Read-only subagents & pure verification runners (FR-004, FR-005)
    if is_readonly:
        if not simple_lookup_checked:
            is_simple_lookup = bool(SIMPLE_LOOKUP_RE.search(combined)) and not bool(
                COMPLEX_RESEARCH_RE.search(combined)
            )
        if is_simple_lookup:
            complexity = score_prompt_complexity(prompt)
            if (
                int(complexity["char_len"]) > 600
                or int(complexity["path_count"]) >= 3
                or bool(complexity["has_code_fence"])
                or bool(complexity["has_stack_trace"])
            ):
                target = _resolve_target_model(host, "fast")
                return _finish(
                    RoutingDecision(
                        host=host,
                        tier="fast",
                        target_model=target,
                        should_overwrite=True,
                        reason="promoted_to_fast:multi_file_or_long_prompt",
                    ),
                    7,
                    "readonly_complexity_promotion",
                )
            target = _resolve_target_model(host, "lite")
            return _finish(
                RoutingDecision(
                    host=host,
                    tier="lite",
                    target_model=target,
                    should_overwrite=True,
                    reason="read_only_simple_lookup",
                ),
                7,
                "readonly_simple_lookup",
            )
        target = _resolve_target_model(host, "fast")
        return _finish(
            RoutingDecision(
                host=host,
                tier="fast",
                target_model=target,
                should_overwrite=True,
                reason="read_only_research_survey",
            ),
            7,
            "readonly_research_survey",
        )

    if (
        norm_type in GENERAL_SUBAGENTS
        and VERIFICATION_RUNNER_RE.search(combined)
        and not has_mutation_intent
    ):
        target = _resolve_target_model(host, "fast")
        return _finish(
            RoutingDecision(
                host=host,
                tier="fast",
                target_model=target,
                should_overwrite=True,
                reason="verification_test_or_lint_runner",
            ),
            7,
            "verification_runner",
        )

    # 8. Default: keep frontier model for stateful code mutation & general engineering
    return _finish(
        RoutingDecision(
            host=host,
            tier="frontier",
            target_model="inherit",
            should_overwrite=False,
            reason="frontier_default_stateful_work",
        ),
        8,
        "frontier_default",
    )


def classify_subagent_call(
    type_name: str,
    role: str,
    prompt: str,
    current_model: str = "inherit",
    host: str = "antigravity",
    config: DownshiftConfig | None = None,
) -> RoutingDecision:
    """Classify a subagent tool call into lite, fast, or frontier with safety guardrails."""
    return _classify_subagent_internal(
        type_name=type_name,
        role=role,
        prompt=prompt,
        current_model=current_model,
        host=host,
        config=config,
        trace_out=None,
    )


def explain_subagent_call(
    type_name: str,
    role: str,
    prompt: str,
    current_model: str = "inherit",
    host: str = "antigravity",
    workspace_dir: str | Path | None = None,
    config: DownshiftConfig | None = None,
    env: Mapping[str, str] | None = None,
) -> RoutingTrace:
    """Produce a 100%-faithful pure dry-run diagnostic trace across all 3 hosts (zero ledger I/O)."""
    if config is not None:
        cfg = config
        cfg_source = (
            resolve_config_source(workspace_dir=workspace_dir, env=env)
            if workspace_dir is not None
            else "explicit_config"
        )
    else:
        cfg_source = resolve_config_source(workspace_dir=workspace_dir, env=env)
        cfg = load_config(workspace_dir=workspace_dir, env=env)

    trace_meta: dict[str, Any] = {}
    decision = _classify_subagent_internal(
        type_name=type_name,
        role=role,
        prompt=prompt,
        current_model=current_model,
        host=host,
        config=cfg,
        trace_out=trace_meta,
    )

    combined = f"{role or ''}\n{prompt or ''}".strip()
    norm_type = (type_name or "").strip().lower()
    intent_text = (
        HTTP_ROUTE_METHOD_RE.sub(" <route> ", combined) if "/" in combined else combined
    )
    has_mutation_intent = bool(CODE_MUTATION_RE.search(intent_text))
    has_audit_or_mutation_intent = bool(AUDIT_OR_MUTATION_INTENT_RE.search(intent_text))
    is_readonly_subagent = norm_type in READ_ONLY_SUBAGENTS
    is_readonly_role = bool(READ_ONLY_ROLE_RE.search(role or ""))
    is_verification_runner = bool(VERIFICATION_RUNNER_RE.search(combined))
    is_readonly = is_readonly_subagent or (
        norm_type in GENERAL_SUBAGENTS
        and not has_mutation_intent
        and not has_audit_or_mutation_intent
        and not is_verification_runner
        and bool(
            SIMPLE_LOOKUP_RE.search(combined)
            or COMPLEX_RESEARCH_RE.search(combined)
            or is_readonly_role
        )
    )
    is_simple_lookup = bool(SIMPLE_LOOKUP_RE.search(combined)) and not bool(
        COMPLEX_RESEARCH_RE.search(combined)
    )
    complexity = score_prompt_complexity(prompt)
    masked_tokens = _extract_masked_tokens(
        combined, include_benign_identifiers=is_readonly
    )
    matched_tags = _extract_inline_tags(combined)
    write_capable = is_write_capable(
        type_name=type_name,
        role=role,
        prompt=prompt,
        decision=decision,
    )

    hosts_matrix: dict[str, dict[str, Any]] = {}
    for target_host in ("antigravity", "claude", "codex"):
        host_decision = (
            decision
            if target_host == host
            else _classify_subagent_internal(
                type_name=type_name,
                role=role,
                prompt=prompt,
                current_model=current_model,
                host=target_host,
                config=cfg,
                trace_out=None,
            )
        )
        hosts_matrix[target_host] = {
            "tier": host_decision.tier,
            "target_model": host_decision.target_model,
            "should_overwrite": host_decision.should_overwrite,
            "reason": host_decision.reason,
            "reasoning_effort": (
                CODEX_TIER_REASONING_EFFORT.get(host_decision.tier, "high")
                if target_host == "codex"
                else None
            ),
        }

    return RoutingTrace(
        decision=decision,
        config_source=cfg_source,
        effective_mode=cfg.mode,
        max_burst_subagents=cfg.max_burst_subagents,
        winning_stage=int(trace_meta.get("winning_stage", 8)),
        winning_stage_name=str(
            trace_meta.get("winning_stage_name", "frontier_default")
        ),
        matched_tags=matched_tags,
        matched_guardrail_label=str(trace_meta.get("matched_guardrail_label", "")),
        matched_guardrail_text=str(trace_meta.get("matched_guardrail_text", "")),
        matched_pinned_role=str(trace_meta.get("matched_pinned_role", "")),
        masked_tokens=masked_tokens,
        prompt_chars=int(complexity["char_len"]),
        path_count=int(complexity["path_count"]),
        line_count=int(complexity["line_count"]),
        has_code_fence=bool(complexity["has_code_fence"]),
        has_stack_trace=bool(complexity["has_stack_trace"]),
        is_readonly_subagent=is_readonly_subagent,
        is_readonly_role=is_readonly_role,
        is_simple_lookup=is_simple_lookup,
        is_verification_runner=is_verification_runner,
        is_code_mutation=has_mutation_intent,
        is_write_capable=write_capable,
        hosts=hosts_matrix,
    )


def classify_user_prompt(
    last_user_input: str,
    invocation_num: int = 1,
    host: str = "antigravity",
    config: DownshiftConfig | None = None,
) -> PromptAdvisory:
    """Classify a user prompt's work shape into direct, 2-4 slice fan-out, homogeneous batch, or routine delegation."""
    text = (last_user_input or "").strip()
    if invocation_num < 1 or not text:
        return PromptAdvisory(
            host=host, should_inject=False, message="", reason="not_first_turn_or_empty"
        )

    cfg = config or load_config()
    if (
        FRONTIER_TAG_RE.search(text)
        or _match_high_risk(text) is not None
        or _match_custom_high_risk(text, cfg.high_risk_keywords) is not None
    ):
        return PromptAdvisory(
            host=host,
            should_inject=False,
            message="",
            reason="high_risk_or_frontier_tag",
        )

    # Work shape 1: Homogeneous batch sweep (5+ items or plural-all sweep verbs)
    has_batch_count = bool(BATCH_WORKFLOW_COUNT_RE.search(text))
    if (has_batch_count or len(text) >= 25) and (
        BATCH_WORKFLOW_NUDGE_RE.search(text) or has_batch_count
    ):
        if host == "codex":
            msg = (
                "[Downshift] Homogeneous batch sweep detected. Chunk items into 2-4 bounded "
                "spawn_agent slices tagged @medium/@low (gpt-6.1-sol/gpt-6-luna) rather than "
                "spawning one subagent per item (avoiding the 4-subagent burst cap), require "
                "compact <=15-line hand-backs, and aggregate results on the frontier main agent."
            )
        elif host == "claude":
            msg = (
                "[Downshift] Homogeneous batch sweep detected. Chunk items into 2-4 bounded "
                "Task/Explore slices tagged @medium/@low (Sonnet/Haiku) rather than spawning "
                "one subagent per item (avoiding the 4-subagent burst cap), require compact "
                "<=15-line hand-backs, and aggregate results on the frontier main agent."
            )
        else:
            msg = (
                "[Downshift] Homogeneous batch sweep detected. Prefer Antigravity's native "
                'run_workflow tool (parallel() / pipeline() with JSON schema and model="flash" '
                'or "flash_lite") over manual invoke_subagent fan-out to avoid hitting the '
                "4-subagent burst cap and keep aggregation deterministic."
            )
        return PromptAdvisory(
            host=host,
            should_inject=True,
            message=msg,
            reason="batch_workflow_recommended",
        )

    # Work shape 2: Heterogeneous multi-layer drafting, planning, mapping, or comparison
    if PARALLEL_MAP_NUDGE_RE.search(text):
        if host == "codex":
            delegation = "spawn_agent calls in 1 turn (Downshift routes bounded slices to gpt-6-luna/gpt-6.1-sol)"
        elif host == "claude":
            delegation = "Task/Explore subagents in 1 turn (Downshift routes bounded slices to Haiku/Sonnet)"
        else:
            delegation = "invoke_subagent calls in 1 turn (Downshift routes bounded slices to flash_lite/flash)"
        msg = (
            "[Downshift] Multi-part issue drafting, planning, or mapping task detected. "
            "Use the 7-stage v2 workflow: (1) fast-path <=2 known files directly (0 subagents), "
            "(2) decompose 3+-file scope into 2-4 disjoint slices in 1 turn, "
            "(3) hand out effort tags (@low unknown-file discovery or 1-4-file contract lookup, @medium 3+-file survey/test/lint, @high architecture/mutations/migrations/locking/auth), "
            f"(4) spawn parallel {delegation}, "
            "(5) require <=15-line 4-field hand-backs (Files & Lines, Exists vs Missing, Boundary Contract, Risks) and reconcile cross-layer boundary contracts on the frontier main agent, "
            "(6) run <=2-file checks directly or heavy suites via @medium with a 2-retry circuit breaker, and "
            "(7) verify clean diff and release."
        )
        return PromptAdvisory(
            host=host,
            should_inject=True,
            message=msg,
            reason="parallel_issue_map_or_comparison",
        )

    # Work shape 3: Routine multi-step search/log/caller delegation (skipping direct <=2-file tasks & short follow-ups)
    if ROUTINE_PROMPT_NUDGE_RE.search(text):
        complexity = score_prompt_complexity(text)
        path_count = int(complexity["path_count"])
        if 1 <= path_count <= 2:
            return PromptAdvisory(
                host=host,
                should_inject=False,
                message="",
                reason="direct_small_task",
            )
        if invocation_num > 1 and len(text) < 25:
            return PromptAdvisory(
                host=host,
                should_inject=False,
                message="",
                reason="short_followup_suppressed",
            )
        if host == "codex":
            msg = (
                "[Downshift] Routine multi-step search/log/verification task detected. "
                "Prefer delegating mechanical exploration via spawn_agent (which Downshift routes to gpt-6-luna/gpt-6.1-sol) "
                "rather than looping on the frontier model."
            )
        elif host == "claude":
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
    config: DownshiftConfig | None = None,
) -> str | None:
    """Convenience wrapper returning the nudge string or None."""
    advisory = classify_user_prompt(
        last_user_input=last_user_input,
        invocation_num=invocation_num,
        host=host,
        config=config,
    )
    return advisory.message if advisory.should_inject else None


def get_advisory_state_path(
    override: str | Path | None = None,
    ledger_path: str | Path | None = None,
) -> Path:
    """Resolve the sidecar JSON state path used for multi-turn PreInvocation cooldowns."""
    if override is not None:
        return Path(override).expanduser()
    env_path = os.environ.get("DOWNSHIFT_ADVISORY_STATE_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    base = get_ledger_path(ledger_path)
    return Path(f"{base}.advisory_state.json")


def evaluate_pre_invocation_with_cooldown(
    last_user_input: str,
    *,
    conversation_id: str = "",
    invocation_num: int | None = 1,
    host: str = "antigravity",
    config: DownshiftConfig | None = None,
    state_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    now_utc: datetime | None = None,
) -> PromptAdvisory:
    """Classify a user prompt and enforce locked per-conversation multi-turn cooldown & prompt dedup."""
    cfg = config if config is not None else load_config()
    eff_inv = invocation_num if invocation_num is not None else 1
    advisory = classify_user_prompt(
        last_user_input=last_user_input,
        invocation_num=eff_inv,
        host=host,
        config=cfg,
    )
    if not advisory.should_inject:
        return advisory

    norm_conv = str(conversation_id or "").strip()
    if not norm_conv:
        return advisory

    now_ts = now_utc if now_utc is not None else datetime.now(UTC)
    if now_ts.tzinfo is None:
        now_ts = now_ts.replace(tzinfo=UTC)
    now_epoch = now_ts.timestamp()
    prompt_fp = compute_prompt_fingerprint(last_user_input)

    try:
        path = get_advisory_state_path(override=state_path, ledger_path=ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
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

            os.lseek(fd, 0, os.SEEK_SET)
            raw_chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 16_384)
                if not chunk:
                    break
                raw_chunks.append(chunk)
            raw_bytes = b"".join(raw_chunks)
            state: dict[str, dict[str, Any]] = {}
            if raw_bytes.strip():
                with contextlib.suppress(ValueError, UnicodeDecodeError):
                    parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
                    if isinstance(parsed, dict):
                        for k, v in parsed.items():
                            if isinstance(k, str) and isinstance(v, dict):
                                state[k] = v

            # Prune stale entries beyond 24h TTL
            cutoff = now_epoch - float(ADVISORY_STATE_TTL_SECONDS)
            pruned: dict[str, dict[str, Any]] = {}
            for k, v in state.items():
                try:
                    ts_val = float(v.get("ts") or 0.0)
                except (TypeError, ValueError):
                    continue
                if ts_val >= cutoff:
                    pruned[k] = v
            state = pruned

            prev = state.get(norm_conv)
            if isinstance(prev, dict):
                prev_fp = str(prev.get("prompt_fp") or "")
                try:
                    prev_ts = float(prev.get("ts") or 0.0)
                except (TypeError, ValueError):
                    prev_ts = 0.0
                prev_inv = prev.get("invocation_num")

                # Always suppress duplicate prompt re-injections during tool loops
                if prev_fp and prev_fp == prompt_fp:
                    return PromptAdvisory(
                        host=host,
                        should_inject=False,
                        message="",
                        reason="advisory_cooldown",
                    )

                elapsed = now_epoch - prev_ts
                if 0.0 <= elapsed < float(cfg.advisory_cooldown_seconds):
                    return PromptAdvisory(
                        host=host,
                        should_inject=False,
                        message="",
                        reason="advisory_cooldown",
                    )

                if (
                    invocation_num is not None
                    and isinstance(prev_inv, int)
                    and not isinstance(prev_inv, bool)
                    and (invocation_num - prev_inv) < cfg.advisory_cooldown_turns
                ):
                    return PromptAdvisory(
                        host=host,
                        should_inject=False,
                        message="",
                        reason="advisory_cooldown",
                    )

            state[norm_conv] = {
                "ts": round(now_epoch, 3),
                "invocation_num": invocation_num,
                "prompt_fp": prompt_fp,
            }
            if len(state) > ADVISORY_STATE_MAX_CONVERSATIONS:
                ordered = sorted(
                    state.items(),
                    key=lambda item: float(item[1].get("ts") or 0.0),
                    reverse=True,
                )
                state = dict(ordered[:ADVISORY_STATE_MAX_CONVERSATIONS])

            out_bytes = (json.dumps(state, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, out_bytes)
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
        return advisory
    except OSError:
        # Non-blocking fail-open if sidecar directory is unwritable
        return advisory


def classify_swarm_add(
    name: str,
    brief: str,
    workspace: str = "inherit",
    current_model: str = "",
    n: int | None = None,
    tasks: list[Any] | None = None,
    fork: str = "",
    config: DownshiftConfig | None = None,
) -> tuple[RoutingDecision, int]:
    """Classify an Antigravity swarm(action='add') call using Option A (safe-by-default swarm_models)."""
    cfg = config if config is not None else load_config()
    task_items = (
        [str(t).strip() for t in tasks if isinstance(t, str) and t.strip()]
        if isinstance(tasks, list)
        else []
    )
    if task_items:
        spawn_count = len(task_items)
    elif isinstance(n, int) and not isinstance(n, bool) and n > 0:
        spawn_count = n
    else:
        spawn_count = 1

    orig_model = (current_model or "").strip()
    lower_orig = orig_model.lower()
    configured_lower_tier: str | None = LOWER_TIER_MODEL_TIERS.get(lower_orig)
    if configured_lower_tier is None and lower_orig:
        for tier_key, configured_name in cfg.swarm_models.items():
            if configured_name.strip().lower() == lower_orig:
                configured_lower_tier = tier_key
                break
    probe_model = (
        _resolve_target_model("antigravity", configured_lower_tier)
        if configured_lower_tier is not None
        else (orig_model or "inherit")
    )

    effective_prompt = (brief or "").strip()
    if task_items:
        joined_tasks = "\n".join(task_items)
        effective_prompt = (
            f"{effective_prompt}\n{joined_tasks}" if effective_prompt else joined_tasks
        )

    base = classify_subagent_call(
        type_name="self",
        role=name,
        prompt=effective_prompt,
        current_model=probe_model,
        host="antigravity",
        config=cfg,
    )

    if base.tier == "frontier":
        if configured_lower_tier is not None and base.should_overwrite:
            # Clear lower-tier pin so swarm falls back to the coordinator's frontier model
            return (
                RoutingDecision(
                    host="antigravity",
                    tier="frontier",
                    target_model="",
                    should_overwrite=True,
                    reason=base.reason,
                ),
                spawn_count,
            )
        return (
            RoutingDecision(
                host="antigravity",
                tier="frontier",
                target_model=orig_model or "inherit",
                should_overwrite=False,
                reason=base.reason,
            ),
            spawn_count,
        )

    norm_ws = (workspace or "inherit").strip().lower() or "inherit"
    if norm_ws in ("share", "branch") or bool((fork or "").strip()):
        if configured_lower_tier is not None:
            return (
                RoutingDecision(
                    host="antigravity",
                    tier=configured_lower_tier,
                    target_model=orig_model,
                    should_overwrite=False,
                    reason="caller_explicit_model_preserved",
                ),
                spawn_count,
            )
        return (
            RoutingDecision(
                host="antigravity",
                tier="frontier",
                target_model=orig_model or "inherit",
                should_overwrite=False,
                reason="swarm_isolated_workspace_worker",
            ),
            spawn_count,
        )

    configured_target = cfg.swarm_models.get(base.tier)
    if configured_target:
        return (
            RoutingDecision(
                host="antigravity",
                tier=base.tier,
                target_model=configured_target,
                should_overwrite=(lower_orig != configured_target.lower()),
                reason=base.reason,
            ),
            spawn_count,
        )

    if configured_lower_tier is not None:
        return (
            RoutingDecision(
                host="antigravity",
                tier=configured_lower_tier,
                target_model=orig_model,
                should_overwrite=False,
                reason="caller_explicit_model_preserved",
            ),
            spawn_count,
        )

    # Option A safe default: never guess unconfigured swarm model names; record honest frontier execution
    return (
        RoutingDecision(
            host="antigravity",
            tier="frontier",
            target_model=orig_model or "inherit",
            should_overwrite=False,
            reason=f"swarm_models_unconfigured:{base.tier}",
        ),
        spawn_count,
    )


def inspect_workflow_script(
    script: str = "",
    description: str = "",
) -> tuple[RoutingDecision, int]:
    """Read-only static telemetry inspection for Antigravity run_workflow (never rewrites Python)."""
    text = script or ""
    agent_calls = len(WORKFLOW_AGENT_CALL_RE.findall(text))
    parallel_calls = len(WORKFLOW_PARALLEL_CALL_RE.findall(text))
    explicit_models = [
        m.strip().lower() for m in WORKFLOW_MODEL_ARG_RE.findall(text) if m.strip()
    ]
    spawn_count = max(1, agent_calls)

    if explicit_models and all(m in LOWER_TIER_MODEL_TIERS for m in explicit_models):
        tiers = {LOWER_TIER_MODEL_TIERS[m] for m in explicit_models}
        inferred_tier = "fast" if "fast" in tiers else "lite"
        target_model = _resolve_target_model("antigravity", inferred_tier)
    else:
        inferred_tier = "frontier"
        target_model = "inherit"

    reason = (
        "workflow_dispatch_unpinned_parallel"
        if parallel_calls > 0 and not explicit_models
        else "workflow_dispatch_observed"
    )
    return (
        RoutingDecision(
            host="antigravity",
            tier=inferred_tier,
            target_model=target_model,
            should_overwrite=False,
            reason=reason,
        ),
        spawn_count,
    )


def get_ledger_path(override: str | Path | None = None) -> Path:
    """Resolve the local JSONL telemetry ledger path."""
    if override is not None:
        return Path(override).expanduser()
    env_path = os.environ.get("DOWNSHIFT_LEDGER_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".gemini" / "downshift_ledger.jsonl"


def _get_max_ledger_bytes(env: Mapping[str, str] | None = None) -> int:
    """Return the maximum ledger size in bytes (default 5 MB; fail-open on invalid values)."""
    env_map = env if env is not None else os.environ
    raw = (env_map.get("DOWNSHIFT_MAX_LEDGER_BYTES") or "").strip()
    if not raw:
        return DEFAULT_MAX_LEDGER_BYTES
    try:
        val = int(raw)
        return val if val > 0 else DEFAULT_MAX_LEDGER_BYTES
    except ValueError:
        return DEFAULT_MAX_LEDGER_BYTES


def _append_line_locked(path: Path, line_bytes: bytes) -> None:
    """Append a line to path with a cross-process file lock and bounded size auto-rotation."""
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

        max_bytes = _get_max_ledger_bytes()
        st_size = os.fstat(fd).st_size
        if st_size > 0 and (st_size + len(line_bytes) > max_bytes):
            os.lseek(fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            existing_lines = [
                ln for ln in b"".join(chunks).splitlines(keepends=True) if ln.strip()
            ]
            kept = existing_lines[len(existing_lines) // 2 :]
            kept_size = sum(len(ln) for ln in kept)
            while kept and (kept_size + len(line_bytes) > max_bytes):
                removed = kept.pop(0)
                kept_size -= len(removed)
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, b"".join(kept) + line_bytes)
        else:
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


def normalize_role(role: str) -> str:
    """Normalize a subagent role label by stripping inline @tags and trailing retry/attempt decorations."""
    text = INLINE_TAG_STRIP_RE.sub(" ", str(role or ""))
    text = " ".join(text.split())
    while True:
        stripped = ROLE_RETRY_SUFFIX_RE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return " ".join(text.lower().split())


def compute_role_fingerprint(subagent_type: str, role: str) -> str:
    """Compute a privacy-safe 12-char SHA-256 fingerprint for (subagent_type, normalized role)."""
    norm_type = " ".join(str(subagent_type or "").strip().lower().split())
    norm_role = normalize_role(role)
    if not norm_type and not norm_role:
        return ""
    return hashlib.sha256(f"{norm_type}|{norm_role}".encode()).hexdigest()[:12]


def compute_prompt_fingerprint(prompt: str) -> str:
    """Compute a privacy-safe 12-char SHA-256 fingerprint for a normalized task prompt."""
    text = INLINE_TAG_STRIP_RE.sub(" ", str(prompt or ""))
    norm = " ".join(text.lower().split())
    if not norm:
        return ""
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


def has_explicit_retry_cue(role: str = "", prompt: str = "") -> bool:
    """Detect explicit retry phrasing or trailing retry/attempt role markers (EN/VI)."""
    role_no_tags = " ".join(INLINE_TAG_STRIP_RE.sub(" ", str(role or "")).split())
    if role_no_tags and ROLE_RETRY_SUFFIX_RE.search(role_no_tags):
        return True
    combined = f"{role or ''}\n{prompt or ''}"
    return bool(RETRY_CUE_RE.search(combined))


def classify_error_kind(error_text: str | None) -> str | None:
    """Map a raw error message to a closed, privacy-safe error category enum."""
    raw = str(error_text or "").strip()
    if not raw:
        return None
    for kind, pattern in ERROR_KIND_PATTERNS:
        if pattern.search(raw):
            return kind
    return "tool_error"


def _is_frontier_safety_decision(decision: RoutingDecision) -> bool:
    """Return True when a Frontier decision reflects an explicit safety guardrail or @high override."""
    if decision.tier != "frontier":
        return False
    return (
        decision.reason.startswith(HIGH_RISK_SAFETY_REASONS_PREFIX)
        or decision.reason in HIGH_RISK_SAFETY_REASONS_EXACT
        or decision.reason == "workspace_config:pinned_role"
        or decision.reason == "inline_override:frontier"
    )


def _parse_entry_timestamp(raw_ts: str) -> datetime | None:
    cleaned = raw_ts.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _read_tail_dicts_in_locked_fd(
    fd: int, st_size: int
) -> list[tuple[datetime, dict[str, Any]]]:
    """Read and parse recent JSONL dict rows from the last <= 64 KB of an already-locked ledger fd."""
    if st_size <= 0:
        return []
    start_offset = max(0, st_size - BURST_TAIL_READ_BYTES)
    os.lseek(fd, start_offset, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    lines = b"".join(chunks).splitlines()
    if start_offset > 0 and lines:
        lines = lines[1:]
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for raw_line in lines:
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        entry_ts = _parse_entry_timestamp(str(obj.get("timestamp") or ""))
        if entry_ts is None:
            continue
        parsed.append((entry_ts, obj))
    return parsed


def _scan_recent_session_history_in_locked_fd(
    fd: int,
    st_size: int,
    conversation_id: str,
    now_ts: datetime,
    burst_window_seconds: int,
    retry_window_seconds: int,
    role_fp: str = "",
    parallel_write_window_seconds: int = 0,
) -> tuple[
    int,
    list[tuple[datetime, dict[str, Any]]],
    list[tuple[datetime, dict[str, Any]]],
    int,
]:
    """Single-pass locked tail scan returning (recent_burst_count, matching_routes, matching_outcomes, recent_inherit_writes)."""
    if st_size <= 0 or not conversation_id:
        return 0, [], [], 0
    tail_rows = _read_tail_dicts_in_locked_fd(fd, st_size)
    burst_count = 0
    recent_inherit_writes = 0
    matching_routes: list[tuple[datetime, dict[str, Any]]] = []
    matching_outcomes: list[tuple[datetime, dict[str, Any]]] = []
    matched_call_ids: set[str] = set()

    for entry_ts, obj in tail_rows:
        if str(obj.get("conversation_id") or "").strip() != conversation_id:
            continue
        delta = (now_ts - entry_ts).total_seconds()
        if delta < 0.0:
            continue
        ev_type = str(obj.get("event_type") or "route").strip().lower()
        if ev_type != "outcome":
            if burst_window_seconds > 0 and delta <= float(burst_window_seconds):
                raw_spawn = obj.get("spawn_count", 1)
                try:
                    spawn_weight = max(1, int(raw_spawn or 1))
                except (ValueError, TypeError):
                    spawn_weight = 1
                burst_count += spawn_weight
            if (
                parallel_write_window_seconds > 0
                and delta <= float(parallel_write_window_seconds)
                and str(obj.get("mode") or "active").strip().lower() != "off"
                and obj.get("is_write_capable") is True
                and str(obj.get("workspace") or "inherit").strip().lower() == "inherit"
            ):
                recent_inherit_writes += 1
            if (
                role_fp
                and retry_window_seconds > 0
                and delta <= float(retry_window_seconds)
                and str(obj.get("mode") or "active").strip().lower() != "off"
                and str(obj.get("role_fp") or "").strip() == role_fp
            ):
                matching_routes.append((entry_ts, obj))
                cid = str(obj.get("tool_call_id") or "").strip()
                if cid:
                    matched_call_ids.add(cid)
        elif (
            role_fp
            and retry_window_seconds > 0
            and delta <= float(retry_window_seconds)
        ):
            out_role_fp = str(obj.get("role_fp") or "").strip()
            out_call_id = str(obj.get("tool_call_id") or "").strip()
            if out_role_fp == role_fp or (
                out_call_id and out_call_id in matched_call_ids
            ):
                matching_outcomes.append((entry_ts, obj))

    return burst_count, matching_routes, matching_outcomes, recent_inherit_writes


def _count_recent_burst_in_locked_fd(
    fd: int,
    st_size: int,
    conversation_id: str,
    now_ts: datetime,
    window_seconds: int,
) -> int:
    """Count recent routing events (excluding outcomes) for conversation_id within window_seconds."""
    burst_count, _, _, _ = _scan_recent_session_history_in_locked_fd(
        fd=fd,
        st_size=st_size,
        conversation_id=conversation_id,
        now_ts=now_ts,
        burst_window_seconds=window_seconds,
        retry_window_seconds=0,
    )
    return burst_count


def _build_promoted_decision(
    decision: RoutingDecision,
    *,
    target_tier: str,
    reason: str,
    original_model: str,
    norm_tool: str,
    cfg: DownshiftConfig,
) -> RoutingDecision:
    """Build a host-aware promoted RoutingDecision honoring Option A /swarm safety."""
    raw_orig = str(original_model or "").strip()
    orig_lower = raw_orig.lower()
    if norm_tool == "swarm":
        if target_tier == "fast":
            swarm_fast = cfg.swarm_models.get("fast")
            if swarm_fast:
                return RoutingDecision(
                    host=decision.host,
                    tier="fast",
                    target_model=swarm_fast,
                    should_overwrite=(orig_lower != swarm_fast.lower()),
                    reason=reason,
                )
            # Option A safe default: never invent an unconfigured swarm fast alias; promote cleanly to Frontier.
            target_tier = "frontier"
        return RoutingDecision(
            host=decision.host,
            tier="frontier",
            target_model="",
            should_overwrite=bool(raw_orig and orig_lower != "inherit"),
            reason=reason,
        )

    target_model = _resolve_target_model(decision.host, target_tier)
    if target_tier == "frontier":
        should_overwrite = bool(raw_orig and orig_lower != target_model.lower())
    else:
        should_overwrite = orig_lower != target_model.lower()
    return RoutingDecision(
        host=decision.host,
        tier=target_tier,
        target_model=target_model,
        should_overwrite=should_overwrite,
        reason=reason,
    )


def _evaluate_retry_guard(
    decision: RoutingDecision,
    *,
    cfg: DownshiftConfig,
    matching_routes: list[tuple[datetime, dict[str, Any]]],
    matching_outcomes: list[tuple[datetime, dict[str, Any]]],
    prompt_fp: str,
    has_retry_cue: bool,
    original_model: str,
    norm_tool: str,
) -> tuple[RoutingDecision, str]:
    """Evaluate closed-loop auto-upshift and deterministic 2-retry circuit breaker (Q1 -> A, Q2 -> A)."""
    if not cfg.auto_upshift or norm_tool == "run_workflow":
        return decision, ""
    if _is_frontier_safety_decision(decision):
        return decision, ""
    if decision.tier == "frontier" and not decision.reason.startswith(
        "swarm_models_unconfigured:"
    ):
        return decision, ""
    if not matching_routes and not matching_outcomes:
        return decision, ""

    failed_outcomes = [
        (ts, obj) for ts, obj in matching_outcomes if obj.get("failed") is True
    ]
    ok_outcomes = [
        (ts, obj) for ts, obj in matching_outcomes if obj.get("failed") is False
    ]
    latest_failed_ts = max((ts for ts, _ in failed_outcomes), default=None)
    latest_ok_ts = max((ts for ts, _ in ok_outcomes), default=None)
    has_unresolved_error = latest_failed_ts is not None and (
        latest_ok_ts is None or latest_failed_ts >= latest_ok_ts
    )
    has_same_prompt = bool(prompt_fp) and any(
        str(obj.get("prompt_fp") or "").strip() == prompt_fp
        for _, obj in matching_routes
    )
    has_retry_cue_with_prior = bool(has_retry_cue and matching_routes)

    if not (has_unresolved_error or has_same_prompt or has_retry_cue_with_prior):
        return decision, ""

    failed_call_ids = {
        str(obj.get("tool_call_id") or "").strip()
        for _, obj in failed_outcomes
        if str(obj.get("tool_call_id") or "").strip()
    }
    upshift_reasons = {"auto_upshift_on_retry", "circuit_breaker_frontier_lock"}

    if has_same_prompt:
        relevant_routes = [
            obj
            for _, obj in matching_routes
            if str(obj.get("prompt_fp") or "").strip() == prompt_fp
            or str(obj.get("reason") or "") in upshift_reasons
            or (
                str(obj.get("tool_call_id") or "").strip()
                and str(obj.get("tool_call_id") or "").strip() in failed_call_ids
            )
        ]
    else:
        relevant_routes = [
            obj
            for _, obj in matching_routes
            if str(obj.get("reason") or "") in upshift_reasons
            or (
                str(obj.get("tool_call_id") or "").strip()
                and str(obj.get("tool_call_id") or "").strip() in failed_call_ids
            )
        ]
        if matching_routes and matching_routes[-1][1] not in relevant_routes:
            relevant_routes.append(matching_routes[-1][1])

    chain_attempts = max(len(relevant_routes), len(failed_outcomes))
    if chain_attempts <= 0:
        return decision, ""

    last_tier = ""
    if relevant_routes:
        last_tier = str(relevant_routes[-1].get("tier") or "").strip().lower()
    elif failed_outcomes:
        last_tier = str(failed_outcomes[-1][1].get("routed_tier") or "").strip().lower()
    if last_tier not in VALID_TIERS:
        last_tier = decision.tier if decision.tier in VALID_TIERS else "lite"

    incoming_effective_tier = (
        decision.reason.split(":", 1)[1]
        if decision.reason.startswith("swarm_models_unconfigured:")
        and decision.reason.split(":", 1)[1] in VALID_TIERS
        else decision.tier
    )
    upshifted_from = (
        last_tier
        if last_tier in ("lite", "fast")
        else (
            incoming_effective_tier
            if incoming_effective_tier in ("lite", "fast")
            else "fast"
        )
    )

    if chain_attempts >= cfg.max_auto_retries:
        promoted = _build_promoted_decision(
            decision,
            target_tier="frontier",
            reason="circuit_breaker_frontier_lock",
            original_model=original_model,
            norm_tool=norm_tool,
            cfg=cfg,
        )
        return promoted, upshifted_from

    next_tier = "fast" if last_tier == "lite" else "frontier"
    if TIER_RANK.get(incoming_effective_tier, 0) > TIER_RANK.get(next_tier, 0):
        next_tier = incoming_effective_tier
    promoted = _build_promoted_decision(
        decision,
        target_tier=next_tier,
        reason="auto_upshift_on_retry",
        original_model=original_model,
        norm_tool=norm_tool,
        cfg=cfg,
    )
    return promoted, upshifted_from


def _write_line_with_rotation_in_locked_fd(
    fd: int, st_size: int, line_bytes: bytes
) -> None:
    """Append line_bytes to an already-locked ledger fd with bounded size half-trim rotation."""
    max_bytes = _get_max_ledger_bytes()
    if st_size > 0 and (st_size + len(line_bytes) > max_bytes):
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        existing_lines = [
            ln for ln in b"".join(chunks).splitlines(keepends=True) if ln.strip()
        ]
        kept = existing_lines[len(existing_lines) // 2 :]
        kept_size = sum(len(ln) for ln in kept)
        while kept and (kept_size + len(line_bytes) > max_bytes):
            removed = kept.pop(0)
            kept_size -= len(removed)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, b"".join(kept) + line_bytes)
    else:
        os.lseek(fd, 0, os.SEEK_END)
        os.write(fd, line_bytes)


def _record_event_locked(
    decision: RoutingDecision,
    *,
    conversation_id: str = "",
    subagent_type: str = "",
    role: str = "",
    original_model: str = "inherit",
    ledger_path: str | Path | None = None,
    mode: str = "active",
    prompt_chars: int = 0,
    config: DownshiftConfig | None = None,
    now_utc: datetime | None = None,
    tool: str = "",
    spawn_count: int = 1,
    tool_call_id: str = "",
    role_fp: str = "",
    prompt_fp: str = "",
    has_retry_cue: bool = False,
    workspace: str | None = None,
    write_capable: bool | None = None,
) -> tuple[RoutingDecision, bool]:
    """Atomically evaluate retry auto-upshift, 2-retry circuit breaker, burst cap, and parallel-write isolation inside the OS file lock."""
    cfg = config if config is not None else load_config()
    now_ts = now_utc if now_utc is not None else datetime.now(UTC)
    if now_ts.tzinfo is None:
        now_ts = now_ts.replace(tzinfo=UTC)
    try:
        norm_spawn = max(1, int(spawn_count or 1))
    except (ValueError, TypeError):
        norm_spawn = 1
    norm_tool = str(tool or "").strip()
    norm_call_id = str(tool_call_id or "").strip()[:64]
    norm_role_fp = str(role_fp or "").strip() or compute_role_fingerprint(
        subagent_type, role
    )
    norm_prompt_fp = str(prompt_fp or "").strip()
    norm_mode = str(mode or "active").strip().lower() or "active"
    track_workspace = workspace is not None or write_capable is not None
    norm_ws: str | None = (
        normalize_workspace(workspace) if workspace is not None else None
    )
    is_writer = bool(write_capable)
    is_inherit_ws = norm_ws is None or norm_ws == "inherit"
    try:
        path = get_ledger_path(ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
        locked = False
        effective_decision = decision
        upshifted_from = ""
        workspace_isolated = False
        recorded_ws = norm_ws
        ws_reason = ""
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

            st_size = os.fstat(fd).st_size
            norm_conv = str(conversation_id or "").strip()
            need_burst = (
                cfg.max_burst_subagents > 0
                and bool(norm_conv)
                and decision.tier == "fast"
                and decision.reason in BURST_ELIGIBLE_REASONS
            )
            need_retry = (
                cfg.auto_upshift
                and bool(norm_conv)
                and bool(norm_role_fp)
                and norm_tool != "run_workflow"
                and not _is_frontier_safety_decision(decision)
            )
            isolate_policy = cfg.isolate_parallel_writes
            need_parallel_write = (
                isolate_policy != "off"
                and is_writer
                and is_inherit_ws
                and norm_tool != "run_workflow"
                and (bool(norm_conv) or norm_spawn > 1)
            )
            recent_inherit_writes = 0
            if need_burst or need_retry or (need_parallel_write and bool(norm_conv)):
                (
                    recent_count,
                    matching_routes,
                    matching_outcomes,
                    recent_inherit_writes,
                ) = _scan_recent_session_history_in_locked_fd(
                    fd=fd,
                    st_size=st_size,
                    conversation_id=norm_conv,
                    now_ts=now_ts,
                    burst_window_seconds=cfg.burst_window_seconds if need_burst else 0,
                    retry_window_seconds=cfg.retry_window_seconds if need_retry else 0,
                    role_fp=norm_role_fp if need_retry else "",
                    parallel_write_window_seconds=cfg.burst_window_seconds
                    if (need_parallel_write and bool(norm_conv))
                    else 0,
                )
                if need_retry:
                    effective_decision, upshifted_from = _evaluate_retry_guard(
                        decision,
                        cfg=cfg,
                        matching_routes=matching_routes,
                        matching_outcomes=matching_outcomes,
                        prompt_fp=norm_prompt_fp,
                        has_retry_cue=has_retry_cue,
                        original_model=original_model,
                        norm_tool=norm_tool,
                    )
                # FR-009: auto-upshift / circuit breaker strictly beats burst-cap downgrades.
                if (
                    not upshifted_from
                    and need_burst
                    and effective_decision.tier == "fast"
                    and effective_decision.reason in BURST_ELIGIBLE_REASONS
                    and recent_count + norm_spawn > cfg.max_burst_subagents
                ):
                    if norm_tool == "swarm":
                        swarm_lite = cfg.swarm_models.get("lite")
                        if swarm_lite:
                            effective_decision = RoutingDecision(
                                host=decision.host,
                                tier="lite",
                                target_model=swarm_lite,
                                should_overwrite=(
                                    str(original_model or "").strip().lower()
                                    != swarm_lite.lower()
                                ),
                                reason="burst_cap_downgraded_to_lite",
                            )
                    else:
                        effective_decision = RoutingDecision(
                            host=decision.host,
                            tier="lite",
                            target_model=_resolve_target_model(decision.host, "lite"),
                            should_overwrite=True,
                            reason="burst_cap_downgraded_to_lite",
                        )

            if need_parallel_write and (recent_inherit_writes >= 1 or norm_spawn > 1):
                supports_ws_rewrite = (
                    effective_decision.host == "antigravity"
                    and norm_tool in ("invoke_subagent", "swarm")
                )
                if (
                    supports_ws_rewrite
                    and isolate_policy in ("share", "branch")
                    and norm_mode == "active"
                ):
                    target_ws = isolate_policy
                    ws_reason = f"parallel_write_isolated_to_{target_ws}"
                    workspace_isolated = True
                    recorded_ws = target_ws
                else:
                    target_ws = ""
                    ws_reason = "parallel_write_race_warning"
                    workspace_isolated = False
                    recorded_ws = norm_ws
                effective_decision = RoutingDecision(
                    host=effective_decision.host,
                    tier=effective_decision.tier,
                    target_model=effective_decision.target_model,
                    should_overwrite=effective_decision.should_overwrite,
                    reason=effective_decision.reason,
                    target_workspace=target_ws,
                    workspace_guardrail_reason=ws_reason,
                )

            entry: dict[str, Any] = {
                "timestamp": now_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event_type": "route",
                "conversation_id": str(conversation_id or ""),
                "host": effective_decision.host,
                "mode": norm_mode,
                "subagent_type": str(subagent_type or ""),
                "role": str(role or "")[:80],
                "prompt_chars": max(0, int(prompt_chars or 0)),
                "original_model": str(original_model or "inherit"),
                "target_model": effective_decision.target_model,
                "tier": effective_decision.tier,
                "reason": effective_decision.reason,
                "overwritten": effective_decision.should_overwrite,
            }
            if norm_tool:
                entry["tool"] = norm_tool
            if norm_spawn > 1:
                entry["spawn_count"] = norm_spawn
            if norm_call_id:
                entry["tool_call_id"] = norm_call_id
            if norm_role_fp:
                entry["role_fp"] = norm_role_fp
            if norm_prompt_fp:
                entry["prompt_fp"] = norm_prompt_fp
            if upshifted_from:
                entry["upshifted_from"] = upshifted_from
            if track_workspace:
                entry["workspace"] = recorded_ws
                entry["is_write_capable"] = is_writer
                entry["workspace_isolated"] = workspace_isolated
                if ws_reason:
                    entry["workspace_guardrail_reason"] = ws_reason
            line_bytes = (json.dumps(entry, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            _write_line_with_rotation_in_locked_fd(fd, st_size, line_bytes)
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
        return effective_decision, True
    except OSError:
        # Non-blocking fail-open if disk or directory is unwritable
        return decision, False


def record_and_evaluate_session_guards(
    decision: RoutingDecision,
    conversation_id: str = "",
    subagent_type: str = "",
    role: str = "",
    original_model: str = "inherit",
    ledger_path: str | Path | None = None,
    mode: str = "active",
    prompt_chars: int = 0,
    config: DownshiftConfig | None = None,
    now_utc: datetime | None = None,
    tool: str = "",
    spawn_count: int = 1,
    tool_call_id: str = "",
    role_fp: str = "",
    prompt_fp: str = "",
    has_retry_cue: bool = False,
    workspace: str | None = None,
    write_capable: bool | None = None,
) -> RoutingDecision:
    """Atomically evaluate closed-loop auto-upshift, 2-retry circuit breaker, burst cap, and parallel-write isolation."""
    effective_decision, _ = _record_event_locked(
        decision=decision,
        conversation_id=conversation_id,
        subagent_type=subagent_type,
        role=role,
        original_model=original_model,
        ledger_path=ledger_path,
        mode=mode,
        prompt_chars=prompt_chars,
        config=config,
        now_utc=now_utc,
        tool=tool,
        spawn_count=spawn_count,
        tool_call_id=tool_call_id,
        role_fp=role_fp,
        prompt_fp=prompt_fp,
        has_retry_cue=has_retry_cue,
        workspace=workspace,
        write_capable=write_capable,
    )
    return effective_decision


def record_and_apply_burst_cap(
    decision: RoutingDecision,
    conversation_id: str = "",
    subagent_type: str = "",
    role: str = "",
    original_model: str = "inherit",
    ledger_path: str | Path | None = None,
    mode: str = "active",
    prompt_chars: int = 0,
    config: DownshiftConfig | None = None,
    now_utc: datetime | None = None,
    tool: str = "",
    spawn_count: int = 1,
    tool_call_id: str = "",
    role_fp: str = "",
    prompt_fp: str = "",
    has_retry_cue: bool = False,
    workspace: str | None = None,
    write_capable: bool | None = None,
) -> RoutingDecision:
    """Backward-compatible alias for record_and_evaluate_session_guards()."""
    return record_and_evaluate_session_guards(
        decision=decision,
        conversation_id=conversation_id,
        subagent_type=subagent_type,
        role=role,
        original_model=original_model,
        ledger_path=ledger_path,
        mode=mode,
        prompt_chars=prompt_chars,
        config=config,
        now_utc=now_utc,
        tool=tool,
        spawn_count=spawn_count,
        tool_call_id=tool_call_id,
        role_fp=role_fp,
        prompt_fp=prompt_fp,
        has_retry_cue=has_retry_cue,
        workspace=workspace,
        write_capable=write_capable,
    )


def record_routing_event(
    decision: RoutingDecision,
    conversation_id: str = "",
    subagent_type: str = "",
    role: str = "",
    original_model: str = "inherit",
    ledger_path: str | Path | None = None,
    mode: str = "active",
    prompt_chars: int = 0,
    config: DownshiftConfig | None = None,
    now_utc: datetime | None = None,
    tool: str = "",
    spawn_count: int = 1,
    tool_call_id: str = "",
    role_fp: str = "",
    prompt_fp: str = "",
    has_retry_cue: bool = False,
) -> bool:
    """Append a privacy-safe routing event (never storing raw Prompt text) to the JSONL ledger."""
    _, ok = _record_event_locked(
        decision=decision,
        conversation_id=conversation_id,
        subagent_type=subagent_type,
        role=role,
        original_model=original_model,
        ledger_path=ledger_path,
        mode=mode,
        prompt_chars=prompt_chars,
        config=config,
        now_utc=now_utc,
        tool=tool,
        spawn_count=spawn_count,
        tool_call_id=tool_call_id,
        role_fp=role_fp,
        prompt_fp=prompt_fp,
        has_retry_cue=has_retry_cue,
    )
    return ok


def record_outcome_event(
    *,
    host: str,
    conversation_id: str = "",
    tool: str = "",
    tool_call_id: str = "",
    role_fp: str = "",
    failed: bool = False,
    error_kind: str | None = None,
    ledger_path: str | Path | None = None,
    now_utc: datetime | None = None,
) -> bool:
    """Append a privacy-safe PostToolUse outcome event under the OS file lock, stamping correlated routed_tier."""
    now_ts = now_utc if now_utc is not None else datetime.now(UTC)
    if now_ts.tzinfo is None:
        now_ts = now_ts.replace(tzinfo=UTC)
    norm_host = str(host or "antigravity").strip().lower() or "antigravity"
    norm_conv = str(conversation_id or "").strip()
    norm_tool = str(tool or "").strip()
    norm_call_id = str(tool_call_id or "").strip()[:64]
    norm_role_fp = str(role_fp or "").strip()
    is_failed = bool(failed)
    norm_error_kind = str(error_kind).strip() if (is_failed and error_kind) else None
    try:
        path = get_ledger_path(ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
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

            st_size = os.fstat(fd).st_size
            routed_tier: str | None = None
            if st_size > 0 and (norm_call_id or (norm_conv and norm_role_fp)):
                tail_rows = _read_tail_dicts_in_locked_fd(fd, st_size)
                # Prefer exact tool_call_id correlation; fall back to latest matching (conversation_id, role_fp).
                fallback_tier: str | None = None
                for _, obj in reversed(tail_rows):
                    if (
                        str(obj.get("event_type") or "route").strip().lower()
                        == "outcome"
                    ):
                        continue
                    tier_val = str(obj.get("tier") or "").strip().lower()
                    if tier_val not in VALID_TIERS:
                        continue
                    if (
                        norm_call_id
                        and str(obj.get("tool_call_id") or "").strip() == norm_call_id
                    ):
                        routed_tier = tier_val
                        break
                    if (
                        fallback_tier is None
                        and norm_conv
                        and norm_role_fp
                        and str(obj.get("conversation_id") or "").strip() == norm_conv
                        and str(obj.get("role_fp") or "").strip() == norm_role_fp
                    ):
                        fallback_tier = tier_val
                if routed_tier is None:
                    routed_tier = fallback_tier

            entry: dict[str, Any] = {
                "timestamp": now_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event_type": "outcome",
                "host": norm_host,
                "conversation_id": norm_conv,
                "failed": is_failed,
                "error_kind": norm_error_kind,
            }
            if norm_tool:
                entry["tool"] = norm_tool
            if norm_call_id:
                entry["tool_call_id"] = norm_call_id
            if norm_role_fp:
                entry["role_fp"] = norm_role_fp
            if routed_tier in VALID_TIERS:
                entry["routed_tier"] = routed_tier

            line_bytes = (json.dumps(entry, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            _write_line_with_rotation_in_locked_fd(fd, st_size, line_bytes)
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
        return True
    except OSError:
        return False


def clean_ledger_file(ledger_path: str | Path | None = None) -> tuple[int, int]:
    """Remove corrupted JSONL lines and synthetic empty rows (prompt_chars <= 0 and empty role) under an OS file lock."""
    path = get_ledger_path(ledger_path)
    if not path.exists():
        return (0, 0)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    locked = False
    removed_count = 0
    kept_lines: list[bytes] = []
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

        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)

        for raw_line in b"".join(chunks).splitlines(keepends=True):
            if not raw_line.strip():
                continue
            try:
                item = json.loads(raw_line.decode("utf-8", errors="replace"))
            except ValueError:
                removed_count += 1
                continue
            if not isinstance(item, dict):
                removed_count += 1
                continue
            if str(item.get("event_type") or "").strip().lower() == "outcome":
                host_str = str(item.get("host") or "").strip()
                if not host_str or not isinstance(item.get("failed"), bool):
                    removed_count += 1
                    continue
                kept_lines.append(
                    raw_line if raw_line.endswith(b"\n") else (raw_line + b"\n")
                )
                continue
            try:
                p_chars = int(item.get("prompt_chars") or 0)
            except (ValueError, TypeError):
                p_chars = 0
            role_str = str(item.get("role") or "").strip()
            if p_chars <= 0 and not role_str:
                removed_count += 1
                continue
            kept_lines.append(
                raw_line if raw_line.endswith(b"\n") else (raw_line + b"\n")
            )

        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        if kept_lines:
            os.write(fd, b"".join(kept_lines))
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
    return (removed_count, len(kept_lines))


def reset_ledger_file(ledger_path: str | Path | None = None) -> None:
    """Truncate the JSONL ledger file to 0 bytes under an exclusive OS file lock."""
    path = get_ledger_path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
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

        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
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
