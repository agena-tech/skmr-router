#!/usr/bin/env python3
"""SKMR entrypoint. Every `/skmr:<command>` runs through this file.

    /usr/bin/python3 /root/.claude/skmr/cli.py <command> [args...]

The package directory's parent is put on sys.path so `import skmr` resolves when
the file is executed directly rather than as a module.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PARENT = str(Path(__file__).resolve().parent.parent)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from skmr.core.dispatcher import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
