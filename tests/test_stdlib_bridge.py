"""Routing standard-library logging into loglyte."""

import json
import logging

import pytest

from loglyte.sinks import DispatcherSink
from loglyte.stdlib import LoglyteHandler, install, uninstall

from .conftest import ListSink


@pytest.fixture
def bridged():
    """A dispatcher fed by a stdlib logger, isolated from the root logger."""
    dispatcher = DispatcherSink(capacity=1000, flush_threshold=1_000_000, flush_interval=0)
    collector = ListSink(level="DEBUG", json_mode=True)
    dispatcher.sinks = [collector]

    logger = logging.getLogger("test.bridge")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = LoglyteHandler(logging.DEBUG, dispatcher=dispatcher)
    logger.addHandler(handler)

    yield logger, dispatcher, collector

    logger.removeHandler(handler)
    dispatcher.stop(flush=False)


def entries(collector):
    return [json.loads(line) for line in collector.lines]


def test_a_third_party_log_reaches_loglyte(bridged):
    """The gap this closes: without the bridge, dependencies' logs are invisible."""
    logger, dispatcher, collector = bridged
    logger.warning("connection pool is full")
    dispatcher.flush()

    entry = entries(collector)[0]
    assert entry["message"] == "connection pool is full"
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "test.bridge"


def test_percent_style_arguments_are_interpolated(bridged):
    logger, dispatcher, collector = bridged
    logger.info("retrying %s after %d seconds", "https://x", 5)
    dispatcher.flush()
    assert entries(collector)[0]["message"] == "retrying https://x after 5 seconds"


def test_non_string_messages_are_rendered(bridged):
    class Message:
        def __str__(self):
            return "object message"

    logger, dispatcher, collector = bridged
    logger.info(Message())
    dispatcher.flush()
    assert entries(collector)[0]["message"] == "object message"


@pytest.mark.parametrize(
    "method,level",
    [
        ("debug", "DEBUG"),
        ("info", "INFO"),
        ("warning", "WARNING"),
        ("error", "ERROR"),
        ("critical", "CRITICAL"),
    ],
)
def test_levels_map_across(bridged, method, level):
    logger, dispatcher, collector = bridged
    getattr(logger, method)("m")
    dispatcher.flush()
    assert entries(collector)[0]["level"] == level


def test_a_custom_level_maps_down_instead_of_being_dropped(bridged):
    """An unrecognised level name would score 0 and be filtered out entirely."""
    logger, dispatcher, collector = bridged
    logging.addLevelName(25, "NOTICE")
    logger.log(25, "halfway between INFO and WARNING")
    dispatcher.flush()

    entry = entries(collector)[0]
    assert entry["level"] == "INFO", "NOTICE(25) should land on the level below it"
    assert entry["message"] == "halfway between INFO and WARNING"


def test_extra_fields_are_forwarded_as_structured_data(bridged):
    logger, dispatcher, collector = bridged
    logger.info("charge", extra={"order_id": 7, "region": "eu"})
    dispatcher.flush()

    entry = entries(collector)[0]
    assert entry["order_id"] == 7
    assert entry["region"] == "eu"


def test_exception_and_traceback_are_forwarded(bridged):
    logger, dispatcher, collector = bridged
    try:
        raise ValueError("upstream boom")
    except ValueError:
        logger.exception("request failed")
    dispatcher.flush()

    entry = entries(collector)[0]
    assert entry["exception_type"] == "ValueError"
    assert entry["exception_detail"] == "upstream boom"
    assert "ValueError" in entry["traceback"]


def test_caller_location_is_forwarded(bridged):
    """stdlib already paid for the frame introspection, so pass it on."""
    logger, dispatcher, collector = bridged
    logger.info("located")
    dispatcher.flush()

    entry = entries(collector)[0]
    assert entry["file"].endswith("test_stdlib_bridge.py")
    assert isinstance(entry["line"], int)
    assert entry["function"] == "test_caller_location_is_forwarded"


