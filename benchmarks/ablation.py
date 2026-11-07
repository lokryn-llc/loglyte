"""Where does loglyte's speed actually come from?

A raw "Nx faster" number is not very informative: some of the gap is design, and
some of it is just stdlib doing optional work that can be switched off. This
script turns stdlib's optional work off one knob at a time, so the remaining gap
is attributable rather than mysterious.

Ablations, cumulative top to bottom:

  stdlib (default)        what you get out of the box
  + no caller introspect  logging._srcfile = None -- stop walking the stack to
                          find the caller's file/line on every record
  + no thread/proc info   logging.logThreads/logProcesses/logMultiprocessing = False
  + batched writes        MemoryHandler in front of the FileHandler, so the write
                          syscall is amortised over 1,000 records
  loglyte                 all of the above by construction, plus deferred formatting

Also reports the cost of loglyte's ``@info`` decorator, since the main benchmark
calls the logger directly and idiomatic loglyte goes through the wrapper.

Usage: uv run python benchmarks/ablation.py [--iterations N] [--reps R]
"""

from __future__ import annotations

import argparse
import gc
import logging
import logging.handlers
import os
import sys
import tempfile
import time
from collections.abc import Callable

import loglyte

ITERATIONS = 50_000
REPS = 5


def timed(setup: Callable[[str], Callable[[], None]], path: str, n: int, reps: int) -> float:
    """Best-of-`reps` end-to-end seconds for writing `n` records to `path`."""
    best = float("inf")
    for _ in range(reps + 1):
        if os.path.exists(path):
            os.remove(path)
        run = setup(path)
        gc.collect()
        start = time.perf_counter()
        run()
        elapsed = time.perf_counter() - start
        best = min(best, elapsed)
    return best


def make_stdlib(
    *, introspect: bool, thread_info: bool, batched: bool
) -> Callable[[str], Callable[[], None]]:
    def setup(path: str) -> Callable[[], None]:
        logging._srcfile = logging.__file__ if introspect else None  # noqa: SLF001
        logging.logThreads = thread_info
        logging.logProcesses = thread_info
        logging.logMultiprocessing = thread_info

        logger = logging.getLogger(f"abl.{time.time_ns()}")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)

        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(levelname)s %(funcName)s: %(message)s"))
        handler: logging.Handler = file_handler
        if batched:
            handler = logging.handlers.MemoryHandler(
                capacity=1000, flushLevel=logging.CRITICAL, target=file_handler
            )
        logger.addHandler(handler)

        def run() -> None:
            for i in range(ITER):
                logger.info("Processing item item_id=%s user_id=%s region=%s", i, "usr_9", "eu")
            handler.close()
            if batched:
                file_handler.close()

        return run

    return setup


def make_loglyte(*, decorated: bool) -> Callable[[str], Callable[[], None]]:
    def setup(path: str) -> Callable[[], None]:
        loglyte.configure(sinks=[loglyte.FileSink(path, level="INFO")], flush_interval=0)

        if decorated:

            @loglyte.info
            def emit(i: int, log=None) -> None:
                log.info("Processing item", item_id=i, user_id="usr_9", region="eu")

            def run() -> None:
                for i in range(ITER):
                    emit(i)
                loglyte.SINK.stop()
        else:
            log = loglyte.ContextLogger("emit")

            def run() -> None:
                for i in range(ITER):
                    log.info("Processing item", item_id=i, user_id="usr_9", region="eu")
                loglyte.SINK.stop()

        return run

    return setup


ITER = ITERATIONS


def main() -> None:
    global ITER
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=ITERATIONS)
    parser.add_argument("--reps", type=int, default=REPS)
    args = parser.parse_args()
    ITER = args.iterations

    configs: list[tuple[str, Callable[[str], Callable[[], None]]]] = [
        ("stdlib (default)", make_stdlib(introspect=True, thread_info=True, batched=False)),
        (
            "+ no caller introspect",
            make_stdlib(introspect=False, thread_info=True, batched=False),
        ),
        (
            "+ no thread/proc info",
            make_stdlib(introspect=False, thread_info=False, batched=False),
        ),
        ("+ batched writes", make_stdlib(introspect=False, thread_info=False, batched=True)),
        ("loglyte (direct)", make_loglyte(decorated=False)),
        ("loglyte (@info)", make_loglyte(decorated=True)),
    ]

    print(f"python      {sys.version.split()[0]}")
    print(f"iterations  {ITER:,} per measurement, best of {args.reps}")
    print("scenario    message + 3 structured fields -> text file\n")

    header = f"{'configuration':<24} | {'ns/record':>10} | {'vs default':>10} | {'delta':>10}"
    print(header)
    print("-" * len(header))

    baseline = None
    previous = None
    with tempfile.TemporaryDirectory() as tmpdir:
        for index, (name, setup) in enumerate(configs):
            path = os.path.join(tmpdir, f"config-{index}.log")
            ns = timed(setup, path, ITER, args.reps) / ITER * 1e9
            baseline = baseline or ns
            delta = "" if previous is None else f"{ns - previous:>+9,.0f}"
            print(f"{name:<24} | {ns:>10,.0f} | {baseline / ns:>9.2f}x | {delta:>10}")
            previous = ns
            # The decorator row is a variant, not a further ablation step.
            if name == "loglyte (direct)":
                previous = ns

    logging._srcfile = logging.__file__  # noqa: SLF001 - restore module state
    logging.logThreads = logging.logProcesses = logging.logMultiprocessing = True

    print("\n'delta' is the change from the row above: how much that single knob cost.")
    print("The gap that survives all the stdlib knobs is loglyte's deferred formatting")
    print("and batched dispatch; everything above it is work stdlib does by default.")


if __name__ == "__main__":
    main()
