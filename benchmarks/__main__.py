"""Command line: ``uv run python -m benchmarks [--quick] [--only LAYER|GROUP ...]``."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from benchmarks.adapters import restore_stdlib_switches
from benchmarks.features import analyse_features
from benchmarks.groups import GROUP_NAMES, build_groups
from benchmarks.harness import LAYERS, Result, Sizing, measure
from benchmarks.report import markdown_report, problems, terminal_summary
from benchmarks.storage import KEEP_RUNS, environment, load_previous, save_run

REPS = 5
QUICK = Sizing(fixed=2_000)
QUICK_REPS = 2


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not number > 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return number


def _ambient_config() -> str | None:
    """Loglyte loads these at import; a benchmark must not depend on them."""
    if os.environ.get("LOGLYTE_CONFIG"):
        return "LOGLYTE_CONFIG is set"
    if (Path.cwd() / "loglyte.toml").is_file():
        return f"{Path.cwd() / 'loglyte.toml'} exists"
    return None


def _progress(done: int, total: int, label: str) -> None:
    if sys.stderr.isatty():
        sys.stderr.write(f"\r\x1b[K[{done}/{total}] {label}")
        sys.stderr.flush()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks",
        description="Benchmark loglyte against stdlib logging, Loguru and structlog.",
    )
    parser.add_argument(
        "--budget",
        type=_positive_float,
        default=Sizing.budget_s,
        metavar="SECONDS",
        help="target duration of one timed pass; cases are sized to it (default: %(default)s)",
    )
    parser.add_argument(
        "--records",
        type=_positive_int,
        metavar="N",
        help="use exactly N records for every case instead of sizing by budget",
    )
    parser.add_argument("--reps", type=_positive_int, default=REPS)
    parser.add_argument(
        "--quick",
        action="store_true",
        help=f"smoke test of the harness: {QUICK.fixed:,} records, {QUICK_REPS} reps",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=(*LAYERS, *GROUP_NAMES),
        metavar="LAYER|GROUP",
        help=f"run only these layers ({', '.join(LAYERS)}) or groups",
    )
    args = parser.parse_args()

    ambient = _ambient_config()
    if ambient:
        print(f"Refusing to run: {ambient}; it would change loglyte's behaviour.", file=sys.stderr)
        return 2

    if args.quick:
        sizing, reps = QUICK, QUICK_REPS
    else:
        sizing, reps = Sizing(budget_s=args.budget, fixed=args.records), args.reps
    groups = build_groups()
    if args.only:
        wanted = set(args.only)
        groups = tuple(
            g for g in groups if g.name in wanted or g.layer in wanted or g.name == "overhead"
        )

    env = environment(sizing, reps, args.quick)
    previous = load_previous(env)
    total = sum(len(group.cases) for group in groups)
    if not sizing.fixed:
        # warm-up + timed passes (+ latency pass), each about one budget long
        passes = sum(len(g.cases) * (reps + 1 + g.latency) for g in groups)
        estimate = passes * sizing.budget_s / 60
        print(
            f"{total} cases, estimated {estimate:.0f}-{estimate * 1.5:.0f} min "
            "(cases slower than the budget at their minimum size take longer)",
            file=sys.stderr,
        )
    started = time.monotonic()
    results: list[Result] = []
    try:
        with tempfile.TemporaryDirectory(prefix="loglyte-bench-") as tmp:
            for group in groups:
                for case in group.cases:
                    _progress(len(results) + 1, total, f"{group.name} / {case.label}")
                    results.append(measure(group, case, sizing, reps, Path(tmp)))
    finally:
        env["duration_s"] = time.monotonic() - started
        restore_stdlib_switches()
        if sys.stderr.isatty():
            sys.stderr.write("\r\x1b[K")

    analysis = analyse_features(results)
    print(terminal_summary(groups, results, env, previous, analysis))
    report = markdown_report(groups, results, env, previous, analysis)
    saved = save_run(env, results, analysis, report)
    try:
        shown = saved.relative_to(Path.cwd())
    except ValueError:
        shown = saved
    print(f"Report: {shown}  (JSON alongside; last {KEEP_RUNS} runs kept)")
    return 1 if problems(results) else 0


if __name__ == "__main__":
    sys.exit(main())
