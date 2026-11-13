"""Layer 2: what each loglyte feature costs, from one designed experiment.

Every run of the design writes the same record with a different combination of features
switched on. ``design.analyse`` then recovers each feature's cost per record, with a 95%
interval, and checks that the features add up rather than interact. Only features whose
cost is paid on every record belong here; features that skip work (level filters,
sampling, rate limits) are measured on their own in the micro layer.
"""

from __future__ import annotations

import math
import random
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import loglyte
from benchmarks.adapters import LOGLYTE_SETTINGS, LoglyteAdapter
from benchmarks.design import Analysis, analyse, fold_over_design
from benchmarks.harness import Case, Group, Result, Run
from loglyte import FileSink, StdoutSink, logger

BOUND_FIELDS = {"request_id": "req_abc123", "tenant": "acme"}

#: Fixed, so the run order (and each run's label) is the same in every benchmark run.
_RUN_ORDER_SEED = 7


@dataclass(frozen=True)
class Feature:
    name: str
    description: str


FEATURES = (
    Feature("json", "JSON output instead of text"),
    Feature("redaction", "redact_fields on two of the record's fields"),
    Feature("sink filter", "a sink filter that keeps every record"),
    Feature("process_safe", "process_safe=True on the file sink"),
    Feature("second sink", "a second file sink"),
    Feature("console sink", "a coloured stdout sink (stdout captured to a file)"),
    Feature("location", "logger.with_location(): caller file, line and function"),
    Feature("bind scope", "with logger.bind(2 fields): around each call"),
    Feature("bound logger", "logger.bind(2 fields).info(...) on each call"),
    Feature("decorator", "the logging call happens inside an @logger function"),
    Feature("debug noise", "an extra DEBUG call per record, discarded by the INFO level"),
)

DESIGN = fold_over_design(tuple(f.name for f in FEATURES))


def _keep_all(record: Any) -> bool:
    return True


def _emitter(on: set[str]) -> Callable[[int], None]:
    """The per-record call for one combination of features, built once per run."""
    base = logger.with_location() if "location" in on else logger
    bound, scope, noise = "bound logger" in on, "bind scope" in on, "debug noise" in on

    def emit(i: int) -> None:
        log = base.bind(**BOUND_FIELDS) if bound else base
        if noise:
            log.debug("Cache probe", item_id=i)
        log.info("Processing item", item_id=i, user_id="usr_9", region="eu")

    def emit_in_scope(i: int) -> None:
        with logger.bind(**BOUND_FIELDS):
            emit(i)

    call = emit_in_scope if scope else emit
    return logger(call) if "decorator" in on else call


def _case(row: tuple[int, ...]) -> Case:
    on = {f.name for f, level in zip(FEATURES, row, strict=True) if level > 0}
    call = _emitter(on)

    def prepare(path: str) -> Run:
        primary = FileSink(
            path,
            level="INFO",
            render_mode="json" if "json" in on else "text",
            max_bytes=0,
            redact_fields=("user_id", "region") if "redaction" in on else (),
            process_safe="process_safe" in on,
        )
        if "sink filter" in on:
            primary.add_filter(_keep_all)
        sinks = [primary]
        if "second sink" in on:
            sinks.append(FileSink(f"{path}.second", level="INFO", max_bytes=0))
        console = None
        if "console sink" in on:
            # StdoutSink looks up sys.stdout per batch, so capture it for this pass.
            console = open(f"{path}.console", "w", encoding="utf-8")  # noqa: SIM115
            sys.stdout, saved_stdout = console, sys.stdout
            sinks.append(StdoutSink(level="INFO", color="always"))
        loglyte.configure(sinks, **LOGLYTE_SETTINGS)

        def finish() -> None:
            try:
                if not logger.shutdown():
                    raise RuntimeError("loglyte shutdown timed out")
            finally:
                if console is not None:
                    sys.stdout = saved_stdout
                    console.close()

        return Run(call=call, finish=finish)

    return Case(DESIGN.pattern(row), prepare, min_records=LoglyteAdapter().min_records)


def feature_group() -> Group:
    rows = list(DESIGN.rows)
    random.Random(_RUN_ORDER_SEED).shuffle(rows)  # decouple features from time drift
    return Group(
        "features",
        f"{len(FEATURES)} loglyte features, {len(rows)} runs of a resolution IV design",
        tuple(_case(row) for row in rows),
        layer="features",
        kind="matrix",
        latency=False,
        design=DESIGN,
    )


def analyse_features(results: list[Result]) -> Analysis | None:
    """Feature costs in ns/record, or None if the group did not run cleanly."""
    by_pattern = {r.label: r for r in results if r.group == "features"}
    runs = [by_pattern.get(DESIGN.pattern(row)) for row in DESIGN.rows]
    if not all(r is not None and r.times_s and len(r.times_s) > 1 for r in runs):
        return None
    # Each run's response is its median; its noise is the standard error of a median.
    responses = [r.median_ns for r in runs]
    noise = [
        math.sqrt(math.pi / 2)
        * statistics.stdev(t / r.records * 1e9 for t in r.times_s)
        / math.sqrt(len(r.times_s))
        for r in runs
    ]
    return analyse(DESIGN, responses, math.sqrt(statistics.fmean(s * s for s in noise)))
