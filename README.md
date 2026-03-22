# spitter

[![Release](https://img.shields.io/github/v/release/decent-tools-for-thought/spitter?sort=semver)](https://github.com/decent-tools-for-thought/spitter/releases)
![Python](https://img.shields.io/badge/python-3.13%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Text-to-speech CLI for Cartesia with local playback, bytes mode, and reusable websocket sessions.

> [!IMPORTANT]
> This codebase is largely AI-generated. It is useful to me, I hope it might be useful to others, and issues and contributions are welcome.

## Why This Exists

- Speak text locally without building a larger hosted voice stack.
- Support one-shot synthesis and lower-latency websocket workflows.
- Give humans and coding agents a stable terminal interface for TTS.

## Install

```bash
uv tool install .
spitter --help
```

Requirements:

- Python 3.13+
- `ffplay` for local playback
- `CARTESIA_API_KEY` or a saved token file

For local development:

```bash
uv sync --group dev
uv run spitter --help
```

## Quick Start

Log in once:

```bash
spitter login --validate
```

Speak some text:

```bash
spitter say "Build finished."
echo "Tea is ready." | spitter say --stdin
```

Inspect voices and command metadata:

```bash
spitter voices list --language en --query narrator
spitter describe say
```

Use websocket mode:

```bash
spitter say "Tell me now." --transport websocket --container raw
```

## Configuration

- Default API base URL: `https://api.cartesia.ai`
- Default model: `sonic-3`
- Default language: `en`
- Default transport: `bytes`
- Default token file: `~/.config/spitter/cartesia-api-key`

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

## Credits

This client builds on the Cartesia speech API. Credit goes to Cartesia for the underlying voices, synthesis endpoints, and realtime transport model this tool wraps.
