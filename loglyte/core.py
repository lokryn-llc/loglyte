from __future__ import annotations

import sys
import traceback
from functools import cache
from types import TracebackType
from typing import Any

from loglyte.context import get_context
from loglyte.sinks import SINK

#: What ``exc_info=`` accepts, mirroring the stdlib convention:
#: ``True`` (use the exception being handled), an exception instance, or a
#: ``(type, value, traceback)`` triple.
ExcInfo = (
    bool | BaseException | tuple[type[BaseException], BaseException, TracebackType | None] | None
)


def _resolve_exc_info(exc_info: ExcInfo) -> BaseException | None:
    """Normalise a stdlib-style ``exc_info`` value into an exception, or None."""
    if not exc_info:  # None, False
        return None
    if exc_info is True:
        return sys.exc_info()[1]
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple) and len(exc_info) == 3:
        return exc_info[1]
    return None


def _exception_fields(exc: BaseException) -> dict[str, str]:
    """Describe ``exc``, including its traceback.

    Formatted eagerly: deferring it would mean holding the traceback -- and so
    every frame and every local it references -- alive until the next flush.
    """
    return {
        "exception_type": type(exc).__name__,
        "exception_detail": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }


class ContextLogger:
    """Emits records tagged with the decorated function they came from.

    Every method takes its message positionally-only, so an ``extra`` field may be
    named ``msg``, ``level`` or anything else without colliding with a parameter.

    Pass ``exc_info`` to attach an exception and its traceback::

        try:
            charge(card)
        except PaymentError:
            log.error("payment failed", exc_info=True)
    """

    __slots__ = ("_func_name",)

    def __init__(self, func_name: str):
        self._func_name = func_name

    def _push(self, level: str, msg: str, extra: dict[str, Any], exc_info: ExcInfo = None) -> None:
        # Check the level before doing anything else: resolving the exception and
        # merging the bound context are the expensive parts, and for a record no
        # sink wants they are pure waste.
        if not SINK.is_enabled(level):
            return
        if exc_info:
            exc = _resolve_exc_info(exc_info)
            if exc is not None:
                extra.update(_exception_fields(exc))
        bound = get_context()
        if bound:
            extra = {**bound, **extra}
        SINK.push(level, msg, self._func_name, extra)

    def debug(self, msg: str, /, *, exc_info: ExcInfo = None, **extra: Any) -> None:
        self._push("DEBUG", msg, extra, exc_info)

    def info(self, msg: str, /, *, exc_info: ExcInfo = None, **extra: Any) -> None:
        self._push("INFO", msg, extra, exc_info)

    def warning(self, msg: str, /, *, exc_info: ExcInfo = None, **extra: Any) -> None:
        self._push("WARNING", msg, extra, exc_info)

    def error(self, msg: str, /, *, exc_info: ExcInfo = None, **extra: Any) -> None:
        self._push("ERROR", msg, extra, exc_info)

    def exception(self, msg: str, /, *, exc_info: ExcInfo = True, **extra: Any) -> None:
        """Log at ERROR with the exception currently being handled attached."""
        self._push("ERROR", msg, extra, exc_info)

    def critical(
        self,
        msg: str,
        /,
        *,
        exception: BaseException | None = None,
        exc_info: ExcInfo = None,
        **extra: Any,
    ) -> None:
        self._push("CRITICAL", msg, extra, exception if exception is not None else exc_info)

    def is_enabled_for(self, level: str) -> bool:
        """Whether a standard level would reach at least one configured sink."""
        return isinstance(level, str) and SINK.is_enabled(level.upper())

    def bind(self, **fields: Any) -> BoundContextLogger:
        """Return an immutable logger that adds ``fields`` to every event."""
        return BoundContextLogger(self._func_name, fields)

    def with_location(self, *, stacklevel: int = 1, stack_info: bool = False) -> LocationLogger:
        """Return an opt-in logger that captures caller location and stack data.

        Frame walking happens only on calls made through the returned logger.
        ``stacklevel`` counts outward from the eventual logging call, matching the
        standard-library convention.
        """
        return LocationLogger(self._func_name, stacklevel=stacklevel, stack_info=stack_info)


