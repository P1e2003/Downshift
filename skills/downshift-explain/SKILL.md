---
name: downshift-explain
description: >
  Dry-run trace of Downshift subagent routing decisions (winning precedence stage, masked tokens,
  guardrail matches, .downshift.json provenance, and 3-host model matrix) plus read-only ledger
  hotspot audits — zero writes to ~/.gemini/downshift_ledger.jsonl. Trigger: /downshift-explain,
  "why did downshift route", "explain downshift routing", "calibrate downshift", "audit downshift ledger".
---

# Downshift Explain & Calibration Audit

1. **Explain a specific subagent routing decision (100% pure dry-run, zero ledger I/O)**: Run `stats_cli.py explain` from the Downshift plugin directory via the portable launcher and display its box-drawn output in a fenced `text` block (or pass `--json` when summarizing programmatically):

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py explain \
  --type research \
  --role "<role>" \
  --prompt "<prompt>" \
  --workspace "." \
  --host all
```

2. **Audit the live ledger for guardrail hotspots & false-positive calibration candidates (strictly read-only)**: When the user asks to calibrate Downshift or find read-only mappers hitting `frontier`, run:

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py --audit-ledger
```

Optional flags:
- `--host antigravity|claude|codex|all` — filter the host output matrix in `explain`
- `--current-model <model>` — simulate an explicit caller model pin in `explain`
- `--limit N` — inspect only the most recent `N` events in `--audit-ledger`
- `--json` — emit machine-readable JSON for either `explain` or `--audit-ledger`
