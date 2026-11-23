---
name: downshift-help
description: >
  Quick-reference card for Downshift model routing tiers, supported hosts
  (Google Antigravity, Claude Code, OpenAI Codex, Gemini CLI), safety guardrails,
  and inline @tag overrides. Trigger: /downshift-help, "downshift help",
  "how does downshift work", "what @tags does downshift support".
---

# Downshift Quick Reference

Display this compact reference card in a fenced `text` block:

```text
  downshift · multi-host subagent model router

  Tiers (English & Vietnamese EN/VI)
    Tier 1 (lite · 0.04x)      flash_lite / haiku / gpt-6-luna   file locate (incl. benign filenames), git status, tìm file/hàm
    Tier 2 (fast · 0.20x)      flash / sonnet / gpt-6.1-sol      codebase survey, >600-char/multi-file lookup, pytest/vitest/ruff
    Tier 3 (frontier · 1.00x)  inherit (Pro / Opus / Astra)      architecture, code edits, migrations, locking, auth, bảo mật

  Safety Guardrails (never downshifted)
    alembic · migration · deadlock · race condition · advisory lock · for update · savepoint
    rollback · security · auth · permission · idor · phân quyền · bảo mật · khóa bảng · di trú dữ liệu

  7-Stage v2 Subagent Orchestration & Effort Hand-Out
    1. Triage & Gate     <=2 already-known files -> 0 subagents (read/edit/verify directly on main agent)
    2. Decompose         Split 3+-file / multi-layer scope into 2-4 disjoint slices in 1 turn
    3. Effort Hand-Out   @low (unknown-file discovery & 1-4-file contract lookup) · @medium (3+-file survey/test/lint) · @high (arch/security audits/edits/migrations/locks)
    4. Parallel Fan-Out  Spawn 2-4 subagents in 1 turn (inherit = read-only; branch/share required for parallel @high edits)
    5. Hand-Back (<=15L) 4 fields (Files & Lines, Exists vs Missing, Boundary Contract, Risks) -> frontier cross-layer synthesis
    6. Adaptive Check    1-2 file checks direct; full test/lint/build via @medium · max 2-retry circuit breaker on failures
    7. Clean Release     Verify clean git status/diff, update progress tracker, and report verification steps
    Guardrails           Cap 2-4 subagents/turn (runtime: 5th+ fast -> lite) · <=2 known files direct · read-only shared workspace · boundary contract check

  Inline @tag Overrides (highest tier wins: @high > @medium > @low; safety guardrails block lower-tier tags)
    @high / @pro / @opus / @frontier / @inherit        Force Tier 3 (frontier · high effort)
    @medium / @flash / @sonnet / @fast / @downshift    Route safe tasks to Tier 2 (fast · medium effort)
    @low / @flash_lite / @lite / @haiku                Route safe tasks to Tier 1 (lite · low effort)
    Host Effort          Antigravity: flash_lite/flash/inherit aliases (keep main chat on Carbon/Argon) · Claude: haiku/sonnet/inherit · Codex: model + low/medium/high reasoning_effort

  Modes & Workspace Config (.downshift.json / DOWNSHIFT_MODE / DOWNSHIFT_MAX_BURST)
    active (default)   Enforce model routing + record telemetry
    shadow             Dry-run: record telemetry without overwriting models
    off                Disable routing, telemetry, and hints
    .downshift.json    mode, hints, high_risk_keywords, pinned_roles, max_burst_subagents (4), burst_window_seconds (5), swarm_models ({lite, fast}), auto_upshift (true), retry_window_seconds (120), max_auto_retries (2), isolate_parallel_writes ("share"|"branch"|"warn"|"off")
    Antigravity Hooks  PreToolUse: invoke_subagent, swarm(action="add") (safe-by-default unless swarm_models set + sharded burst cap + auto worktree isolation to share/branch on concurrent writers), run_workflow (read-only telemetry) · PostToolUse: invoke_subagent|swarm (privacy-safe outcome capture)
    Closed-Loop Retry  Confirmed PostToolUse error, same prompt fingerprint, or explicit retry cue -> auto-upshift (lite->fast->frontier) & 2-retry circuit breaker (overrides @low/@medium in active; log-only in shadow; beats burst cap)
    Worktree Isolation 2nd+ concurrent write-capable workers (or sharded swarm writers) on inherit within burst_window -> auto-isolated to share/branch on Antigravity; schema-safe non-blocking warning on Claude/Codex (DOWNSHIFT_ISOLATE_PARALLEL_WRITES)

  Commands
    /downshift-stats   Show live routing scoreboard (gross + net credit cost saved & Quality & rework) from ~/.gemini/downshift_ledger.jsonl
    /downshift-help    Show this reference card
```

