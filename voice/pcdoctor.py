"""`glasses doctor` on a PC: checks every piece and says what to fix, in plain words.

The Mac's bin/glasses-doctor does the same checks in TypeScript. This one uses no Unix tools, so it
runs on Windows.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import envfile  # noqa: E402
import voicecheck  # noqa: E402
from launcher import GATEWAY_PORT, LIVEKIT_KEYS, Launcher, code_changed_at, venv_python  # noqa: E402

API_ERROR_HELP = {
    "billing_error": ("Claude Code said the credit balance is too low",
                      "run glasses doctor without --quick: its live test shows which account Claude Code uses"),
    "authentication_failed": ("Claude Code isn't logged in", "run claude in a terminal and type /login"),
    "rate_limit": ("you hit your Claude usage limit", "wait for it to reset, or use a lighter model (GLASSES_WORK_MODEL=sonnet)"),
}


def first_words_verdict(s: float) -> str:
    return "good" if s < 3.5 else "slow" if s < 6 else "very slow"


def work_verdict(s: float) -> str:
    return "good" if s < 8 else "slow" if s < 15 else "very slow"


@dataclass
class VoiceStats:
    quick: list[float] = field(default_factory=list)
    first_words: list[float] = field(default_factory=list)
    done: list[tuple[float, int]] = field(default_factory=list)
    calls: int = 0
    last_call: str | None = None
    errors: list[str] = field(default_factory=list)
    credits_out: bool = False
    crashes: int = 0
    not_connected: list[str] = field(default_factory=list)
    api_error: str | None = None


_LINE = re.compile(r"^(\S+ \S+) \[\d+\] (.*)$")


def parse_voice_log(text: str) -> VoiceStats:
    """Reads voice.log, which voice/worker.py writes."""
    stats = VoiceStats()
    for raw in text.splitlines():
        line = _LINE.match(raw)
        if not line:
            continue
        at, msg = line.groups()
        if m := re.match(r"^(.+?): first words after ([\d.]+)s$", msg):
            (stats.quick if m[1] == "quick voice" else stats.first_words).append(float(m[2]))
        elif m := re.search(r": done after ([\d.]+)s, (\d+) tool call", msg):
            stats.done.append((float(m[1]), int(m[2])))
            if ", failed:" not in msg:
                stats.api_error = None
        elif m := re.search(r": Claude API error \(([^)]*)\)", msg):
            stats.api_error = m[1]
        elif msg.startswith("call started in room"):
            stats.calls += 1
            stats.last_call = at
        elif m := re.match(r"^voice error \(([^)]*)\): (.*)$", msg):
            stats.errors.append(f"{at}  {m[1]}: {m[2]}"[:220])
        elif "out of free credits" in msg:
            stats.credits_out = True
        elif (m := re.search(r": claude exited with code (-?\d+)$", msg)) and int(m[1]) not in (0, -15, -9, 143, 137):
            stats.crashes += 1
        elif m := re.search(r": connectors not connected: (.*)$", msg):
            stats.not_connected = m[1].split(", ")
    return stats


@dataclass
class Report:
    lines: list[tuple[str, str, str | None]] = field(default_factory=list)  # (kind, text, fix)

    def ok(self, text: str) -> None:
        self.lines.append(("ok", text, None))

    def bad(self, text: str, fix: str | None = None) -> None:
        self.lines.append(("bad", text, fix))

    def info(self, text: str) -> None:
        self.lines.append(("info", text, None))

    def heading(self, text: str) -> None:
        self.lines.append(("heading", text, None))

    @property
    def problems(self) -> int:
        return sum(1 for kind, _, _ in self.lines if kind == "bad")

    def render(self) -> str:
        out = []
        for kind, text, fix in self.lines:
            if kind == "heading":
                out.append(f"\n{text}")
            elif kind == "ok":
                out.append(f"  ✓ {text}")
            elif kind == "bad":
                out.append(f"  ✗ {text}")
                if fix:
                    out.append(f"      → {fix}")
            else:
                out.append(f"    {text}")
        out.append("\nEverything is connected." if not self.problems else
                   f"\n{self.problems} problem{'' if self.problems == 1 else 's'} found.")
        return "\n".join(out).lstrip("\n")


def http_ok(url: str, timeout: float = 3.0, opener: Callable[..., Any] = urllib.request.urlopen) -> bool:
    try:
        with opener(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def tailscale_exe(which: Callable[[str], str | None] = shutil.which,
                  exists: Callable[[str], bool] = lambda p: Path(p).exists()) -> str | None:
    """Tailscale's command, which is not always on PATH right after the installer ran."""
    return which("tailscale") or next((p for p in (r"C:\Program Files\Tailscale\tailscale.exe",) if exists(p)), None)


