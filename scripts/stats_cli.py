"""Scoreboard CLI for Downshift local JSONL telemetry ledger (US-1.2 / Issue #5 & US-2.9 / Issue #23)."""

from __future__ import annotations

import argparse
import contextlib
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
    """Render compact ASCII scoreboard for terminal or chat display."""
    total = int(summary["total"])
    if total == 0:
        return (
            f"  downshift stats                   live ledger · {ledger_path}\n\n"
            "  0 subagents intercepted yet.\n"
        )

    saved = int(summary["saved"])
    saved_pct = float(summary["saved_pct"])
    est_cost_saved_pct = float(summary.get("est_cost_saved_pct", 0.0))
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

    lite_pct = round((lite / total) * 100.0, 1)
    fast_pct = round((fast / total) * 100.0, 1)
    frontier_pct = round((frontier / total) * 100.0, 1)

    return (
        f"  downshift stats                   live ledger · {ledger_path}\n\n"
        f"  Total subagents intercepted : {total}  ({active_count} active, {shadow_count} shadow · {total_prompt_chars:,} prompt chars)\n"
        f"  Frontier calls saved        : {saved} ({saved_pct:.1f}%)  · Est. credit cost saved: {est_cost_saved_pct:.1f}%\n"
        f"  ├── Routed to lite (Tier 1) : {lite:2d} ({lite_pct:.1f}%)  [flash_lite / haiku / gpt-6-luna · 0.04x cost]\n"
        f"  ├── Routed to fast (Tier 2) : {fast:2d} ({fast_pct:.1f}%)  [flash / sonnet / gpt-6.1-sol · 0.20x cost]\n"
        f"  └── Kept on frontier        : {frontier:2d} ({frontier_pct:.1f}%)  [safety guardrail / stateful mutation · 1.00x]\n"
        f"  Hosts & guardrails          : {host_summary} · {guardrail_count} high-risk call(s) protected by guardrails\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Downshift routing telemetry scoreboard"
    )
    parser.add_argument(
        "--ledger", type=str, default=None, help="Path to downshift_ledger.jsonl"
    )
    parser.add_argument("--json", action="store_true", help="Output summary as JSON")
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
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    else:
        sys.stdout.write(render_ascii_scoreboard(summary, ledger_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
