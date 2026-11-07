"""Buffering, flushing and delivery guarantees of DispatcherSink."""

import threading
import time

import pytest

from loglyte.sinks import DispatcherSink, StdoutSink

from .conftest import ListSink


def test_reaching_the_threshold_flushes_without_a_manual_call():
    """Records must reach sinks on their own; a library that only flushes at exit
    loses everything a crash interrupts."""
    d = DispatcherSink(capacity=1000, flush_threshold=10, flush_interval=0)
    collector = ListSink(level="INFO")
    d.sinks = [collector]

    for i in range(9):
        d.push("INFO", f"m{i}", "f")
    assert collector.lines == [], "should still be batching"

    d.push("INFO", "m9", "f")
    assert len(collector.lines) == 10, "threshold reached -> batch delivered"


def test_background_thread_flushes_on_its_own():
    d = DispatcherSink(capacity=1000, flush_threshold=1_000_000, flush_interval=0.05)
    collector = ListSink(level="INFO")
    d.sinks = [collector]
    try:
        d.push("INFO", "hello", "f")
        deadline = time.monotonic() + 5
        while not collector.lines and time.monotonic() < deadline:
            time.sleep(0.01)
        assert collector.lines, "periodic flusher never delivered the record"
    finally:
        d.stop(flush=False)


def test_worker_is_not_started_until_something_is_logged():
    d = DispatcherSink(flush_interval=0.05)
    assert d._worker is None, "importing/creating must not spawn a thread"
    d.sinks = [ListSink(level="INFO")]
    d.push("INFO", "x", "f")
    assert d._worker is not None
    d.stop(flush=False)


def test_records_no_sink_accepts_are_dropped_at_push(dispatcher):
    """Filtering at write time let discarded DEBUG traffic evict real records."""
    collector = ListSink(level="INFO")
    dispatcher.sinks = [collector]

    for i in range(500):
        dispatcher.push("DEBUG", f"noise-{i}", "f")
    assert len(dispatcher._buffer) == 0, "DEBUG must not occupy buffer space"

    dispatcher.push("ERROR", "PAYMENT FAILED", "f")
    dispatcher.flush()
    assert any("PAYMENT FAILED" in line for line in collector.lines)


def test_an_error_survives_a_flood_of_filtered_debug(dispatcher):
    """The exact regression: 1 ERROR followed by a full ring of DEBUG used to
    evict the ERROR before anything was written."""
    d = DispatcherSink(capacity=10, flush_threshold=1_000_000, flush_interval=0)
    collector = ListSink(level="INFO")
    d.sinks = [collector]

    d.push("ERROR", "PAYMENT FAILED - must not be lost", "f")
    for i in range(50):
        d.push("DEBUG", f"noisy-debug-{i}", "f")
    d.flush()

    assert any("PAYMENT FAILED" in line for line in collector.lines)


def test_flush_threshold_is_clamped_to_capacity():
    """A threshold above capacity would guarantee drops, so it is clamped: the
    buffer can never fill without a flush being attempted first."""
    d = DispatcherSink(capacity=10, flush_threshold=1_000_000, flush_interval=0)
    assert d.flush_threshold == 10


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"capacity": 0}, "capacity"),
        ({"capacity": -1}, "capacity"),
        ({"flush_threshold": 0}, "flush_threshold"),
        ({"flush_interval": -0.1}, "flush_interval"),
        ({"flush_interval": float("inf")}, "flush_interval"),
        ({"overflow_policy": "discard"}, "overflow_policy"),
    ],
)
def test_dispatcher_rejects_invalid_configuration(kwargs, name):
    with pytest.raises(ValueError, match=name):
        DispatcherSink(**kwargs)


def _saturate(d, count):
    """Push ``count`` records into a dispatcher whose flusher cannot keep up.

    Raising the threshold past capacity after construction is how we stand in for
    a real backlog (sinks blocked on slow I/O while the application logs a burst)
    without the timing sensitivity of an actual blocked sink.
    """
    d.flush_threshold = 10**9
    for i in range(count):
        d.push("INFO", f"m{i}", "f")