def tailscale_address(run: Callable[..., Any] = subprocess.run, which: Callable[[str], str | None] = shutil.which,
                      exists: Callable[[str], bool] = lambda p: Path(p).exists()) -> dict[str, Any]:
    """Where the phone can reach this PC through Tailscale: its https name, and whether it is being served."""
    exe = tailscale_exe(which, exists)
    if not exe:
        return {"installed": False}
    try:
        status = json.loads(run([exe, "status", "--json"], capture_output=True, text=True, timeout=15).stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"installed": True, "state": "unknown"}
    dns = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    result: dict[str, Any] = {"installed": True, "state": status.get("BackendState", "unknown"), "dns": dns,
                              "https": f"https://{dns}" if dns else None, "serving": False}
    try:
        text = run([exe, "serve", "status"], capture_output=True, text=True, timeout=15).stdout or ""
        result["serving"] = str(GATEWAY_PORT) in text
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def check_phone(report: Report, tailscale: dict[str, Any], reachable: Callable[[str], bool] = http_ok) -> None:
    if not tailscale.get("installed"):
        report.bad("Tailscale isn't installed, so the phone can only reach this PC on your home Wi-Fi",
                   "run glasses setup, or install Tailscale from tailscale.com/download")
        return
    if tailscale.get("state") != "Running":
        report.bad(f"Tailscale isn't signed in (it says {tailscale.get('state')})",
                   "open Tailscale from the system tray (near the clock) and sign in")
        return
    address = tailscale.get("https")
    if not tailscale.get("serving"):
        report.bad("Tailscale isn't sharing the login service with your phone yet",
                   f"run: tailscale serve --bg {GATEWAY_PORT}   (if it asks you to approve HTTPS, open the link it prints)")
        return
    if address and reachable(f"{address}/health"):
        report.ok(f"the phone can reach this PC at {address}")
    else:
        report.bad(f"Tailscale is sharing {address}, but it doesn't answer",
                   "start the login service with glasses start, and check the login service line above")


