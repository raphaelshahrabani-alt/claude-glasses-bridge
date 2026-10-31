"""The ElevenLabs key checker, and the backup voice that speaks when ElevenLabs refuses.

ElevenLabs itself isn't reachable from the test machine, so both run against stand-ins.
Run it with the voice worker's Python (it needs livekit-agents).
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import voicecheck  # noqa: E402

try:
    from livekit.agents import APIConnectOptions, tts as lk_tts
    from livekit.agents.types import APIConnectOptions as _  # noqa: F401
    from livekit.agents._exceptions import APIStatusError
    import worker
except ImportError:  # pragma: no cover
    worker = None


def http_error(code, body):
    return urllib.error.HTTPError("https://api.elevenlabs.io/x", code, "err", {}, io.BytesIO(json.dumps(body).encode()))


class Response:
    def __init__(self, data=b"ID3audio"):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self.data[:n]


def opener_raising(error):
    def opener(request, timeout=0):
        raise error
    return opener


class VoiceCheckTest(unittest.TestCase):
    def test_a_working_key(self):
        sent = {}

        def opener(request, timeout=0):
            sent["key"] = request.get_header("Xi-api-key")
            sent["url"] = request.full_url
            return Response()

        self.assertEqual(voicecheck.check("sk_good", "voice123", "eleven_turbo_v2_5", opener), {"ok": True})
        self.assertEqual(sent["key"], "sk_good")
        self.assertIn("/text-to-speech/voice123", sent["url"])

    def test_each_refusal_is_explained(self):
        cases = {
            "invalid_api_key": "isn't valid",
            "missing_permissions": "isn't allowed to make speech",
            "quota_exceeded": "out of credits",
            "detected_unusual_activity": "blocked its free plan",
            "voice_not_found": "voice isn't available",
        }
        for status, words in cases.items():
            error = http_error(401 if status == "detected_unusual_activity" else 400,
                               {"detail": {"status": status, "message": "details"}})
            result = voicecheck.check("k", opener=opener_raising(error))
            self.assertFalse(result["ok"], status)
            self.assertIn(words, result["problem"], status)
            self.assertTrue(result["fix"])

    def test_a_401_with_no_reason_means_the_key_is_wrong(self):
        result = voicecheck.check("k", opener=opener_raising(http_error(401, {"detail": "nope"})))
        self.assertEqual(result["status"], "invalid_api_key")

    def test_no_internet(self):
        result = voicecheck.check("k", opener=opener_raising(urllib.error.URLError("offline")))
        self.assertEqual(result["status"], "network")

    def test_key_is_read_from_voice_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "voice.env").write_text("GLASSES_MODEL=x\nexport ELEVEN_API_KEY='sk_abc1234'\n")
            old = voicecheck.DATA_DIR
            voicecheck.DATA_DIR = Path(tmp)
            try:
                self.assertEqual(voicecheck.key_from_env_file(), "sk_abc1234")
            finally:
                voicecheck.DATA_DIR = old

    def test_no_key_is_reported_without_calling_elevenlabs(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, env = voicecheck.DATA_DIR, {k: os.environ.pop(k, None) for k in ("ELEVEN_API_KEY", "ELEVENLABS_API_KEY")}
            voicecheck.DATA_DIR = Path(tmp)
            try:
                self.assertEqual(voicecheck.main([]), 1)
            finally:
                voicecheck.DATA_DIR = old
                for k, v in env.items():
                    if v is not None:
                        os.environ[k] = v


if worker is not None:

    class FakeStream(lk_tts.ChunkedStream):
        async def _run(self, output_emitter):
            if self._tts.fail:
                raise APIStatusError("ElevenLabs websocket connection closed unexpectedly", status_code=1008, retryable=False)
            output_emitter.initialize(request_id="r", sample_rate=24000, num_channels=1, mime_type="audio/pcm")
            output_emitter.push(b"\x01\x00" * 2400)
            output_emitter.flush()

    class FakeTTS(lk_tts.TTS):
        def __init__(self, name, fail=False):
            super().__init__(capabilities=lk_tts.TTSCapabilities(streaming=False), sample_rate=24000, num_channels=1)
            self.name, self.fail, self.calls = name, fail, 0

        @property
        def provider(self):
            return self.name

        def synthesize(self, text, *, conn_options=None):
            self.calls += 1
            return FakeStream(tts=self, input_text=text, conn_options=conn_options or APIConnectOptions(max_retry=0))


@unittest.skipIf(worker is None, "livekit-agents is not installed")
class BackupVoiceTest(unittest.IsolatedAsyncioTestCase):
    async def speak(self, adapter):
        frames = 0
        async for audio in adapter.synthesize("Hello there."):
            frames += 1
        return frames

    async def test_the_backup_speaks_when_the_first_voice_is_refused(self):
        primary, backup = FakeTTS("elevenlabs", fail=True), FakeTTS("backup")
        adapter = lk_tts.FallbackAdapter([primary, backup], max_retry_per_tts=0)
        self.assertGreater(await self.speak(adapter), 0)
        self.assertGreaterEqual(primary.calls, 1)  # tried first (and re-probed later to see if it recovered)
        self.assertEqual(backup.calls, 1)  # and the backup spoke

    async def test_the_first_voice_is_used_while_it_works(self):
        primary, backup = FakeTTS("elevenlabs"), FakeTTS("backup")
        adapter = lk_tts.FallbackAdapter([primary, backup], max_retry_per_tts=0)
        self.assertGreater(await self.speak(adapter), 0)
        self.assertEqual((primary.calls, backup.calls), (1, 0))

    def test_make_tts_wires_a_backup_behind_elevenlabs(self):
        keys = ["ELEVEN_API_KEY", "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL"]
        saved = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys[:3]:
                os.environ.pop(k, None)
            os.environ.update({"LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s" * 40, "LIVEKIT_URL": "wss://x.livekit.cloud"})
            self.assertEqual(type(worker.make_tts()).__name__, "TTS")  # no key: just the usual voice
            os.environ["ELEVEN_API_KEY"] = "sk_test"
            adapter = worker.make_tts()
            self.assertEqual(type(adapter).__name__, "FallbackAdapter")
            self.assertEqual([type(t).__module__.split(".")[-2] for t in adapter._tts_instances], ["elevenlabs", "inference"])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
