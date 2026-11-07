"""Capture a dependency's stdlib logging, plus exc_info on ordinary log calls."""

import logging

import loglyte
from loglyte import StderrSink, logger
from loglyte.stdlib import install


def main() -> None:
    loglyte.configure(sinks=[StderrSink(level="INFO", json_mode=True)], flush_interval=0)
    install(level="INFO")

    # Stands in for urllib3 / sqlalchemy / uvicorn: a library that knows nothing
    # about loglyte and logs through the standard library.
    dependency = logging.getLogger("urllib3.connectionpool")
    dependency.warning("Connection pool is full, discarding connection: %s", "api.example.com")
    dependency.info("Starting new HTTPS connection (%d): %s", 1, "api.example.com")

    try:
        {"a": 1}["missing"]
    except KeyError:
        logger.error("lookup failed", exc_info=True, order_id=7)

    loglyte.SINK.flush()


if __name__ == "__main__":
    main()
