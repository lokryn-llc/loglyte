"""Formatting, rotation and filesystem behaviour of the concrete sinks."""

import gzip
import json
import multiprocessing
import os
import time

import pytest

from loglyte.sinks import FileSink, StderrSink, StdoutSink, StreamSink

from .conftest import ListSink


def record(level="INFO", func="f", msg="hello", extra=None, **kw):
    return (1700000000.0, level, func, msg, {**(extra or {}), **kw})


def _write_records_in_process(path, prefix, *, max_bytes=10_000_000):
    sink = FileSink(path, level="INFO", process_safe=True, max_bytes=max_bytes, retention_count=100)
    for number in range(10):
        sink.write_batch([record(msg=f"{prefix}-{number}")])


# -- formatting ---------------------------------------------------------------


def test_json_mode_emits_one_object_per_line():
    s = ListSink(level="INFO", json_mode=True)
    s.write_batch([record(order_id=7)])
    payload = json.loads(s.lines[0])
    assert payload == {
        "timestamp": 1700000000.0,
        "level": "INFO",
        "logger": "f",
        "message": "hello",
        "order_id": 7,
    }


def test_non_serialisable_value_does_not_destroy_the_batch():
    """json.dumps used to raise mid-flush, taking every well-formed record with it."""
    s = ListSink(level="INFO", json_mode=True)
    s.write_batch([record(msg="first"), record(conn=object()), record(msg="third")])

    assert len(s.lines) == 3, "all three records survived"
    assert isinstance(json.loads(s.lines[1])["conn"], str), "coerced, not dropped"


def test_a_record_that_cannot_be_formatted_is_skipped_not_fatal(capsys):
    class Unprintable:
        def __repr__(self):
            raise ValueError("nope")

    s = ListSink(level="INFO", json_mode=False)
    s.write_batch([record(msg="before"), record(bad=Unprintable()), record(msg="after")])

    assert len(s.lines) == 2
    assert "before" in s.lines[0] and "after" in s.lines[1]
    assert "loglyte internal error" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["timestamp", "level", "logger", "message"])
def test_extra_cannot_overwrite_a_reserved_field(field):
    s = ListSink(level="INFO", json_mode=True)
    s.write_batch([record(msg="real message", extra={field: "spoofed"})])
    payload = json.loads(s.lines[0])

    assert payload[field] != "spoofed", "reserved field must win"
    assert payload[f"extra_{field}"] == "spoofed", "user value preserved, not discarded"


def test_level_filtering_within_a_sink():
    s = ListSink(level="ERROR")
    s.write_batch([record(level="INFO"), record(level="ERROR", msg="boom")])
    assert len(s.lines) == 1 and "boom" in s.lines[0]


def test_logfmt_render_mode_is_available_at_the_sink_layer():
    s = ListSink(level="INFO", render_mode="logfmt")
    s.write_batch([record(order_id=7, user="Ada Lovelace")])

    assert 'level="INFO"' in s.lines[0]
    assert "order_id=7" in s.lines[0]
    assert 'user="Ada Lovelace"' in s.lines[0]


def test_stream_sink_colours_text_only_when_requested():
    class TtyStream:
        def __init__(self):
            self.payload = ""

        def isatty(self):
            return True

        def write(self, value):
            self.payload += value
            return len(value)

        def flush(self):
            pass

    class TtySink(StdoutSink):
        def __init__(self, stream, **kwargs):
            super().__init__(**kwargs)
            self.stream = stream

        def _stream(self):
            return self.stream

    stream = TtyStream()
    TtySink(stream, color="auto").write_batch([record(level="WARNING")])
    assert stream.payload.startswith("\x1b[33m[WARNING]")
    assert stream.payload.endswith("\x1b[0m")


def test_sampling_and_rate_limiting_happen_in_the_sink_not_the_producer():
    sampled = ListSink(level="INFO", json_mode=True, sample=0)
    sampled.write_batch([record()])
    assert sampled.lines == []

    limited = ListSink(level="INFO", json_mode=True, rate_limit=1)
    limited.write_batch([record(msg="first"), record(msg="second")])
    assert len(limited.lines) == 1


