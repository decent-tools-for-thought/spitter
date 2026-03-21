#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from spitter_ws import (
    SessionDaemon,
    WebSocketError,
    get_session_paths,
    get_session_root,
    make_context_id,
    run_ephemeral_websocket_synthesis,
    sanitize_session_name,
)

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_BASE_URL = "https://api.cartesia.ai"
DEFAULT_API_VERSION = "2026-03-01"
DEFAULT_MODEL_ID = "sonic-3"
DEFAULT_LANGUAGE = "en"
DEFAULT_VOICE_ID = "71a7ad14-091c-4e8e-a314-022ece01c121"
DEFAULT_VOICE_NAME = "Charlotte - Heiress"
DEFAULT_TRANSPORT = "bytes"
DEFAULT_CONTAINER = "wav"
DEFAULT_ENCODING = "pcm_s16le"
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_SPEED = 1.0
DEFAULT_VOLUME = 1.0
DEFAULT_EMOTION = "neutral"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MP3_BIT_RATE = 128000
DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS = 90
DEFAULT_AUDIO_CHECK = "enforce"
USER_AGENT = "spitter/0.1.1"

SUPPORTED_TRANSPORTS = ("bytes", "websocket")
SUPPORTED_CONTAINERS = ("wav", "raw", "mp3")
SUPPORTED_STREAM_CONTAINERS = ("raw",)
SUPPORTED_ENCODINGS = ("pcm_f32le", "pcm_s16le", "pcm_mulaw", "pcm_alaw")
SUPPORTED_GENDERS = ("masculine", "feminine", "gender_neutral")
SUPPORTED_TIMESTAMP_MODES = ("off", "word")
SUPPORTED_SESSION_POLICIES = ("start", "require")
SUPPORTED_AUDIO_CHECK_POLICIES = ("enforce", "warn", "ignore")

DOCS = {
    "overview": "https://docs.cartesia.ai/get-started/overview",
    "voices": "https://docs.cartesia.ai/api-reference/voices/list",
    "voice": "https://docs.cartesia.ai/api-reference/voices/get",
    "tts_bytes": "https://docs.cartesia.ai/api-reference/tts/bytes",
    "tts_websocket": "https://docs.cartesia.ai/api-reference/tts/websocket",
    "compare_tts_endpoints": "https://docs.cartesia.ai/api-reference/tts/compare-tts-endpoints",
    "contexts": "https://docs.cartesia.ai/api-reference/tts/working-with-web-sockets/contexts",
}


class SpitterError(RuntimeError):
    pass


class HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    pass


@dataclass(frozen=True)
class AudioOutputStatus:
    backend: str
    sink: str | None
    volume: float | None
    muted: bool | None
    available: bool
    reason: str

    @property
    def ok_for_playback(self) -> bool:
        if not self.available:
            return False
        if self.muted:
            return False
        if self.volume is not None and self.volume <= 0.0:
            return False
        return True


@dataclass(frozen=True)
class RuntimeSettings:
    repo_root: Path
    token_file: Path
    base_url: str
    api_version: str
    default_model_id: str
    default_language: str
    default_voice_id: str | None
    default_voice_source: str
    ffplay_path: str | None
    session_root: Path
    default_session_idle_timeout_seconds: int
    default_audio_check: str


def get_runtime_settings() -> RuntimeSettings:
    token_override = os.getenv("SPITTER_TOKEN_FILE")
    token_file = (
        Path(token_override).expanduser()
        if token_override
        else SCRIPT_DIR / "token.txt"
    )
    idle_timeout = int(
        os.getenv(
            "SPITTER_SESSION_IDLE_TIMEOUT",
            str(DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS),
        )
    )
    return RuntimeSettings(
        repo_root=SCRIPT_DIR,
        token_file=token_file,
        base_url=os.getenv("CARTESIA_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        api_version=os.getenv("CARTESIA_API_VERSION", DEFAULT_API_VERSION),
        default_model_id=os.getenv("SPITTER_MODEL_ID", DEFAULT_MODEL_ID),
        default_language=os.getenv("SPITTER_LANGUAGE", DEFAULT_LANGUAGE),
        default_voice_id=os.getenv("SPITTER_VOICE_ID") or DEFAULT_VOICE_ID,
        default_voice_source="env" if os.getenv("SPITTER_VOICE_ID") else "builtin",
        ffplay_path=shutil.which("ffplay"),
        session_root=get_session_root(),
        default_session_idle_timeout_seconds=idle_timeout,
        default_audio_check=os.getenv("SPITTER_AUDIO_CHECK", DEFAULT_AUDIO_CHECK),
    )


def load_api_key(settings: RuntimeSettings) -> str:
    env_key = os.getenv("CARTESIA_API_KEY", "").strip()
    if env_key:
        return env_key

    if settings.token_file.exists():
        file_key = settings.token_file.read_text(encoding="utf-8").strip()
        if file_key:
            return file_key

    raise SpitterError(
        "No Cartesia API key found. Set CARTESIA_API_KEY or create "
        f"{settings.token_file}."
    )


def write_token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def resolve_login_token(args: argparse.Namespace) -> str:
    token_sources = sum(
        1 for enabled in (bool(args.token), bool(args.stdin)) if enabled
    )
    if token_sources > 1:
        raise SpitterError("Choose only one token source: --token or --stdin.")

    if args.token:
        token = args.token.strip()
    elif args.stdin:
        token = sys.stdin.read().strip()
    else:
        token = getpass.getpass("Cartesia API token: ").strip()

    if not token:
        raise SpitterError("Token is empty.")
    return token


def normalize_query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return str(value).lower()
    return value


def format_http_error(exc: HTTPError, payload: bytes) -> str:
    detail = payload.decode("utf-8", errors="replace").strip()
    if detail:
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            pass
        else:
            detail = json.dumps(parsed, indent=2, sort_keys=True)
    else:
        detail = exc.reason

    return f"Cartesia API error {exc.code} {exc.reason}: {detail}"


class CartesiaClient:
    def __init__(self, settings: RuntimeSettings, api_key: str) -> None:
        self.settings = settings
        self.api_key = api_key

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        url = f"{self.settings.base_url}/{path.lstrip('/')}"
        if query:
            encoded_query: list[tuple[str, Any]] = []
            for key, value in query.items():
                if value is None:
                    continue
                if isinstance(value, (list, tuple)):
                    for item in value:
                        encoded_query.append((key, normalize_query_value(item)))
                else:
                    encoded_query.append((key, normalize_query_value(value)))
            if encoded_query:
                url = f"{url}?{urlencode(encoded_query)}"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Cartesia-Version": self.settings.api_version,
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")

        request = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                return response.read(), dict(response.headers.items())
        except HTTPError as exc:
            payload = exc.read()
            raise SpitterError(format_http_error(exc, payload)) from exc
        except URLError as exc:
            raise SpitterError(f"Network error calling {url}: {exc.reason}") from exc

    def get_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        payload, _ = self._request(method, path, query=query, body=body)
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SpitterError(
                f"Expected JSON from {path}, got undecodable payload."
            ) from exc

    def get_bytes(
        self,
        path: str,
        *,
        body: dict[str, Any],
    ) -> tuple[bytes, dict[str, str]]:
        return self._request("POST", path, body=body)

    def list_voices(
        self,
        *,
        limit: int = 20,
        starting_after: str | None = None,
        ending_before: str | None = None,
        query_text: str | None = None,
        is_owner: bool | None = None,
        gender: str | None = None,
        language: str | None = None,
        include_preview_url: bool = False,
    ) -> dict[str, Any]:
        query = {
            "limit": limit,
            "starting_after": starting_after,
            "ending_before": ending_before,
            "q": query_text,
            "is_owner": is_owner,
            "gender": gender,
            "language": language,
        }
        if include_preview_url:
            query["expand[]"] = ["preview_file_url"]
        return self.get_json("GET", "/voices", query=query)

    def get_voice(self, voice_id: str, *, include_preview_url: bool = False) -> dict[str, Any]:
        query = {}
        if include_preview_url:
            query["expand[]"] = ["preview_file_url"]
        return self.get_json("GET", f"/voices/{voice_id}", query=query)

    def tts_bytes(self, body: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        return self.get_bytes("/tts/bytes", body=body)


def truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def render_voice_table(payload: dict[str, Any]) -> str:
    voices = payload.get("data", [])
    headers = ("id", "name", "lang", "owner", "public", "gender", "description")
    rows = [headers]
    for voice in voices:
        rows.append(
            (
                str(voice.get("id", "")),
                str(voice.get("name", "")),
                str(voice.get("language", "")),
                "yes" if voice.get("is_owner") else "no",
                "yes" if voice.get("is_public") else "no",
                str(voice.get("gender") or ""),
                truncate(str(voice.get("description") or ""), 56),
            )
        )

    widths = [max(len(row[index]) for row in rows) for index in range(len(headers))]
    lines = []
    for index, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if index == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(widths))))

    if payload.get("has_more"):
        lines.append("")
        lines.append("More voices are available. Use --starting-after with the last id.")
    return "\n".join(lines)


