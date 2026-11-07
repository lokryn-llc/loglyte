from loglyte.config import autoload, load_config
from loglyte.core import get_logger
from loglyte.decorators import catch
from loglyte.facade import logger
from loglyte.sinks import (
    SINK,
    BaseSink,
    DispatcherSink,
    FileSink,
    StderrSink,
    StdoutSink,
    StreamSink,
    configure,
)

__all__ = [
    "logger",
    "catch",
    "get_logger",
    "SINK",
    "configure",
    "BaseSink",
    "DispatcherSink",
    "StreamSink",
    "StdoutSink",
    "StderrSink",
    "FileSink",
    "load_config",
]

# Configuration is a single import-time lookup, not work performed by a log call.
autoload()