def test_custom_renderer_receives_each_accepted_record():
    def renderer(ts, level, logger, message, extra):
        return f"{level}:{logger}:{message}:{extra['order_id']}\n"

    s = ListSink(level="INFO", renderer=renderer)
    s.write_batch([record(order_id=7)])
    assert s.lines == ["INFO:f:hello:7"]


def test_redact_fields_replaces_sensitive_values_at_dispatch():
    s = ListSink(level="INFO", json_mode=True, redact_fields=["password", "token"])
    s.write_batch([record(password="secret", token="abc", user="ada")])
    payload = json.loads(s.lines[0])

    assert payload["password"] == "***"
    assert payload["token"] == "***"
    assert payload["user"] == "ada"


def test_custom_redactor_can_transform_every_field():
    s = ListSink(
        level="INFO",
        json_mode=True,
        redactor=lambda key, value: "masked" if key == "email" else value,
    )
    s.write_batch([record(email="ada@example.test", user="ada")])
    payload = json.loads(s.lines[0])

    assert payload["email"] == "masked"
    assert payload["user"] == "ada"


def test_iso_timestamp_is_an_opt_in_text_renderer():
    s = ListSink(level="INFO", timestamp_mode="iso")
    s.write_batch([record()])
    assert s.lines[0].startswith("2023-11-14T22:13:20.000Z [INFO]")


def test_sink_filters_run_at_dispatch_and_can_be_removed():
    s = ListSink(level="INFO")

    def only_payments(item):
        return item[4].get("component") == "payments"

    s.add_filter(only_payments)
    s.write_batch([record(component="payments"), record(component="search")])
    assert len(s.lines) == 1

    s.remove_filter(only_payments)
    s.write_batch([record(component="search")])
    assert len(s.lines) == 2


def test_a_failing_filter_skips_only_its_record(capsys):
    def broken_filter(item):
        if item[3] == "bad":
            raise RuntimeError("bad filter")
        return True

    s = ListSink(level="INFO")
    s.add_filter(broken_filter)
    s.write_batch([record(msg="good"), record(msg="bad"), record(msg="also good")])

    assert len(s.lines) == 2
    assert "loglyte internal error" in capsys.readouterr().err


@pytest.mark.parametrize("level", ["TRACE", "", None])
def test_invalid_sink_level_is_rejected(level):
    with pytest.raises(ValueError, match="Unknown log level"):
        StderrSink(level=level)


def test_stdout_sink_writes_and_flushes(capsys):
    StdoutSink(level="INFO").write_batch([record(msg="to stdout")])
    assert "to stdout" in capsys.readouterr().out


def test_stream_sink_retries_partial_writes_until_the_payload_is_complete():
    class PartialStream:
        def __init__(self):
            self.payload = ""

        def write(self, value):
            count = min(3, len(value))
            self.payload += value[:count]
            return count

        def flush(self):
            pass

    class PartialSink(StreamSink):
        def __init__(self, stream):
            super().__init__(level="INFO")
            self.stream = stream

        def _stream(self):
            return self.stream

    stream = PartialStream()
    PartialSink(stream).write_batch([record(msg="partial")])
    assert stream.payload == "[INFO] f: partial\n"


# -- file sink ----------------------------------------------------------------


def test_parent_directories_are_created(tmp_path):
    path = tmp_path / "deep" / "nested" / "app.log"
    FileSink(str(path), level="INFO").write_batch([record()])
    assert path.exists()


def test_rotation_within_one_second_keeps_every_file(tmp_path):
    """strftime has one-second resolution; os.rename silently overwrote the
    previous rotated file when two rotations landed in the same second."""
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="INFO", max_bytes=100, retention_count=100)

    for _ in range(5):
        s.write_batch([record(msg="y" * 200)])

    rotated = list(tmp_path.glob("app.log.*"))
    assert len(rotated) == 4, f"expected 4 rotated files, found {len(rotated)}: {rotated}"


