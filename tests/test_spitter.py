from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import spitter
import spitter.core as spitter_core
import spitter.websocket as spitter_websocket


class SpitterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_patch = mock.patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()

    def test_load_api_key_prefers_environment(self) -> None:
        os.environ["CARTESIA_API_KEY"] = "from-env"
        settings = spitter.get_runtime_settings()
        self.assertEqual(spitter.load_api_key(settings), "from-env")

    def test_runtime_settings_default_to_charlotte(self) -> None:
        settings = spitter.get_runtime_settings()
        self.assertEqual(settings.default_voice_id, spitter.DEFAULT_VOICE_ID)
        self.assertEqual(settings.default_voice_source, "builtin")

    def test_runtime_settings_use_installed_safe_default_token_path(self) -> None:
        settings = spitter.get_runtime_settings()
        self.assertEqual(settings.token_file, spitter.get_default_token_file())

    def test_default_token_file_uses_xdg_config_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            os.environ["XDG_CONFIG_HOME"] = directory
            self.assertEqual(
                spitter.get_default_token_file(),
                Path(directory) / "spitter" / "cartesia-api-key",
            )

    def test_default_token_file_falls_back_to_home_config(self) -> None:
        fake_home = Path("/tmp/spitter-home")
        with mock.patch("spitter.core.Path.home", return_value=fake_home):
            self.assertEqual(
                spitter.get_default_token_file(),
                fake_home / ".config" / "spitter" / "cartesia-api-key",
            )

    def test_load_api_key_reads_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "cartesia-api-key"
            token_path.write_text("from-file\n", encoding="utf-8")
            os.environ["SPITTER_TOKEN_FILE"] = str(token_path)
            settings = spitter.get_runtime_settings()
            self.assertEqual(spitter.load_api_key(settings), "from-file")

    def test_handle_login_writes_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "cartesia-api-key"
            os.environ["SPITTER_TOKEN_FILE"] = str(token_path)
            settings = spitter.get_runtime_settings()
            args = mock.Mock(token="from-login", stdin=False, validate=False, json=False)
            exit_code = spitter.handle_login(args, settings)
            self.assertEqual(exit_code, 0)
            self.assertEqual(token_path.read_text(encoding="utf-8"), "from-login\n")

    def test_handle_login_writes_default_xdg_token_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            os.environ["XDG_CONFIG_HOME"] = directory
            settings = spitter.get_runtime_settings()
            args = mock.Mock(token="xdg-login", stdin=False, validate=False, json=False)
            exit_code = spitter.handle_login(args, settings)
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                (Path(directory) / "spitter" / "cartesia-api-key").read_text(encoding="utf-8"),
                "xdg-login\n",
            )

    @mock.patch("sys.stdin", new_callable=io.StringIO)
    def test_handle_login_reads_stdin(self, stdin: io.StringIO) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "cartesia-api-key"
            os.environ["SPITTER_TOKEN_FILE"] = str(token_path)
            stdin.write("stdin-token\n")
            stdin.seek(0)
            settings = spitter.get_runtime_settings()
            args = mock.Mock(token=None, stdin=True, validate=False, json=False)
            exit_code = spitter.handle_login(args, settings)
            self.assertEqual(exit_code, 0)
            self.assertEqual(token_path.read_text(encoding="utf-8"), "stdin-token\n")

    @mock.patch("subprocess.Popen")
    def test_spawn_session_daemon_uses_module_entrypoint(self, popen: mock.Mock) -> None:
        settings = spitter.get_runtime_settings()
        process = mock.Mock()
        process.poll.return_value = None
        popen.return_value = process

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "session.log"
            status = {"socket_path": "/tmp/spitter.sock", "idle_timeout_seconds": 90}
            with (
                mock.patch("spitter.core.get_session_paths") as get_paths,
                mock.patch("spitter.core.load_api_key"),
                mock.patch("spitter.core.get_session_status", return_value=status),
            ):
                get_paths.return_value = mock.Mock(
                    root=Path(directory),
                    socket_path=Path(directory) / "session.sock",
                    state_path=Path(directory) / "session.json",
                    log_path=log_path,
                )
                result = spitter_core.spawn_session_daemon(
                    "default",
                    settings,
                    idle_timeout_seconds=90,
                )

        self.assertEqual(result, status)
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(
            args[0][:4],
            [sys.executable, "-m", "spitter", "_sessiond"],
        )
        self.assertNotIn("cwd", kwargs)

    def test_runtime_settings_respect_session_root_and_idle_timeout(self) -> None:
        os.environ["SPITTER_SESSION_DIR"] = "~/spitter-sessions"
        os.environ["SPITTER_SESSION_IDLE_TIMEOUT"] = "123"
        settings = spitter.get_runtime_settings()
        self.assertEqual(
            settings.session_root,
            Path("~/spitter-sessions").expanduser(),
        )
        self.assertEqual(settings.default_session_idle_timeout_seconds, 123)

    def test_build_tts_request_uses_expected_shape(self) -> None:
        output_format = spitter.build_output_format(
            transport="bytes",
            container="wav",
            encoding="pcm_s16le",
            sample_rate=44100,
            bit_rate=128000,
        )
        request = spitter.build_tts_request(
            transport="bytes",
            transcript="hello",
            model_id="sonic-3",
            voice_id="voice-123",
            language="en",
            output_format=output_format,
            speed=1.1,
            volume=0.9,
            emotion="curious",
            pronunciation_dict_id="dict-123",
            context_id=None,
            add_timestamps=False,
        )
        self.assertEqual(request["model_id"], "sonic-3")
        self.assertEqual(request["voice"]["id"], "voice-123")
        self.assertEqual(request["output_format"]["container"], "wav")
        self.assertEqual(request["generation_config"]["emotion"], "curious")
        self.assertEqual(request["pronunciation_dict_id"], "dict-123")
        self.assertFalse(request["save"])

    def test_filter_schema_by_topic(self) -> None:
        schema = spitter.describe_command_schema(spitter.get_runtime_settings())
        filtered = spitter.filter_schema(schema, "voices")
        command_names = [command["name"] for command in filtered["commands"]]
        self.assertEqual(command_names, ["voices list", "voices get"])

    def test_filter_schema_can_find_login(self) -> None:
        schema = spitter.describe_command_schema(spitter.get_runtime_settings())
        filtered = spitter.filter_schema(schema, "login")
        command_names = [command["name"] for command in filtered["commands"]]
        self.assertEqual(command_names, ["login"])

    def test_describe_schema_contract_has_expected_command_names(self) -> None:
        schema = spitter.describe_command_schema(spitter.get_runtime_settings())
        self.assertEqual(
            [command["name"] for command in schema["commands"]],
            [
                "login",
                "say",
                "sessions start",
                "sessions list",
                "sessions get",
                "sessions stop",
                "voices list",
                "voices get",
                "describe",
            ],
        )
        self.assertEqual(schema["defaults"]["audio_check"], spitter.DEFAULT_AUDIO_CHECK)
        self.assertIn("--session-idle-timeout", schema["commands"][1]["options"])
        self.assertIn("voice_resolution_order", schema)

    def test_websocket_output_rejects_wav(self) -> None:
        with self.assertRaises(spitter.SpitterError):
            spitter.build_output_format(
                transport="websocket",
                container="wav",
                encoding="pcm_s16le",
                sample_rate=44100,
                bit_rate=128000,
            )

    @mock.patch("spitter.core.run_local_command")
    def test_probe_audio_output_status_notices_muted_wpctl(self, run_local_command: mock.Mock) -> None:
        run_local_command.side_effect = [
            mock.Mock(returncode=0, stdout="Volume: 0.75 [MUTED]\n", stderr=""),
            mock.Mock(returncode=0, stdout="alsa_output.test\n", stderr=""),
        ]
        status = spitter.probe_audio_output_status()
        self.assertTrue(status.available)
        self.assertTrue(status.muted)
        self.assertFalse(status.ok_for_playback)
        self.assertEqual(status.sink, "alsa_output.test")

    @mock.patch("spitter.core.run_local_command")
    def test_probe_audio_output_status_falls_back_to_pactl(self, run_local_command: mock.Mock) -> None:
        run_local_command.side_effect = [
            None,
            mock.Mock(returncode=0, stdout="alsa_output.test\n", stderr=""),
            mock.Mock(returncode=0, stdout="Mute: no\n", stderr=""),
            mock.Mock(
                returncode=0,
                stdout="Volume: front-left: 65536 / 100% / 0.00 dB\n",
                stderr="",
            ),
        ]
        status = spitter.probe_audio_output_status()
        self.assertEqual(status.backend, "pactl")
        self.assertEqual(status.sink, "alsa_output.test")
        self.assertEqual(status.volume, 1.0)
        self.assertFalse(status.muted)
        self.assertTrue(status.ok_for_playback)

    @mock.patch("spitter.core.run_local_command")
    def test_probe_audio_output_status_reports_missing_backend(self, run_local_command: mock.Mock) -> None:
        run_local_command.side_effect = [
            mock.Mock(returncode=1, stdout="", stderr="wpctl unavailable\n"),
            mock.Mock(returncode=1, stdout="", stderr="pactl unavailable\n"),
        ]
        status = spitter.probe_audio_output_status()
        self.assertEqual(status.backend, "none")
        self.assertFalse(status.available)
        self.assertIn("wpctl unavailable", status.reason)
        self.assertIn("pactl unavailable", status.reason)

    @mock.patch("spitter.core.probe_audio_output_status")
    def test_enforce_audio_output_policy_refuses_muted_sink(
        self,
        probe_audio_output_status: mock.Mock,
    ) -> None:
        probe_audio_output_status.return_value = spitter.AudioOutputStatus(
            backend="wpctl",
            sink="alsa_output.test",
            volume=0.75,
            muted=True,
            available=True,
            reason="default sink is muted",
        )
        with self.assertRaises(spitter.SpitterError):
            spitter.enforce_audio_output_policy(play_requested=True, policy="enforce")

    @mock.patch("sys.stdin", new_callable=io.StringIO)
    def test_resolve_transcript_reads_stdin(self, stdin: io.StringIO) -> None:
        stdin.write("hello from stdin")
        stdin.seek(0)
        args = mock.Mock(text="-", stdin=False)
        self.assertEqual(spitter.resolve_transcript(args), "hello from stdin")

    def test_http_error_translation_includes_json_detail(self) -> None:
        settings = spitter.get_runtime_settings()
        client = spitter_core.CartesiaClient(settings, "test-token")
        error = HTTPError(
            url="https://api.cartesia.ai/voices",
            code=401,
            msg="Unauthorized",
            hdrs=Message(),
            fp=io.BytesIO(b'{"error":"bad token"}'),
        )
        with (
            mock.patch("spitter.core.urlopen", side_effect=error),
            self.assertRaises(spitter.SpitterError) as exc_info,
        ):
            client.list_voices(limit=1)
        self.assertIn("Cartesia API error 401 Unauthorized", str(exc_info.exception))
        self.assertIn("bad token", str(exc_info.exception))

    def test_network_error_translation_includes_url(self) -> None:
        settings = spitter.get_runtime_settings()
        client = spitter_core.CartesiaClient(settings, "test-token")
        with (
            mock.patch(
                "spitter.core.urlopen",
                side_effect=URLError("connection refused"),
            ),
            self.assertRaises(spitter.SpitterError) as exc_info,
        ):
            client.list_voices(limit=1)
        self.assertIn(
            "Network error calling https://api.cartesia.ai/voices?limit=1",
            str(exc_info.exception),
        )
        self.assertIn("connection refused", str(exc_info.exception))

    def test_execute_websocket_say_translates_websocket_error(self) -> None:
        args = mock.Mock(
            timestamps="off",
            output=None,
            play=False,
            session=None,
            session_policy="start",
            session_idle_timeout=90,
        )
        settings = spitter.get_runtime_settings()
        request_body = {
            "transcript": "hello",
            "context_id": "ctx-123",
            "model_id": "sonic-3",
        }
        voice = {"id": "voice-123", "name": "Test Voice", "language": "en"}
        output_format = {"encoding": "pcm_s16le", "sample_rate": 44100}
        with (
            mock.patch(
                "spitter.core.run_ephemeral_websocket_synthesis",
                side_effect=spitter_websocket.WebSocketError("upstream websocket failed"),
            ),
            self.assertRaises(spitter.SpitterError) as exc_info,
        ):
            spitter_core.execute_websocket_say(
                args=args,
                settings=settings,
                api_key="token",
                request_body=request_body,
                voice=voice,
                output_format=output_format,
            )
        self.assertEqual(str(exc_info.exception), "upstream websocket failed")

    def test_session_daemon_handles_lifecycle_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = spitter_websocket.SessionPaths(
                root=Path(directory),
                name="default",
                socket_path=Path(directory) / "default.sock",
                state_path=Path(directory) / "default.json",
                log_path=Path(directory) / "default.log",
            )
            with mock.patch("spitter.websocket.CartesiaWebSocketDispatcher") as dispatcher_cls:
                dispatcher = dispatcher_cls.return_value
                dispatcher.status.return_value = {"active_context_count": 0}
                daemon = spitter_websocket.SessionDaemon(
                    name="default",
                    paths=paths,
                    base_url="https://api.cartesia.ai",
                    api_key="token",
                    api_version="2026-03-01",
                    user_agent="spitter/test",
                    ffplay_path=None,
                    idle_timeout_seconds=90,
                )
            daemon.server = mock.Mock()
            status_response = daemon.handle_request({"action": "status"})
            shutdown_response = daemon.handle_request({"action": "shutdown"})
            invalid_response = daemon.handle_request({"action": "nope"})
        self.assertTrue(status_response["ok"])
        self.assertEqual(status_response["status"]["name"], "default")
        self.assertTrue(shutdown_response["ok"])
        self.assertTrue(daemon.shutdown_requested.is_set())
        self.assertFalse(invalid_response["ok"])
        self.assertIn("Unsupported session action", invalid_response["error"])

    def test_describe_outputs_json(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = spitter.main(["describe", "say"])
        self.assertEqual(exit_code, 0)
        output = buffer.getvalue()
        self.assertIn('"name": "spitter"', output)
        self.assertIn('"commands"', output)


if __name__ == "__main__":
    unittest.main()
