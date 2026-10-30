"""Unit tests for Downshift core classification & safety guardrails (US-1.1 / Issue #4)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from downshift_core import (
    classify_subagent_call,
    classify_user_prompt,
    detect_host,
    should_nudge_pre_invocation,
)


def test_detect_host_identifies_antigravity_claude_and_codex() -> None:
    assert (
        detect_host({"toolCall": {"name": "invoke_subagent", "args": {}}})
        == "antigravity"
    )
    assert detect_host({"lastUserInput": "hello", "invocationNum": 1}) == "antigravity"
    assert detect_host({"tool_name": "Task", "tool_input": {}}) == "claude"
    assert (
        detect_host(
            {"hook_event_name": "UserPromptSubmit", "prompt": "hi"},
            env={"CODEX_CI": "1"},
        )
        == "codex"
    )


def test_antigravity_research_simple_lookup_routes_to_flash_lite() -> None:
    decision = classify_subagent_call(
        type_name="research",
        role="File Locator",
        prompt="Find file where resolve_contact is defined and list files in backend/app/services",
        current_model="inherit",
        host="antigravity",
    )
    assert decision.tier == "lite"
    assert decision.target_model == "flash_lite"
    assert decision.should_overwrite is True
    assert decision.reason == "read_only_simple_lookup"


def test_antigravity_research_codebase_survey_routes_to_flash() -> None:
    decision = classify_subagent_call(
        type_name="research",
        role="Codebase Researcher",
        prompt="Survey all webhook handlers across backend/app/api and summarize how inbound messages are processed",
        current_model="inherit",
        host="antigravity",
    )
    assert decision.tier == "fast"
    assert decision.target_model == "flash"
    assert decision.should_overwrite is True
    assert decision.reason == "read_only_research_survey"


def test_claude_code_explore_task_routes_to_haiku_or_sonnet() -> None:
    lite_decision = classify_subagent_call(
        type_name="Explore",
        role="Locate symbol",
        prompt="Where is TenantMemberORM located in the repository?",
        current_model="",
        host="claude",
    )
    assert lite_decision.tier == "lite"
    assert lite_decision.target_model == "haiku"
    assert lite_decision.should_overwrite is True

    fast_decision = classify_subagent_call(
        type_name="Explore",
        role="Survey callers",
        prompt="Trace all callers of NotificationService across the backend and summarize the flow",
        current_model="",
        host="claude",
    )
    assert fast_decision.tier == "fast"
    assert fast_decision.target_model == "sonnet"
    assert fast_decision.should_overwrite is True


def test_safety_guardrail_blocks_downgrade_on_high_risk_keywords() -> None:
    high_risk_prompts = [
        ("research", "Check alembic migration head conflicts", "antigravity"),
        (
            "research",
            "Investigate PostgreSQL deadlock and advisory lock ordering",
            "antigravity",
        ),
        (
            "Explore",
            "Audit auth permission and IDOR boundary in tenant endpoints",
            "claude",
        ),
        ("self", "Run pytest on savepoint rollback recovery", "antigravity"),
        ("general-purpose", "Analyze race condition in for update lock", "claude"),
    ]
    for subagent_type, prompt, host in high_risk_prompts:
        decision = classify_subagent_call(
            type_name=subagent_type,
            role="Investigator",
            prompt=prompt,
            current_model="inherit",
            host=host,
        )
        assert decision.tier == "frontier", f"Expected frontier for: {prompt}"
        assert decision.target_model == "inherit"
        assert decision.should_overwrite is False
        assert decision.reason.startswith("safety_guardrail:")


def test_self_subagent_code_mutation_stays_on_inherit() -> None:
    decision = classify_subagent_call(
        type_name="self",
        role="Backend Engineer",
        prompt="Implement the new webhook retry handler and refactor message_service.py",
        current_model="inherit",
        host="antigravity",
    )
    assert decision.tier == "frontier"
    assert decision.target_model == "inherit"
    assert decision.should_overwrite is False
    assert decision.reason == "frontier_default_stateful_work"


def test_self_subagent_pure_test_or_lint_runner_routes_to_fast() -> None:
    agy_decision = classify_subagent_call(
        type_name="self",
        role="Test Runner",
        prompt="Run pytest backend/tests/unit -v and ruff check backend/ and report test results",
        current_model="inherit",
        host="antigravity",
    )
    assert agy_decision.tier == "fast"
    assert agy_decision.target_model == "flash"
    assert agy_decision.should_overwrite is True
    assert agy_decision.reason == "verification_test_or_lint_runner"

    claude_decision = classify_subagent_call(
        type_name="general-purpose",
        role="Run vitest suite",
        prompt="Execute npm run test and npm run lint and summarize any failing tests",
        current_model="",
        host="claude",
    )
    assert claude_decision.tier == "fast"
    assert claude_decision.target_model == "sonnet"
    assert claude_decision.should_overwrite is True


def test_inline_override_tags_take_precedence() -> None:
    force_frontier = classify_subagent_call(
        type_name="research",
        role="Deep Researcher",
        prompt="Find all files in backend/ @pro",
        current_model="inherit",
        host="antigravity",
    )
    assert force_frontier.tier == "frontier"
    assert force_frontier.should_overwrite is False
    assert force_frontier.reason == "inline_override:frontier"

    force_opus = classify_subagent_call(
        type_name="Explore",
        role="Deep Researcher",
        prompt="Locate config file @opus",
        current_model="",
        host="claude",
    )
    assert force_opus.tier == "frontier"
    assert force_opus.should_overwrite is False

    force_lite_agy = classify_subagent_call(
        type_name="self",
        role="Quick helper",
        prompt="Format this snippet @lite",
        current_model="inherit",
        host="antigravity",
    )
    assert force_lite_agy.tier == "lite"
    assert force_lite_agy.target_model == "flash_lite"
    assert force_lite_agy.should_overwrite is True

    force_haiku_claude = classify_subagent_call(
        type_name="general-purpose",
        role="Quick helper",
        prompt="Summarize README @haiku",
        current_model="",
        host="claude",
    )
    assert force_haiku_claude.tier == "lite"
    assert force_haiku_claude.target_model == "haiku"
    assert force_haiku_claude.should_overwrite is True

    force_fast = classify_subagent_call(
        type_name="self",
        role="Fast helper",
        prompt="Check imports @downshift",
        current_model="inherit",
        host="antigravity",
    )
    assert force_fast.tier == "fast"
    assert force_fast.target_model == "flash"
    assert force_fast.should_overwrite is True


def test_explicit_non_inherit_model_is_preserved() -> None:
    decision = classify_subagent_call(
        type_name="research",
        role="Codebase Researcher",
        prompt="Find all callers of resolve_contact",
        current_model="pro",
        host="antigravity",
    )
    assert decision.should_overwrite is False
    assert decision.target_model == "pro"
    assert decision.reason == "caller_explicit_model_preserved"


def test_pre_invocation_nudges_only_on_first_turn_routine_task() -> None:
    msg = should_nudge_pre_invocation(
        last_user_input="Search the logs and find all occurrences of timeout errors across the codebase",
        invocation_num=1,
    )
    assert msg is not None
    assert "[Downshift]" in msg

    # Subsequent turns in the same prompt must never re-inject
    assert (
        should_nudge_pre_invocation(
            last_user_input="Search the logs and find all occurrences of timeout errors",
            invocation_num=2,
        )
        is None
    )

    # High-risk or @pro prompts must not nudge
    assert (
        should_nudge_pre_invocation(
            last_user_input="Search the logs for deadlock and alembic migration errors",
            invocation_num=1,
        )
        is None
    )
    assert (
        should_nudge_pre_invocation(
            last_user_input="Search the logs for timeout errors @pro",
            invocation_num=1,
        )
        is None
    )

    advisory = classify_user_prompt(
        "Find all files referencing GlobalSearchDialog and run tests",
        invocation_num=1,
        host="claude",
    )
    assert advisory.should_inject is True
    assert "Haiku/Sonnet" in advisory.message
