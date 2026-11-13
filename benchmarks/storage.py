"""Run metadata and saved runs.

Each run is saved as ``results/bench-<timestamp>.json`` plus a Markdown report with
the same name. Only the newest ``KEEP_RUNS`` are kept. The latest saved run is the
comparison point for "vs last run", provided it was recorded under the same conditions.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from benchmarks.adapters import LOGLYTE_SETTINGS
from benchmarks.design import Analysis
from benchmarks.groups import THREADS
from benchmarks.harness import Result, Sizing

KEEP_RUNS = 3
REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"
#: Millisecond names keep quick back-to-back runs apart; older second-only names
#: still match so they rotate out.
RUN_FILE = re.compile(r"^bench-\d{8}-\d{6}(-\d{3})?$")


def _git_revision() -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()

    try:
        revision = git("rev-parse", "--short", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{revision} (uncommitted changes)" if dirty else revision


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "unknown"


#: Runs are comparable only if these match: same machine, same Python, same sizing.
_COMPARABLE = ("python", "machine", "processor", "sizing")


def environment(sizing: Sizing, reps: int, quick: bool) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "git": _git_revision(),
        "versions": {pkg: _version(pkg) for pkg in ("loglyte", "loguru", "structlog")},
        "sizing": {
            "budget_s": sizing.budget_s,
            "max_records": sizing.max_records,
            "fixed": sizing.fixed,
        },
        "sizing_description": sizing.describe(),
        "reps": reps,
        "quick": quick,
        "threads": THREADS,
        "loglyte_settings": LOGLYTE_SETTINGS,
    }


def _saved_runs() -> list[str]:
    """Saved run names, oldest first (the timestamped names sort chronologically)."""
    if not RESULTS_DIR.is_dir():
        return []
    return sorted({p.stem for p in RESULTS_DIR.glob("bench-*") if RUN_FILE.match(p.stem)})


@dataclass(frozen=True)
class Previous:
    """The latest comparable saved run, for "vs last run"."""

    name: str = "no previous run"
    best_ns: dict[tuple[str, str], float] = field(default_factory=dict)
    feature_costs: dict[str, float] = field(default_factory=dict)


def load_previous(env: dict[str, Any]) -> Previous:
    """The latest saved run, if it was recorded under the same conditions."""
    runs = [RESULTS_DIR / f"{stem}.json" for stem in _saved_runs()]
    runs = [path for path in runs if path.is_file()]
    if not runs:
        return Previous()
    latest = runs[-1].name
    try:
        previous = json.loads(runs[-1].read_text(encoding="utf-8"))
        meta = previous["meta"]
        if any(meta.get(key) != env[key] for key in _COMPARABLE):
            return Previous(f"{latest} is not comparable (different sizing or machine)")
        best = {
            (r["group"], r["label"]): r["best_ns"]
            for r in previous["results"]
            if r.get("best_ns") is not None
        }
        costs = previous.get("feature_costs") or {}
    except (OSError, ValueError, KeyError, TypeError):
        return Previous(f"{latest} could not be read")
    return Previous(latest, best, costs)


def save_run(
    env: dict[str, Any], results: list[Result], analysis: Analysis | None, report: str
) -> Path:
    """Write this run's JSON and Markdown, then keep only the newest ``KEEP_RUNS``."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = "bench-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    payload = {
        "meta": env,
        "results": [r.to_json() for r in results],
        "feature_baseline": analysis.baseline if analysis else None,
        "feature_costs": {e.factor: e.cost for e in analysis.effects} if analysis else None,
        "feature_intervals": (
            {e.factor: e.interval for e in analysis.effects} if analysis else None
        ),
    }
    (RESULTS_DIR / f"{stem}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    markdown = RESULTS_DIR / f"{stem}.md"
    markdown.write_text(report, encoding="utf-8")

    for old in _saved_runs()[:-KEEP_RUNS]:
        for suffix in (".json", ".md"):
            (RESULTS_DIR / f"{old}{suffix}").unlink(missing_ok=True)
    return markdown
