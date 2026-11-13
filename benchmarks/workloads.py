"""Layer 1: realistic workloads, run through every library.

Each profile is written once, as a script against ``Output``; every library implements
``Output`` in its own idiomatic way. So all libraries run exactly the same sequence of
log calls, and a profile's number is the cost of one unit of real work (a request, a
row) rather than of one isolated call.
"""

from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import structlog
from loguru import logger as loguru_logger

import loglyte
from benchmarks.adapters import (
    LOGLYTE_SETTINGS,
    LOGURU_FORMATS,
    LoglyteAdapter,
    fail,
    set_stdlib_switches,
    stdlib_formatter,
    structlog_processors,
)
from benchmarks.harness import MIN_RECORDS, Case, Expect, Group, Run, lines
from loglyte import logger
from loglyte.stdlib import install, uninstall

#: A value the web profile logs that every library must redact.
SECRET = "hunter2"
SECRET_FIELD = "password"


class Output(ABC):
    """What a profile script needs from a logging library."""

    @abstractmethod
    def debug(self, message: str, fields: dict[str, Any]) -> None: ...

    @abstractmethod
    def info(self, message: str, fields: dict[str, Any]) -> None: ...

    @abstractmethod
    def warning(self, message: str, fields: dict[str, Any]) -> None: ...

    @abstractmethod
    def exception(self, message: str, fields: dict[str, Any]) -> None:
        """Log at ERROR with the exception being handled."""

    @abstractmethod
    def context(self, **fields: Any) -> AbstractContextManager[Any]:
        """Attach ``fields`` to every record logged inside the scope."""

    @abstractmethod
    def finish(self) -> None: ...


# ---------------------------------------------------------------------------
# Profile scripts: identical for every library
# ---------------------------------------------------------------------------


def web_request(i: int, out: Output) -> None:
    """One API request: request context, three INFO lines, and a WARNING or, every
    twentieth request, a logged exception. One field must be redacted."""
    with out.context(request_id=f"req-{i}", user_id=f"usr-{i % 97}"):
        out.info("Request received", {"method": "POST", "path": "/orders"})
        out.info("Order validated", {"order_id": i, "items": 3, SECRET_FIELD: SECRET})
        out.info("Payment authorised", {"order_id": i, "amount_cents": 1999})
        if i % 20:
            out.warning("Slow inventory lookup", {"elapsed_ms": 250})
            return
        try:
            fail(i)
        except ValueError:
            out.exception("Inventory sync failed", {"order_id": i})


def batch_row(i: int, out: Output) -> None:
    """One row of an ETL job: two DEBUG lines (discarded in production) and one INFO."""
    out.debug("Parsing row", {"row": i})
    out.debug("Row parsed", {"row": i, "columns": 12})
    out.info(
        "Row loaded",
        {"row": i, "table": "orders", "bytes": 512, "partition": i % 16, "status": "ok"},
    )


# ---------------------------------------------------------------------------
# Outputs: one per library
# ---------------------------------------------------------------------------

_STDLIB_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("bench_context", default=None)