def test_stack_info_is_forwarded(bridged):
    logger, dispatcher, collector = bridged
    logger.warning("with stack", stack_info=True)
    dispatcher.flush()

    entry = entries(collector)[0]
    assert "test_stack_info_is_forwarded" in entry["stack_info"]


def test_logger_adapter_context_is_forwarded(bridged):
    logger, dispatcher, collector = bridged
    logging.LoggerAdapter(logger, {"request_id": "req_1"}).info("adapter event")
    dispatcher.flush()

    entry = entries(collector)[0]
    assert entry["request_id"] == "req_1"


def test_custom_record_factory_fields_are_forwarded(bridged):
    logger, dispatcher, collector = bridged
    original_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = original_factory(*args, **kwargs)
        record.deployment = "blue"
        return record

    logging.setLogRecordFactory(factory)
    try:
        logger.info("factory event")
    finally:
        logging.setLogRecordFactory(original_factory)
    dispatcher.flush()

    assert entries(collector)[0]["deployment"] == "blue"


def test_location_can_be_switched_off():
    dispatcher = DispatcherSink(flush_threshold=1_000_000, flush_interval=0)
    collector = ListSink(level="DEBUG", json_mode=True)
    dispatcher.sinks = [collector]
    logger = logging.getLogger("test.bridge.noloc")
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(LoglyteHandler(dispatcher=dispatcher, include_location=False))

    logger.warning("no location")
    dispatcher.flush()
    assert "file" not in entries(collector)[0]
    dispatcher.stop(flush=False)


def test_handler_level_filters_before_loglyte_sees_it(bridged):
    logger, dispatcher, collector = bridged
    logger.handlers[0].setLevel(logging.ERROR)
    logger.info("below the handler level")
    logger.error("at the handler level")
    dispatcher.flush()

    messages = [e["message"] for e in entries(collector)]
    assert messages == ["at the handler level"]


def test_a_broken_record_does_not_raise_into_the_caller(bridged, monkeypatch):
    logger, dispatcher, collector = bridged
    monkeypatch.setattr(logging, "raiseExceptions", False)

    class Exploding:
        def __str__(self):
            raise RuntimeError("cannot render")

    logger.info("%s", Exploding())  # must not propagate
    dispatcher.flush()


def test_install_and_uninstall_on_the_root_logger():
    dispatcher = DispatcherSink(flush_threshold=1_000_000, flush_interval=0)
    collector = ListSink(level="DEBUG", json_mode=True)
    dispatcher.sinks = [collector]

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        handler = install(level="INFO", dispatcher=dispatcher)
        logging.getLogger("some.third.party").warning("from a dependency")
        dispatcher.flush()
        assert entries(collector)[0]["logger"] == "some.third.party"

        uninstall(handler)
        collector.lines.clear()
        logging.getLogger("some.third.party").warning("after uninstall")
        dispatcher.flush()
        assert collector.lines == []
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        dispatcher.stop(flush=False)


def test_install_preserves_existing_handlers_and_is_idempotent():
    dispatcher = DispatcherSink(flush_threshold=1_000_000, flush_interval=0)
    collector = ListSink(level="DEBUG", json_mode=True)
    dispatcher.sinks = [collector]
    logger = logging.getLogger("test.bridge.install")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    existing = logging.NullHandler()
    logger.addHandler(existing)
    try:
        first = install(logger=logger, dispatcher=dispatcher)
        second = install(logger=logger, dispatcher=dispatcher)
        logger.warning("once")
        dispatcher.flush()

        assert existing in logger.handlers
        assert first is second
        assert [entry["message"] for entry in entries(collector)] == ["once"]
    finally:
        logger.handlers.clear()
        dispatcher.stop(flush=False)


def test_install_can_explicitly_replace_existing_handlers():
    dispatcher = DispatcherSink(flush_interval=0)
    logger = logging.getLogger("test.bridge.replace")
    logger.handlers.clear()
    logger.propagate = False
    existing = logging.NullHandler()
    logger.addHandler(existing)
    try:
        handler = install(logger=logger, dispatcher=dispatcher, replace_existing=True)
        assert logger.handlers == [handler]
    finally:
        logger.handlers.clear()
        dispatcher.stop(flush=False)
