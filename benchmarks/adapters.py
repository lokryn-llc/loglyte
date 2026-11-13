"""How each library is configured and called for the micro scenarios.

Every adapter writes to a file in any of ``FORMATS`` and implements the same call
shapes, so a scenario runs unchanged through every library. The logging configuration
helpers here (formatters, stdlib switches, loglyte settings) are shared with
``workloads``.
"""

from __future__ import annotations

import gzip
import json
import logging
import logging.handlers
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

import structlog
from loguru import logger as loguru_logger

import loglyte
from benchmarks.harness import MIN_RECORDS
from loglyte import FileSink, logger

#: Set explicitly on every run so results never depend on an ambient loglyte.toml.
#: These equal the library defaults, so they measure what users get out of the box.
LOGLYTE_SETTINGS: dict[str, Any] = {
    "flush_threshold": 1_000,
    "flush_interval": 0.5,
    "overflow_policy": "drop_oldest",
    "failure_policy": "continue",
    "retry_attempts": 0,
    "retry_backoff": 0.05,
}

#: text: level, logger and message. iso: text with an ISO-8601 timestamp.
#: json and logfmt: timestamp, level, logger, message and the structured fields.
FORMATS = ("text", "iso", "json", "logfmt")

#: The rotation scenarios rotate this often, so every pass rotates many times.
ROTATE_BYTES = 256 * 1024

#: stdlib module-level switches the ablation group changes; restored on exit.
_STDLIB_DEFAULTS = {
    name: getattr(logging, name)
    for name in ("_srcfile", "logThreads", "logProcesses", "logMultiprocessing", "logAsyncioTasks")
    if hasattr(logging, name)  # logAsyncioTasks is 3.12+
}


def set_stdlib_switches(*, introspect: bool = True, thread_info: bool = True) -> None:
    """``introspect`` controls caller file/line lookup; ``thread_info`` the rest."""
    for name, default in _STDLIB_DEFAULTS.items():
        if name == "_srcfile":
            logging._srcfile = default if introspect else None
        else:
            setattr(logging, name, thread_info)


def restore_stdlib_switches() -> None:
    for name, value in _STDLIB_DEFAULTS.items():
        setattr(logging, name, value)


def fail(i: int) -> None:
    """The exception every exception scenario raises and logs."""
    raise ValueError(f"item {i} is invalid")


# ---------------------------------------------------------------------------
# stdlib formatters: structured fields travel in ``extra={"fields": {...}}``
# ---------------------------------------------------------------------------


def _payload(record: logging.LogRecord) -> dict[str, Any]:
    return {
        "timestamp": record.created,
        "level": record.levelname,
        "logger": record.funcName,
        "message": record.getMessage(),
        **getattr(record, "fields", {}),
    }


class JsonFormatter(logging.Formatter):
    """Minimal equivalent of what python-json-logger emits."""

    def format(self, record: logging.LogRecord) -> str:
        payload = _payload(record)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class LogfmtFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return " ".join(f"{k}={json.dumps(v, default=str)}" for k, v in _payload(record).items())


class IsoTextFormatter(logging.Formatter):
    """ISO-8601 timestamp, level, logger, message, then any fields as key=value."""

    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03d"

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None)
        tail = "".join(f" {k}={v}" for k, v in fields.items()) if fields else ""
        line = f"{self.formatTime(record)} {record.levelname} {record.name}: {record.getMessage()}"
        return line + tail


def stdlib_formatter(fmt: str) -> logging.Formatter:
    if fmt == "text":
        return logging.Formatter("%(levelname)s %(funcName)s: %(message)s")
    return {"iso": IsoTextFormatter, "json": JsonFormatter, "logfmt": LogfmtFormatter}[fmt]()


def _gzip_rotator(source: str, dest: str) -> None:
    """The logging cookbook's recipe for compressing rotated files."""
    with open(source, "rb") as plain, gzip.open(dest, "wb") as packed:
        shutil.copyfileobj(plain, packed)
    os.remove(source)


def stdlib_file_handler(path: str, rotation: str | None) -> logging.Handler:
    if rotation is None:
        return logging.FileHandler(path, encoding="utf-8")
    # A high backupCount keeps every rotated file so records can be counted; stdlib
    # checks each backup name on rollover, which is part of its real cost.
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=ROTATE_BYTES, backupCount=500, encoding="utf-8"
    )
    if rotation == "gzip":
        handler.namer = lambda name: f"{name}.gz"
        handler.rotator = _gzip_rotator
    return handler


# ---------------------------------------------------------------------------
# structlog and Loguru configuration
# ---------------------------------------------------------------------------


