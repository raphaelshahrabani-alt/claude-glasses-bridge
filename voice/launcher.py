"""Starts, stops and checks the glasses on a PC: VisionClaw's login service (gateway) and the voice worker.

Both run hidden in the background, each writing its own log, and are remembered by process id so
`glasses stop` can end them. The Mac keeps using bin/start-glasses, which opens Terminal windows.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import envfile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GATEWAY_PORT = 8788
LIVEKIT_KEYS = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
CODE_FILES = ("*.py", "*.md")


def default_data_dir() -> Path:
    return Path(os.environ.get("GLASSES_DATA_DIR", Path.home() / ".claude-glasses-bridge")).expanduser()


def default_visionclaw_dir() -> Path:
    return Path(os.environ.get("VISIONCLAW_DIR", Path.home() / "VisionClaw")).expanduser()


def venv_python(data_dir: Path) -> Path:
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return data_dir / "voice-venv" / sub[0] / sub[1]


# ---- processes -------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a finished child nobody has collected yet is not running
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def kill_tree(pid: int, wait_s: float = 8.0) -> None:
    """Ends a process and everything it started (npm starts node, for one)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    end = time.monotonic() + wait_s
    while time.monotonic() < end and pid_alive(pid):
        time.sleep(0.1)
    if pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def spawn(cmd: list[str], cwd: Path, env: Mapping[str, str], log: Path) -> int:
    """Starts a hidden background process that keeps running after this command ends."""
    log.parent.mkdir(parents=True, exist_ok=True)
    out = open(log, "ab")
    kwargs: dict = {}
    if os.name == "nt":
        # no window, and not tied to the PowerShell window it was started from
        kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL, stdout=out, stderr=out, **kwargs)
    out.close()
    return proc.pid


# ---- what to run -----------------------------------------------------------


def worker_env(data_dir: Path, visionclaw: Path, base: Mapping[str, str], project: str,
               extra_dirs: list[str]) -> dict[str, str]:
    """The environment the voice worker starts with: your settings, the LiveKit keys, and the folders."""
    env = dict(base)
    # what's already in the environment wins, as in bin/glasses-voice
    for key, value in envfile.read(data_dir / "voice.env").items():
        env.setdefault(key, value)
    gateway = envfile.read(visionclaw / "gateway" / ".env")
    for key in LIVEKIT_KEYS:
        if key in gateway:
            env.setdefault(key, gateway[key])
    env["GLASSES_PROJECT"] = project
    if extra_dirs:
        env["GLASSES_ADD_DIRS"] = os.pathsep.join(extra_dirs)
    env["GLASSES_DATA_DIR"] = str(data_dir)
    env["PYTHONUTF8"] = "1"
    return env


def missing_setup(data_dir: Path, visionclaw: Path) -> list[str]:
    """What `glasses setup` still has to do, in plain words."""
    problems = []
    if not venv_python(data_dir).exists():
        problems.append("the voice program isn't installed yet")
    gateway = visionclaw / "gateway"
    if not gateway.is_dir():
        problems.append(f"VisionClaw isn't downloaded ({visionclaw})")
    elif not (gateway / "node_modules").is_dir():
        problems.append("VisionClaw's login service isn't installed yet")
    env = envfile.read(gateway / ".env")
    absent = [k for k in LIVEKIT_KEYS if not env.get(k) or env[k].startswith("paste-")]
    if gateway.is_dir() and absent:
        problems.append(f"the login service's settings are missing {', '.join(absent)}")
    return problems


