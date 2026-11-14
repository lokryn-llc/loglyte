"""Regression tests for reusable ``logger.bind()`` scopes (TODO B3).

A ``ScopedLogger`` used to keep its context token on the instance, so re-entering
one scope, or sharing it between threads or tasks, raised errors, hid application
exceptions, and left fields attached to later records. Interleavings here are forced
with events rather than timing, so every test is deterministic.
"""

import asyncio
import contextvars
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from loglyte import logger
from loglyte.context import bind, get_context

TIMEOUT = 5


def payloads(collector):
    return [json.loads(line) for line in collector.lines]


def wait(event):
    assert event.wait(TIMEOUT), "test thread did not reach its checkpoint"


def run_threads(*targets):
    threads = [threading.Thread(target=target) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT)
        assert not thread.is_alive()


# -- nested re-use --------------------------------------------------------------


def test_same_scope_can_be_nested():
    scope = logger.bind(request_id="r1")
    with scope:
        with scope:
            assert get_context() == {"request_id": "r1"}
        assert get_context() == {"request_id": "r1"}
    assert get_context() == {}


def test_nested_reuse_keeps_the_scopes_in_between():
    outer = logger.bind(a=1)
    with outer:
        with logger.bind(b=2):
            with outer:
                assert get_context() == {"a": 1, "b": 2}
            assert get_context() == {"a": 1, "b": 2}
        assert get_context() == {"a": 1}
    assert get_context() == {}


def test_scope_can_be_reused_after_its_body_raised():
    scope = logger.bind(request_id="r1")
    with pytest.raises(KeyError), scope:
        raise KeyError("boom")
    assert get_context() == {}
    with scope:
        assert get_context() == {"request_id": "r1"}
    assert get_context() == {}


def test_nested_reuse_binds_records(sink):
    dispatcher, collector = sink
    scope = logger.bind(request_id="r1")
    with scope:
        with scope as log:
            log.info("inner")
        logger.info("outer")
    logger.info("after")
    dispatcher.flush()

    inner, outer, after = payloads(collector)
    assert inner["request_id"] == outer["request_id"] == "r1"
    assert "request_id" not in after


# -- sharing between threads ----------------------------------------------------


@pytest.mark.parametrize("first_out", ["first-in", "second-in"])
def test_scope_shared_by_two_threads(sink, first_out):
    dispatcher, collector = sink
    shared = logger.bind(request_id="shared")
    first_in, second_in, first_done = threading.Event(), threading.Event(), threading.Event()
    results = {}

    def first():
        try:
            with shared:
                first_in.set()
                wait(second_in)
                if first_out == "second-in":
                    wait(first_done)
                logger.info("from first", who="first")
            results["first"] = get_context()
        finally:
            first_done.set()

    def second():
        wait(first_in)
        with shared:
            second_in.set()
            if first_out == "first-in":
                wait(first_done)
            logger.info("from second", who="second")
        results["second"] = get_context()
        first_done.set()

    run_threads(first, second)
    dispatcher.flush()

    # Neither thread raised, and neither kept the fields after leaving the scope.
    assert results == {"first": {}, "second": {}}
    entries = payloads(collector)
    assert sorted(e["who"] for e in entries) == ["first", "second"]
    assert all(e["request_id"] == "shared" for e in entries)


def test_shared_scope_does_not_leak_into_later_work_on_pooled_threads(sink):
    dispatcher, collector = sink
    shared = logger.bind(request_id="req-A")
    a_in, b_in, a_done = threading.Event(), threading.Event(), threading.Event()

    def request_a():
        try:
            with shared:
                a_in.set()
                wait(b_in)
        finally:
            a_done.set()

    def request_b():
        with shared:
            b_in.set()
            wait(a_done)

    def later_job(tag):
        logger.info("later job", job=tag)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(request_a)
        wait(a_in)
        future_b = pool.submit(request_b)
        future_a.result(TIMEOUT)
        future_b.result(TIMEOUT)
        for i in range(6):
            pool.submit(later_job, i).result(TIMEOUT)
    dispatcher.flush()

    entries = payloads(collector)
    assert [e["job"] for e in entries] == list(range(6))
    assert all("request_id" not in e for e in entries)