def render_session_table(payload: list[dict[str, Any]]) -> str:
    headers = ("name", "running", "idle_timeout_s", "active_contexts", "socket")
    rows = [headers]
    for entry in payload:
        status = entry.get("status") or {}
        upstream = status.get("upstream") or {}
        rows.append(
            (
                str(entry.get("name", "")),
                "yes" if entry.get("running") else "no",
                str(status.get("idle_timeout_seconds", "")),
                str(upstream.get("active_context_count", "")),
                str(status.get("socket_path", "")),
            )
        )

    widths = [max(len(row[index]) for row in rows) for index in range(len(headers))]
    lines = []
    for index, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if index == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(widths))))
    return "\n".join(lines)


def resolve_transcript(args: argparse.Namespace) -> str:
    if args.text and args.text != "-":
        return args.text.strip()

    should_read_stdin = args.stdin or args.text == "-" or not sys.stdin.isatty()
    if should_read_stdin:
        transcript = sys.stdin.read().strip()
        if transcript:
            return transcript

    raise SpitterError(
        "No transcript provided. Pass text as an argument or pipe it on stdin."
    )


def choose_output_path(
    *,
    container: str,
    requested_path: str | None,
    play_requested: bool,
) -> tuple[Path, bool]:
    suffix = f".{container}"
    if requested_path:
        return Path(requested_path).expanduser(), False

    if not play_requested:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        return Path.cwd() / f"spitter-{timestamp}{suffix}", False

    handle = tempfile.NamedTemporaryFile(prefix="spitter-", suffix=suffix, delete=False)
    handle.close()
    return Path(handle.name), True


def choose_stream_output_path(
    *,
    requested_path: str | None,
    play_requested: bool,
) -> Path | None:
    if requested_path:
        return Path(requested_path).expanduser()
    if play_requested:
        return None
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / f"spitter-{timestamp}.raw"


def write_audio_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def play_audio(ffplay_path: str, path: Path) -> None:
    result = subprocess.run(
        [
            ffplay_path,
            "-nodisp",
            "-autoexit",
            "-hide_banner",
            "-loglevel",
            "error",
            str(path),
        ],
        check=False,
    )
    if result.returncode != 0:
        raise SpitterError(f"ffplay exited with status {result.returncode}.")


