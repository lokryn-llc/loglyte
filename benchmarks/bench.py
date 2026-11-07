"""Benchmark loglyte against stdlib logging, Loguru and structlog.

Method
------
Every configuration writes the *same number of records* to a *real file* and is
timed end-to-end: the clock stops only once the bytes are on disk (handlers
closed / queues joined / buffers flushed). Per-record byte counts are reported so
you can see whether the compared work was genuinely comparable.

Each measurement is repeated ``REPS`` times; the minimum is reported (least
contaminated by scheduler noise) alongside the median.

Caveats worth reading before quoting any number
-----------------------------------------------
* Output is *similar*, not identical. stdlib and Loguru capture the caller's
  file, line and function by frame introspection on every call; loglyte captures
  the function name once, at decoration, and never introspects. That is a real
  architectural difference and a large part of the gap -- but it means loglyte
  records cannot tell you which *line* logged them.
* loglyte and ``loguru(enqueue)`` deliver asynchronously. The end-to-end column
  includes the drain, so the batching win is counted honestly, but a hard kill
  can still lose what is buffered. The others are synchronous.
* Absolute numbers are machine-specific. Ratios travel better than nanoseconds.

Usage: uv run python benchmarks/bench.py [--iterations N] [--reps R] [--quick]
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import logging
import os
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import structlog
from loguru import logger as loguru_logger

import loglyte

ITERATIONS = 50_000
REPS = 5
THREADS = 4

FIELDS = ("item_id", "user_id", "region")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class Result:
    library: str
    scenario: str
    times: list[float] = field(default_factory=list)
    lines: int = 0
    bytes_written: int = 0

    @property
    def best_ns(self) -> float:
        return min(self.times) / self.n * 1e9

    @property
    def median_ns(self) -> float:
        return statistics.median(self.times) / self.n * 1e9

    n: int = ITERATIONS


class Adapter:
    """One logging library, configured to write plain text to ``path``."""

    name = "?"
    #: True if records are delivered off the calling thread.
    asynchronous = False
    #: True if the library captures caller file/line on every record.
    introspects_caller = True

    def setup(self, path: str, level: str = "INFO", json_mode: bool = False) -> None:
        raise NotImplementedError

    def teardown(self) -> None:
        """Must guarantee every record is on disk when it returns."""
        raise NotImplementedError

    # The call shapes exercised by the scenarios.
    def static(self) -> None:
        raise NotImplementedError

    def fields(self, i: int) -> None:
        raise NotImplementedError

    def interpolated(self, i: int) -> None:
        raise NotImplementedError

    def filtered(self, i: int) -> None:
        raise NotImplementedError

    def json_fields(self, i: int) -> None:
        """Structured record in JSON mode: timestamp + level + message + 3 fields."""
        raise NotImplementedError

    @contextmanager
    def bound(self, **kw: object) -> Iterator[None]:
        yield


class _JsonFormatter(logging.Formatter):
    """Minimal equivalent of what python-json-logger emits."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": record.created,
            "level": record.levelname,
            "logger": record.funcName,
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload)


class StdlibAdapter(Adapter):
    name = "stdlib logging"

    def setup(self, path: str, level: str = "INFO", json_mode: bool = False) -> None:
        self.logger = logging.getLogger(f"bench.{id(self)}.{time.time_ns()}")
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.logger.setLevel(getattr(logging, level))
        self.handler = logging.FileHandler(path, encoding="utf-8")
        self.handler.setFormatter(
            _JsonFormatter()
            if json_mode
            else logging.Formatter("%(levelname)s %(funcName)s: %(message)s")
        )
        self.logger.addHandler(self.handler)

    def json_fields(self, i: int) -> None:
        self.logger.info(
            "Processing item",
            extra={"fields": {"item_id": i, "user_id": "usr_9", "region": "eu"}},
        )

    def teardown(self) -> None:
        self.handler.close()
        self.logger.handlers.clear()

    def static(self) -> None:
        self.logger.info("Static message")

    def fields(self, i: int) -> None:
        self.logger.info("Processing item item_id=%s user_id=%s region=%s", i, "usr_9", "eu")

    def interpolated(self, i: int) -> None:
        self.logger.info(f"Processing item {i}")

    def filtered(self, i: int) -> None:
        self.logger.debug("Processing item item_id=%s", i)