@dataclass
class Launcher:
    data_dir: Path = field(default_factory=default_data_dir)
    visionclaw: Path = field(default_factory=default_visionclaw_dir)
    root: Path = ROOT
    base_env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    say: Callable[[str], None] = print
    # replaceable in tests
    spawn: Callable[..., int] = staticmethod(spawn)
    port_open: Callable[[int], bool] = staticmethod(port_open)
    sleep: Callable[[float], None] = staticmethod(time.sleep)

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "run"

    def pid_file(self, name: str) -> Path:
        return self.run_dir / f"{name}.json"

    def info(self, name: str) -> dict | None:
        """{"pid", "started"} for a process this launcher started and that is still running."""
        try:
            info = json.loads(self.pid_file(name).read_text())
        except (OSError, ValueError):
            return None
        return info if pid_alive(int(info.get("pid", 0))) else None

    def remember(self, name: str, pid: int) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file(name).write_text(json.dumps({"pid": pid, "started": time.time()}))

    def log(self, name: str) -> Path:
        return self.data_dir / ("voice-console.log" if name == "voice" else f"{name}.log")

    def wait_for(self, check: Callable[[], bool], seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if check():
                return True
            self.sleep(0.5)
        return check()

    # ---- commands -----------------------------------------------------

    def start(self, project: str | None = None, extra_dirs: list[str] | None = None, quiet: bool = False,
              wait_s: float = 60.0) -> int:
        say = (lambda _t: None) if quiet else self.say
        problems = missing_setup(self.data_dir, self.visionclaw)
        if problems:
            for p in problems:
                self.say(f"✗ {p}")
            self.say("  Run: glasses setup")
            return 1
        saved = self._saved_launch()
        project = project or saved.get("project") or str(Path.home() / "Desktop")
        extra_dirs = extra_dirs if extra_dirs is not None else saved.get("extra_dirs", [])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "launch.json").write_text(json.dumps({"project": project, "extra_dirs": extra_dirs}))

        code = 0
        if self.port_open(GATEWAY_PORT):
            say("login service: already running")
        else:
            npm = shutil.which("npm") or "npm"
            pid = self.spawn([npm, "start"], self.visionclaw / "gateway", self._gateway_env(), self.log("gateway"))
            self.remember("gateway", pid)
            if self.wait_for(lambda: self.port_open(GATEWAY_PORT), 40):
                say("login service: started")
            else:
                self.say(f"✗ the login service didn't start. The end of {self.log('gateway')}:")
                self.say(self._tail(self.log("gateway"), 15))
                code = 1

        if self.info("voice"):
            say("voice: already running")
        else:
            env = worker_env(self.data_dir, self.visionclaw, self.base_env, project, extra_dirs)
            cmd = [str(venv_python(self.data_dir)), str(self.root / "voice" / "worker.py"), "start"]
            log = self.log("voice")
            size = log.stat().st_size if log.exists() else 0
            pid = self.spawn(cmd, self.root, env, log)
            self.remember("voice", pid)
            if self.wait_for(lambda: self._registered(log, size), wait_s):
                say(f"voice: started. Claude Code works in {project}")
            else:
                self.say(f"✗ the voice program didn't finish starting. The end of {log}:")
                self.say(self._tail(log, 15))
                code = 1
        if code == 0:
            say("Ready. Start a call from the phone.")
        return code

    def stop(self) -> int:
        stopped = []
        for name in ("voice", "gateway"):
            info = self.info(name)
            if info:
                kill_tree(int(info["pid"]))
                stopped.append(name)
            self.pid_file(name).unlink(missing_ok=True)
        if self.port_open(GATEWAY_PORT) and "gateway" not in stopped:
            self.say("(something else is serving the login service port; it was not started by glasses start, so I left it)")
        self.say("Stopped: " + ", ".join(stopped) if stopped else "Nothing was running.")
        return 0

    def status(self) -> dict[str, dict | None]:
        return {"gateway": self.info("gateway") or ({"pid": None, "started": None} if self.port_open(GATEWAY_PORT) else None),
                "voice": self.info("voice")}

    # ---- helpers ------------------------------------------------------

    def _saved_launch(self) -> dict:
        try:
            return json.loads((self.data_dir / "launch.json").read_text())
        except (OSError, ValueError):
            return {}

    def _gateway_env(self) -> dict[str, str]:
        env = dict(self.base_env)
        env["PYTHONUTF8"] = "1"
        return env

    @staticmethod
    def _tail(log: Path, lines: int) -> str:
        """The end of a log, indented, for showing when something didn't start."""
        try:
            text = log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        except OSError:
            return "    (no log was written)"
        return "\n".join("    " + line[:300] for line in text) or "    (the log is empty)"

    @staticmethod
    def _registered(log: Path, from_byte: int) -> bool:
        try:
            with open(log, "rb") as f:
                f.seek(from_byte)
                return b"registered worker" in f.read()
        except OSError:
            return False


def code_changed_at(root: Path = ROOT) -> float:
    """When the voice program's own files last changed."""
    newest = 0.0
    for pattern in CODE_FILES:
        for path in (root / "voice").glob(pattern):
            newest = max(newest, path.stat().st_mtime)
    return newest
