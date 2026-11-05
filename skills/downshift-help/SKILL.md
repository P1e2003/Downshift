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

  4-Step Parallel Multi-Subagent Fan-Out (Issue Mapping & Comparison)
    1. Decompose   Split multi-layer issue/spec into 2-4 disjoint slices on the main agent
    2. Map         Spawn 2-4 read-only subagents in 1 turn (<600 chars & <3 paths -> Tier 1 lite)
    3. Compare     Each subagent compares its slice vs baseline & extracts exact boundary contracts
    4. Verdict     Main (frontier) agent reconciles cross-layer contracts (snake_case/camelCase, auth)
    Guardrails     Cap at 2-4 subagents/turn · 1-2 known files stay direct · read-only fan-out only

  Inline @tag Overrides
    @pro / @opus / @frontier / @inherit   Force Tier 3 (frontier)
    @flash / @sonnet / @fast / @downshift Force Tier 2 (fast)
    @flash_lite / @lite / @haiku          Force Tier 1 (lite)

  Modes & Workspace Config (.downshift.json / DOWNSHIFT_MODE)
    active (default)   Enforce model routing + record telemetry
    shadow             Dry-run: record telemetry without overwriting models
    off                Disable routing, telemetry, and hints
    .downshift.json    mode, hints, high_risk_keywords, pinned_roles

  Commands
    /downshift-stats   Show live routing scoreboard from ~/.gemini/downshift_ledger.jsonl
    /downshift-help    Show this reference card
```

