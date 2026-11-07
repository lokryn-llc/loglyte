# Changelog

All notable changes to Loglyte are documented here.

## Unreleased

- Relicense Loglyte from GPL-3.0-only to MIT. Earlier releases retain their
  original license.

## 1.0.0 - 2026-08-10

First public release.

- Global `logger` API for structured events.
- Optional `@logger` and `@logger.critical` function/class decorators.
- TOML-first configuration, buffered dispatch, JSON/text/logfmt sinks, redaction,
  rotation, and explicit process-safe file writing.
- Standard-library logging bridge and structured exception capture.
