import pytest

from loglyte.sinks import SINK, DispatcherSink, StdoutSink


class ListSink(StdoutSink):
    """Collects rendered lines in memory instead of writing them anywhere."""

    __slots__ = ("lines",)

    def __init__(self, level: str = "INFO", json_mode: bool = False, **kwargs):
        super().__init__(level, json_mode, **kwargs)
        self.lines: list[str] = []

    def write_batch(self, items):
        payload = self._render(items)
        if payload:
            self.lines.extend(payload.splitlines())


@pytest.fixture
def dispatcher():
    """An isolated dispatcher, so tests never touch the global SINK."""
    d = DispatcherSink(capacity=1000, flush_threshold=1_000_000, flush_interval=0)
    yield d
    d.stop(flush=False)


@pytest.fixture
def sink(monkeypatch):
    """Route the module-level SINK used by ContextLogger into a fresh dispatcher."""
    d = DispatcherSink(capacity=1000, flush_threshold=1_000_000, flush_interval=0)
    collector = ListSink(level="DEBUG", json_mode=True)
    d.sinks = [collector]

    import loglyte.core

    monkeypatch.setattr(loglyte.core, "SINK", d)
    try:
        yield d, collector
    finally:
        d.stop(flush=False)


@pytest.fixture(autouse=True)
def _keep_global_sink_quiet():
    """Stop the real SINK from emitting anything a test happened to leak into it."""
    yield
    SINK._buffer.clear()
    SINK._dropped = 0
