# Downshift — Smart Subagent Model Routing Rules

Always follow these model-tier delegation rules when spawning subagents (`invoke_subagent` in Google Antigravity, `spawn_agent` in OpenAI Codex, or `Task` / `Agent` in Claude Code):

## 1. 3-Tier Subagent Routing Policy

| Tier | Antigravity Model | Claude Code Model | OpenAI Codex Model | When to Use |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1 (`lite`)** | `flash_lite` | `haiku` | `gpt-6-luna` (`low`) | Bounded read-only lookups: locating files, checking `git status` / `git branch`, listing directory contents, counting lines, or finding where a symbol is defined. |
| **Tier 2 (`fast`)** | `flash` | `sonnet` | `gpt-6.1-sol` (`medium`) | Broad read-only codebase surveys (`research` / `Explore` / `spawn_agent`), tracing callers across modules, log scanning, and running pure test/lint/build verification (`pytest`, `vitest`, `ruff`, `eslint`, `npm run build`). |
| **Tier 3 (`frontier`)** | `inherit` | `inherit` | `inherit` (`gpt-6-astra`, `high`) | Architecture design, multi-file stateful code mutations, database migrations (`alembic`), concurrency/locking (`FOR UPDATE`, `advisory lock`, `savepoint`, `deadlock`), and security/auth/permission logic. |

## 2. 7-Stage v2 End-to-End Subagent Orchestration & Effort Hand-Out Workflow

- When a task requires multi-step file discovery, log scanning, or running a test/lint suite to inspect failures, delegate that read-only or verification step to a subagent (`invoke_subagent`, `spawn_agent`, or `Task` / `Agent`) rather than burning multiple frontier-model turns.
- Downshift's `PreToolUse` lifecycle hook automatically inspects subagent invocations and downshifts eligible calls to `flash_lite`/`flash` (Antigravity), `haiku`/`sonnet` (Claude Code), or `gpt-6-luna`/`gpt-6.1-sol` (OpenAI Codex) while enforcing safety guardrails.

### 7-Stage v2 Orchestration Lifecycle

When drafting an issue, planning a feature/spec, mapping an issue against `main` / `develop`, or executing multi-module work, follow this deterministic 7-stage lifecycle instead of running 10–15 sequential tool calls on the frontier model or spawning an oversized subagent:

1. **Stage 1 — Triage & Fast-Path Gate (`<= 2` Known Files):** Read the issue or spec first. If the task touches **`<= 2` known files**, allocate **`0` subagents** and read, edit, and verify directly on the main agent to avoid subagent startup overhead.
2. **Stage 2 — Plan & Decompose Slices (`Main Agent, 1 Turn`):** For `3+`-file or multi-layer tasks, partition the work into **`2–4` disjoint slices** in a single turn (for example: Slice 1 = Backend Models/Migrations, Slice 2 = API Routes/Services, Slice 3 = Frontend UI/Hooks).
3. **Stage 3 — Deterministic Effort Hand-Out (`@low` / `@medium` / `@high`):** Assign an explicit effort tag to each slice based on scope and risk:

| Effort Tag | Resolved Tier | Host Models (`Antigravity` / `Claude` / `Codex`) | Slice Scope & Assignment Criteria |
| :--- | :---: | :--- | :--- |
| **`@low`** | **Tier 1 (`lite` · `0.04x`)** | `flash_lite` / `haiku` / `gpt-6-luna` (`low`) | Bounded `< 3`-file read-only lookups (`< 600` chars): locating files/symbols, extracting exact route/type signatures, `git status` / `git branch`, line counts. |
| **`@medium`** | **Tier 2 (`fast` · `0.20x`)** | `flash` / `sonnet` / `gpt-6.1-sol` (`medium`) | `3+`-file read-only module surveys, cross-module caller tracing, log scanning, baseline comparisons, and pure test/lint/build suite execution (`pytest`, `vitest`, `ruff`, `eslint`, `npm run build`). |
| **`@high`** | **Tier 3 (`frontier` · `1.00x`)** | `inherit` (`Pro` / `Opus` / `gpt-6-astra`, `high`) | Architecture audits, multi-file stateful code mutations, `alembic` migrations, `FOR UPDATE`/advisory locks, and security/auth/RBAC logic. |

