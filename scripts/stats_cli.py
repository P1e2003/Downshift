"""Scoreboard CLI for Downshift local JSONL telemetry ledger (US-1.2 / Issue #5 & US-2.9 / Issue #23)."""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import sys
from pathlib import Path
from typing import Any

from downshift_core import clean_ledger_file, get_ledger_path, reset_ledger_file

TIER_COST_WEIGHT: dict[str, float] = {
    "frontier": 1.00,
    "fast": 0.20,
    "lite": 0.04,
}


def _render_bar(pct: float, width: int = 20) -> str:
    """Render a proportional Unicode block progress bar (█ and ·)."""
    clamped = max(0.0, min(100.0, pct))
    filled = round((clamped / 100.0) * width)
    return ("█" * filled) + ("·" * (width - filled))


def _truncate_cell(value: str, max_len: int = 53) -> str:
    """Clamp a single-line scoreboard field so 35-col prefix + value stays <= 88 cols."""
    return value if len(value) <= max_len else value[: max_len - 1] + "…"


def _format_top_reasons(reasons: dict[str, Any], limit: int = 4) -> str:
    """Format top routing reasons in descending count order."""
    return (
        ", ".join(
            f"{r}: {c}"
            for r, c in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        )
        or "none"
    )


def load_events(ledger_path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Read valid JSON objects from the JSONL ledger, skipping corrupted lines."""
    if not ledger_path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        for raw_line in ledger_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    events.append(item)
            except ValueError:
                continue
    except OSError:
        return []
    if limit is not None and limit > 0:
        return events[-limit:]
    return events


def compute_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate total subagents intercepted, frontier calls saved, cost ROI, and mode/host/reason counts."""
    total = len(events)
    tiers = {"lite": 0, "fast": 0, "frontier": 0}
    models: dict[str, int] = {}
    hosts: dict[str, int] = {}
    reasons: dict[str, int] = {}
    saved = 0
    active_count = 0
    shadow_count = 0
    guardrail_count = 0
    total_prompt_chars = 0
    actual_weighted_cost = 0.0

    for ev in events:
        tier = str(ev.get("tier") or "frontier")
        if tier not in tiers:
            tier = "frontier"
        tiers[tier] += 1
        actual_weighted_cost += TIER_COST_WEIGHT.get(tier, 1.00)
        model = str(ev.get("target_model") or "inherit")
        models[model] = models.get(model, 0) + 1
        if tier in ("lite", "fast") and bool(ev.get("overwritten", True)):
            saved += 1

        host = str(ev.get("host") or "antigravity").strip().lower() or "antigravity"
        hosts[host] = hosts.get(host, 0) + 1

        reason = str(ev.get("reason") or "unknown").strip() or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
        if (
            reason.startswith("safety_guardrail:")
            or reason == "workspace_config:high_risk_keyword"
            or (reason == "workspace_config:pinned_role" and tier == "frontier")
        ):
            guardrail_count += 1

        mode = str(ev.get("mode") or "active").strip().lower()
        if mode == "shadow":
            shadow_count += 1
        else:
            active_count += 1

        raw_chars = ev.get("prompt_chars", 0)
        try:
            total_prompt_chars += max(0, int(raw_chars or 0))
        except (ValueError, TypeError):
            pass

    saved_pct = round((saved / total) * 100.0, 1) if total > 0 else 0.0
    est_cost_saved_pct = (
        round((1.0 - (actual_weighted_cost / total)) * 100.0, 1) if total > 0 else 0.0
    )
    return {
        "total": total,
        "saved": saved,
        "saved_pct": saved_pct,
        "est_cost_saved_pct": est_cost_saved_pct,
        "active_count": active_count,
        "shadow_count": shadow_count,
        "total_prompt_chars": total_prompt_chars,
        "tiers": tiers,
        "models": models,
        "hosts": hosts,
        "reasons": reasons,
        "guardrail_count": guardrail_count,
    }


def render_ascii_scoreboard(summary: dict[str, Any], ledger_path: Path) -> str:
    """Render box-drawn ASCII scoreboard with proportional Unicode progress bars."""
    total = int(summary["total"])
    if total == 0:
        return (
            f"  ╭─ ⚡ downshift stats ──────────────── live ledger · {ledger_path}\n"
            "  │  0 subagents intercepted yet.\n"
            "  ╰─────────────────────────────────────────────────────────────────────────────────────\n"
        )

    saved = int(summary["saved"])
    saved_pct = float(summary["saved_pct"])
    est_cost_saved_pct = float(summary.get("est_cost_saved_pct", 0.0))
    router_cost_pct = round(max(0.0, 100.0 - est_cost_saved_pct), 1)
    active_count = int(summary.get("active_count", total))
    shadow_count = int(summary.get("shadow_count", 0))
    total_prompt_chars = int(summary.get("total_prompt_chars", 0))
    tiers = summary["tiers"]
    lite = int(tiers.get("lite", 0))
    fast = int(tiers.get("fast", 0))
    frontier = int(tiers.get("frontier", 0))
    hosts = summary.get("hosts") or {}
    host_summary = ", ".join(f"{h}: {c}" for h, c in sorted(hosts.items())) or "none"
    guardrail_count = int(summary.get("guardrail_count", 0))
    host_line = _truncate_cell(f"{host_summary} · {guardrail_count} guardrail(s)")
    top_reasons = _truncate_cell(_format_top_reasons(summary.get("reasons") or {}))

    lite_pct = round((lite / total) * 100.0, 1)
    fast_pct = round((fast / total) * 100.0, 1)
    frontier_pct = round((frontier / total) * 100.0, 1)

    baseline_bar = _render_bar(100.0, 20)
    router_bar = _render_bar(router_cost_pct, 20)
    lite_bar = _render_bar(lite_pct, 10)
    fast_bar = _render_bar(fast_pct, 10)
    frontier_bar = _render_bar(frontier_pct, 10)

    return (
        f"  ╭─ ⚡ downshift stats ──────────────── live ledger · {ledger_path}\n"
        "  │\n"
        f"  │  Total subagents intercepted : {total}  ({active_count} active, {shadow_count} shadow · {total_prompt_chars:,} prompt chars)\n"
        f"  │  Frontier calls saved        : {saved} ({saved_pct:.1f}%)  · Est. credit cost saved: {est_cost_saved_pct:.1f}%\n"
        "  │\n"
        f"  │  Credit cost     all-frontier  {baseline_bar}  100.0%\n"
        f"  │                  with router   {router_bar}  {router_cost_pct:5.1f}%   ▼ {est_cost_saved_pct:.1f}% saved\n"
        "  │\n"
        f"  │  ├── Routed to lite (Tier 1) : {lite:2d} ({lite_pct:.1f}%) {lite_bar} [flash_lite/haiku/luna 0.04x]\n"
        f"  │  ├── Routed to fast (Tier 2) : {fast:2d} ({fast_pct:.1f}%) {fast_bar} [flash/sonnet/sol · 0.20x]\n"
        f"  │  └── Kept on frontier        : {frontier:2d} ({frontier_pct:.1f}%) {frontier_bar} [guardrail/stateful · 1.00x]\n"
        "  │\n"
        f"  │  Hosts & guardrails          : {host_line}\n"
        f"  │  Top routing reasons         : {top_reasons}\n"
        "  ╰─────────────────────────────────────────────────────────────────────────────────────\n"
    )


def render_html_widget(summary: dict[str, Any], ledger_path: Path) -> str:
    """Render a compact, theme-aware HTML widget card for Google Antigravity generative_ui."""
    total = int(summary["total"])
    saved = int(summary["saved"])
    saved_pct = float(summary["saved_pct"])
    est_cost_saved_pct = float(summary.get("est_cost_saved_pct", 0.0))
    router_cost_pct = round(max(0.0, 100.0 - est_cost_saved_pct), 1)
    active_count = int(summary.get("active_count", total))
    shadow_count = int(summary.get("shadow_count", 0))
    total_prompt_chars = int(summary.get("total_prompt_chars", 0))
    tiers = summary["tiers"]
    lite = int(tiers.get("lite", 0))
    fast = int(tiers.get("fast", 0))
    frontier = int(tiers.get("frontier", 0))
    lite_pct = round((lite / total) * 100.0, 1) if total > 0 else 0.0
    fast_pct = round((fast / total) * 100.0, 1) if total > 0 else 0.0
    frontier_pct = round((frontier / total) * 100.0, 1) if total > 0 else 0.0
    hosts = summary.get("hosts") or {}
    host_summary = ", ".join(f"{h}: {c}" for h, c in sorted(hosts.items())) or "none"
    guardrail_count = int(summary.get("guardrail_count", 0))
    esc_path = html.escape(str(ledger_path))
    esc_hosts = html.escape(host_summary)
    esc_reasons = html.escape(_format_top_reasons(summary.get("reasons") or {}))

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
</head>
<body class="bg-transparent text-[var(--foreground)] antialiased p-3">
  <div class="bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] rounded-xl p-4 shadow-sm space-y-3">
    <div class="flex items-center justify-between border-b border-[var(--border)] pb-2.5">
      <div class="flex items-center gap-2">
        <span class="inline-flex items-center justify-center w-6 h-6 rounded-md bg-emerald-500/15 text-emerald-400 font-bold text-xs">⚡</span>
        <div>
          <h2 class="text-sm font-semibold tracking-tight">Downshift Scoreboard 2.0</h2>
          <p class="text-xs text-[var(--muted-foreground)] font-mono truncate max-w-md">{esc_path}</p>
        </div>
      </div>
      <div class="text-right">
        <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/15 text-emerald-400">
          ▼ {est_cost_saved_pct:.1f}% credit cost saved
        </span>
      </div>
    </div>

    <div class="grid grid-cols-3 gap-2.5">
      <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-2.5">
        <div class="text-xs text-[var(--muted-foreground)]">Subagents Intercepted</div>
        <div class="text-lg font-bold mt-0.5">{total}</div>
        <div class="text-xs text-[var(--muted-foreground)]">{active_count} active · {shadow_count} shadow · {total_prompt_chars:,} chars</div>
      </div>
      <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-2.5">
        <div class="text-xs text-[var(--muted-foreground)]">Frontier Calls Saved</div>
        <div class="text-lg font-bold text-emerald-400 mt-0.5">{saved} <span class="text-xs font-normal">({saved_pct:.1f}%)</span></div>
        <div class="text-xs text-[var(--muted-foreground)]">Weighted cost: {router_cost_pct:.1f}% vs 100%</div>
      </div>
      <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-2.5">
        <div class="text-xs text-[var(--muted-foreground)]">Guardrail Protections</div>
        <div class="text-lg font-bold text-amber-400 mt-0.5">{guardrail_count}</div>
        <div class="text-xs text-[var(--muted-foreground)] truncate">Hosts: {esc_hosts}</div>
      </div>
    </div>

    <div class="space-y-1.5 pt-1">
      <div class="flex items-center justify-between text-xs">
        <span class="font-medium">Tier 1 · Lite <span class="text-[var(--muted-foreground)] font-mono">(flash_lite / haiku / gpt-6-luna · 0.04x)</span></span>
        <span class="font-mono font-semibold text-emerald-400">{lite} ({lite_pct:.1f}%)</span>
      </div>
      <div class="w-full h-2 rounded-full bg-[var(--background)] overflow-hidden border border-[var(--border)]">
        <div class="h-full bg-emerald-400 rounded-full" style="width: {lite_pct:.1f}%"></div>
      </div>

      <div class="flex items-center justify-between text-xs pt-1">
        <span class="font-medium">Tier 2 · Fast <span class="text-[var(--muted-foreground)] font-mono">(flash / sonnet / gpt-6.1-sol · 0.20x)</span></span>
        <span class="font-mono font-semibold text-sky-400">{fast} ({fast_pct:.1f}%)</span>
      </div>
      <div class="w-full h-2 rounded-full bg-[var(--background)] overflow-hidden border border-[var(--border)]">
        <div class="h-full bg-sky-400 rounded-full" style="width: {fast_pct:.1f}%"></div>
      </div>

      <div class="flex items-center justify-between text-xs pt-1">
        <span class="font-medium">Tier 3 · Frontier <span class="text-[var(--muted-foreground)] font-mono">(safety guardrail / stateful mutation · 1.00x)</span></span>
        <span class="font-mono font-semibold text-amber-400">{frontier} ({frontier_pct:.1f}%)</span>
      </div>
      <div class="w-full h-2 rounded-full bg-[var(--background)] overflow-hidden border border-[var(--border)]">
        <div class="h-full bg-amber-400 rounded-full" style="width: {frontier_pct:.1f}%"></div>
      </div>
    </div>

    <div class="pt-2 border-t border-[var(--border)] flex items-center justify-between gap-3 text-xs text-[var(--muted-foreground)]">
      <span class="shrink-0 font-medium">Top routing reasons</span>
      <span class="font-mono truncate" title="{esc_reasons}">{esc_reasons}</span>
    </div>
  </div>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Downshift routing telemetry scoreboard"
    )
    parser.add_argument(
        "--ledger", type=str, default=None, help="Path to downshift_ledger.jsonl"
    )
    parser.add_argument("--json", action="store_true", help="Output summary as JSON")
    parser.add_argument(
        "--html",
        type=str,
        default=None,
        help="Write an interactive Antigravity HTML scoreboard widget to the given path",
    )
    parser.add_argument(
        "--reset", action="store_true", help="Truncate/reset the ledger file"
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove synthetic empty and corrupted rows from the ledger",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Only aggregate the last N events"
    )
    args = parser.parse_args(argv)

    ledger_path = get_ledger_path(args.ledger)

    if args.reset:
        try:
            reset_ledger_file(ledger_path)
            sys.stdout.write(f"Reset Downshift ledger at {ledger_path}\n")
            return 0
        except OSError as exc:
            sys.stderr.write(f"Failed to reset ledger: {exc}\n")
            return 1

    if args.clean:
        try:
            removed, retained = clean_ledger_file(ledger_path)
            sys.stdout.write(
                f"Cleaned {removed} synthetic/corrupted event(s) from {ledger_path} ({retained} valid event(s) retained)\n"
            )
            return 0
        except OSError as exc:
            sys.stderr.write(f"Failed to clean ledger: {exc}\n")
            return 1

    events = load_events(ledger_path, limit=args.limit)
    summary = compute_summary(events)
    if args.html:
        out_path = Path(args.html).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_html_widget(summary, ledger_path), encoding="utf-8")
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    else:
        sys.stdout.write(render_ascii_scoreboard(summary, ledger_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
