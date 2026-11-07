"""The small public logger surface: one import for normal application logging."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, overload

from loglyte.context import bind as bind_context
from loglyte.core import ContextLogger, ExcInfo
from loglyte.decorators import decorate
from loglyte.sinks import shutdown as shutdown_dispatcher

P = ParamSpec("P")
R = TypeVar("R")


def _bind_names(names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not all(isinstance(name, str) and name for name in names):
        raise TypeError("bind must contain non-empty parameter names")
    if len(set(names)) != len(names):
        raise ValueError("bind cannot contain duplicate parameter names")
    return tuple(names)


class ScopedLogger(ContextLogger):
    """A logger with fields that can also be used as a scoped context manager."""

    __slots__ = ("_fields", "_scope")

    def __init__(self, fields: dict[str, Any]):
        super().__init__("loglyte")
        self._fields = dict(fields)
        self._scope: Any = None

    def _push(self, level: str, msg: str, extra: dict[str, Any], exc_info: ExcInfo = None) -> None:
        super()._push(level, msg, {**self._fields, **extra}, exc_info)

    def bind(self, **fields: Any) -> ScopedLogger:
        return ScopedLogger({**self._fields, **fields})

    def __enter__(self) -> ScopedLogger:
        self._scope = bind_context(**self._fields)
        self._scope.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._scope is None:
            raise RuntimeError("logger binding scope was not entered")
        self._scope.__exit__(*args)
        self._scope = None


class Logger(ContextLogger):
    """Global Loglyte logger and optional function/class decorator.

    ``logger.info(...)`` is the fast, ordinary event path. ``@logger`` captures
    callable identity once and enables optional argument binding and ``log``
    injection without a per-module logger factory.
    """

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("loglyte")

    @overload
    def __call__(self, target: Callable[P, R], /) -> Callable[P, R]: ...

    @overload
    def __call__(self, target: type, /) -> type: ...

    @overload
    def __call__(
        self, target: None = None, /, *, bind: tuple[str, ...] = ()
    ) -> Callable[[Any], Any]: ...

    def __call__(self, target: Any = None, /, *, bind: tuple[str, ...] = ()) -> Any:
        names = _bind_names(bind)
        if target is None:
            return lambda value: self._decorate(value, names, critical=False)
        return self._decorate(target, names, critical=False)

    def _decorate(self, target: Any, names: tuple[str, ...], *, critical: bool) -> Any:
        if isinstance(target, type):
            return self._decorate_class(target, names, critical=critical)
        if not callable(target):
            raise TypeError("@logger can decorate only functions or classes")
        return decorate(target, bind=names, critical=critical)

    @staticmethod
    def _decorate_class(cls: type, names: tuple[str, ...], *, critical: bool) -> type:
        for name, value in tuple(vars(cls).items()):
            if name.startswith("_"):
                continue
            if isinstance(value, staticmethod):
                setattr(
                    cls, name, staticmethod(decorate(value.__func__, bind=names, critical=critical))
                )
            elif isinstance(value, classmethod):
                setattr(
                    cls, name, classmethod(decorate(value.__func__, bind=names, critical=critical))
                )
            elif callable(value):
                setattr(cls, name, decorate(value, bind=names, critical=critical))
        return cls

    def bind(self, **fields: Any) -> ScopedLogger:
        """Add fields to events directly, or for the duration of a ``with`` block."""
        return ScopedLogger(fields)

    def shutdown(self, timeout: float | None = 5.0) -> bool:
        """Flush queued records and stop the background writer for service shutdown."""
        return shutdown_dispatcher(timeout)

    def critical(
        self,
        msg: str | Callable[..., Any] | None = None,
        /,
        *,
        bind: tuple[str, ...] = (),
        exception: BaseException | None = None,
        exc_info: ExcInfo = None,
        **extra: Any,
    ) -> Any:
        """Log a critical event, or use as ``@logger.critical``.

        Decorated functions log unhandled ``Exception`` instances with a traceback
        and then re-raise them; application control flow is never swallowed.
        """
        names = _bind_names(bind)
        if msg is None:
            if names:
                return lambda value: self._decorate(value, names, critical=True)
            raise TypeError("critical() requires a message, or use @logger.critical")
        if callable(msg) and not isinstance(msg, str):
            if exception is not None or exc_info is not None or extra:
                raise TypeError("@logger.critical does not accept event fields")
            return self._decorate(msg, names, critical=True)
        if names:
            raise TypeError("bind is only valid when @logger.critical decorates a callable")
        return super().critical(msg, exception=exception, exc_info=exc_info, **extra)


logger = Logger()
