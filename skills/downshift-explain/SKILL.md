---
name: downshift-explain
description: >
  Dry-run trace of Downshift subagent routing decisions (winning precedence stage, masked tokens,
  guardrail matches, .downshift.json provenance, and 3-host model matrix) plus read-only ledger
  hotspot audits — zero writes to ~/.gemini/downshift_ledger.jsonl. Trigger: /downshift-explain,
  "why did downshift route", "explain downshift routing", "calibrate downshift", "audit downshift ledger".
---

# Downshift Explain & Calibration Audit

1. **On Google Antigravity (`generative_ui` / `<agent-embed>` available — Default UI)**: Run the diagnostic CLI from the Downshift plugin directory with `--html`, register the generated HTML file via `write_file` (`Overwrite: true`, `ArtifactMetadata` with `UserFacing: true`, `RequestFeedback: false`), and embed the interactive HTML card inline **by default** (do **not** paste the raw `text` box unless the user explicitly asks for plain text):

- **Explain a specific subagent routing decision (100% pure dry-run, zero ledger I/O):**

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py explain \
  --type research \
  --role "<role>" \
  --prompt "<prompt>" \
  --workspace "." \
  --host all \
  --html "<artifact_dir>/downshift_explain.html"
```

```markdown
<agent-embed src="file:///<artifact_dir>/downshift_explain.html"></agent-embed>
```

- **Audit the live ledger for guardrail hotspots & calibration candidates (strictly read-only):**

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py --audit-ledger --html "<artifact_dir>/downshift_audit.html"
```

```markdown
<agent-embed src="file:///<artifact_dir>/downshift_audit.html"></agent-embed>
```

2. **On Terminal CLI Hosts (`Claude Code`, `OpenAI Codex`, `Gemini CLI` — no `<agent-embed>`)**: Run without `--html` from the Downshift plugin directory and display the `<= 88`-col box-drawn output verbatim in a fenced `text` block:

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py explain \
  --type research \
  --role "<role>" \
  --prompt "<prompt>" \
  --workspace "." \
  --host all
```

```bash
"./scripts/run_hook.cmd" ./scripts/stats_cli.py --audit-ledger
```

Optional flags:
- `--host antigravity|claude|codex|all` — filter the host output matrix in `explain`
- `--current-model <model>` — simulate an explicit caller model pin in `explain`
- `--limit N` — inspect only the most recent `N` events in `--audit-ledger`
- `--json` — emit machine-readable JSON for either `explain` or `--audit-ledger`
