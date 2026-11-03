"""The `glasses` command for a PC. Run it as `glasses <command>` (glasses.cmd) or `py glasses.py <command>`."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from launcher import GATEWAY_PORT, ROOT, Launcher, default_data_dir, venv_python  # noqa: E402

USAGE = """glasses: your Meta glasses assistant, on this PC

  glasses setup               one-time setup of a new PC (safe to run again)
  glasses start [folder ...]  start everything; Claude Code works in the first folder
  glasses stop                stop everything
  glasses status              what is running
  glasses doctor              check every piece and say what to fix (--quick skips the live test)
  glasses phone               let your phone reach this PC from anywhere (Tailscale)
  glasses newcode             make a new access code for the phone app
  glasses key NAME            save a key: ELEVEN_API_KEY (voice) or DEEPGRAM_API_KEY (hearing)
  glasses email add you@example.com | list | remove you@example.com
  glasses pick-voice          hear the voices and pick one
  glasses profile             open your profile in Notepad
  glasses logs [voice|gateway]  the end of a log
  glasses autostart on|off    start by itself when you sign in to Windows
"""

VENV_COMMANDS = {"email", "pick-voice"}  # these use the voice program's own Python (it has the password keychain)


def in_venv(script: str, args: list[str]) -> int:
    python = venv_python(default_data_dir())
    if not python.exists():
        print("The voice program isn't installed yet. Run: glasses setup")
        return 1
    return subprocess.run([str(python), str(ROOT / "voice" / script), *args]).returncode


def startup_script() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "claude-glasses.cmd"


def autostart(on: bool, say=print, windows: bool | None = None) -> int:
    if not (os.name == "nt" if windows is None else windows):
        say("Starting at login is set up for Windows only.")
        return 1
    path = startup_script()
    if not on:
        path.unlink(missing_ok=True)
        say("Won't start by itself any more.")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'@echo off\r\nstart "" /min "{sys.executable}" "{ROOT / "glasses.py"}" start --quiet\r\n')
    say(f"It will start by itself when you sign in to Windows ({path}).")
    say("Tip: so that it also starts after a restart without you, turn on automatic sign-in")
    say("(press Windows+R, type  netplwiz,  untick 'Users must enter a user name and password').")
    return 0


def phone(launcher: Launcher, say=print) -> int:
    from pcdoctor import tailscale_address, tailscale_exe

    info = tailscale_address()
    if not info.get("installed"):
        say("Tailscale isn't installed. Run: glasses setup")
        return 1
    if info.get("state") != "Running":
        say(f"Tailscale isn't signed in yet (it says {info.get('state')}). Open Tailscale from the system tray")
        say("(the little icons near the clock), click Log in, and sign in. Then run this again.")
        return 1
    if not info.get("serving"):
        say(f"Sharing the login service (port {GATEWAY_PORT}) with your own devices through Tailscale...")
        result = subprocess.run([tailscale_exe() or "tailscale", "serve", "--bg", str(GATEWAY_PORT)],
                                capture_output=True, text=True, timeout=90)
        say((result.stdout or "").strip() + (result.stderr or "").strip())
        if result.returncode != 0:
            say("")
            say("If it printed a link, open it and approve HTTPS for your Tailscale network, then run this again.")
            return 1
        info = tailscale_address()
    address = info.get("https")
    say("")
    say("On your iPhone: install Tailscale from the App Store and sign in with the same account as here.")
    say("In the VisionClaw app, set the Gateway URL (on the login screen, or in Settings) to:")
    say(f"    {address}")
    say("and log in with your access code (it's the GATEWAY_TOKENS value before the colon in")
    say(f"{launcher.visionclaw / 'gateway' / '.env'}).")
    return 0


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    launcher = Launcher()

    if command in VENV_COMMANDS:
        return in_venv("email_accounts.py" if command == "email" else "pickvoice.py", rest)
    if command == "setup":
        from setup_pc import setup

        return setup(launcher)
    if command == "start":
        quiet = "--quiet" in rest
        folders = [str(Path(f).expanduser().resolve()) for f in rest if f != "--quiet"]
        return launcher.start(folders[0] if folders else None, folders[1:] if folders else None, quiet=quiet)
    if command == "stop":
        return launcher.stop()
    if command == "status":
        for name, info in launcher.status().items():
            print(f"  {name}: {'running' if info else 'not running'}")
        return 0
    if command == "doctor":
        from pcdoctor import check

        report = check(launcher, quick="--quick" in rest)
        print(report.render())
        return 1 if report.problems else 0
    if command == "key":
        from keys import save

        return save(rest[0] if rest else "")
    if command == "phone":
        return phone(launcher)
    if command == "newcode":
        from setup_pc import new_access_code

        env_file = launcher.visionclaw / "gateway" / ".env"
        code = new_access_code(env_file)
        if code is None:
            print(f"I can't change the code in {env_file}: it isn't the simple one-code kind. Edit GATEWAY_TOKENS there.")
            return 1
        print(f"Your new access code is: {code}")
        print("Write it down; it's what you type into the phone app. The old one no longer works.")
        if launcher.status()["gateway"]:
            print("The login service is running, so restart it to use the new code: glasses stop, then glasses start")
        return 0
    if command == "profile":
        path = launcher.data_dir / "persona.md"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((ROOT / "templates" / "business-partner.md").read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Opening {path}. Fill it in, save, and close it; then run: glasses stop, glasses start")
        if os.name == "nt":
            subprocess.Popen(["notepad", str(path)])
        return 0
    if command == "logs":
        name = rest[0] if rest else "voice"
        log = default_data_dir() / ("voice.log" if name == "voice" else f"{name}.log")
        try:
            print("".join(log.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-40:]))
        except OSError:
            print(f"No log yet at {log}")
        return 0
    if command == "autostart":
        return autostart(rest[:1] == ["on"])
    print(f"I don't know '{command}'.\n")
    print(USAGE)
    return 1
