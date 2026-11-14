from __future__ import annotations

import atexit
import contextlib
import gzip
import json
import math
import os
import random
import re
import shutil
import sys
import threading
import time
import traceback
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

try:  # pragma: no cover - Windows is covered in CI, Unix locally
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - Unix locally
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]

LEVEL_MAP = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

_OVERFLOW_POLICIES = frozenset({"drop_oldest", "drop_newest"})

#: A rotated log is ``<log path>.<timestamp>.<token>``, plus ``.gz`` when compressed.
#: The timestamp keeps a directory listing readable and sorted; the token makes the
#: name unique, since ``strftime`` cannot distinguish two rotations in one second.
_ROTATION_STAMP = "%Y-%m-%d_%H-%M-%S"
_ROTATION_TOKEN_BYTES = 4
_ROTATION_CLAIM_ATTEMPTS = 8

#: Matches the names above, and the ``<timestamp>[.<counter>]`` form written before
#: tokens existed, so archives from an older loglyte are still pruned after an upgrade.
#: Anchored and fully specified: an operator's ``app.log.backup`` must never match.
_ROTATED_SUFFIX = (
    r"\.\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}"  # timestamp
    r"(?:\.[0-9a-f]{1,8})?"  # token, or a legacy counter
    r"(?:\.gz)?"  # compressed, or a leftover pair from an interrupted compression
)
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_LOCK = threading.Lock()

#: Sentinel meaning "no sink will ever accept a record", used when no sinks are configured.
_LEVEL_NEVER = 1 << 30

#: Fields the formatter owns. A user-supplied ``extra`` key that collides with one of
#: these is emitted under ``extra_<key>`` rather than silently overwriting it.
_RESERVED_FIELDS = frozenset({"timestamp", "level", "logger", "message"})

#: A log record: (timestamp, level, func_name, message, extra).
Record = tuple[float, str, str, str, dict[str, Any]]
Renderer = Callable[[float, str, str, str, dict[str, Any]], str]
SinkFilter = Callable[[Record], bool]
Redactor = Callable[[str, Any], Any]


def level_value(level: str) -> int:
    """Numeric severity for ``level``; unknown levels sort below everything."""
    return LEVEL_MAP.get(level, 0)


def _validated_level(level: str) -> int:
    """Return ``level``'s numeric value, rejecting configuration typos."""
    if not isinstance(level, str) or level.upper() not in LEVEL_MAP:
        choices = ", ".join(LEVEL_MAP)
        raise ValueError(f"Unknown log level {level!r}; expected one of: {choices}")
    return LEVEL_MAP[level.upper()]


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
    return value


