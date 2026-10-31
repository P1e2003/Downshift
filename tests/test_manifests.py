"""Manifest parity, hook script path resolution, skill frontmatter, and benchmark tests (US-1.3 / Issue #6)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_manifests_parse_and_share_name_and_version() -> None:
    manifest_paths = [
        ROOT / "plugin.json",
        ROOT / ".claude-plugin" / "plugin.json",
        ROOT / ".codex-plugin" / "plugin.json",
        ROOT / "gemini-extension.json",
    ]
    loaded: list[dict] = []
    for path in manifest_paths:
        assert path.exists(), f"Missing manifest: {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["name"] == "downshift"
        assert data["version"] == "0.1.0"
        assert data.get("description")
        loaded.append(data)

    assert loaded[3]["contextFileName"] == "rules/AGENTS.md"
    assert (ROOT / loaded[3]["contextFileName"]).exists()


def test_hooks_json_references_existing_scripts() -> None:
    launcher_cmd = ROOT / "scripts" / "run_hook.cmd"
    launcher_sh = ROOT / "scripts" / "run_hook.sh"
    assert launcher_cmd.exists(), f"Missing Windows/polyglot launcher: {launcher_cmd}"
    assert launcher_sh.exists(), f"Missing POSIX launcher: {launcher_sh}"

    agy_hooks = json.loads((ROOT / "hooks.json").read_text(encoding="utf-8"))
    for event_list in agy_hooks["hooks"].values():
        for matcher_group in event_list:
            for hook in matcher_group["hooks"]:
                cmd = hook["command"]
                assert "run_hook.cmd" in cmd, f"Expected run_hook.cmd in: {cmd}"
                match = re.search(r"\./(scripts/[a-zA-Z0-9_]+\.py)", cmd)
                assert match is not None, f"Could not parse script path from: {cmd}"
                assert (ROOT / match.group(1)).exists()

    claude_hooks_path = ROOT / "hooks" / "claude-codex-hooks.json"
    assert claude_hooks_path.exists()
    claude_hooks = json.loads(claude_hooks_path.read_text(encoding="utf-8"))
    for event_list in claude_hooks["hooks"].values():
        for matcher_group in event_list:
            for hook in matcher_group["hooks"]:
                cmd = hook["command"]
                assert "run_hook.cmd" in cmd, f"Expected run_hook.cmd in: {cmd}"
                match = re.search(
                    r"\$\{CLAUDE_PLUGIN_ROOT\}/(scripts/[a-zA-Z0-9_]+\.py)", cmd
                )
                assert match is not None, (
                    f"Could not parse CLAUDE_PLUGIN_ROOT script from: {cmd}"
                )
                assert (ROOT / match.group(1)).exists()


def test_skills_have_valid_frontmatter() -> None:
    skill_files = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert len(skill_files) >= 2
    for skill_md in skill_files:
        text = skill_md.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"Missing YAML frontmatter in {skill_md}"
        frontmatter = text.split("---\n", 2)[1]
        assert "name:" in frontmatter
        assert "description:" in frontmatter


def test_benchmark_suite_passes_100_percent_accuracy_and_zero_false_downgrades() -> (
    None
):
    bench_script = ROOT / "benchmarks" / "run_benchmark.py"
    assert bench_script.exists()
    proc = subprocess.run(
        [sys.executable, str(bench_script), "--json"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["total_scenarios"] == 20
    assert report["accuracy_pct"] == 100.0
    assert report["false_downgrades_on_high_risk"] == 0
    assert report["in_memory_median_us"] < 5000.0
    assert report["p95_latency_ms"] < 500.0
