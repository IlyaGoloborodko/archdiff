#!/usr/bin/env python3
"""Compatibility wrapper for running archdiff from a source checkout."""

from pathlib import Path
import sys

SOURCE_DIR = Path(__file__).resolve().parent / "src"
if SOURCE_DIR.exists():
    sys.path.insert(0, str(SOURCE_DIR))

from archdiff.cli import main


if __name__ == "__main__":
    raise SystemExit(main())