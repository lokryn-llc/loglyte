"""Measure loglyte's call latency *and* its end-to-end cost.

loglyte defers formatting and I/O, so the time spent inside a ``log.*`` call is not
the whole cost of a log line. Both numbers are reported so the deferral is visible
rather than hidden behind a flattering per-call figure.
"""

import os
import tempfile
import time

from loglyte import SINK, FileSink, configure, info

ITERATIONS = 100_000


@info
def run_pipeline(item_id: int, log=None):
    log.info("Processing item", item_id=item_id)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "bench.log")
        # flush_interval=0 disables the background thread, so the numbers below
        # account for all the work instead of racing a flusher.
        configure(sinks=[FileSink(path, level="INFO")], flush_interval=0)

        run_pipeline(0)  # warm-up
        SINK.flush()

        start = time.perf_counter()
        for i in range(ITERATIONS):
            run_pipeline(i)
        call_time = time.perf_counter() - start

        SINK.flush()
        total_time = time.perf_counter() - start

        with open(path, encoding="utf-8") as f:
            written = sum(1 for _ in f)

    print(f"Iterations:             {ITERATIONS:,}")
    print(f"Lines actually written: {written:,}")
    print()
    print(
        f"Call latency (buffered):  {call_time:.4f}s  {(call_time / ITERATIONS) * 1e9:,.0f} ns/call"
    )
    print(
        f"End-to-end (incl. flush): {total_time:.4f}s  "
        f"{(total_time / ITERATIONS) * 1e9:,.0f} ns/call"
    )
    print(f"Throughput:               {ITERATIONS / total_time:,.0f} records/sec")


if __name__ == "__main__":
    main()
