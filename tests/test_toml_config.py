import json
import logging
import warnings

import pytest

from loglyte.config import load_config
from loglyte.sinks import SINK, FileSink


def test_load_config_builds_and_applies_sinks(tmp_path):
    config = tmp_path / "loglyte.toml"
    output = tmp_path / "events.jsonl"
    config.write_text(
        f"""
[loglyte]
flush_interval = 0
flush_threshold = 1

[[sinks]]
type = "file"
path = {json.dumps(str(output))}
format = "json"
level = "DEBUG"
redact = ["token"]
process_safe = true
"""
    )
    previous = SINK.sinks
    try:
        assert load_config(config) == config.resolve()
        assert isinstance(SINK.sinks[0], FileSink)
        assert SINK.sinks[0].process_safe
        from loglyte import logger

        logger.info("configured", token="secret")
        SINK.flush()
        entry = json.loads(output.read_text().strip())
        assert entry["token"] == "***"
    finally:
        SINK.sinks = previous


def test_load_config_explains_bad_sink_type(tmp_path):
    config = tmp_path / "loglyte.toml"
    config.write_text("[[sinks]]\ntype = 'database'\n")
    with pytest.raises(ValueError, match="sinks\\[0\\].type"):
        load_config(config)


def test_load_config_requires_a_sink(tmp_path):
    config = tmp_path / "loglyte.toml"
    config.write_text("[loglyte]\nflush_interval = 0\n")
    with pytest.raises(ValueError, match="at least one"):
        load_config(config)


def test_toml_can_capture_python_warnings(tmp_path):
    config = tmp_path / "loglyte.toml"
    output = tmp_path / "warnings.jsonl"
    config.write_text(
        f"""
[loglyte]
flush_interval = 0
flush_threshold = 1
capture_warnings = true

[[sinks]]
type = "file"
path = {json.dumps(str(output))}
format = "json"
"""
    )
    previous_sinks = SINK.sinks
    warning_logger = logging.getLogger("py.warnings")
    previous_handlers = warning_logger.handlers[:]
    try:
        load_config(config)
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.warn("deprecated setting", DeprecationWarning, stacklevel=1)
        SINK.flush()
        assert "deprecated setting" in output.read_text()
    finally:
        logging.captureWarnings(False)
        warning_logger.handlers = previous_handlers
        SINK.sinks = previous_sinks
