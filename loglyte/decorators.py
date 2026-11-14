from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, cast

from loglyte.context import bind as bind_context
from loglyte.core import ContextLogger

P = ParamSpec("P")
R = TypeVar("R")

_ExcTypes = type[BaseException] | tuple[type[BaseException], ...]


def _declares_log(func: Callable[..., Any]) -> bool:
    """Whether ``func`` explicitly declares a ``log`` parameter it can be passed by name.

    Injecting ``log`` into a function that does not accept it raises ``TypeError`` at
    call time -- and, worse, the wrapper would then report that ``TypeError`` as a
    genuine unhandled application exception. So the check happens once, at decoration.

    A bare ``**kwargs`` does not count: silently adding a ``log`` key to someone's
    keyword bag is more likely to break them than to help.
    """
    try:
        parameters = inspect.signature(func).parameters
    except (ValueError, TypeError):  # builtins and other unintrospectable callables
        return False
    param = parameters.get("log")
    return param is not None and param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def _validate_bind_names(func: Callable[..., Any], bind_names: tuple[str, ...]) -> None:
    """Reject misspelled decorator bindings while the application starts."""
    try:
        parameters = inspect.signature(func).parameters
    except (ValueError, TypeError) as exc:
        if bind_names:
            raise TypeError(
                f"Cannot bind arguments on {func!r}: its signature is unavailable"
            ) from exc
        return
    missing = [name for name in bind_names if name not in parameters]
    if missing:
        names = ", ".join(repr(name) for name in missing)
        raise TypeError(f"Cannot bind {names} on {func.__qualname__}: no such parameter")


def _bound_fields(
    signature: inspect.Signature | None,
    bind_names: tuple[str, ...],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    if not bind_names or signature is None:
        return {}
    values = signature.bind(*args, **kwargs)
    values.apply_defaults()
    return {name: values.arguments[name] for name in bind_names if name in values.arguments}


def _build_wrapper(
    func: Callable[P, R],
    *,
    inject: bool,
    catch_types: _ExcTypes | None,
    reraise: bool = True,
    message: str = "",
    bind_names: tuple[str, ...] = (),
) -> Callable[P, R]:
    logger_instance = ContextLogger(func_name=func.__qualname__)
    try:
        signature: inspect.Signature | None = inspect.signature(func)
    except (ValueError, TypeError):
        signature = None

    def log_exception(exc: BaseException) -> None:
        # ContextLogger attaches type, detail and traceback.
        logger_instance.critical(message, exc_info=exc)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            fields = _bound_fields(signature, bind_names, args, kwargs)
            if inject and "log" not in kwargs:
                kwargs["log"] = logger_instance
            with bind_context(**fields):
                if catch_types is None:
                    return await func(*args, **kwargs)
                try:
                    return await func(*args, **kwargs)
                except catch_types as exc:
                    log_exception(exc)
                    if reraise:
                        raise
                    return None

        return cast(Callable[P, R], async_wrapper)

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        fields = _bound_fields(signature, bind_names, args, kwargs)
        if inject and "log" not in kwargs:
            kwargs["log"] = logger_instance
        with bind_context(**fields):
            if catch_types is None:
                return func(*args, **kwargs)
            try:
                return func(*args, **kwargs)
            except catch_types as exc:
                log_exception(exc)
                if reraise:
                    raise
                return None

    return cast(Callable[P, R], sync_wrapper)


def decorate(
    func: Callable[P, R], *, bind: tuple[str, ...] = (), critical: bool = False
) -> Callable[P, R]:
    """Implementation behind ``@logger`` and ``@logger.critical``.

    It captures the function name once during import, injects ``log`` only when the
    signature explicitly asks for it, and binds selected call arguments for the
    duration of the invocation.
    """
    _validate_bind_names(func, bind)
    return _build_wrapper(
        func,
        inject=_declares_log(func),
        catch_types=Exception if critical else None,
        reraise=True,
        message=f"Unhandled Exception in {func.__qualname__}",
        bind_names=bind,
    )


def catch(
    exception_types: _ExcTypes = Exception,
    *,
    reraise: bool = True,
    message: str = "Unhandled exception occurred in decorated function",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Automatically catch, log, and optionally re-raise exceptions.

    Args:
        exception_types: Exception class(es) to catch. Defaults to ``Exception``.
        reraise: Whether to re-raise the caught exception. Defaults to ``True``.
        message: Custom message logged alongside the exception.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        return _build_wrapper(
            func,
            inject=_declares_log(func),
            catch_types=exception_types,
            reraise=reraise,
            message=message,
        )

    return decorator