class _ContextFilter(logging.Filter):
    """The usual stdlib way to add request context: a contextvar read by a filter."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.fields = {**(_STDLIB_CONTEXT.get() or {}), **getattr(record, "fields", {})}
        return True


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        fields = getattr(record, "fields", None)
        if fields and SECRET_FIELD in fields:
            record.fields = {**fields, SECRET_FIELD: "***"}
        return True


class StdlibOutput(Output):
    def __init__(self, path: str, *, fmt: str, redact: bool = False):
        set_stdlib_switches()
        self.logger = logging.getLogger("bench.workload")
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        self.handler = logging.FileHandler(path, encoding="utf-8")
        self.handler.setFormatter(stdlib_formatter(fmt))
        self.handler.addFilter(_ContextFilter())
        if redact:
            self.handler.addFilter(_RedactFilter())
        self.logger.addHandler(self.handler)

    def debug(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.debug(message, extra={"fields": fields})

    def info(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.info(message, extra={"fields": fields})

    def warning(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.warning(message, extra={"fields": fields})

    def exception(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.exception(message, extra={"fields": fields})

    @contextmanager
    def context(self, **fields: Any) -> Iterator[None]:
        token = _STDLIB_CONTEXT.set({**(_STDLIB_CONTEXT.get() or {}), **fields})
        try:
            yield
        finally:
            _STDLIB_CONTEXT.reset(token)

    def finish(self) -> None:
        self.handler.close()
        self.logger.handlers.clear()


def _redact_loguru(record: dict[str, Any]) -> None:
    if SECRET_FIELD in record["extra"]:
        record["extra"][SECRET_FIELD] = "***"


class LoguruOutput(Output):
    def __init__(self, path: str, *, fmt: str, redact: bool = False, enqueue: bool = False):
        loguru_logger.remove()
        loguru_logger.add(
            path,
            format=LOGURU_FORMATS[fmt] if fmt != "iso" else LOGURU_FORMATS["iso"] + " {extra}",
            level="INFO",
            serialize=fmt == "json",
            enqueue=enqueue,
        )
        self.logger = loguru_logger.patch(_redact_loguru) if redact else loguru_logger

    def debug(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.debug(message, **fields)

    def info(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.info(message, **fields)

    def warning(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.warning(message, **fields)

    def exception(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.exception(message, **fields)

    def context(self, **fields: Any) -> AbstractContextManager[Any]:
        return loguru_logger.contextualize(**fields)

    def finish(self) -> None:
        loguru_logger.remove()


def _redact_structlog(_logger: Any, _method: str, event: dict[str, Any]) -> dict[str, Any]:
    if SECRET_FIELD in event:
        event[SECRET_FIELD] = "***"
    return event


class StructlogOutput(Output):
    def __init__(self, path: str, *, fmt: str, redact: bool = False):
        self.file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed by finish()
        processors = structlog_processors(fmt)
        if redact:
            processors.insert(-1, _redact_structlog)  # just before the renderer
        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
            logger_factory=structlog.PrintLoggerFactory(file=self.file),
            cache_logger_on_first_use=True,
        )
        self.logger = structlog.get_logger()

    def debug(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.debug(message, **fields)

    def info(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.info(message, **fields)

    def warning(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.warning(message, **fields)

    def exception(self, message: str, fields: dict[str, Any]) -> None:
        self.logger.exception(message, **fields)

    def context(self, **fields: Any) -> AbstractContextManager[Any]:
        return structlog.contextvars.bound_contextvars(**fields)

    def finish(self) -> None:
        self.file.close()
        structlog.reset_defaults()


class LoglyteOutput(Output):
    def __init__(self, path: str, *, fmt: str, redact: bool = False):
        sink = LoglyteAdapter(redact_fields=(SECRET_FIELD,) if redact else ()).sink(path, fmt=fmt)
        loglyte.configure([sink], **LOGLYTE_SETTINGS)

    def debug(self, message: str, fields: dict[str, Any]) -> None:
        logger.debug(message, **fields)

    def info(self, message: str, fields: dict[str, Any]) -> None:
        logger.info(message, **fields)

    def warning(self, message: str, fields: dict[str, Any]) -> None:
        logger.warning(message, **fields)

    def exception(self, message: str, fields: dict[str, Any]) -> None:
        logger.exception(message, **fields)

    def context(self, **fields: Any) -> AbstractContextManager[Any]:
        return logger.bind(**fields)

    def finish(self) -> None:
        if not logger.shutdown():
            raise RuntimeError("loglyte shutdown timed out")


#: Library name -> Output factory taking (path, fmt, redact).
OUTPUTS: dict[str, Callable[..., Output]] = {
    "stdlib": StdlibOutput,
    "loguru": LoguruOutput,
    "loguru-enqueue": lambda path, **kw: LoguruOutput(path, enqueue=True, **kw),
    "structlog": StructlogOutput,
    "loglyte": LoglyteOutput,
}


# ---------------------------------------------------------------------------
# The stdlib bridge profile: a dependency logs through stdlib ``logging``
# ---------------------------------------------------------------------------

_DEPENDENCY = logging.getLogger("bench.dependency")


def dependency_call(i: int) -> None:
    """What a third-party client library logs per request, via stdlib."""
    if i % 10:
        _DEPENDENCY.info("GET %s -> %d in %.1f ms", "/v1/items", 200, 12.5)
    else:
        _DEPENDENCY.warning("GET %s -> %d in %.1f ms (retrying)", "/v1/items", 503, 80.0)


class _InterceptHandler(logging.Handler):
    """Loguru's documented recipe for receiving stdlib records."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        loguru_logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _attach(handler: logging.Handler) -> Callable[[], None]:
    """Send the dependency's records to ``handler``; returns the matching cleanup."""
    set_stdlib_switches()
    _DEPENDENCY.handlers.clear()
    _DEPENDENCY.propagate = False
    _DEPENDENCY.setLevel(logging.INFO)
    _DEPENDENCY.addHandler(handler)

    def detach() -> None:
        handler.close()
        _DEPENDENCY.handlers.clear()

    return detach


