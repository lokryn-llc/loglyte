# Benchmark results

Recorded 2026-08-04. Reproduce with:

```bash
uv run python benchmarks/bench.py       # cross-library comparison
uv run python benchmarks/ablation.py    # where the difference comes from
```

Machine: Apple Silicon (darwin, arm64), CPython 3.13.7.
50,000 records per measurement, best of 5 repetitions, timed **end-to-end** —
the clock stops only once the bytes are on disk (handlers closed, queues joined,
buffers flushed). `bytes/rec` is shown so you can check the compared work was
actually comparable.

## Cross-library

`vs stdlib` > 1 means faster than stdlib logging.

### static — constant message, no fields

| library          | ns/record | vs stdlib | bytes/rec |
| ---------------- | --------: | --------: | --------: |
| loglyte          |       596 |     7.14x |      29.0 |
| structlog        |     2,551 |     1.67x |      36.0 |
| stdlib logging   |     4,256 |     1.00x |      28.0 |
| loguru           |     5,941 |     0.72x |      28.0 |
| loguru (enqueue) |    29,067 |     0.15x |      28.0 |

### fields — message + 3 structured fields

| library          | ns/record | vs stdlib | bytes/rec |
| ---------------- | --------: | --------: | --------: |
| loglyte          |       934 |     4.87x |      86.8 |
| structlog        |     3,034 |     1.50x |      78.8 |
| stdlib logging   |     4,545 |     1.00x |      66.8 |
| loguru           |     6,300 |     0.72x |      66.8 |
| loguru (enqueue) |    29,810 |     0.15x |      66.8 |

### interpolated — eager f-string in the call

| library          | ns/record | vs stdlib | bytes/rec |
| ---------------- | --------: | --------: | --------: |
| loglyte          |       616 |     7.33x |      35.8 |
| structlog        |     2,595 |     1.74x |      42.8 |
| stdlib logging   |     4,515 |     1.00x |      40.8 |
| loguru           |     6,188 |     0.73x |      40.8 |
| loguru (enqueue) |    28,652 |     0.16x |      40.8 |

### filtered — DEBUG call, sink at INFO (discarded) — **loglyte loses here**

| library          | ns/record | vs stdlib | bytes/rec |
| ---------------- | --------: | --------: | --------: |
| stdlib logging   |        64 |     1.00x |       0.0 |
| loglyte          |       114 |     0.56x |       0.0 |
| loguru           |       121 |     0.53x |       0.0 |
| loguru (enqueue) |       123 |     0.52x |       0.0 |
| structlog        |       132 |     0.49x |       0.0 |

### bound — fields + 2 bound context values

| library          | ns/record | vs stdlib | bytes/rec |
| ---------------- | --------: | --------: | --------: |
| loglyte          |     1,045 |     4.29x |     114.8 |
| structlog        |     3,364 |     1.33x |     102.8 |
| stdlib logging   |     4,484 |     1.00x |      66.8 |
| loguru           |     6,267 |     0.72x |      66.8 |
| loguru (enqueue) |    29,742 |     0.15x |      66.8 |

stdlib has no context-binding equivalent, so its column is the plain `fields`
call — it is doing strictly less work here.

### threaded — 4 threads, fields

| library          | ns/record | vs stdlib | bytes/rec |
| ---------------- | --------: | --------: | --------: |
| loglyte          |       934 |     9.81x |      86.1 |
| structlog        |     8,278 |     1.11x |      78.1 |
| stdlib logging   |     9,162 |     1.00x |      66.1 |
| loguru           |     9,314 |     0.98x |      66.1 |
| loguru (enqueue) |    30,340 |     0.30x |      66.1 |

### json — timestamp + level + message + 3 fields, JSON output

The closest to apples-to-apples: every library emits the same fields.

| library          | ns/record | vs stdlib | bytes/rec |
| ---------------- | --------: | --------: | --------: |
| loglyte          |     2,200 |     2.73x |     153.0 |
| structlog        |     5,038 |     1.19x |     143.8 |
| stdlib logging   |     6,014 |     1.00x |     159.0 |
| loguru           |    14,304 |     0.42x |     661.6 |
| loguru (enqueue) |    41,083 |     0.15x |     661.6 |

Loguru's `serialize=True` emits a much richer record (661 vs ~155 bytes:
file, line, module, thread, process, elapsed), so part of its cost there is
extra information, not pure overhead.

## Ablation — where does the difference come from?

Switching off stdlib's optional work, one knob at a time:

| configuration          | ns/record | vs default |  delta |
| ---------------------- | --------: | ---------: | -----: |
| stdlib (default)       |     4,458 |      1.00x |        |
| + no caller introspect |     3,810 |      1.17x |   -648 |
| + no thread/proc info  |     3,593 |      1.24x |   -217 |
| + batched writes       |     3,476 |      1.28x |   -117 |
| loglyte (direct)       |       917 |      4.86x | -2,559 |
| loglyte (`@info`)      |     1,039 |      4.29x |   +122 |

Tuning stdlib to the bone recovers **1.28x**. The remaining **3.8x** is
architectural — deferred formatting and batched dispatch — and is not
configuration you can turn on in stdlib.

The `@info` decorator costs about **122 ns/call** (~13%). The main benchmark
calls the logger directly; idiomatic decorated usage is the last row.

## Honest caveats

* **loglyte is slower on filtered records** (114 ns vs stdlib's 64 ns). stdlib's
  `isEnabledFor` short-circuit is extremely tight. If your hot path is dominated
  by disabled debug calls, loglyte is not the win.
* **Delivery is asynchronous.** The timings include the drain, so the batching
  advantage is counted honestly — but a `SIGKILL` can still lose buffered
  records. stdlib and non-enqueue loguru cannot.
* **loglyte records carry no file/line.** stdlib and loguru walk the stack on
  every call to capture them; loglyte captures the function name once, at
  decoration. That is a capability difference, and it is roughly 648 ns of the
  gap (the `no caller introspect` row).
* **Absolute numbers are machine-specific.** Ratios travel better.
* Output is *similar*, not byte-identical, across libraries. The `bytes/rec`
  column is there so you can judge that for yourself.
