<div align="center">

# spitter

[![Release](https://img.shields.io/github/v/release/decent-tools-for-thought/spitter?sort=semver&color=c0c0c0)](https://github.com/decent-tools-for-thought/spitter/releases)
![Python](https://img.shields.io/badge/python-3.13%2B-d4d4d8)
![License](https://img.shields.io/badge/license-MIT-a1a1aa)

Cartesia text-to-speech CLI for saved login, local playback, bytes mode, websocket streaming, and reusable local speech sessions.

</div>

> [!IMPORTANT]
> This codebase is entirely AI-generated. It is useful to me, I hope it might be useful to others, and issues and contributions are welcome.

## Map
- [Install](#install)
- [Functionality](#functionality)
- [Runtime Defaults](#runtime-defaults)
- [Quick Start](#quick-start)
- [Development](#development)
- [Credits](#credits)

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

## Functionality

### Login
- `spitter login`: save a Cartesia API token to the configured token file.
- `spitter login --token ...`: pass the token directly on the command line.
- `spitter login --stdin`: read the token from stdin.
- `spitter login --validate`: verify the token immediately against the Cartesia API.
- `spitter login --json`: emit a JSON result object.

### Speech Synthesis
- `spitter say <text>`: synthesize speech from a positional transcript.
- `spitter say --stdin`: force transcript input from stdin.
- `spitter say --transport bytes|websocket`: choose one-shot bytes mode or websocket streaming.
- `spitter say --voice <id>`: synthesize with an explicit Cartesia voice ID.
- `spitter say --voice-query <text>`: resolve the first matching voice by search text.
- `spitter say`: supports language, model, container, encoding, sample rate, MP3 bit rate, speed, volume, emotion, pronunciation dictionary IDs, and timestamp requests.
- `spitter say --session <name>`: reuse a named local websocket session.
- `spitter say --session-policy start|require`: auto-start or require an existing local websocket session.
- `spitter say --session-idle-timeout <seconds>`: control idle timeout for auto-started sessions.
- `spitter say --play/--no-play`: enable or disable local playback.
- `spitter say --output <path>`: save the generated audio to a file.
- `spitter say --audio-check enforce|warn|ignore`: control playback preflight behavior against the local audio sink state.
- `spitter say --dry-run`: print the resolved request instead of calling the API.
- `spitter say --json`: emit a JSON result object.

### Local Websocket Sessions
- `spitter sessions start <name>`: start a named local websocket session daemon.
- `spitter sessions start --idle-timeout <seconds>`: set the idle timeout before the upstream websocket is closed.
- `spitter sessions list`: list local websocket session daemons.
- `spitter sessions get <name>`: fetch status for one local websocket session.
- `spitter sessions stop <name>`: stop a named local websocket session daemon.
- Session commands support `--json` for machine-readable output.

### Voice Inspection
- `spitter voices list`: list voices with page-size, cursor, query, ownership, gender, language, and preview URL controls.
- `spitter voices get <voice-id>`: fetch one voice by ID.
- Voice commands support JSON output and preview URL expansion.

### Self-Description
- `spitter describe`: emit the CLI contract, defaults, runtime assumptions, environment variables, and transport model as JSON.
- `spitter describe <topic>`: focus the description on a command prefix such as `say`, `sessions`, or `voices`.

## Runtime Defaults

- Default API base URL: `https://api.cartesia.ai`
- Default model: `sonic-3`
- Default language: `en`
- Default transport: `bytes`
- Default token file: `~/.config/spitter/cartesia-api-key`

## Quick Start

```bash
spitter login --validate

spitter say "Build finished."
echo "Tea is ready." | spitter say --stdin

spitter voices list --language en --query narrator
spitter describe say

spitter say "Tell me now." --transport websocket --container raw
```

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

## Credits

This client is built for the Cartesia speech API and is not affiliated with Cartesia.

Credit goes to Cartesia for the underlying voices, synthesis endpoints, realtime transport model, and API documentation this tool wraps.
