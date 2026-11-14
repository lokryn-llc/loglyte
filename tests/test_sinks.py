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


def _write_records_in_process(path, prefix, *, max_bytes=10_000_000, compression=None):
    sink = FileSink(
        path,
        level="INFO",
        process_safe=True,
        max_bytes=max_bytes,
        retention_count=100,
        compression=compression,
    )
    for number in range(10):
        sink.write_batch([record(msg=f"{prefix}-{number}")])


def _write_records_unlocked(path, prefix, start, compression):
    """One of several processes writing one file WITHOUT process_safe, so every
    rotation decision races with the others. ``start`` releases them together."""
    sink = FileSink(
        path, level="INFO", max_bytes=200, retention_count=None, compression=compression
    )
    start.wait()
    for number in range(40):
        sink.write_batch([record(msg=f"{prefix}-{number}")])


def _run_unlocked_writers(tmp_path, compression, writers=4):
    context = multiprocessing.get_context("spawn")
    start = context.Barrier(writers)
    processes = [
        context.Process(
            target=_write_records_unlocked,
            args=(str(tmp_path / "app.log"), f"w{index}", start, compression),
        )
        for index in range(writers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
    return [process.exitcode for process in processes]


def _freeze_rotation_stamp(monkeypatch, stamp="2026-01-01_00-00-00"):
    """Pin strftime so every rotation in a test lands in the same second."""
    real_strftime = time.strftime
    monkeypatch.setattr(
        time,
        "strftime",
        lambda fmt, *args: stamp if fmt == "%Y-%m-%d_%H-%M-%S" else real_strftime(fmt, *args),
    )


def _read_log(path):
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt") as file:
        return file.read()


def _age(path, seconds_old):
    when = time.time() - seconds_old
    os.utime(path, (when, when))


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


def test_rotations_within_one_second_keep_every_file(tmp_path, monkeypatch):
    """The timestamp is only second-resolution, so the name carries a unique token."""
    _freeze_rotation_stamp(monkeypatch)
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="INFO", max_bytes=100, retention_count=100)

    for batch in range(5):
        s.write_batch([record(msg=f"batch-{batch}-" + "y" * 200)])

    text = "".join(_read_log(p) for p in tmp_path.iterdir())
    assert [batch for batch in range(5) if f"batch-{batch}-" not in text] == []


def test_gzip_rotations_within_one_second_keep_every_archive(tmp_path, monkeypatch):
    """Regression: the compressed name was never checked, so each rotation inside one
    second overwrote the previous ``.gz`` and its records vanished silently."""
    _freeze_rotation_stamp(monkeypatch)
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="INFO", max_bytes=100, retention_count=100, compression="gzip")

    for batch in range(5):
        s.write_batch([record(msg=f"batch-{batch}-" + "y" * 200)])

    assert len(list(tmp_path.glob("app.log.*.gz"))) == 4
    text = "".join(_read_log(p) for p in tmp_path.iterdir())
    assert [batch for batch in range(5) if f"batch-{batch}-" not in text] == []


def test_rotation_target_is_claimed_not_merely_checked(tmp_path, monkeypatch):
    """Two claims in the same second must differ, and each must already exist on disk:
    a name that is only *observed* free can be taken by a concurrent rotation."""
    _freeze_rotation_stamp(monkeypatch)
    s = FileSink(str(tmp_path / "app.log"), level="INFO", compression="gzip")

    first, second = s._claim_rotated_path(), s._claim_rotated_path()

    assert first != second
    for claimed in (first, second):
        assert os.path.exists(claimed) and os.path.exists(f"{claimed}.gz")


def test_rotation_skipped_when_another_process_rotated_first(tmp_path, monkeypatch):
    """A lost rotation race is not an error, and must not leave claimed names behind."""
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="INFO", max_bytes=100, retention_count=100, compression="gzip")
    s.write_batch([record(msg="y" * 200)])
    before = {p.name for p in tmp_path.iterdir()}

    def vanished(src, dst):
        raise FileNotFoundError(src)

    monkeypatch.setattr(os, "replace", vanished)
    s.write_batch([record(msg="z" * 200)])  # triggers a rotation that loses the race

    assert {p.name for p in tmp_path.iterdir()} == before
    assert "z" * 200 in path.read_text()


