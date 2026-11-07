"""@logger injects a logger; @logger.critical logs and re-raises exceptions."""

from loglyte import SINK, logger


@logger(bind=("batch_id",))
def process_data_pipeline(batch_id: int, log=None):
    log.info("Starting batch process", batch_id=batch_id)
    # Business logic here...
    log.debug("Internal memory checks passed.")


@logger.critical
def risky_calculation(x: int):
    return x / 0  # logged as CRITICAL, then re-raised


if __name__ == "__main__":
    print("--- Executing @logger demo ---")
    process_data_pipeline(42)
    SINK.flush()

    print("\n--- Executing @logger.critical demo ---")
    try:
        risky_calculation(10)
    except ZeroDivisionError:
        print("Caught re-raised exception successfully.")
    SINK.flush()
