"""Context binding, the ContextLogger surface, and packaging invariants."""

import asyncio
import inspect
import json
import threading

import pytest

import loglyte.core
from loglyte import get_logger, logger
from loglyte.context import bind, get_context
from loglyte.core import ContextLogger

from .conftest import ListSink


def payloads(collector):
    return [json.loads(line) for line in collector.lines]


# -- bind ---------------------------------------------------------------------


def test_bound_fields_appear_on_every_record_in_scope(sink):
    dispatcher, collector = sink

    @logger
    def emit(log=None):
        log.info("inside")

    with bind(request_id="req_1", user_id="usr_9"):
        emit()
    dispatcher.flush()

    entry = payloads(collector)[0]
    assert entry["request_id"] == "req_1"
    assert entry["user_id"] == "usr_9"


def test_bind_is_scoped(sink):
    dispatcher, collector = sink

    @logger
    def emit(log=None):
        log.info("m")

    with bind(request_id="req_1"):
        pass
    emit()
    dispatcher.flush()
    assert "request_id" not in payloads(collector)[0]


def test_nested_bind_merges_and_unwinds():
    with bind(a=1):
        with bind(b=2):
            assert get_context() == {"a": 1, "b": 2}
        assert get_context() == {"a": 1}
    assert get_context() == {}


def test_inner_bind_shadows_outer():
    with bind(a=1), bind(a=2):
        assert get_context() == {"a": 2}


def test_bind_unwinds_on_exception():
    with pytest.raises(RuntimeError), bind(a=1):
        raise RuntimeError
    assert get_context() == {}


def test_call_site_extra_beats_bound_context(sink):
    dispatcher, collector = sink

    @logger
    def emit(log=None):
        log.info("m", user_id="explicit")

    with bind(user_id="bound"):
        emit()
    dispatcher.flush()
    assert payloads(collector)[0]["user_id"] == "explicit"


def test_get_context_returns_a_copy_not_the_live_dict():
    with bind(a=1):
        get_context()["a"] = "mutated"
        assert get_context() == {"a": 1}


def test_bind_does_not_leak_across_threads():
    seen = {}

    def worker():
        seen["ctx"] = get_context()

    with bind(request_id="main-thread"):
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert seen["ctx"] == {}


async def test_bind_is_isolated_between_concurrent_tasks():
    seen = []

    async def task(name):
        with bind(task=name):
            await asyncio.sleep(0.01)
            seen.append(get_context()["task"])

    await asyncio.gather(task("a"), task("b"))
    assert sorted(seen) == ["a", "b"]


# -- ContextLogger surface ----------------------------------------------------


@pytest.mark.parametrize(
    "method,level",
    [("debug", "DEBUG"), ("info", "INFO"), ("warning", "WARNING"), ("error", "ERROR")],
)
def test_each_level_method(sink, method, level):
    dispatcher, collector = sink
    getattr(ContextLogger("f"), method)("m")
    dispatcher.flush()
    assert payloads(collector)[0]["level"] == level


@pytest.mark.parametrize("name", ["level", "msg", "message", "self", "func_name"])
def test_extra_field_named_like_a_parameter_does_not_raise(sink, name):
    """log.info("m", level="high") used to fail with 'got multiple values for
    argument level' -- these are all plausible field names."""
    dispatcher, collector = sink
    ContextLogger("f").info("real message", **{name: "user value"})
    dispatcher.flush()

    entry = payloads(collector)[0]
    assert entry["message"] == "real message"
    assert entry["level"] == "INFO"
    assert "user value" in entry.values()


def test_a_filtered_record_does_not_read_the_bound_context(sink, monkeypatch):
    """Reading and merging bound context is the expensive part of a log call, so a
    record no sink would accept must be rejected before that work happens."""
    dispatcher, _ = sink
    dispatcher.sinks = [ListSink(level="ERROR")]

    calls = []
    monkeypatch.setattr(loglyte.core, "get_context", lambda: calls.append(1) or {})

    with bind(request_id="req_1"):
        ContextLogger("f").debug("filtered out")
    assert calls == [], "context was read for a record nothing would emit"

    ContextLogger("f").error("kept")
    assert calls == [1], "context must still be read for records that are kept"


