"""The terminal summary and the Markdown report.

The terminal is a sniff test: one short section per layer, then checks and noise. The
Markdown report holds every number, the caveats needed to read them, and any errors.
"""

from __future__ import annotations

from typing import Any

from benchmarks.design import Analysis
from benchmarks.features import FEATURES
from benchmarks.harness import PERCENTILES, Group, Result
from benchmarks.storage import Previous

#: A case whose median is this much slower than its best run was disturbed by noise.
NOISY_SPREAD = 0.20

CAVEATS = (
    "Numbers are machine-specific. Compare runs from the same machine; ratios travel "
    "better than nanoseconds.",
    "Workloads run one script per profile through every library, so each library does "
    "the same logging work; each library uses its own idiomatic configuration for it.",
    "Feature costs come from a resolution IV fractional factorial design: each feature's "
    "cost is the mean of the runs where it was on minus the mean where it was off. The "
    "interval is 95%, from the larger of the measurement noise and what the additive "
    "model leaves unexplained. 'Lack of fit' near 1 means features add up; well above 2 "
    "means they interact or the machine drifted.",
    "stdlib and Loguru look up the caller's file, line and function on every record; "
    "loglyte (unless `with_location()` is used) and structlog do not. The `ablation` "
    "group shows what that lookup costs stdlib.",
    "loglyte buffers records. Every `flush_threshold`-th call formats and writes the whole "
    "batch on the calling thread; the background thread only flushes every "
    "`flush_interval`. That cost is inside the totals and shows in the p99.9/max columns.",
    "Loguru (enqueue) pays one queue handoff per record, so it runs only where that tells "
    "something new: `web-request`, `fields`, `threaded` and `json`.",
    "`bound`: stdlib has no context binding, so its row is the plain `fields` call.",
    "Every library runs with its default settings. Loguru's default `diagnose=True` "
    "renders local variable values into every traceback, which dominates its "
    "`exceptions` and `web-request` cost (and can leak data in production logs).",
    "Rotation cases keep every rotated file so records can be counted; stdlib uses a high "
    "backupCount, and checks each backup name on every rollover.",
    "bytes/rec is what each library wrote (compressed size for gzip); fewer bytes means "
    "less information per record, not only less overhead.",
    "Each case is sized from its warm-up pass so one timed pass lasts about the budget "
    "(floors: 1,000 records; 5,000 for loglyte so batches are flushed; about 5 rotations "
    "in the rotation cases; cap 500,000). A one-off cost per pass inflates ns/record by "
    "about that cost divided by "
    "the budget, equally for every case.",
    "Per-call latency is a separate pass timing each call with `perf_counter_ns`, so it "
    "includes the timer (see `overhead`). Threaded and feature cases have none.",
    "The garbage collector stays enabled while timing: allocation is part of the real "
    "cost of a log call.",
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _ns(value: float | None) -> str:
    return "error" if value is None else f"{value:,.0f}"


def _duration(ns: int | None) -> str:
    if ns is None:
        return "-"
    if ns < 10_000:
        return f"{ns:,} ns"
    if ns < 1_000_000:
        return f"{ns / 1e3:,.1f} us"
    return f"{ns / 1e6:,.1f} ms"


def _elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _speed(baseline: float | None, value: float | None) -> str:
    if not baseline or not value:
        return "-"
    ratio = baseline / value
    return f"{ratio:.2f}x faster" if ratio >= 1 else f"{1 / ratio:.2f}x slower"


def _diff(baseline: float | None, value: float | None) -> str:
    return "" if baseline is None or value is None else f"{value - baseline:+,.0f}"


def _change(previous: float | None, value: float | None) -> str:
    return "-" if not previous or value is None else f"{(value - previous) / previous:+.0%}"


def _by_group(results: list[Result]) -> dict[str, list[Result]]:
    grouped: dict[str, list[Result]] = {}
    for result in results:
        grouped.setdefault(result.group, []).append(result)
    return grouped


def problems(results: list[Result]) -> list[str]:
    """Every failed check and error, one line each; empty means a clean run."""
    found = [f"{r.group} / {r.label}: {issue}" for r in results for issue in r.issues]
    found += [f"{r.group} / {r.label}: raised an error (see report)" for r in results if r.error]
    return found


def _noise_summary(results: list[Result]) -> str:
    timed = [r for r in results if r.spread is not None]
    noisy = [r for r in timed if r.spread > NOISY_SPREAD]
    if not noisy:
        return f"Noise: low - every case's median was within {NOISY_SPREAD:.0%} of its best run"
    return (
        f"Noise: {len(noisy)} of {len(timed)} cases had a median >{NOISY_SPREAD:.0%} slower "
        "than their best run; treat small differences as noise (see 'spread' in the report)"
    )


def _fit(analysis: Analysis) -> str:
    fit = f"sum of feature costs explains {analysis.r_squared:.0%} of the variation"
    if analysis.lack_of_fit is not None:
        verdict = "features add up" if analysis.lack_of_fit < 2 else "features interact or drift"
        fit += f"; lack of fit {analysis.lack_of_fit:.1f} ({verdict})"
    return fit


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


def _library_table(
    groups: list[Group], grouped: dict[str, list[Result]], prev: Previous
) -> list[str]:
    widest_first = sorted(groups, key=lambda g: -len(g.cases))
    names = list(dict.fromkeys(c.label for g in widest_first for c in g.cases))
    header = f"{'':<15}" + "".join(f"{name:>15}" for name in names)
    out = [header + f"{'loglyte vs stdlib':>19}{'vs last':>9}"]
    for group in groups:
        rows = {r.label: r for r in grouped[group.name]}
        cells = "".join(f"{_ns(rows[n].best_ns) if n in rows else '-':>15}" for n in names)
        loglyte_ns = rows["loglyte"].best_ns
        out.append(
            f"{group.name:<15}{cells}{_speed(rows['stdlib'].best_ns, loglyte_ns):>19}"
            f"{_change(prev.best_ns.get((group.name, 'loglyte')), loglyte_ns):>9}"
        )
    return out


def _breakdown_line(group: Group, rows: list[Result], prev: Previous) -> str:
    first = rows[0].best_ns
    line = f"  {group.name:<18}row 1 {_ns(first):>7} ns"
    others = [r for r in rows[1:] if r.best_ns is not None and first is not None]
    if others:
        costliest = max(others, key=lambda r: abs(r.best_ns - first))
        line += f" | biggest: {costliest.label} {_diff(first, costliest.best_ns)}"
    changes = [
        (r.best_ns / prev.best_ns[(r.group, r.label)] - 1, r.label)
        for r in rows
        if r.best_ns is not None and prev.best_ns.get((r.group, r.label))
    ]
    if changes:
        change, label = max(changes, key=lambda c: abs(c[0]))
        line += f" | vs last: {change:+.0%} ({label})"
    return line


def terminal_summary(
    groups: tuple[Group, ...],
    results: list[Result],
    env: dict[str, Any],
    prev: Previous,
    analysis: Analysis | None,
) -> str:
    grouped = _by_group(results)
    ran = [g for g in groups if g.name in grouped]
    out = [
        f"loglyte benchmark | {env['sizing_description']} x {env['reps']} reps | "
        f"{env['python']} | {env['machine']} | git {env['git']}",
        f"run time: {_elapsed(env.get('duration_s'))} | previous run: {prev.name}",
    ]

    workloads = [g for g in ran if g.layer == "workload"]
    if workloads:
        units = ", ".join(f"{g.name} = 1 {g.unit}" for g in workloads)
        out += ["", f"LAYER 1 - workloads, ns per unit of work ({units})"]
        out += _library_table(workloads, grouped, prev)

    if any(g.layer == "features" for g in ran):
        out += ["", "LAYER 2 - loglyte feature costs, ns/record (+/- 95%)"]
        if analysis is None:
            out.append("  not available: a feature run failed (see report)")
        else:
            out.append(f"  {'all features off (baseline)':<30}{analysis.baseline:>9,.0f}")
            for effect in sorted(analysis.effects, key=lambda e: -e.cost):
                mark = "" if effect.distinguishable else "  (not distinguishable from 0)"
                change = _diff(prev.feature_costs.get(effect.factor), effect.cost)
                change = f"  vs last {change}" if change else ""
                out.append(
                    f"  {effect.factor:<30}{effect.cost:>+9,.0f} +/- {effect.interval:<7,.0f}"
                    f"{mark}{change}"
                )
            out.append(f"  {_fit(analysis)}")

    micro_libraries = [g for g in ran if g.layer == "micro" and g.kind == "libraries"]
    breakdowns = [g for g in ran if g.kind == "breakdown" and g.name != "overhead"]
    if micro_libraries or breakdowns:
        out += ["", "LAYER 3 - micro, ns/record (best; lower is better)"]
        if "overhead" in grouped:
            empty = grouped["overhead"][0]
            out.append(
                f"harness: empty loop {_ns(empty.best_ns)} ns, timer floor "
                f"{_duration(empty.latency_ns.get('p50'))} (included in every number)"
            )
    if micro_libraries:
        out += _library_table(micro_libraries, grouped, prev)
    if "fields" in grouped:
        out += [f"{'per-call latency, fields':<28}" + "".join(f"{k:>11}" for k in PERCENTILES)]
        for r in grouped["fields"]:
            cells = "".join(f"{_duration(r.latency_ns.get(k)):>11}" for k in PERCENTILES)
            out.append(f"  {r.label:<26}{cells}")
    if breakdowns:
        out.append("loglyte breakdowns (full tables in the report)")
        out += [_breakdown_line(g, grouped[g.name], prev) for g in breakdowns]

    found = problems(results)
    out.append("")
    if found:
        out.append(f"CHECKS: {len(found)} problem(s)")
        out += [f"  ! {problem}" for problem in found]
    else:
        out.append("Checks: OK - expected records written, nothing leaked, no loglyte drops")
    out.append(_noise_summary(results))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

_LAYER_TITLES = {
    "workload": "Layer 1: workloads",
    "features": "Layer 2: loglyte feature costs",
    "micro": "Layer 3: micro",
}


def _case_table(rows: list[Result], prev: Previous) -> list[str]:
    first = rows[0].best_ns
    out = [
        "| case | best | median | spread | vs row 1 | diff | p50 | p99 | p99.9 | max "
        "| records | lines | bytes/rec | vs last run |",
        "|" + " --- |" + " ---: |" * 13,
    ]
    for r in rows:
        latency = [_duration(r.latency_ns.get(k)) for k in PERCENTILES]
        is_first = r is rows[0]
        out.append(
            f"| {r.label} | {_ns(r.best_ns)} | {_ns(r.median_ns)} "
            f"| {'-' if r.spread is None else f'{r.spread:.0%}'} "
            f"| {'baseline' if is_first else _speed(first, r.best_ns)} "
            f"| {'' if is_first else _diff(first, r.best_ns)} | {' | '.join(latency)} "
            f"| {r.records:,} | {r.lines:,} | {r.bytes_per_record:.1f} "
            f"| {_change(prev.best_ns.get((r.group, r.label)), r.best_ns)} |"
        )
    return out


def _run_table(rows: list[Result], prev: Previous) -> list[str]:
    """The runs of a designed experiment: no baseline row, no per-call latency."""
    out = [
        "| run | best | median | spread | records | lines | bytes/rec | vs last run |",
        "| --- |" + " ---: |" * 7,
    ]
    for r in sorted(rows, key=lambda r: r.label, reverse=True):
        out.append(
            f"| `{r.label}` | {_ns(r.best_ns)} | {_ns(r.median_ns)} "
            f"| {'-' if r.spread is None else f'{r.spread:.0%}'} | {r.records:,} "
            f"| {r.lines:,} | {r.bytes_per_record:.1f} "
            f"| {_change(prev.best_ns.get((r.group, r.label)), r.best_ns)} |"
        )
    return out


def _feature_section(analysis: Analysis | None, prev: Previous) -> list[str]:
    if analysis is None:
        return ["Not available: at least one feature run failed or was not timed."]
    described = {f.name: f.description for f in FEATURES}
    out = [
        f"Baseline with every feature off: **{analysis.baseline:,.0f} ns/record**. "
        f"{_fit(analysis).capitalize()}.",
        "",
        "| feature | what is switched on | cost ns/record | 95% interval | "
        "distinguishable from 0 | vs last run |",
        "| --- | --- | ---: | ---: | --- | ---: |",
    ]
    for effect in sorted(analysis.effects, key=lambda e: -e.cost):
        out.append(
            f"| {effect.factor} | {described[effect.factor]} | {effect.cost:+,.0f} "
            f"| +/- {effect.interval:,.0f} | {'yes' if effect.distinguishable else 'no'} "
            f"| {_diff(prev.feature_costs.get(effect.factor), effect.cost) or '-'} |"
        )
    return out


def markdown_report(
    groups: tuple[Group, ...],
    results: list[Result],
    env: dict[str, Any],
    prev: Previous,
    analysis: Analysis | None,
) -> str:
    grouped = _by_group(results)
    settings = ", ".join(f"{k}={v}" for k, v in env["loglyte_settings"].items())
    out = [
        "# loglyte benchmark run",
        "",
        "| | |",
        "| --- | --- |",
        f"| recorded (UTC) | {env['timestamp']} |",
        f"| git | {env['git']} |",
        f"| python | {env['python']} |",
        f"| platform | {env['platform']} ({env['processor']}, {env['cpu_count']} CPUs) |",
        f"| versions | {', '.join(f'{k} {v}' for k, v in env['versions'].items())} |",
        f"| records per case | {env['sizing_description']}"
        f"{' (quick run)' if env['quick'] else ''} |",
        f"| repetitions | {env['reps']} timed, after 1 warm-up that also sizes the case |",
        f"| run time | {_elapsed(env.get('duration_s'))} |",
        f"| loglyte settings | {settings} |",
        f"| compared with | {prev.name} |",
        "",
        "## Checks",
        "",
    ]
    out += [f"- {problem}" for problem in problems(results)] or [
        "Every case wrote the expected number of records, nothing that must be redacted "
        "reached the output, and loglyte reported no dropped records or sink failures."
    ]
    out += ["", _noise_summary(results) + "."]
    out += [
        "",
        "In the tables, `best`/`median` are ns per unit of work, end to end. `vs row 1` "
        "compares with the group's first row. `spread` is how much slower the median run "
        f"was than the best; above {NOISY_SPREAD:.0%} the machine was noisy. Latency "
        "columns are single calls. `vs last run` compares `best` with the previous run.",
    ]

    for layer, title in _LAYER_TITLES.items():
        layer_groups = [g for g in groups if g.layer == layer and g.name in grouped]
        if not layer_groups:
            continue
        out += ["", f"## {title}"]
        if layer == "features":
            out += ["", *_feature_section(analysis, prev)]
        for group in layer_groups:
            out += ["", f"### {group.name}: {group.title}", ""]
            if group.design is not None:
                legend = ", ".join(
                    f"{i + 1}. {name}" for i, name in enumerate(group.design.factors)
                )
                out += [f"Each run's label marks its features on (+) or off (-): {legend}.", ""]
            if group.unit != "record":
                out += [f"Unit of work: one {group.unit}.", ""]
            if group.kind == "matrix":
                out += _run_table(grouped[group.name], prev)
            else:
                out += _case_table(grouped[group.name], prev)

    out += ["", "## How to read these numbers", ""]
    out += [f"- {caveat}" for caveat in CAVEATS]

    errors = [r for r in results if r.error]
    if errors:
        out += ["", "## Errors", ""]
        for r in errors:
            out += [f"### {r.group} / {r.label}", "", "```text", r.error.rstrip(), "```", ""]
    return "\n".join(out) + "\n"
