from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import socketserver
import ssl
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse


class WebSocketError(RuntimeError):
    pass


class WebSocketClosedError(WebSocketError):
    pass


@dataclass(frozen=True)
class SessionPaths:
    root: Path
    name: str
    socket_path: Path
    state_path: Path
    log_path: Path


def sanitize_session_name(name: str) -> str:
    cleaned = "".join(character for character in name if character.isalnum() or character in ("-", "_", "."))
    cleaned = cleaned.strip(".")
    if not cleaned:
        raise WebSocketError("Session names may only use letters, numbers, ., _, and -.")
    return cleaned


def get_session_root() -> Path:
    return Path(os.getenv("SPITTER_SESSION_DIR", "/tmp/spitter-sessions")).expanduser()


def get_session_paths(name: str) -> SessionPaths:
    clean_name = sanitize_session_name(name)
    root = get_session_root()
    return SessionPaths(
        root=root,
        name=clean_name,
        socket_path=root / f"{clean_name}.sock",
        state_path=root / f"{clean_name}.json",
        log_path=root / f"{clean_name}.log",
    )


def build_websocket_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in ("https", "http"):
        raise WebSocketError(f"CARTESIA_BASE_URL must start with http:// or https://, got {base_url!r}.")

    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    resource_path = f"{base_path}/tts/websocket" if base_path else "/tts/websocket"
    if parsed.query:
        resource_path = f"{resource_path}?{parsed.query}"
    return f"{ws_scheme}://{parsed.netloc}{resource_path}"


def ffmpeg_input_format(encoding: str) -> str:
    mapping = {
        "pcm_s16le": "s16le",
        "pcm_f32le": "f32le",
        "pcm_mulaw": "mulaw",
        "pcm_alaw": "alaw",
    }
    try:
        return mapping[encoding]
    except KeyError as exc:
        raise WebSocketError(f"Unsupported ffplay encoding {encoding!r}.") from exc


