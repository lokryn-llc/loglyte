import asyncio

from loglyte import StdoutSink, configure, logger

configure(sinks=[StdoutSink(level="INFO", json_mode=True)])


@logger
def process_order_item(item_id: str, log=None):
    log.info("Processing line item", item_id=item_id)


@logger
async def handle_http_request(req_id: str, user_id: str, log=None):
    with logger.bind(request_id=req_id, user_id=user_id):
        log.info("HTTP Request received")
        process_order_item("item_992")
        log.info("HTTP Request completed")


if __name__ == "__main__":
    asyncio.run(handle_http_request("req_abc123", "usr_774"))
