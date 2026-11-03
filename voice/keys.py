"""`glasses key NAME`: save a key in voice.env without showing it, and check it with the service.

The Mac's bin/glasses-key does the same in bash. ElevenLabs and Deepgram keys are checked as they are
saved, and a key the service says is invalid is not kept.
"""
from __future__ import annotations

import getpass
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import envfile  # noqa: E402
import voicecheck  # noqa: E402

DATA_DIR = Path(os.environ.get("GLASSES_DATA_DIR", Path.home() / ".claude-glasses-bridge")).expanduser()


def save(name: str, ask: Callable[[str], str] = getpass.getpass, data_dir: Path | None = None,
         check_eleven: Callable[[str], dict[str, Any]] | None = None,
         check_deepgram: Callable[[str], dict[str, Any]] | None = None) -> int:
    data_dir = data_dir or DATA_DIR
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name or ""):
        print("usage: glasses key NAME   (for example: glasses key ELEVEN_API_KEY)", file=sys.stderr)
        return 1
    value = "".join((ask(f"Paste your {name} and press Enter: ") or "").split())
    if not value:
        print("Nothing was pasted, so nothing changed.", file=sys.stderr)
        return 1
    if name.endswith("_KEY") and (len(value) < 16 or not value.isprintable()):
        # real keys are 40 characters or more; a paste that went wrong is often one odd character
        print(f"That doesn't look like a key ({len(value)} character{'' if len(value) == 1 else 's'}; real ones are much "
              "longer). Nothing was saved.", file=sys.stderr)
        print("  Copy the key again, wait for the 'Paste your ...' line, then right-click in this window to paste.",
              file=sys.stderr)
        return 1
    file = data_dir / "voice.env"
    before = file.read_text(encoding="utf-8") if file.exists() else None
    envfile.set_value(file, name, value)
    print(f"Saved {name} (it ends in {value[-4:]}).")

    service = {"ELEVEN_API_KEY": "ElevenLabs", "ELEVENLABS_API_KEY": "ElevenLabs", "DEEPGRAM_API_KEY": "Deepgram"}.get(name)
    if service is None:
        print("Restart the glasses (glasses stop, then glasses start) to use it.")
        return 0
    print(f"Checking it with {service}...")
    if service == "Deepgram":
        result = (check_deepgram or voicecheck.check_deepgram)(value)
    else:
        result = (check_eleven or voicecheck.check)(value)
    status = result.get("status")
    if result.get("ok"):
        print(f"✓ {service} accepts this key. Restart the glasses to use it.")
        if result.get("note"):
            print(f"  ({result['note']})")
    elif status == "invalid_api_key":
        if before is None:
            file.unlink(missing_ok=True)
        else:
            file.write_text(before, encoding="utf-8")
        print(f"✗ {service} says this key isn't valid, so I did not keep it.", file=sys.stderr)
        if service == "Deepgram":
            print("  On console.deepgram.com open API Keys, create a new key, and copy it right away.", file=sys.stderr)
        else:
            print("  On elevenlabs.io open API Keys, click Create API Key, and copy the new key right away", file=sys.stderr)
            print("  (it is only shown once).", file=sys.stderr)
        print("  Then run this again and paste it.", file=sys.stderr)
        return 1
    elif status in ("network", "unchecked"):
        print(f"(Couldn't check it with {service}: {result.get('problem')}. It is saved; glasses doctor tests it later.)")
    else:
        print(f"! The key itself is fine, but: {result.get('problem')}.", file=sys.stderr)
        print(f"  {result.get('fix', '')}", file=sys.stderr)
        print("  Saved anyway.", file=sys.stderr)
    return 0
