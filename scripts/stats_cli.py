"""Scoreboard CLI for Downshift local JSONL telemetry ledger (US-1.2 / Issue #5 & US-2.9 / Issue #23)."""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import sys
from pathlib import Path
from typing import Any

from downshift_core import (
    READ_ONLY_ROLE_RE,
    READ_ONLY_SUBAGENTS,
    RoutingTrace,
    clean_ledger_file,
    explain_subagent_call,
    get_ledger_path,
    reset_ledger_file,
)

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
    """Aggregate total subagents intercepted, frontier calls saved, cost ROI, rework, and mode/host/reason counts."""
    route_events = [
        ev
        for ev in events
        if str(ev.get("event_type") or "route").strip().lower() != "outcome"
    ]
    outcome_events = [
        ev
        for ev in events
        if str(ev.get("event_type") or "route").strip().lower() == "outcome"
    ]
    total = len(route_events)
    tiers = {"lite": 0, "fast": 0, "frontier": 0}
    models: dict[str, int] = {}
    hosts: dict[str, int] = {}
    tools: dict[str, int] = {}
    reasons: dict[str, int] = {}
    saved = 0
    overwritten_calls = 0
    active_count = 0
    shadow_count = 0
    guardrail_count = 0
    workspace_isolated_count = 0
    upshift_count = 0
    circuit_breaker_count = 0
    total_prompt_chars = 0
    total_workers = 0
    actual_weighted_cost = 0.0

    # Pre-tally failed lower-tier outcome costs and deduplication credits (Q3 -> A).
    error_outcome_count = 0
    wasted_lower_tier_cost = 0.0
    unconsumed_error_credits: dict[tuple[str, str], int] = {}
    for ev in outcome_events:
        if ev.get("failed") is not True:
            continue
        routed_tier = str(ev.get("routed_tier") or "").strip().lower()
        if routed_tier == "frontier":
            continue
        error_outcome_count += 1
        if routed_tier in ("lite", "fast"):
            wasted_lower_tier_cost += TIER_COST_WEIGHT[routed_tier]
            key = (
                str(ev.get("conversation_id") or "").strip(),
                str(ev.get("role_fp") or "").strip(),
            )
            unconsumed_error_credits[key] = unconsumed_error_credits.get(key, 0) + 1

    for ev in route_events:
        tier = str(ev.get("tier") or "frontier")
        if tier not in tiers:
            tier = "frontier"
        tiers[tier] += 1
        actual_weighted_cost += TIER_COST_WEIGHT.get(tier, 1.00)
        model = str(ev.get("target_model") or "inherit")
        models[model] = models.get(model, 0) + 1

        host = str(ev.get("host") or "antigravity").strip().lower() or "antigravity"
        hosts[host] = hosts.get(host, 0) + 1

        default_tool = (
            "spawn_agent"
            if host == "codex"
            else ("Task" if host == "claude" else "invoke_subagent")
        )
        tool_name = str(ev.get("tool") or "").strip() or default_tool
        tools[tool_name] = tools.get(tool_name, 0) + 1
        try:
            total_workers += max(1, int(ev.get("spawn_count") or 1))
        except (ValueError, TypeError):
            total_workers += 1

        reason = str(ev.get("reason") or "unknown").strip() or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
        is_ws_isolated = ev.get("workspace_isolated") is True
        if is_ws_isolated:
            workspace_isolated_count += 1
        if (
            reason.startswith("safety_guardrail:")
            or reason == "workspace_config:high_risk_keyword"
            or (reason == "workspace_config:pinned_role" and tier == "frontier")
            or is_ws_isolated
        ):
            guardrail_count += 1
        if reason in ("auto_upshift_on_retry", "circuit_breaker_frontier_lock"):
            upshift_count += 1
            if reason == "circuit_breaker_frontier_lock":
                circuit_breaker_count += 1
            up_from = str(ev.get("upshifted_from") or "").strip().lower()
            if up_from in ("lite", "fast"):
                key = (
                    str(ev.get("conversation_id") or "").strip(),
                    str(ev.get("role_fp") or "").strip(),
                )
                if unconsumed_error_credits.get(key, 0) > 0:
                    unconsumed_error_credits[key] -= 1
                else:
                    wasted_lower_tier_cost += TIER_COST_WEIGHT[up_from]

        mode = str(ev.get("mode") or "active").strip().lower()
        if mode == "shadow":
            shadow_count += 1
        else:
            active_count += 1

        is_overwritten = bool(ev.get("overwritten", True))
        if is_overwritten:
            overwritten_calls += 1
        is_downshift_delegated = is_overwritten or (
            mode != "shadow"
            and reason.startswith(("inline_override:", "workspace_config:"))
        )
        if tier in ("lite", "fast") and is_downshift_delegated:
            saved += 1

        raw_chars = ev.get("prompt_chars", 0)
        try:
            total_prompt_chars += max(0, int(raw_chars or 0))
        except (ValueError, TypeError):
            pass

    saved_pct = round((saved / total) * 100.0, 1) if total > 0 else 0.0
    est_cost_saved_pct = (
        round((1.0 - (actual_weighted_cost / total)) * 100.0, 1) if total > 0 else 0.0
    )
    downshifted_total = tiers["lite"] + tiers["fast"]
    rework_rate_pct = (
        min(100.0, round((upshift_count / downshifted_total) * 100.0, 1))
        if downshifted_total > 0
        else 0.0
    )
    if total <= 0:
        net_cost_saved_pct = 0.0
    elif wasted_lower_tier_cost <= 0.0:
        net_cost_saved_pct = est_cost_saved_pct
    else:
        net_saved_units = max(
            0.0, (float(total) - actual_weighted_cost) - wasted_lower_tier_cost
        )
        net_cost_saved_pct = round((net_saved_units / total) * 100.0, 1)

    return {
        "total": total,
        "saved": saved,
        "overwritten_calls": overwritten_calls,
        "saved_pct": saved_pct,
        "est_cost_saved_pct": est_cost_saved_pct,
        "net_cost_saved_pct": net_cost_saved_pct,
        "active_count": active_count,
        "shadow_count": shadow_count,
        "total_prompt_chars": total_prompt_chars,
        "total_workers": total_workers,
        "outcome_count": len(outcome_events),
        "upshift_count": upshift_count,
        "circuit_breaker_count": circuit_breaker_count,
        "error_outcome_count": error_outcome_count,
        "rework_rate_pct": rework_rate_pct,
        "tiers": tiers,
        "models": models,
        "hosts": hosts,
        "tools": tools,
        "reasons": reasons,
        "guardrail_count": guardrail_count,
        "workspace_isolated_count": workspace_isolated_count,
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
    tools = summary.get("tools") or {}
    has_extra_tools = any(k in tools for k in ("swarm", "run_workflow"))
    tool_line = (
        f"  │  Orchestrators               : {_truncate_cell(', '.join(f'{t}: {c}' for t, c in sorted(tools.items())))}\n"
        if has_extra_tools
        else ""
    )
    host_line = _truncate_cell(f"{host_summary} · {guardrail_count} guardrail(s)")
    top_reasons = _truncate_cell(_format_top_reasons(summary.get("reasons") or {}))
    upshift_count = int(summary.get("upshift_count", 0))
    error_outcome_count = int(summary.get("error_outcome_count", 0))
    rework_rate_pct = float(summary.get("rework_rate_pct", 0.0))
    net_cost_saved_pct = float(summary.get("net_cost_saved_pct", est_cost_saved_pct))
    rework_line = (
        f"  │  Quality & rework            : {_truncate_cell(f'{upshift_count} upshift(s) · {error_outcome_count} err · {rework_rate_pct:.1f}% rework · Net {net_cost_saved_pct:.1f}%')}\n"
        if (upshift_count > 0 or error_outcome_count > 0)
        else ""
    )

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
        f"{tool_line}"
        f"{rework_line}"
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
    tools = summary.get("tools") or {}
    if any(k in tools for k in ("swarm", "run_workflow")):
        tool_summary = ", ".join(f"{t}: {c}" for t, c in sorted(tools.items()))
        host_summary = f"{host_summary} · {tool_summary}"
    guardrail_count = int(summary.get("guardrail_count", 0))
    upshift_count = int(summary.get("upshift_count", 0))
    error_outcome_count = int(summary.get("error_outcome_count", 0))
    rework_rate_pct = float(summary.get("rework_rate_pct", 0.0))
    net_cost_saved_pct = float(summary.get("net_cost_saved_pct", est_cost_saved_pct))
    esc_path = html.escape(str(ledger_path))
    esc_hosts = html.escape(host_summary)
    esc_reasons = html.escape(_format_top_reasons(summary.get("reasons") or {}))
    rework_html = ""
    if upshift_count > 0 or error_outcome_count > 0:
        esc_rework = html.escape(
            f"{upshift_count} upshift(s) · {error_outcome_count} err · {rework_rate_pct:.1f}% rework · Net {net_cost_saved_pct:.1f}% saved"
        )
        rework_html = (
            '\n    <div class="pt-1.5 border-t border-[var(--border)] flex items-center justify-between gap-3 text-xs text-[var(--muted-foreground)]">\n'
            '      <span class="shrink-0 font-medium">Quality &amp; rework</span>\n'
            f'      <span class="font-mono truncate" title="{esc_rework}">{esc_rework}</span>\n'
            "    </div>"
        )

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
    </div>{rework_html}
  </div>
</body>
</html>
"""


def compute_ledger_audit(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute a strictly read-only guardrail hotspot and calibration candidate summary."""
    route_events = [
        ev
        for ev in events
        if str(ev.get("event_type") or "route").strip().lower() != "outcome"
    ]
    outcome_events = [
        ev
        for ev in events
        if str(ev.get("event_type") or "route").strip().lower() == "outcome"
    ]
    host_tiers: dict[str, dict[str, int]] = {}
    guardrail_counts: dict[str, int] = {}
    candidate_counts: dict[tuple[str, str, str], int] = {}
    burst_cap_hits = 0
    upshift_hits = 0
    circuit_breaker_hits = 0
    workspace_isolated_hits = 0

    for ev in route_events:
        host = str(ev.get("host") or "antigravity").strip().lower() or "antigravity"
        tier = str(ev.get("tier") or "frontier").strip().lower()
        if tier not in ("lite", "fast", "frontier"):
            tier = "frontier"
        bucket = host_tiers.setdefault(
            host, {"total": 0, "lite": 0, "fast": 0, "frontier": 0}
        )
        bucket["total"] += 1
        bucket[tier] += 1

        reason = str(ev.get("reason") or "unknown").strip() or "unknown"
        if reason == "burst_cap_downgraded_to_lite":
            burst_cap_hits += 1
            guardrail_counts[reason] = guardrail_counts.get(reason, 0) + 1
        if reason in ("auto_upshift_on_retry", "circuit_breaker_frontier_lock"):
            upshift_hits += 1
            if reason == "circuit_breaker_frontier_lock":
                circuit_breaker_hits += 1
            guardrail_counts[reason] = guardrail_counts.get(reason, 0) + 1
        if (
            reason.startswith("safety_guardrail:")
            or reason == "workspace_config:high_risk_keyword"
            or (reason == "workspace_config:pinned_role" and tier == "frontier")
        ):
            guardrail_counts[reason] = guardrail_counts.get(reason, 0) + 1
        if ev.get("workspace_isolated") is True:
            workspace_isolated_hits += 1
            ws_reason = (
                str(ev.get("workspace_guardrail_reason") or "").strip()
                or "parallel_write_isolated"
            )
            guardrail_counts[ws_reason] = guardrail_counts.get(ws_reason, 0) + 1

        if tier == "frontier":
            sub_type = str(ev.get("subagent_type") or "").strip().lower()
            role_text = str(ev.get("role") or "").strip()
            is_readonly_like = sub_type in READ_ONLY_SUBAGENTS or bool(
                READ_ONLY_ROLE_RE.search(role_text)
            )
            if is_readonly_like and reason != "inline_override:frontier":
                key = (role_text or sub_type or "(unnamed)", reason, host)
                candidate_counts[key] = candidate_counts.get(key, 0) + 1

    host_balance: dict[str, dict[str, Any]] = {}
    for host, counts in sorted(host_tiers.items()):
        total_h = counts["total"]
        host_balance[host] = {
            **counts,
            "lite_pct": round((counts["lite"] / total_h) * 100.0, 1)
            if total_h > 0
            else 0.0,
            "fast_pct": round((counts["fast"] / total_h) * 100.0, 1)
            if total_h > 0
            else 0.0,
            "frontier_pct": round((counts["frontier"] / total_h) * 100.0, 1)
            if total_h > 0
            else 0.0,
        }

    guardrail_hotspots = [
        {"reason": r, "count": c}
        for r, c in sorted(guardrail_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    calibration_candidates = [
        {"role": role, "reason": reason, "host": host, "count": c}
        for (role, reason, host), c in sorted(
            candidate_counts.items(),
            key=lambda kv: (-kv[1], kv[0][0], kv[0][1], kv[0][2]),
        )
    ]

    return {
        "total_route_events": len(route_events),
        "total_outcome_events": len(outcome_events),
        "burst_cap_hits": burst_cap_hits,
        "upshift_hits": upshift_hits,
        "circuit_breaker_hits": circuit_breaker_hits,
        "workspace_isolated_hits": workspace_isolated_hits,
        "host_tier_balance": host_balance,
        "guardrail_hotspots": guardrail_hotspots,
        "calibration_candidates": calibration_candidates,
    }


def render_ascii_explain(trace: RoutingTrace, host_filter: str = "all") -> str:
    """Render a compact <=88-col dry-run routing trace card."""
    d = trace.decision
    selected_hosts = (
        ("antigravity", "claude", "codex")
        if host_filter == "all"
        else (host_filter if host_filter in trace.hosts else "antigravity",)
    )
    tags_str = ", ".join(trace.matched_tags) if trace.matched_tags else "none"
    masked_str = ", ".join(trace.masked_tokens) if trace.masked_tokens else "none"
    guardrail_str = (
        f"{trace.matched_guardrail_label} ({trace.matched_guardrail_text!r})"
        if trace.matched_guardrail_label
        else "none"
    )
    flags_active = [
        name
        for name, enabled in (
            ("readonly_subagent", trace.is_readonly_subagent),
            ("readonly_role", trace.is_readonly_role),
            ("simple_lookup", trace.is_simple_lookup),
            ("verification", trace.is_verification_runner),
            ("code_mutation", trace.is_code_mutation),
            ("write_capable", trace.is_write_capable),
        )
        if enabled
    ]
    flags_str = ", ".join(flags_active) if flags_active else "none"
    complexity_str = (
        f"{trace.prompt_chars} chars · {trace.path_count} paths · {trace.line_count} lines"
        f"{' · fence' if trace.has_code_fence else ''}"
        f"{' · trace' if trace.has_stack_trace else ''}"
    )

    lines = [
        "  ╭─ ⚡ downshift explain ─────────────── dry-run trace · zero ledger I/O",
        "  │",
        f"  │  Config source               : {_truncate_cell(trace.config_source)}",
        f"  │  Effective mode & burst      : {_truncate_cell(f'{trace.effective_mode} · max_burst={trace.max_burst_subagents}')}",
        f"  │  Winning stage               : {_truncate_cell(f'Stage {trace.winning_stage} ({trace.winning_stage_name})')}",
        f"  │  Routing reason              : {_truncate_cell(d.reason)}",
        f"  │  Inline @tags                : {_truncate_cell(tags_str)}",
        f"  │  Guardrail match             : {_truncate_cell(guardrail_str)}",
    ]
    if trace.matched_pinned_role:
        lines.append(
            f"  │  Pinned role                 : {_truncate_cell(trace.matched_pinned_role)}"
        )
    lines.extend(
        [
            f"  │  Masked tokens               : {_truncate_cell(masked_str)}",
            f"  │  Complexity                  : {_truncate_cell(complexity_str)}",
            f"  │  Classifier flags            : {_truncate_cell(flags_str)}",
            "  │",
        ]
    )
    for idx, h in enumerate(selected_hosts):
        entry = trace.hosts[h]
        branch = "└──" if idx == len(selected_hosts) - 1 else "├──"
        effort_part = (
            f" · effort={entry['reasoning_effort']}"
            if entry.get("reasoning_effort")
            else ""
        )
        action = "overwrite" if entry.get("should_overwrite") else "keep"
        val = f"{entry['tier']} -> {entry['target_model']}{effort_part} ({action})"
        lines.append(f"  │  {branch} {h:<24}: {_truncate_cell(val)}")
    lines.append(
        "  ╰─────────────────────────────────────────────────────────────────────────────────────"
    )
    return "\n".join(lines) + "\n"


def render_ascii_audit(audit: dict[str, Any], ledger_path: Path) -> str:
    """Render a compact <=88-col read-only ledger guardrail & calibration audit card."""
    total = int(audit["total_route_events"])
    if total == 0:
        return (
            f"  ╭─ ⚡ downshift audit ──────────────── read-only · {ledger_path}\n"
            "  │  0 routing events recorded yet.\n"
            "  ╰─────────────────────────────────────────────────────────────────────────────────────\n"
        )

    outcome_total = int(audit["total_outcome_events"])
    burst_hits = int(audit["burst_cap_hits"])
    upshift_hits = int(audit["upshift_hits"])
    breaker_hits = int(audit["circuit_breaker_hits"])
    isolated_hits = int(audit["workspace_isolated_hits"])
    events_val = f"{total} route · {outcome_total} outcome"
    guardrail_val = (
        f"burst={burst_hits} · upshift={upshift_hits} "
        f"(breaker={breaker_hits}) · isolated={isolated_hits}"
    )

    lines = [
        f"  ╭─ ⚡ downshift audit ──────────────── read-only · {ledger_path}",
        "  │",
        f"  │  Events audited              : {_truncate_cell(events_val)}",
        f"  │  Guardrail counters          : {_truncate_cell(guardrail_val)}",
        "  │",
    ]
    host_balance = audit.get("host_tier_balance") or {}
    host_items = list(host_balance.items())
    for idx, (h, b) in enumerate(host_items):
        branch = "└──" if idx == len(host_items) - 1 else "├──"
        val = (
            f"{b['total']} · L:{b['lite']} ({b['lite_pct']:.1f}%) "
            f"F:{b['fast']} ({b['fast_pct']:.1f}%) T3:{b['frontier']} ({b['frontier_pct']:.1f}%)"
        )
        lines.append(f"  │  {branch} {h:<24}: {_truncate_cell(val)}")

    hotspots = audit.get("guardrail_hotspots") or []
    hotspot_str = (
        ", ".join(f"{item['reason']}: {item['count']}" for item in hotspots[:4])
        or "none"
    )
    lines.extend(
        [
            "  │",
            f"  │  Top guardrail hotspots      : {_truncate_cell(hotspot_str)}",
        ]
    )
    candidates = audit.get("calibration_candidates") or []
    if not candidates:
        lines.append("  │  Calibration candidates      : none")
    else:
        for idx, cand in enumerate(candidates[:5]):
            label = "Calibration candidates" if idx == 0 else ""
            val = (
                f"[{cand['count']}x {cand['host']}] {cand['role']} -> {cand['reason']}"
            )
            lines.append(f"  │  {label:<27} : {_truncate_cell(val)}")
    lines.append(
        "  ╰─────────────────────────────────────────────────────────────────────────────────────"
    )
    return "\n".join(lines) + "\n"


def _tier_badge_classes(tier: str) -> str:
    if tier == "lite":
        return "bg-emerald-500/15 text-emerald-400"
    if tier == "fast":
        return "bg-sky-500/15 text-sky-400"
    return "bg-amber-500/15 text-amber-400"


def _tier_label(tier: str) -> str:
    if tier == "lite":
        return "Tier 1 · Lite"
    if tier == "fast":
        return "Tier 2 · Fast"
    return "Tier 3 · Frontier"


def render_html_explain(trace: RoutingTrace, host_filter: str = "all") -> str:
    """Render a compact, theme-aware Antigravity HTML card for a dry-run explain trace."""
    d = trace.decision
    selected_hosts = (
        ("antigravity", "claude", "codex")
        if host_filter == "all"
        else (host_filter if host_filter in trace.hosts else "antigravity",)
    )
    tier_cls = _tier_badge_classes(d.tier)
    tier_text = _tier_label(d.tier)

    esc_cfg = html.escape(trace.config_source, quote=True)
    esc_mode = html.escape(trace.effective_mode, quote=True)
    esc_stage = html.escape(trace.winning_stage_name, quote=True)
    esc_reason = html.escape(d.reason, quote=True)

    def _chips(items: list[str], chip_cls: str) -> str:
        if not items:
            return '<span class="text-xs text-[var(--muted-foreground)] font-mono">none</span>'
        return " ".join(
            f'<span class="inline-flex items-center px-2 py-0.5 rounded-md text-xs font-mono {chip_cls}">{html.escape(item, quote=True)}</span>'
            for item in items
        )

    tags_html = _chips(list(trace.matched_tags), "bg-sky-500/15 text-sky-400")
    masked_html = _chips(
        list(trace.masked_tokens),
        "bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)]",
    )
    if trace.matched_guardrail_label:
        guard_text = (
            f"{trace.matched_guardrail_label} ({trace.matched_guardrail_text!r})"
        )
        guardrail_html = _chips([guard_text], "bg-amber-500/15 text-amber-400")
    else:
        guardrail_html = (
            '<span class="text-xs text-[var(--muted-foreground)] font-mono">none</span>'
        )

    pinned_row_html = ""
    if trace.matched_pinned_role:
        pinned_chip = _chips([trace.matched_pinned_role], "bg-sky-500/15 text-sky-400")
        pinned_row_html = (
            '\n      <div class="flex items-start justify-between gap-3 py-1.5 border-t border-[var(--border)]">\n'
            '        <span class="text-xs text-[var(--muted-foreground)] shrink-0">Pinned role</span>\n'
            f'        <div class="flex flex-wrap justify-end gap-1.5">{pinned_chip}</div>\n'
            "      </div>"
        )

    flags_active = [
        name
        for name, enabled in (
            ("readonly_subagent", trace.is_readonly_subagent),
            ("readonly_role", trace.is_readonly_role),
            ("simple_lookup", trace.is_simple_lookup),
            ("verification", trace.is_verification_runner),
            ("code_mutation", trace.is_code_mutation),
            ("write_capable", trace.is_write_capable),
        )
        if enabled
    ]
    flags_html = _chips(
        flags_active,
        "bg-[var(--background)] border border-[var(--border)] text-[var(--muted-foreground)]",
    )

    complexity_extras = [
        lbl
        for lbl, active in (
            ("code fence", trace.has_code_fence),
            ("stack trace", trace.has_stack_trace),
        )
        if active
    ]
    complexity_sub = (
        " · ".join(complexity_extras) if complexity_extras else "plain prompt"
    )

    host_rows: list[str] = []
    for h in selected_hosts:
        entry = trace.hosts[h]
        h_tier = str(entry.get("tier") or "frontier")
        h_cls = _tier_badge_classes(h_tier)
        esc_h = html.escape(h, quote=True)
        esc_model = html.escape(str(entry.get("target_model") or "inherit"), quote=True)
        effort_val = entry.get("reasoning_effort")
        esc_effort = html.escape(str(effort_val), quote=True) if effort_val else "—"
        action = "overwrite" if entry.get("should_overwrite") else "keep"
        action_cls = (
            "bg-emerald-500/15 text-emerald-400"
            if action == "overwrite"
            else "bg-[var(--background)] border border-[var(--border)] text-[var(--muted-foreground)]"
        )
        host_rows.append(
            '        <tr class="border-t border-[var(--border)]">\n'
            f'          <td class="py-1.5 pr-3 font-medium">{esc_h}</td>\n'
            f'          <td class="py-1.5 pr-3"><span class="inline-flex px-2 py-0.5 rounded-full text-xs font-semibold {h_cls}">{html.escape(h_tier, quote=True)}</span></td>\n'
            f'          <td class="py-1.5 pr-3 font-mono">{esc_model}</td>\n'
            f'          <td class="py-1.5 pr-3 font-mono text-[var(--muted-foreground)]">{esc_effort}</td>\n'
            f'          <td class="py-1.5 text-right"><span class="inline-flex px-2 py-0.5 rounded-md text-xs font-mono {action_cls}">{action}</span></td>\n'
            "        </tr>"
        )
    host_rows_html = "\n".join(host_rows)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
</head>
<body class="bg-transparent text-[var(--foreground)] antialiased p-3">
  <div class="bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] rounded-xl p-4 shadow-sm space-y-3">
    <div class="flex items-center justify-between gap-3 border-b border-[var(--border)] pb-2.5">
      <div class="flex items-center gap-2 min-w-0">
        <span class="inline-flex items-center justify-center w-6 h-6 rounded-md bg-sky-500/15 text-sky-400 font-bold text-xs shrink-0">⚡</span>
        <div class="min-w-0">
          <h2 class="text-sm font-semibold tracking-tight">Downshift Explain Trace</h2>
          <p class="text-xs text-[var(--muted-foreground)] font-mono truncate" title="{esc_cfg}">dry-run · zero ledger I/O · config: {esc_cfg}</p>
        </div>
      </div>
      <span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold shrink-0 {tier_cls}">
        {tier_text}
      </span>
    </div>

    <div class="grid grid-cols-3 gap-2.5">
      <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-2.5">
        <div class="text-xs text-[var(--muted-foreground)]">Winning Stage</div>
        <div class="text-base font-bold mt-0.5">Stage {trace.winning_stage}</div>
        <div class="text-xs text-[var(--muted-foreground)] font-mono break-all">{esc_stage}</div>
      </div>
      <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-2.5">
        <div class="text-xs text-[var(--muted-foreground)]">Routing Reason</div>
        <div class="text-xs font-mono font-semibold mt-1 break-all">{esc_reason}</div>
        <div class="text-xs text-[var(--muted-foreground)] mt-0.5">mode: {esc_mode} · burst: {trace.max_burst_subagents}</div>
      </div>
      <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-2.5">
        <div class="text-xs text-[var(--muted-foreground)]">Prompt Complexity</div>
        <div class="text-base font-bold mt-0.5">{trace.prompt_chars:,} <span class="text-xs font-normal text-[var(--muted-foreground)]">chars</span></div>
        <div class="text-xs text-[var(--muted-foreground)]">{trace.path_count} paths · {trace.line_count} lines · {complexity_sub}</div>
      </div>
    </div>

    <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-1">
      <div class="flex items-start justify-between gap-3 py-1.5">
        <span class="text-xs text-[var(--muted-foreground)] shrink-0">Inline @tags</span>
        <div class="flex flex-wrap justify-end gap-1.5">{tags_html}</div>
      </div>
      <div class="flex items-start justify-between gap-3 py-1.5 border-t border-[var(--border)]">
        <span class="text-xs text-[var(--muted-foreground)] shrink-0">Guardrail match</span>
        <div class="flex flex-wrap justify-end gap-1.5">{guardrail_html}</div>
      </div>{pinned_row_html}
      <div class="flex items-start justify-between gap-3 py-1.5 border-t border-[var(--border)]">
        <span class="text-xs text-[var(--muted-foreground)] shrink-0">Masked tokens</span>
        <div class="flex flex-wrap justify-end gap-1.5">{masked_html}</div>
      </div>
      <div class="flex items-start justify-between gap-3 py-1.5 border-t border-[var(--border)]">
        <span class="text-xs text-[var(--muted-foreground)] shrink-0">Classifier flags</span>
        <div class="flex flex-wrap justify-end gap-1.5">{flags_html}</div>
      </div>
    </div>

    <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 py-2">
      <div class="text-xs font-medium text-[var(--muted-foreground)] mb-1.5">Host Routing Matrix</div>
      <table class="w-full text-xs">
        <thead>
          <tr class="text-left text-[var(--muted-foreground)]">
            <th class="pb-1 pr-3 font-medium">Host</th>
            <th class="pb-1 pr-3 font-medium">Tier</th>
            <th class="pb-1 pr-3 font-medium">Target Model</th>
            <th class="pb-1 pr-3 font-medium">Effort</th>
            <th class="pb-1 text-right font-medium">Action</th>
          </tr>
        </thead>
        <tbody>
{host_rows_html}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


def render_html_audit(audit: dict[str, Any], ledger_path: Path) -> str:
    """Render a compact, theme-aware Antigravity HTML card for a read-only ledger audit."""
    total_routes = int(audit["total_route_events"])
    total_outcomes = int(audit["total_outcome_events"])
    burst_hits = int(audit["burst_cap_hits"])
    upshift_hits = int(audit["upshift_hits"])
    breaker_hits = int(audit["circuit_breaker_hits"])
    isolated_hits = int(audit["workspace_isolated_hits"])
    hotspots = audit.get("guardrail_hotspots") or []
    candidates = audit.get("calibration_candidates") or []
    host_balance = audit.get("host_tier_balance") or {}
    esc_path = html.escape(str(ledger_path), quote=True)

    host_bars: list[str] = []
    for h, b in host_balance.items():
        esc_h = html.escape(str(h), quote=True)
        lite_pct = float(b["lite_pct"])
        fast_pct = float(b["fast_pct"])
        frontier_pct = float(b["frontier_pct"])
        host_bars.append(
            '      <div class="space-y-1">\n'
            '        <div class="flex items-center justify-between text-xs gap-2">\n'
            f'          <span class="font-medium">{esc_h} <span class="text-[var(--muted-foreground)] font-mono">({int(b["total"])})</span></span>\n'
            f'          <span class="font-mono text-[var(--muted-foreground)]">Lite {int(b["lite"])} ({lite_pct:.1f}%) · Fast {int(b["fast"])} ({fast_pct:.1f}%) · Frontier {int(b["frontier"])} ({frontier_pct:.1f}%)</span>\n'
            "        </div>\n"
            '        <div class="w-full h-2 rounded-full bg-[var(--background)] overflow-hidden border border-[var(--border)] flex">\n'
            f'          <div class="h-full bg-emerald-400" style="width: {lite_pct:.1f}%"></div>\n'
            f'          <div class="h-full bg-sky-400" style="width: {fast_pct:.1f}%"></div>\n'
            f'          <div class="h-full bg-amber-400" style="width: {frontier_pct:.1f}%"></div>\n'
            "        </div>\n"
            "      </div>"
        )
    host_bars_html = (
        "\n".join(host_bars)
        if host_bars
        else '      <div class="text-xs text-[var(--muted-foreground)]">0 routing events recorded yet.</div>'
    )

    if hotspots:
        hotspot_chips = " ".join(
            f'<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-mono bg-amber-500/15 text-amber-400">'
            f'{html.escape(str(item["reason"]), quote=True)} <span class="font-semibold">×{int(item["count"])}</span></span>'
            for item in hotspots[:8]
        )
    else:
        hotspot_chips = (
            '<span class="text-xs text-[var(--muted-foreground)] font-mono">none</span>'
        )

    if candidates:
        cand_rows: list[str] = []
        for cand in candidates[:10]:
            esc_host = html.escape(str(cand["host"]), quote=True)
            esc_role = html.escape(str(cand["role"]), quote=True)
            esc_reason = html.escape(str(cand["reason"]), quote=True)
            cand_rows.append(
                '        <tr class="border-t border-[var(--border)]">\n'
                f'          <td class="py-1.5 pr-2 font-mono font-semibold text-amber-400">{int(cand["count"])}×</td>\n'
                f'          <td class="py-1.5 pr-2 font-mono text-[var(--muted-foreground)]">{esc_host}</td>\n'
                f'          <td class="py-1.5 pr-2 break-words">{esc_role}</td>\n'
                f'          <td class="py-1.5 font-mono text-[var(--muted-foreground)] break-all">{esc_reason}</td>\n'
                "        </tr>"
            )
        extra_note = (
            f'<div class="text-xs text-[var(--muted-foreground)] pt-1.5 border-t border-[var(--border)]">Showing top 10 of {len(candidates)} candidate groups (run with --json for full list).</div>'
            if len(candidates) > 10
            else ""
        )
        candidates_html = (
            '      <table class="w-full text-xs">\n'
            "        <thead>\n"
            '          <tr class="text-left text-[var(--muted-foreground)]">\n'
            '            <th class="pb-1 pr-2 font-medium">Hits</th>\n'
            '            <th class="pb-1 pr-2 font-medium">Host</th>\n'
            '            <th class="pb-1 pr-2 font-medium">Read-Only Role</th>\n'
            '            <th class="pb-1 font-medium">Guardrail / Reason</th>\n'
            "          </tr>\n"
            "        </thead>\n"
            "        <tbody>\n"
            + "\n".join(cand_rows)
            + "\n        </tbody>\n      </table>"
            + extra_note
        )
    else:
        candidates_html = '<div class="text-xs text-[var(--muted-foreground)]">No read-only calibration candidates detected.</div>'

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
</head>
<body class="bg-transparent text-[var(--foreground)] antialiased p-3">
  <div class="bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] rounded-xl p-4 shadow-sm space-y-3">
    <div class="flex items-center justify-between gap-3 border-b border-[var(--border)] pb-2.5">
      <div class="flex items-center gap-2 min-w-0">
        <span class="inline-flex items-center justify-center w-6 h-6 rounded-md bg-amber-500/15 text-amber-400 font-bold text-xs shrink-0">⚡</span>
        <div class="min-w-0">
          <h2 class="text-sm font-semibold tracking-tight">Downshift Ledger Audit</h2>
          <p class="text-xs text-[var(--muted-foreground)] font-mono truncate" title="{esc_path}">read-only · {esc_path}</p>
        </div>
      </div>
      <span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold shrink-0 bg-sky-500/15 text-sky-400">
        {total_routes} route · {total_outcomes} outcome
      </span>
    </div>

    <div class="grid grid-cols-4 gap-2">
      <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-2.5">
        <div class="text-xs text-[var(--muted-foreground)]">Burst Cap Hits</div>
        <div class="text-lg font-bold mt-0.5">{burst_hits}</div>
      </div>
      <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-2.5">
        <div class="text-xs text-[var(--muted-foreground)]">Auto-Upshifts</div>
        <div class="text-lg font-bold mt-0.5">{upshift_hits} <span class="text-xs font-normal text-[var(--muted-foreground)]">({breaker_hits} breaker)</span></div>
      </div>
      <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-2.5">
        <div class="text-xs text-[var(--muted-foreground)]">Worktree Isolated</div>
        <div class="text-lg font-bold mt-0.5">{isolated_hits}</div>
      </div>
      <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-2.5">
        <div class="text-xs text-[var(--muted-foreground)]">Calibration Targets</div>
        <div class="text-lg font-bold text-amber-400 mt-0.5">{len(candidates)}</div>
      </div>
    </div>

    <div class="space-y-2">
      <div class="text-xs font-medium text-[var(--muted-foreground)]">Per-Host Tier Balance</div>
{host_bars_html}
    </div>

    <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-3 space-y-1.5">
      <div class="text-xs font-medium text-[var(--muted-foreground)]">Guardrail Hotspots</div>
      <div class="flex flex-wrap gap-1.5">{hotspot_chips}</div>
    </div>

    <div class="rounded-lg border border-[var(--border)] bg-[var(--background)] p-3 space-y-1.5">
      <div class="text-xs font-medium text-[var(--muted-foreground)]">Calibration Candidates (Read-Only Roles on Frontier)</div>
{candidates_html}
    </div>
  </div>
</body>
</html>
"""


def _main_explain(explain_argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="stats_cli.py explain",
        description="Pure dry-run diagnostic trace of Downshift subagent routing (zero ledger I/O)",
    )
    parser.add_argument(
        "--type",
        dest="type_name",
        type=str,
        default="research",
        help="Subagent type (e.g., research, self, Explore, Task, spawn_agent)",
    )
    parser.add_argument(
        "--role", type=str, default="", help="Subagent role or description"
    )
    parser.add_argument("--prompt", type=str, default="", help="Subagent prompt text")
    parser.add_argument(
        "--current-model",
        type=str,
        default="inherit",
        help="Caller's requested model before routing",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Workspace directory for .downshift.json config discovery",
    )
    parser.add_argument(
        "--host",
        choices=("antigravity", "claude", "codex", "all"),
        default="all",
        help="Target host matrix to display (default: all)",
    )
    parser.add_argument(
        "--html",
        type=str,
        default=None,
        help="Write an interactive Antigravity HTML explain trace card to the given path",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output diagnostic trace as JSON"
    )
    args = parser.parse_args(explain_argv)

    primary_host = "antigravity" if args.host == "all" else args.host
    trace = explain_subagent_call(
        type_name=args.type_name,
        role=args.role,
        prompt=args.prompt,
        current_model=args.current_model,
        host=primary_host,
        workspace_dir=args.workspace,
    )
    if args.html:
        out_path = Path(args.html).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            render_html_explain(trace, host_filter=args.host), encoding="utf-8"
        )
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    if args.json:
        payload = trace.to_dict()
        if args.host != "all":
            payload["hosts"] = {args.host: payload["hosts"][args.host]}
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(render_ascii_explain(trace, host_filter=args.host))
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "explain":
        return _main_explain(raw_argv[1:])

    parser = argparse.ArgumentParser(
        description="Downshift routing telemetry scoreboard & read-only diagnostics"
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
        "--audit-ledger",
        action="store_true",
        help="Show read-only guardrail hotspots and calibration candidates from the ledger",
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
    args = parser.parse_args(raw_argv)

    ledger_path = get_ledger_path(args.ledger)

    if args.audit_ledger:
        events = load_events(ledger_path, limit=args.limit)
        audit = compute_ledger_audit(events)
        if args.html:
            out_path = Path(args.html).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(render_html_audit(audit, ledger_path), encoding="utf-8")
        with contextlib.suppress(AttributeError, OSError, ValueError):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        if args.json:
            sys.stdout.write(json.dumps(audit, indent=2) + "\n")
        else:
            sys.stdout.write(render_ascii_audit(audit, ledger_path))
        return 0

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
