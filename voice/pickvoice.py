"""Pick the glasses' ElevenLabs voice by listening to it.

    pickvoice.py              a list of voices; type a number to hear it, then y to keep it
    pickvoice.py VOICE_ID     hear one voice by its id (for a voice you added from elevenlabs.io/voice-library)

Samples are made with your own ElevenLabs key, in the same model the glasses use, and
played with the Mac's afplay, or Windows' own media player. Keeping one writes GLASSES_ELEVENLABS_VOICE into
~/.claude-glasses-bridge/voice.env; restart the voice window to use it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from voicecheck import DEFAULT_MODEL, DEFAULT_VOICE, key_from_env_file  # noqa: E402

DATA_DIR = Path(os.environ.get("GLASSES_DATA_DIR", Path.home() / ".claude-glasses-bridge")).expanduser()
API = "https://api.elevenlabs.io/v1"
SAMPLE = ("Hey, it's me, your assistant. I checked your email, and two messages need you. "
          "Want me to read them out?")

# ElevenLabs' own conversational voices, used when the account's list can't be read.
CURATED = [
    ("cjVigY5qzO86Huf0OWal", "Eric", "smooth, trustworthy man, made for assistants (the default)"),
    ("iP95p4xoKVk53GoZ742B", "Chris", "charming, down-to-earth man"),
    ("bIHbv24MWmeRgasZH58o", "Will", "relaxed, optimistic young man"),
    ("CwhRBWXzGAHq8TQ4Fs17", "Roger", "laid-back, resonant man"),
    ("IKne3meq5aSn9XLyUdCD", "Charlie", "confident, energetic young Australian man"),
    ("cgSgspJ2msm6clMCkdW9", "Jessica", "playful, bright, warm young woman"),
    ("SAz9YHcvj6GT2YYXdXww", "River", "relaxed, neutral, calm"),
]

Opener = Callable[..., Any]
Player = Callable[[Path], bool]


def api_key() -> str | None:
    return (os.environ.get("ELEVEN_API_KEY") or os.environ.get("ELEVENLABS_API_KEY")
            or key_from_env_file(directory=DATA_DIR))


def account_voices(key: str, opener: Opener = urllib.request.urlopen) -> list[tuple[str, str, str]] | None:
    """The voices this ElevenLabs account can use, or None if the key may not list them."""
    request = urllib.request.Request(f"{API}/voices", headers={"xi-api-key": key})
    try:
        with opener(request, timeout=20) as response:
            voices = json.loads(response.read())["voices"]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
        return None
    found = []
    for v in voices:
        labels = v.get("labels") or {}
        bits = [labels.get(k) for k in ("gender", "age", "accent", "descriptive", "use_case") if labels.get(k)]
        text = (v.get("description") or ", ".join(bits) or v.get("category") or "").strip().replace("\n", " ")
        found.append((v["voice_id"], v.get("name", v["voice_id"]), text[:90]))
    return found or None


def sample(key: str, voice: str, model: str, opener: Opener = urllib.request.urlopen) -> bytes:
    request = urllib.request.Request(
        f"{API}/text-to-speech/{voice}?output_format=mp3_44100_64",
        data=json.dumps({"text": SAMPLE, "model_id": model}).encode(),
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    with opener(request, timeout=30) as response:
        return response.read()


def player_command(path: Path, platform: str | None = None) -> list[str] | None:
    """How to play an mp3 here: afplay on a Mac, Windows' own media player through PowerShell."""
    platform = platform or sys.platform
    if platform == "darwin":
        return ["afplay", str(path)]
    if platform.startswith("win"):
        quoted = str(path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName presentationCore; $p = New-Object System.Windows.Media.MediaPlayer; "
            f"$p.Open([uri]'{quoted}'); while (-not $p.NaturalDuration.HasTimeSpan) {{ Start-Sleep -Milliseconds 100 }}; "
            "$p.Play(); Start-Sleep -Seconds ([math]::Ceiling($p.NaturalDuration.TimeSpan.TotalSeconds) + 1)"
        )
        return ["powershell", "-NoProfile", "-Command", script]
    for player in ("mpg123", "ffplay", "mpv"):
        if shutil.which(player):
            return [player, *(["-nodisp", "-autoexit"] if player == "ffplay" else []), str(path)]
    return None


