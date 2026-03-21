from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import spitter


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

    def test_load_api_key_reads_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token.txt"
            token_path.write_text("from-file\n", encoding="utf-8")
            os.environ["SPITTER_TOKEN_FILE"] = str(token_path)
            settings = spitter.get_runtime_settings()
            self.assertEqual(spitter.load_api_key(settings), "from-file")

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

    def test_websocket_output_rejects_wav(self) -> None:
        with self.assertRaises(spitter.SpitterError):
            spitter.build_output_format(
                transport="websocket",
                container="wav",
                encoding="pcm_s16le",
                sample_rate=44100,
                bit_rate=128000,
            )

    @mock.patch("spitter.run_local_command")
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

    @mock.patch("spitter.probe_audio_output_status")
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

    def test_describe_outputs_json(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = spitter.main(["describe", "say"])
        self.assertEqual(exit_code, 0)
        output = buffer.getvalue()
        self.assertIn("\"name\": \"spitter\"", output)
        self.assertIn("\"commands\"", output)


if __name__ == "__main__":
    unittest.main()
