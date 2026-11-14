# loglyte

Fast, structured Python logging with one import, optional function awareness,
and TOML configuration.

```python
from loglyte import logger

logger.info("Server started", port=8080)
logger.error("Payment failed", order_id=order_id)
```

No logger factories, module names, or runtime sink setup are required for ordinary
application logging. Loglyte buffers records in memory and renders/writes them in
batches, so most logging calls only append a record to the buffer (see
[Flushing](#flushing) for where the batch work runs).

Python 3.10+. The only conditional dependency is `tomli` on Python 3.10, where TOML
is not yet in the standard library.

## The model

Use the global `logger` for normal events. Add `@logger` only where function or class
awareness adds value:

```python
from loglyte import logger


@logger(bind=("request_id", "user_id"))
def charge(order_id, request_id, user_id, log=None):
    log.info("Charging order", order_id=order_id)
    reserve_inventory(order_id, log=log)


def reserve_inventory(order_id, *, log):
    # This retains the parent operation's identity and bound context.
    log.info("Inventory reserved", order_id=order_id)
```

The decorator captures the function identity once at import time. It binds selected
arguments for the call and injects `log` only if the function declares `log=None`.
Global `logger` calls inside the decorated scope inherit the bound fields too.

Decorate a class when that policy belongs on all of its public methods:

```python
@logger(bind=("tenant_id",))
class BillingService:
    def charge(self, tenant_id, order_id, log=None):
        log.info("Charging", order_id=order_id)
```

Private methods and `__init__` are not wrapped. Instance methods, static methods, and
class methods are.

Use `@logger.critical` for an error boundary. It logs the traceback and re-raises the
exception—it never changes application control flow by swallowing an error.

```python
@logger.critical(bind=("job_id",))
def run_job(job_id):
    raise RuntimeError("worker unavailable")
```

For context that is not a function parameter, use a short scope:

```python
with logger.bind(request_id=request_id):
    logger.info("Request accepted")
```

Context is backed by `contextvars`, so it isolates concurrent asyncio tasks and does
not leak across threads.

## Configuration

Put `loglyte.toml` at the application working directory, or set `LOGLYTE_CONFIG` to
its path. Loglyte discovers it once when imported. Configuration mistakes fail early
with a clear error rather than silently changing production logging.

```toml
[loglyte]
flush_threshold = 500
flush_interval = 0.25
overflow_policy = "drop_oldest"
failure_policy = "retry"
retry_attempts = 2

[[sinks]]
type = "stderr"
level = "INFO"
format = "text"
color = "auto" # auto, always, or never
sample = 1.0
rate_limit = 500 # records per second for this sink
redact = ["password", "token", "authorization"]

[[sinks]]
type = "file"
path = "logs/app.jsonl"
level = "DEBUG"
format = "json"
max_bytes = 10_000_000
rotation_seconds = 86_400
retention_count = 7
process_safe = true
compression = "gzip"
```

Supported sink types are `stderr`, `stdout`, and `file`; formats are `text`, `json`,
and `logfmt`. Configure all deployment behavior here: levels, redaction, rotation,
queue flushes, retry behavior, sampling, and overflow policy. `color = "auto"` only
emits ANSI colour to a TTY; use `"always"` for a local terminal or `"never"` for plain
text. Sampling and rate limits are applied per sink when a batch is flushed, not as each
record is logged.

`process_safe = true` serializes complete file batches and rotation with a sidecar
lock. Enable it only when multiple Python processes write the same file: it has
unavoidable synchronization overhead. The default is the lowest-latency,
single-process path; threads are already serialized by Loglyte's dispatcher.

Set `capture_warnings = true` in `[loglyte]` to route Python warnings through the same
configured sinks. Gzip compression runs when a file rotates, as part of the flush that
triggered the rotation.

### Flushing

A background thread flushes buffered records every `flush_interval` seconds
(`flush_interval = 0` disables it). The
logging call that brings the buffer to `flush_threshold` records flushes it
immediately, **on the calling thread**: that call renders and writes the whole batch,
including any file rotation, gzip compression, and sink retries. Most calls only append
to the buffer, but that occasional call can take much longer. `logger.shutdown()` and
interpreter exit also flush.

Rotated files are named `<log>.<timestamp>.<token>`, for example
`app.log.2026-08-10_14-03-01.9f2c7ab4`, with `.gz` appended when compression is on. The
token keeps rotations unique when several land in the same second. `retention_count`
only ever counts and deletes files matching that exact pattern, so a neighbouring
`app.log.audit` or `app.log.backup` is never touched.

Concurrent processes must use `process_safe = true`. Unlocked rotation relies on
POSIX open-file rename semantics; Windows can reject a rotation while another
writer holds the file open. Even on POSIX, unlocked gzip rotation can lose records.

## Integrations and escape hatches

`get_logger(name)`, `catch()`, `configure()`, and custom sinks remain available for
libraries and advanced integrations, but they are not the recommended application
workflow. `logger.exception("message")` records a caught exception at `ERROR`;
`logger.shutdown()` flushes on controlled worker/service shutdown.

`catch()` is deliberately retained for the distinct case where a background boundary
must selectively report and suppress an expected exception type:

```python
from loglyte import catch


@catch(ValueError, reraise=False, message="optional sync failed")
def sync_optional_resource(): ...
```

To collect standard-library logs from dependencies, install the bridge once at startup:

```python
from loglyte.stdlib import install

install(level="INFO")
```

## Delivery guarantees

Records are accepted into a bounded in-memory queue, then written when its threshold
is reached, its interval elapses, `logger.shutdown()` runs, or Python exits normally.
Overflows are counted and reported as warning records; `SINK.health()` exposes queue
and sink-health counters. A hard kill can lose records still buffered in memory.

## Performance

On the included Apple Silicon benchmark, Loglyte was 4–10x faster than standard
library logging for emitted structured events and about 2.7x faster for JSON output.
It is slower for discarded `DEBUG` calls, where the standard library has an extremely
short level-check path. See [benchmark results](benchmarks/RESULTS.md) and rerun the
suite on your own workload before treating numbers as representative.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

## License

MIT License. See [LICENSE](LICENSE).

## Synthetic test data

All email addresses and credential-like values in the tests and benchmarks are
fictional fixtures used to verify redaction. `ada@example.test` is a dummy email
address; values such as `secret`, `abc`, and `hunter2` are dummy strings and do not
authenticate to any service. No live credentials are required for these fixtures.
