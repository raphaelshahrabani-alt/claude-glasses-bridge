#!/usr/bin/env python3
"""Run the glasses assistant on a PC: `py glasses.py start`, or glasses.cmd start. See `glasses help`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "voice"))

from glasses_cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
