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


def clean(text: str) -> str:
    """What was pasted, without spaces, line breaks or stray control characters (Ctrl+V can leave one)."""
    return "".join(ch for ch in text if ch.isprintable() and not ch.isspace())


def windows_clipboard() -> str:
    """The text on the Windows clipboard, or "" if it can't be read."""
    try:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32  # type: ignore[attr-defined]
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
            pointer = kernel32.GlobalLock(handle) if handle else None
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""


def read_secret(prompt: str, getch: Callable[[], str] | None = None,
                clipboard: Callable[[], str] = windows_clipboard, write: Callable[[str], object] | None = None) -> str:
    """Hidden input. On Windows, getpass stores Ctrl+V as a control character instead of pasting, so read the
    keys ourselves: Ctrl+V pastes from the clipboard, and right-click paste works too."""
    if getch is None:
        if os.name != "nt":
            return getpass.getpass(prompt)
        import msvcrt

        getch = msvcrt.getwch  # type: ignore[attr-defined]
    out = write or (lambda text: (sys.stdout.write(text), sys.stdout.flush()))
    out(prompt)
    chars: list[str] = []
    while True:
        ch = getch()
        if ch in ("\r", "\n"):
            break
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\b", "\x7f"):
            if chars:
                chars.pop()
        elif ch == "\x16":  # Ctrl+V
            chars.extend(clipboard())
        elif ch in ("\x00", "\xe0"):  # the first half of an arrow or function key
            getch()
        elif ch.isprintable():
            chars.append(ch)
    out("\n")
    return "".join(chars)


def key_shape_note(name: str, value: str) -> str | None:
    """A hint when a key is not the shape the service's keys have, which usually means part of it was not copied.
    Only a hint: shown, never used to refuse a key."""
    if name not in ("ELEVEN_API_KEY", "ELEVENLABS_API_KEY"):
        return None
    if re.fullmatch(r"sk_[0-9a-f]{48}", value):
        return None
    return (f"(Note: the key you pasted is {len(value)} characters and starts with {value[:3]!r}. ElevenLabs keys are "
            "usually 51 characters starting with 'sk_'. If yours is shorter, or it has dots in it, the whole key wasn't "
            "copied: use the Copy button in the box that appears when you create the key.)")


def save(name: str, ask: Callable[[str], str] = read_secret, data_dir: Path | None = None,
         check_eleven: Callable[[str], dict[str, Any]] | None = None,
         check_deepgram: Callable[[str], dict[str, Any]] | None = None) -> int:
    data_dir = data_dir or DATA_DIR
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name or ""):
        print("usage: glasses key NAME   (for example: glasses key ELEVEN_API_KEY)", file=sys.stderr)
        return 1
    value = clean(ask(f"Paste your {name} and press Enter: ") or "")
    if not value:
        print("Nothing was pasted, so nothing changed. Copy the key again, then paste it here "
              "(Ctrl+V or right-click) and press Enter.", file=sys.stderr)
        return 1
    if name.endswith("_KEY") and len(value) < 16:
        # real keys are 40 characters or more
        print(f"That doesn't look like a key ({len(value)} character{'' if len(value) == 1 else 's'}; real ones are much "
              "longer). Nothing was saved.", file=sys.stderr)
        print("  Copy the whole key again, then paste it here (Ctrl+V or right-click) and press Enter.", file=sys.stderr)
        return 1
    file = data_dir / "voice.env"
    before = file.read_text(encoding="utf-8") if file.exists() else None
    envfile.set_value(file, name, value)
    print(f"Saved {name} (it ends in {value[-4:]}).")

    service = {"ELEVEN_API_KEY": "ElevenLabs", "ELEVENLABS_API_KEY": "ElevenLabs", "DEEPGRAM_API_KEY": "Deepgram"}.get(name)
    if service is None:
        print("Restart the glasses (glasses stop, then glasses start) to use it.")
        return 0
    shape = key_shape_note(name, value)
    if shape:
        print(shape)
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
        if result.get("message"):
            print(f"  {service} said: {result['message']}", file=sys.stderr)
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
