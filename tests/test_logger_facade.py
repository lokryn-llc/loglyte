"""The intended one-import API for applications."""

import json

import pytest

from loglyte import catch, logger


def payloads(collector):
    return [json.loads(line) for line in collector.lines]


def test_global_logger_needs_no_factory(sink):
    dispatcher, collector = sink
    logger.info("started", port=8080)
    dispatcher.flush()

    entry = payloads(collector)[0]
    assert entry["logger"] == "loglyte"
    assert entry["port"] == 8080


def test_logger_decorator_injects_function_aware_log_and_binding(sink):
    dispatcher, collector = sink

    @logger(bind=("request_id",))
    def checkout(order_id, request_id, log=None):
        log.info("checkout", order_id=order_id)

    checkout("ord_1", "req_1")
    dispatcher.flush()

    entry = payloads(collector)[0]
    assert entry["logger"].endswith("checkout")
    assert entry["request_id"] == "req_1"
    assert entry["order_id"] == "ord_1"


def test_global_logger_inherits_decorator_binding(sink):
    dispatcher, collector = sink

    @logger(bind=("request_id",))
    def checkout(request_id):
        logger.info("child event")

    checkout("req_1")
    dispatcher.flush()
    assert payloads(collector)[0]["request_id"] == "req_1"


def test_injected_logger_can_be_passed_to_helper(sink):
    dispatcher, collector = sink

    def helper(log):
        log.info("child event")

    @logger(bind=("job_id",))
    def run(job_id, log=None):
        helper(log)

    run("job_1")
    dispatcher.flush()
    entry = payloads(collector)[0]
    assert entry["logger"].endswith("run")
    assert entry["job_id"] == "job_1"


def test_scoped_binding_works_for_global_and_bound_calls(sink):
    dispatcher, collector = sink
    with logger.bind(request_id="req_1") as log:
        logger.info("global")
        log.info("scoped")
    dispatcher.flush()

    first, second = payloads(collector)
    assert first["request_id"] == second["request_id"] == "req_1"


def test_critical_decorator_logs_and_reraises(sink):
    dispatcher, collector = sink

    @logger.critical(bind=("request_id",))
    def fail(request_id, log=None):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        fail("req_1")
    dispatcher.flush()

    entry = payloads(collector)[0]
    assert entry["level"] == "CRITICAL"
    assert entry["request_id"] == "req_1"
    assert entry["exception_type"] == "ValueError"


def test_class_decorator_wraps_public_instance_static_and_class_methods(sink):
    dispatcher, collector = sink

    @logger(bind=("request_id",))
    class Service:
        def run(self, request_id, log=None):
            log.info("run")

        @staticmethod
        def static(request_id, log=None):
            log.info("static")

        @classmethod
        def named(cls, request_id, log=None):
            log.info("named")

        def _private(self, request_id, log=None):
            logger.info("private")

    service = Service()
    service.run("req_1")
    Service.static("req_2")
    Service.named("req_3")
    service._private("req_4")
    dispatcher.flush()

    entries = payloads(collector)
    assert [entry["request_id"] for entry in entries[:3]] == ["req_1", "req_2", "req_3"]
    assert "request_id" not in entries[3]


def test_bad_decorator_binding_fails_at_import_time():
    with pytest.raises(TypeError, match="no such parameter"):

        @logger(bind=("missing",))
        def handler(value):
            return value


def test_decorator_binding_includes_defaulted_arguments(sink):
    dispatcher, collector = sink

    @logger(bind=("request_id",))
    def handler(request_id="default", log=None):
        log.info("handled")

    handler()
    dispatcher.flush()
    assert payloads(collector)[0]["request_id"] == "default"


def test_catch_retains_its_distinct_selective_suppression_use_case(sink):
    dispatcher, collector = sink

    @catch(ValueError, reraise=False, message="background work failed")
    def background(log=None):
        raise ValueError("boom")

    assert background() is None
    dispatcher.flush()
    entry = payloads(collector)[0]
    assert entry["message"] == "background work failed"
    assert entry["exception_type"] == "ValueError"
