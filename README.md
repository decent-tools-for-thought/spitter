# spitter

`spitter` is a repo-local Cartesia text-to-speech CLI meant for humans and coding agents. It wraps the Cartesia `GET /voices`, `GET /voices/{id}`, `POST /tts/bytes`, and websocket TTS endpoints behind a stable command-line contract and a machine-readable `describe` command.

Cartesia does ship a separate CLI for Line voice agents, but that is aimed at building and deploying hosted agents rather than simple local text-to-speech playback. This repo fills that gap for "say this out loud on this machine" workflows.

## Requirements

- Python 3.13+
- `ffplay` for local playback
- A Cartesia API key in `CARTESIA_API_KEY` or repo-local `token.txt`

## Quick Start

```bash
./spitter say "Build finished."
```

Pipe text from another command:

```bash
echo "Tea is ready." | ./spitter say --stdin
```

List voices:

```bash
./spitter voices list --language en --query narrator
```

Inspect the command contract as JSON:

```bash
./spitter describe
```

Stream directly over websocket:

```bash
./spitter say "Tell me now." --transport websocket --container raw
```

Start a reusable named websocket session:

```bash
./spitter sessions start default
./spitter say "Low-latency reply." --transport websocket --container raw --session default
./spitter sessions stop default
```

## Defaults

- API base URL: `https://api.cartesia.ai`
- Cartesia API version: `2026-03-01`
- Transport: `bytes`
- Model: `sonic-3`
- Language: `en`
- Output format: `wav` / `pcm_s16le` / `44100`
- MP3 bit rate: `128000`
- Playback: enabled by default through `ffplay`
- Named websocket session idle timeout: `90` seconds

Voice resolution order for `say`:

1. `--voice`
2. `--voice-query`
3. `SPITTER_VOICE_ID`
4. First owned voice for the requested language
5. First public voice for the requested language

## Useful Commands

Speak with an explicit voice:

```bash
./spitter say "Stand-up starts in five minutes." --voice <voice-id>
```

Save bytes output without playing it:

```bash
./spitter say "Leave this on disk." --no-play --output /tmp/notice.wav
```

Save MP3 output without playing it:

```bash
./spitter say "Export MP3." --container mp3 --bit-rate 128000 --no-play --output /tmp/notice.mp3
```

Stream raw audio over websocket without playback:

```bash
./spitter say "Stream to disk." --transport websocket --container raw --no-play --output /tmp/notice.raw
```

Inspect websocket session status:

```bash
./spitter sessions get default
./spitter sessions list
```

Inspect the exact API request without sending it:

```bash
./spitter say "Dry run." --dry-run --json
```

Get voice details:

```bash
./spitter voices get <voice-id>
```

## Environment Variables

- `CARTESIA_API_KEY`: preferred token source
- `SPITTER_TOKEN_FILE`: override the token file path
- `CARTESIA_API_VERSION`: override the `Cartesia-Version` header
- `CARTESIA_BASE_URL`: override the API base URL
- `SPITTER_MODEL_ID`: override the default model
- `SPITTER_LANGUAGE`: override the default language
- `SPITTER_VOICE_ID`: pin a default voice ID
- `SPITTER_SESSION_DIR`: override the local websocket session directory
- `SPITTER_SESSION_IDLE_TIMEOUT`: override the default websocket session idle timeout in seconds

## Transport Model

`bytes` mode:

- Simpler one-shot request/response flow
- Supports `wav`, `raw`, and `mp3`
- Waits for the full audio response before playback
- Does not use local sessions

`websocket` mode:

- Streams audio chunks as they arrive
- Only supports raw audio output from Cartesia
- Can run directly, or through a named local session daemon
- Named sessions keep one upstream websocket warm until local idle timeout, then reconnect on demand
- Each `say` call uses a fresh Cartesia `context_id`; this tool does not currently expose multi-part continuation contexts

If you want the exact command contract and runtime defaults, prefer:

```bash
./spitter describe
./spitter describe say
./spitter describe sessions
```

## Audio Output Preflight

When playback is enabled, `spitter` now checks the local default sink before it talks to Cartesia.

- Default policy: `--audio-check enforce`
- Refuses playback if the default sink is muted
- Refuses playback if the default sink volume is `0`
- Refuses playback if it cannot discover a usable default sink

Override behavior when needed:

```bash
./spitter say "Warn but continue." --audio-check warn
./spitter say "Ignore sink state." --audio-check ignore
```

You can also set the default policy through:

- `SPITTER_AUDIO_CHECK`

## References

- Cartesia overview: <https://docs.cartesia.ai/get-started/overview>
- Text-to-speech bytes endpoint: <https://docs.cartesia.ai/api-reference/tts/bytes>
- Text-to-speech websocket endpoint: <https://docs.cartesia.ai/api-reference/tts/websocket>
- Compare TTS endpoints: <https://docs.cartesia.ai/api-reference/tts/compare-tts-endpoints>
- Voice list endpoint: <https://docs.cartesia.ai/api-reference/voices/list>
