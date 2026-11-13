"""How a case is timed and checked.

A ``Case`` prepares a ``Run`` against a log file; ``measure`` sizes it, times it end to
end, profiles per-call latency, and verifies what was written.
"""

from __future__ import annotations

import asyncio
import gc
import gzip
import inspect
import math
import statistics
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import loglyte

if TYPE_CHECKING:
    from benchmarks.design import Design

#: Default floor on records per case. The time budget sets precision; the floor only
#: keeps very slow cases (about 1 ms per record) from being sized to a handful.
MIN_RECORDS = 1_000

#: Per-call latency columns: name -> quantile (nearest rank; 1.0 is the maximum).
PERCENTILES = {"p50": 0.50, "p99": 0.99, "p99.9": 0.999, "max": 1.0}

#: The three layers, in the order they run and are reported.
LAYERS = ("workload", "features", "micro")

#: (records, seconds the pass took) -> the (fewest, most) lines the case may write.
Expect = Callable[[int, float], tuple[int, int]]


def lines(per_record: float = 1) -> Expect:
    """Exactly ``per_record`` lines for every record."""

    def expect(records: int, elapsed_s: float) -> tuple[int, int]:
        count = round(records * per_record)
        return count, count

    return expect


#: Every record is discarded, so nothing is written.
NOTHING = lines(0)


@dataclass
class Run:
    """One prepared repetition of a case."""

    #: Emits one record (or one request); receives its index. May be ``async``.
    call: Callable[[int], Any]
    #: Must leave every record on disk when it returns.
    finish: Callable[[], None]
    #: Entered around the whole loop, e.g. a bound-context scope.
    scope: AbstractContextManager[Any] = field(default_factory=nullcontext)


@dataclass(frozen=True)
class Case:
    label: str
    #: Receives the log file path and returns a ready-to-time run.
    prepare: Callable[[str], Run]
    expect: Expect = lines()
    #: The fewest records that exercise the case's mechanics (e.g. enough batch flushes).
    min_records: int = MIN_RECORDS
    #: Count every file in the case directory, because the sink rotates on purpose.
    #: Otherwise only the log file itself counts, so an unexpected rotation fails.
    rotates: bool = False
    #: Text that must never reach the output, such as a value that should be redacted.
    must_not_contain: bytes | None = None


@dataclass(frozen=True)
class Group:
    name: str
    title: str
    #: The first case is the baseline the other rows are compared against.
    cases: tuple[Case, ...]
    layer: str = "micro"
    #: "libraries": one case per library. "breakdown": loglyte variants compared with
    #: the first row. "matrix": the runs of a designed experiment (see ``design``).
    kind: str = "libraries"
    threads: int = 1
    #: What one call of a case does, for the "ns/<unit>" label.
    unit: str = "record"
    #: Whether to run the per-call latency pass.
    latency: bool = True
    design: Design | None = None


@dataclass(frozen=True)
class Sizing:
    """How many records each case writes.

    By default the warm-up pass measures a case's cost per record, and the case is then
    sized so one timed pass lasts about ``budget_s``. A one-off cost per pass (starting
    or joining a thread, the final flush) inflates ns/record by roughly that cost divided
    by the pass duration, so an equal duration gives every case the same small bias.
    ``fixed`` replaces this with one count for every case (smoke tests, reproductions).
    """

    budget_s: float = 0.3
    max_records: int = 500_000
    fixed: int | None = None

    def warmup_records(self, case: Case) -> int:
        return self.fixed or case.min_records

    def records(self, case: Case, warmup_s: float, warmup_records: int) -> int:
        if self.fixed:
            return self.fixed
        per_record_s = warmup_s / warmup_records
        wanted = int(self.budget_s / per_record_s) if per_record_s > 0 else self.max_records
        return max(case.min_records, min(self.max_records, wanted))

    def describe(self) -> str:
        if self.fixed:
            return f"{self.fixed:,} records per case"
        return f"~{self.budget_s:g} s per timed pass ({MIN_RECORDS:,}-{self.max_records:,} records)"


@dataclass
class Result:
    group: str
    label: str
    records: int = 0
    times_s: list[float] = field(default_factory=list)
    latency_ns: dict[str, int] = field(default_factory=dict)
    lines: int = 0
    bytes_written: int = 0
    issues: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def best_ns(self) -> float | None:
        return min(self.times_s) / self.records * 1e9 if self.times_s else None

    @property
    def median_ns(self) -> float | None:
        return statistics.median(self.times_s) / self.records * 1e9 if self.times_s else None

    @property
    def spread(self) -> float | None:
        """How much slower the median run was than the best; high means a noisy machine."""
        if not self.times_s:
            return None
        return statistics.median(self.times_s) / min(self.times_s) - 1

    @property
    def bytes_per_record(self) -> float:
        return self.bytes_written / self.lines if self.lines else 0.0

    def note(self, issue: str) -> None:
        if issue not in self.issues:
            self.issues.append(issue)

    def to_json(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "best_ns": self.best_ns,
            "median_ns": self.median_ns,
            "spread": self.spread,
        }


