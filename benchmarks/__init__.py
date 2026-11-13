"""Benchmark loglyte against the standard library, Loguru and structlog.

Run from the repository root::

    uv run python -m benchmarks                  # everything, a few minutes
    uv run python -m benchmarks --quick          # smoke test of the harness
    uv run python -m benchmarks --only features  # one layer, or named groups

The terminal shows a short summary. Every run also writes a detailed Markdown report
and a JSON file to ``benchmarks/results/``; the three most recent runs are kept.

Layers
------
1. Workloads: realistic profiles (a web request, a batch row, a dependency logging via
   stdlib) run as the same script through every library. Where we stand overall.
2. Features: loglyte's per-record features switched on and off together in a designed
   experiment, so each one's cost is recovered from a few combined runs.
3. Micro: single call shapes across libraries, costs too small to see in a workload, and
   features that skip work (level filters, sampling, rate limits).

Method
------
* Every case writes to a real file and is timed end to end: the clock stops only once
  handlers are closed, queues are drained and the bytes are on disk.
* A warm-up pass sizes each case so one timed pass lasts about the time budget.
* Best and median of the timed passes are reported, plus per-call latency percentiles
  from a separate pass, so occasional expensive calls are visible.
* Every case is checked for the lines it wrote and for values that must be redacted, and
  loglyte for dropped records and sink failures, so no library can look fast by losing
  or leaking records.

Modules
-------
``harness``    how a case is sized, timed and checked
``adapters``   each library's configuration and single call shapes
``workloads``  layer 1 profiles
``features``   layer 2 factors; ``design`` holds the experiment's statistics
``groups``     layer 3, and the order every group runs in
``report``     the terminal summary and the Markdown report
``storage``    run metadata, saved runs and their rotation
``__main__``   the command line
"""
