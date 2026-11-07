# Contributing

Thanks for contributing to Loglyte.

Keep the public model small: `logger` for events, decorators for function awareness,
and TOML for operational configuration. New capabilities must either fit that model or
serve a concrete integration case that cannot.

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Please include focused tests and documentation updates with behavior changes. Do not
add compatibility aliases for an API that the current public model already replaces.
