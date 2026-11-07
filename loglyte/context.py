from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

_LOG_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "_LOG_CONTEXT", default=None
)


def get_context() -> dict[str, Any]:
    """Return a shallow copy of the currently bound log context."""
    ctx = _LOG_CONTEXT.get()
    return ctx.copy() if ctx is not None else {}


@contextmanager
def bind(**kwargs: Any) -> Generator[None, None, None]:
    """Bind key-value pairs to every log emitted within the current scope.

    Works in both threads and async tasks via :class:`contextvars.ContextVar`.

    Example::

        with bind(request_id="req_12345", user_id="usr_99"):
            process_payment()

    """
    current = _LOG_CONTEXT.get()
    token = _LOG_CONTEXT.set({**current, **kwargs} if current is not None else kwargs)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)