def test_application_exception_is_not_replaced_when_scope_is_shared():
    shared = logger.bind(request_id="x")
    raised_in, held_in, raised_out = threading.Event(), threading.Event(), threading.Event()
    seen = {}

    def raising():
        try:
            with shared:
                raised_in.set()
                wait(held_in)
                raise KeyError("application error")
        except BaseException as exc:
            seen["exc"] = exc
        finally:
            raised_out.set()

    def holding():
        wait(raised_in)
        with shared:
            held_in.set()
            wait(raised_out)

    run_threads(raising, holding)
    assert type(seen["exc"]) is KeyError
    assert seen["exc"].__context__ is None
    assert not getattr(seen["exc"], "__notes__", None)


def test_shared_scope_leaves_no_unraisable_errors(monkeypatch):
    """The old scope dropped entered generators, whose finalisers failed on other threads."""
    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    shared = logger.bind(request_id="x")
    first_in, second_in = threading.Event(), threading.Event()

    def first():
        with shared:
            first_in.set()
            wait(second_in)

    def second():
        wait(first_in)
        with shared:
            second_in.set()

    run_threads(first, second)
    assert unraisable == []


# -- sharing between asyncio tasks ----------------------------------------------


async def test_scope_shared_by_many_tasks():
    shared = logger.bind(service="api")
    gate = asyncio.Event()

    async def request(n):
        with shared, bind(request_id=n):
            await gate.wait()
            ctx = get_context()
        return ctx, get_context()

    tasks = [asyncio.create_task(request(n)) for n in range(20)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)

    for n, (inside, after) in enumerate(results):
        assert inside == {"service": "api", "request_id": n}
        assert after == {}
    assert get_context() == {}


# -- out-of-order exit: reported, and the context is repaired --------------------


def interleaved_scopes(first_scope, second_scope):
    """Two generators advanced so the first scope exits while the second is open."""

    def hold(scope):
        with scope:
            yield

    first, second = hold(first_scope), hold(second_scope)
    next(first)
    next(second)
    return first, second


@pytest.mark.parametrize(
    "make_scope",
    [lambda **f: logger.bind(**f), lambda **f: bind(**f)],
    ids=["logger.bind", "context.bind"],
)
def test_out_of_order_exit_raises_and_repairs_the_context(make_scope):
    first, second = interleaved_scopes(make_scope(a=1), make_scope(b=2))
    assert get_context() == {"a": 1, "b": 2}

    with pytest.raises(RuntimeError, match="out of order"):
        next(first)
    # Only the exited scope's fields were removed; the open scope keeps its own.
    assert get_context() == {"b": 2}

    with pytest.raises(StopIteration):
        next(second)
    assert get_context() == {}


def test_logging_continues_normally_after_an_out_of_order_exit(sink):
    dispatcher, collector = sink
    first, second = interleaved_scopes(logger.bind(a=1), logger.bind(b=2))
    with pytest.raises(RuntimeError):
        next(first)
    logger.info("while second is open")
    with pytest.raises(StopIteration):
        next(second)
    logger.info("after both")
    with logger.bind(c=3):
        logger.info("fresh scope")
    dispatcher.flush()

    during, after, fresh = payloads(collector)
    assert during["b"] == 2 and "a" not in during
    assert "a" not in after and "b" not in after
    assert fresh["c"] == 3 and "a" not in fresh and "b" not in fresh


def test_out_of_order_exit_does_not_replace_the_application_exception():
    first = logger.bind(a=1)
    second = logger.bind(b=2)
    first.__enter__()
    second.__enter__()
    error = KeyError("application error")

    if sys.version_info >= (3, 11):
        assert first.__exit__(KeyError, error, None) is None
        assert any("out of order" in note for note in error.__notes__)
    else:  # pragma: no cover - Python 3.10 has no exception notes
        with pytest.warns(RuntimeWarning, match="out of order"):
            assert first.__exit__(KeyError, error, None) is None

    assert get_context() == {"b": 2}
    second.__exit__(None, None, None)
    assert get_context() == {}


# -- exit without a matching enter ------------------------------------------------


def test_exit_without_enter_raises_and_changes_nothing():
    scope = logger.bind(a=1)
    with pytest.raises(RuntimeError, match="not entered"):
        scope.__exit__(None, None, None)

    with logger.bind(b=2):
        with pytest.raises(RuntimeError, match="not entered"):
            scope.__exit__(None, None, None)
        assert get_context() == {"b": 2}
    assert get_context() == {}


def test_exit_in_a_context_that_never_entered_the_scope():
    scope = logger.bind(a=1)
    with scope:
        other = contextvars.Context()
        with pytest.raises(RuntimeError, match="not entered"):
            other.run(scope.__exit__, None, None, None)
        assert get_context() == {"a": 1}
    assert get_context() == {}
