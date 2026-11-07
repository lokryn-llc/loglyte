import glob
import os
import tempfile
import time

from loglyte import FileSink, configure, logger
from loglyte.sinks import SINK


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "app.log")
        configure(
            sinks=[
                FileSink(
                    log_path,
                    level="INFO",
                    max_bytes=200,
                    rotation_seconds=1,
                    retention_count=3,
                )
            ]
        )

        @logger
        def log_event(idx: int, log=None) -> None:
            log.info("x" * 100, idx=idx)

        # Emit and flush repeatedly so size/time triggers are exercised.
        for i in range(30):
            log_event(i)
            SINK.flush()
            time.sleep(0.15)

        rotated = sorted(glob.glob(f"{log_path}.*"), key=os.path.getmtime)
        print(f"Active log: {log_path}")
        print(f"Rotated files ({len(rotated)}): {rotated}")
        assert len(rotated) <= 3, f"Expected at most 3 rotated files, found {len(rotated)}"


if __name__ == "__main__":
    main()