def bridge_stdlib(path: str) -> Run:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(stdlib_formatter("iso"))
    return Run(call=dependency_call, finish=_attach(handler))


def bridge_loguru(path: str) -> Run:
    loguru_logger.remove()
    loguru_logger.add(path, format=LOGURU_FORMATS["iso"], level="INFO")
    detach = _attach(_InterceptHandler())

    def finish() -> None:
        detach()
        loguru_logger.remove()

    return Run(call=dependency_call, finish=finish)


def bridge_structlog(path: str) -> Run:
    """structlog's documented stdlib integration: a ProcessorFormatter on the handler."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.KeyValueRenderer(),
            ],
            foreign_pre_chain=[
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
            ],
        )
    )
    return Run(call=dependency_call, finish=_attach(handler))


def bridge_loglyte(path: str) -> Run:
    loglyte.configure([LoglyteAdapter().sink(path, fmt="iso")], **LOGLYTE_SETTINGS)
    set_stdlib_switches()
    _DEPENDENCY.handlers.clear()
    _DEPENDENCY.propagate = False
    handler = install("INFO", logger=_DEPENDENCY)

    def finish() -> None:
        uninstall(handler, logger=_DEPENDENCY)
        if not logger.shutdown():
            raise RuntimeError("loglyte shutdown timed out")

    return Run(call=dependency_call, finish=finish)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    unit: str
    script: Callable[[int, Output], None]
    fmt: str
    redact: bool = False
    expect: Expect = lines()
    libraries: tuple[str, ...] = ("stdlib", "loguru", "structlog", "loglyte")


PROFILES = (
    Profile(
        "web-request",
        "API request: context, 3 INFO + 1 WARNING or exception, redaction, JSON file",
        "request",
        web_request,
        fmt="json",
        redact=True,
        expect=lines(4),
        libraries=("stdlib", "loguru", "loguru-enqueue", "structlog", "loglyte"),
    ),
    Profile(
        "batch-job",
        "ETL row: 2 discarded DEBUG + 1 INFO with 5 fields, ISO-timestamped text",
        "row",
        batch_row,
        fmt="iso",
    ),
)

BRIDGES: dict[str, Callable[[str], Run]] = {
    "stdlib": bridge_stdlib,
    "loguru": bridge_loguru,
    "structlog": bridge_structlog,
    "loglyte": bridge_loglyte,
}


def _min_records(library: str) -> int:
    return LoglyteAdapter().min_records if library == "loglyte" else MIN_RECORDS


def _profile_case(profile: Profile, library: str) -> Case:
    def prepare(path: str) -> Run:
        out = OUTPUTS[library](path, fmt=profile.fmt, redact=profile.redact)
        return Run(call=lambda i: profile.script(i, out), finish=out.finish)

    return Case(
        library,
        prepare,
        expect=profile.expect,
        min_records=_min_records(library),
        must_not_contain=SECRET.encode() if profile.redact else None,
    )


def workload_groups() -> tuple[Group, ...]:
    profiles = tuple(
        Group(
            p.name,
            p.title,
            tuple(_profile_case(p, library) for library in p.libraries),
            layer="workload",
            unit=p.unit,
        )
        for p in PROFILES
    )
    bridge = Group(
        "stdlib-bridge",
        "a dependency logs via stdlib (10% warnings); each library receives and writes it",
        tuple(
            Case(
                name,
                build,
                min_records=_min_records(name),
            )
            for name, build in BRIDGES.items()
        ),
        layer="workload",
    )
    return (*profiles, bridge)
