"""Reading and writing KEY=value settings files such as ~/.claude-glasses-bridge/voice.env."""
from __future__ import annotations

import os
from pathlib import Path


def parse(text: str) -> dict[str, str]:
    """KEY=value lines; `export` is allowed, quotes are removed, # starts a comment line."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def read(path: Path) -> dict[str, str]:
    try:
        return parse(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def set_value(path: Path, name: str, value: str) -> None:
    """Sets one value, replacing any earlier one and keeping every other line."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    kept = [l for l in lines if not _sets(l, name)]
    kept.append(f"{name}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(kept) + "\n")
    os.replace(tmp, path)


def _sets(line: str, name: str) -> bool:
    line = line.strip()
    if line.startswith("export "):
        line = line[7:].lstrip()
    return line.startswith(name + "=")
