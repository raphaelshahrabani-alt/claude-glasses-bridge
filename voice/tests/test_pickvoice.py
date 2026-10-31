"""The voice picker, against a stand-in ElevenLabs and a stand-in speaker.

Run: python -m unittest discover -s voice/tests
"""
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

import pickvoice  # noqa: E402


class Response:
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self.data


def http_error(code, body):
    return urllib.error.HTTPError("https://api.elevenlabs.io/x", code, "err", {}, io.BytesIO(json.dumps(body).encode()))


class PickVoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.old = pickvoice.DATA_DIR
        pickvoice.DATA_DIR = self.dir
        self.saved_env = {k: os.environ.pop(k, None) for k in ("ELEVEN_API_KEY", "ELEVENLABS_API_KEY", "GLASSES_ELEVENLABS_VOICE", "GLASSES_ELEVENLABS_MODEL")}
        (self.dir / "voice.env").write_text("DEEPGRAM_API_KEY=dg\nELEVEN_API_KEY=sk_test\nGLASSES_WORK_EFFORT=high\n")
        self.requests = []
        self.played = []
        self.voices = [
            {"voice_id": "v_eric", "name": "Eric", "labels": {"gender": "male", "accent": "american", "descriptive": "classy"}},
            {"voice_id": "v_jess", "name": "Jessica", "description": "Playful, bright and warm\nyoung woman"},
        ]
        self.fail_samples = {}

    def tearDown(self):
        pickvoice.DATA_DIR = self.old
        for k, v in self.saved_env.items():
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def opener(self, request, timeout=0):
        self.requests.append((request.get_method(), request.full_url, request.get_header("Xi-api-key"),
                              json.loads(request.data) if request.data else None))
        if request.full_url.endswith("/v1/voices"):
            if self.voices is None:
                raise http_error(401, {"detail": {"status": "missing_permissions"}})
            return Response({"voices": self.voices})
        voice = request.full_url.split("/text-to-speech/")[1].split("?")[0]
        if voice in self.fail_samples:
            raise self.fail_samples[voice]
        return Response(b"ID3-" + voice.encode())

    def play(self, path):
        self.played.append(path.read_bytes())
        return True

    def run_main(self, answers, argv=()):
        answers = iter(answers)
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            code = pickvoice.main(list(argv), ask=lambda prompt: next(answers), opener=self.opener, play=self.play)
        finally:
            sys.stdout = real
        return code, out.getvalue()

    def env(self):
        return (self.dir / "voice.env").read_text()

    def test_hear_one_and_keep_it(self):
        code, out = self.run_main(["2", "y"])
        self.assertEqual(code, 0)
        self.assertIn("1. Eric", out)
        self.assertIn("Playful, bright and warm young woman", out)  # newline in the description flattened
        self.assertEqual(self.played, [b"ID3-v_jess"])
        self.assertIn("Saved Jessica", out)
        # the sample is made with the user's own key, and the model the glasses use
        method, url, key, body = self.requests[-1]
        self.assertEqual((method, key, body["model_id"]), ("POST", "sk_test", pickvoice.DEFAULT_MODEL))
        self.assertEqual(self.env(), "DEEPGRAM_API_KEY=dg\nELEVEN_API_KEY=sk_test\nGLASSES_WORK_EFFORT=high\nGLASSES_ELEVENLABS_VOICE=v_jess\n")
        self.assertEqual(oct((self.dir / "voice.env").stat().st_mode & 0o777), "0o600")

    def test_keep_looking_then_replace_an_earlier_choice(self):
        (self.dir / "voice.env").write_text("GLASSES_ELEVENLABS_VOICE=old_voice\nELEVEN_API_KEY=sk_test\n")
        code, out = self.run_main(["1", "n", "2", "r", "y"])
        self.assertEqual(code, 0)
        self.assertEqual(self.played, [b"ID3-v_eric", b"ID3-v_jess", b"ID3-v_jess"])  # "r" hears it again
        self.assertEqual(self.env(), "ELEVEN_API_KEY=sk_test\nGLASSES_ELEVENLABS_VOICE=v_jess\n")

    def test_quitting_changes_nothing(self):
        before = self.env()
        code, out = self.run_main(["q"])
        self.assertEqual(code, 0)
        self.assertEqual(self.env(), before)
        self.assertIn("Nothing changed", out)

    def test_bad_numbers_are_asked_again(self):
        code, out = self.run_main(["9", "banana", "q"])
        self.assertIn("Type a number from 1 to 2", out)
        self.assertEqual(self.played, [])

    def test_a_voice_that_cannot_be_made_is_skipped(self):
        self.fail_samples["v_eric"] = http_error(402, {"detail": {"message": "Free users cannot use library voices via the API"}})
        code, out = self.run_main(["1", "2", "y"])
        self.assertIn("Couldn't make a sample of Eric: needs a paid ElevenLabs plan", out)
        self.assertEqual(code, 0)
        self.assertIn("GLASSES_ELEVENLABS_VOICE=v_jess", self.env())

    def test_falls_back_to_elevenlabs_own_voices_when_the_key_cannot_list(self):
        self.voices = None
        code, out = self.run_main(["1", "n", "q"])
        self.assertIn("ElevenLabs' conversational voices", out)
        self.assertIn("Eric  (current): smooth, trustworthy man", out)
        self.assertIn("Jessica: playful", out)
        self.assertEqual(self.played, [b"ID3-cjVigY5qzO86Huf0OWal"])

    def test_the_current_voice_is_marked(self):
        os.environ["GLASSES_ELEVENLABS_VOICE"] = "v_jess"
        code, out = self.run_main(["q"])
        self.assertIn("Jessica  (current)", out)
        self.assertNotIn("Eric  (current)", out)

    def test_a_voice_by_id(self):
        code, out = self.run_main(["y"], argv=["lib_voice_123"])
        self.assertEqual(code, 0)
        self.assertEqual(self.played, [b"ID3-lib_voice_123"])
        self.assertIn("GLASSES_ELEVENLABS_VOICE=lib_voice_123", self.env())
        self.assertFalse(any(r[1].endswith("/v1/voices") for r in self.requests))  # no list needed
        code, out = self.run_main(["n"], argv=["other"])
        self.assertIn("GLASSES_ELEVENLABS_VOICE=lib_voice_123", self.env())

    def test_a_voice_by_id_that_does_not_exist_changes_nothing(self):
        self.fail_samples["nope"] = http_error(404, {"detail": {"message": "A voice with that id does not exist"}})
        code, out = self.run_main([], argv=["nope"])
        self.assertEqual(code, 1)
        self.assertIn("does not exist", out)
        self.assertNotIn("GLASSES_ELEVENLABS_VOICE", self.env())

    def test_no_key(self):
        (self.dir / "voice.env").write_text("GLASSES_WORK_EFFORT=high\n")
        code, out = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn("glasses-key ELEVEN_API_KEY", out)

    def test_the_worker_uses_the_saved_voice(self):
        # voice.env is loaded by bin/glasses-voice into GLASSES_ELEVENLABS_VOICE, which worker.make_tts reads
        text = (HERE.parent / "worker.py").read_text()
        self.assertIn('os.environ.get("GLASSES_ELEVENLABS_VOICE"', text)


if __name__ == "__main__":
    unittest.main()
