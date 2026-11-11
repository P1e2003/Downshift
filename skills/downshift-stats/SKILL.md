---
name: downshift-stats
description: >
  Show Downshift's live model-routing scoreboard from ~/.gemini/downshift_ledger.jsonl:
  total subagents intercepted, frontier calls saved, and Tier 1 (lite) / Tier 2 (fast) /
  Tier 3 (frontier) breakdown. Trigger: /downshift-stats, "downshift stats", "router stats",
  "how many frontier calls did downshift save", "model routing scoreboard".
---

# Downshift Stats

1. **Default (1 tool call)**: Run the scoreboard CLI once and display its box-drawn output verbatim in a fenced `text` block (do **not** generate or write an HTML file unless requested):

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py
```

2. **Optional HTML widget (`generative_ui`)**: Only if the user explicitly asks for an interactive HTML widget/card on Google Antigravity, generate and embed it:

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py --html "<artifact_dir>/downshift_scoreboard.html"
```

Then include `<agent-embed src="file:///<artifact_dir>/downshift_scoreboard.html"></agent-embed>` in your response (after calling `write_file` with `UserFacing: true` if required by the host).

3. If the user asks to clean synthetic empty or corrupted rows from the ledger (preserving all valid routing events), run:

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py --clean
```

4. If the user asks to reset the ledger, run:

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py --reset
```
