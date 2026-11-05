"""The PC (Windows) side: settings files, keys, the launcher, the doctor, setup and the glasses command.

Everything runs here with stand-ins for Windows programs (winget, npm, tailscale); the Windows-only
calls are small and are covered by checking the commands they would run.
Run: python -m unittest discover -s voice/tests
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import envfile  # noqa: E402
import glasses_cli  # noqa: E402
import keys  # noqa: E402
import launcher as launcher_mod  # noqa: E402
import pcdoctor  # noqa: E402
import setup_pc  # noqa: E402
from launcher import Launcher, missing_setup, venv_python, worker_env  # noqa: E402


FAKE_KEY = "APIkey1234567"
FAKE_SECRET = "s3cr3t" * 7 + "ab"  # 44 characters, like a real one


def make_install(root: Path, livekit=True, node_modules=True, venv=True):
    """A data folder and a VisionClaw folder that look set up."""
    data, vc = root / "data", root / "VisionClaw"
    (vc / "gateway").mkdir(parents=True)
    if node_modules:
        (vc / "gateway" / "node_modules").mkdir()
    if livekit:
        (vc / "gateway" / ".env").write_text(f"PORT=8788\nLIVEKIT_URL=wss://x.livekit.cloud\nLIVEKIT_API_KEY={FAKE_KEY}\n"
                                              f"LIVEKIT_API_SECRET={FAKE_SECRET}\nGATEWAY_TOKENS=abc123:me\n")
    if venv:
        py = venv_python(data)
        py.parent.mkdir(parents=True)
        py.write_text("")
    data.mkdir(exist_ok=True)
    return data, vc


class EnvFileTest(unittest.TestCase):
    def test_parse(self):
        text = "# comment\nA=1\nexport B='two words'\nC=\"3\"\n\nbad line\n1X=nope\nD = spaced \nE=a=b\n"
        self.assertEqual(envfile.parse(text), {"A": "1", "B": "two words", "C": "3", "D": "spaced", "E": "a=b"})

    def test_set_value_keeps_other_lines_and_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.env"
            path.write_text("A=1\nexport KEY=old\n# note\n")
            envfile.set_value(path, "KEY", "new")
            self.assertEqual(path.read_text(), "A=1\n# note\nKEY=new\n")
            envfile.set_value(Path(tmp) / "sub" / "fresh.env", "X", "y")
            self.assertEqual((Path(tmp) / "sub" / "fresh.env").read_text(), "X=y\n")


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.real = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self.out, self.err

    def tearDown(self):
        sys.stdout, sys.stderr = self.real
        self.tmp.cleanup()

    def save(self, name, value, **checks):
        return keys.save(name, ask=lambda prompt: value, data_dir=self.dir, **checks)

    def env(self):
        return (self.dir / "voice.env").read_text() if (self.dir / "voice.env").exists() else None

    def test_a_good_elevenlabs_key_is_kept(self):
        (self.dir / "voice.env").write_text("GLASSES_WORK_EFFORT=high\n")
        code = self.save("ELEVEN_API_KEY", "  sk_new_value 1234567890 \n", check_eleven=lambda k: {"ok": True})
        self.assertEqual(code, 0)
        self.assertEqual(self.env(), "GLASSES_WORK_EFFORT=high\nELEVEN_API_KEY=sk_new_value1234567890\n")
        self.assertIn("ElevenLabs accepts this key", self.out.getvalue())
        self.assertNotIn("sk_new_value1234567890", self.out.getvalue())  # only the last four are shown

    def test_an_invalid_key_is_not_kept_and_the_old_one_stays(self):
        (self.dir / "voice.env").write_text("ELEVEN_API_KEY=old_key\n")
        code = self.save("ELEVEN_API_KEY", "typo-typo-typo-typo-typo", check_eleven=lambda k: {"ok": False, "status": "invalid_api_key"})
        self.assertEqual(code, 1)
        self.assertEqual(self.env(), "ELEVEN_API_KEY=old_key\n")
        self.assertIn("did not keep it", self.err.getvalue())

    def test_an_invalid_key_with_no_earlier_file_leaves_no_file(self):
        code = self.save("DEEPGRAM_API_KEY", "typo-typo-typo-typo-typo", check_deepgram=lambda k: {"ok": False, "status": "invalid_api_key"})
        self.assertEqual(code, 1)
        self.assertIsNone(self.env())
        self.assertIn("console.deepgram.com", self.err.getvalue())

    def test_a_key_accepted_with_a_note_shows_the_note(self):
        code = self.save("ELEVEN_API_KEY", "key-0123456789abcdef", check_eleven=lambda k: {"ok": True, "note": "the test speech was refused (400)"})
        self.assertEqual(code, 0)
        self.assertIn("ElevenLabs accepts this key", self.out.getvalue())
        self.assertIn("(the test speech was refused (400))", self.out.getvalue())

    def test_an_unrecognised_reply_is_not_called_fine(self):
        code = self.save("ELEVEN_API_KEY", "key-0123456789abcdef", check_eleven=lambda k: {
            "ok": False, "status": "unchecked", "problem": "ElevenLabs answered unexpectedly: 400", "fix": "x"})
        self.assertEqual(code, 0)
        self.assertIn("Couldn't check it with ElevenLabs", self.out.getvalue())
        self.assertNotIn("itself is fine", self.err.getvalue())

    def test_an_account_problem_keeps_the_key_and_says_it(self):
        code = self.save("ELEVEN_API_KEY", "key-0123456789abcdef", check_eleven=lambda k: {
            "ok": False, "status": "quota_exceeded", "problem": "the ElevenLabs account is out of credits", "fix": "upgrade"})
        self.assertEqual(code, 0)
        self.assertEqual(self.env(), "ELEVEN_API_KEY=key-0123456789abcdef\n")
        self.assertIn("The key itself is fine, but: the ElevenLabs account is out of credits", self.err.getvalue())

    def test_no_internet_keeps_the_key_without_claiming_it_works(self):
        code = self.save("DEEPGRAM_API_KEY", "key-0123456789abcdef", check_deepgram=lambda k: {"ok": False, "status": "network", "problem": "offline"})
        self.assertEqual(code, 0)
        self.assertIn("Couldn't check it with Deepgram", self.out.getvalue())
        self.assertNotIn("itself is fine", self.err.getvalue())

    def test_an_invalid_key_shows_what_elevenlabs_said_and_the_shape_of_what_was_pasted(self):
        masked = "sk_ac7f1234abcd...b5bf5678"  # the shortened form some key lists show
        code = self.save("ELEVEN_API_KEY", masked, check_eleven=lambda k: {
            "ok": False, "status": "invalid_api_key", "message": "Invalid API key"})
        self.assertEqual(code, 1)
        self.assertIn(f"is {len(masked)} characters and starts with 'sk_'", self.out.getvalue())
        self.assertIn("Copy button", self.out.getvalue())
        self.assertIn("ElevenLabs said: Invalid API key", self.err.getvalue())
        self.assertEqual(keys.key_shape_note("ELEVEN_API_KEY", "sk_" + "ab01" * 12), None)  # the right shape: no note
        self.assertIsNone(keys.key_shape_note("DEEPGRAM_API_KEY", "short"))  # only ElevenLabs' shape is known

    def test_a_stray_ctrl_v_does_not_spoil_the_key(self):
        # what getpass stored on Windows: the key followed by the Ctrl+V character, shown as a flat bar
        key = "sk_" + "0123456789abcdef" * 3
        code = self.save("ELEVEN_API_KEY", key + "\x16", check_eleven=lambda k: {"ok": True} if k == key else {
            "ok": False, "status": "invalid_api_key"})
        self.assertEqual(code, 0)
        self.assertEqual(self.env(), f"ELEVEN_API_KEY={key}\n")

    def test_a_paste_that_went_wrong_is_not_saved_as_a_key(self):
        (self.dir / "voice.env").write_text("ELEVEN_API_KEY=" + "x" * 40 + "\n")
        for junk in ("=", "-", "abc", "sk_short"):
            self.assertEqual(self.save("ELEVEN_API_KEY", junk), 1, junk)
        self.assertEqual(self.save("ELEVEN_API_KEY", "\x16"), 1)  # only the Ctrl+V character: nothing was pasted
        self.assertEqual(self.env(), "ELEVEN_API_KEY=" + "x" * 40 + "\n")  # the good key is still there
        self.assertIn("doesn't look like a key", self.err.getvalue())
        self.assertEqual(self.save("GLASSES_THING", "short"), 0)  # only keys are held to this

    def test_other_keys_and_empty_pastes(self):
        self.assertEqual(self.save("GLASSES_THING", "v"), 0)
        self.assertEqual(self.env(), "GLASSES_THING=v\n")
        self.assertEqual(self.save("ELEVEN_API_KEY", "   "), 1)
        self.assertEqual(self.save("bad name", "v"), 1)


class ReadSecretTest(unittest.TestCase):
    def read(self, typed, clipboard=""):
        keys_iter = iter(typed)
        shown = []
        value = keys.read_secret("Paste: ", getch=lambda: next(keys_iter), clipboard=lambda: clipboard, write=shown.append)
        return value, "".join(shown)

    def test_typed_and_right_click_pasted_characters(self):
        value, shown = self.read(list("abc123") + ["\r"])
        self.assertEqual(value, "abc123")
        self.assertEqual(shown, "Paste: \n")  # nothing of the key is echoed

    def test_ctrl_v_pastes_the_clipboard(self):
        self.assertEqual(self.read(["\x16", "\r"], clipboard="sk_from_clipboard")[0], "sk_from_clipboard")
        # even when it is pressed twice or after something was typed
        self.assertEqual(self.read(["a", "\x16", "\x16", "\r"], clipboard="XY")[0], "aXYXY")

    def test_backspace_arrow_keys_and_ctrl_c(self):
        self.assertEqual(self.read(["a", "b", "\b", "c", "\r"])[0], "ac")
        self.assertEqual(self.read(["a", "\xe0", "H", "b", "\r"])[0], "ab")  # an arrow key is two characters; both ignored
        with self.assertRaises(KeyboardInterrupt):
            self.read(["a", "\x03"])

    def test_a_line_break_before_anything_is_ignored_not_taken_for_an_empty_answer(self):
        # a copied selection that starts with a line break, or Enter pressed before pasting
        self.assertEqual(self.read(["\r", "\n", "a", "b", "\r"])[0], "ab")

    def test_it_records_which_way_a_paste_failed(self):
        self.read(["\x16", "a", "\r"], clipboard="")  # Ctrl+V gave nothing; one letter was typed
        self.assertEqual(keys.last_read, {"typed": 1, "ctrl_v": 1, "pasted": 0})
        self.assertIn("Ctrl+V was pressed 1 time and gave 0 characters", keys.what_arrived())
        self.read(list("abc") + ["\r"])
        self.assertEqual(keys.last_read, {"typed": 3, "ctrl_v": 0, "pasted": 0})

    def test_the_clipboard_falls_back_to_powershell_when_reading_it_directly_fails(self):
        real = keys._clipboard_ctypes, keys._clipboard_powershell
        try:
            keys._clipboard_ctypes = lambda: ""
            keys._clipboard_powershell = lambda: "abcd efgh ijkl mnop\r\n"
            self.assertEqual(keys.clean(keys.windows_clipboard()), "abcdefghijklmnop")
            keys._clipboard_ctypes = lambda: "from-direct-read"
            self.assertEqual(keys.windows_clipboard(), "from-direct-read")
        finally:
            keys._clipboard_ctypes, keys._clipboard_powershell = real

    def test_line_breaks_in_a_pasted_clipboard_do_not_end_up_in_the_key(self):
        self.assertEqual(keys.clean("sk_abc\r\n  def\x16"), "sk_abcdef")


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.said = []
        self.started = []

    def tearDown(self):
        self.tmp.cleanup()

    def launcher(self, **kw):
        data, vc = make_install(self.root, **kw)
        gateway_up = {"up": False}
        self.gateway_up = gateway_up

        def spawn(cmd, cwd, env, log):
            self.started.append((cmd, Path(cwd), dict(env), log))
            if cmd[-2:] == ["worker.py", "start"] or cmd[-1] == "start" and "worker.py" in cmd[-2]:
                log.parent.mkdir(parents=True, exist_ok=True)
                with open(log, "ab") as f:
                    f.write(b'{"message": "registered worker"}\n')
            else:
                gateway_up["up"] = True
            return os.getpid()  # a pid that is certainly alive

        return Launcher(data_dir=data, visionclaw=vc, root=HERE.parent.parent, base_env={"PATH": "x", "KEEP": "env"},
                        say=self.said.append, spawn=spawn, port_open=lambda p: gateway_up["up"], sleep=lambda s: None)

    def test_worker_env(self):
        data, vc = make_install(self.root)
        (data / "voice.env").write_text("ELEVEN_API_KEY=ek\nGLASSES_WORK_EFFORT=low\n")
        env = worker_env(data, vc, {"GLASSES_WORK_EFFORT": "high", "PATH": "p"}, "C:/proj", ["C:/a", "C:/b"])
        self.assertEqual(env["ELEVEN_API_KEY"], "ek")
        self.assertEqual(env["GLASSES_WORK_EFFORT"], "high")  # what's already set wins
        self.assertEqual((env["LIVEKIT_URL"], env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"]),
                         ("wss://x.livekit.cloud", FAKE_KEY, FAKE_SECRET))
        self.assertEqual(env["GLASSES_PROJECT"], "C:/proj")
        self.assertEqual(env["GLASSES_ADD_DIRS"], os.pathsep.join(["C:/a", "C:/b"]))
        self.assertEqual((env["PYTHONUTF8"], env["GLASSES_DATA_DIR"]), ("1", str(data)))
        self.assertNotIn("GATEWAY_TOKENS", env)  # the access code stays in the login service

    def test_missing_setup_says_what_is_missing(self):
        data, vc = make_install(self.root, livekit=False, node_modules=False, venv=False)
        problems = " | ".join(missing_setup(data, vc))
        for part in ("isn't installed yet", "login service isn't installed", "missing LIVEKIT_URL"):
            self.assertIn(part, problems)
        self.assertEqual(missing_setup(*make_install(self.root / "ok")), [])

    def test_start_runs_the_login_service_then_the_voice(self):
        launcher = self.launcher()
        self.assertEqual(launcher.start("C:/proj", ["C:/extra"]), 0)
        gateway, voice = self.started
        self.assertEqual(gateway[0][-1], "start")
        self.assertEqual(gateway[1], launcher.visionclaw / "gateway")
        self.assertEqual(voice[0][0], str(venv_python(launcher.data_dir)))
        self.assertTrue(voice[0][1].endswith("worker.py"))
        self.assertEqual(voice[2]["GLASSES_PROJECT"], "C:/proj")
        self.assertEqual(voice[2]["KEEP"], "env")
        self.assertIn("Ready.", self.said[-1])
        # and what it did is remembered for next time
        self.assertEqual(json.loads((launcher.data_dir / "launch.json").read_text()), {"project": "C:/proj", "extra_dirs": ["C:/extra"]})

    def test_start_again_leaves_running_things_alone_and_reuses_the_folders(self):
        launcher = self.launcher()
        launcher.start("C:/proj", ["C:/extra"])
        self.started.clear()
        self.assertEqual(launcher.start(), 0)
        self.assertEqual(self.started, [])
        self.assertIn("login service: already running", self.said)
        self.assertIn("voice: already running", self.said)

    def test_a_dead_voice_program_is_restarted(self):
        launcher = self.launcher()
        launcher.start("C:/proj")
        launcher.pid_file("voice").write_text(json.dumps({"pid": 2 ** 22 + 12345, "started": 0}))  # not running
        self.started.clear()
        self.assertEqual(launcher.start(), 0)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0][3], launcher.log("voice"))
        self.assertEqual(self.started[0][2]["GLASSES_PROJECT"], "C:/proj")  # the folder it had before

    def test_start_says_when_setup_is_missing_or_the_voice_never_comes_up(self):
        data, vc = make_install(self.root / "none", livekit=False)
        launcher = Launcher(data_dir=data, visionclaw=vc, say=self.said.append)
        self.assertEqual(launcher.start(), 1)
        self.assertIn("  Run: glasses setup", self.said)
        # the voice program starts but never says it registered
        launcher = self.launcher()
        launcher.spawn = lambda cmd, cwd, env, log: os.getpid()
        launcher.port_open = lambda p: True
        self.said.clear()
        launcher.log("voice").parent.mkdir(parents=True, exist_ok=True)
        launcher.log("voice").write_text("starting\nTraceback (most recent call last):\nImportError: no module named x\n")
        self.assertEqual(launcher.start("C:/proj", wait_s=0.2), 1)
        text = "\n".join(self.said)
        self.assertIn("didn't finish starting", text)
        self.assertIn("    ImportError: no module named x", text)  # the cause is shown, not just where to look

    def test_a_one_character_livekit_secret_is_caught_before_starting(self):
        data, vc = make_install(self.root / "bad")
        (vc / "gateway" / ".env").write_text("LIVEKIT_URL=wss://x.livekit.cloud\nLIVEKIT_API_KEY=" + FAKE_KEY +
                                             "\nLIVEKIT_API_SECRET=\x16\nGATEWAY_TOKENS=abc:me\n")
        launcher = Launcher(data_dir=data, visionclaw=vc, say=self.said.append)
        self.assertEqual(launcher.start(), 1)
        text = "\n".join(self.said)
        self.assertIn("the LiveKit secret is only 1 character, so LiveKit will refuse it", text)
        self.assertIn("Run: glasses setup", text)

    def test_livekit_problems(self):
        good = {"LIVEKIT_URL": "wss://x.livekit.cloud", "LIVEKIT_API_KEY": FAKE_KEY, "LIVEKIT_API_SECRET": FAKE_SECRET}
        self.assertEqual(launcher_mod.livekit_problems(good), [])
        self.assertIn("missing LIVEKIT_API_SECRET", " ".join(launcher_mod.livekit_problems({**good, "LIVEKIT_API_SECRET": ""})))
        self.assertIn("missing LIVEKIT_URL", " ".join(launcher_mod.livekit_problems({**good, "LIVEKIT_URL": "paste-here"})))
        short = " ".join(launcher_mod.livekit_problems({**good, "LIVEKIT_API_SECRET": "abc"}))
        self.assertIn("only 3 characters", short)
        self.assertIn("should start with wss://", " ".join(launcher_mod.livekit_problems({**good, "LIVEKIT_URL": "https://x"})))

    def test_diagnose_voice_log(self):
        refused = "livekit.agents ERROR failed to connect to livekit, retrying in 2s: 401 Unauthorized"
        self.assertIn("LiveKit refused the keys (error 401)", launcher_mod.diagnose_voice_log(refused))
        self.assertIn("Run: glasses livekit", launcher_mod.diagnose_voice_log(refused))
        self.assertIn("can't reach LiveKit", launcher_mod.diagnose_voice_log("failed to connect to livekit: timed out"))
        self.assertIsNone(launcher_mod.diagnose_voice_log("registered worker"))
        self.assertIsNone(launcher_mod.diagnose_voice_log("HTTP 401 from some other service"))

    def test_a_refused_start_says_why_and_how_to_fix_it(self):
        launcher = self.launcher()
        launcher.spawn = lambda cmd, cwd, env, log: (
            log.parent.mkdir(parents=True, exist_ok=True),
            log.write_text("failed to connect to livekit, retrying: 401 Unauthorized\nThe HMAC key is 1 bytes long\n"),
            os.getpid())[-1]
        launcher.port_open = lambda p: True
        self.assertEqual(launcher.start("C:/proj", wait_s=0.2), 1)
        text = "\n".join(self.said)
        self.assertIn("didn't finish starting", text)
        self.assertIn("→ LiveKit refused the keys (error 401)", text)

    def test_quiet_start_says_nothing_when_all_is_well(self):
        launcher = self.launcher()
        self.assertEqual(launcher.start("C:/proj", quiet=True), 0)
        self.assertEqual(self.said, [])

    def test_stop_ends_what_was_started(self):
        data, vc = make_install(self.root)
        launcher = Launcher(data_dir=data, visionclaw=vc, say=self.said.append, port_open=lambda p: False)
        pid = launcher_mod.spawn([sys.executable, "-c", "import time; time.sleep(60)"], self.root, os.environ,
                                 data / "sleeper.log")
        launcher.remember("voice", pid)
        self.assertTrue(launcher_mod.pid_alive(pid))
        self.assertEqual(launcher.stop(), 0)
        end = time.monotonic() + 5
        while launcher_mod.pid_alive(pid) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertFalse(launcher_mod.pid_alive(pid))
        self.assertFalse(launcher.pid_file("voice").exists())
        self.assertIn("Stopped: voice", self.said)
        self.said.clear()
        launcher.stop()
        self.assertEqual(self.said, ["Nothing was running."])


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data, self.vc = make_install(self.root)
        self.launcher = Launcher(data_dir=self.data, visionclaw=self.vc, root=HERE.parent.parent, say=lambda s: None,
                                 port_open=lambda p: True)
        self.real_which = pcdoctor.shutil.which
        pcdoctor.shutil.which = lambda name: r"C:\Users\me\.local\bin\claude.exe" if name == "claude" else None

    def tearDown(self):
        pcdoctor.shutil.which = self.real_which
        self.tmp.cleanup()

    def run_check(self, **kw):
        kw.setdefault("gateway_up", lambda: True)
        kw.setdefault("tailscale", {"installed": True, "state": "Running", "dns": "pc.tail1234.ts.net",
                                     "https": "https://pc.tail1234.ts.net", "serving": True})
        kw.setdefault("reachable", lambda url: True)
        return pcdoctor.check(self.launcher, **kw).render()

    def test_everything_fine(self):
        self.launcher.remember("voice", os.getpid())
        (self.data / "persona.md").write_text("# me\n")
        (self.data / "email-accounts.json").write_text(json.dumps([{"address": "a@gmail.com"}, {"address": "b@icloud.com"}]))
        (self.data / "voice.env").write_text("ELEVEN_API_KEY=sk_secret_value\nDEEPGRAM_API_KEY=dg_secret_value\n")
        (self.data / "voice.log").write_text("\n".join([
            "2026-10-31 14:00:01,000 [10] call started in room room-1",
            "2026-10-31 14:00:03,000 [10] quick voice: first words after 1.0s",
            "2026-10-31 14:00:04,100 [10] line 1: first words after 4.2s",
            "2026-10-31 14:00:09,300 [10] line 1: done after 7.4s, 2 tool call(s)",
        ]))
        out = self.run_check(quick=True)
        for line in ("✓ VisionClaw's login service is running", "✓ the voice program is running the current code",
                     "✓ the LiveKit keys are in VisionClaw's settings",
                     "✓ the voice is ElevenLabs (run glasses doctor without --quick to test the key)",
                     "✓ speech-to-text is Deepgram (run glasses doctor without --quick to test the key)",
                     "✓ email accounts: a@gmail.com, b@icloud.com",
                     "✓ the phone can reach this PC at https://pc.tail1234.ts.net",
                     "✓ the quick voice answered after 1.0s (median of the last 1)",
                     "✓ the work model started talking after 4.2s (median of the last 1)",
                     "work requests were done after 7.4s on median; 1 of 1 used tools",
                     "Everything is connected."):
            self.assertIn(line, out)
        self.assertNotIn("secret_value", out)

    def test_keys_are_tested_and_refusals_explained(self):
        self.launcher.remember("voice", os.getpid())
        (self.data / "voice.env").write_text("ELEVEN_API_KEY=sk_secret_value\nDEEPGRAM_API_KEY=dg_secret_value\n")
        out = self.run_check(
            elevenlabs=lambda k: {"ok": False, "problem": "ElevenLabs blocked its free plan", "fix": "a paid plan lifts it"},
            deepgram=lambda k: {"ok": True}, run_selftest=lambda: [])
        self.assertIn("✗ the ElevenLabs voice can't speak: ElevenLabs blocked its free plan; the backup voice speaks instead", out)
        self.assertIn("→ a paid plan lifts it", out)
        self.assertIn("✓ speech-to-text is Deepgram, and its key (ending alue) works", out)
        self.assertNotIn("secret_value", out)

    def test_problems_get_a_fix(self):
        out = self.run_check(gateway_up=lambda: False, quick=True,
                             tailscale={"installed": True, "state": "NeedsLogin"})
        self.assertIn("✗ VisionClaw's login service isn't answering", out)
        self.assertIn("→ run: glasses start", out)
        self.assertIn("✗ the voice program isn't running", out)
        self.assertIn("✗ Tailscale isn't signed in (it says NeedsLogin)", out)
        self.assertRegex(out, r"\d+ problems? found\.")

    def test_a_short_livekit_secret_is_flagged(self):
        self.launcher.remember("voice", os.getpid())
        (self.vc / "gateway" / ".env").write_text(f"LIVEKIT_URL=wss://x.livekit.cloud\nLIVEKIT_API_KEY={FAKE_KEY}\n"
                                                   "LIVEKIT_API_SECRET=x\nGATEWAY_TOKENS=abc:me\n")
        out = self.run_check(quick=True)
        self.assertIn("✗ the LiveKit secret is only 1 character", out)
        self.assertIn("→ run: glasses livekit", out)
        self.assertNotIn("✓ the LiveKit keys are in VisionClaw's settings", out)

    def test_a_voice_program_that_failed_to_start_with_401_says_so(self):
        self.launcher.log("voice").write_text("failed to connect to livekit, retrying: 401 Unauthorized\n")
        out = self.run_check(quick=True)
        self.assertIn("✗ the voice program's last start failed: LiveKit refused the keys (error 401)", out)
        self.assertIn("run: glasses livekit", out)

    def test_a_stale_voice_program(self):
        self.launcher.pid_file("voice").parent.mkdir(parents=True, exist_ok=True)
        self.launcher.pid_file("voice").write_text(json.dumps({"pid": os.getpid(), "started": 1.0}))
        self.assertIn("✗ the voice program is still running code from before your last update", self.run_check(quick=True))

    def test_phone_checks(self):
        report = pcdoctor.Report()
        pcdoctor.check_phone(report, {"installed": False})
        pcdoctor.check_phone(report, {"installed": True, "state": "Running", "https": "https://pc.ts.net", "serving": False})
        pcdoctor.check_phone(report, {"installed": True, "state": "Running", "https": "https://pc.ts.net", "serving": True},
                             reachable=lambda url: False)
        text = report.render()
        self.assertIn("Tailscale isn't installed", text)
        self.assertIn("tailscale serve --bg 8788", text)
        self.assertIn("Tailscale is sharing https://pc.ts.net, but it doesn't answer", text)

    def test_why_the_phone_address_does_not_answer(self):
        import socket
        import ssl
        import urllib.error

        def failing(error):
            def opener(url, timeout):
                raise error
            return opener

        def answer(status):
            class Response:
                def __init__(self):
                    self.status = status

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return lambda url, timeout: Response()

        why = pcdoctor.why_unreachable
        self.assertIsNone(why("https://pc.ts.net/health", opener=answer(200)))
        self.assertIn("answered with error 502", why("https://pc.ts.net/health", opener=answer(502))[0])
        slow = why("https://pc.ts.net/health", timeout=30, opener=failing(urllib.error.URLError(socket.timeout("timed out"))))
        self.assertEqual(slow[0], "it didn't answer within 30 seconds")
        self.assertIn("Tailscale gets a security certificate", slow[1])  # says what the wait is for
        generic = why("https://pc.ts.net/health", opener=failing(urllib.error.URLError(ssl.SSLError(1, "TLSV1_ALERT_INTERNAL_ERROR"))))
        self.assertIn("the secure connection failed (TLSV1_ALERT_INTERNAL_ERROR)", generic[0])  # the real error is shown
        untrusted = why("https://pc.ts.net/health", opener=failing(urllib.error.URLError(
            ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer certificate"))))
        self.assertIn("doesn't trust its certificate", untrusted[0])
        self.assertIn("Safari", untrusted[1])
        # a handshake that takes too long is a slow certificate, not a bad one
        self.assertIn("didn't answer within", why("https://pc.ts.net/health", opener=failing(urllib.error.URLError(
            TimeoutError("_ssl.c:1112: The handshake operation timed out"))))[0])
        self.assertIn("can't look up that name", why("https://pc.ts.net/health",
                      opener=failing(urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))))[0])
        report = pcdoctor.Report()
        pcdoctor.check_phone(report, {"installed": True, "state": "Running", "dns": "", "https": None, "serving": True})
        self.assertIn("didn't give this PC a name yet", report.render())

    def test_credits_out_until_a_deepgram_key_is_added(self):
        self.launcher.remember("voice", os.getpid())
        (self.data / "voice.log").write_text(
            "2026-10-31 14:00:01,000 [10] call started in room room-1\n"
            "2026-10-31 14:00:02,001 [10] LiveKit Inference may be out of free credits. Add a Deepgram key\n")
        self.assertIn("✗ LiveKit's free speech credits ran out, so it can't hear you", self.run_check(quick=True))
        (self.data / "voice.env").write_text("DEEPGRAM_API_KEY=dg_secret_value\n")
        self.assertNotIn("can't hear you", self.run_check(quick=True))

    def test_live_test_results(self):
        self.launcher.remember("voice", os.getpid())
        out = self.run_check(run_selftest=lambda: [
            {"test": "quick voice startup", "ok": True, "total_s": 0.7, "model": "haiku", "effort": "low", "cwd": "C:/Desktop"},
            {"test": "quick answer", "ok": True, "first_s": 1.1, "total_s": 1.2, "reply": "OK", "credentials": "none"},
            {"test": "hand-off", "ok": True, "first_s": 1.2, "reply": "[work] Sure, one sec.", "handed_off": True},
            {"test": "simple question", "ok": True, "first_s": 9.5, "total_s": 9.6, "reply": "OK",
             "connectors": {"plugin:small-business:gmail": "connected", "plugin:small-business:google-calendar": "needs-auth"}},
            {"test": "web search", "ok": True, "first_s": 2.0, "total_s": 12.0, "reply": "Stocks fell, says Reuters.", "tools": ["Bash"]},
        ])
        self.assertIn("✓ quick voice: Claude Code started in 0.7s (haiku, effort low, in C:/Desktop)", out)
        self.assertIn('✓ hand-off: the quick voice passed an email question to the work model, saying "Sure, one sec." after 1.2s', out)
        self.assertIn("✗ simple question: first words after 9.5s, done in 9.6s (slow)", out)
        self.assertIn("✓ plugin:small-business:gmail is connected", out)
        self.assertIn("✗ plugin:small-business:google-calendar is needs-auth", out)
        self.assertIn("✗ web search: the work model answered a news question without searching the web", out)
        self.assertIn("✓ Claude Code uses your Claude subscription login", out)

    def test_parse_voice_log_matches_the_mac_doctor(self):
        stats = pcdoctor.parse_voice_log("\n".join([
            "2026-10-31 14:00:01,000 [10] call started in room room-1",
            "2026-10-31 14:00:04,000 [10] quick voice: first words after 0.9s",
            "2026-10-31 14:00:05,100 [10] line 1: first words after 2.1s",
            "2026-10-31 14:01:00,000 [10] line 1: connectors not connected: plugin:small-business:gmail, plugin:small-business:hubspot",
            "2026-10-31 14:02:00,000 [10] voice error (tts): 429 Too Many Requests",
            "2026-10-31 14:03:00,000 [10] line 2: claude exited with code 1",
            "2026-10-31 14:03:01,000 [10] line 1: claude exited with code 0",
            "2026-10-31 15:00:00,000 [10] line 1: Claude API error (billing_error): Credit balance is too low",
            "not a log line",
        ]))
        self.assertEqual((stats.quick, stats.first_words, stats.calls, stats.crashes), ([0.9], [2.1], 1, 1))
        self.assertEqual(stats.not_connected, ["plugin:small-business:gmail", "plugin:small-business:hubspot"])
        self.assertEqual(stats.api_error, "billing_error")
        self.assertEqual(stats.errors, ["2026-10-31 14:02:00,000  tts: 429 Too Many Requests"])
        done = pcdoctor.parse_voice_log("2026-10-31 15:00:00,000 [10] line 1: Claude API error (billing_error): x\n"
                                        "2026-10-31 15:05:00,000 [10] line 1: done after 2.0s, 0 tool call(s)")
        self.assertIsNone(done.api_error)  # an answer after the error means it's fixed

    def test_tailscale_address(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[1:])
            out = ({"status": json.dumps({"BackendState": "Running", "Self": {"DNSName": "pc.tail1234.ts.net."}}),
                    "serve": "https://pc.tail1234.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8788\n"})[cmd[1]]
            return subprocess.CompletedProcess(cmd, 0, stdout=out)

        info = pcdoctor.tailscale_address(run=run, which=lambda n: "tailscale", exists=lambda p: False)
        self.assertEqual((info["state"], info["https"], info["serving"]), ("Running", "https://pc.tail1234.ts.net", True))
        self.assertEqual(pcdoctor.tailscale_address(which=lambda n: None, exists=lambda p: False), {"installed": False})


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.said = []
        self.ran = []
        self.present = {"git", "winget"}
        self.winget_hangs = False

    def tearDown(self):
        self.tmp.cleanup()

    def setup_run(self, launcher, answers, answers_hidden=(FAKE_SECRET,), present=None):
        present = self.present if present is None else present
        answers, hidden = iter(answers), iter(answers_hidden)

        def run(cmd, cwd=None, capture_output=False, timeout=None):
            self.ran.append(cmd)
            if cmd[0] == "winget" and self.winget_hangs:
                assert timeout, "winget must never be waited on for ever"
                raise subprocess.TimeoutExpired(cmd, timeout)
            if cmd[:2] == ["git", "clone"]:
                (launcher.visionclaw / "gateway").mkdir(parents=True)
            if cmd[1:3] == ["install"] and cmd[0] == "npm":
                (launcher.visionclaw / "gateway" / "node_modules").mkdir()
            if cmd[1:3] == ["-m", "venv"]:
                py = venv_python(launcher.data_dir)
                py.parent.mkdir(parents=True, exist_ok=True)
                py.write_text("")
            if cmd[:2] == ["winget", "install"]:
                present.add({"OpenJS.NodeJS.LTS": "node", "Tailscale.Tailscale": "tailscale"}[cmd[3]])
                if cmd[3] == "OpenJS.NodeJS.LTS":
                    present.add("npm")
            return subprocess.CompletedProcess(cmd, 0 if "-R" not in cmd else 1)

        def which(name):
            return name if name in present else None

        return setup_pc.setup(launcher, run=run, ask=lambda p: next(answers), askpass=lambda p: next(hidden),
                              which=which, windows=True, path_refresh=lambda: None)

    def test_a_fresh_pc(self):
        launcher = Launcher(data_dir=self.root / "data", visionclaw=self.root / "VisionClaw", root=HERE.parent.parent,
                            say=self.said.append)
        code = self.setup_run(launcher, ["wss://p.livekit.cloud", FAKE_KEY, "mycode"])
        commands = [" ".join(c) for c in self.ran]
        self.assertTrue(any(c.startswith("winget install --id OpenJS.NodeJS.LTS -e") for c in commands))
        self.assertTrue(any(c.startswith("winget install --id Tailscale.Tailscale -e") for c in commands))
        self.assertTrue(any(c.startswith("git clone https://github.com/sseanliu/VisionClaw") for c in commands))
        self.assertIn("git checkout 3f39807", commands)
        self.assertTrue(any(c.startswith("git apply ") and c.endswith("gateway-without-anthropic-key.patch") for c in commands))
        self.assertIn("npm install", commands)
        self.assertTrue(any("-m venv" in c for c in commands))
        self.assertTrue(any("pip install --quiet -r" in c and c.endswith("requirements.txt") for c in commands))
        env = envfile.read(launcher.visionclaw / "gateway" / ".env")
        self.assertEqual((env["LIVEKIT_URL"], env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"]),
                         ("wss://p.livekit.cloud", FAKE_KEY, FAKE_SECRET))
        self.assertEqual(env["GATEWAY_TOKENS"], "mycode:me")
        self.assertEqual((env["PORT"], env["ANTHROPIC_API_KEY"]), ("8788", "sk-ant-placeholder"))
        self.assertEqual(len(env["GATEWAY_SERVICE_TOKEN"]), 32)
        # Claude Code isn't installed, and setup says how, rather than running a downloaded script itself
        self.assertEqual(code, 1)
        self.assertTrue(any("irm https://claude.ai/install.ps1 | iex" in s for s in self.said))
        self.assertEqual(oct((launcher.visionclaw / "gateway" / ".env").stat().st_mode & 0o777), "0o600")

    def test_running_it_again_does_nothing_it_already_did(self):
        launcher = Launcher(data_dir=self.root / "data", visionclaw=self.root / "VisionClaw", root=HERE.parent.parent,
                            say=self.said.append)
        self.setup_run(launcher, ["wss://p.livekit.cloud", FAKE_KEY, ""])
        first_env = (launcher.visionclaw / "gateway" / ".env").read_text()
        self.ran.clear()
        self.said.clear()
        code = self.setup_run(launcher, [], present=self.present | {"claude"})
        self.assertEqual(code, 0)
        commands = [" ".join(c) for c in self.ran]
        self.assertFalse(any(c.startswith(("winget", "git clone", "npm install")) or "-m venv" in c for c in commands))
        self.assertEqual((launcher.visionclaw / "gateway" / ".env").read_text(), first_env)
        self.assertTrue(any("Setup is done" in s for s in self.said))
        # a code was made for the user, because they pressed Enter
        self.assertRegex(first_env, r"GATEWAY_TOKENS=[0-9a-f]{16}:me")

    def test_a_winget_that_hangs_is_stopped_and_the_website_is_named(self):
        self.winget_hangs = True
        launcher = Launcher(data_dir=self.root / "data", visionclaw=self.root / "VisionClaw", root=HERE.parent.parent,
                            say=self.said.append)
        code = self.setup_run(launcher, ["wss://p.livekit.cloud", FAKE_KEY, ""])
        self.assertEqual(code, 1)
        text = "\n".join(self.said)
        self.assertIn("winget didn't finish in 10 minutes, so I stopped it", text)
        self.assertIn("Node.js didn't install: download it from https://nodejs.org", text)
        self.assertIn("Tailscale didn't install: download it from https://tailscale.com/download/windows", text)
        self.assertTrue(all("--source" in c and "winget" in c for c in self.ran if c[0] == "winget"))

    def test_a_pc_without_winget_is_told_what_to_install(self):
        launcher = Launcher(data_dir=self.root / "data", visionclaw=self.root / "VisionClaw", root=HERE.parent.parent,
                            say=self.said.append)
        self.setup_run(launcher, ["wss://p.livekit.cloud", FAKE_KEY, ""], present={"git"})
        self.assertFalse(any(c[0] == "winget" for c in self.ran))
        self.assertTrue(any("Node.js isn't installed: download it from https://nodejs.org" in s for s in self.said))


class LiveKitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.said = []

    def tearDown(self):
        self.tmp.cleanup()

    def gateway_env(self, text):
        path = self.root / "VisionClaw" / "gateway" / ".env"
        path.parent.mkdir(parents=True)
        path.write_text(text)
        return path

    def run_set(self, path, answers, hidden):
        answers, hidden = iter(answers), iter(hidden)
        return setup_pc.set_livekit(path, ask=lambda p: next(answers), askpass=lambda p: next(hidden), say=self.said.append)

    def test_the_new_values_replace_the_old_and_everything_else_stays(self):
        path = self.gateway_env("PORT=8788\nLIVEKIT_URL=wss://old.livekit.cloud\nLIVEKIT_API_KEY=APIold\n"
                                "LIVEKIT_API_SECRET=\x16\nGATEWAY_TOKENS=mycode:me\n")
        self.assertEqual(self.run_set(path, ["", FAKE_KEY], [FAKE_SECRET]), 0)  # Enter keeps the address
        env = envfile.read(path)
        self.assertEqual((env["LIVEKIT_URL"], env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"]),
                         ("wss://old.livekit.cloud", FAKE_KEY, FAKE_SECRET))
        self.assertEqual((env["PORT"], env["GATEWAY_TOKENS"]), ("8788", "mycode:me"))
        self.assertTrue(any("Saved" in s and "glasses stop" in s for s in self.said))
        self.assertEqual(launcher_mod.livekit_problems(env), [])

    def test_a_secret_that_arrived_as_one_character_is_not_saved(self):
        path = self.gateway_env(f"LIVEKIT_URL=wss://x.livekit.cloud\nLIVEKIT_API_KEY=APIold\nLIVEKIT_API_SECRET={FAKE_SECRET}\n")
        before = path.read_text()
        self.assertEqual(self.run_set(path, ["wss://new.livekit.cloud", FAKE_KEY], ["\x16"]), 1)
        self.assertEqual(path.read_text(), before)  # nothing changed, not even the address
        self.assertTrue(any("only 0 characters" in s or "only 1 character" in s or "nothing was entered" in s for s in self.said))

    def test_a_short_secret_says_how_long_a_real_one_is(self):
        path = self.gateway_env("LIVEKIT_URL=wss://x.livekit.cloud\n")
        self.assertEqual(self.run_set(path, ["", FAKE_KEY], ["abc"]), 1)
        text = "\n".join(self.said)
        self.assertIn("only 3 characters; a real one is about 44", text)
        self.assertIn("type it by hand", text)

    def test_a_bad_address_or_key_is_refused(self):
        path = self.gateway_env("PORT=8788\n")
        self.assertEqual(self.run_set(path, ["https://x.livekit.cloud"], []), 1)
        self.assertIn("The address should start with wss://", "\n".join(self.said))
        self.assertEqual(self.run_set(path, ["wss://x.livekit.cloud", "AP"], []), 1)
        self.assertIn("That key is only 2 characters", "\n".join(self.said))

    def test_it_needs_setup_to_have_run_first(self):
        path = self.root / "nowhere" / "gateway" / ".env"
        self.assertEqual(self.run_set(path, [], []), 1)
        self.assertTrue(any("Run: glasses setup" in s for s in self.said))

    def test_setup_asks_again_for_a_secret_that_was_saved_too_short(self):
        path = self.gateway_env(f"LIVEKIT_URL=wss://x.livekit.cloud\nLIVEKIT_API_KEY={FAKE_KEY}\nLIVEKIT_API_SECRET=x\n"
                                "GATEWAY_TOKENS=mycode:me\n")
        asked = []
        ok = setup_pc.write_gateway_env(path, ask=lambda p: asked.append(p) or "",
                                        askpass=lambda p: asked.append(p) or FAKE_SECRET, say=self.said.append)
        self.assertTrue(ok)
        self.assertEqual(len(asked), 1)  # only the secret
        self.assertIn("secret", asked[0])
        self.assertEqual(envfile.read(path)["LIVEKIT_API_SECRET"], FAKE_SECRET)
        self.assertEqual(envfile.read(path)["GATEWAY_TOKENS"], "mycode:me")

    def test_the_cli_command_exists(self):
        self.assertIn("glasses livekit", glasses_cli.USAGE)


class AccessCodeTest(unittest.TestCase):
    def test_a_new_code_replaces_the_old_one_and_keeps_everything_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("PORT=8788\nGATEWAY_TOKENS=oldcode123:me\nLIVEKIT_URL=wss://x\n")
            code = setup_pc.new_access_code(path, make=lambda: "newcode456")
            self.assertEqual(code, "newcode456")
            env = envfile.read(path)
            self.assertEqual((env["GATEWAY_TOKENS"], env["PORT"], env["LIVEKIT_URL"]), ("newcode456:me", "8788", "wss://x"))
            self.assertNotIn("oldcode123", path.read_text())
            self.assertNotEqual(setup_pc.new_access_code(path), "newcode456")  # a real random one by default

    def test_it_refuses_a_settings_file_it_did_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("GATEWAY_TOKENS=a:me,b:you\n")
            self.assertIsNone(setup_pc.new_access_code(path))
            self.assertEqual(path.read_text(), "GATEWAY_TOKENS=a:me,b:you\n")
            self.assertIsNone(setup_pc.new_access_code(Path(tmp) / "missing.env"))


class CliTest(unittest.TestCase):
    def test_autostart_writes_and_removes_the_startup_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.environ.get("APPDATA")
            os.environ["APPDATA"] = tmp
            said = []
            try:
                self.assertEqual(glasses_cli.autostart(True, said.append, windows=True), 0)
                script = glasses_cli.startup_script()
                self.assertTrue(str(script).endswith(os.path.join("Startup", "claude-glasses.cmd")))
                text = script.read_text()
                self.assertIn(f'"{sys.executable}"', text)
                self.assertIn("glasses.py", text)
                self.assertTrue(text.rstrip().endswith("start --quiet"))
                self.assertEqual(glasses_cli.autostart(False, said.append, windows=True), 0)
                self.assertFalse(script.exists())
                self.assertEqual(glasses_cli.autostart(True, said.append, windows=False), 1)
            finally:
                if real is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = real

    def test_help_and_unknown_commands(self):
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            self.assertEqual(glasses_cli.main([]), 0)
            self.assertEqual(glasses_cli.main(["nonsense"]), 1)
        finally:
            sys.stdout = real
        self.assertIn("glasses start [folder ...]", out.getvalue())
        self.assertIn("I don't know 'nonsense'", out.getvalue())

    def test_the_entry_point_and_shortcut_exist(self):
        root = HERE.parent.parent
        result = subprocess.run([sys.executable, str(root / "glasses.py"), "help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("glasses setup", result.stdout)
        cmd = (root / "glasses.cmd").read_text()
        self.assertIn('py "%~dp0glasses.py" %*', cmd)
        self.assertNotIn("||", cmd)  # `A && B || C` would run glasses twice when it fails
        self.assertNotIn("/dev/null", cmd)


if __name__ == "__main__":
    unittest.main()
