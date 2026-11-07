"""StdoutSink / StderrSink stream routing."""

import io

from loglyte.sinks import StderrSink, StdoutSink


def record(level="INFO", msg="hello"):
    return (1700000000.0, level, "f", msg, {})


def test_stderr_sink_writes_to_stderr_not_stdout(capsys):
    """Diagnostics on stderr keep log output from corrupting a program's stdout."""
    StderrSink(level="INFO").write_batch([record(msg="diagnostic")])
    captured = capsys.readouterr()
    assert "diagnostic" in captured.err
    assert captured.out == ""


def test_stdout_sink_writes_to_stdout_not_stderr(capsys):
    StdoutSink(level="INFO").write_batch([record(msg="program output")])
    captured = capsys.readouterr()
    assert "program output" in captured.out
    assert captured.err == ""


def test_streams_are_resolved_per_batch_not_captured_at_construction(monkeypatch):
    """Constructing a sink early must not pin a stream that is replaced later."""
    sink = StderrSink(level="INFO")
    replacement = io.StringIO()
    monkeypatch.setattr("sys.stderr", replacement)

    sink.write_batch([record(msg="late binding")])
    assert "late binding" in replacement.getvalue()


def test_stream_sinks_honour_their_level(capsys):
    StderrSink(level="ERROR").write_batch([record(level="INFO", msg="filtered")])
    assert capsys.readouterr().err == ""


def test_both_streams_can_be_used_at_once(capsys):
    """The conventional split: routine output one way, problems the other."""
    StdoutSink(level="INFO").write_batch([record(msg="routine")])
    StderrSink(level="ERROR").write_batch([record(level="ERROR", msg="problem")])

    captured = capsys.readouterr()
    assert "routine" in captured.out and "problem" not in captured.out
    assert "problem" in captured.err and "routine" not in captured.err
