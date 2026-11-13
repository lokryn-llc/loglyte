"""What is measured, in the order it runs: workloads, then features, then micro.

The micro layer holds what the other two cannot show: single call shapes compared
across libraries, costs too small to see inside a workload, and features that skip work
(level filters, sampling, rate limits) and so do not add up in the feature matrix.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from benchmarks.adapters import (
    ROTATE_BYTES,
    Adapter,
    LoglyteAdapter,
    StdlibAdapter,
    fail,
    libraries,
)
from benchmarks.features import feature_group
from benchmarks.harness import NOTHING, Case, Expect, Group, Run, lines
from benchmarks.workloads import workload_groups
from loglyte import catch, get_logger, logger

THREADS = 4
BOUND_FIELDS = {"request_id": "req_abc123", "user_id": "usr_774"}

CORE = ("stdlib", "loguru", "structlog", "loglyte")
#: Loguru's enqueue mode pays one queue handoff per record, which dominates its cost in
#: every scenario; it runs only where that handoff meets something new.
WITH_ENQUEUE = ("stdlib", "loguru", "loguru-enqueue", "structlog", "loglyte")
#: structlog rotates through stdlib handlers, so it has no rotation of its own to measure.
ROTATING = ("stdlib", "loguru", "loglyte")


# ---------------------------------------------------------------------------
# Cross-library micro scenarios
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """A call shape run through every library in ``libraries``."""

    name: str
    title: str
    method: str
    fmt: str = "text"
    rotation: str | None = None
    bound: bool = False
    threads: int = 1
    expect: Expect = lines()
    libraries: tuple[str, ...] = CORE
    #: Fewest records the scenario needs for its own mechanics, for every library.
    min_records: int = 0


#: About five rotations of a ~90-byte text record.
ROTATION_RECORDS = 5 * ROTATE_BYTES // 90

FIELDS = Scenario("fields", "message + 3 structured fields", "fields", libraries=WITH_ENQUEUE)

SCENARIOS = (
    Scenario("static", "constant message, no fields", "static"),
    FIELDS,
    Scenario("interpolated", "message built by an f-string before the call", "interpolated"),
    Scenario("filtered", "DEBUG call discarded by an INFO level", "filtered", expect=NOTHING),
    Scenario("bound", "fields + 2 context values bound around the loop", "fields", bound=True),
    Scenario(
        "threaded",
        f"fields from {THREADS} threads at once",
        "fields",
        threads=THREADS,
        libraries=WITH_ENQUEUE,
    ),
    Scenario(
        "json",
        "JSON: timestamp + level + message + 3 fields",
        "structured",
        fmt="json",
        libraries=WITH_ENQUEUE,
    ),
    Scenario("iso", "fields as text with an ISO-8601 timestamp", "fields", fmt="iso"),
    Scenario(
        "logfmt", "logfmt: timestamp + level + message + 3 fields", "structured", fmt="logfmt"
    ),
    Scenario("exceptions", "raise, catch, log with traceback, JSON", "exception", fmt="json"),
    Scenario(
        "rotation",
        "fields, size rotation every 256 KB, every file kept",
        "fields",
        rotation="size",
        libraries=ROTATING,
        min_records=ROTATION_RECORDS,
    ),
    Scenario(
        "rotation-gzip",
        "as rotation, each rotated file gzip-compressed",
        "fields",
        rotation="gzip",
        libraries=ROTATING,
        min_records=ROTATION_RECORDS,
    ),
)


def adapter_case(adapter: Adapter, scenario: Scenario) -> Case:
    def prepare(path: str) -> Run:
        adapter.setup(path, fmt=scenario.fmt, rotation=scenario.rotation)
        return Run(
            call=getattr(adapter, scenario.method),
            finish=adapter.finish,
            scope=adapter.bound(**BOUND_FIELDS) if scenario.bound else nullcontext(),
        )

    return Case(
        adapter.name,
        prepare,
        expect=scenario.expect,
        min_records=max(adapter.min_records, scenario.min_records),
        rotates=scenario.rotation is not None,
    )


def library_group(scenario: Scenario) -> Group:
    return Group(
        scenario.name,
        scenario.title,
        tuple(adapter_case(a, scenario) for a in libraries() if a.name in scenario.libraries),
        threads=scenario.threads,
    )


# ---------------------------------------------------------------------------
# loglyte breakdowns: call paths
# ---------------------------------------------------------------------------


def plain_fields(i: int) -> None:
    logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")


def empty_scope_fields(i: int) -> None:
    with logger.bind():
        logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")


def scope_fields(i: int) -> None:
    with logger.bind(**BOUND_FIELDS):
        logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")


def bound_logger_fields(i: int) -> None:
    logger.bind(**BOUND_FIELDS).info("Processing item", item_id=i, user_id="usr_9", region="eu")


@logger
def decorated_fields(i: int) -> None:
    logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")


@logger
def decorated_injected_fields(i: int, log: Any = None) -> None:
    log.info("Processing item", item_id=i, user_id="usr_9", region="eu")


@logger(bind=("i",))
def decorated_bind_fields(i: int) -> None:
    logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")


@logger.critical
def critical_fields(i: int) -> None:
    logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")


@logger
class _Service:
    def handle(self, i: int) -> None:
        logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")


_NAMED = get_logger("bench.named")


def named_logger_fields(i: int) -> None:
    _NAMED.info("Processing item", item_id=i, user_id="usr_9", region="eu")


def plain_filtered(i: int) -> None:
    logger.debug("Processing item", item_id=i)


@logger
def decorated_filtered(i: int) -> None:
    logger.debug("Processing item", item_id=i)


async def plain_async_fields(i: int) -> None:
    logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")


@logger
async def decorated_async_fields(i: int) -> None:
    logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")


# ---------------------------------------------------------------------------
# loglyte breakdowns: error boundaries
# ---------------------------------------------------------------------------


def manual_exception(i: int) -> None:
    try:
        fail(i)
    except ValueError:
        logger.exception("Processing failed", item_id=i)


@logger.critical
def _critical_failure(i: int) -> None:
    fail(i)


def critical_boundary(i: int) -> None:
    """@logger.critical logs and re-raises; the caller then handles the error."""
    with contextlib.suppress(ValueError):
        _critical_failure(i)


@catch(ValueError, reraise=False, message="Optional sync failed")
def catch_suppressed(i: int) -> None:
    fail(i)


# ---------------------------------------------------------------------------
# loglyte breakdowns: features that skip work
# ---------------------------------------------------------------------------

RATE_LIMIT = 20_000


def _sampled(rate: float) -> Expect:
    """A binomial count: within five standard deviations of ``records * rate``."""

    def expect(records: int, elapsed_s: float) -> tuple[int, int]:
        mean = records * rate
        spread = 5 * math.sqrt(records * rate * (1 - rate))
        return max(0, math.floor(mean - spread)), math.ceil(mean + spread)

    return expect


def _rate_limited(per_second: float) -> Expect:
    """At least one line; at most a full bucket plus what refilled during the pass."""
    return lambda records, elapsed_s: (1, min(records, math.ceil(per_second * (elapsed_s + 1))))


def _even_items(records: int, elapsed_s: float) -> tuple[int, int]:
    return (records + 1) // 2, (records + 1) // 2


def _keep_even(record: Any) -> bool:
    return record[4]["item_id"] % 2 == 0


def _redact_user(key: str, value: Any) -> Any:
    return "***" if key == "user_id" else value


def loglyte_case(
    label: str,
    call: Callable[[int], Any],
    adapter: LoglyteAdapter | None = None,
    *,
    fmt: str = "text",
    **case_options: Any,
) -> Case:
    adapter = adapter or LoglyteAdapter()

    def prepare(path: str) -> Run:
        adapter.setup(path, fmt=fmt)
        return Run(call=call, finish=adapter.finish)

    return Case(label, prepare, min_records=adapter.min_records, **case_options)


def breakdown(name: str, title: str, *cases: Case) -> Group:
    return Group(name, title, cases, kind="breakdown")


def breakdown_groups() -> tuple[Group, ...]:
    stdlib_knobs = (
        StdlibAdapter("stdlib (default)"),
        StdlibAdapter("stdlib, no caller lookup", introspect=False),
        StdlibAdapter("stdlib, no caller/thread info", introspect=False, thread_info=False),
        StdlibAdapter(
            "stdlib, no caller/thread info, batched",
            introspect=False,
            thread_info=False,
            batched=True,
        ),
        LoglyteAdapter(),
    )
    hidden = b"usr_9"
    return (
        breakdown(
            "ablation",
            "stdlib with optional work switched off cumulatively, vs loglyte (fields, text)",
            *(adapter_case(a, FIELDS) for a in stdlib_knobs),
        ),
        breakdown(
            "callpath",
            "loglyte call paths; every call writes the fields record",
            loglyte_case("plain function -> logger.info", plain_fields),
            loglyte_case("get_logger(name).info", named_logger_fields),
            loglyte_case("with logger.bind(): (empty), per call", empty_scope_fields),
            loglyte_case("with logger.bind(2 fields):, per call", scope_fields),
            loglyte_case("logger.bind(2 fields).info(), per call", bound_logger_fields),
            loglyte_case("@logger function", decorated_fields),
            loglyte_case("@logger function, injected log", decorated_injected_fields),
            loglyte_case("@logger(bind=('i',)) function", decorated_bind_fields),
            loglyte_case("@logger.critical function, no error", critical_fields),
            loglyte_case("@logger class, method call", _Service().handle),
        ),
        breakdown(
            "callpath-filtered",
            "loglyte call paths when the DEBUG record is discarded",
            loglyte_case("plain function -> logger.debug", plain_filtered, expect=NOTHING),
            loglyte_case("@logger function -> logger.debug", decorated_filtered, expect=NOTHING),
        ),
        breakdown(
            "callpath-async",
            "loglyte from async functions, awaited in an event loop",
            loglyte_case("async function -> logger.info", plain_async_fields),
            loglyte_case("@logger async function", decorated_async_fields),
        ),
        breakdown(
            "error-boundary",
            "raise, catch and log an exception with traceback (JSON)",
            loglyte_case("try/except + logger.exception", manual_exception, fmt="json"),
            loglyte_case("@logger.critical, re-raised", critical_boundary, fmt="json"),
            loglyte_case("catch(reraise=False), suppressed", catch_suppressed, fmt="json"),
        ),
        breakdown(
            "sink-control",
            "sink features applied while dispatching (fields, text)",
            loglyte_case("no sink options", plain_fields),
            loglyte_case(
                "redact_fields=('user_id',)",
                plain_fields,
                LoglyteAdapter(redact_fields=("user_id",)),
                must_not_contain=hidden,
            ),
            loglyte_case(
                "custom redactor",
                plain_fields,
                LoglyteAdapter(redactor=_redact_user),
                must_not_contain=hidden,
            ),
            loglyte_case(
                "filter keeps even item_ids",
                plain_fields,
                LoglyteAdapter(filters=(_keep_even,)),
                expect=_even_items,
            ),
            loglyte_case(
                "sample=0.5", plain_fields, LoglyteAdapter(sample=0.5), expect=_sampled(0.5)
            ),
            loglyte_case(
                f"rate_limit={RATE_LIMIT:,}/s",
                plain_fields,
                LoglyteAdapter(rate_limit=RATE_LIMIT),
                expect=_rate_limited(RATE_LIMIT),
            ),
        ),
        breakdown(
            "flush",
            "dispatcher settings (fields, text); watch p99.9/max for the batch flush",
            loglyte_case("defaults (threshold 1,000, interval 0.5 s)", plain_fields),
            *(
                loglyte_case(
                    f"flush_threshold={threshold:,}",
                    plain_fields,
                    LoglyteAdapter(settings={"flush_threshold": threshold}),
                )
                for threshold in (100, 10_000)
            ),
            loglyte_case(
                "flush_interval=0 (no background thread)",
                plain_fields,
                LoglyteAdapter(settings={"flush_interval": 0}),
            ),
        ),
    )


def build_groups() -> tuple[Group, ...]:
    overhead = Group(
        "overhead",
        "the harness itself: an empty call per record",
        (Case("empty loop", lambda path: Run(_Noop().call, _Noop.finish), expect=NOTHING),),
        kind="breakdown",
    )
    return (
        *workload_groups(),
        feature_group(),
        overhead,
        *(library_group(s) for s in SCENARIOS),
        *breakdown_groups(),
    )


class _Noop:
    """A bound-method call, like the adapters' calls, that does nothing."""

    def call(self, i: int) -> None:
        pass

    @staticmethod
    def finish() -> None:
        pass


GROUP_NAMES = tuple(group.name for group in build_groups())
