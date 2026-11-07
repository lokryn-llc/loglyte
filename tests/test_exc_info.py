"""Attaching exceptions and tracebacks to ordinary log calls.

Before this existed, a manually caught exception could only be recorded as a type
name and a string -- the traceback was reachable only through @catch.
"""

import json

import pytest

from loglyte import catch, logger
from loglyte.core import ContextLogger


def entries(collector):
    return [json.loads(line) for line in collector.lines]


def raise_deep(exc=None):
    def inner():
        raise exc or ValueError("boom")

    inner()


@pytest.mark.parametrize("method", ["debug", "info", "warning", "error"])
def test_exc_info_true_uses_the_exception_being_handled(sink, method):
    dispatcher, collector = sink
    try:
        raise_deep()
    except ValueError:
        getattr(ContextLogger("f"), method)("payment failed", exc_info=True)
    dispatcher.flush()

    entry = entries(collector)[0]
    assert entry["exception_type"] == "ValueError"
    assert entry["exception_detail"] == "boom"
    assert "in inner" in entry["traceback"], "the real stack, not just the type"


def test_exc_info_accepts_an_exception_instance(sink):
    dispatcher, collector = sink
    exc = KeyError("missing")
    ContextLogger("f").error("lookup failed", exc_info=exc)
    dispatcher.flush()
    assert entries(collector)[0]["exception_type"] == "KeyError"


def test_exc_info_accepts_a_sys_exc_info_triple(sink):
    import sys

    dispatcher, collector = sink
    try:
        raise_deep()
    except ValueError:
        ContextLogger("f").error("failed", exc_info=sys.exc_info())
    dispatcher.flush()
    assert entries(collector)[0]["exception_type"] == "ValueError"


@pytest.mark.parametrize("value", [None, False])
def test_falsy_exc_info_attaches_nothing(sink, value):
    dispatcher, collector = sink
    try:
        raise_deep()
    except ValueError:
        ContextLogger("f").error("no exception wanted", exc_info=value)
    dispatcher.flush()
    assert "exception_type" not in entries(collector)[0]


def test_exc_info_true_outside_an_except_block_is_harmless(sink):
    dispatcher, collector = sink
    ContextLogger("f").error("nothing being handled", exc_info=True)
    dispatcher.flush()
    entry = entries(collector)[0]
    assert entry["message"] == "nothing being handled"
    assert "exception_type" not in entry


def test_exception_helper_defaults_to_the_current_exception(sink):
    dispatcher, collector = sink
    try:
        raise_deep()
    except ValueError:
        ContextLogger("f").exception("charge failed")
    dispatcher.flush()

    entry = entries(collector)[0]
    assert entry["level"] == "ERROR"
    assert "in inner" in entry["traceback"]


def test_exc_info_composes_with_structured_fields(sink):
    dispatcher, collector = sink
    try:
        raise_deep()
    except ValueError:
        ContextLogger("f").error("failed", exc_info=True, order_id=7, region="eu")
    dispatcher.flush()

    entry = entries(collector)[0]
    assert entry["order_id"] == 7 and entry["region"] == "eu"
    assert entry["exception_type"] == "ValueError"


def test_critical_exception_kwarg_still_works_and_now_carries_a_traceback(sink):
    dispatcher, collector = sink
    try:
        raise_deep()
    except ValueError as exc:
        ContextLogger("f").critical("fatal", exception=exc)
    dispatcher.flush()

    entry = entries(collector)[0]
    assert entry["exception_type"] == "ValueError"
    assert "in inner" in entry["traceback"]


def test_a_filtered_record_does_not_format_a_traceback(sink, monkeypatch):
    """Formatting a traceback is expensive; it must not happen for a dropped record."""
    import loglyte.core

    from .conftest import ListSink

    dispatcher, _ = sink
    dispatcher.sinks = [ListSink(level="ERROR")]

    calls = []
    monkeypatch.setattr(loglyte.core, "_exception_fields", lambda exc: calls.append(1) or {})
    try:
        raise_deep()
    except ValueError:
        ContextLogger("f").debug("dropped", exc_info=True)
    assert calls == []


def test_decorators_attach_tracebacks(sink):
    dispatcher, collector = sink

    @logger.critical
    def fails(log=None):
        raise_deep()

    with pytest.raises(ValueError):
        fails()
    dispatcher.flush()
    assert "in inner" in entries(collector)[0]["traceback"]

    collector.lines.clear()

    @catch(reraise=False)
    def caught(log=None):
        raise_deep()

    caught()
    dispatcher.flush()
    assert "in inner" in entries(collector)[0]["traceback"]