class LoguruAdapter(Adapter):
    name = "loguru"
    enqueue = False

    def setup(self, path: str, level: str = "INFO", json_mode: bool = False) -> None:
        loguru_logger.remove()
        self.sink_id = loguru_logger.add(
            path,
            format="{level} {function}: {message}",
            level=level,
            enqueue=self.enqueue,
            serialize=json_mode,
        )

    def json_fields(self, i: int) -> None:
        loguru_logger.bind(item_id=i, user_id="usr_9", region="eu").info("Processing item")

    def teardown(self) -> None:
        loguru_logger.remove()  # joins the enqueue worker and closes the file

    def static(self) -> None:
        loguru_logger.info("Static message")

    def fields(self, i: int) -> None:
        loguru_logger.info("Processing item item_id={} user_id={} region={}", i, "usr_9", "eu")

    def interpolated(self, i: int) -> None:
        loguru_logger.info(f"Processing item {i}")

    def filtered(self, i: int) -> None:
        loguru_logger.debug("Processing item item_id={}", i)

    @contextmanager
    def bound(self, **kw: object) -> Iterator[None]:
        with loguru_logger.contextualize(**kw):
            yield


class LoguruEnqueueAdapter(LoguruAdapter):
    name = "loguru (enqueue)"
    enqueue = True
    asynchronous = True


class StructlogAdapter(Adapter):
    name = "structlog"
    introspects_caller = False

    def setup(self, path: str, level: str = "INFO", json_mode: bool = False) -> None:
        self.fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed in teardown()
        renderer = (
            [structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()]
            if json_mode
            else [structlog.processors.KeyValueRenderer()]
        )
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                *renderer,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
            logger_factory=structlog.PrintLoggerFactory(file=self.fh),
            cache_logger_on_first_use=True,
        )
        self.logger = structlog.get_logger()

    def teardown(self) -> None:
        self.fh.flush()
        self.fh.close()
        structlog.reset_defaults()

    def static(self) -> None:
        self.logger.info("Static message")

    def fields(self, i: int) -> None:
        self.logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")

    def interpolated(self, i: int) -> None:
        self.logger.info(f"Processing item {i}")

    def filtered(self, i: int) -> None:
        self.logger.debug("Processing item", item_id=i)

    def json_fields(self, i: int) -> None:
        self.logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")

    @contextmanager
    def bound(self, **kw: object) -> Iterator[None]:
        structlog.contextvars.bind_contextvars(**kw)
        try:
            yield
        finally:
            structlog.contextvars.clear_contextvars()


class LoglyteAdapter(Adapter):
    name = "loglyte"
    asynchronous = True
    introspects_caller = False

    def setup(self, path: str, level: str = "INFO", json_mode: bool = False) -> None:
        loglyte.configure(
            sinks=[loglyte.FileSink(path, level=level, json_mode=json_mode)],
            flush_interval=0,  # measure all the work here, don't race a flusher
        )
        self.log = loglyte.ContextLogger("bench")

    def json_fields(self, i: int) -> None:
        self.log.info("Processing item", item_id=i, user_id="usr_9", region="eu")

    def teardown(self) -> None:
        loglyte.SINK.stop()

    def static(self) -> None:
        self.log.info("Static message")

    def fields(self, i: int) -> None:
        self.log.info("Processing item", item_id=i, user_id="usr_9", region="eu")

    def interpolated(self, i: int) -> None:
        self.log.info(f"Processing item {i}")

    def filtered(self, i: int) -> None:
        self.log.debug("Processing item", item_id=i)

    @contextmanager
    def bound(self, **kw: object) -> Iterator[None]:
        with loglyte.bind(**kw):
            yield


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_static(a: Adapter, n: int) -> None:
    for _ in range(n):
        a.static()


def scenario_fields(a: Adapter, n: int) -> None:
    for i in range(n):
        a.fields(i)


def scenario_interpolated(a: Adapter, n: int) -> None:
    for i in range(n):
        a.interpolated(i)


def scenario_filtered(a: Adapter, n: int) -> None:
    for i in range(n):
        a.filtered(i)


def scenario_bound(a: Adapter, n: int) -> None:
    with a.bound(request_id="req_abc123", user_id="usr_774"):
        for i in range(n):
            a.fields(i)


def scenario_json(a: Adapter, n: int) -> None:
    for i in range(n):
        a.json_fields(i)


def scenario_threaded(a: Adapter, n: int) -> None:
    per = n // THREADS

    def worker() -> None:
        for i in range(per):
            a.fields(i)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


@dataclass(frozen=True)
class Scenario:
    fn: Callable[[Adapter, int], None]
    description: str
    json_mode: bool = False


