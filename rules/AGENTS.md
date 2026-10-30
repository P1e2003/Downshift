# Downshift — Smart Subagent Model Routing Rules

Always follow these model-tier delegation rules when spawning subagents (`invoke_subagent` in Google Antigravity or `Task` / `Agent` in Claude Code & OpenAI Codex):

## 1. 3-Tier Subagent Routing Policy

| Tier | Antigravity Model | Claude Code Model | When to Use |
| :--- | :--- | :--- | :--- |
| **Tier 1 (`lite`)** | `flash_lite` | `haiku` | Bounded read-only lookups: locating files, checking `git status` / `git branch`, listing directory contents, counting lines, or finding where a symbol is defined. |
| **Tier 2 (`fast`)** | `flash` | `sonnet` | Broad read-only codebase surveys (`research` / `Explore`), tracing callers across modules, log scanning, and running pure test/lint/build verification (`pytest`, `vitest`, `ruff`, `eslint`, `npm run build`). |
| **Tier 3 (`frontier`)** | `inherit` | `inherit` | Architecture design, multi-file stateful code mutations, database migrations (`alembic`), concurrency/locking (`FOR UPDATE`, `advisory lock`, `savepoint`, `deadlock`), and security/auth/permission logic. |

## 2. Delegate Mechanical Exploration Instead of Looping on the Frontier Model

- When a task requires multi-step file discovery, log scanning, or running a test/lint suite to inspect failures, delegate that read-only or verification step to a subagent rather than burning multiple frontier-model turns.
- Downshift's `PreToolUse` lifecycle hook automatically inspects subagent invocations and downshifts eligible calls to `flash_lite`/`flash` (Antigravity) or `haiku`/`sonnet` (Claude Code) while enforcing safety guardrails.

## 3. Inline `@tag` Overrides

Include any of these inline tags in a prompt or subagent `Role`/`Prompt` to override heuristic routing:

- `@pro`, `@opus`, `@frontier`, `@inherit` — Force **Tier 3 (`frontier`)**; never downshift.
- `@flash`, `@sonnet`, `@fast`, `@downshift` — Force **Tier 2 (`fast`)**.
- `@flash_lite`, `@lite`, `@haiku` — Force **Tier 1 (`lite`)**.