def afplay(path: Path) -> bool:
    command = player_command(path)
    if command is None:
        return False
    try:
        return subprocess.run(command, timeout=90).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def save_voice(voice: str, directory: Path | None = None) -> Path:
    """Sets GLASSES_ELEVENLABS_VOICE in voice.env, keeping every other line."""
    directory = directory or DATA_DIR
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / "voice.env"
    try:
        lines = file.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    lines = [l for l in lines if not l.strip().removeprefix("export ").lstrip().startswith("GLASSES_ELEVENLABS_VOICE=")]
    lines.append(f"GLASSES_ELEVENLABS_VOICE={voice}")
    tmp = file.with_suffix(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, file)
    return file


def hear(key: str, voice: str, name: str, model: str, opener: Opener, play: Player) -> bool:
    """Plays a sample. False when it couldn't be made or played, after saying why."""
    try:
        audio = sample(key, voice, model, opener)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read() or b"{}").get("detail") or {}
            detail = body.get("message", "") if isinstance(body, dict) else str(body)
        except (ValueError, AttributeError):
            pass
        why = "needs a paid ElevenLabs plan" if e.code == 402 or "paid" in detail.lower() else (detail or f"error {e.code}")
        print(f"  Couldn't make a sample of {name}: {why}.")
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  Couldn't reach ElevenLabs: {e}")
        return False
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.mp3"
        path.write_bytes(audio)
        if not play(path):
            print("  Couldn't play it on this computer.")
            return False
    return True


def main(argv: list[str], ask: Callable[[str], str] = input, opener: Opener = urllib.request.urlopen,
         play: Player = afplay) -> int:
    key = api_key()
    if not key:
        print("No ElevenLabs key is saved yet. Run: ~/claude-glasses-bridge/bin/glasses-key ELEVEN_API_KEY")
        return 1
    model = os.environ.get("GLASSES_ELEVENLABS_MODEL") or DEFAULT_MODEL

    if argv:
        voice = argv[0].strip()
        print(f"Playing a sample of voice {voice}...")
        if not hear(key, voice, voice, model, opener, play):
            return 1
        if ask("Use this voice? [y/n] ").strip().lower().startswith("y"):
            print(f"Saved. Restart the voice window to use it ({save_voice(voice)}).")
        else:
            print("Nothing changed.")
        return 0

    voices = account_voices(key, opener)
    source = "your ElevenLabs account"
    if voices is None:
        voices, source = [(i, n, d) for i, n, d in CURATED], "ElevenLabs' conversational voices"
    voices = voices[:20]
    current = os.environ.get("GLASSES_ELEVENLABS_VOICE") or _current() or DEFAULT_VOICE
    while True:
        print(f"\nVoices from {source}:\n")
        for i, (vid, name, text) in enumerate(voices, 1):
            print(f"  {i:>2}. {name}{'  (current)' if vid == current else ''}" + (f": {text}" if text else ""))
        print("\n  To use a voice from elevenlabs.io/voice-library, add it to My Voices there, then run this again.")
        answer = ask("\nType a number to hear it, or q to quit: ").strip().lower()
        if answer in ("q", "quit", ""):
            print("Nothing changed.")
            return 0
        if not answer.isdigit() or not 1 <= int(answer) <= len(voices):
            print(f"Type a number from 1 to {len(voices)}.")
            continue
        vid, name, _ = voices[int(answer) - 1]
        print(f"\nPlaying {name}...")
        if not hear(key, vid, name, model, opener, play):
            continue
        reply = ask(f"Use {name}? y = use it, r = hear it again, n = keep looking: ").strip().lower()
        while reply.startswith("r"):
            hear(key, vid, name, model, opener, play)
            reply = ask(f"Use {name}? y = use it, r = hear it again, n = keep looking: ").strip().lower()
        if reply.startswith("y"):
            print(f"Saved {name}. Restart the voice window to use it ({save_voice(vid)}).")
            return 0


def _current() -> str | None:
    try:
        for line in (DATA_DIR / "voice.env").read_text(encoding="utf-8").splitlines():
            line = line.strip().removeprefix("export ").lstrip()
            if line.startswith("GLASSES_ELEVENLABS_VOICE="):
                return line.split("=", 1)[1].strip().strip("'\"") or None
    except OSError:
        pass
    return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