def test_retention_prunes_oldest_first(tmp_path):
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="INFO", max_bytes=100, retention_count=3)
    for _ in range(8):
        s.write_batch([record(msg="y" * 200)])

    rotated = sorted(tmp_path.glob("app.log.*"), key=os.path.getmtime)
    assert len(rotated) <= 3


def test_time_based_rotation(tmp_path):
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="INFO", max_bytes=0, rotation_seconds=1, retention_count=10)

    s.write_batch([record(msg="first")])
    assert list(tmp_path.glob("app.log.*")) == [], "too early to rotate"

    time.sleep(1.05)
    s.write_batch([record(msg="second")])
    assert len(list(tmp_path.glob("app.log.*"))) == 1


def test_idle_period_does_not_force_a_spurious_rotation(tmp_path):
    """The interval clock must advance even while no file exists, or the first
    write after an idle gap rotates a file that was just created."""
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="INFO", max_bytes=0, rotation_seconds=1, retention_count=10)

    time.sleep(1.05)  # elapse the interval before anything is ever written
    s.write_batch([record(msg="first")])
    s.write_batch([record(msg="second")])

    assert list(tmp_path.glob("app.log.*")) == []
    assert "first" in path.read_text() and "second" in path.read_text()


def test_glob_metacharacters_in_path_do_not_break_retention(tmp_path):
    path = tmp_path / "app[0].log"
    s = FileSink(str(path), level="INFO", max_bytes=100, retention_count=2)
    for _ in range(6):
        s.write_batch([record(msg="y" * 200)])

    rotated = [p for p in tmp_path.iterdir() if p.name.startswith("app[0].log.")]
    assert len(rotated) <= 2, "glob.escape missing: retention silently did nothing"


def test_max_bytes_zero_disables_size_rotation(tmp_path):
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="INFO", max_bytes=0)
    for _ in range(5):
        s.write_batch([record(msg="y" * 500)])
    assert list(tmp_path.glob("app.log.*")) == []


def test_gzip_compression_runs_after_rotation(tmp_path):
    path = tmp_path / "app.log"
    sink = FileSink(str(path), level="INFO", max_bytes=10, compression="gzip")
    sink.write_batch([record(msg="first" * 20)])
    sink.write_batch([record(msg="second" * 20)])
    compressed = list(tmp_path.glob("app.log.*.gz"))
    assert len(compressed) == 1
    with gzip.open(compressed[0], "rt") as file:
        assert "first" in file.read()


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"max_bytes": -1}, "max_bytes"),
        ({"rotation_seconds": -1}, "rotation_seconds"),
        ({"retention_count": -1}, "retention_count"),
    ],
)
def test_invalid_file_rotation_configuration_is_rejected(tmp_path, kwargs, name):
    with pytest.raises(ValueError, match=name):
        FileSink(str(tmp_path / "app.log"), **kwargs)


def test_filtered_out_batch_does_not_trigger_rotation(tmp_path):
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="ERROR", max_bytes=10)
    s.write_batch([record(level="INFO", msg="y" * 500)])
    assert not path.exists()
    assert list(tmp_path.glob("app.log.*")) == []


def test_process_safe_file_sink_serialises_multiple_processes(tmp_path):
    path = tmp_path / "shared.log"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_write_records_in_process, args=(str(path), prefix))
        for prefix in ("one", "two", "three")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    lines = path.read_text().splitlines()
    assert len(lines) == 30
    assert {line.rsplit(": ", 1)[1] for line in lines} == {
        f"{prefix}-{number}" for prefix in ("one", "two", "three") for number in range(10)
    }


def test_process_safe_file_sink_coordinates_rotation_and_retains_its_lock(tmp_path):
    path = tmp_path / "shared.log"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_write_records_in_process,
            args=(str(path), prefix),
            kwargs={"max_bytes": 40},
        )
        for prefix in ("one", "two")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert path.with_name("shared.log.lock").exists()
    rotated = [
        candidate for candidate in tmp_path.glob("shared.log.*") if candidate.suffix != ".lock"
    ]
    assert rotated
