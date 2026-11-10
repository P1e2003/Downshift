"""Unit tests for Downshift core classification & safety guardrails (US-1.1 / Issue #4)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from downshift_core import (
    DownshiftConfig,
    classify_subagent_call,
    classify_user_prompt,
    detect_host,
    extract_workspace_dir,
    load_config,
    score_prompt_complexity,
    should_nudge_pre_invocation,
    strip_benign_code_identifiers,
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


def test_load_config_reads_valid_downshift_json_and_env_override(
    tmp_path: Path,
) -> None:
    import json

    workspace_root = tmp_path / "repo"
    nested_dir = workspace_root / "backend" / "app"
    nested_dir.mkdir(parents=True)
    cfg_file = workspace_root / ".downshift.json"
    cfg_file.write_text(
        json.dumps(
            {
                "mode": "shadow",
                "hints": False,
                "high_risk_keywords": ["vietmoney", "zalo_oa", "clerk"],
                "pinned_roles": {
                    "Security Auditor": "frontier",
                    "Log Locator": "lite",
                },
            }
        ),
        encoding="utf-8",
    )

    # 1. Upward walk from nested_dir finds workspace_root/.downshift.json (FR-008)
    cfg = load_config(workspace_dir=nested_dir, env={})
    assert cfg.mode == "shadow"
    assert cfg.hints is False
    assert cfg.high_risk_keywords == ("vietmoney", "zalo_oa", "clerk")
    assert cfg.pinned_roles == {
        "Security Auditor": "frontier",
        "Log Locator": "lite",
    }

    # 2. DOWNSHIFT_MODE env var overrides .downshift.json mode (FR-005)
    cfg_active = load_config(
        workspace_dir=nested_dir,
        env={"DOWNSHIFT_MODE": "active", "DOWNSHIFT_HINTS": "1"},
    )
    assert cfg_active.mode == "active"
    assert cfg_active.hints is True

    # 3. DOWNSHIFT_ENABLED=0 forces mode="off" (FR-005)
    cfg_off = load_config(
        workspace_dir=nested_dir,
        env={"DOWNSHIFT_ENABLED": "0", "DOWNSHIFT_MODE": "active"},
    )
    assert cfg_off.mode == "off"

    # 4. DOWNSHIFT_CONFIG_PATH explicit file override
    explicit_file = tmp_path / "custom_config.json"
    explicit_file.write_text(
        json.dumps({"mode": "active", "high_risk_keywords": ["weaviate"]}),
        encoding="utf-8",
    )
    cfg_explicit = load_config(
        workspace_dir=nested_dir,
        env={"DOWNSHIFT_CONFIG_PATH": str(explicit_file)},
    )
    assert cfg_explicit.mode == "active"
    assert cfg_explicit.high_risk_keywords == ("weaviate",)


def test_load_config_fails_open_on_corrupted_json(tmp_path: Path) -> None:
    corrupt_dir = tmp_path / "corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / ".downshift.json").write_text("{not-valid-json", encoding="utf-8")

    cfg = load_config(workspace_dir=corrupt_dir, env={})
    assert cfg == DownshiftConfig()

    mistyped_dir = tmp_path / "mistyped"
    mistyped_dir.mkdir()
    (mistyped_dir / ".downshift.json").write_text(
        '{"mode": "invalid_mode", "hints": "yes", "high_risk_keywords": 123, "pinned_roles": {"A": "unknown_tier", "B": "fast"}}',
        encoding="utf-8",
    )
    cfg_mistyped = load_config(workspace_dir=mistyped_dir, env={})
    assert cfg_mistyped.mode == "active"
    assert cfg_mistyped.hints is True
    assert cfg_mistyped.high_risk_keywords == ()
    assert cfg_mistyped.pinned_roles == {"B": "fast"}


def test_custom_high_risk_keywords_and_pinned_roles() -> None:
    cfg = DownshiftConfig(
        mode="active",
        hints=True,
        high_risk_keywords=("vietmoney", "zalo_oa", "clerk"),
        pinned_roles={
            "Security Auditor": "frontier",
            "Log Locator": "lite",
            "Codebase Researcher": "fast",
        },
    )

    # 1. Pinned role -> lite when prompt has no high-risk keywords
    pinned_lite = classify_subagent_call(
        type_name="self",
        role="Log Locator Helper",
        prompt="Summarize recent status entries in output.txt",
        current_model="inherit",
        host="antigravity",
        config=cfg,
    )
    assert pinned_lite.tier == "lite"
    assert pinned_lite.target_model == "flash_lite"
    assert pinned_lite.should_overwrite is True
    assert pinned_lite.reason == "workspace_config:pinned_role"

    # 2. Pinned role -> frontier
    pinned_frontier = classify_subagent_call(
        type_name="research",
        role="Security Auditor",
        prompt="Find file where constants are defined",
        current_model="inherit",
        host="antigravity",
        config=cfg,
    )
    assert pinned_frontier.tier == "frontier"
    assert pinned_frontier.target_model == "inherit"
    assert pinned_frontier.should_overwrite is False
    assert pinned_frontier.reason == "workspace_config:pinned_role"

    # 3. FR-010 Clarification Q1: workspace high_risk_keyword overrides lower-tier pinned_role
    high_risk_override = classify_subagent_call(
        type_name="research",
        role="Codebase Researcher",
        prompt="Survey all vietmoney CRM integration handlers",
        current_model="inherit",
        host="antigravity",
        config=cfg,
    )
    assert high_risk_override.tier == "frontier"
    assert high_risk_override.target_model == "inherit"
    assert high_risk_override.should_overwrite is False
    assert high_risk_override.reason == "workspace_config:high_risk_keyword"

    # 4. FR-010 Clarification Q1: built-in high-risk pattern also overrides lower-tier pinned_role
    builtin_override = classify_subagent_call(
        type_name="research",
        role="Log Locator",
        prompt="Check alembic migration logs",
        current_model="inherit",
        host="antigravity",
        config=cfg,
    )
    assert builtin_override.tier == "frontier"
    assert builtin_override.target_model == "inherit"
    assert builtin_override.should_overwrite is False
    assert builtin_override.reason == "safety_guardrail:alembic_or_migration"

    # 5. extract_workspace_dir helper extracts workspacePaths[0] or cwd
    assert extract_workspace_dir({"workspacePaths": ["/repo/chaxy"]}) == "/repo/chaxy"
    assert extract_workspace_dir({"cwd": "/repo/claude"}) == "/repo/claude"
    assert extract_workspace_dir({}) is None


def test_strip_benign_code_identifiers_and_pure_lookup_disambiguation() -> None:
    masked = strip_benign_code_identifiers(
        "Find the file where auth_service.py is defined and check_permission_gate or AuthPermissionService is called"
    )
    assert "auth_service.py" not in masked
    assert "check_permission_gate" not in masked
    assert "AuthPermissionService" not in masked

    # Standalone natural-language words remain unmasked
    unmasked_nl = strip_benign_code_identifiers(
        "Audit auth and permission checks in the service"
    )
    assert "auth" in unmasked_nl.lower()
    assert "permission" in unmasked_nl.lower()

    # 1. Pure read-only lookup referencing auth_service.py routes to lite (FR-001, FR-002)
    lookup_auth = classify_subagent_call(
        type_name="research",
        role="File Locator",
        prompt="Find the file where auth_service.py is defined",
        current_model="inherit",
        host="antigravity",
    )
    assert lookup_auth.tier == "lite"
    assert lookup_auth.target_model == "flash_lite"
    assert lookup_auth.should_overwrite is True
    assert lookup_auth.reason == "read_only_simple_lookup"

    # 2. Pure read-only lookup referencing 001_migration.py routes to lite
    lookup_mig = classify_subagent_call(
        type_name="Explore",
        role="File Locator",
        prompt="Locate 001_migration.py in the repository",
        current_model="",
        host="claude",
    )
    assert lookup_mig.tier == "lite"
    assert lookup_mig.target_model == "haiku"
    assert lookup_mig.should_overwrite is True

    # 3. Genuine security audit on auth_service.py stays on frontier
    audit_auth = classify_subagent_call(
        type_name="research",
        role="Security Auditor",
        prompt="Audit auth_service.py for JWT token bypass",
        current_model="inherit",
        host="antigravity",
    )
    assert audit_auth.tier == "frontier"
    assert audit_auth.target_model == "inherit"
    assert audit_auth.should_overwrite is False
    assert audit_auth.reason.startswith("safety_guardrail:")

    # 4. Genuine migration rollback investigation on 001_migration.py stays on frontier
    investigate_mig = classify_subagent_call(
        type_name="research",
        role="DB Investigator",
        prompt="Investigate migration rollback failure in 001_migration.py",
        current_model="inherit",
        host="antigravity",
    )
    assert investigate_mig.tier == "frontier"
    assert investigate_mig.should_overwrite is False

    # 5. Workspace custom high_risk_keywords are enforced on unmasked text (FR-003)
    custom_cfg = DownshiftConfig(high_risk_keywords=("auth_service.py",))
    custom_blocked = classify_subagent_call(
        type_name="research",
        role="File Locator",
        prompt="Find the file where auth_service.py is defined",
        current_model="inherit",
        host="antigravity",
        config=custom_cfg,
    )
    assert custom_blocked.tier == "frontier"
    assert custom_blocked.should_overwrite is False
    assert custom_blocked.reason == "workspace_config:high_risk_keyword"


def test_score_prompt_complexity_and_promotion_from_lite_to_fast() -> None:
    # 1. Repeated occurrences of the same path are deduplicated (edge case)
    dup_profile = score_prompt_complexity(
        "Find file where resolve_contact.py is imported in resolve_contact.py and exported from resolve_contact.py"
    )
    assert dup_profile["path_count"] == 1
    dup_decision = classify_subagent_call(
        type_name="research",
        role="File Locator",
        prompt="Find file where resolve_contact.py is imported in resolve_contact.py and exported from resolve_contact.py",
        current_model="inherit",
        host="antigravity",
    )
    assert dup_decision.tier == "lite"

    # 2. >= 3 distinct file paths promotes simple lookup to fast (FR-004, FR-005)
    multi_path_prompt = (
        "Find file definitions and check imports across backend/app/main.py, "
        "backend/app/services/webhook_service.py, and frontend/src/App.tsx"
    )
    mp_profile = score_prompt_complexity(multi_path_prompt)
    assert int(mp_profile["path_count"]) >= 3
    mp_decision = classify_subagent_call(
        type_name="research",
        role="File Locator",
        prompt=multi_path_prompt,
        current_model="inherit",
        host="antigravity",
    )
    assert mp_decision.tier == "fast"
    assert mp_decision.target_model == "flash"
    assert mp_decision.reason == "promoted_to_fast:multi_file_or_long_prompt"

    # 3. Prompt > 600 chars promotes simple lookup to fast
    long_prompt = "Find file where resolve_contact is defined. " + (
        "Additional repository context for locating caller module. " * 12
    )
    assert score_prompt_complexity(long_prompt)["char_len"] > 600
    long_decision = classify_subagent_call(
        type_name="Explore",
        role="Locator",
        prompt=long_prompt,
        current_model="",
        host="claude",
    )
    assert long_decision.tier == "fast"
    assert long_decision.target_model == "sonnet"
    assert long_decision.reason == "promoted_to_fast:multi_file_or_long_prompt"

    # 4. Fenced code block promotes simple lookup to fast
    fence_prompt = "Where is this function defined?\n```python\ndef handle_webhook(payload):\n    return True\n```"
    assert score_prompt_complexity(fence_prompt)["has_code_fence"] is True
    fence_decision = classify_subagent_call(
        type_name="research",
        role="Code Locator",
        prompt=fence_prompt,
        current_model="inherit",
        host="antigravity",
    )
    assert fence_decision.tier == "fast"
    assert fence_decision.reason == "promoted_to_fast:multi_file_or_long_prompt"

    # 5. Stack trace promotes simple lookup to fast
    trace_prompt = (
        "Locate the line causing this error:\n"
        "Traceback (most recent call last):\n"
        '  File "backend/app/main.py", line 42, in startup\n'
        "    raise RuntimeError('boom')"
    )
    assert score_prompt_complexity(trace_prompt)["has_stack_trace"] is True
    trace_decision = classify_subagent_call(
        type_name="research",
        role="Traceback Locator",
        prompt=trace_prompt,
        current_model="inherit",
        host="antigravity",
    )
    assert trace_decision.tier == "fast"
    assert trace_decision.reason == "promoted_to_fast:multi_file_or_long_prompt"


def test_bilingual_vietnamese_routing_and_safety_guardrails() -> None:
    # 1. Vietnamese simple lookups (accented & unaccented) -> lite (FR-006)
    vi_lookup_1 = classify_subagent_call(
        type_name="research",
        role="Tìm kiếm",
        prompt="Tìm file định nghĩa hàm resolve_contact",
        current_model="inherit",
        host="antigravity",
    )
    assert vi_lookup_1.tier == "lite"
    assert vi_lookup_1.target_model == "flash_lite"

    vi_lookup_2 = classify_subagent_call(
        type_name="Explore",
        role="Tim kiem",
        prompt="Tim ham check_health o file nao trong repo?",
        current_model="",
        host="claude",
    )
    assert vi_lookup_2.tier == "lite"
    assert vi_lookup_2.target_model == "haiku"

    # 2. Vietnamese codebase survey & pure test/lint runner -> fast (FR-007)
    vi_survey = classify_subagent_call(
        type_name="research",
        role="Khảo sát mã nguồn",
        prompt="Tóm tắt luồng xử lý tin nhắn và kiểm tra toàn bộ các nơi gọi NotificationService",
        current_model="inherit",
        host="antigravity",
    )
    assert vi_survey.tier == "fast"
    assert vi_survey.target_model == "flash"
    assert vi_survey.reason == "read_only_research_survey"

    vi_test_runner = classify_subagent_call(
        type_name="self",
        role="Kiểm thử",
        prompt="Chạy pytest và kiểm tra lint trên thư mục scripts",
        current_model="inherit",
        host="antigravity",
    )
    assert vi_test_runner.tier == "fast"
    assert vi_test_runner.target_model == "flash"
    assert vi_test_runner.reason == "verification_test_or_lint_runner"

    # 3. Vietnamese code mutation blocks Tier 2 test-runner offloading on self (FR-007)
    vi_mutation = classify_subagent_call(
        type_name="self",
        role="Kỹ sư backend",
        prompt="Sửa lỗi trong webhook_service.py rồi chạy pytest",
        current_model="inherit",
        host="antigravity",
    )
    assert vi_mutation.tier == "frontier"
    assert vi_mutation.should_overwrite is False
    assert vi_mutation.reason == "frontier_default_stateful_work"

    # 4. Vietnamese high-risk engineering terms stay on frontier (FR-008)
    vi_high_risk_prompts = [
        "Kiểm tra phân quyền và xác thực người dùng trong tenants.py",
        "Ra soat bao mat va phan quyen API",
        "Phân tích lỗi khóa bảng và tranh chấp đồng thời khi tạo contact",
        "Kiem tra di tru du lieu va schema trong backend",
        "Điều tra lỗi mất tin nhắn trên production",
        "Tìm file auth_service.py và kiểm tra lỗi bảo mật JWT",
    ]
    for prompt in vi_high_risk_prompts:
        decision = classify_subagent_call(
            type_name="research",
            role="Kiểm tra",
            prompt=prompt,
            current_model="inherit",
            host="antigravity",
        )
        assert decision.tier == "frontier", f"Expected frontier for: {prompt}"
        assert decision.should_overwrite is False
        assert decision.reason.startswith("safety_guardrail:")

    # 5. Vietnamese PreInvocation user prompt classification (FR-009)
    vi_nudge = classify_user_prompt(
        "Quét log và tìm tất cả các file gọi NotificationService",
        invocation_num=1,
        host="antigravity",
    )
    assert vi_nudge.should_inject is True

    vi_no_nudge_high_risk = classify_user_prompt(
        "Tìm tất cả lỗi bảo mật và phân quyền trong hệ thống",
        invocation_num=1,
        host="antigravity",
    )
    assert vi_no_nudge_high_risk.should_inject is False
    assert vi_no_nudge_high_risk.reason == "high_risk_or_frontier_tag"


def test_codex_tier_model_mapping_and_spawn_agent_routing() -> None:
    # 1. Host detection for spawn_agent, multi_agent_v1__spawn_agent, and PLUGIN_DATA
    assert detect_host({"tool_name": "spawn_agent", "tool_input": {}}) == "codex"
    assert (
        detect_host({"tool_name": "multi_agent_v1__spawn_agent", "tool_input": {}})
        == "codex"
    )
    assert (
        detect_host(
            {"hook_event_name": "UserPromptSubmit", "prompt": "hi"},
            env={"PLUGIN_DATA": "/tmp/codex-plugin"},
        )
        == "codex"
    )

    # 2. Codex Tier 1 (lite -> gpt-6-luna) on simple lookup via spawn_agent
    lite_codex = classify_subagent_call(
        type_name="spawn_agent",
        role="spawn_agent",
        prompt="Find the file where resolve_contact is defined",
        current_model="",
        host="codex",
    )
    assert lite_codex.tier == "lite"
    assert lite_codex.target_model == "gpt-6-luna"
    assert lite_codex.should_overwrite is True
    assert lite_codex.reason == "read_only_simple_lookup"

    # 3. Codex Tier 2 (fast -> gpt-6.1-sol) on codebase survey and verification runner
    fast_survey_codex = classify_subagent_call(
        type_name="spawn_agent",
        role="spawn_agent",
        prompt="Survey all callers of NotificationService across the backend and summarize the flow",
        current_model="",
        host="codex",
    )
    assert fast_survey_codex.tier == "fast"
    assert fast_survey_codex.target_model == "gpt-6.1-sol"
    assert fast_survey_codex.should_overwrite is True
    assert fast_survey_codex.reason == "read_only_research_survey"

    fast_test_codex = classify_subagent_call(
        type_name="spawn_agent",
        role="spawn_agent",
        prompt="Run pytest tests/unit -v and ruff check backend/",
        current_model="",
        host="codex",
    )
    assert fast_test_codex.tier == "fast"
    assert fast_test_codex.target_model == "gpt-6.1-sol"
    assert fast_test_codex.should_overwrite is True
    assert fast_test_codex.reason == "verification_test_or_lint_runner"

    # 4. Codex Tier 3 (frontier -> inherit) on high-risk prompt even when caller requested gpt-6-luna
    high_risk_codex = classify_subagent_call(
        type_name="spawn_agent",
        role="spawn_agent",
        prompt="Audit alembic migration lock contention and savepoint rollback",
        current_model="gpt-6-luna",
        host="codex",
    )
    assert high_risk_codex.tier == "frontier"
    assert high_risk_codex.target_model == "inherit"
    assert high_risk_codex.should_overwrite is True
    assert high_risk_codex.reason.startswith("safety_guardrail:")

    # 5. Codex UserPromptSubmit advisory mentions spawn_agent and gpt-6-luna/gpt-6.1-sol
    codex_advisory = classify_user_prompt(
        "Search the codebase and list all callers of NotificationService",
        invocation_num=1,
        host="codex",
    )
    assert codex_advisory.should_inject is True
    assert "spawn_agent" in codex_advisory.message
    assert "gpt-6-luna/gpt-6.1-sol" in codex_advisory.message


def test_classify_user_prompt_parallel_map_en() -> None:
    agy = classify_user_prompt(
        "Map out the issue and compare the spec against develop across backend and frontend",
        invocation_num=1,
        host="antigravity",
    )
    assert agy.should_inject is True
    assert agy.reason == "parallel_issue_map_or_comparison"
    assert "4-step parallel fan-out" in agy.message
    assert "invoke_subagent" in agy.message
    assert "flash_lite/flash" in agy.message
    assert "reconcile cross-layer boundary contracts" in agy.message

    claude = classify_user_prompt(
        "Check the new plugin downshift for me, all of it",
        invocation_num=1,
        host="claude",
    )
    assert claude.should_inject is True
    assert claude.reason == "parallel_issue_map_or_comparison"
    assert "Task/Explore" in claude.message
    assert "Haiku/Sonnet" in claude.message

    codex = classify_user_prompt(
        "Audit the entire codebase and compare the PR against main",
        invocation_num=1,
        host="codex",
    )
    assert codex.should_inject is True
    assert codex.reason == "parallel_issue_map_or_comparison"
    assert "spawn_agent" in codex.message
    assert "gpt-6-luna/gpt-6.1-sol" in codex.message

    turn2 = classify_user_prompt(
        "Map out the issue across backend and frontend",
        invocation_num=2,
        host="antigravity",
    )
    assert turn2.should_inject is False
    assert turn2.reason == "not_first_turn_or_empty"


def test_classify_user_prompt_parallel_map_vi() -> None:
    vi_accented = classify_user_prompt(
        "Phân tích issue và đối chiếu spec với codebase",
        invocation_num=1,
        host="antigravity",
    )
    assert vi_accented.should_inject is True
    assert vi_accented.reason == "parallel_issue_map_or_comparison"
    assert "invoke_subagent" in vi_accented.message

    vi_unaccented = classify_user_prompt(
        "phan tich issue va so sanh nhanh develop",
        invocation_num=1,
        host="claude",
    )
    assert vi_unaccented.should_inject is True
    assert vi_unaccented.reason == "parallel_issue_map_or_comparison"
    assert "Task/Explore" in vi_unaccented.message

    vi_audit_accented = classify_user_prompt(
        "Rà soát toàn bộ plugin và kiểm tra tất cả",
        invocation_num=1,
        host="codex",
    )
    assert vi_audit_accented.should_inject is True
    assert vi_audit_accented.reason == "parallel_issue_map_or_comparison"
    assert "spawn_agent" in vi_audit_accented.message

    vi_audit_unaccented = classify_user_prompt(
        "ra soat toan bo va kiem tra tat ca",
        invocation_num=1,
        host="antigravity",
    )
    assert vi_audit_unaccented.should_inject is True
    assert vi_audit_unaccented.reason == "parallel_issue_map_or_comparison"


def test_classify_user_prompt_parallel_map_suppressed_by_high_risk_or_tag() -> None:
    high_risk_en = classify_user_prompt(
        "Map out the issue for the JWT authentication bypass and alembic migration",
        invocation_num=1,
        host="antigravity",
    )
    assert high_risk_en.should_inject is False
    assert high_risk_en.reason == "high_risk_or_frontier_tag"

    frontier_tag = classify_user_prompt(
        "@pro map out issue #18 across backend and frontend",
        invocation_num=1,
        host="antigravity",
    )
    assert frontier_tag.should_inject is False
    assert frontier_tag.reason == "high_risk_or_frontier_tag"

    high_risk_vi = classify_user_prompt(
        "Phân tích issue phân quyền và bảo mật trên nhánh develop",
        invocation_num=1,
        host="claude",
    )
    assert high_risk_vi.should_inject is False
    assert high_risk_vi.reason == "high_risk_or_frontier_tag"


def test_classify_user_prompt_routine_search_preserved() -> None:
    routine_en = classify_user_prompt(
        "Scan logs for timeout errors and list all callers of send_message",
        invocation_num=1,
        host="antigravity",
    )
    assert routine_en.should_inject is True
    assert routine_en.reason == "routine_search_or_log_task"

    routine_vi = classify_user_prompt(
        "Kiểm tra toàn bộ log và tìm tất cả các file gọi hàm send_message",
        invocation_num=1,
        host="antigravity",
    )
    assert routine_vi.should_inject is True
    assert routine_vi.reason == "routine_search_or_log_task"


def test_codex_and_claude_slice_mapper_routes_to_lite_or_fast() -> None:
    # 1. OpenAI Codex spawn_agent bounded slice lookup (<600 chars, <3 files) -> Tier 1 lite (gpt-6-luna)
    codex_lite = classify_subagent_call(
        type_name="spawn_agent",
        role="Slice 1 Backend Mapper",
        prompt="Read backend/app/models/tenant.py and backend/app/schemas/tenant.py and extract the field and type signatures",
        current_model="inherit",
        host="codex",
    )
    assert codex_lite.tier == "lite"
    assert codex_lite.target_model == "gpt-6-luna"
    assert codex_lite.should_overwrite is True
    assert codex_lite.reason == "read_only_simple_lookup"

    # 2. Claude Code default general-purpose Task bounded slice lookup -> Tier 1 lite (haiku)
    claude_lite = classify_subagent_call(
        type_name="general-purpose",
        role="Slice 1 Route Extractor",
        prompt="Inspect the file backend/app/api/tenants.py and extract the route signatures",
        current_model="",
        host="claude",
    )
    assert claude_lite.tier == "lite"
    assert claude_lite.target_model == "haiku"
    assert claude_lite.should_overwrite is True
    assert claude_lite.reason == "read_only_simple_lookup"

    # 3. Vietnamese accented & unaccented slice lookup on Codex / Claude -> Tier 1 lite
    vi_accented_lite = classify_subagent_call(
        type_name="spawn_agent",
        role="Slice 1 Nghiên cứu",
        prompt="Đọc file backend/app/main.py và trích xuất route",
        current_model="inherit",
        host="codex",
    )
    assert vi_accented_lite.tier == "lite"
    assert vi_accented_lite.target_model == "gpt-6-luna"

    vi_unaccented_lite = classify_subagent_call(
        type_name="general-purpose",
        role="Slice 2 Khao sat",
        prompt="Doc file backend/app/main.py va trich xuat chu ky",
        current_model="",
        host="claude",
    )
    assert vi_unaccented_lite.tier == "lite"
    assert vi_unaccented_lite.target_model == "haiku"

    # 4. Multi-file slice comparison (>=3 files or COMPLEX_RESEARCH_RE) -> Tier 2 fast
    claude_fast = classify_subagent_call(
        type_name="general-purpose",
        role="Slice 2 API Comparator",
        prompt=(
            "Map out slice across backend/app/api/tenants.py, "
            "backend/app/services/tenant_service.py, and "
            "backend/app/repositories/tenant.py and check what already exists vs develop"
        ),
        current_model="",
        host="claude",
    )
    assert claude_fast.tier == "fast"
    assert claude_fast.target_model == "sonnet"
    assert claude_fast.should_overwrite is True
    assert claude_fast.reason == "read_only_research_survey"

    # 5. Role-only read-only slice survey without explicit lookup verb -> Tier 2 fast
    role_only_fast = classify_subagent_call(
        type_name="spawn_agent",
        role="Slice 3 Frontend Comparator",
        prompt="Check frontend/src/hooks/useWorkspaceStore.ts and summarize state transitions",
        current_model="inherit",
        host="codex",
    )
    assert role_only_fast.tier == "fast"
    assert role_only_fast.target_model == "gpt-6.1-sol"
    assert role_only_fast.should_overwrite is True
    assert role_only_fast.reason == "read_only_research_survey"


def test_read_only_role_never_downgrades_code_mutation_or_high_risk() -> None:
    mutation_prompts = [
        "Read backend/app/api/tenants.py and update the endpoint to fix the bug",
        "Inspect backend/app/models/tenant.py and add a new field for workspace_cap",
        "Read backend/app/services/tenant_service.py and delete the method",
        "Inspect the slice and apply the fix to backend/app/main.py",
        "Đọc file backend/app/main.py và chỉnh sửa logic xử lý",
        "Doc file backend/app/main.py va them moi endpoint",
    ]
    for prompt in mutation_prompts:
        decision = classify_subagent_call(
            type_name="general-purpose",
            role="Slice 1 Backend Mapper",
            prompt=prompt,
            current_model="",
            host="claude",
        )
        assert decision.tier == "frontier", (
            f"Expected frontier for mutation prompt: {prompt}"
        )
        assert decision.target_model == "inherit"
        assert decision.should_overwrite is False

    # Audit/review intent with a Mapper role also stays on frontier
    audit_decision = classify_subagent_call(
        type_name="spawn_agent",
        role="Slice 1 Mapper",
        prompt="Read backend/app/services/tenant_service.py and review the implementation for bugs",
        current_model="inherit",
        host="codex",
    )
    assert audit_decision.tier == "frontier"
    assert audit_decision.should_overwrite is False

    # High-risk domain with a Mapper role stays on frontier
    high_risk_decision = classify_subagent_call(
        type_name="general-purpose",
        role="Slice 1 Auth Mapper",
        prompt="Read backend/app/core/security.py and extract JWT RBAC permission contracts",
        current_model="",
        host="claude",
    )
    assert high_risk_decision.tier == "frontier"
    assert high_risk_decision.reason.startswith("safety_guardrail:")


def test_classify_user_prompt_respects_custom_high_risk_keywords() -> None:
    cfg = DownshiftConfig(high_risk_keywords=(" vietmoney_vault ", "billing_ledger"))

    # Parallel mapping prompt containing custom high-risk keyword is suppressed
    suppressed_map = classify_user_prompt(
        "Map out the issue for vietmoney_vault across backend and frontend",
        invocation_num=1,
        host="antigravity",
        config=cfg,
    )
    assert suppressed_map.should_inject is False
    assert suppressed_map.reason == "high_risk_or_frontier_tag"

    # Routine search prompt containing custom high-risk keyword is suppressed via should_nudge_pre_invocation
    assert (
        should_nudge_pre_invocation(
            "Search the logs for billing_ledger errors",
            invocation_num=1,
            host="antigravity",
            config=cfg,
        )
        is None
    )

    # Unrelated parallel mapping prompt still nudges normally with the same config
    allowed_map = classify_user_prompt(
        "Map out the issue for contact tags across backend and frontend",
        invocation_num=1,
        host="antigravity",
        config=cfg,
    )
    assert allowed_map.should_inject is True
    assert allowed_map.reason == "parallel_issue_map_or_comparison"