def test_a_claimed_name_cannot_be_taken_by_a_concurrent_rotation(tmp_path, monkeypatch):
    """Two writers (sinks, or processes) that draw the same token in the same second
    must still get different names: the first claim exists on disk, so the second
    writer's exclusive create fails and it draws again. An existence check alone
    would hand both of them the same name, and the second rename would overwrite."""
    _freeze_rotation_stamp(monkeypatch)
    tokens = iter([b"\x00" * 4, b"\x00" * 4, b"\x11" * 4])
    monkeypatch.setattr(os, "urandom", lambda size: next(tokens))
    path = str(tmp_path / "app.log")
    first_writer, second_writer = FileSink(path), FileSink(path)

    first = first_writer._claim_rotated_path()  # claimed, not yet renamed onto
    second = second_writer._claim_rotated_path()

    assert first.endswith(".00000000")
    assert second.endswith(".11111111")


def test_log_rotated_away_mid_check_is_not_an_error(tmp_path, monkeypatch):
    """Another process can rotate the file away between our size check and our use of
    the result. That is a lost race, not a failure: the batch must still be written."""
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="INFO", max_bytes=100, retention_count=None)
    s.write_batch([record(msg="y" * 200)])
    real_stat = os.stat
    rotated_by_someone_else = []

    def stat_then_lose_the_race(target, *args, **kwargs):
        result = real_stat(target, *args, **kwargs)
        if os.fspath(target) == str(path) and not rotated_by_someone_else:
            rotated_by_someone_else.append(True)
            os.rename(path, tmp_path / "app.log.2020-01-01_00-00-00.cafef00d")
        return result

    monkeypatch.setattr(os, "stat", stat_then_lose_the_race)
    s.write_batch([record(msg="after the race")])

    assert "after the race" in path.read_text()


def test_unlocked_processes_rotating_one_file_lose_nothing(tmp_path):
    """Regression: without process_safe, concurrent rotations picked the same name
    (the later rename overwrote the earlier archive) and the losers crashed with
    FileNotFoundError. Under contention most runs lost most of their records."""
    exit_codes = _run_unlocked_writers(tmp_path, compression=None)

    assert exit_codes == [0, 0, 0, 0]
    files = list(tmp_path.iterdir())
    assert [f.name for f in files if f.stat().st_size == 0] == [], "claim placeholder left"
    lines = "".join(_read_log(f) for f in files).splitlines()
    messages = sorted(line.rsplit(": ", 1)[1] for line in lines)
    assert messages == sorted(f"w{index}-{number}" for index in range(4) for number in range(40))


def test_unlocked_gzip_processes_rotating_one_file_do_not_crash_or_collide(tmp_path):
    """The gzip variant of the race. Every process finishes, every archive is a
    readable gzip file under its own name, and no uncompressed file is left behind.

    Record *completeness* is deliberately not asserted here: an unlocked writer can
    still append to a file after another process compressed it (TODO.md B6), which
    this test would hit intermittently. That gap is pinned, deterministically, by
    ``test_record_written_through_a_handle_opened_before_rotation_is_kept``."""
    exit_codes = _run_unlocked_writers(tmp_path, compression="gzip")

    assert exit_codes == [0, 0, 0, 0]
    rotated = [f for f in tmp_path.iterdir() if f.name != "app.log"]
    assert rotated and all(f.name.endswith(".gz") for f in rotated), "uncompressed leftover"
    for archive in rotated:
        # Decompresses completely (a truncated or overwritten archive raises). It may
        # legitimately be empty: a process that decided to rotate on a stale size can
        # rotate a file another process has only just created (TODO.md B6).
        _read_log(archive)


@pytest.mark.parametrize(
    "compression",
    [
        None,
        pytest.param(
            "gzip",
            marks=pytest.mark.xfail(
                strict=True,
                reason="TODO.md B6: compression deletes the rotated file while an "
                "unlocked writer in another process may still append to it",
            ),
        ),
    ],
)
def test_record_written_through_a_handle_opened_before_rotation_is_kept(tmp_path, compression):
    """A writer in another process opens the log, this process rotates it, then the
    other writer's append lands. Uncompressed, the append follows the renamed file
    into its archive. Under gzip the file has already been compressed and deleted."""
    path = tmp_path / "app.log"
    s = FileSink(
        str(path), level="INFO", max_bytes=100, retention_count=None, compression=compression
    )
    s.write_batch([record(msg="y" * 200)])

    with open(path, "a", encoding="utf-8") as other_process:
        s.write_batch([record(msg="triggers the rotation")])
        other_process.write("late record\n")

    assert "late record" in "".join(_read_log(f) for f in tmp_path.iterdir())