def test_overflow_is_reported_rather_than_silent():
    d = DispatcherSink(capacity=10, flush_threshold=10, flush_interval=0)
    collector = ListSink(level="DEBUG")
    d.sinks = [collector]

    _saturate(d, 25)
    d.flush()

    overflow = [line for line in collector.lines if "buffer overflow" in line]
    assert len(overflow) == 1, "the drop must be announced"
    assert "15 record(s) dropped" in overflow[0]


def test_overflow_warning_is_emitted_once_not_repeatedly():
    d = DispatcherSink(capacity=10, flush_threshold=10, flush_interval=0)
    collector = ListSink(level="DEBUG")
    d.sinks = [collector]

    _saturate(d, 25)
    d.flush()
    collector.lines.clear()

    d.push("INFO", "after", "f")
    d.flush()
    assert not any("buffer overflow" in line for line in collector.lines)


def test_drop_newest_keeps_the_oldest_records_and_reports_its_policy():
    d = DispatcherSink(
        capacity=3,
        flush_threshold=3,
        flush_interval=0,
        overflow_policy="drop_newest",
    )
    collector = ListSink(level="DEBUG", json_mode=True)
    d.sinks = [collector]

    _saturate(d, 5)
    d.flush()

    assert any('"overflow_policy": "drop_newest"' in line for line in collector.lines)
    messages = [line for line in collector.lines if '"message": "m' in line]
    assert [f'"message": "m{i}"' in line for i, line in enumerate(messages)] == [True] * 3


def test_health_reports_overflow_and_sink_failure(dispatcher):
    class Exploding(StdoutSink):
        def write_batch(self, items):
            raise RuntimeError("sink is on fire")

    dispatcher.sinks = [Exploding(level="INFO")]
    _saturate(dispatcher, 1001)
    dispatcher.flush()

    health = dispatcher.health()
    assert health["dropped_records_total"] == 1
    assert health["sink_failures"] == 1
    assert health["last_flush_time"] is not None
    assert health["last_error"] == "Exploding.write_batch"


def test_a_failing_sink_does_not_starve_the_others(dispatcher, capsys):
    class Exploding(StdoutSink):
        def write_batch(self, items):
            raise RuntimeError("sink is on fire")

    good = ListSink(level="INFO")
    dispatcher.sinks = [Exploding(level="INFO"), good]

    dispatcher.push("INFO", "important", "f")
    dispatcher.flush()  # must not raise

    assert any("important" in line for line in good.lines)
    assert "loglyte internal error" in capsys.readouterr().err


def test_retry_policy_retries_only_the_failing_sink():
    class Flaky(ListSink):
        def __init__(self):
            super().__init__(level="INFO")
            self.calls = 0

        def write_batch(self, items):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary outage")
            super().write_batch(items)

    flaky = Flaky()
    d = DispatcherSink(
        flush_interval=0,
        failure_policy="retry",
        retry_attempts=1,
        retry_backoff=0,
    )
    d.sinks = [flaky]
    d.push("INFO", "event", "f")
    d.flush()

    assert flaky.calls == 2
    assert any("event" in line for line in flaky.lines)
    assert d.health()["sink_failures"] == 1


def test_disable_policy_stops_repeatedly_failing_sink_but_keeps_others(capsys):
    class Exploding(StdoutSink):
        def __init__(self):
            super().__init__(level="INFO")
            self.calls = 0

        def write_batch(self, items):
            self.calls += 1
            raise OSError("permanent outage")

    bad, good = Exploding(), ListSink(level="INFO")
    d = DispatcherSink(flush_interval=0, failure_policy="disable")
    d.sinks = [bad, good]
    d.push("INFO", "first", "f")
    d.flush()
    d.push("INFO", "second", "f")
    d.flush()

    assert bad.calls == 1
    assert len(good.lines) == 2
    assert d.health()["disabled_sinks"] == ["Exploding"]
    assert "loglyte internal error" in capsys.readouterr().err


def test_shutdown_timeout_bounds_waiting_for_a_blocked_background_sink():
    class BlockingSink(StdoutSink):
        def __init__(self):
            super().__init__(level="INFO")
            self.started = threading.Event()
            self.release = threading.Event()

        def write_batch(self, items):
            self.started.set()
            assert self.release.wait(timeout=5)

    blocked = BlockingSink()
    d = DispatcherSink(flush_threshold=1_000_000, flush_interval=0.01)
    d.sinks = [blocked]
    d.push("INFO", "waiting", "f")
    assert blocked.started.wait(timeout=5)

    assert d.stop(timeout=0) is False
    assert d.health()["last_shutdown_timed_out"] is True
    blocked.release.set()
    assert d.stop(flush=False, timeout=5) is True