def measure(group: Group, case: Case, sizing: Sizing, reps: int, workdir: Path) -> Result:
    """Size ``case`` with a warm-up pass, time it ``reps`` times, then profile each call.

    ``workdir`` belongs to the harness: it is emptied before every pass. Exceptions are
    recorded on the result rather than raised, so one broken case cannot end a long run.
    """
    threads = group.threads
    result = Result(group.name, case.label)
    path = workdir / "case.log"
    dropped_before, failures_before = _loglyte_health()

    try:
        warmup_records = _whole(sizing.warmup_records(case), threads)
        warmup_s = _timed_pass(case, warmup_records, threads, path, result)
        result.records = _whole(sizing.records(case, warmup_s, warmup_records), threads)
        for _ in range(reps):
            result.times_s.append(_timed_pass(case, result.records, threads, path, result))

        if group.latency and threads == 1:
            _clear(workdir)
            result.latency_ns = _latency_profile(case.prepare(str(path)), result.records)
    except Exception:
        result.error = traceback.format_exc()
    finally:
        _clear(workdir)

    dropped_after, failures_after = _loglyte_health()
    if dropped_after > dropped_before:
        result.note(f"loglyte dropped {dropped_after - dropped_before:,} records")
    if failures_after > failures_before:
        result.note(f"loglyte sink failed {failures_after - failures_before:,} times")
    return result


def _whole(records: int, threads: int) -> int:
    """Round down so every thread writes the same number of records."""
    return max(threads, records // threads * threads)


def _timed_pass(case: Case, records: int, threads: int, path: Path, result: Result) -> float:
    """One end-to-end pass: prepare, drive, finish. Then check what was written."""
    _clear(path.parent)
    run = case.prepare(str(path))
    gc.collect()
    started = time.perf_counter()
    _drive(run, records, threads)
    elapsed = time.perf_counter() - started

    output = _read_output(path, rotates=case.rotates)
    result.lines, result.bytes_written = output.count(b"\n"), _size_on_disk(path, case.rotates)
    fewest, most = case.expect(records, elapsed)
    if not fewest <= result.lines <= most:
        wanted = f"{fewest:,}" if fewest == most else f"{fewest:,}-{most:,}"
        result.note(f"wrote {result.lines:,} lines, expected {wanted}")
    if case.must_not_contain and case.must_not_contain in output:
        result.note(f"output contains {case.must_not_contain.decode()!r}, which must be hidden")
    return elapsed


def _drive(run: Run, records: int, threads: int) -> None:
    try:
        with run.scope:
            if threads == 1:
                _loop(run.call, records)
                return
            workers = [
                threading.Thread(target=_loop, args=(run.call, records // threads))
                for _ in range(threads)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
    finally:
        run.finish()


def _loop(call: Callable[[int], Any], count: int) -> None:
    if inspect.iscoroutinefunction(call):
        asyncio.run(_async_loop(call, count))
        return
    for i in range(count):
        call(i)


async def _async_loop(call: Callable[[int], Any], count: int) -> None:
    for i in range(count):
        await call(i)


def _latency_profile(run: Run, records: int) -> dict[str, int]:
    """Time every call individually; nearest-rank percentiles plus the maximum."""
    samples = [0] * records
    try:
        with run.scope:
            if inspect.iscoroutinefunction(run.call):
                asyncio.run(_async_samples(run.call, samples))
            else:
                _samples(run.call, samples)
    finally:
        run.finish()
    samples.sort()
    return {
        name: samples[max(0, math.ceil(quantile * records) - 1)]
        for name, quantile in PERCENTILES.items()
    }


def _samples(call: Callable[[int], Any], samples: list[int]) -> None:
    clock = time.perf_counter_ns
    for i in range(len(samples)):
        started = clock()
        call(i)
        samples[i] = clock() - started


async def _async_samples(call: Callable[[int], Any], samples: list[int]) -> None:
    clock = time.perf_counter_ns
    for i in range(len(samples)):
        started = clock()
        await call(i)
        samples[i] = clock() - started


def _log_files(path: Path, rotates: bool) -> list[Path]:
    if not rotates:
        return [path] if path.exists() else []
    # Every log file in the case directory; lock files hold no records.
    return sorted(p for p in path.parent.iterdir() if p.is_file() and p.suffix != ".lock")


def _read_output(path: Path, *, rotates: bool) -> bytes:
    """Everything the case wrote, decompressing rotated ``.gz`` files."""
    chunks = []
    for log_file in _log_files(path, rotates):
        data = log_file.read_bytes()
        chunks.append(gzip.decompress(data) if log_file.suffix == ".gz" else data)
    return b"".join(chunks)


def _size_on_disk(path: Path, rotates: bool) -> int:
    return sum(log_file.stat().st_size for log_file in _log_files(path, rotates))


def _clear(workdir: Path) -> None:
    """Empty the case directory: log files, rotated files, lock files, captures."""
    for leftover in workdir.iterdir():
        if leftover.is_file():
            leftover.unlink()


def _loglyte_health() -> tuple[int, int]:
    health = loglyte.SINK.health()
    return health["dropped_records_total"], health["sink_failures"]
