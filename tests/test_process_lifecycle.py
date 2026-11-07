"""Interpreter shutdown and fork behaviour.

These run in subprocesses: both paths are process-global and cannot be exercised
meaningfully in-process.
"""

import subprocess
import sys
import textwrap

import pytest

ATEXIT_SCRIPT = """
import sys
from loglyte import StdoutSink, configure, logger

configure(sinks=[StdoutSink(level="INFO")], flush_interval=30)

@logger
def emit(log=None):
    log.info("buffered-at-exit")

emit()
sys.stdout.write("exiting-without-flushing\\n")
"""

FORK_SCRIPT = """
import os, sys, tempfile
from loglyte import FileSink, SINK, configure, logger

path = os.path.join(tempfile.mkdtemp(), "fork.log")
configure(sinks=[FileSink(path, level="INFO")], flush_interval=0.2)

@logger
def emit(who, log=None):
    log.info("record", who=who)

for i in range(20):
    emit("parent-%d" % i)      # buffered, deliberately not flushed

pid = os.fork()
if pid == 0:
    emit("child")
    sys.exit(0)
os.waitpid(pid, 0)
SINK.stop()

lines = open(path).read().splitlines()
print("parent=%d child=%d" % (
    sum("parent-" in ln for ln in lines),
    sum("child" in ln for ln in lines),
))
"""


def run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_atexit_drains_the_buffer():
    """Whatever is still buffered must reach the sinks when the interpreter exits."""
    result = run(ATEXIT_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "buffered-at-exit" in result.stdout
    assert result.stdout.index("exiting-without-flushing") < result.stdout.index(
        "buffered-at-exit"
    ), "the record should be emitted by the atexit hook, not before"


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="requires fork()")
def test_fork_does_not_duplicate_the_parent_buffer():
    """A forked child inherits a copy of the buffer; if it flushed that copy, every
    buffered record would be written twice."""
    result = run(FORK_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "parent=20 child=1" in result.stdout, result.stdout


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="requires fork()")
def test_fork_does_not_warn_about_a_background_thread():
    """The flusher is joined before fork, so applications do not inherit CPython's
    'multi-threaded process, fork() may lead to deadlocks' warning from us.

    The subprocess runs with -W error::DeprecationWarning, so the warning would
    turn into a non-zero exit.
    """
    result = run(FORK_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "DeprecationWarning" not in result.stderr