def test_flush_is_serialised_across_threads(tmp_path):
    from loglyte.sinks import FileSink

    path = tmp_path / "a.log"
    d = DispatcherSink(capacity=10_000, flush_threshold=1_000_000, flush_interval=0)
    d.sinks = [FileSink(str(path), level="INFO")]

    for i in range(4000):
        d.push("INFO", f"m{i}", "f")

    threads = [threading.Thread(target=d.flush) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 4000, "no record duplicated or lost"
    order = [int(ln.rsplit("m", 1)[1]) for ln in lines]
    assert order == sorted(order), "concurrent flushes interleaved the batch"


def test_concurrent_push_loses_nothing(dispatcher):
    collector = ListSink(level="INFO")
    dispatcher.sinks = [collector]

    def worker(n):
        for i in range(100):
            dispatcher.push("INFO", f"t{n}-{i}", "f")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dispatcher.flush()

    assert len(collector.lines) == 800


def test_configure_drains_to_the_old_sinks_before_swapping(monkeypatch):
    import loglyte.core
    from loglyte import configure

    d = DispatcherSink(capacity=100, flush_threshold=1_000_000, flush_interval=0)
    old, new = ListSink(level="INFO"), ListSink(level="INFO")
    d.sinks = [old]
    monkeypatch.setattr(loglyte.core, "SINK", d)
    monkeypatch.setattr("loglyte.sinks.SINK", d)

    d.push("INFO", "logged-before-reconfigure", "f")
    configure(sinks=[new])

    assert any("logged-before-reconfigure" in line for line in old.lines)
    assert new.lines == []


def test_configure_with_no_sinks_discards_everything(dispatcher):
    dispatcher.sinks = []
    dispatcher.push("CRITICAL", "nowhere to go", "f")
    assert len(dispatcher._buffer) == 0
    dispatcher.flush()  # must not raise


def test_configure_stops_an_active_worker_when_interval_becomes_zero(monkeypatch):
    from loglyte import configure

    d = DispatcherSink(flush_threshold=1_000_000, flush_interval=10)
    d.sinks = [ListSink(level="INFO")]
    d.push("INFO", "starts-worker", "f")
    worker = d._worker
    assert worker is not None and worker.is_alive()
    monkeypatch.setattr("loglyte.sinks.SINK", d)

    configure(sinks=d.sinks, flush_interval=0)

    assert d._worker is None
    assert not worker.is_alive()
    assert d.flush_interval == 0


def test_configure_sets_the_global_overflow_policy(monkeypatch):
    from loglyte import configure

    d = DispatcherSink(flush_interval=0)
    monkeypatch.setattr("loglyte.sinks.SINK", d)

    configure(sinks=[ListSink(level="INFO")], overflow_policy="drop_newest")

    assert d.overflow_policy == "drop_newest"


def test_configure_restarts_an_active_worker_after_interval_change(monkeypatch):
    from loglyte import configure

    d = DispatcherSink(flush_threshold=1_000_000, flush_interval=10)
    d.sinks = [ListSink(level="INFO")]
    d.push("INFO", "starts-worker", "f")
    first_worker = d._worker
    assert first_worker is not None
    monkeypatch.setattr("loglyte.sinks.SINK", d)

    configure(sinks=d.sinks, flush_interval=0.1)

    assert d._worker is not None and d._worker is not first_worker
    assert d._worker.is_alive()
    d.stop(flush=False)


def test_dispatcher_context_manager_stops_and_flushes():
    collector = ListSink(level="INFO")
    with DispatcherSink(flush_threshold=1_000_000, flush_interval=0) as d:
        d.sinks = [collector]
        d.push("INFO", "delivered-on-exit", "f")

    assert any("delivered-on-exit" in line for line in collector.lines)


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_min_level_is_recomputed_when_sinks_change(dispatcher, level):
    dispatcher.sinks = [ListSink(level=level)]
    from loglyte.sinks import LEVEL_MAP

    assert dispatcher._min_level == LEVEL_MAP[level]