def check(launcher: Launcher, quick: bool = False, report: Report | None = None,
          gateway_up: Callable[[], bool] | None = None, tailscale: dict[str, Any] | None = None,
          run_selftest: Callable[[], list[dict[str, Any]]] | None = None,
          reachable: Callable[[str], bool] = http_ok,
          elevenlabs: Callable[[str], dict[str, Any]] | None = None,
          deepgram: Callable[[str], dict[str, Any]] | None = None) -> Report:
    report = report or Report()
    data = launcher.data_dir
    report.heading("Connections (direct voice: you talk straight to Claude Code)")

    up = gateway_up() if gateway_up else http_ok(f"http://127.0.0.1:{GATEWAY_PORT}/health")
    if up:
        report.ok("VisionClaw's login service is running")
    else:
        report.bad("VisionClaw's login service isn't answering", "run: glasses start")

    voice = launcher.info("voice")
    if not voice:
        report.bad("the voice program isn't running", "run: glasses start")
    else:
        report.ok("the voice program is running")
        if voice.get("started", 0) < code_changed_at(launcher.root) - 1:
            report.bad("the voice program is still running code from before your last update",
                       "run: glasses stop, then glasses start")
        else:
            report.ok("the voice program is running the current code")

    gateway_env = envfile.read(launcher.visionclaw / "gateway" / ".env")
    absent = [k for k in LIVEKIT_KEYS if not gateway_env.get(k)]
    if absent:
        report.bad(f"{launcher.visionclaw / 'gateway' / '.env'} is missing {', '.join(absent)}", "run: glasses setup")
    else:
        report.ok("the LiveKit keys are in VisionClaw's settings")

    claude = shutil.which("claude")
    if not claude:
        report.bad("Claude Code isn't installed (or isn't on PATH)",
                   'open a new PowerShell window and run: irm https://claude.ai/install.ps1 | iex')
    elif claude.lower().endswith((".cmd", ".bat")):
        report.info(f"(Claude Code is {claude}, an npm install; the native installer from claude.ai/install.ps1 is more reliable)")

    voice_env = envfile.read(data / "voice.env")
    has = lambda key: bool(voice_env.get(key) or os.environ.get(key))  # noqa: E731
    python = venv_python(data)
    eleven = voice_env.get("ELEVEN_API_KEY") or voice_env.get("ELEVENLABS_API_KEY")
    if eleven:
        if quick:
            report.ok("the voice is ElevenLabs (run glasses doctor without --quick to test the key)")
        else:
            result = (elevenlabs or voicecheck.check)(eleven)
            if result.get("ok"):
                report.ok(f"the voice is ElevenLabs, and its key (ending {eleven[-4:]}) works")
                if result.get("note"):
                    report.info(f"({result['note']})")
            elif result.get("status") in ("unchecked", "network"):
                report.info(f"(couldn't test the ElevenLabs key: {result.get('problem')})")
            else:
                report.bad(f"the ElevenLabs voice can't speak: {result.get('problem')}; the backup voice speaks instead",
                           result.get("fix"))
    elif has("DEEPGRAM_API_KEY"):
        report.ok("the voice is Deepgram (for ElevenLabs run: glasses key ELEVEN_API_KEY)")
    else:
        report.info("(the voice is LiveKit Inference; for ElevenLabs run: glasses key ELEVEN_API_KEY)")
    deepgram_key = voice_env.get("DEEPGRAM_API_KEY")
    if deepgram_key:
        if quick:
            report.ok("speech-to-text is Deepgram (run glasses doctor without --quick to test the key)")
        else:
            result = (deepgram or voicecheck.check_deepgram)(deepgram_key)
            if result.get("ok"):
                report.ok(f"speech-to-text is Deepgram, and its key (ending {deepgram_key[-4:]}) works")
            else:
                report.bad(f"Deepgram, which hears you, has a problem: {result.get('problem')}", result.get("fix"))
    else:
        report.info("(speech-to-text is LiveKit Inference, whose free credits run out; for more: glasses key DEEPGRAM_API_KEY)")

    try:
        accounts = [a["address"] for a in json.loads((data / "email-accounts.json").read_text(encoding="utf-8"))]
    except (OSError, ValueError, KeyError):
        accounts = []
    if accounts:
        report.ok(f"email accounts: {', '.join(accounts)} (glasses email list checks that each logs in)")
    else:
        report.info("(no email accounts yet: add each one with: glasses email add you@example.com)")
    persona = data / "persona.md"
    if persona.exists():
        report.ok(f"your profile is loaded on every call ({persona})")
    else:
        report.info("(no profile yet: run glasses profile and paste yours in)")

    check_phone(report, tailscale if tailscale is not None else tailscale_address(), reachable)

    report.heading("Recent calls (from voice.log)")
    log_file = data / "voice.log"
    stats = parse_voice_log(log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else "")
    if stats.calls:
        report.info(f"{stats.calls} call{'' if stats.calls == 1 else 's'} in the log, the last at {stats.last_call}")

    def timing(label: str, values: list[float], judge: Callable[[float], str]) -> bool:
        recent = values[-10:]
        if not recent:
            return False
        mid = median(recent)
        line = f"{label} after {mid:.1f}s (median of the last {len(recent)})"
        verdict = judge(mid)
        if verdict == "good":
            report.ok(line)
        else:
            report.bad(f"{line}: {verdict}", 'see "Faster answers" in the README')
        return True

    any_quick = timing("the quick voice answered", stats.quick, first_words_verdict)
    any_work = timing("the work model started talking" if any_quick else "first words", stats.first_words,
                      work_verdict if any_quick else first_words_verdict)
    if not (any_quick or any_work):
        report.info("no answers yet")
    elif stats.done:
        recent = stats.done[-10:]
        report.info(f"work requests were done after {median(s for s, _ in recent):.1f}s on median; "
                    f"{sum(1 for _, t in recent if t)} of {len(recent)} used tools")
    if stats.credits_out and not has("DEEPGRAM_API_KEY"):
        report.bad("LiveKit's free speech credits ran out, so it can't hear you (the log says 429 Too Many Requests)",
                   "create a free account on deepgram.com, then run: glasses key DEEPGRAM_API_KEY")
    if stats.crashes:
        report.bad(f"Claude Code quit unexpectedly {stats.crashes} time{'' if stats.crashes == 1 else 's'}",
                   f"read {log_file}")
    for e in stats.errors[-3:]:
        report.info(f"voice error: {e}")
    if stats.api_error:
        say, fix = API_ERROR_HELP.get(stats.api_error, (f"Claude Code got an API error ({stats.api_error})", f"read {log_file}"))
        report.bad(f"on your last call, {say}", fix)
    for name in ("gmail", "calendar"):
        down = next((n for n in stats.not_connected if name in n.lower()), None)
        if down:
            report.bad(f"{down} was not connected the last time Claude Code started",
                       "run claude in a terminal, type /mcp, choose it and log in")

    if quick or not python.exists():
        return report
    report.heading("Live test (Claude Code, run exactly as the voice program runs it)")
    rows = (run_selftest or (lambda: _selftest(launcher)))()
    if not rows:
        report.bad("Claude Code did not start", "run claude in a terminal and check that you are logged in")
    for row in rows:
        test = str(row.get("test", ""))
        if test.endswith("startup"):
            who = test.removesuffix(" startup")
            if row.get("ok"):
                report.ok(f"{who}: Claude Code started in {row.get('total_s')}s ({row.get('model')}, effort {row.get('effort')}, in {row.get('cwd')})")
            else:
                report.bad(f"{who}: Claude Code quit right after starting", "run claude in a terminal and check that you are logged in")
            continue
        work = test in ("simple question", "email check", "web search")
        reply = re.sub(r"^\s*\[work\]\s*", "", str(row.get("reply") or ""), flags=re.I)
        if test == "web search" and row.get("ok") and not any(re.search(r"WebSearch|WebFetch|chrome", t, re.I) for t in row.get("tools") or []):
            report.bad("web search: the work model answered a news question without searching the web", f"read {log_file}")
        if test == "hand-off":
            if row.get("handed_off"):
                report.ok(f'hand-off: the quick voice passed an email question to the work model, saying "{reply}" after {row.get("first_s")}s')
            else:
                report.bad("hand-off: the quick voice answered an email question itself instead of passing it on", f"read {log_file}")
        elif row.get("ok") and row.get("first_s") is not None:
            verdict = (work_verdict if work else first_words_verdict)(float(row["first_s"]))
            line = f"{test}: first words after {row['first_s']}s, done in {row.get('total_s')}s"
            if verdict == "good":
                report.ok(line)
            else:
                report.bad(f"{line} ({verdict})", 'see "Faster answers" in the README')
            if reply and not row.get("api_error"):
                report.info(f"Claude said: {json.dumps(reply)}")
        else:
            report.bad(f"{test}: {row.get('error') or 'failed'}", f"read {log_file}")
        creds = row.get("credentials")
        if creds:
            ok_, say, fix = voicecheck_credentials(str(creds))
            if ok_:
                report.ok(say)
            else:
                report.bad(say, fix)
        for name, status in (row.get("connectors") or {}).items():
            if re.search(r"gmail|calendar", name, re.I):
                if status == "connected":
                    report.ok(f"{name} is connected")
                else:
                    report.bad(f"{name} is {status}", "run claude in a terminal, type /mcp, choose it and log in")
    report.info("On a call, add about a second for speech-to-text to notice you stopped talking.")
    return report


