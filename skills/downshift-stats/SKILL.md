---
name: downshift-stats
description: >
  Show Downshift's live model-routing scoreboard from ~/.gemini/downshift_ledger.jsonl:
  total subagents intercepted, frontier calls saved, and Tier 1 (lite) / Tier 2 (fast) /
  Tier 3 (frontier) breakdown. Trigger: /downshift-stats, "downshift stats", "router stats",
  "how many frontier calls did downshift save", "model routing scoreboard".
---

# Downshift Stats

1. Run the scoreboard CLI and display its box-drawn output verbatim in a fenced `text` block:

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py
```

2. **On Google Antigravity (`generative_ui`)**: Also generate the compact interactive HTML widget card into your conversation artifact directory and embed it inline below the text block:

```bash
python ./scripts/stats_cli.py --html "<artifact_dir>/downshift_scoreboard.html"
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
