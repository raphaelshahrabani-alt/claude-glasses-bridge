"""Checks the ElevenLabs key the way the voice uses it, and says in plain words
why ElevenLabs refuses it. The voice's own connection only reports "closed
unexpectedly (1008)"; the regular API says why.

    voicecheck.py           a sentence for people
    voicecheck.py --json    {"ok": ..., "problem": ..., "fix": ...} for glasses-doctor

The key comes from ELEVEN_API_KEY, or from ~/.claude-glasses-bridge/voice.env.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

DATA_DIR = Path(os.environ.get("GLASSES_DATA_DIR", Path.home() / ".claude-glasses-bridge")).expanduser()
DEFAULT_VOICE = "cjVigY5qzO86Huf0OWal"
DEFAULT_MODEL = "eleven_turbo_v2_5"
SAVE_AGAIN = "copy the key again from elevenlabs.io (API Keys) and run: ~/claude-glasses-bridge/bin/glasses-key ELEVEN_API_KEY"

FIXES = {
    "invalid_api_key": ("ElevenLabs says the key isn't valid", SAVE_AGAIN),
    "missing_permissions": (
        "the key isn't allowed to make speech",
        "on elevenlabs.io → API Keys, edit the key and set Text to Speech to Access (or make a new key "
        "without restrictions), then run: ~/claude-glasses-bridge/bin/glasses-key ELEVEN_API_KEY",
    ),
    "quota_exceeded": (
        "the ElevenLabs account is out of credits for this month",
        "upgrade the plan on elevenlabs.io, or wait for the monthly reset (the backup voice speaks meanwhile)",
    ),
    "detected_unusual_activity": (
        "ElevenLabs blocked its free plan for this account (\"unusual activity\")",
        "ElevenLabs only lifts this with a paid plan (Starter is enough); the backup voice speaks meanwhile",
    ),
    "payment_required": ("ElevenLabs needs a payment on this account", "check billing on elevenlabs.io"),
    "voice_not_found": (
        "that voice isn't available to this account",
        "remove GLASSES_ELEVENLABS_VOICE from ~/.claude-glasses-bridge/voice.env to use the default voice",
    ),
}


def key_from_env_file() -> str | None:
    try:
        text = (DATA_DIR / "voice.env").read_text(encoding="utf-8")
    except OSError:
        return None
    value = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        for name in ("ELEVEN_API_KEY", "ELEVENLABS_API_KEY"):
            if line.startswith(name + "="):
                value = line.split("=", 1)[1].strip().strip("'\"") or value
    return value


def check(key: str, voice: str = DEFAULT_VOICE, model: str = DEFAULT_MODEL,
          opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_22050_32",
        data=json.dumps({"text": "Hi.", "model_id": model}).encode(),
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    try:
        with opener(request, timeout=20) as response:
            response.read(64)
        return {"ok": True}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except (json.JSONDecodeError, ValueError):
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        status = (detail.get("status") if isinstance(detail, dict) else None) or ""
        message = (detail.get("message") if isinstance(detail, dict) else detail) or e.reason
        if not status and e.code == 401:
            status = "invalid_api_key"
        problem, fix = FIXES.get(status, (f"ElevenLabs refused it ({e.code} {status}): {message}", "see the message"))
        return {"ok": False, "status": status or str(e.code), "problem": problem, "fix": fix,
                "message": str(message)[:300]}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "status": "network", "problem": f"couldn't reach ElevenLabs: {e}",
                "fix": "check the Mac's internet connection"}


def main(argv: list[str]) -> int:
    key = os.environ.get("ELEVEN_API_KEY") or os.environ.get("ELEVENLABS_API_KEY") or key_from_env_file()
    if not key:
        result: dict[str, Any] = {"ok": False, "status": "no_key", "problem": "no ElevenLabs key is saved",
                                  "fix": "run: ~/claude-glasses-bridge/bin/glasses-key ELEVEN_API_KEY"}
    else:
        result = check(key, os.environ.get("GLASSES_ELEVENLABS_VOICE", DEFAULT_VOICE),
                       os.environ.get("GLASSES_ELEVENLABS_MODEL", DEFAULT_MODEL))
        result["key_ends"] = key[-4:]
    if "--json" in argv:
        print(json.dumps(result))
    elif result["ok"]:
        print(f"✓ ElevenLabs works with the key ending in {result['key_ends']}.")
    else:
        print(f"✗ The ElevenLabs voice can't speak: {result['problem']}.")
        print(f"  → {result['fix']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