def voicecheck_credentials(source: str) -> tuple[bool, str, str | None]:
    if source == "none":
        return True, "Claude Code uses your Claude subscription login", None
    if source == "ANTHROPIC_API_KEY":
        return False, "Claude Code is paying with an API key (ANTHROPIC_API_KEY) instead of your Claude subscription", \
            "take GLASSES_USE_API_KEY=1 out of voice.env, then restart"
    if source == "apiKeyHelper":
        return False, "Claude Code gets an API key from an apiKeyHelper setting instead of your Claude subscription", \
            'remove "apiKeyHelper" from your Claude settings.json'
    if source == "/login managed key":
        return False, "Claude Code is logged in with an Anthropic Console account (prepaid API credit), not your Claude subscription", \
            "run claude, type /logout, then /login and choose your Claude account"
    return True, f"Claude Code's credentials: {source}", None


def _selftest(launcher: Launcher) -> list[dict[str, Any]]:
    env = dict(launcher.base_env)
    env.update({"GLASSES_DATA_DIR": str(launcher.data_dir), "PYTHONUTF8": "1"})
    for key, value in envfile.read(launcher.data_dir / "voice.env").items():
        env.setdefault(key, value)
    try:
        out = subprocess.run([str(venv_python(launcher.data_dir)), str(launcher.root / "voice" / "selftest.py")],
                             capture_output=True, text=True, timeout=300, env=env, cwd=str(launcher.root)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows
