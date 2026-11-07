"""Bridge from the standard library's ``logging`` into loglyte.

Third-party packages log through ``logging``. Without this bridge an application
that uses loglyte sees nothing from SQLAlchemy, urllib3, uvicorn, boto3 or any
other dependency -- which makes loglyte unusable as the only logger.

Typical use, once, at startup::

    import loglyte
    from loglyte.stdlib import install

    loglyte.configure(sinks=[loglyte.StderrSink(level="INFO", json_mode=True)])
    install(level="INFO")          # every stdlib logger now feeds loglyte

Imported separately from ``loglyte`` itself so that the core package never pays
for importing ``logging``.
"""

from __future__ import annotations

import logging
from typing import Any

from loglyte.core import _exception_fields
from loglyte.sinks import LEVEL_MAP, SINK, DispatcherSink

#: Attributes every LogRecord carries. Anything else was supplied by the caller
#: via ``extra=`` and is worth forwarding as a structured field.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "getMessage",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",  # 3.12+
        "thread",
        "threadName",
    }
)

#: Standard levels, highest first, for mapping custom numeric levels down.
_LEVELS_DESC = sorted(LEVEL_MAP.items(), key=lambda kv: kv[1], reverse=True)


def _level_name(record: logging.LogRecord) -> str:
    """The loglyte level for ``record``.

    Custom levels (``logging.addLevelName(25, "NOTICE")``) have no loglyte
    equivalent, so they map down to the nearest standard level at or below their
    numeric value -- a NOTICE(25) record is emitted as INFO rather than silently
    dropped for having an unrecognised name.
    """
    if record.levelname in LEVEL_MAP:
        return record.levelname
    for name, value in _LEVELS_DESC:
        if record.levelno >= value:
            return name
    return "DEBUG"


class LoglyteHandler(logging.Handler):
    """A ``logging.Handler`` that forwards records into a loglyte dispatcher.

    Attach it to any stdlib logger, or use :func:`install` to attach it to the
    root logger.
    """

    def __init__(
        self,
        level: int | str = logging.NOTSET,
        *,
        dispatcher: DispatcherSink | None = None,
        include_location: bool = True,
    ):
        super().__init__(level)
        self.dispatcher = dispatcher if dispatcher is not None else SINK
        self.include_location = include_location

    def emit(self, record: logging.LogRecord) -> None:
        try:
            extra: dict[str, Any] = {
                key: value for key, value in record.__dict__.items() if key not in _STANDARD_ATTRS
            }
            if self.include_location:
                # stdlib already paid for this; passing it on is free.
                extra["file"] = record.pathname
                extra["line"] = record.lineno
                if record.funcName:
                    extra["function"] = record.funcName

            if record.exc_info and record.exc_info[1] is not None:
                extra.update(_exception_fields(record.exc_info[1]))
            elif record.exc_text:
                extra["traceback"] = record.exc_text
            if record.stack_info:
                extra["stack_info"] = record.stack_info

            self.dispatcher.push(_level_name(record), record.getMessage(), record.name, extra)
        except Exception:
            # Honours logging.raiseExceptions, and never propagates into the caller.
            self.handleError(record)


def install(
    level: int | str = logging.INFO,
    *,
    logger: logging.Logger | None = None,
    dispatcher: DispatcherSink | None = None,
    replace_existing: bool = False,
    include_location: bool = True,
) -> LoglyteHandler:
    """Route stdlib logging into loglyte and return the installed handler.

    Args:
        level: Minimum level the stdlib side will pass through. Sinks still apply
            their own levels on top.
        logger: Which logger to attach to. Defaults to the root logger, which
            captures every library that has not disabled propagation.
        dispatcher: Dispatcher to feed. Defaults to the global ``SINK``.
        replace_existing: Remove every existing handler on the target logger before
            installing the bridge. Defaults to ``False`` so existing observability
            is never silently disabled. Repeated non-replacing installs for the same
            dispatcher return the existing bridge instead of duplicating records.
        include_location: Forward the file/line/function stdlib already captured.

    Returns:
        The handler, so it can be passed to :func:`uninstall` later.
    """
    target = logger if logger is not None else logging.getLogger()
    target_dispatcher = dispatcher if dispatcher is not None else SINK

    if replace_existing:
        for existing in list(target.handlers):
            target.removeHandler(existing)
        handler = LoglyteHandler(
            level, dispatcher=target_dispatcher, include_location=include_location
        )
        target.addHandler(handler)
    else:
        handler = next(
            (
                existing
                for existing in target.handlers
                if isinstance(existing, LoglyteHandler) and existing.dispatcher is target_dispatcher
            ),
            None,
        )
        if handler is None:
            handler = LoglyteHandler(
                level, dispatcher=target_dispatcher, include_location=include_location
            )
            target.addHandler(handler)
        else:
            handler.setLevel(level)
            handler.include_location = include_location

    numeric = logging.getLevelName(level) if isinstance(level, str) else level
    if isinstance(numeric, int):
        target.setLevel(numeric)
    return handler


def uninstall(handler: LoglyteHandler, *, logger: logging.Logger | None = None) -> None:
    """Detach a handler installed by :func:`install`."""
    target = logger if logger is not None else logging.getLogger()
    target.removeHandler(handler)