def structlog_processors(fmt: str) -> list[Any]:
    """Context, level, then the renderer for ``fmt``."""
    common = [structlog.contextvars.merge_contextvars, structlog.processors.add_log_level]
    iso = structlog.processors.TimeStamper(fmt="iso")
    renderer = {
        "text": [structlog.processors.KeyValueRenderer()],
        "iso": [iso, structlog.processors.KeyValueRenderer()],
        "json": [
            iso,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        "logfmt": [structlog.processors.TimeStamper(), structlog.processors.LogfmtRenderer()],
    }[fmt]
    return [*common, *renderer]


LOGURU_FORMATS = {
    "text": "{level} {function}: {message}",
    "iso": "{time:YYYY-MM-DDTHH:mm:ss.SSSZZ} {level} {function}: {message}",
    "json": "{message}",  # serialize=True writes the whole record as JSON
    "logfmt": (
        'timestamp={time:X.SSSSSS} level={level} logger={function} message="{message}" '
        "item_id={extra[item_id]} user_id={extra[user_id]} region={extra[region]}"
    ),
}


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class Adapter(ABC):
    """The call shapes every library implements. Each takes the record index ``i``."""

    name: str
    #: Fewest records per case for this library; see ``harness.Sizing``.
    min_records: int = MIN_RECORDS

    @abstractmethod
    def setup(self, path: str, *, fmt: str = "text", rotation: str | None = None) -> None:
        """``rotation``: None, "size" (every ``ROTATE_BYTES``) or "gzip" (size + gzip)."""

    @abstractmethod
    def finish(self) -> None:
        """Flush and close; every record must be on disk when this returns."""

    @abstractmethod
    def static(self, i: int) -> None: ...

    @abstractmethod
    def fields(self, i: int) -> None:
        """Message plus three values, in each library's idiomatic form."""

    @abstractmethod
    def interpolated(self, i: int) -> None: ...

    @abstractmethod
    def filtered(self, i: int) -> None: ...

    @abstractmethod
    def exception(self, i: int) -> None:
        """Raise, catch and log an exception with its traceback."""

    def structured(self, i: int) -> None:
        """The three values as structured data, for JSON and logfmt output."""
        self.fields(i)

    def bound(self, **fields: Any) -> AbstractContextManager[Any]:
        """Bind context for a scope. Libraries without context binding do nothing."""
        return nullcontext()


class StdlibAdapter(Adapter):
    """stdlib ``logging``; the switches exist for the ablation group."""

    def __init__(
        self,
        name: str = "stdlib",
        *,
        introspect: bool = True,
        thread_info: bool = True,
        batched: bool = False,
    ):
        self.name = name
        self.introspect = introspect
        self.thread_info = thread_info
        self.batched = batched
        self.logger = logging.getLogger("bench.stdlib")
        self.handlers: list[logging.Handler] = []

    def setup(self, path: str, *, fmt: str = "text", rotation: str | None = None) -> None:
        set_stdlib_switches(introspect=self.introspect, thread_info=self.thread_info)
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        file_handler = stdlib_file_handler(path, rotation)
        file_handler.setFormatter(stdlib_formatter(fmt))
        # MemoryHandler.close() flushes into its target, so it must close first.
        self.handlers = [file_handler]
        if self.batched:
            self.handlers.insert(
                0,
                logging.handlers.MemoryHandler(
                    capacity=1_000, flushLevel=logging.CRITICAL, target=file_handler
                ),
            )
        self.logger.addHandler(self.handlers[0])

    def finish(self) -> None:
        for handler in self.handlers:
            handler.close()
        self.logger.handlers.clear()

    def static(self, i: int) -> None:
        self.logger.info("Static message")

    def fields(self, i: int) -> None:
        self.logger.info("Processing item item_id=%s user_id=%s region=%s", i, "usr_9", "eu")

    def interpolated(self, i: int) -> None:
        self.logger.info(f"Processing item {i}")

    def filtered(self, i: int) -> None:
        self.logger.debug("Processing item item_id=%s", i)

    def exception(self, i: int) -> None:
        try:
            fail(i)
        except ValueError:
            self.logger.exception("Processing failed", extra={"fields": {"item_id": i}})

    def structured(self, i: int) -> None:
        self.logger.info(
            "Processing item",
            extra={"fields": {"item_id": i, "user_id": "usr_9", "region": "eu"}},
        )


class LoguruAdapter(Adapter):
    def __init__(self, *, enqueue: bool = False):
        self.name = "loguru-enqueue" if enqueue else "loguru"
        self.enqueue = enqueue

    def setup(self, path: str, *, fmt: str = "text", rotation: str | None = None) -> None:
        loguru_logger.remove()
        loguru_logger.add(
            path,
            format=LOGURU_FORMATS[fmt],
            level="INFO",
            enqueue=self.enqueue,
            serialize=fmt == "json",
            rotation=ROTATE_BYTES if rotation else None,
            compression="gz" if rotation == "gzip" else None,
        )

    def finish(self) -> None:
        loguru_logger.remove()  # joins the enqueue worker and closes the file

    def static(self, i: int) -> None:
        loguru_logger.info("Static message")

    def fields(self, i: int) -> None:
        loguru_logger.info("Processing item item_id={} user_id={} region={}", i, "usr_9", "eu")

    def interpolated(self, i: int) -> None:
        loguru_logger.info(f"Processing item {i}")

    def filtered(self, i: int) -> None:
        loguru_logger.debug("Processing item item_id={}", i)

    def exception(self, i: int) -> None:
        try:
            fail(i)
        except ValueError:
            loguru_logger.exception("Processing failed", item_id=i)

    def structured(self, i: int) -> None:
        loguru_logger.bind(item_id=i, user_id="usr_9", region="eu").info("Processing item")

    def bound(self, **fields: Any) -> AbstractContextManager[Any]:
        return loguru_logger.contextualize(**fields)


class StructlogAdapter(Adapter):
    """structlog has no file rotation of its own, so it skips the rotation scenarios."""

    name = "structlog"

    def setup(self, path: str, *, fmt: str = "text", rotation: str | None = None) -> None:
        if rotation:
            raise ValueError("structlog rotates through stdlib handlers; not benchmarked")
        self.file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed by finish()
        structlog.configure(
            processors=structlog_processors(fmt),
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
            logger_factory=structlog.PrintLoggerFactory(file=self.file),
            cache_logger_on_first_use=True,
        )
        self.logger = structlog.get_logger()

    def finish(self) -> None:
        self.file.close()
        structlog.reset_defaults()

    def static(self, i: int) -> None:
        self.logger.info("Static message")

    def fields(self, i: int) -> None:
        self.logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")

    def interpolated(self, i: int) -> None:
        self.logger.info(f"Processing item {i}")

    def filtered(self, i: int) -> None:
        self.logger.debug("Processing item", item_id=i)

    def exception(self, i: int) -> None:
        try:
            fail(i)
        except ValueError:
            self.logger.exception("Processing failed", item_id=i)

    def bound(self, **fields: Any) -> AbstractContextManager[Any]:
        return structlog.contextvars.bound_contextvars(**fields)


class LoglyteAdapter(Adapter):
    """The global ``logger``: the path the README recommends for application code.

    ``settings`` overrides dispatcher settings and ``sink_options`` are passed to the
    ``FileSink``, for the breakdown groups that vary them.
    """

    def __init__(
        self,
        name: str = "loglyte",
        *,
        settings: dict[str, Any] | None = None,
        filters: Iterable[Any] = (),
        min_records: int | None = None,
        **sink_options: Any,
    ):
        self.name = name
        self.settings = {**LOGLYTE_SETTINGS, **(settings or {})}
        self.filters = tuple(filters)
        self.sink_options = sink_options
        #: Five batch flushes, so batching is measured, not just the final drain.
        #: Cheap cases get far more from the time budget anyway.
        self.min_records = min_records or 5 * self.settings["flush_threshold"]

    def setup(self, path: str, *, fmt: str = "text", rotation: str | None = None) -> None:
        loglyte.configure([self.sink(path, fmt=fmt, rotation=rotation)], **self.settings)

    def sink(self, path: str, *, fmt: str = "text", rotation: str | None = None) -> FileSink:
        # max_bytes=0 unless rotating: the default (10 MB) would rotate mid-pass on
        # larger cases and time rotation as if it were logging.
        sink = FileSink(
            path,
            level="INFO",
            render_mode="text" if fmt == "iso" else fmt,
            timestamp_mode="iso" if fmt == "iso" else "none",
            max_bytes=ROTATE_BYTES if rotation else 0,
            retention_count=None,
            compression="gzip" if rotation == "gzip" else None,
            **self.sink_options,
        )
        for filter_ in self.filters:
            sink.add_filter(filter_)
        return sink

    def finish(self) -> None:
        if not logger.shutdown():
            raise RuntimeError("loglyte shutdown timed out")

    def static(self, i: int) -> None:
        logger.info("Static message")

    def fields(self, i: int) -> None:
        logger.info("Processing item", item_id=i, user_id="usr_9", region="eu")

    def interpolated(self, i: int) -> None:
        logger.info(f"Processing item {i}")

    def filtered(self, i: int) -> None:
        logger.debug("Processing item", item_id=i)

    def exception(self, i: int) -> None:
        try:
            fail(i)
        except ValueError:
            logger.exception("Processing failed", item_id=i)

    def bound(self, **fields: Any) -> AbstractContextManager[Any]:
        return logger.bind(**fields)


def libraries() -> tuple[Adapter, ...]:
    """stdlib first: it is the baseline of every cross-library group."""
    return (
        StdlibAdapter(),
        LoguruAdapter(),
        LoguruAdapter(enqueue=True),
        StructlogAdapter(),
        LoglyteAdapter(),
    )