def test_retention_ignores_files_it_did_not_create(tmp_path):
    """Regression: retention globbed ``app.log.*``, so anything sharing the stem --
    an operator's backup, another sink's file -- was treated as a rotated log."""
    strangers = ("app.log.audit", "app.log.backup", "app.log.json", "app.log.1.bak", "app.log.gz")
    for name in strangers:
        (tmp_path / name).write_text("not a rotated log\n")
        _age(tmp_path / name, 3600)  # older than every rotation: pruned first
    s = FileSink(str(tmp_path / "app.log"), level="INFO", max_bytes=100, retention_count=2)

    for _ in range(5):
        s.write_batch([record(msg="y" * 200)])

    survivors = sorted(p.name for p in tmp_path.iterdir())
    assert [name for name in strangers if not (tmp_path / name).exists()] == [], survivors


def test_retention_counts_only_rotated_logs(tmp_path):
    """An unrelated neighbour must not use up a retention slot either."""
    s = FileSink(str(tmp_path / "app.log"), level="INFO", max_bytes=100, retention_count=3)
    for _ in range(3):
        s.write_batch([record(msg="y" * 200)])
    (tmp_path / "app.log.backup").write_text("user copy\n")
    _age(tmp_path / "app.log.backup", -60)  # newest file in the directory

    s.write_batch([record(msg="y" * 200)])

    rotated = [p.name for p in tmp_path.iterdir() if p.name not in {"app.log", "app.log.backup"}]
    assert len(rotated) == 3, rotated


def test_retention_still_prunes_legacy_rotated_names(tmp_path):
    """Archives written before rotation names carried a token are still pruned."""
    legacy = (
        "app.log.2020-01-01_00-00-00",
        "app.log.2020-01-01_00-00-00.1",
        "app.log.2020-01-01_00-00-01.gz",
        "app.log.2020-01-01_00-00-02.2.gz",
    )
    for name in legacy:
        (tmp_path / name).write_text("old archive\n")
        _age(tmp_path / name, 3600)
    s = FileSink(str(tmp_path / "app.log"), level="INFO", max_bytes=100, retention_count=1)

    for _ in range(2):
        s.write_batch([record(msg="y" * 200)])

    assert [name for name in legacy if (tmp_path / name).exists()] == []


def test_retention_leaves_a_neighbouring_sinks_files_alone(tmp_path):
    """``app.log`` retention must not delete the sink writing ``app.log.audit``."""
    audit = FileSink(str(tmp_path / "app.log.audit"), level="INFO", max_bytes=100)
    audit.write_batch([record(msg="audit-0" + "y" * 200)])
    audit.write_batch([record(msg="audit-1")])  # live file plus one rotated audit log
    for leftover in tmp_path.iterdir():
        _age(leftover, 3600)
    main = FileSink(str(tmp_path / "app.log"), level="INFO", max_bytes=100, retention_count=1)

    for _ in range(3):
        main.write_batch([record(msg="y" * 200)])

    assert len(list(tmp_path.glob("app.log.audit*"))) == 2


def test_retention_keeps_the_process_safe_lock_file(tmp_path):
    path = tmp_path / "app.log"
    s = FileSink(str(path), level="INFO", max_bytes=100, retention_count=1, process_safe=True)
    for _ in range(3):
        s.write_batch([record(msg="y" * 200)])
    assert (tmp_path / "app.log.lock").exists()


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


def test_process_safe_gzip_rotation_keeps_every_record(tmp_path):
    """Several processes rotating and compressing one file must not lose a batch."""
    path = tmp_path / "shared.log"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_write_records_in_process,
            args=(str(path), prefix),
            kwargs={"max_bytes": 40, "compression": "gzip"},
        )
        for prefix in ("one", "two", "three")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    text = "".join(
        _read_log(candidate)
        for candidate in tmp_path.iterdir()
        if candidate.suffix != ".lock" and candidate.stat().st_size
    )
    assert {line.rsplit(": ", 1)[1] for line in text.splitlines()} == {
        f"{prefix}-{number}" for prefix in ("one", "two", "three") for number in range(10)
    }
