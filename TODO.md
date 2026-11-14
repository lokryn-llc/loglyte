# TODO

Bugs and improvements found while building the benchmark suite
(`uv run python -m benchmarks`) that were not fixed or covered there.

- Every bug below was reproduced; each has a snippet that shows it.
- Performance numbers came from a noisy shared container and are directional only.
  Confirm them on the reference machine before acting on them.
- "Verify with" names the benchmark group or test that shows the problem, so a fix can
  be checked with, for example, `uv run python -m benchmarks --only rotation-gzip`.

Contents: [Bugs](#bugs) · [Performance](#performance) · [Code quality](#code-quality) ·
[Documentation and defaults](#documentation-and-defaults) ·
[Benchmark backlog](#benchmark-backlog) ·
[Benchmark known limitations](#benchmark-known-limitations)

---

## Bugs

### B1. Rotation with gzip compression silently loses records (data loss)
- [x] Fixed: rotated names carry a random token and both the plain and `.gz` name are
  claimed with `O_EXCL` before the move. Regression tests:
  `test_gzip_rotations_within_one_second_keep_every_archive`,
  `test_rotation_target_is_claimed_not_merely_checked`.
- **Where:** `FileSink._rotated_path()` / `_rotate_and_cleanup()` in `loglyte/sinks.py`.
- **Cause:** `_rotated_path()` only checks that the *uncompressed* name is free. After
  compression that name is free again, so every rotation within the same second reuses
  it and `gzip.open(..., "wb")` overwrites the previous `.gz`.
- **Impact:** any service that rotates more than once per second with
  `compression = "gzip"` loses most of its logs without any error.
- **Reproduce:**
  ```python
  import gzip, os, tempfile, loglyte
  from loglyte import FileSink, logger

  with tempfile.TemporaryDirectory() as d:
      loglyte.configure(
          [
              FileSink(
                  os.path.join(d, "app.log"),
                  max_bytes=64 * 1024,
                  retention_count=None,
                  compression="gzip",
              )
          ],
          flush_interval=0,
      )
      for i in range(20_000):
          logger.info("Processing item", item_id=i)
      logger.shutdown()
      on_disk = sum(
          (
              gzip.open(os.path.join(d, f)).read()
              if f.endswith(".gz")
              else open(os.path.join(d, f), "rb").read()
          ).count(b"\n")
          for f in os.listdir(d)
      )
      print(on_disk)  # far fewer than 20,000; varies with timing
  ```
- **Suggested fix:** treat a candidate as taken if either it or `candidate + ".gz"`
  exists, or name rotated files with a monotonic counter instead of the time.
- **Verify with:** `rotation-gzip` (its line-count check fails until fixed); add a unit
  test for several rotations within one second.

### B2. Retention cleanup deletes unrelated files that share the log's name (data loss)
- [x] Fixed: retention matches the rotation name exactly (including legacy names) instead
  of globbing `app.log.*`. Regression tests: `test_retention_ignores_files_it_did_not_create`,
  `test_retention_counts_only_rotated_logs`, `test_retention_still_prunes_legacy_rotated_names`,
  `test_retention_leaves_a_neighbouring_sinks_files_alone`.
- **Where:** `FileSink._rotate_and_cleanup()`; it globs `f"{file_path}.*"`.
- **Cause:** every file whose name starts with `app.log.` counts as a rotated log:
  `app.log.audit`, `app.log.json`, another sink's file, a user's backup.
- **Impact:** with `retention_count` set, loglyte can delete files it did not create.
- **Reproduce:**
  ```python
  import os, tempfile, loglyte
  from loglyte import FileSink, logger

  with tempfile.TemporaryDirectory() as d:
      open(os.path.join(d, "app.log.audit"), "w").write("important\n")
      loglyte.configure(
          [FileSink(os.path.join(d, "app.log"), max_bytes=2_000, retention_count=1)],
          flush_interval=0,
          flush_threshold=10,
      )
      for i in range(2_000):
          logger.info("x", i=i)
      logger.shutdown()
      print(os.path.exists(os.path.join(d, "app.log.audit")))  # False
  ```
- **Suggested fix:** only consider names that match the rotation format exactly
  (`app.log.YYYY-MM-DD_HH-MM-SS[.N][.gz]`), e.g. with a regular expression.
- **Verify with:** a unit test like the snippet above.

### B6. Unlocked multi-process rotation: gzip can still lose records; extra archives
- [ ] Decide (options in the rotation review)
- **Where:** `FileSink._should_rotate()` / `_rotate_and_cleanup()`, `process_safe=False` only.
- **Fixed already:** concurrent rotations can no longer pick the same name (`O_EXCL`
  claim), and losing a rotation race is no longer a `FileNotFoundError` at either the
  size check or the rename.
- **Windows:** unlocked concurrent rotation can fail with `PermissionError` because
  Windows prevents renaming files held open by another writer. The unlocked
  concurrency tests require POSIX semantics; use `process_safe=True` on Windows.
- **Remaining, gzip (data loss):** a writer in another process that opened `app.log`
  before a rotation can append after the file was compressed and deleted; that append
  is lost. 4 unlocked writers released together lost 1-2 of 160 records in 13 of 40
  runs. Uncompressed rotation is unaffected: the append follows the renamed file.
- **Remaining, both modes (no loss):** each writer decides to rotate on its own view of
  the size, so a stale decision can rotate a file another process only just created,
  producing small or empty archives.
- **Pinned by:** `test_record_written_through_a_handle_opened_before_rotation_is_kept`
  (`gzip` case is `xfail(strict=True)`; remove the mark when fixed).

### B3. A `logger.bind()` scope cannot be re-entered or shared; context can leak
- [x] Fix. The context now holds a chain of immutable scope frames instead of a token
  stored on the `ScopedLogger`, so a scope can be nested and shared by threads and tasks.
  An out-of-order exit repairs the context, then raises `RuntimeError`. If the body is
  already raising, the error is attached as a note (a `RuntimeWarning` on 3.10) and the
  application's exception is not replaced. Regression tests: `tests/test_bind_scope.py`.
- **Where:** `ScopedLogger.__enter__` / `__exit__` in `loglyte/facade.py`.
- **Cause:** the scope's context-variable token is stored on the `ScopedLogger`
  instance (`self._scope`). A second `__enter__` overwrites the first.
- **Impact:**
  - Nested re-use raises `RuntimeError: logger binding scope was not entered`.
  - A scope shared by threads or tasks (for example one created at module level)
    raises `RuntimeError`, fails to reset the context (`ValueError: Token ... was
    created in a different Context`), and can leave one request's fields attached
    to later records in that thread.
- **Reproduce:**
  ```python
  from loglyte import logger

  scope = logger.bind(request_id="r1")
  with scope:
      with scope:  # RuntimeError on the outer exit
          pass
  ```
  Sharing one scope between two threads that are inside it at the same time raises too.
- **Suggested fix:** keep tokens per entry instead of on the instance. For example,
  `__enter__` can return a fresh context manager, or tokens can be kept in a stack held
  in a `ContextVar`.
- **Verify with:** unit tests for nested re-use and for two threads sharing one scope.

### B4. Deprecated `asyncio.iscoroutinefunction` will break the tests on Python 3.14
- [x] Fix. `decorators.py` now uses `inspect.iscoroutinefunction`, which gives the same
  result for every callable kind checked on 3.10-3.14. The suite passes on 3.10-3.14.
- [ ] Add 3.14 to the existing CI matrix (currently 3.10–3.13).
- **Where:** `loglyte/decorators.py` (`_build_wrapper`).
- **Cause:** `asyncio.iscoroutinefunction` is deprecated from Python 3.14 (removal
  planned for 3.16). It runs when a function is decorated, so users see a
  `DeprecationWarning` at import. The test suite sets `filterwarnings = ["error"]`,
  so every decorator test will fail on 3.14.
- **Suggested fix:** use `inspect.iscoroutinefunction`. Add 3.14 to the CI matrix.

### B5. Documentation contradicts behaviour
- [x] Fix (documentation only; behaviour unchanged). The `autoload()` docstring now
  names the stdout default. The README no longer claims flushing is off the hot path;
  a new "Flushing" section says the call that reaches `flush_threshold` flushes on its
  own thread, including rotation and gzip. The same claim about sampling was corrected
  in the README and the `_allows_delivery` docstring. If P1 moves the flush to the
  background worker, update that section.
- `config.autoload()` docstring: "leaves the small stderr/stdout-free default untouched",
  but the default sink is `StdoutSink(level="INFO")` (`DispatcherSink.__init__`).
- README: "renders/writes them in batches, off the producer's hot path" and "Gzip
  compression runs after a file rotates, on the dispatcher rather than the application
  thread". In fact the call that fills a batch (`flush_threshold`) renders, writes,
  rotates and compresses the whole batch on the **calling** thread. Confirmed: gzip ran
  on `MainThread` with the background thread running.
- Either change the behaviour (see P1) or correct the wording.

---

## Performance

Numbers from the container; confirm on the reference machine.

- [ ] **P1. Batch flush runs on the calling thread.** The call that reaches
  `flush_threshold` formats and writes the whole batch, plus any rotation and gzip.
  p99.9/max latency is 0.5-3.7 ms, against ~2 µs for a typical call. Consider handing the
  batch to the background worker (and update the README "Flushing" section, see B5).
  *Verify with:* the per-call latency table, `flush`, `rotation-gzip` latency columns.
- [ ] **P2. `@logger(bind=...)` calls `inspect.Signature.bind()` + `apply_defaults()` on
  every call** (about +12 µs per call). Pre-compute argument positions when decorating.
  *Verify with:* `callpath`.
- [ ] **P3. `@logger` costs even with nothing to bind.** The wrapper always enters
  `bind_context(**{})`, a generator-based context manager (~1.4 µs in isolation), and
  pays it even when the record is discarded. Skip the scope when there are no fields.
  *Verify with:* `callpath`, `callpath-filtered`, `callpath-async`, feature `decorator`.
- [ ] **P4. Per-call `with logger.bind(...)` is expensive** (`ScopedLogger` +
  `@contextmanager` + dict copies). A class-based context manager would avoid the
  generator; see also B3. *Verify with:* `callpath`, feature `bind scope`, `web-request`.
- [ ] **P5. Discarded DEBUG calls are ~2x slower than stdlib.** Each call goes through
  the level string, a dict lookup, then `is_enabled`. Consider a cached numeric threshold
  checked first. *Verify with:* `filtered`, `callpath-filtered`.
- [ ] **P6. The bound context is copied on every record.** `get_context()` copies the
  dict, then `_push` merges it into a new dict again. One merge would do.
  *Verify with:* `bound`, feature `bind scope`.
- [ ] **P7. `FileSink` does file-system work on every batch.** It checks the file exists
  (one `stat`) even with rotation off, and opens and closes the file for every batch.
  Keeping the handle open (reopening on rotation) would save system calls.
  *Verify with:* `fields`, `rotation`.
- [ ] **P8. logfmt rendering is slow** (`json.dumps` per value); loglyte trails structlog.
  *Verify with:* `logfmt`.
- [ ] **P9. The stdlib bridge is slower than plain stdlib** (`LoglyteHandler.emit` copies
  `record.__dict__` per record). *Verify with:* `stdlib-bridge`.
- [ ] **P10. `@logger.critical` and `catch()` error paths cost more than a plain
  `try/except` + `logger.exception`.** *Verify with:* `error-boundary`.
- [ ] **P11. Retry backoff sleeps on the calling thread.** With `failure_policy =
  "retry"`, `time.sleep()` runs inside `flush()`, which the threshold flush runs on the
  producer (see P1). Not yet benchmarked (see the backlog).

## Code quality

- [ ] `configure()` in `sinks.py` reaches into `SINK`'s private attributes (`_worker`,
  `_flush_lock`, `_capacity`, `_ensure_worker`). A `DispatcherSink.reconfigure(...)`
  method would keep that logic with the class.
- [ ] Caller location is captured in two places with different frame offsets
  (`LocationLogger._push` inline, and `_location_fields` for `BoundLocationLogger`), and
  the `stacklevel` validation is duplicated. Share one helper.
- [ ] `BaseSink._render` has two near-identical loops (fast path and filter/redaction
  path). Consider one loop, or document why both exist.
- [ ] `_FILE_LOCKS` in `sinks.py` keeps one lock per file path for the life of the
  process. Fine for normal use; unbounded if paths are generated.

## Documentation and defaults

- [ ] `FileSink` rotates at 10 MB by default (`max_bytes=10_000_000`, `retention_count=5`).
  It silently rotated mid-benchmark, and with B2 the default retention can delete
  unrelated files. Document it prominently, or reconsider the defaults.
- [ ] The default sink is stdout, while `StderrSink`'s docstring calls stderr
  conventional for diagnostics. Decide which is intended and align code and docs (B5).
- [ ] README performance claims ("4-10x faster", "about 2.7x for JSON", "slower for
  discarded DEBUG") come from the pre-1.0 snapshot. Update them from a new baseline.
- [ ] Replace the historical snapshot in `benchmarks/RESULTS.md` with a baseline recorded
  on the reference machine.
- [ ] Document how to get a stable benchmark run: on mains power, other applications
  closed, no other heavy processes, same machine between comparisons.

---

## Benchmark backlog

Measurement quality
- [ ] `--calibrate`: an A/A noise test on the current machine. Mark "vs last run"
  changes smaller than the detectable threshold as noise. Two runs of identical code in
  the container differed by -29% to +200%, so this matters.
- [ ] A pinned baseline (`--save-baseline`, e.g. one per release) that rotation never
  deletes; compare with both the baseline and the last run.
- [ ] Decide how known library bugs affect the exit code. (B1 used to make every
  default run exit 1; `--only rotation-gzip` now passes.) Option: a short list of known issues that are still reported but
  only new problems fail the run.
- [ ] Layer 2 reported "lack of fit" between 1.3 and 2.6 across container runs. On the
  reference machine, check whether features interact or the machine drifted. If real,
  add a follow-up design that separates the interacting pairs.
- [ ] Tiers (core vs all) if the full run grows past ~15 minutes.

Coverage still missing
- [ ] Startup and import time per library, including loglyte's TOML autoload; a CLI
  workload profile (start, log a few lines, exit).
- [ ] Multiple processes writing one file (`process_safe` vs Loguru enqueue vs stdlib),
  checking that lines do not interleave. Needs a process driver in the harness.
- [ ] Overflow and back-pressure: bursts beyond `capacity`, `drop_oldest` vs
  `drop_newest`. Expected drops must not fail the checks.
- [ ] `failure_policy` continue / disable / retry with a failing sink (see P11).
- [ ] Thread scaling (1, 2, 4, 8 threads).
- [ ] Memory held by a full buffer (`tracemalloc`).
- [ ] `capture_warnings`, `load_config` (TOML) and time-based rotation
  (`rotation_seconds`) as functional checks.
- [ ] stdlib `QueueHandler` + `QueueListener` as a stdlib adapter: the fair counterpart
  to loglyte's buffering and Loguru's enqueue.
- [ ] Loguru with `diagnose=False` (its documented production setting) next to the
  default in `exceptions` and `web-request`; the default dominates those numbers.
- [ ] An async web workload (requests as asyncio tasks), including B3's shared-scope
  pattern once fixed.
- [ ] Regression cases for B3 once fixed (a shared bind scope across threads). B2's
  cases now live in `tests/test_sinks.py`.

Feature matrix (layer 2)
- [ ] Factors left out because they are not simple on/off: ISO timestamps (only affect
  text output), logfmt (a third format), `stack_info`, exceptions at a rate.
- [ ] Split the `console sink` factor into the stream sink and colour.

Checks and tooling
- [ ] Validate content, not only line counts: every JSON line parses, logfmt is
  well-formed.
- [ ] A pytest smoke test of the harness. Needs `--results-dir`, so it never rotates out
  real runs.
- [ ] A `schema_version` in saved JSON, so "vs last run" can refuse or migrate files from
  an older harness.

## Benchmark known limitations

Listed so nobody over-reads the numbers; change them only if they start to matter.

- Workloads call each library through a small `Output` wrapper and a lambda, which adds
  the same constant cost for every library. Idiomatic direct calls would be slightly
  cheaper for all of them.
- Library configurations are reasonable idioms, not the only ones: stdlib request context
  uses a contextvar plus a filter; Loguru's logfmt format hard-codes the field names and
  leaves values unquoted; structlog's bridge uses `ProcessorFormatter` without caching.
- The warm-up that sizes each case runs at the floor size and includes one-off costs, so
  it slightly overestimates the per-record cost. Cases end up a little smaller than the
  budget allows, which is conservative.
- The run-time estimate ignores the floors. Cases slower than the budget at their minimum
  size (Loguru's exceptions and enqueue) take longer than estimated.
- Quick mode grew to ~1.5 minutes with 127 cases. A smaller quick set could bring it back
  under 30 s.
- Layer 2's "all features off" baseline is the model's prediction, not a measured run. It
  is reliable only when lack of fit is low.
- Threaded cases and feature-matrix runs have no per-call latency pass. Threaded and async
  calls are never combined.
- The garbage collector stays on and nothing pins CPUs; a laptop's frequency scaling and
  background work show up as noise (see `spread` and `--calibrate` above).
