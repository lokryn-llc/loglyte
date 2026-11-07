import asyncio
import contextlib

from loglyte import logger


@logger(bind=("user_id",))
async def process_order(order_id: int, user_id: str, log=None) -> None:
    log.info("Processing order", order_id=order_id, user_id=user_id)


@logger.critical
def failing_task(log=None) -> None:
    raise ValueError("Database connection timeout")


async def main() -> None:
    await process_order(101, "usr_9921")
    with contextlib.suppress(ValueError):
        failing_task()


if __name__ == "__main__":
    asyncio.run(main())
