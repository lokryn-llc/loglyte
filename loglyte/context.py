from __future__ import annotations

import contextvars
import warnings
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any


class _Frame:
    """One active binding scope in the current context.

    Frames form an immutable chain: ``parent`` is the scope that was active when this
    one was entered, and ``merged`` is the complete context this scope exposes.
    ``owner`` identifies the scope object that pushed the frame so that its exit can
    find it again. A frame carries no token and is never mutated, so the same scope
    object can be entered concurrently by any number of threads or tasks.
    """

    __slots__ = ("fields", "merged", "owner", "parent")

    def __init__(self, fields: dict[str, Any], owner: object, parent: _Frame | None):
        self.fields = fields
        self.owner = owner
        self.parent = parent
        self.merged = {**parent.merged, **fields} if parent is not None else dict(fields)


_LOG_CONTEXT: contextvars.ContextVar[_Frame | None] = contextvars.ContextVar(
    "_LOG_CONTEXT", default=None
)


def get_context() -> dict[str, Any]:
    """Return a shallow copy of the currently bound log context."""
    frame = _LOG_CONTEXT.get()
    return frame.merged.copy() if frame is not None else {}


def push_scope(fields: dict[str, Any], owner: object) -> None:
    """Enter a binding scope owned by ``owner`` in the current context."""
    _LOG_CONTEXT.set(_Frame(fields, owner, _LOG_CONTEXT.get()))


def pop_scope(owner: object, exc: BaseException | None = None) -> None:
    """Leave the innermost scope owned by ``owner`` in the current context.

    Scopes are expected to exit in the reverse order they were entered. When they do
    not (for example two generators that each hold a scope and are advanced in an
    interleaved way), the scope is still removed and every scope entered after it
    keeps its own fields, so the context is repaired and later records are correct.
    The mistake is then reported with a ``RuntimeError``.

    ``exc`` is the exception propagating out of the scope's body, if any. A logging
    error must never replace the application's own exception, so in that case the
    problem is attached to ``exc`` as a note (or, on Python 3.10, emitted as a
    ``RuntimeWarning``) instead of being raised.
    """
    top = _LOG_CONTEXT.get()
    if top is not None and top.owner is owner:
        _LOG_CONTEXT.set(top.parent)
        return

    above: list[_Frame] = []
    frame = top
    while frame is not None and frame.owner is not owner:
        above.append(frame)
        frame = frame.parent

    if frame is None:
        problem = "logger binding scope was not entered in this context"
    else:
        # Re-stack the scopes entered after this one onto its parent, dropping only
        # this scope's fields.
        repaired = frame.parent
        for inner in reversed(above):
            repaired = _Frame(inner.fields, inner.owner, repaired)
        _LOG_CONTEXT.set(repaired)
        problem = (
            "logger binding scope exited out of order: a scope entered after it is "
            "still active; the log context has been repaired"
        )

    if exc is None:
        raise RuntimeError(problem)
    if hasattr(exc, "add_note"):
        exc.add_note(f"loglyte: {problem}")
    else:  # pragma: no cover - Python 3.10
        warnings.warn(f"loglyte: {problem}", RuntimeWarning, stacklevel=3)


@contextmanager
def bind(**kwargs: Any) -> Generator[None, None, None]:
    """Bind key-value pairs to every log emitted within the current scope.

    Works in both threads and async tasks via :class:`contextvars.ContextVar`.

    Example::

        with bind(request_id="req_12345", user_id="usr_99"):
            process_payment()

    """
    owner = object()
    push_scope(kwargs, owner)
    try:
        yield
    except BaseException as exc:
        pop_scope(owner, exc)
        raise
    pop_scope(owner)
