"""TOML-first configuration for the global Loglyte dispatcher."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised on Python 3.10 in the CI matrix
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from loglyte.sinks import BaseSink, FileSink, StderrSink, StdoutSink, configure

_CONFIG_ENV = "LOGLYTE_CONFIG"


def _value(section: dict[str, Any], key: str, default: Any = None) -> Any:
    return section.get(key, default)


def _sink_from_toml(spec: dict[str, Any], index: int) -> BaseSink:
    kind = spec.get("type", "stderr")
    if kind not in {"stderr", "stdout", "file"}:
        raise ValueError(f"sinks[{index}].type must be 'stderr', 'stdout', or 'file'")

    format_ = spec.get("format", "text")
    if format_ not in {"text", "json", "logfmt"}:
        raise ValueError(f"sinks[{index}].format must be 'text', 'json', or 'logfmt'")
    common = {
        "level": spec.get("level", "INFO"),
        "render_mode": format_,
        "timestamp_mode": spec.get("timestamp", "none"),
        "redact_fields": spec.get("redact", []),
        "sample": spec.get("sample", 1.0),
        "rate_limit": spec.get("rate_limit"),
    }
    if not isinstance(common["redact_fields"], list):
        raise ValueError(f"sinks[{index}].redact must be a list of field names")

    if kind == "stderr":
        return StderrSink(**common, color=spec.get("color", "auto"))
    if kind == "stdout":
        return StdoutSink(**common, color=spec.get("color", "auto"))

    path = spec.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError(f"sinks[{index}].path is required for a file sink")
    return FileSink(
        path,
        **common,
        max_bytes=spec.get("max_bytes", 10_000_000),
        rotation_seconds=spec.get("rotation_seconds"),
        retention_count=spec.get("retention_count", 5),
        process_safe=spec.get("process_safe", False),
        compression=spec.get("compression"),
    )


def load_config(path: str | Path) -> Path:
    """Load and validate a Loglyte TOML file, then configure the global dispatcher.

    The file is deliberately operational rather than programmable: applications
    need no setup code for normal console or file logging.
    """
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("rb") as file:
            document = tomllib.load(file)
    except FileNotFoundError:
        raise FileNotFoundError(f"Loglyte configuration not found: {config_path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid Loglyte TOML in {config_path}: {exc}") from exc

    section = document.get("loglyte", {})
    if not isinstance(section, dict):
        raise ValueError("[loglyte] must be a TOML table")
    raw_sinks = document.get("sinks", [])
    if not isinstance(raw_sinks, list) or not raw_sinks:
        raise ValueError("at least one [[sinks]] table is required")
    if not all(isinstance(spec, dict) for spec in raw_sinks):
        raise ValueError("each [[sinks]] item must be a TOML table")

    sinks = [_sink_from_toml(spec, index) for index, spec in enumerate(raw_sinks)]
    configure(
        sinks,
        flush_threshold=_value(section, "flush_threshold"),
        flush_interval=_value(section, "flush_interval"),
        overflow_policy=_value(section, "overflow_policy"),
        failure_policy=_value(section, "failure_policy"),
        retry_attempts=_value(section, "retry_attempts"),
        retry_backoff=_value(section, "retry_backoff"),
    )
    if _value(section, "capture_warnings", False):
        import logging

        from loglyte.stdlib import install

        logging.captureWarnings(True)
        install(
            _value(section, "warnings_level", "WARNING"),
            logger=logging.getLogger("py.warnings"),
        )
    return config_path


def autoload() -> Path | None:
    """Load ``$LOGLYTE_CONFIG`` or ``./loglyte.toml`` once during import.

    Absence is normal and leaves the default in place: ``INFO`` and above, as text, on
    stdout. A path explicitly supplied through the environment is treated as required.
    """
    configured = os.environ.get(_CONFIG_ENV)
    if configured:
        return load_config(configured)
    candidate = Path.cwd() / "loglyte.toml"
    return load_config(candidate) if candidate.is_file() else None
