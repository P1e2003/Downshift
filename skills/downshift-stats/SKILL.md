---
name: downshift-stats
description: >
  Show Downshift's live model-routing scoreboard from ~/.gemini/downshift_ledger.jsonl:
  total subagents intercepted, frontier calls saved, and Tier 1 (lite) / Tier 2 (fast) /
  Tier 3 (frontier) breakdown. Trigger: /downshift-stats, "downshift stats", "router stats",
  "how many frontier calls did downshift save", "model routing scoreboard".
---

# Downshift Stats

Run the scoreboard CLI and display its output verbatim in a fenced `text` block:

```bash
python ./scripts/stats_cli.py
```

If the user asks to reset the ledger, run:

```bash
python ./scripts/stats_cli.py --reset
```

