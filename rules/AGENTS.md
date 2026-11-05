# Downshift — Smart Subagent Model Routing Rules

Always follow these model-tier delegation rules when spawning subagents (`invoke_subagent` in Google Antigravity, `spawn_agent` in OpenAI Codex, or `Task` / `Agent` in Claude Code):

## 1. 3-Tier Subagent Routing Policy

| Tier | Antigravity Model | Claude Code Model | OpenAI Codex Model | When to Use |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1 (`lite`)** | `flash_lite` | `haiku` | `gpt-6-luna` (`low`) | Bounded read-only lookups: locating files, checking `git status` / `git branch`, listing directory contents, counting lines, or finding where a symbol is defined. |
| **Tier 2 (`fast`)** | `flash` | `sonnet` | `gpt-6.1-sol` (`medium`) | Broad read-only codebase surveys (`research` / `Explore` / `spawn_agent`), tracing callers across modules, log scanning, and running pure test/lint/build verification (`pytest`, `vitest`, `ruff`, `eslint`, `npm run build`). |
| **Tier 3 (`frontier`)** | `inherit` | `inherit` | `inherit` (`gpt-6-astra`) | Architecture design, multi-file stateful code mutations, database migrations (`alembic`), concurrency/locking (`FOR UPDATE`, `advisory lock`, `savepoint`, `deadlock`), and security/auth/permission logic. |

## 2. Delegate Mechanical Exploration & Parallel Multi-Subagent Fan-Out

- When a task requires multi-step file discovery, log scanning, or running a test/lint suite to inspect failures, delegate that read-only or verification step to a subagent (`invoke_subagent`, `spawn_agent`, or `Task` / `Agent`) rather than burning multiple frontier-model turns.
- Downshift's `PreToolUse` lifecycle hook automatically inspects subagent invocations and downshifts eligible calls to `flash_lite`/`flash` (Antigravity), `haiku`/`sonnet` (Claude Code), or `gpt-6-luna`/`gpt-6.1-sol` (OpenAI Codex) while enforcing safety guardrails.

### 4-Step Parallel Multi-Subagent Fan-Out Protocol (Issue Mapping & Baseline Comparison)

When mapping a new issue, auditing a plugin/repository, or comparing a specification against the baseline (`main` / `develop`) across multiple layers or modules, follow this 4-step workflow instead of running 10–15 sequential tool calls on the frontier model or spawning a single oversized subagent:

1. **Step 1 — Decompose Issue Scope (Main Agent, 1 Turn):** Read the issue or spec first and partition the investigation into **2–4 disjoint slices** (for example: Slice 1 = Backend Models/Migrations, Slice 2 = API Routes/Services, Slice 3 = Frontend UI/Hooks).
2. **Step 2 — Parallel Slice Mapping (Single-Turn Fan-Out):** Spawn **2–4 read-only subagents in the same tool-call turn** (`invoke_subagent` on Antigravity, `Task`/`Explore` on Claude Code, `spawn_agent` on OpenAI Codex) so all slices execute concurrently ($T_{\text{parallel}} \approx \max(T_i)$). Keep bounded file/symbol lookups `< 600` chars and `< 3` file paths so Downshift routes them to **Tier 1 (`lite`)**, or scope each subagent to a single module survey for **Tier 2 (`fast`)**.
3. **Step 3 — Parallel Baseline Comparison & Boundary Contract Extraction:** Instruct each subagent to compare its assigned slice against `main` / `develop` and return:
   - What already exists vs. what is missing
   - Stale spec assumptions and candidate files to touch
   - **Exact cross-layer boundary contracts** (API route paths, request/response field names such as `snake_case` vs `camelCase`, function/type signatures, and DB/lock invariants)
4. **Step 4 — Main-Agent Cross-Layer Check & Verdict (Fan-In):** Once all parallel subagents report back, the main (`frontier`) agent explicitly cross-checks boundary contracts across slices (e.g., Backend response fields vs. Frontend TypeScript interfaces, auth/tenant dependencies vs. caller headers), verifies security/concurrency/migration guardrails, and writes the final unified **Map-Out Verdict**.

**Parallel Fan-Out Guardrails:**
- **2–4 Subagent Cap (Anti-Storm Rule):** Never spawn more than `4` subagents in a single turn.
- **Minimum Task Threshold:** Do not fan out for `1–2` known files; read them directly or use a single Tier 1 (`lite`) lookup subagent.
- **Read-Only Fan-Out Boundary:** Parallel subagents sharing `Workspace="inherit"` must remain read-only (`research` / `Explore`) so concurrent workers never race on file edits or DB state.
- **Cross-Layer Boundary Blind-Spot Guardrail:** Because parallel subagents inspect slices in isolation, each subagent MUST report exact interface/payload signatures in Step 3 so the main agent can catch cross-layer mismatches in Step 4.

## 3. Inline `@tag` Overrides

Include any of these inline tags in a prompt or subagent `Role`/`Prompt` to override heuristic routing:

- `@pro`, `@opus`, `@frontier`, `@inherit` — Force **Tier 3 (`frontier`)**; never downshift.
- `@flash`, `@sonnet`, `@fast`, `@downshift` — Force **Tier 2 (`fast`)**.
- `@flash_lite`, `@lite`, `@haiku` — Force **Tier 1 (`lite`)**.