class BoundContextLogger(ContextLogger):
    """A logger with immutable per-instance structured fields."""

    __slots__ = ("_bound",)

    def __init__(self, func_name: str, fields: dict[str, Any]):
        super().__init__(func_name)
        self._bound = dict(fields)

    def _push(self, level: str, msg: str, extra: dict[str, Any], exc_info: ExcInfo = None) -> None:
        super()._push(level, msg, {**self._bound, **extra}, exc_info)

    def bind(self, **fields: Any) -> BoundContextLogger:
        return BoundContextLogger(self._func_name, {**self._bound, **fields})

    def with_location(
        self, *, stacklevel: int = 1, stack_info: bool = False
    ) -> BoundLocationLogger:
        return BoundLocationLogger(
            self._func_name,
            self._bound,
            stacklevel=stacklevel,
            stack_info=stack_info,
        )


class LocationLogger(ContextLogger):
    """A logger that pays for caller metadata only when explicitly requested."""

    __slots__ = ("_stacklevel", "_stack_info")

    def __init__(self, func_name: str, *, stacklevel: int, stack_info: bool):
        if isinstance(stacklevel, bool) or not isinstance(stacklevel, int) or stacklevel < 1:
            raise ValueError(f"stacklevel must be a positive integer, got {stacklevel!r}")
        super().__init__(func_name)
        self._stacklevel = stacklevel
        self._stack_info = stack_info

    def _push(self, level: str, msg: str, extra: dict[str, Any], exc_info: ExcInfo = None) -> None:
        if not SINK.is_enabled(level):
            return
        try:
            frame = sys._getframe(1 + self._stacklevel)
            location = {
                "file": frame.f_code.co_filename,
                "line": frame.f_lineno,
                "function": frame.f_code.co_name,
            }
            if self._stack_info:
                location["stack_info"] = "".join(traceback.format_stack(frame))
            extra = {**extra, **location}
        except ValueError:
            # A shallow stack should not make a log call fail. Location is simply
            # unavailable in this unusual case.
            pass
        super()._push(level, msg, extra, exc_info)

    def bind(self, **fields: Any) -> BoundLocationLogger:
        return BoundLocationLogger(
            self._func_name,
            fields,
            stacklevel=self._stacklevel,
            stack_info=self._stack_info,
        )


class BoundLocationLogger(ContextLogger):
    """The opt-in combination of bound fields and caller metadata."""

    __slots__ = ("_bound", "_stacklevel", "_stack_info")

    def __init__(
        self,
        func_name: str,
        fields: dict[str, Any],
        *,
        stacklevel: int,
        stack_info: bool,
    ):
        if isinstance(stacklevel, bool) or not isinstance(stacklevel, int) or stacklevel < 1:
            raise ValueError(f"stacklevel must be a positive integer, got {stacklevel!r}")
        ContextLogger.__init__(self, func_name)
        self._bound = dict(fields)
        self._stacklevel = stacklevel
        self._stack_info = stack_info

    def _push(self, level: str, msg: str, extra: dict[str, Any], exc_info: ExcInfo = None) -> None:
        if not SINK.is_enabled(level):
            return
        extra = {
            **self._bound,
            **extra,
            **_location_fields(self._stacklevel, self._stack_info),
        }
        super()._push(level, msg, extra, exc_info)

    def bind(self, **fields: Any) -> BoundLocationLogger:
        return BoundLocationLogger(
            self._func_name,
            {**self._bound, **fields},
            stacklevel=self._stacklevel,
            stack_info=self._stack_info,
        )


def get_logger(name: str) -> ContextLogger:
    """Return the process-local named logger for non-decorated code and libraries."""
    if not isinstance(name, str) or not name:
        raise ValueError("logger name must be a non-empty string")
    return _get_logger(name)


def _location_fields(stacklevel: int, include_stack: bool) -> dict[str, Any]:
    """Capture caller metadata for opt-in location-aware loggers only."""
    try:
        frame = sys._getframe(2 + stacklevel)
        fields: dict[str, Any] = {
            "file": frame.f_code.co_filename,
            "line": frame.f_lineno,
            "function": frame.f_code.co_name,
        }
        if include_stack:
            fields["stack_info"] = "".join(traceback.format_stack(frame))
        return fields
    except ValueError:
        # A shallow stack should not make a log call fail. Location is simply
        # unavailable in this unusual case.
        return {}


@cache
def _get_logger(name: str) -> ContextLogger:
    return ContextLogger(name)
