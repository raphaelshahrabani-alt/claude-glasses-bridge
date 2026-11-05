"""`glasses setup`: the one-time setup of the glasses on a PC.

Installs what's missing (Node.js, Tailscale) with winget, downloads VisionClaw at the version the
patches were made for, installs the voice program's own Python environment, and writes VisionClaw's
settings file by asking for your LiveKit values. Safe to run again: it only does what is missing.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import envfile  # noqa: E402
import keys  # noqa: E402
from launcher import ROOT, Launcher, livekit_problems, venv_python  # noqa: E402

VISIONCLAW_REPO = "https://github.com/sseanliu/VisionClaw"
VISIONCLAW_COMMIT = "3f39807"  # the version the patches in integrations/visionclaw were made for
GATEWAY_PATCH = "gateway-without-anthropic-key.patch"
WINGET_TIMEOUT_S = 600  # winget can hang on some PCs; don't wait for ever
WINGET = [
    ("node", "OpenJS.NodeJS.LTS", "Node.js", "https://nodejs.org (the LTS button)"),
    ("tailscale", "Tailscale.Tailscale", "Tailscale", "https://tailscale.com/download/windows"),
]
CLAUDE_INSTALL = "irm https://claude.ai/install.ps1 | iex"


def refresh_path() -> None:
    """After winget installs a program, this window's PATH doesn't know about it yet: read it again."""
    if os.name != "nt":
        return
    import winreg

    parts: list[str] = []
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(hive, key) as k:
                value, _ = winreg.QueryValueEx(k, "Path")
                parts.append(os.path.expandvars(value))
        except OSError:
            pass
    if parts:
        os.environ["PATH"] = os.pathsep.join(parts + [os.environ.get("PATH", "")])


def setup(launcher: Launcher | None = None, run: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
          ask: Callable[[str], str] = input, askpass: Callable[[str], str] = keys.read_secret,
          which: Callable[[str], str | None] = shutil.which, windows: bool | None = None,
          path_refresh: Callable[[], None] = refresh_path) -> int:
    launcher = launcher or Launcher()
    say = launcher.say
    windows = (os.name == "nt") if windows is None else windows
    todo: list[str] = []

    def do(cmd: list[str], cwd: Path | None = None, timeout: float | None = None) -> bool:
        say("  $ " + " ".join(cmd))
        try:
            return run(cmd, cwd=str(cwd) if cwd else None, timeout=timeout).returncode == 0
        except subprocess.TimeoutExpired:
            say(f"  ✗ {cmd[0]} didn't finish in {int((timeout or 0) / 60)} minutes, so I stopped it")
            return False
        except OSError as e:
            say(f"  ✗ couldn't run {cmd[0]}: {e}")
            return False

    say("1. Checking the Python version")
    if not (3, 10) <= sys.version_info[:2] < (3, 15):
        say(f"✗ Python {sys.version_info[0]}.{sys.version_info[1]} is too old or too new here; install Python 3.12 (winget install --id Python.Python.3.12 -e)")
        return 1
    say(f"  ✓ Python {sys.version_info[0]}.{sys.version_info[1]}")

    say("2. Installing the programs it needs")
    if not which("git"):
        todo.append("Git isn't installed: winget install --id Git.Git -e   (then open a new PowerShell window)")
    for exe, package, label, website in WINGET:
        if which(exe) or (exe == "tailscale" and Path(r"C:\Program Files\Tailscale\tailscale.exe").exists()):
            say(f"  ✓ {label}")
            continue
        if not (windows and which("winget")):
            todo.append(f"{label} isn't installed: download it from {website}, install it, then run this again")
            continue
        say(f"  installing {label} (Windows may ask for permission; click Yes)")
        if not do(["winget", "install", "--id", package, "-e", "--source", "winget", "--accept-package-agreements",
                   "--accept-source-agreements"], timeout=WINGET_TIMEOUT_S):
            todo.append(f"{label} didn't install: download it from {website}, install it, then run this again")
    path_refresh()
    if which("claude"):
        say("  ✓ Claude Code")
    else:
        todo.append(f"Claude Code isn't installed: in PowerShell run   {CLAUDE_INSTALL}   then open a new window, run   claude   and log in")

    say("3. Downloading VisionClaw")
    vc = launcher.visionclaw
    if not (vc / "gateway").is_dir():
        if not which("git"):
            todo.append("VisionClaw can't be downloaded until Git is installed")
        elif not (do(["git", "clone", VISIONCLAW_REPO, str(vc)]) and do(["git", "checkout", VISIONCLAW_COMMIT], vc)):
            todo.append(f"VisionClaw didn't download: check the internet connection and run this again")
    else:
        say(f"  ✓ {vc}")
    patch = launcher.root / "integrations" / "visionclaw" / GATEWAY_PATCH
    if (vc / "gateway").is_dir() and which("git"):
        if run(["git", "apply", "--check", "-R", str(patch)], cwd=str(vc), capture_output=True).returncode == 0:
            say("  ✓ the login-service fix is applied")
        elif run(["git", "apply", "--check", str(patch)], cwd=str(vc), capture_output=True).returncode == 0:
            do(["git", "apply", str(patch)], vc)
            say("  ✓ applied the login-service fix")
        else:
            todo.append("the login-service fix doesn't apply to this copy of VisionClaw (it may have changed); tell Claude")

    say("4. Installing the login service")
    npm = which("npm")
    if (vc / "gateway").is_dir() and npm:
        if (vc / "gateway" / "node_modules").is_dir():
            say("  ✓ already installed")
        elif not do([npm, "install"], vc / "gateway"):
            todo.append("the login service didn't install (npm install failed); run this again")
    elif (vc / "gateway").is_dir():
        todo.append("the login service can't be installed until Node.js is: open a new PowerShell window and run this again")

    say("5. Installing the voice program")
    python = venv_python(launcher.data_dir)
    if not python.exists():
        launcher.data_dir.mkdir(parents=True, exist_ok=True)
        do([sys.executable, "-m", "venv", str(launcher.data_dir / "voice-venv")])
    if python.exists():
        if do([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"]) and \
           do([str(python), "-m", "pip", "install", "--quiet", "-r", str(launcher.root / "voice" / "requirements.txt")]):
            say("  ✓ installed")
        else:
            todo.append("the voice program didn't install; check the internet connection and run this again")
    else:
        todo.append("the voice program's Python environment couldn't be created")

    say("6. VisionClaw's settings")
    env_file = vc / "gateway" / ".env"
    if (vc / "gateway").is_dir():
        if not write_gateway_env(env_file, ask, askpass, say):
            todo.append(f"the login service's settings are incomplete: run this again to fill them in ({env_file})")

    say("")
    if todo:
        say("Still to do:")
        for item in todo:
            say(f"  ✗ {item}")
        say("Then run: glasses setup   (it picks up where it left off)")
        return 1
    say("✓ Setup is done. Next: glasses phone   (to let your phone reach this PC), then glasses start")
    return 0


def write_gateway_env(path: Path, ask: Callable[[str], str], askpass: Callable[[str], str],
                      say: Callable[[str], None]) -> bool:
    """VisionClaw's settings file: the LiveKit values and your access code. Asks only for what's missing."""
    current = envfile.read(path)

    wrong = {k: True for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
             if not current.get(k) or current[k].startswith("paste-")}
    if livekit_problems(current) and "LIVEKIT_API_SECRET" not in wrong and current.get("LIVEKIT_API_SECRET"):
        wrong["LIVEKIT_API_SECRET"] = True  # present, but too short to be real
    needed = list(wrong)
    if not needed and current.get("GATEWAY_TOKENS"):
        say("  ✓ already filled in")
        return True
    say("  Open cloud.livekit.io, pick your project, then Settings → API Keys. Create a new key for this PC")
    say("  (the secret is shown only once). The address looks like wss://your-project.livekit.cloud")
    values = dict(current)
    values.setdefault("ANTHROPIC_API_KEY", "sk-ant-placeholder")
    values.setdefault("PORT", "8788")
    values.setdefault("GATEWAY_SERVICE_TOKEN", secrets.token_hex(16))
    values.setdefault("STORE_PATH", "./data/gateway-store.json")
    for key in needed:
        answer = ask_livekit_value(key, ask, askpass, say)
        if answer is None:
            return False
        values[key] = answer
    if not values.get("GATEWAY_TOKENS"):
        code = ask("  Access code you'll type into the phone app (press Enter to make a new one): ").strip()
        code = code or secrets.token_hex(8)
        values["GATEWAY_TOKENS"] = f"{code}:me"
        say(f"  Your access code is: {code}   (you type it into the phone app)")
    path.parent.mkdir(parents=True, exist_ok=True)
    order = ["ANTHROPIC_API_KEY", "PORT", "GATEWAY_TOKENS", "GATEWAY_SERVICE_TOKEN", "STORE_PATH",
             "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"]
    lines = [f"{k}={values[k]}" for k in order if k in values] + [f"{k}={v}" for k, v in values.items() if k not in order]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    say("  ✓ saved")
    return True


LIVEKIT_LABELS = {"LIVEKIT_URL": "LiveKit address (wss://...)", "LIVEKIT_API_KEY": "LiveKit API key",
                  "LIVEKIT_API_SECRET": "LiveKit API secret (nothing shows as you paste)"}


def ask_livekit_value(key: str, ask: Callable[[str], str], askpass: Callable[[str], str],
                      say: Callable[[str], None]) -> str | None:
    """One LiveKit value, cleaned and checked. None (after saying why) if it can't be right."""
    raw = (askpass if key == "LIVEKIT_API_SECRET" else ask)(f"  {LIVEKIT_LABELS[key]}: ")
    value = keys.clean(raw or "")
    if not value:
        say("  (nothing was entered)")
        return None
    if key == "LIVEKIT_URL" and not value.startswith(("wss://", "ws://")):
        say("  ✗ The address should start with wss://")
        return None
    if key == "LIVEKIT_API_KEY" and len(value) < 8:
        say(f"  ✗ That key is only {len(value)} characters; a real one is about 15 (it starts with API).")
        return None
    if key == "LIVEKIT_API_SECRET" and len(value) < 20:
        say(f"  ✗ That secret is only {len(value)} character{'' if len(value) == 1 else 's'}; a real one is about 44. "
            + keys.what_arrived())
        say("  Copy it again (type it by hand if pasting fails) and run: glasses livekit")
        return None
    return value