def test_is_enabled_reflects_the_configured_sinks(dispatcher):
    dispatcher.sinks = [ListSink(level="WARNING")]
    assert not dispatcher.is_enabled("INFO")
    assert dispatcher.is_enabled("ERROR")

    dispatcher.sinks = []
    assert not dispatcher.is_enabled("CRITICAL")


def test_critical_without_an_exception(sink):
    dispatcher, collector = sink
    ContextLogger("f").critical("just critical")
    dispatcher.flush()
    entry = payloads(collector)[0]
    assert entry["level"] == "CRITICAL"
    assert "exception_type" not in entry


def test_get_logger_returns_a_cached_named_logger(sink):
    dispatcher, collector = sink
    first = get_logger("payments.charge")
    assert first is get_logger("payments.charge")

    first.info("authorised", order_id=7)
    dispatcher.flush()
    entry = payloads(collector)[0]
    assert entry["logger"] == "payments.charge"
    assert entry["order_id"] == 7


@pytest.mark.parametrize("name", ["", None])
def test_get_logger_rejects_an_invalid_name(name):
    with pytest.raises(ValueError, match="logger name"):
        get_logger(name)


def test_bound_logger_is_immutable_and_call_fields_win(sink):
    dispatcher, collector = sink
    base = ContextLogger("f")
    first = base.bind(request_id="one", stable=True)
    second = first.bind(request_id="two")

    first.info("first", stable=False)
    second.info("second")
    dispatcher.flush()

    first_entry, second_entry = payloads(collector)
    assert first_entry["request_id"] == "one"
    assert first_entry["stable"] is False
    assert second_entry["request_id"] == "two"
    assert second_entry["stable"] is True


def test_location_is_captured_only_by_the_opt_in_logger(sink):
    dispatcher, collector = sink
    logger = ContextLogger("f")
    expected_line = inspect.currentframe().f_lineno + 1
    logger.with_location().info("located")
    dispatcher.flush()

    entry = payloads(collector)[0]
    assert entry["file"].endswith("test_context_and_api.py")
    assert entry["line"] == expected_line
    assert entry["function"] == "test_location_is_captured_only_by_the_opt_in_logger"


def test_location_stacklevel_and_stack_info(sink):
    dispatcher, collector = sink
    logger = ContextLogger("f").with_location(stacklevel=2, stack_info=True)

    def helper():
        logger.info("located-through-helper")

    expected_line = inspect.currentframe().f_lineno + 1
    helper()
    dispatcher.flush()

    entry = payloads(collector)[0]
    assert entry["line"] == expected_line
    assert "test_location_stacklevel_and_stack_info" in entry["stack_info"]


def test_bound_location_logger_merges_both_features(sink):
    dispatcher, collector = sink
    ContextLogger("f").bind(request_id="r1").with_location().info("located-and-bound")
    dispatcher.flush()

    entry = payloads(collector)[0]
    assert entry["request_id"] == "r1"
    assert entry["function"] == "test_bound_location_logger_merges_both_features"


# -- packaging ----------------------------------------------------------------


def test_py_typed_marker_ships_with_the_package():
    """Without this file, PEP 561 says every type checker ignores our annotations."""
    import pathlib

    import loglyte

    marker = pathlib.Path(loglyte.__file__).parent / "py.typed"
    assert marker.exists()


def test_everything_in_dunder_all_is_importable():
    import loglyte

    missing = [name for name in loglyte.__all__ if not hasattr(loglyte, name)]
    assert missing == []


def test_replaced_public_apis_are_not_left_as_compatibility_debt():
    import loglyte

    for name in (
        "info",
        "critical",
        "bind",
        "get_context",
        "register_level",
        "ContextLogger",
        "Logger",
        "shutdown",
    ):
        assert not hasattr(loglyte, name), f"{name} was replaced and must not remain public"


def test_no_stale_stub_shadowing_the_package():
    """A partial __init__.pyi hides the whole package from type checkers."""
    import pathlib

    import loglyte

    assert not (pathlib.Path(loglyte.__file__).parent / "__init__.pyi").exists()
