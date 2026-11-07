import contextlib
import os
import tempfile

from loglyte import FileSink, StdoutSink, configure, logger


@logger
def process_data(data_id: str, log=None) -> None:
    log.info("Started processing", id=data_id)
    log.error("Failed to process record!", id=data_id)


@logger.critical
def failing_task(log=None) -> None:
    raise RuntimeError("disk full")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        error_log = os.path.join(tmpdir, "errors.log")
        configure(
            sinks=[
                StdoutSink(level="INFO", json_mode=True),
                FileSink(error_log, level="ERROR"),
            ]
        )

        process_data("abc-123")
        with contextlib.suppress(RuntimeError):
            failing_task()

        # Force a flush so the file sink writes before we read it.
        from loglyte.sinks import SINK

        SINK.flush()

        with open(error_log, encoding="utf-8") as f:
            content = f.read()
        print("--- errors.log ---")
        print(content)


if __name__ == "__main__":
    main()