def set_livekit(path: Path, ask: Callable[[str], str] = input, askpass: Callable[[str], str] = keys.read_secret,
                say: Callable[[str], None] = print) -> int:
    """`glasses livekit`: enter the LiveKit address, key and secret again, keeping everything else."""
    current = envfile.read(path)
    if not path.parent.is_dir():
        say(f"VisionClaw isn't downloaded yet ({path.parent.parent}). Run: glasses setup")
        return 1
    say("Open cloud.livekit.io, pick your project, then Settings → API Keys, and create a new key.")
    say("The secret is shown only once. Use the Copy button, then paste here (Ctrl+V or right-click).")
    shown = current.get("LIVEKIT_URL", "")
    url = keys.clean(ask(f"  LiveKit address{' (Enter keeps ' + shown + ')' if shown else ''}: ") or "") or shown
    if not url.startswith(("wss://", "ws://")):
        say("  ✗ The address should start with wss://")
        return 1
    api_key = ask_livekit_value("LIVEKIT_API_KEY", ask, askpass, say)
    if api_key is None:
        return 1
    secret = ask_livekit_value("LIVEKIT_API_SECRET", ask, askpass, say)
    if secret is None:
        return 1
    for name, value in (("LIVEKIT_URL", url), ("LIVEKIT_API_KEY", api_key), ("LIVEKIT_API_SECRET", secret)):
        envfile.set_value(path, name, value)
    say("✓ Saved. Restart to use it: glasses stop, then glasses start")
    return 0


def new_access_code(path: Path, make: Callable[[], str] = lambda: secrets.token_hex(8)) -> str | None:
    """Replaces the phone app's access code in VisionClaw's settings. None if the file isn't the simple
    one-code kind that `glasses setup` writes."""
    entries = [t for t in envfile.read(path).get("GATEWAY_TOKENS", "").split(",") if t]
    if len(entries) != 1 or ":" not in entries[0]:
        return None
    code = make()
    envfile.set_value(path, "GATEWAY_TOKENS", f"{code}:{entries[0].split(':', 1)[1]}")
    return code