4. **Stage 4 — Parallel Subagent Fan-Out (`Single-Turn Delegation`):** Spawn **`2–4` subagents concurrently in the same tool-call turn** (`invoke_subagent` on Antigravity, `Task`/`Explore` on Claude Code, `spawn_agent` on OpenAI Codex; $T_{\text{parallel}} \approx \max(T_i)$) with their `@low` / `@medium` / `@high` tag and target boundary questions. Keep shared `Workspace="inherit"` strictly read-only; any parallel mutating `@high` workers MUST use isolated `branch` / `share` worktrees.
5. **Stage 5 — Compact 4-Field Hand-Back (`<= 15` Lines) & Frontier Synthesis:** Require each subagent reply to stay **`<= 15` lines** covering four structured fields:
   - **`Files & Lines`**: Exact `path/to/file:Lstart-Lend` references inspected
   - **`Exists vs. Missing`**: Concise delta against the issue/spec baseline
   - **`Boundary Contract`**: Exact API route paths, request/response field names (`snake_case` vs `camelCase`), function/type signatures, and DB/lock/auth invariants
   - **`Risks / Stale Assumptions`**: Stale spec assumptions, edge cases, or high-risk guardrails flagged for the main agent
   Once all parallel subagents report back, the main (`frontier`) agent reconciles cross-layer boundary contracts across slices, verifies security/concurrency/migration guardrails, and executes stateful mutations (or reconciles isolated branches).
6. **Stage 6 — Adaptive Check & 2-Retry Circuit Breaker:** Run focused `1–2`-file lint/test checks directly on the main agent; delegate heavy or noisy full test/lint/build suites to a `@medium` verification subagent. Cap automatic fix-and-recheck cycles at **max `2` retries** (`2-retry circuit breaker`) before halting and surfacing the root blocker to the user.
7. **Stage 7 — Clean Diff & Release Back to User:** Verify `git status` / `git diff` cleanliness, update project progress tracking where applicable, and present a concise summary with verification steps.

**Parallel Fan-Out Guardrails:**
- **2–4 Subagent Cap (Anti-Storm Rule):** Never spawn more than `4` subagents in a single turn. Enforced at runtime by Downshift's `PreToolUse` dynamic burst-cap guardrail (`max_burst_subagents: 4` per `burst_window_seconds: 5` per `conversation_id`, configurable in `.downshift.json` or `DOWNSHIFT_MAX_BURST`), which atomically downgrades a 5th+ non-high-risk `fast` subagent to Tier 1 (`lite`, `reason="burst_cap_downgraded_to_lite"`).
- **Minimum Task Threshold:** Do not fan out for `1–2` known files — handle `<= 2` known files directly on the main agent (`0` subagents per Stage 1); reserve single `@low` (`lite`) lookups for locating unknown file/symbol paths.
- **Read-Only Shared-Workspace Boundary:** Parallel subagents sharing `Workspace="inherit"` must remain read-only (`research` / `Explore`) so concurrent workers never race on file edits or DB state; any parallel mutating `@high` workers must run in isolated `branch` / `share` worktrees.
- **Cross-Layer Boundary Blind-Spot Guardrail:** Because parallel subagents inspect slices in isolation, each subagent MUST report exact interface/payload signatures in Stage 5's `Boundary Contract` field so the `frontier` main agent can catch cross-layer mismatches before synthesis or mutation.

## 3. Inline `@tag` Overrides

Include any of these inline tags in a prompt or subagent `Role`/`Prompt` to override heuristic routing:

- `@high`, `@pro`, `@opus`, `@frontier`, `@inherit` — Force **Tier 3 (`frontier` / `high` effort)**; never downshift (upgrades explicit lower-tier models to `inherit`).
- `@medium`, `@flash`, `@sonnet`, `@fast`, `@downshift` — Route safe tasks to **Tier 2 (`fast` / `medium` effort)**; blocked by high-risk safety guardrails.
- `@low`, `@flash_lite`, `@lite`, `@haiku` — Route safe tasks to **Tier 1 (`lite` / `low` effort)**; blocked by high-risk safety guardrails.

When multiple inline tags appear in the same subagent call, the highest tier wins (`@high` > `@medium` > `@low`).

