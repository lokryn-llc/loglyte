# Changelog

All notable changes to Loglyte are documented here.

## Unreleased

### Fixed

- **File rotation no longer loses records under gzip compression.** Rotated files are
  now named `<log>.<timestamp>.<token>`, and both the plain and the `.gz` name are
  claimed with an exclusive create before the file is moved. Previously only the
  uncompressed name was checked, so every rotation within the same second overwrote the
  previous `.gz` archive.
- **Retention only deletes files loglyte itself rotated.** Candidates must match the
  rotation name exactly; `app.log.audit`, `app.log.backup`, another sink's file and the
  `app.log.lock` sidecar are neither deleted nor counted against `retention_count`.
  Archives written by earlier versions (`<log>.<timestamp>[.<counter>]`) are still pruned.
- **On POSIX, processes rotating one file without `process_safe` no longer collide on names or fail when another process rotates first.**
  Claiming the name with `O_EXCL` means two processes cannot pick the same target, and a
  process that loses a rotation race -- at the size check or at the rename -- skips that
  rotation instead of raising `FileNotFoundError`. With `compression = "gzip"`, unlocked
  writers can still lose a record appended after another process compressed the file;
  use `process_safe = true` whenever several processes write one file.

### Changed

- Rotated file names gained a short random token (`app.log.2026-08-10_14-03-01.9f2c7ab4`).
  Tooling that matches rotated logs by exact name needs updating; a `<log>.*` glob is
  unaffected.

- Relicense Loglyte from GPL-3.0-only to MIT. Earlier releases retain their
  original license.
- Fix `logger.bind()` scopes that were re-entered or shared between threads or
  tasks. They raised `RuntimeError`/`ValueError`, could hide the application's own
  exception, and could leave bound fields attached to later records (for example on
  pooled worker threads). A scope exited out of order now repairs the log context
  before reporting the error.
- Stop the `DeprecationWarning` on Python 3.14 when decorating a function with
  `@logger`: use `inspect.iscoroutinefunction` instead of the deprecated
  `asyncio.iscoroutinefunction`.
- Correct documentation that contradicted behaviour: the default sink is stdout at
  `INFO`, and the logging call that reaches `flush_threshold` flushes the batch
  (rendering, writing, rotation and gzip compression) on its own thread. See the new
  README "Flushing" section.

## 1.0.0 - 2026-08-10

First public release.

- Global `logger` API for structured events.
- Optional `@logger` and `@logger.critical` function/class decorators.
- TOML-first configuration, buffered dispatch, JSON/text/logfmt sinks, redaction,
  rotation, and explicit process-safe file writing.
- Standard-library logging bridge and structured exception capture.
