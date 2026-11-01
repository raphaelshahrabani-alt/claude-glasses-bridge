"""The small changes that let the existing code run on Windows, checked with Windows paths and commands."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import email_accounts  # noqa: E402
import pickvoice  # noqa: E402
from claude_lane import LaneConfig, git_bash_path, launch_command  # noqa: E402


class FindingClaudeTest(unittest.TestCase):
    def test_not_windows_changes_nothing(self):
        self.assertEqual(launch_command(["claude", "-p"], windows=False), ["claude", "-p"])

    def test_the_native_install_is_started_by_its_full_path(self):
        which = lambda name: r"C:\Users\me\.local\bin\claude.exe"  # noqa: E731
        self.assertEqual(launch_command(["claude", "-p", "--model", "opus"], windows=True, which=which),
                         [r"C:\Users\me\.local\bin\claude.exe", "-p", "--model", "opus"])

    def test_an_npm_install_is_a_cmd_shim_that_only_cmd_can_run(self):
        which = lambda name: r"C:\Users\me\AppData\Roaming\npm\claude.CMD"  # noqa: E731
        self.assertEqual(launch_command(["claude", "-p"], windows=True, which=which),
                         ["cmd.exe", "/c", r"C:\Users\me\AppData\Roaming\npm\claude.CMD", "-p"])

    def test_git_bash_is_pointed_out_when_it_is_not_on_the_path(self):
        found = git_bash_path(windows=True, exists=lambda p: p == r"C:\Program Files\Git\bin\bash.exe", which=lambda n: None)
        self.assertEqual(found, r"C:\Program Files\Git\bin\bash.exe")
        self.assertIsNone(git_bash_path(windows=False))
        self.assertIsNone(git_bash_path(windows=True, exists=lambda p: False, which=lambda n: None))

    def test_the_always_ask_settings_can_go_in_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice-settings.json"
            cmd = LaneConfig(cwd="/x", settings_path=str(path)).command()
            self.assertEqual(cmd[cmd.index("--settings") + 1], str(path))  # no JSON on the command line
            self.assertIn("mcp__email__send_email", json.loads(path.read_text())["permissions"]["ask"])
            inline = LaneConfig(cwd="/x").command()
            self.assertTrue(inline[inline.index("--settings") + 1].startswith("{"))


class PlayerTest(unittest.TestCase):
    def test_each_platform_has_a_player(self):
        path = Path("sample.mp3")
        self.assertEqual(pickvoice.player_command(path, "darwin"), ["afplay", "sample.mp3"])
        windows = pickvoice.player_command(Path(r"C:\Users\O'Neil\sample.mp3"), "win32")
        self.assertEqual(windows[:3], ["powershell", "-NoProfile", "-Command"])
        self.assertIn("System.Windows.Media.MediaPlayer", windows[3])
        self.assertIn("O''Neil", windows[3])  # an apostrophe in the path can't end the PowerShell string early


class MailServersTest(unittest.TestCase):
    def setUp(self):
        self.which, self.run = email_accounts.shutil.which, email_accounts.subprocess.run

    def tearDown(self):
        email_accounts.shutil.which, email_accounts.subprocess.run = self.which, self.run

    def test_windows_has_no_dig_so_nslookup_is_used(self):
        nslookup = (
            "Server:  dns.google\nAddress:  8.8.8.8\n\nNon-authoritative answer:\n"
            "example.ca\tMX preference = 1, mail exchanger = aspmx.l.google.com\n"
            "example.ca\tMX preference = 5, mail exchanger = alt1.aspmx.l.google.com\n")
        email_accounts.shutil.which = lambda name: {"nslookup": "nslookup"}.get(name)
        email_accounts.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=nslookup)
        self.assertIn("aspmx.l.google.com", email_accounts.mx_records("example.ca"))
        self.assertEqual(email_accounts.mx_provider("example.ca"), "gmail")

    def test_the_dns_server_named_in_the_first_lines_is_not_mistaken_for_the_mail_server(self):
        nslookup = "Server:  dns.google.com\nAddress:  8.8.8.8\n\nexample.ca\tMX preference = 10, mail exchanger = mx.selfhosted.example\n"
        email_accounts.shutil.which = lambda name: "nslookup" if name == "nslookup" else None
        email_accounts.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=nslookup)
        self.assertIsNone(email_accounts.mx_provider("example.ca"))

    def test_no_lookup_tool_at_all(self):
        email_accounts.shutil.which = lambda name: None
        self.assertEqual(email_accounts.mx_records("example.ca"), "")


if __name__ == "__main__":
    unittest.main()
