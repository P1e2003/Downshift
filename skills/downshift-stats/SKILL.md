---
name: downshift-stats
description: >
  Show Downshift's live model-routing scoreboard from ~/.gemini/downshift_ledger.jsonl:
  total subagents intercepted, frontier calls saved, and Tier 1 (lite) / Tier 2 (fast) /
  Tier 3 (frontier) breakdown. Trigger: /downshift-stats, "downshift stats", "router stats",
  "how many frontier calls did downshift save", "model routing scoreboard".
---

# Downshift Stats

1. **On Google Antigravity (`generative_ui` / `<agent-embed>` available — Default UI)**: Run the scoreboard CLI from the Downshift plugin directory with `--html`, register `<artifact_dir>/downshift_scoreboard.html` via `write_file` (`Overwrite: true`, `ArtifactMetadata` with `UserFacing: true`, `RequestFeedback: false`), and embed the interactive HTML scoreboard card inline **by default** (do **not** paste the raw `text` box unless the user asks for plain text):

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py --html "<artifact_dir>/downshift_scoreboard.html"
```

```markdown
<agent-embed src="file:///<artifact_dir>/downshift_scoreboard.html"></agent-embed>
```

2. **On Terminal CLI Hosts (`Claude Code`, `OpenAI Codex`, `Gemini CLI` — no `<agent-embed>`)**: Run the scoreboard CLI once from the Downshift plugin directory and display its box-drawn output verbatim in a fenced `text` block:

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py
```

3. If the user asks to clean synthetic empty or corrupted rows from the ledger (preserving all valid routing events), run:

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py --clean
```

4. If the user asks to reset the ledger, run:

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py --reset
```