def _nonnegative_interval(name: str, value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite, non-negative number, got {value!r}")
    return float(value)


def _validated_overflow_policy(value: str) -> str:
    if value not in _OVERFLOW_POLICIES:
        choices = ", ".join(sorted(_OVERFLOW_POLICIES))
        raise ValueError(f"Unknown overflow_policy {value!r}; expected one of: {choices}")
    return value


def _report_internal_error(context: str) -> None:
    """Report a loglyte-internal failure to stderr without raising.

    A logging library must never take down the calling application, and must never
    let one misbehaving sink or record destroy a whole batch.
    """
    with contextlib.suppress(Exception):
        sys.stderr.write(f"--- loglyte internal error ({context}) ---\n")
        traceback.print_exc(file=sys.stderr)


def _remove_quietly(paths: Iterable[str]) -> None:
    """Delete paths, ignoring the ones that are already gone."""
    for path in paths:
        with contextlib.suppress(OSError):
            os.remove(path)


def _write_all(stream: Any, payload: str) -> None:
    """Write a complete text payload even when a custom stream writes partially."""
    remaining = payload
    while remaining:
        written = stream.write(remaining)
        if written is None:
            return
        if not isinstance(written, int) or written <= 0:
            raise OSError("stream write made no progress")
        remaining = remaining[written:]


class BaseSink(ABC):
    """Abstract Base Class for loglyte sinks."""

    __slots__ = (
        "min_level",
        "json_mode",
        "timestamp_mode",
        "_filters",
        "_renderer",
        "_redactor",
        "_sample_rate",
        "_rate_limit",
        "_rate_tokens",
        "_rate_updated_at",
    )

    def __init__(
        self,
        level: str = "INFO",
        json_mode: bool = False,
        *,
        render_mode: str | None = None,
        renderer: Renderer | None = None,
        timestamp_mode: str = "none",
        redact_fields: Iterable[str] = (),
        redactor: Redactor | None = None,
        sample: float = 1.0,
        rate_limit: float | None = None,
    ):
        self.min_level = _validated_level(level)
        if render_mode is None:
            render_mode = "json" if json_mode else "text"
        if render_mode not in {"text", "json", "logfmt"}:
            raise ValueError("render_mode must be 'text', 'json', or 'logfmt'")
        if json_mode and render_mode != "json":
            raise ValueError("json_mode=True cannot be combined with another render_mode")
        if renderer is not None and not callable(renderer):
            raise TypeError("renderer must be callable")
        if timestamp_mode not in {"none", "iso"}:
            raise ValueError("timestamp_mode must be 'none' or 'iso'")
        if redactor is not None and not callable(redactor):
            raise TypeError("redactor must be callable")
        if not isinstance(sample, (int, float)) or isinstance(sample, bool) or not 0 <= sample <= 1:
            raise ValueError(f"sample must be a number from 0 to 1, got {sample!r}")
        if rate_limit is not None and (
            not isinstance(rate_limit, (int, float))
            or isinstance(rate_limit, bool)
            or not math.isfinite(rate_limit)
            or rate_limit <= 0
        ):
            raise ValueError(f"rate_limit must be a positive finite number, got {rate_limit!r}")
        self.json_mode = render_mode == "json"
        self.timestamp_mode = timestamp_mode
        self._sample_rate = float(sample)
        self._rate_limit = float(rate_limit) if rate_limit is not None else None
        self._rate_tokens = self._rate_limit if self._rate_limit is not None else 0.0
        self._rate_updated_at = time.monotonic()
        self._renderer = renderer or (
            _render_logfmt
            if render_mode == "logfmt"
            else _render_text_iso
            if render_mode == "text" and timestamp_mode == "iso"
            else None
        )
        self._filters: list[SinkFilter] = []
        sensitive = frozenset(redact_fields)
        if redactor is not None:
            self._redactor = lambda fields: {
                key: redactor(key, value) for key, value in fields.items()
            }
        elif sensitive:
            self._redactor = lambda fields: {
                key: "***" if key in sensitive else value for key, value in fields.items()
            }
        else:
            self._redactor = None

    @abstractmethod
    def write_batch(self, items: list[Record]) -> None:
        raise NotImplementedError

    def _accepts(self, level: str) -> bool:
        return level_value(level) >= self.min_level

    def add_filter(self, filter_: SinkFilter) -> None:
        """Run ``filter_`` during dispatch; false results omit that record here."""
        if not callable(filter_):
            raise TypeError("filter must be callable")
        self._filters.append(filter_)

    def remove_filter(self, filter_: SinkFilter) -> None:
        """Remove a previously registered per-sink filter if present."""
        with contextlib.suppress(ValueError):
            self._filters.remove(filter_)

    def _allows_delivery(self) -> bool:
        """Sample and rate-limit on the dispatcher, never on producer calls."""
        if self._sample_rate < 1 and random.random() >= self._sample_rate:
            return False
        if self._rate_limit is None:
            return True
        now = time.monotonic()
        self._rate_tokens = min(
            self._rate_limit,
            self._rate_tokens + (now - self._rate_updated_at) * self._rate_limit,
        )
        self._rate_updated_at = now
        if self._rate_tokens < 1:
            return False
        self._rate_tokens -= 1
        return True

    @staticmethod
    def _colourise(level: str, payload: str) -> str:
        colours = {
            "DEBUG": "\x1b[36m",
            "INFO": "\x1b[32m",
            "WARNING": "\x1b[33m",
            "ERROR": "\x1b[31m",
            "CRITICAL": "\x1b[1;31m",
        }
        return f"{colours.get(level, '')}{payload}\x1b[0m" if level in colours else payload

    def _render(self, items: list[Record], *, colour: bool = False) -> str:
        """Format every accepted record in ``items`` into a single string.

        A record that fails to format is reported to stderr and skipped, so one bad
        value cannot discard the rest of the batch.
        """
        formatted = []
        renderer = self._renderer or self._format_item
        if not self._filters and self._redactor is None:
            for ts, level, func, msg, extra in items:
                if not self._accepts(level) or not self._allows_delivery():
                    continue
                try:
                    item = renderer(ts, level, func, msg, extra)
                    formatted.append(self._colourise(level, item) if colour else item)
                except Exception:
                    _report_internal_error(f"formatting {level} record from {func!r}")
            return "".join(formatted)

        for item in items:
            ts, level, func, msg, extra = item
            if not self._accepts(level) or not self._allows_delivery():
                continue
            try:
                if all(filter_(item) for filter_ in self._filters):
                    fields = self._redactor(extra) if self._redactor is not None else extra
                    rendered = renderer(ts, level, func, msg, fields)
                    formatted.append(self._colourise(level, rendered) if colour else rendered)
            except Exception:
                _report_internal_error(f"filtering or formatting {level} record from {func!r}")
        return "".join(formatted)

    def _format_item(
        self,
        ts: float,
        level: str,
        func_name: str,
        message: str,
        extra: dict[str, Any],
    ) -> str:
        if self.json_mode:
            payload: dict[str, Any] = {
                "timestamp": ts,
                "level": level,
                "logger": func_name,
                "message": message,
            }
            for key, value in extra.items():
                payload[f"extra_{key}" if key in _RESERVED_FIELDS else key] = value
            # default=str keeps a non-serializable value from destroying the batch.
            return json.dumps(payload, default=str) + "\n"

        extra_str = f" | {extra}" if extra else ""
        return f"[{level}] {func_name}: {message}{extra_str}\n"


def _render_text_iso(
    ts: float, level: str, func_name: str, message: str, extra: dict[str, Any]
) -> str:
    timestamp = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")
    timestamp = timestamp.replace("+00:00", "Z")
    extra_str = f" | {extra}" if extra else ""
    return f"{timestamp} [{level}] {func_name}: {message}{extra_str}\n"


def _render_logfmt(
    ts: float, level: str, func_name: str, message: str, extra: dict[str, Any]
) -> str:
    payload = {
        "timestamp": ts,
        "level": level,
        "logger": func_name,
        "message": message,
        **{
            f"extra_{key}" if key in _RESERVED_FIELDS else key: value
            for key, value in extra.items()
        },
    }
    return (
        " ".join(f"{key}={json.dumps(value, default=str)}" for key, value in payload.items()) + "\n"
    )


class StreamSink(BaseSink):
    """Writes to a text stream.

    The stream is resolved per batch rather than captured at construction, so
    reassigning ``sys.stdout``/``sys.stderr`` (test capture, redirection, a
    supervisor rebinding them) is honoured.
    """

    __slots__ = ("_colour",)

    def __init__(self, *args: Any, color: str | bool = "auto", **kwargs: Any):
        super().__init__(*args, **kwargs)
        if color is True:
            color = "always"
        elif color is False:
            color = "never"
        if color not in {"auto", "always", "never"}:
            raise ValueError("color must be 'auto', 'always', or 'never'")
        if self.json_mode or self._renderer is _render_logfmt:
            if color == "always":
                raise ValueError("color is only available with text output")
            color = "never"
        self._colour = color

    def _stream(self) -> Any:
        raise NotImplementedError

    def write_batch(self, items: list[Record]) -> None:
        stream = self._stream()
        colour = self._colour == "always" or (
            self._colour == "auto" and bool(getattr(stream, "isatty", lambda: False)())
        )
        payload = self._render(items, colour=colour)
        if payload:
            _write_all(stream, payload)
            stream.flush()


class StdoutSink(StreamSink):
    """Writes to ``sys.stdout``."""

    __slots__ = ()

    def _stream(self) -> Any:
        return sys.stdout


class StderrSink(StreamSink):
    """Writes to ``sys.stderr``.

    Conventional for diagnostics: it keeps log output from interleaving with a
    program's actual stdout, which matters for anything piped or parsed.
    """

    __slots__ = ()

    def _stream(self) -> Any:
        return sys.stderr


class FileSink(BaseSink):
    """Buffered file sink with size/time-based rotation and retention cleanup.

    ``process_safe=True`` serialises complete batches and rotation through a small
    sidecar lock file. It is intended for several Python processes writing the same
    file; leave it off for the lowest-latency single-process path.
    """

    __slots__ = (
        "file_path",
        "max_bytes",
        "rotation_seconds",
        "retention_count",
        "compression",
        "process_safe",
        "_last_rotation_time",
        "_rotated_name",
    )

    def __init__(
        self,
        file_path: str,
        level: str = "INFO",
        json_mode: bool = False,
        max_bytes: int = 10_000_000,
        rotation_seconds: int | None = None,
        retention_count: int | None = 5,
        process_safe: bool = False,
        compression: str | None = None,
        *,
        render_mode: str | None = None,
        renderer: Renderer | None = None,
        timestamp_mode: str = "none",
        redact_fields: Iterable[str] = (),
        redactor: Redactor | None = None,
        sample: float = 1.0,
        rate_limit: float | None = None,
    ):
        super().__init__(
            level,
            json_mode,
            render_mode=render_mode,
            renderer=renderer,
            timestamp_mode=timestamp_mode,
            redact_fields=redact_fields,
            redactor=redactor,
            sample=sample,
            rate_limit=rate_limit,
        )
        _nonnegative_int("max_bytes", max_bytes)
        if rotation_seconds is not None:
            _nonnegative_int("rotation_seconds", rotation_seconds)
        if retention_count is not None:
            _nonnegative_int("retention_count", retention_count)
        if not isinstance(process_safe, bool):
            raise ValueError(f"process_safe must be a boolean, got {process_safe!r}")
        if compression not in {None, "gzip"}:
            raise ValueError("compression must be 'gzip' or None")
        self.file_path = file_path
        self.max_bytes = max_bytes
        self.rotation_seconds = rotation_seconds
        self.retention_count = retention_count
        self.compression = compression
        self.process_safe = process_safe
        # Monotonic: rotation intervals must not be affected by NTP steps or DST.
        self._last_rotation_time = time.monotonic()
        # re.escape: a log named ``app[0].log`` is a literal name, not a character class.
        self._rotated_name = re.compile(re.escape(os.path.basename(file_path)) + _ROTATED_SUFFIX)

        parent = os.path.dirname(os.path.abspath(file_path))
        os.makedirs(parent, exist_ok=True)

    def _should_rotate(self) -> bool:
        elapsed = time.monotonic() - self._last_rotation_time
        time_triggered = self.rotation_seconds is not None and elapsed >= self.rotation_seconds

        try:
            # One stat, not exists() then getsize(): another process can rotate the
            # file away between two calls, and the second one would raise.
            size = os.stat(self.file_path).st_size
        except FileNotFoundError:
            # Nothing to rotate, but keep the interval clock moving so an idle period
            # does not force a spurious rotation on the very next write.
            if time_triggered:
                self._last_rotation_time = time.monotonic()
            return False

        size_triggered = self.max_bytes > 0 and size >= self.max_bytes
        return size_triggered or time_triggered

    def _claim_rotated_path(self) -> str:
        """Reserve an unused rotation target and return it.

        ``strftime`` has one-second resolution, so the timestamp alone does not
        identify a file: two rotations in the same second would land on the same name.
        A random token makes the name unique without keeping any state, and each name
        is *claimed* by creating it exclusively (``O_EXCL``) rather than merely observed
        to be free, so a concurrent rotation -- in this process or another one -- cannot
        take the same name between the check and the rename. Under gzip the compressed
        name is claimed too: it is the file that survives the rotation, so leaving it
        unclaimed is what let a second rotation overwrite the first one's archive.
        """
        stamp = time.strftime(_ROTATION_STAMP)
        suffixes = ("", ".gz") if self.compression == "gzip" else ("",)
        for _ in range(_ROTATION_CLAIM_ATTEMPTS):
            candidate = f"{self.file_path}.{stamp}.{os.urandom(_ROTATION_TOKEN_BYTES).hex()}"
            claimed: list[str] = []
            try:
                for suffix in suffixes:
                    # O_EXCL fails if the name exists, so the winner is decided by the
                    # filesystem rather than by a check we race against.
                    descriptor = os.open(
                        candidate + suffix, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666
                    )
                    os.close(descriptor)
                    claimed.append(candidate + suffix)
            except FileExistsError:
                _remove_quietly(claimed)  # release the half-claimed name, then retry
                continue
            return candidate
        raise OSError(f"could not claim a rotation target for {self.file_path!r}")

    def _rotate_and_cleanup(self) -> None:
        if not self._should_rotate():
            return

        rotated_path = self._claim_rotated_path()
        try:
            # os.replace, not os.rename: the claimed placeholder is in the way, and
            # Windows' rename refuses an existing target.
            os.replace(self.file_path, rotated_path)
        except FileNotFoundError:
            # Another process rotated this file first. Its records are safe in its own
            # archive; leave the placeholders behind and let the next write start fresh.
            _remove_quietly(f"{rotated_path}{suffix}" for suffix in ("", ".gz"))
            return
        if self.compression == "gzip":
            compressed_path = f"{rotated_path}.gz"
            with open(rotated_path, "rb") as source, gzip.open(compressed_path, "wb") as target:
                shutil.copyfileobj(source, target)
            os.remove(rotated_path)
        self._last_rotation_time = time.monotonic()

        if self.retention_count is None:
            return

        # Only files this sink could have written are retention candidates. A plain
        # ``app.log.*`` match also covers an operator's ``app.log.backup``, another
        # sink's ``app.log.audit``, and the process-safe ``app.log.lock`` sidecar.
        directory = os.path.dirname(self.file_path) or os.curdir
        candidates = []
        with contextlib.suppress(OSError):  # directory replaced underneath us
            for entry in os.scandir(directory):
                if not self._rotated_name.fullmatch(entry.name):
                    continue
                with contextlib.suppress(OSError):  # concurrently removed
                    if entry.is_file():
                        candidates.append((entry.stat().st_mtime, entry.path))
        candidates.sort()

        while len(candidates) > self.retention_count:
            _, oldest = candidates.pop(0)
            with contextlib.suppress(OSError):
                os.remove(oldest)

    @staticmethod
    def _thread_lock(path: str) -> threading.Lock:
        """Coordinate separate FileSink instances in this process as well."""
        with _FILE_LOCKS_LOCK:
            return _FILE_LOCKS.setdefault(path, threading.Lock())

    @contextlib.contextmanager
    def _process_lock(self) -> Iterable[None]:
        """Lock the sidecar file across processes for one write/rotation operation."""
        lock_path = f"{self.file_path}.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows-only branch
                lock_file.seek(0)
                if lock_file.tell() == 0:
                    lock_file.write("0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows-only branch
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

    def _write_payload(self, payload: str) -> None:
        self._rotate_and_cleanup()
        with open(self.file_path, "a", encoding="utf-8") as file:
            _write_all(file, payload)
            file.flush()

    def write_batch(self, items: list[Record]) -> None:
        payload = self._render(items)
        if not payload:
            return
        if not self.process_safe:
            self._write_payload(payload)
            return
        # flock is process-wide on some systems, so use an ordinary mutex too:
        # two FileSink objects in this process must not rotate around each other.
        with self._thread_lock(self.file_path), self._process_lock():
            self._write_payload(payload)


class DispatcherSink:
    """Thread-safe ring buffer that batches records and broadcasts them to sinks.

    Records are buffered and drained to the configured sinks when any of the
    following happens, whichever comes first:

    * the buffer reaches ``flush_threshold`` records,
    * ``flush_interval`` seconds elapse (a daemon thread, started on first use),
    * :meth:`flush` is called explicitly,
    * the interpreter exits (via ``atexit``).

    Records below the minimum level of every configured sink are discarded in
    :meth:`push`, so output nobody would ever emit cannot evict records that matter.
    """

    __slots__ = (
        "_buffer",
        "_lock",
        "_flush_lock",
        "_capacity",
        "_sinks",
        "_min_level",
        "_dropped",
        "_total_dropped",
        "overflow_policy",
        "_sink_failures",
        "_last_flush_time",
        "_last_error_time",
        "_last_error",
        "failure_policy",
        "retry_attempts",
        "retry_backoff",
        "_disabled_sink_ids",
        "_last_shutdown_timed_out",
        "flush_threshold",
        "flush_interval",
        "_worker",
        "_stop",
    )

    def __init__(
        self,
        capacity: int = 100_000,
        flush_threshold: int = 1_000,
        flush_interval: float = 0.5,
        overflow_policy: str = "drop_oldest",
        failure_policy: str = "continue",
        retry_attempts: int = 0,
        retry_backoff: float = 0.05,
    ):
        self._capacity = _positive_int("capacity", capacity)
        flush_threshold = _positive_int("flush_threshold", flush_threshold)
        self.flush_interval = _nonnegative_interval("flush_interval", flush_interval)
        self.overflow_policy = _validated_overflow_policy(overflow_policy)
        if failure_policy not in {"continue", "disable", "retry"}:
            raise ValueError("failure_policy must be 'continue', 'disable', or 'retry'")
        self.failure_policy = failure_policy
        self.retry_attempts = _nonnegative_int("retry_attempts", retry_attempts)
        self.retry_backoff = _nonnegative_interval("retry_backoff", retry_backoff)
        self._buffer: deque[Record] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        # Serialises whole flushes so concurrent callers cannot interleave batches
        # into a sink out of order.
        self._flush_lock = threading.RLock()
        self._dropped = 0
        self._total_dropped = 0
        self._sink_failures = 0
        self._last_flush_time: float | None = None
        self._last_error_time: float | None = None
        self._last_error: str | None = None
        self._disabled_sink_ids: set[int] = set()
        self._last_shutdown_timed_out = False
        self.flush_threshold = max(1, min(flush_threshold, capacity))
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._sinks: list[BaseSink] = [StdoutSink(level="INFO")]
        self._min_level = self._compute_min_level(self._sinks)

    # -- sink configuration -------------------------------------------------

    @staticmethod
    def _compute_min_level(sinks: list[BaseSink]) -> int:
        return min((s.min_level for s in sinks), default=_LEVEL_NEVER)

    @property
    def sinks(self) -> list[BaseSink]:
        return self._sinks

    @sinks.setter
    def sinks(self, sinks: list[BaseSink]) -> None:
        sinks = list(sinks)
        if not all(isinstance(sink, BaseSink) for sink in sinks):
            raise TypeError("sinks must contain BaseSink instances")
        min_level = self._compute_min_level(sinks)
        # Swap under the flush lock so a concurrent flush sees one consistent set.
        with self._flush_lock:
            self._sinks = sinks
            self._min_level = min_level
            self._disabled_sink_ids.intersection_update(id(sink) for sink in sinks)

    def health(self) -> dict[str, Any]:
        """Return an inexpensive snapshot of dispatcher delivery health.

        This is intentionally pull-based: normal logging calls do not pay for
        metrics collection unless an overflow or sink failure already occurred.
        """
        with self._lock:
            buffered = len(self._buffer)
            dropped_pending = self._dropped
            total_dropped = self._total_dropped
        with self._flush_lock:
            worker_running = self._worker is not None and self._worker.is_alive()
            return {
                "buffered_records": buffered,
                "capacity": self._capacity,
                "dropped_records_pending": dropped_pending,
                "dropped_records_total": total_dropped,
                "sink_failures": self._sink_failures,
                "last_flush_time": self._last_flush_time,
                "last_error_time": self._last_error_time,
                "last_error": self._last_error,
                "disabled_sinks": [
                    type(sink).__name__
                    for sink in self._sinks
                    if id(sink) in self._disabled_sink_ids
                ],
                "last_shutdown_timed_out": self._last_shutdown_timed_out,
                "failure_policy": self.failure_policy,
                "worker_running": worker_running,
                "overflow_policy": self.overflow_policy,
            }

    # -- ingest -------------------------------------------------------------

    def is_enabled(self, level: str) -> bool:
        """Whether any configured sink would accept a record at ``level``.

        Lets callers skip building a record -- reading bound context, formatting
        arguments -- that nothing would emit.
        """
        return level_value(level) >= self._min_level

    def push(
        self,
        level: str,
        message: str,
        func_name: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if level_value(level) < self._min_level:
            return

        ts = time.time()
        with self._lock:
            if len(self._buffer) == self._capacity:
                # deque(maxlen=...) evicts silently; make loss visible instead.
                self._dropped += 1
                self._total_dropped += 1
                if self.overflow_policy == "drop_newest":
                    should_flush = len(self._buffer) >= self.flush_threshold
                else:
                    self._buffer.append((ts, level, func_name, message, extra or {}))
                    should_flush = len(self._buffer) >= self.flush_threshold
            else:
                self._buffer.append((ts, level, func_name, message, extra or {}))
                should_flush = len(self._buffer) >= self.flush_threshold

        self._ensure_worker()
        if should_flush:
            self.flush()

    # -- background flushing ------------------------------------------------

    def _ensure_worker(self) -> None:
        """Start the periodic flusher on first use.

        Started lazily rather than at import so that merely importing loglyte does
        not spawn a thread in processes that never log.
        """
        if self._worker is not None or self.flush_interval <= 0:
            return
        with self._lock:
            if self._worker is not None:
                return
            self._stop.clear()
            worker = threading.Thread(target=self._run, name="loglyte-flusher", daemon=True)
            self._worker = worker
        worker.start()

    def _run(self) -> None:
        while not self._stop.wait(self.flush_interval):
            try:
                self.flush()
            except Exception:
                _report_internal_error("background flush")

    def stop(self, flush: bool = True, timeout: float | None = 5.0) -> bool:
        """Stop the background flusher and (by default) drain what is buffered.

        Registered with ``atexit`` so the daemon thread is joined *before* the
        interpreter tears down, rather than being killed part-way through a write.
        A later :meth:`push` restarts the flusher.
        """
        if timeout is not None:
            timeout = _nonnegative_interval("timeout", timeout)
        self._stop.set()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)
            if worker.is_alive():
                self._last_shutdown_timed_out = True
                return False
        self._worker = None
        self._last_shutdown_timed_out = False
        if flush:
            self.flush()
        return True

    shutdown = stop

    def __enter__(self) -> DispatcherSink:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()

    # -- drain --------------------------------------------------------------

    def _drain(self) -> list[Record]:
        with self._lock:
            if not self._buffer and not self._dropped:
                return []
            items = list(self._buffer)
            self._buffer.clear()
            dropped, self._dropped = self._dropped, 0

        if dropped:
            # Surface the loss in-band rather than letting it vanish.
            items.insert(
                0,
                (
                    time.time(),
                    "WARNING",
                    "loglyte",
                    f"Log buffer overflow: {dropped} record(s) dropped "
                    f"(capacity={self._capacity}, policy={self.overflow_policy}). "
                    "Sinks cannot keep up.",
                    {
                        "dropped_records": dropped,
                        "capacity": self._capacity,
                        "overflow_policy": self.overflow_policy,
                    },
                ),
            )
        return items

    def flush(self) -> None:
        """Drain buffered records to every configured sink.

        A sink that raises is reported to stderr and skipped; the remaining sinks
        still receive the batch.
        """
        with self._flush_lock:
            items = self._drain()
            if not items:
                return
            for sink in self._sinks:
                if id(sink) not in self._disabled_sink_ids:
                    self._write_to_sink(sink, items)
            self._last_flush_time = time.time()

    def _write_to_sink(self, sink: BaseSink, items: list[Record]) -> None:
        attempts = self.retry_attempts if self.failure_policy == "retry" else 0
        for attempt in range(attempts + 1):
            try:
                sink.write_batch(items)
                return
            except Exception:
                self._sink_failures += 1
                self._last_error_time = time.time()
                self._last_error = f"{type(sink).__name__}.write_batch"
                if attempt < attempts:
                    time.sleep(self.retry_backoff * (2**attempt))
                    continue
                if self.failure_policy == "disable":
                    self._disabled_sink_ids.add(id(sink))
                _report_internal_error(self._last_error)

    # -- fork safety --------------------------------------------------------

    def _before_fork(self) -> None:
        """Join the flusher so the process is single-threaded across ``fork()``.

        Two reasons: a live thread's locks would be inherited by the child in an
        arbitrary state, and merely *having* a background thread makes CPython warn
        that ``fork()`` may deadlock -- a warning an application should not inherit
        just because it imported a logging library. The next :meth:`push` restarts it.
        """
        self.stop(flush=False)

    def _after_fork_in_parent(self) -> None:
        self._ensure_worker()

    def _after_fork(self) -> None:
        """Reset dispatcher state in a freshly forked child.

        The child inherits a copy of the parent's buffer -- which the parent will
        flush itself, so emitting it here too would duplicate every record.
        """
        self._lock = threading.Lock()
        self._flush_lock = threading.RLock()
        self._buffer = deque(maxlen=self._capacity)
        self._dropped = 0
        self._worker = None
        self._stop = threading.Event()
        self._disabled_sink_ids = set()


SINK = DispatcherSink()


def configure(
    sinks: list[BaseSink],
    *,
    flush_threshold: int | None = None,
    flush_interval: float | None = None,
    overflow_policy: str | None = None,
    failure_policy: str | None = None,
    retry_attempts: int | None = None,
    retry_backoff: float | None = None,
) -> None:
    """Configure the global sinks for loglyte.

    Buffered records are drained to the *existing* sinks before the swap, so nothing
    already logged is lost or misrouted.
    """
    # Validate everything before pausing a running worker, so a configuration
    # error cannot change delivery behaviour.
    sinks = list(sinks)
    if not all(isinstance(sink, BaseSink) for sink in sinks):
        raise TypeError("sinks must contain BaseSink instances")
    if flush_threshold is not None:
        flush_threshold = _positive_int("flush_threshold", flush_threshold)
    if flush_interval is not None:
        flush_interval = _nonnegative_interval("flush_interval", flush_interval)
    if overflow_policy is not None:
        overflow_policy = _validated_overflow_policy(overflow_policy)
    if failure_policy is not None and failure_policy not in {"continue", "disable", "retry"}:
        raise ValueError("failure_policy must be 'continue', 'disable', or 'retry'")
    if retry_attempts is not None:
        retry_attempts = _nonnegative_int("retry_attempts", retry_attempts)
    if retry_backoff is not None:
        retry_backoff = _nonnegative_interval("retry_backoff", retry_backoff)

    # A worker may currently be blocked in ``flush``. Stop and join it before
    # taking the flush lock, otherwise reconfiguration can wait on its own lock.
    restart_worker = flush_interval is not None and SINK._worker is not None
    if restart_worker:
        SINK.stop()

    with SINK._flush_lock:
        SINK.flush()  # drain to the old sinks before swapping
        if flush_threshold is not None:
            SINK.flush_threshold = min(flush_threshold, SINK._capacity)
        if flush_interval is not None:
            SINK.flush_interval = flush_interval
        if overflow_policy is not None:
            SINK.overflow_policy = overflow_policy
        if failure_policy is not None:
            SINK.failure_policy = failure_policy
        if retry_attempts is not None:
            SINK.retry_attempts = retry_attempts
        if retry_backoff is not None:
            SINK.retry_backoff = retry_backoff
        SINK.sinks = sinks

    if restart_worker and SINK.flush_interval > 0:
        SINK._ensure_worker()


def shutdown(timeout: float | None = 5.0) -> bool:
    """Stop the global worker and flush records; false means the worker timed out."""
    return SINK.stop(timeout=timeout)


# Join the flusher and drain anything still buffered when the interpreter exits.
atexit.register(SINK.stop)

if hasattr(os, "register_at_fork"):  # pragma: no branch - absent only on Windows
    os.register_at_fork(
        before=SINK._before_fork,
        after_in_parent=SINK._after_fork_in_parent,
        after_in_child=SINK._after_fork,
    )
