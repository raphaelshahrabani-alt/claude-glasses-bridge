"""Checks the ElevenLabs key the way the voice uses it, and says in plain words
why ElevenLabs refuses it. The voice's own connection only reports "closed
unexpectedly (1008)"; the regular API says why.

    voicecheck.py           a sentence for people
    voicecheck.py --json    {"ok": ..., "problem": ..., "fix": ...} for glasses-doctor
    voicecheck.py --deepgram  the same for the Deepgram key, which hears you

The key comes from ELEVEN_API_KEY (or DEEPGRAM_API_KEY), or from ~/.claude-glasses-bridge/voice.env.
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
API = "https://api.elevenlabs.io/v1"
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


def key_from_env_file(names: tuple[str, ...] = ("ELEVEN_API_KEY", "ELEVENLABS_API_KEY"),
                      directory: Path | None = None) -> str | None:
    try:
        text = ((directory or DATA_DIR) / "voice.env").read_text(encoding="utf-8")
    except OSError:
        return None
    value = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        for name in names:
            if line.startswith(name + "="):
                value = line.split("=", 1)[1].strip().strip("'\"") or value
    return value


def check_deepgram(key: str, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    """Asks Deepgram (which hears you on a call) whether it accepts the key."""
    request = urllib.request.Request(
        "https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {key}"}, method="GET"
    )
    try:
        with opener(request, timeout=20) as response:
            response.read(64)
        return {"ok": True}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "status": "invalid_api_key", "problem": "Deepgram says the key isn't valid",
                    "fix": "create a new key on console.deepgram.com (API Keys) and run: "
                           "~/claude-glasses-bridge/bin/glasses-key DEEPGRAM_API_KEY"}
        if e.code == 403:
            # a real key that isn't allowed to list projects: fine for hearing, so don't call it wrong
            return {"ok": False, "status": "unchecked",
                    "problem": "Deepgram accepts the key but it isn't allowed to list projects, which hearing doesn't need",
                    "fix": "a real call is the real test"}
        if e.code == 402:
            return {"ok": False, "status": "payment_required", "problem": "the Deepgram account is out of credit",
                    "fix": "add credit on console.deepgram.com"}
        # anything else (such as the page moving) says nothing about the key
        return {"ok": False, "status": "unchecked", "problem": f"Deepgram answered {e.code}",
                "fix": "glasses-doctor tests it again later"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "status": "network", "problem": f"couldn't reach Deepgram: {e}",
                "fix": "check the Mac's internet connection"}


def _reply(e: urllib.error.HTTPError) -> tuple[str, str]:
    """(status, message) from an ElevenLabs error reply, whatever shape it comes in."""
    raw = e.read() or b""
    try:
        body = json.loads(raw)
    except ValueError:
        return "", raw.decode("utf-8", "replace").strip()[:200]
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("status") or ""), str(detail.get("message") or "")
    if isinstance(detail, list):
        return "", "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)[:200]
    if isinstance(detail, str):
        return "", detail
    return "", str(body)[:200]


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
        status, message = _reply(e)
        if not status and e.code == 401:
            status = "invalid_api_key"
        if status in FIXES:
            problem, fix = FIXES[status]
            return {"ok": False, "status": status, "problem": problem, "fix": fix, "message": message[:300]}
        # A reply we don't recognise says little about the key. Ask a simpler question: is it a key at all?
        refused = f"the test speech was refused ({e.code}{': ' + message if message else ''})"
        probe = urllib.request.Request(f"{API}/models", headers={"xi-api-key": key})
        try:
            with opener(probe, timeout=20) as response:
                response.read(64)
            return {"ok": True, "note": f"ElevenLabs accepts the key, but {refused}; a real call is the real test"}
        except urllib.error.HTTPError as p:
            p_status, p_message = _reply(p)
            if p.code == 401 and p_status in ("", "invalid_api_key"):
                problem, fix = FIXES["invalid_api_key"]
                return {"ok": False, "status": "invalid_api_key", "problem": problem, "fix": fix,
                        "message": (p_message or message)[:300]}
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        return {"ok": False, "status": "unchecked", "problem": f"ElevenLabs answered unexpectedly: {refused}",
                "fix": "a real call is the real test; send a screenshot of this message if the voice stays silent"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "status": "network", "problem": f"couldn't reach ElevenLabs: {e}",
                "fix": "check the internet connection"}


def main(argv: list[str]) -> int:
    deepgram = "--deepgram" in argv
    names = ("DEEPGRAM_API_KEY",) if deepgram else ("ELEVEN_API_KEY", "ELEVENLABS_API_KEY")
    label = "Deepgram" if deepgram else "ElevenLabs"
    key = next((os.environ[n] for n in names if os.environ.get(n)), None) or key_from_env_file(names)
    if not key:
        result: dict[str, Any] = {"ok": False, "status": "no_key", "problem": f"no {label} key is saved",
                                  "fix": f"run: ~/claude-glasses-bridge/bin/glasses-key {names[0]}"}
    elif deepgram:
        result = check_deepgram(key)
        result["key_ends"] = key[-4:]
    else:
        result = check(key, os.environ.get("GLASSES_ELEVENLABS_VOICE", DEFAULT_VOICE),
                       os.environ.get("GLASSES_ELEVENLABS_MODEL", DEFAULT_MODEL))
        result["key_ends"] = key[-4:]
    if "--json" in argv:
        print(json.dumps(result))
    elif result["ok"]:
        print(f"✓ {label} works with the key ending in {result['key_ends']}.")
        if result.get("note"):
            print(f"  ({result['note']})")
    else:
        print(f"✗ {label} has a problem: {result['problem']}.")
        print(f"  → {result['fix']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