def run_local_command(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    executable = shutil.which(command[0])
    if not executable:
        return None
    return subprocess.run(
        [executable, *command[1:]],
        capture_output=True,
        text=True,
        check=False,
    )


def probe_audio_output_status() -> AudioOutputStatus:
    wpctl_volume = run_local_command(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
    if wpctl_volume is not None and wpctl_volume.returncode == 0:
        volume_output = wpctl_volume.stdout.strip()
        volume_match = re.search(r"Volume:\s*([0-9.]+)", volume_output)
        volume = float(volume_match.group(1)) if volume_match else None
        muted = "[MUTED]" in volume_output

        sink_name = None
        pactl_sink = run_local_command(["pactl", "get-default-sink"])
        if pactl_sink is not None and pactl_sink.returncode == 0:
            sink_name = pactl_sink.stdout.strip() or None

        reason = f"default sink volume {volume_output}"
        if muted:
            reason = f"default sink is muted ({volume_output})"
        elif volume is not None and volume <= 0.0:
            reason = f"default sink volume is zero ({volume_output})"

        return AudioOutputStatus(
            backend="wpctl",
            sink=sink_name,
            volume=volume,
            muted=muted,
            available=True,
            reason=reason,
        )

    pactl_sink = run_local_command(["pactl", "get-default-sink"])
    if pactl_sink is not None and pactl_sink.returncode == 0:
        sink_name = pactl_sink.stdout.strip() or None
        mute_output = run_local_command(["pactl", "get-sink-mute", sink_name or ""])
        volume_output = run_local_command(["pactl", "get-sink-volume", sink_name or ""])
        muted = None
        volume = None
        reason = "Audio sink status unknown."

        if mute_output is not None and mute_output.returncode == 0:
            muted = mute_output.stdout.strip().endswith("yes")
            reason = mute_output.stdout.strip()

        if volume_output is not None and volume_output.returncode == 0:
            match = re.search(r"(\d+)%", volume_output.stdout)
            if match:
                volume = int(match.group(1)) / 100.0
                reason = volume_output.stdout.strip()

        if muted:
            reason = f"default sink {sink_name} is muted"
        elif volume is not None and volume <= 0.0:
            reason = f"default sink {sink_name} volume is zero"

        return AudioOutputStatus(
            backend="pactl",
            sink=sink_name,
            volume=volume,
            muted=muted,
            available=True,
            reason=reason,
        )

    details = []
    if wpctl_volume is not None and wpctl_volume.stderr.strip():
        details.append(wpctl_volume.stderr.strip().splitlines()[-1])
    if pactl_sink is not None and pactl_sink.stderr.strip():
        details.append(pactl_sink.stderr.strip().splitlines()[-1])
    detail_text = "; ".join(details) if details else "no audio backend commands succeeded"
    return AudioOutputStatus(
        backend="none",
        sink=None,
        volume=None,
        muted=None,
        available=False,
        reason=detail_text,
    )


def enforce_audio_output_policy(
    *,
    play_requested: bool,
    policy: str,
) -> AudioOutputStatus | None:
    if not play_requested or policy == "ignore":
        return None

    status = probe_audio_output_status()
    if status.ok_for_playback:
        return status

    if policy == "warn":
        print(
            f"warning: audio output may be off: {status.reason}",
            file=sys.stderr,
        )
        return status

    raise SpitterError(
        f"Audio output check failed: {status.reason}. "
        "Use --audio-check warn or --audio-check ignore to bypass."
    )


def resolve_voice(
    client: CartesiaClient,
    settings: RuntimeSettings,
    *,
    voice_id: str | None,
    voice_query: str | None,
    language: str,
) -> dict[str, Any]:
    if voice_id:
        return client.get_voice(voice_id)

    if voice_query:
        for owner_only in (True, None):
            payload = client.list_voices(
                limit=1,
                query_text=voice_query,
                is_owner=owner_only,
                language=language,
            )
            voices = payload.get("data", [])
            if voices:
                return voices[0]
        raise SpitterError(
            f"No voice matched query {voice_query!r} for language {language!r}."
        )

    if settings.default_voice_id:
        if settings.default_voice_source == "env":
            return client.get_voice(settings.default_voice_id)
        try:
            return client.get_voice(settings.default_voice_id)
        except SpitterError:
            pass

    for owner_only in (True, None):
        payload = client.list_voices(limit=1, is_owner=owner_only, language=language)
        voices = payload.get("data", [])
        if voices:
            return voices[0]

    raise SpitterError(
        "No usable voice found. Run `./spitter voices list` and pass --voice."
    )


def build_output_format(
    *,
    transport: str,
    container: str,
    encoding: str,
    sample_rate: int,
    bit_rate: int,
) -> dict[str, Any]:
    if transport == "websocket":
        if container != "raw":
            raise SpitterError(
                "Cartesia websocket streaming only supports raw audio output. "
                "Use --transport bytes for wav or mp3."
            )
        return {
            "container": "raw",
            "encoding": encoding,
            "sample_rate": sample_rate,
        }

    if container == "mp3":
        return {
            "container": "mp3",
            "sample_rate": sample_rate,
            "bit_rate": bit_rate,
        }

    return {
        "container": container,
        "encoding": encoding,
        "sample_rate": sample_rate,
    }


def build_tts_request(
    *,
    transport: str,
    transcript: str,
    model_id: str,
    voice_id: str,
    language: str,
    output_format: dict[str, Any],
    speed: float,
    volume: float,
    emotion: str,
    pronunciation_dict_id: str | None,
    context_id: str | None,
    add_timestamps: bool,
) -> dict[str, Any]:
    body = {
        "model_id": model_id,
        "transcript": transcript,
        "voice": {
            "mode": "id",
            "id": voice_id,
        },
        "output_format": output_format,
        "language": language,
        "generation_config": {
            "speed": speed,
            "volume": volume,
            "emotion": emotion,
        },
    }
    if pronunciation_dict_id:
        body["pronunciation_dict_id"] = pronunciation_dict_id

    if transport == "bytes":
        body["save"] = False
    else:
        if context_id is None:
            raise SpitterError("A websocket request requires a context_id.")
        body["context_id"] = context_id
        body["continue"] = False
        if add_timestamps:
            body["add_timestamps"] = True
    return body


def describe_command_schema(settings: RuntimeSettings) -> dict[str, Any]:
    return {
        "name": "spitter",
        "summary": "Repo-local Cartesia text-to-speech CLI for humans and coding agents.",
        "docs": DOCS,
        "defaults": {
            "base_url": settings.base_url,
            "api_version": settings.api_version,
            "transport": DEFAULT_TRANSPORT,
            "model_id": settings.default_model_id,
            "language": settings.default_language,
            "voice_id": settings.default_voice_id,
            "voice_name": DEFAULT_VOICE_NAME,
            "container": DEFAULT_CONTAINER,
            "encoding": DEFAULT_ENCODING,
            "sample_rate": DEFAULT_SAMPLE_RATE,
            "mp3_bit_rate": DEFAULT_MP3_BIT_RATE,
            "speed": DEFAULT_SPEED,
            "volume": DEFAULT_VOLUME,
            "emotion": DEFAULT_EMOTION,
            "audio_check": settings.default_audio_check,
            "session_idle_timeout_seconds": settings.default_session_idle_timeout_seconds,
            "playback_command": "ffplay -nodisp -autoexit -hide_banner -loglevel error <file>",
        },
        "runtime": {
            "repo_root": str(settings.repo_root),
            "token_file": str(settings.token_file),
            "token_file_exists": settings.token_file.exists(),
            "ffplay_available": bool(settings.ffplay_path),
            "session_root": str(settings.session_root),
        },
        "voice_resolution_order": [
            "--voice",
            "--voice-query",
            "SPITTER_VOICE_ID",
            f"built-in default voice {DEFAULT_VOICE_NAME} ({DEFAULT_VOICE_ID})",
            "first owned voice for the requested language if the built-in default is unavailable",
            "first public voice for the requested language",
        ],
        "transport_model": {
            "bytes": {
                "summary": "One-shot POST /tts/bytes. Simpler and returns complete audio before playback.",
                "supports_containers": ["wav", "raw", "mp3"],
                "supports_sessions": False,
            },
            "websocket": {
                "summary": (
                    "Streams raw audio over a websocket. Can run directly or through a named "
                    "local session daemon that keeps one upstream connection warm."
                ),
                "supports_containers": ["raw"],
                "supports_sessions": True,
                "session_lifecycle": (
                    "Named sessions persist locally until stopped, but their upstream websocket "
                    "connection is closed after the configured idle timeout and re-opened on demand."
                ),
            },
        },
        "environment": [
            {
                "name": "CARTESIA_API_KEY",
                "required": False,
                "purpose": "Preferred source for the Cartesia API token.",
            },
            {
                "name": "SPITTER_TOKEN_FILE",
                "required": False,
                "purpose": "Override the token file path. Defaults to ./token.txt.",
            },
            {
                "name": "CARTESIA_API_VERSION",
                "required": False,
                "purpose": f"Override the Cartesia-Version header. Defaults to {settings.api_version}.",
            },
            {
                "name": "CARTESIA_BASE_URL",
                "required": False,
                "purpose": f"Override the API base URL. Defaults to {settings.base_url}.",
            },
            {
                "name": "SPITTER_MODEL_ID",
                "required": False,
                "purpose": f"Override the default model. Defaults to {settings.default_model_id}.",
            },
            {
                "name": "SPITTER_LANGUAGE",
                "required": False,
                "purpose": f"Override the default language. Defaults to {settings.default_language}.",
            },
            {
                "name": "SPITTER_VOICE_ID",
                "required": False,
                "purpose": "Pin a default voice ID so `say` does not need to auto-select one.",
            },
            {
                "name": "SPITTER_SESSION_DIR",
                "required": False,
                "purpose": f"Override the local websocket session directory. Defaults to {settings.session_root}.",
            },
            {
                "name": "SPITTER_SESSION_IDLE_TIMEOUT",
                "required": False,
                "purpose": (
                    "Override the default websocket session idle timeout in seconds. "
                    f"Defaults to {settings.default_session_idle_timeout_seconds}."
                ),
            },
            {
                "name": "SPITTER_AUDIO_CHECK",
                "required": False,
                "purpose": (
                    "Override the default playback preflight policy. "
                    f"Defaults to {settings.default_audio_check}."
                ),
            },
        ],
        "commands": [
            {
                "name": "login",
                "summary": (
                    "Persist a Cartesia API token to the configured token file so later "
                    "commands can authenticate without inline secrets."
                ),
                "examples": [
                    "./spitter login",
                    "./spitter login --token <cartesia-token>",
                    "pass show cartesia/token | ./spitter login --stdin --validate",
                ],
                "options": ["--token", "--stdin", "--validate", "--json"],
            },
            {
                "name": "say",
                "summary": (
                    "Generate speech through POST /tts/bytes or websocket streaming. "
                    "Websocket mode can optionally reuse a named warm session."
                ),
                "examples": [
                    "./spitter say \"Build finished.\"",
                    "./spitter say \"Stream this now.\" --transport websocket",
                    "./spitter say \"Low-latency reply.\" --transport websocket --session default",
                    "./spitter say \"Save MP3 only.\" --container mp3 --bit-rate 128000 --no-play --output /tmp/notice.mp3",
                    "./spitter say \"Inspect the resolved request.\" --dry-run --json",
                ],
                "arguments": [
                    {
                        "name": "text",
                        "required": False,
                        "description": "Transcript to synthesize. Use - or --stdin to read from stdin.",
                    }
                ],
                "options": [
                    "--transport",
                    "--voice",
                    "--voice-query",
                    "--language",
                    "--model",
                    "--container",
                    "--encoding",
                    "--sample-rate",
                    "--bit-rate",
                    "--speed",
                    "--volume",
                    "--emotion",
                    "--pronunciation-dict-id",
                    "--timestamps",
                    "--audio-check",
                    "--session",
                    "--session-policy",
                    "--session-idle-timeout",
                    "--play/--no-play",
                    "--output",
                    "--stdin",
                    "--dry-run",
                    "--json",
                ],
            },
            {
                "name": "sessions start",
                "summary": "Start a named local websocket session daemon.",
                "examples": [
                    "./spitter sessions start default",
                    "./spitter sessions start low-latency --idle-timeout 120 --json",
                ],
                "arguments": [
                    {
                        "name": "name",
                        "required": True,
                        "description": "Local session name.",
                    }
                ],
                "options": ["--idle-timeout", "--json"],
            },
            {
                "name": "sessions list",
                "summary": "List local websocket sessions and their current status.",
                "examples": [
                    "./spitter sessions list",
                    "./spitter sessions list --json",
                ],
                "options": ["--json"],
            },
            {
                "name": "sessions get",
                "summary": "Fetch JSON status for one local websocket session.",
                "examples": [
                    "./spitter sessions get default",
                ],
                "arguments": [
                    {
                        "name": "name",
                        "required": True,
                        "description": "Local session name.",
                    }
                ],
                "options": ["--json"],
            },
            {
                "name": "sessions stop",
                "summary": "Stop a named local websocket session daemon.",
                "examples": [
                    "./spitter sessions stop default",
                ],
                "arguments": [
                    {
                        "name": "name",
                        "required": True,
                        "description": "Local session name.",
                    }
                ],
                "options": ["--json"],
            },
            {
                "name": "voices list",
                "summary": "List voices via GET /voices with language, ownership, and search filters.",
                "examples": [
                    "./spitter voices list",
                    "./spitter voices list --language de --query narrator",
                    "./spitter voices list --owned --preview-url --json",
                ],
                "options": [
                    "--limit",
                    "--starting-after",
                    "--ending-before",
                    "--query",
                    "--owned",
                    "--gender",
                    "--language",
                    "--preview-url",
                    "--json",
                ],
            },
            {
                "name": "voices get",
                "summary": "Fetch a specific voice via GET /voices/{id}.",
                "examples": [
                    "./spitter voices get <voice-id>",
                    "./spitter voices get <voice-id> --preview-url --json",
                ],
                "arguments": [
                    {
                        "name": "voice_id",
                        "required": True,
                        "description": "Cartesia voice identifier.",
                    }
                ],
                "options": ["--preview-url", "--json"],
            },
            {
                "name": "describe",
                "summary": "Print a machine-readable description of the CLI contract and runtime defaults.",
                "examples": [
                    "./spitter describe",
                    "./spitter describe say",
                    "./spitter describe sessions",
                ],
                "arguments": [
                    {
                        "name": "topic",
                        "required": False,
                        "description": "Optional command name prefix to filter the schema.",
                    }
                ],
                "options": ["--json"],
            },
        ],
    }


def filter_schema(schema: dict[str, Any], topic: str | None) -> dict[str, Any]:
    if not topic:
        return schema
    filtered = dict(schema)
    filtered["commands"] = [
        command
        for command in schema["commands"]
        if command["name"] == topic or command["name"].startswith(f"{topic} ")
    ]
    return filtered


def handle_describe(args: argparse.Namespace, settings: RuntimeSettings) -> int:
    schema = filter_schema(describe_command_schema(settings), args.topic)
    print_json(schema)
    return 0


def handle_login(args: argparse.Namespace, settings: RuntimeSettings) -> int:
    token = resolve_login_token(args)
    write_token_file(settings.token_file, token)

    validation = {
        "requested": args.validate,
        "ok": None,
        "detail": None,
    }
    if args.validate:
        try:
            client = CartesiaClient(settings, token)
            client.list_voices(limit=1, language=settings.default_language)
        except SpitterError as exc:
            validation["ok"] = False
            validation["detail"] = str(exc)
            if not args.json:
                print(
                    f"Token saved to {settings.token_file}, but validation failed: {exc}"
                )
            return 1
        validation["ok"] = True
        validation["detail"] = "Cartesia API accepted the token."

    result = {
        "command": "login",
        "token_file": str(settings.token_file),
        "token_file_exists": settings.token_file.exists(),
        "validation": validation,
    }
    if args.json:
        print_json(result)
    else:
        message = f"Saved Cartesia token to {settings.token_file}."
        if validation["ok"]:
            message += " Validation succeeded."
        elif validation["requested"]:
            message += f" Validation failed: {validation['detail']}"
        else:
            message += " Validation was skipped."
        print(message)
    return 0


def handle_voices_list(args: argparse.Namespace, settings: RuntimeSettings) -> int:
    client = CartesiaClient(settings, load_api_key(settings))
    payload = client.list_voices(
        limit=args.limit,
        starting_after=args.starting_after,
        ending_before=args.ending_before,
        query_text=args.query,
        is_owner=args.owned if args.owned else None,
        gender=args.gender,
        language=args.language,
        include_preview_url=args.preview_url,
    )
    if args.json:
        print_json(payload)
    else:
        print(render_voice_table(payload))
    return 0


def handle_voices_get(args: argparse.Namespace, settings: RuntimeSettings) -> int:
    client = CartesiaClient(settings, load_api_key(settings))
    payload = client.get_voice(args.voice_id, include_preview_url=args.preview_url)
    print_json(payload)
    return 0


def call_session(name: str, request: dict[str, Any]) -> dict[str, Any]:
    paths = get_session_paths(name)
    if not paths.socket_path.exists():
        raise SpitterError(f"Session {name!r} is not running.")

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(paths.socket_path))
        client.sendall((json.dumps(request) + "\n").encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        response = b""
        while not response.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
    except OSError as exc:
        raise SpitterError(f"Failed to contact session {name!r}: {exc}") from exc
    finally:
        client.close()

    if not response:
        raise SpitterError(f"Session {name!r} returned no response.")
    try:
        return json.loads(response.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SpitterError(f"Session {name!r} returned invalid JSON.") from exc


def get_session_status(name: str) -> dict[str, Any]:
    response = call_session(name, {"action": "status"})
    if not response.get("ok"):
        raise SpitterError(response.get("error", "Unknown session error."))
    return response["status"]


def spawn_session_daemon(
    name: str,
    settings: RuntimeSettings,
    *,
    idle_timeout_seconds: int,
) -> dict[str, Any]:
    paths = get_session_paths(name)
    paths.root.mkdir(parents=True, exist_ok=True)
    if paths.socket_path.exists():
        paths.socket_path.unlink()
    if paths.state_path.exists():
        paths.state_path.unlink()

    load_api_key(settings)

    log_handle = paths.log_path.open("ab")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT_DIR / "spitter.py"),
                "_sessiond",
                "--name",
                name,
                "--idle-timeout",
                str(idle_timeout_seconds),
            ],
            cwd=str(settings.repo_root),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    finally:
        log_handle.close()

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if process.poll() is not None:
            raise SpitterError(
                f"Session daemon for {name!r} exited early. Check {paths.log_path}."
            )
        try:
            return get_session_status(name)
        except SpitterError:
            time.sleep(0.1)

    raise SpitterError(
        f"Timed out waiting for session {name!r} to start. Check {paths.log_path}."
    )


def ensure_session(
    name: str,
    settings: RuntimeSettings,
    *,
    policy: str,
    idle_timeout_seconds: int,
) -> dict[str, Any]:
    try:
        status = get_session_status(name)
    except SpitterError:
        if policy == "require":
            raise
        status = spawn_session_daemon(
            name,
            settings,
            idle_timeout_seconds=idle_timeout_seconds,
        )

    current_idle_timeout = int(status.get("idle_timeout_seconds", idle_timeout_seconds))
    if current_idle_timeout != idle_timeout_seconds and policy == "start":
        raise SpitterError(
            f"Session {name!r} is already running with idle_timeout_seconds="
            f"{current_idle_timeout}. Stop it first to change the timeout."
        )
    return status


def list_sessions(settings: RuntimeSettings) -> list[dict[str, Any]]:
    results = []
    if not settings.session_root.exists():
        return results

    for state_file in sorted(settings.session_root.glob("*.json")):
        name = state_file.stem
        try:
            status = get_session_status(name)
        except SpitterError as exc:
            results.append(
                {
                    "name": name,
                    "running": False,
                    "error": str(exc),
                    "state_file": str(state_file),
                }
            )
            continue
        results.append({"name": name, "running": True, "status": status})
    return results


def handle_sessions_start(args: argparse.Namespace, settings: RuntimeSettings) -> int:
    name = sanitize_session_name(args.name)
    status = ensure_session(
        name,
        settings,
        policy="start",
        idle_timeout_seconds=args.idle_timeout,
    )
    if args.json:
        print_json(status)
    else:
        print(f"Session {name} is ready at {status['socket_path']}.")
    return 0


def handle_sessions_list(args: argparse.Namespace, settings: RuntimeSettings) -> int:
    payload = list_sessions(settings)
    if args.json:
        print_json(payload)
    else:
        if not payload:
            print("No local websocket sessions.")
        else:
            print(render_session_table(payload))
    return 0


def handle_sessions_get(args: argparse.Namespace, settings: RuntimeSettings) -> int:
    name = sanitize_session_name(args.name)
    status = get_session_status(name)
    print_json(status)
    return 0


def handle_sessions_stop(args: argparse.Namespace, settings: RuntimeSettings) -> int:
    name = sanitize_session_name(args.name)
    response = call_session(name, {"action": "shutdown"})
    if not response.get("ok"):
        raise SpitterError(response.get("error", "Unknown session shutdown error."))
    status = response["status"]
    if args.json:
        print_json(status)
    else:
        print(f"Session {name} stopped.")
    return 0


def handle_session_daemon(args: argparse.Namespace, settings: RuntimeSettings) -> int:
    name = sanitize_session_name(args.name)
    daemon = SessionDaemon(
        name=name,
        paths=get_session_paths(name),
        base_url=settings.base_url,
        api_key=load_api_key(settings),
        api_version=settings.api_version,
        user_agent=USER_AGENT,
        ffplay_path=settings.ffplay_path,
        idle_timeout_seconds=args.idle_timeout,
    )
    daemon.run()
    return 0


def validate_say_args(args: argparse.Namespace) -> None:
    if args.transport == "bytes":
        if args.session:
            raise SpitterError("Named sessions are only available with --transport websocket.")
        if args.timestamps != "off":
            raise SpitterError("Timestamp collection is only available with --transport websocket.")
        return

    if args.transport == "websocket" and args.container not in SUPPORTED_STREAM_CONTAINERS:
        raise SpitterError(
            "Cartesia websocket streaming only supports raw audio output. "
            "Use --container raw or switch to --transport bytes."
        )


def execute_bytes_say(
    *,
    args: argparse.Namespace,
    settings: RuntimeSettings,
    client: CartesiaClient,
    request_body: dict[str, Any],
    voice: dict[str, Any],
    output_format: dict[str, Any],
) -> dict[str, Any]:
    audio_bytes, headers = client.tts_bytes(request_body)
    output_path, should_delete_after = choose_output_path(
        container=str(output_format["container"]),
        requested_path=args.output,
        play_requested=args.play,
    )
    write_audio_file(output_path, audio_bytes)

    played = False
    playback_note = None
    if args.play:
        if settings.ffplay_path:
            play_audio(settings.ffplay_path, output_path)
            played = True
        else:
            playback_note = f"ffplay not found, audio saved to {output_path}"

    result = {
        "command": "say",
        "transport": "bytes",
        "session": None,
        "transcript": request_body["transcript"],
        "context_id": None,
        "model_id": request_body["model_id"],
        "voice": {
            "id": voice.get("id"),
            "name": voice.get("name"),
            "language": voice.get("language"),
        },
        "audio": {
            "bytes": len(audio_bytes),
            "container": output_format["container"],
            "encoding": output_format.get("encoding"),
            "sample_rate": output_format.get("sample_rate"),
            "bit_rate": output_format.get("bit_rate"),
            "played": played,
            "output_path": None if should_delete_after else str(output_path),
        },
        "timestamps": {
            "requested": False,
            "word": None,
            "phoneme": None,
        },
        "headers": {
            "Content-Type": headers.get("Content-Type"),
            "Cartesia-File-ID": headers.get("Cartesia-File-ID"),
        },
    }
    if playback_note:
        result["note"] = playback_note
    if should_delete_after:
        output_path.unlink(missing_ok=True)
        result["audio"]["temporary_playback_file"] = True
    return result


def execute_websocket_say(
    *,
    args: argparse.Namespace,
    settings: RuntimeSettings,
    api_key: str,
    request_body: dict[str, Any],
    voice: dict[str, Any],
    output_format: dict[str, Any],
) -> dict[str, Any]:
    requested_timestamps = args.timestamps == "word"
    output_path = choose_stream_output_path(
        requested_path=args.output,
        play_requested=args.play,
    )
    output_path_str = str(output_path) if output_path else None

    if args.session:
        session_name = sanitize_session_name(args.session)
        ensure_session(
            session_name,
            settings,
            policy=args.session_policy,
            idle_timeout_seconds=args.session_idle_timeout,
        )
        response = call_session(
            session_name,
            {
                "action": "speak",
                "transcript": request_body["transcript"],
                "context_id": request_body["context_id"],
                "encoding": output_format["encoding"],
                "sample_rate": output_format["sample_rate"],
                "play": args.play,
                "output_path": output_path_str,
                "requested_timestamps": requested_timestamps,
                "payload": request_body,
            },
        )
        if not response.get("ok"):
            raise SpitterError(response.get("error", "Unknown session websocket error."))
        result = response["result"]
    else:
        try:
            result = run_ephemeral_websocket_synthesis(
                base_url=settings.base_url,
                api_key=api_key,
                api_version=settings.api_version,
                user_agent=USER_AGENT,
                transcript=request_body["transcript"],
                payload=request_body,
                play=args.play,
                output_path=output_path_str,
                ffplay_path=settings.ffplay_path,
                encoding=output_format["encoding"],
                sample_rate=output_format["sample_rate"],
                requested_timestamps=requested_timestamps,
            )
        except WebSocketError as exc:
            raise SpitterError(str(exc)) from exc

    result.update(
        {
            "command": "say",
            "model_id": request_body["model_id"],
            "voice": {
                "id": voice.get("id"),
                "name": voice.get("name"),
                "language": voice.get("language"),
            },
        }
    )
    return result


def format_say_message(result: dict[str, Any]) -> str:
    voice = result["voice"]
    transport = result["transport"]
    session = result.get("session")
    audio = result["audio"]

    if transport == "websocket":
        message = (
            f"Streamed with voice {voice.get('name', voice.get('id'))} "
            f"({voice.get('id')})."
        )
        if session:
            message += f" Session {session} handled the request."
        if audio.get("output_path"):
            message += f" Raw audio saved to {audio['output_path']}."
        elif audio.get("played"):
            message += " Audio was streamed directly to ffplay."
        return message

    message = (
        f"Spoke with voice {voice.get('name', voice.get('id'))} "
        f"({voice.get('id')})."
    )
    if audio.get("temporary_playback_file"):
        message += " Audio was played from a temporary file."
    elif audio.get("output_path"):
        message += f" Audio saved to {audio['output_path']}."
    if result.get("note"):
        message += f" {result['note']}."
    return message


def handle_say(args: argparse.Namespace, settings: RuntimeSettings) -> int:
    validate_say_args(args)
    transcript = resolve_transcript(args)
    audio_output_status = None
    if not args.dry_run:
        audio_output_status = enforce_audio_output_policy(
            play_requested=args.play,
            policy=args.audio_check,
        )
    api_key = load_api_key(settings)
    client = CartesiaClient(settings, api_key)
    voice = resolve_voice(
        client,
        settings,
        voice_id=args.voice,
        voice_query=args.voice_query,
        language=args.language,
    )

    context_id = make_context_id() if args.transport == "websocket" else None
    requested_timestamps = args.timestamps == "word"
    output_format = build_output_format(
        transport=args.transport,
        container=args.container,
        encoding=args.encoding,
        sample_rate=args.sample_rate,
        bit_rate=args.bit_rate,
    )
    request_body = build_tts_request(
        transport=args.transport,
        transcript=transcript,
        model_id=args.model,
        voice_id=str(voice["id"]),
        language=args.language,
        output_format=output_format,
        speed=args.speed,
        volume=args.volume,
        emotion=args.emotion,
        pronunciation_dict_id=args.pronunciation_dict_id,
        context_id=context_id,
        add_timestamps=requested_timestamps,
    )

    if args.dry_run:
        payload = {
            "command": "say",
            "dry_run": True,
            "transport": args.transport,
            "session": args.session,
            "session_policy": args.session_policy if args.session else None,
            "session_idle_timeout_seconds": args.session_idle_timeout if args.session else None,
            "audio_check": args.audio_check,
            "voice": voice,
            "request": request_body,
        }
        print_json(payload)
        return 0

    if args.transport == "bytes":
        result = execute_bytes_say(
            args=args,
            settings=settings,
            client=client,
            request_body=request_body,
            voice=voice,
            output_format=output_format,
        )
    else:
        result = execute_websocket_say(
            args=args,
            settings=settings,
            api_key=api_key,
            request_body=request_body,
            voice=voice,
            output_format=output_format,
        )

    if args.json:
        result["audio_output"] = (
            None
            if audio_output_status is None
            else {
                "backend": audio_output_status.backend,
                "sink": audio_output_status.sink,
                "volume": audio_output_status.volume,
                "muted": audio_output_status.muted,
                "available": audio_output_status.available,
                "reason": audio_output_status.reason,
                "ok_for_playback": audio_output_status.ok_for_playback,
            }
        )
        print_json(result)
    else:
        print(format_say_message(result))
    return 0


def build_parser(settings: RuntimeSettings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spitter",
        formatter_class=HelpFormatter,
        description=(
            "Cartesia text-to-speech CLI for agents.\n\n"
            "Default behavior:\n"
            "- reads CARTESIA_API_KEY or repo-local token.txt\n"
            "- can write token.txt through `spitter login`\n"
            f"- uses Cartesia API version {settings.api_version}\n"
            "- defaults to POST /tts/bytes\n"
            "- can stream over websocket directly or through named local sessions\n"
            "- plays audio with ffplay when available"
        ),
        epilog=textwrap.dedent(
            f"""\
            Docs:
              {DOCS["overview"]}
              {DOCS["tts_bytes"]}
              {DOCS["tts_websocket"]}
              {DOCS["voices"]}

            Examples:
              ./spitter login --validate
              ./spitter say "Build finished."
              ./spitter say "Use websocket now." --transport websocket
              ./spitter sessions start default
              ./spitter say "Low latency." --transport websocket --session default
              ./spitter describe
            """
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser(
        "login",
        formatter_class=HelpFormatter,
        help="Persist a Cartesia API token to the configured token file.",
        description=(
            "Save a Cartesia API token to the configured token file so later commands "
            "can authenticate without inline secrets.\n"
            f"Default token path: {settings.token_file}"
        ),
    )
    login_parser.add_argument(
        "--token",
        help="Token value provided directly on the command line.",
    )
    login_parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read the token from stdin.",
    )
    login_parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate the token immediately by making a lightweight Cartesia API call.",
    )
    login_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON result object.",
    )
    login_parser.set_defaults(handler=handle_login)

    say_parser = subparsers.add_parser(
        "say",
        formatter_class=HelpFormatter,
        help="Synthesize speech through bytes or websocket transport.",
        description=(
            "Generate speech through Cartesia POST /tts/bytes or websocket streaming.\n"
            "Voice resolution order: --voice, --voice-query, SPITTER_VOICE_ID, "
            f"built-in {DEFAULT_VOICE_NAME}, first owned voice, first public voice."
        ),
    )
    say_parser.add_argument("text", nargs="?", help="Transcript to speak. Use - to read stdin.")
    say_parser.add_argument(
        "--stdin",
        action="store_true",
        help="Force reading the transcript from stdin.",
    )
    say_parser.add_argument(
        "--transport",
        choices=SUPPORTED_TRANSPORTS,
        default=DEFAULT_TRANSPORT,
        help="Cartesia transport to use.",
    )
    say_parser.add_argument("--voice", help="Explicit Cartesia voice ID.")
    say_parser.add_argument(
        "--voice-query",
        help="Search text used to resolve the first matching voice.",
    )
    say_parser.add_argument(
        "--language",
        default=settings.default_language,
        help="Language code passed to Cartesia and used for voice lookup.",
    )
    say_parser.add_argument(
        "--model",
        default=settings.default_model_id,
        help="Cartesia model ID.",
    )
    say_parser.add_argument(
        "--container",
        choices=SUPPORTED_CONTAINERS,
        default=DEFAULT_CONTAINER,
        help="Audio container. Websocket transport only supports raw.",
    )
    say_parser.add_argument(
        "--encoding",
        choices=SUPPORTED_ENCODINGS,
        default=DEFAULT_ENCODING,
        help="Audio encoding for raw and wav output.",
    )
    say_parser.add_argument(
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Output sample rate in Hz.",
    )
    say_parser.add_argument(
        "--bit-rate",
        type=int,
        default=DEFAULT_MP3_BIT_RATE,
        help="Bit rate for MP3 output with --transport bytes --container mp3.",
    )
    say_parser.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_SPEED,
        help="Sonic-3 generation_config.speed multiplier.",
    )
    say_parser.add_argument(
        "--volume",
        type=float,
        default=DEFAULT_VOLUME,
        help="Sonic-3 generation_config.volume multiplier.",
    )
    say_parser.add_argument(
        "--emotion",
        default=DEFAULT_EMOTION,
        help="Sonic-3 generation_config.emotion value.",
    )
    say_parser.add_argument(
        "--pronunciation-dict-id",
        help="Optional pronunciation dictionary ID for supported models.",
    )
    say_parser.add_argument(
        "--timestamps",
        choices=SUPPORTED_TIMESTAMP_MODES,
        default="off",
        help="Request word timestamps from websocket transport.",
    )
    say_parser.add_argument(
        "--audio-check",
        choices=SUPPORTED_AUDIO_CHECK_POLICIES,
        default=settings.default_audio_check,
        help="Playback preflight policy for local audio output state.",
    )
    say_parser.add_argument(
        "--session",
        help="Named local websocket session to reuse. Only valid with --transport websocket.",
    )
    say_parser.add_argument(
        "--session-policy",
        choices=SUPPORTED_SESSION_POLICIES,
        default="start",
        help="Whether to auto-start a named websocket session if it is missing.",
    )
    say_parser.add_argument(
        "--session-idle-timeout",
        type=int,
        default=settings.default_session_idle_timeout_seconds,
        help="Idle timeout in seconds for auto-started websocket sessions.",
    )
    say_parser.add_argument(
        "--play",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Play the generated audio with ffplay.",
    )
    say_parser.add_argument(
        "--output",
        help="Write audio to this path. Websocket transport writes raw audio.",
    )
    say_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved request instead of calling the API.",
    )
    say_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON result object.",
    )
    say_parser.set_defaults(handler=handle_say)

    sessions_parser = subparsers.add_parser(
        "sessions",
        formatter_class=HelpFormatter,
        help="Manage local websocket session daemons.",
    )
    sessions_subparsers = sessions_parser.add_subparsers(dest="sessions_command", required=True)

    sessions_start_parser = sessions_subparsers.add_parser(
        "start",
        formatter_class=HelpFormatter,
        help="Start a named websocket session daemon.",
    )
    sessions_start_parser.add_argument("name", help="Local session name.")
    sessions_start_parser.add_argument(
        "--idle-timeout",
        type=int,
        default=settings.default_session_idle_timeout_seconds,
        help="Idle timeout in seconds before the upstream websocket connection is closed.",
    )
    sessions_start_parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON session status.",
    )
    sessions_start_parser.set_defaults(handler=handle_sessions_start)

    sessions_list_parser = sessions_subparsers.add_parser(
        "list",
        formatter_class=HelpFormatter,
        help="List local websocket sessions.",
    )
    sessions_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON session status objects.",
    )
    sessions_list_parser.set_defaults(handler=handle_sessions_list)

    sessions_get_parser = sessions_subparsers.add_parser(
        "get",
        formatter_class=HelpFormatter,
        help="Fetch JSON status for one local websocket session.",
    )
    sessions_get_parser.add_argument("name", help="Local session name.")
    sessions_get_parser.add_argument(
        "--json",
        action="store_true",
        help="Kept for symmetry; output is always JSON.",
    )
    sessions_get_parser.set_defaults(handler=handle_sessions_get)

    sessions_stop_parser = sessions_subparsers.add_parser(
        "stop",
        formatter_class=HelpFormatter,
        help="Stop a named websocket session daemon.",
    )
    sessions_stop_parser.add_argument("name", help="Local session name.")
    sessions_stop_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the final JSON session status snapshot.",
    )
    sessions_stop_parser.set_defaults(handler=handle_sessions_stop)

    voices_parser = subparsers.add_parser(
        "voices",
        formatter_class=HelpFormatter,
        help="Inspect available voices.",
    )
    voices_subparsers = voices_parser.add_subparsers(dest="voices_command", required=True)

    voices_list_parser = voices_subparsers.add_parser(
        "list",
        formatter_class=HelpFormatter,
        help="List voices through GET /voices.",
    )
    voices_list_parser.add_argument("--limit", type=int, default=20, help="Page size (1-100).")
    voices_list_parser.add_argument("--starting-after", help="Pagination cursor.")
    voices_list_parser.add_argument("--ending-before", help="Pagination cursor.")
    voices_list_parser.add_argument("--query", help="Search text for name, description, or id.")
    voices_list_parser.add_argument(
        "--owned",
        action="store_true",
        help="Only return voices owned by your organization.",
    )
    voices_list_parser.add_argument(
        "--gender",
        choices=SUPPORTED_GENDERS,
        help="Filter by voice gender presentation.",
    )
    voices_list_parser.add_argument(
        "--language",
        default=settings.default_language,
        help="Filter by language or locale.",
    )
    voices_list_parser.add_argument(
        "--preview-url",
        action="store_true",
        help="Request preview_file_url expansion.",
    )
    voices_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw API payload as JSON.",
    )
    voices_list_parser.set_defaults(handler=handle_voices_list)

    voices_get_parser = voices_subparsers.add_parser(
        "get",
        formatter_class=HelpFormatter,
        help="Fetch a single voice through GET /voices/{id}.",
    )
    voices_get_parser.add_argument("voice_id", help="Voice ID to fetch.")
    voices_get_parser.add_argument(
        "--preview-url",
        action="store_true",
        help="Request preview_file_url expansion.",
    )
    voices_get_parser.add_argument(
        "--json",
        action="store_true",
        help="Kept for symmetry; output is always JSON.",
    )
    voices_get_parser.set_defaults(handler=handle_voices_get)

    describe_parser = subparsers.add_parser(
        "describe",
        formatter_class=HelpFormatter,
        help="Describe the CLI contract as JSON.",
    )
    describe_parser.add_argument(
        "topic",
        nargs="?",
        help="Optional command name prefix, for example 'say', 'sessions', or 'voices'.",
    )
    describe_parser.add_argument(
        "--json",
        action="store_true",
        help="Kept for symmetry; output is always JSON.",
    )
    describe_parser.set_defaults(handler=handle_describe)

    return parser


def maybe_run_internal_sessiond(
    argv: list[str] | None,
    settings: RuntimeSettings,
) -> int | None:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv or raw_argv[0] != "_sessiond":
        return None

    internal_parser = argparse.ArgumentParser(add_help=False)
    internal_parser.add_argument("_sessiond")
    internal_parser.add_argument("--name", required=True)
    internal_parser.add_argument("--idle-timeout", type=int, required=True)
    internal_args = internal_parser.parse_args(raw_argv)
    return handle_session_daemon(internal_args, settings)


def main(argv: list[str] | None = None) -> int:
    settings = get_runtime_settings()
    internal_result = maybe_run_internal_sessiond(argv, settings)
    if internal_result is not None:
        return internal_result
    parser = build_parser(settings)
    args = parser.parse_args(argv)
    try:
        return args.handler(args, settings)
    except SpitterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