def json_copy(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if data is None:
        return None
    return cast(dict[str, Any], json.loads(json.dumps(data)))


def merge_timestamp_payload(
    target: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged = json_copy(target) or {}
    for key, value in incoming.items():
        if isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key].extend(value)
        elif isinstance(value, list):
            merged[key] = list(value)
        else:
            merged[key] = value
    return merged


class SimpleWebSocketClient:
    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        connect_timeout: float = 10.0,
        recv_timeout: float = 1.0,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.connect_timeout = connect_timeout
        self.recv_timeout = recv_timeout
        self.sock: socket.socket | ssl.SSLSocket | None = None
        self._recv_buffer = bytearray()
        self._fragment_opcode: int | None = None
        self._fragment_payload = bytearray()
        self._closed = False

    def connect(self) -> None:
        parsed = urlparse(self.url)
        host = parsed.hostname
        if not host:
            raise WebSocketError(f"Invalid websocket URL: {self.url!r}")

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        resource = parsed.path or "/"
        if parsed.query:
            resource = f"{resource}?{parsed.query}"

        raw_sock = socket.create_connection((host, port), timeout=self.connect_timeout)
        if parsed.scheme == "wss":
            context = ssl.create_default_context()
            self.sock = context.wrap_socket(raw_sock, server_hostname=host)
        else:
            self.sock = raw_sock
        self.sock.settimeout(self.recv_timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = host if port in (80, 443) else f"{host}:{port}"
        request_lines = [
            f"GET {resource} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for header_name, header_value in self.headers.items():
            request_lines.append(f"{header_name}: {header_value}")
        request_lines.append("")
        request_lines.append("")
        request = "\r\n".join(request_lines).encode("ascii")
        self.sock.sendall(request)

        response = self._read_http_headers()
        status_line, headers = self._parse_http_headers(response)
        if not status_line.startswith("HTTP/1.1 101"):
            raise WebSocketError(f"WebSocket upgrade failed: {status_line}")

        accept = headers.get("sec-websocket-accept")
        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if accept != expected_accept:
            raise WebSocketError("WebSocket upgrade failed: invalid accept header.")

    def close(self, *, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        try:
            payload = code.to_bytes(2, "big") + reason.encode("utf-8")
            self._send_frame(0x8, payload)
        except OSError:
            pass
        finally:
            if self.sock is not None:
                try:
                    self.sock.close()
                finally:
                    self.sock = None

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_message(self, timeout: float | None = None) -> str | bytes:
        if self.sock is None:
            raise WebSocketClosedError("WebSocket is not connected.")

        original_timeout = self.sock.gettimeout()
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            while True:
                opcode, payload, finished = self._read_frame()
                if opcode == 0x8:
                    close_code = None
                    close_reason = ""
                    if len(payload) >= 2:
                        close_code = int.from_bytes(payload[:2], "big")
                        close_reason = payload[2:].decode("utf-8", errors="replace")
                    self.close()
                    raise WebSocketClosedError(f"Remote websocket closed ({close_code}): {close_reason}")
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue

                if opcode in (0x1, 0x2):
                    if finished:
                        if opcode == 0x1:
                            return payload.decode("utf-8")
                        return payload
                    self._fragment_opcode = opcode
                    self._fragment_payload = bytearray(payload)
                    continue

                if opcode == 0x0:
                    if self._fragment_opcode is None:
                        raise WebSocketError("Received continuation frame without a start.")
                    self._fragment_payload.extend(payload)
                    if finished:
                        full_payload = bytes(self._fragment_payload)
                        original_opcode = self._fragment_opcode
                        self._fragment_opcode = None
                        self._fragment_payload = bytearray()
                        if original_opcode == 0x1:
                            return full_payload.decode("utf-8")
                        return full_payload
                    continue

                raise WebSocketError(f"Unsupported websocket opcode {opcode}.")
        finally:
            if timeout is not None and self.sock is not None:
                self.sock.settimeout(original_timeout)

    def _send_frame(self, opcode: int, payload: bytes, *, finished: bool = True) -> None:
        if self.sock is None:
            raise WebSocketClosedError("WebSocket is not connected.")

        first_byte = opcode | (0x80 if finished else 0)
        payload_length = len(payload)
        mask_key = os.urandom(4)

        if payload_length < 126:
            header = bytes([first_byte, 0x80 | payload_length])
        elif payload_length < 65536:
            header = bytes([first_byte, 0x80 | 126]) + payload_length.to_bytes(2, "big")
        else:
            header = bytes([first_byte, 0x80 | 127]) + payload_length.to_bytes(8, "big")

        masked_payload = bytes(payload[index] ^ mask_key[index % 4] for index in range(payload_length))
        self.sock.sendall(header + mask_key + masked_payload)

    def _read_http_headers(self) -> bytes:
        if self.sock is None:
            raise WebSocketClosedError("WebSocket is not connected.")

        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("Connection closed during websocket handshake.")
            response.extend(chunk)
        header_bytes, _, remainder = response.partition(b"\r\n\r\n")
        self._recv_buffer.extend(remainder)
        return header_bytes

    def _parse_http_headers(self, data: bytes) -> tuple[str, dict[str, str]]:
        lines = data.decode("utf-8", errors="replace").split("\r\n")
        status_line = lines[0]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return status_line, headers

    def _read_exactly(self, length: int) -> bytes:
        if self.sock is None:
            raise WebSocketClosedError("WebSocket is not connected.")

        while len(self._recv_buffer) < length:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout as exc:
                raise TimeoutError from exc
            if not chunk:
                raise WebSocketClosedError("Remote websocket closed the connection.")
            self._recv_buffer.extend(chunk)
        payload = bytes(self._recv_buffer[:length])
        del self._recv_buffer[:length]
        return payload

    def _read_frame(self) -> tuple[int, bytes, bool]:
        first_two = self._read_exactly(2)
        first_byte, second_byte = first_two
        finished = bool(first_byte & 0x80)
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        payload_length = second_byte & 0x7F

        if payload_length == 126:
            payload_length = int.from_bytes(self._read_exactly(2), "big")
        elif payload_length == 127:
            payload_length = int.from_bytes(self._read_exactly(8), "big")

        mask_key = self._read_exactly(4) if masked else b""
        payload = self._read_exactly(payload_length)
        if masked:
            payload = bytes(payload[index] ^ mask_key[index % 4] for index in range(payload_length))
        return opcode, payload, finished


class StreamAudioSink:
    def __init__(
        self,
        *,
        play: bool,
        output_path: str | None,
        ffplay_path: str | None,
        encoding: str,
        sample_rate: int,
    ) -> None:
        self.play_requested = play
        self.output_path = output_path
        self.ffplay_path = ffplay_path
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.audio_bytes = 0
        self._file_handle = None
        self._player: subprocess.Popen[bytes] | None = None
        self._closed = False
        self._playback_started = False

        if self.output_path:
            output = Path(self.output_path).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            self._file_handle = output.open("wb")

        if self.play_requested:
            if not self.ffplay_path:
                raise WebSocketError("ffplay is required for websocket playback. Use --no-play or install ffmpeg.")
            self._player = subprocess.Popen(
                [
                    self.ffplay_path,
                    "-nodisp",
                    "-autoexit",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    ffmpeg_input_format(self.encoding),
                    "-ar",
                    str(self.sample_rate),
                    "-ac",
                    "1",
                    "pipe:0",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._playback_started = True

    def write(self, chunk: bytes) -> None:
        if self._closed:
            raise WebSocketError("Cannot write audio after the sink has closed.")

        self.audio_bytes += len(chunk)
        if self._file_handle is not None:
            self._file_handle.write(chunk)
            self._file_handle.flush()

        if self._player is not None and self._player.stdin is not None:
            try:
                self._player.stdin.write(chunk)
                self._player.stdin.flush()
            except BrokenPipeError as exc:
                raise WebSocketError("ffplay stopped accepting audio data.") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._file_handle is not None:
            self._file_handle.close()
            self._file_handle = None

        if self._player is not None:
            if self._player.stdin is not None:
                self._player.stdin.close()
            return_code = self._player.wait()
            if return_code != 0:
                raise WebSocketError(f"ffplay exited with status {return_code}.")
            self._player = None

    def abort(self) -> None:
        if self._file_handle is not None:
            self._file_handle.close()
            self._file_handle = None
        if self._player is not None:
            self._player.kill()
            self._player.wait()
            self._player = None
        self._closed = True

    @property
    def played(self) -> bool:
        return self._playback_started


@dataclass
class SynthesisTask:
    transcript: str
    context_id: str
    play: bool
    output_path: str | None
    ffplay_path: str | None
    encoding: str
    sample_rate: int
    session_name: str | None
    transport: str
    requested_timestamps: bool
    sink: StreamAudioSink = field(init=False)
    done_event: threading.Event = field(default_factory=threading.Event, init=False)
    audio_bytes: int = field(default=0, init=False)
    step_times: list[int] = field(default_factory=list, init=False)
    word_timestamps: dict[str, Any] | None = field(default=None, init=False)
    phoneme_timestamps: dict[str, Any] | None = field(default=None, init=False)
    status_code: int | None = field(default=None, init=False)
    error: str | None = field(default=None, init=False)
    closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.sink = StreamAudioSink(
            play=self.play,
            output_path=self.output_path,
            ffplay_path=self.ffplay_path,
            encoding=self.encoding,
            sample_rate=self.sample_rate,
        )

    def on_message(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        self.status_code = message.get("status_code", self.status_code)

        if message_type == "chunk":
            audio_chunk = base64.b64decode(message.get("data", ""))
            self.sink.write(audio_chunk)
            self.audio_bytes += len(audio_chunk)
            step_time = message.get("step_time")
            if isinstance(step_time, int):
                self.step_times.append(step_time)
            return

        if message_type == "timestamps":
            if "word_timestamps" in message:
                self.word_timestamps = merge_timestamp_payload(
                    self.word_timestamps,
                    message["word_timestamps"],
                )
            if "phoneme_timestamps" in message:
                self.phoneme_timestamps = merge_timestamp_payload(
                    self.phoneme_timestamps,
                    message["phoneme_timestamps"],
                )
            return

        if message_type == "done":
            self.done_event.set()
            return

        if "error" in message:
            self.error = str(message["error"])
            self.done_event.set()

    def fail(self, message: str) -> None:
        self.error = message
        self.done_event.set()

    def finish(self) -> dict[str, Any]:
        if self.closed:
            return self.build_result()

        self.closed = True
        if self.error:
            self.sink.abort()
            raise WebSocketError(self.error)

        self.sink.close()
        return self.build_result()

    def build_result(self) -> dict[str, Any]:
        result = {
            "transport": self.transport,
            "session": self.session_name,
            "context_id": self.context_id,
            "transcript": self.transcript,
            "audio": {
                "bytes": self.audio_bytes,
                "container": "raw",
                "encoding": self.encoding,
                "sample_rate": self.sample_rate,
                "played": self.sink.played,
                "output_path": self.output_path,
            },
            "timestamps": {
                "requested": self.requested_timestamps,
                "word": self.word_timestamps,
                "phoneme": self.phoneme_timestamps,
            },
            "status_code": self.status_code,
            "step_times_ms": self.step_times,
        }
        return result


class CartesiaWebSocketDispatcher:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_version: str,
        user_agent: str,
        connect_timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.api_version = api_version
        self.user_agent = user_agent
        self.connect_timeout = connect_timeout
        self.connection_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.tasks_lock = threading.Lock()
        self.tasks: dict[str, SynthesisTask] = {}
        self.client: SimpleWebSocketClient | None = None
        self.receiver_thread: threading.Thread | None = None
        self.shutdown_event = threading.Event()
        self.last_activity_at = time.time()
        self.last_connect_at: float | None = None
        self.last_disconnect_reason: str | None = None
        self.websocket_url = build_websocket_url(base_url)

    def ensure_connected(self) -> None:
        with self.connection_lock:
            if self.client is not None:
                return

            headers = {
                "X-API-Key": self.api_key,
                "Cartesia-Version": self.api_version,
                "User-Agent": self.user_agent,
            }
            client = SimpleWebSocketClient(
                self.websocket_url,
                headers=headers,
                connect_timeout=self.connect_timeout,
            )
            client.connect()
            self.client = client
            self.last_connect_at = time.time()
            self.last_disconnect_reason = None
            self.shutdown_event.clear()
            self.receiver_thread = threading.Thread(
                target=self._receiver_loop,
                name="spitter-cartesia-recv",
                daemon=True,
            )
            self.receiver_thread.start()

    def submit(self, request: dict[str, Any], task: SynthesisTask) -> dict[str, Any]:
        self.ensure_connected()
        payload = json.dumps(request)

        with self.tasks_lock:
            self.tasks[task.context_id] = task
            self.last_activity_at = time.time()

        try:
            with self.send_lock:
                if self.client is None:
                    raise WebSocketError("WebSocket connection is not available.")
                self.client.send_text(payload)
        except Exception:
            with self.tasks_lock:
                self.tasks.pop(task.context_id, None)
            task.fail("Failed to send websocket request.")
            raise

        task.done_event.wait()
        with self.tasks_lock:
            self.tasks.pop(task.context_id, None)
        self.last_activity_at = time.time()
        return task.finish()

    def _receiver_loop(self) -> None:
        while not self.shutdown_event.is_set():
            client = self.client
            if client is None:
                return
            try:
                payload = client.recv_message(timeout=1.0)
            except TimeoutError:
                continue
            except Exception as exc:
                self._handle_disconnect(str(exc))
                return

            if not isinstance(payload, str):
                continue

            self.last_activity_at = time.time()
            try:
                message = json.loads(payload)
            except json.JSONDecodeError:
                continue

            context_id = message.get("context_id")
            if not context_id:
                continue
            with self.tasks_lock:
                task = self.tasks.get(context_id)
            if task is not None:
                task.on_message(message)

    def _handle_disconnect(self, reason: str) -> None:
        with self.connection_lock:
            client = self.client
            self.client = None
            self.last_disconnect_reason = reason
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        with self.tasks_lock:
            pending = list(self.tasks.values())
            self.tasks.clear()
        for task in pending:
            task.fail(f"WebSocket connection closed: {reason}")

    def maybe_close_idle(self, idle_timeout_seconds: int) -> None:
        with self.tasks_lock:
            has_active_tasks = bool(self.tasks)
        if has_active_tasks:
            return
        if self.client is None:
            return
        if time.time() - self.last_activity_at < idle_timeout_seconds:
            return
        self.close("idle timeout")

    def close(self, reason: str = "client requested") -> None:
        self.shutdown_event.set()
        with self.connection_lock:
            client = self.client
            self.client = None
            self.last_disconnect_reason = reason
        if client is not None:
            client.close(reason=reason[:120])

    def status(self) -> dict[str, Any]:
        with self.tasks_lock:
            active_contexts = sorted(self.tasks.keys())
        return {
            "connected": self.client is not None,
            "active_contexts": active_contexts,
            "active_context_count": len(active_contexts),
            "last_activity_at": self.last_activity_at,
            "last_connect_at": self.last_connect_at,
            "last_disconnect_reason": self.last_disconnect_reason,
            "websocket_url": self.websocket_url,
        }


class _ThreadingUnixStreamServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class SessionRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw_request = self.rfile.readline()
        if not raw_request:
            return
        request = json.loads(raw_request.decode("utf-8"))
        response = self.server.controller.handle_request(request)  # type: ignore[attr-defined]
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
        self.wfile.flush()


class SessionDaemon:
    def __init__(
        self,
        *,
        name: str,
        paths: SessionPaths,
        base_url: str,
        api_key: str,
        api_version: str,
        user_agent: str,
        ffplay_path: str | None,
        idle_timeout_seconds: int,
    ) -> None:
        self.name = name
        self.paths = paths
        self.base_url = base_url
        self.api_key = api_key
        self.api_version = api_version
        self.user_agent = user_agent
        self.ffplay_path = ffplay_path
        self.idle_timeout_seconds = idle_timeout_seconds
        self.started_at = time.time()
        self.shutdown_requested = threading.Event()
        self.dispatcher = CartesiaWebSocketDispatcher(
            base_url=base_url,
            api_key=api_key,
            api_version=api_version,
            user_agent=user_agent,
        )
        self.server: _ThreadingUnixStreamServer | None = None

    def build_status(self) -> dict[str, Any]:
        dispatcher_status = self.dispatcher.status()
        return {
            "name": self.name,
            "pid": os.getpid(),
            "socket_path": str(self.paths.socket_path),
            "state_path": str(self.paths.state_path),
            "log_path": str(self.paths.log_path),
            "started_at": self.started_at,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "ffplay_available": bool(self.ffplay_path),
            "upstream": dispatcher_status,
        }

    def write_state(self) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.state_path.write_text(
            json.dumps(self.build_status(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def run(self) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        if self.paths.socket_path.exists():
            self.paths.socket_path.unlink()

        self.server = _ThreadingUnixStreamServer(
            str(self.paths.socket_path),
            SessionRequestHandler,
        )
        self.server.controller = self  # type: ignore[attr-defined]

        maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name=f"spitter-session-{self.name}-maintenance",
            daemon=True,
        )
        maintenance_thread.start()
        self.write_state()

        try:
            self.server.serve_forever(poll_interval=0.5)
        finally:
            self.dispatcher.close("session shutdown")
            if self.server is not None:
                self.server.server_close()
            if self.paths.socket_path.exists():
                self.paths.socket_path.unlink()
            if self.paths.state_path.exists():
                self.paths.state_path.unlink()

    def _maintenance_loop(self) -> None:
        while not self.shutdown_requested.is_set():
            self.dispatcher.maybe_close_idle(self.idle_timeout_seconds)
            self.write_state()
            time.sleep(1.0)

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "status":
            return {"ok": True, "status": self.build_status()}

        if action == "shutdown":
            self.shutdown_requested.set()
            response = {"ok": True, "status": self.build_status()}
            if self.server is not None:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            return response

        if action != "speak":
            return {"ok": False, "error": f"Unsupported session action {action!r}."}

        task = SynthesisTask(
            transcript=str(request["transcript"]),
            context_id=str(request["context_id"]),
            play=bool(request.get("play", False)),
            output_path=request.get("output_path"),
            ffplay_path=self.ffplay_path,
            encoding=str(request["encoding"]),
            sample_rate=int(request["sample_rate"]),
            session_name=self.name,
            transport="websocket",
            requested_timestamps=bool(request.get("requested_timestamps", False)),
        )
        try:
            result = self.dispatcher.submit(request["payload"], task)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": result}


def run_ephemeral_websocket_synthesis(
    *,
    base_url: str,
    api_key: str,
    api_version: str,
    user_agent: str,
    transcript: str,
    payload: dict[str, Any],
    play: bool,
    output_path: str | None,
    ffplay_path: str | None,
    encoding: str,
    sample_rate: int,
    requested_timestamps: bool,
) -> dict[str, Any]:
    dispatcher = CartesiaWebSocketDispatcher(
        base_url=base_url,
        api_key=api_key,
        api_version=api_version,
        user_agent=user_agent,
    )
    context_id = str(payload["context_id"])
    task = SynthesisTask(
        transcript=transcript,
        context_id=context_id,
        play=play,
        output_path=output_path,
        ffplay_path=ffplay_path,
        encoding=encoding,
        sample_rate=sample_rate,
        session_name=None,
        transport="websocket",
        requested_timestamps=requested_timestamps,
    )
    try:
        return dispatcher.submit(payload, task)
    finally:
        dispatcher.close("ephemeral completion")


def make_context_id() -> str:
    return str(uuid.uuid4())
