"""Finding the folders Claude Code works in.

On Windows, Desktop, Documents and the like often live inside OneDrive, so C:\\Users\\you\\Desktop may not
exist even though "the Desktop" does. Starting Claude Code in a folder that isn't there fails with the
unhelpful "WinError 267: The directory name is invalid", so look in the usual places first.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Mapping

KNOWN = ("Desktop", "Documents", "Downloads", "Pictures")
ONEDRIVE_VARIABLES = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def real_folder(path: str | os.PathLike[str], env: Mapping[str, str] | None = None, home: Path | None = None,
                exists: Callable[[Path], bool] = Path.is_dir) -> str | None:
    """The folder as it really is on this PC, or None if there is no such folder."""
    folder = Path(path).expanduser()
    if exists(folder):
        return str(folder)
    if folder.name in KNOWN:
        env = os.environ if env is None else env
        roots = [Path(env[v]) for v in ONEDRIVE_VARIABLES if env.get(v)] + [(home or Path.home()) / "OneDrive"]
        for root in roots:
            if exists(root / folder.name):
                return str(root / folder.name)
    return None


def work_folders(project: str | None, extra: list[str], say: Callable[[str], None],
                 env: Mapping[str, str] | None = None, home: Path | None = None,
                 exists: Callable[[Path], bool] = Path.is_dir) -> tuple[str, list[str]]:
    """The folder Claude Code starts in, and the others it may use, with what was wrong said in plain words.
    A missing main folder falls back to the home folder, so a call never dies over it."""
    home = home or Path.home()
    main = real_folder(project, env, home, exists) if project else None
    if project and main is None:
        say(f"  ! The folder {project} doesn't exist, so Claude Code will work in {home} instead.")
        say("    Pick a folder that exists, for example: glasses start $HOME\\Documents")
        main = str(home)
    elif project and Path(main) != Path(project).expanduser():
        say(f"  ({project} doesn't exist; your folder is {main}, so I'm using that.)")
    kept = []
    for folder in extra:
        found = real_folder(folder, env, home, exists)
        if found is None:
            say(f"  ! Skipping {folder}: there is no such folder.")
        elif found not in kept and found != main:
            kept.append(found)
    return main or str(home), kept
