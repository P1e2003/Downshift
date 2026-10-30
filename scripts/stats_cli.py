"""Scoreboard CLI for Downshift local JSONL telemetry ledger (US-1.2 / Issue #5)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from downshift_core import get_ledger_path


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
    """Aggregate total subagents intercepted, frontier calls saved, and tier counts."""
    total = len(events)
    tiers = {"lite": 0, "fast": 0, "frontier": 0}
    models: dict[str, int] = {}
    saved = 0

    for ev in events:
        tier = str(ev.get("tier") or "frontier")
        if tier not in tiers:
            tier = "frontier"
        tiers[tier] += 1
        model = str(ev.get("target_model") or "inherit")
        models[model] = models.get(model, 0) + 1
        if tier in ("lite", "fast") and bool(ev.get("overwritten", True)):
            saved += 1

    saved_pct = round((saved / total) * 100.0, 1) if total > 0 else 0.0
    return {
        "total": total,
        "saved": saved,
        "saved_pct": saved_pct,
        "tiers": tiers,
        "models": models,
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
    tiers = summary["tiers"]
    lite = int(tiers.get("lite", 0))
    fast = int(tiers.get("fast", 0))
    frontier = int(tiers.get("frontier", 0))

    lite_pct = round((lite / total) * 100.0, 1)
    fast_pct = round((fast / total) * 100.0, 1)
    frontier_pct = round((frontier / total) * 100.0, 1)

    return (
        f"  downshift stats                   live ledger · {ledger_path}\n\n"
        f"  Total subagents intercepted : {total}\n"
        f"  Frontier calls saved        : {saved} ({saved_pct:.1f}%)\n"
        f"  ├── Routed to lite (Tier 1) : {lite:2d} ({lite_pct:.1f}%)  [flash_lite / haiku]\n"
        f"  ├── Routed to fast (Tier 2) : {fast:2d} ({fast_pct:.1f}%)  [flash / sonnet]\n"
        f"  └── Kept on frontier        : {frontier:2d} ({frontier_pct:.1f}%)  [safety guardrail / stateful mutation]\n"
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
        "--limit", type=int, default=None, help="Only aggregate the last N events"
    )
    args = parser.parse_args(argv)

    ledger_path = get_ledger_path(args.ledger)

    if args.reset:
        try:
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_path.write_text("", encoding="utf-8")
            sys.stdout.write(f"Reset Downshift ledger at {ledger_path}\n")
            return 0
        except OSError as exc:
            sys.stderr.write(f"Failed to reset ledger: {exc}\n")
            return 1

    events = load_events(ledger_path, limit=args.limit)
    summary = compute_summary(events)
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    else:
        sys.stdout.write(render_ascii_scoreboard(summary, ledger_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