SCENARIOS: dict[str, Scenario] = {
    "static": Scenario(scenario_static, "constant message, no fields"),
    "fields": Scenario(scenario_fields, "message + 3 structured fields"),
    "interpolated": Scenario(scenario_interpolated, "eager f-string in the call"),
    "filtered": Scenario(scenario_filtered, "DEBUG call, sink at INFO (discarded)"),
    "bound": Scenario(scenario_bound, "fields + 2 bound context values"),
    "threaded": Scenario(scenario_threaded, f"{THREADS} threads, fields"),
    "json": Scenario(
        scenario_json, "JSON out: timestamp + level + message + 3 fields", json_mode=True
    ),
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def measure(adapter: Adapter, scenario: str, n: int, reps: int, tmpdir: str) -> Result:
    spec = SCENARIOS[scenario]
    result = Result(library=adapter.name, scenario=scenario, n=n)

    for rep in range(reps + 1):  # rep 0 is a warm-up, discarded
        path = os.path.join(tmpdir, f"{adapter.name}-{scenario}-{rep}.log".replace(" ", "_"))
        adapter.setup(path, level="INFO", json_mode=spec.json_mode)
        gc.collect()

        start = time.perf_counter()
        spec.fn(adapter, n)
        adapter.teardown()
        elapsed = time.perf_counter() - start

        if rep == 0:
            if os.path.exists(path):
                os.remove(path)
            continue
        result.times.append(elapsed)
        if os.path.exists(path):  # a fully-filtered scenario writes nothing
            result.bytes_written = os.path.getsize(path)
            with open(path, encoding="utf-8") as f:
                result.lines = sum(1 for _ in f)
            os.remove(path)

    return result


def build_adapters() -> list[Adapter]:
    return [
        StdlibAdapter(),
        LoguruAdapter(),
        LoguruEnqueueAdapter(),
        StructlogAdapter(),
        LoglyteAdapter(),
    ]


def render(results: list[Result], n: int) -> str:
    out = io.StringIO()
    by_scenario: dict[str, list[Result]] = {}
    for r in results:
        by_scenario.setdefault(r.scenario, []).append(r)

    for scenario, rows in by_scenario.items():
        description = SCENARIOS[scenario].description
        print(f"\n### {scenario} — {description}", file=out)
        header = (
            f"{'library':<18} | {'ns/record':>10} | {'median':>10} | "
            f"{'vs stdlib':>9} | {'lines':>7} | {'bytes/rec':>9}"
        )
        print(header, file=out)
        print("-" * len(header), file=out)

        baseline = next((r.best_ns for r in rows if r.library == "stdlib logging"), None)
        for r in sorted(rows, key=lambda r: r.best_ns):
            ratio = f"{baseline / r.best_ns:>8.2f}x" if baseline else "        -"
            per_rec = r.bytes_written / r.lines if r.lines else 0
            print(
                f"{r.library:<18} | {r.best_ns:>10,.0f} | {r.median_ns:>10,.0f} | "
                f"{ratio:>9} | {r.lines:>7,} | {per_rec:>9.1f}",
                file=out,
            )
    return out.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=ITERATIONS)
    parser.add_argument("--reps", type=int, default=REPS)
    parser.add_argument("--quick", action="store_true", help="5k iterations, 2 reps")
    args = parser.parse_args()

    n, reps = (5_000, 2) if args.quick else (args.iterations, args.reps)

    print(f"python      {sys.version.split()[0]}")
    print(f"platform    {sys.platform}")
    print(f"iterations  {n:,} per measurement")
    print(f"repetitions {reps} (best reported; median shown alongside)")
    print("timing      end-to-end: stops once bytes are on disk")

    results: list[Result] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for scenario in SCENARIOS:
            for adapter in build_adapters():
                sys.stderr.write(f"  running {adapter.name:<18} {scenario}\n")
                sys.stderr.flush()
                results.append(measure(adapter, scenario, n, reps, tmpdir))

    print(render(results, n))

    print("\nNotes")
    print("  * lines/bytes-per-record show whether the compared work was comparable;")
    print("    a much smaller bytes/rec means that library emitted less information.")
    print("  * stdlib and loguru capture caller file/line every call; structlog and")
    print("    loglyte do not. That is a capability difference, not just a speed one.")
    print("  * loglyte and loguru(enqueue) deliver asynchronously; the drain is included")
    print("    in the timing, but a hard kill can still lose buffered records.")


if __name__ == "__main__":
    main()
