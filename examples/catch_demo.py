import asyncio
import contextlib

from loglyte import catch


@catch(reraise=False, message="Non-critical background job failed")
def background_cleanup(log=None) -> None:
    log.info("Starting cleanup...")
    _ = 1 / 0


@catch(ZeroDivisionError, reraise=True, message="Math domain error in payment pipeline")
async def calculate_ratio(val: int, log=None) -> float:
    return val / 0


async def main() -> None:
    background_cleanup()
    print("Cleanup finished successfully.")
    with contextlib.suppress(ZeroDivisionError):
        await calculate_ratio(10)
    print("Caught re-raised error in main loop.")


if __name__ == "__main__":
    asyncio.run(main())
