"""Install-safe entry: every skill script starts here to find the vendored libs.

The skill ships `cryptography` under `<skills-root>/.vendor` so the scripts run
without touching the system Python. This module prepends that directory to
`sys.path` and is imported by `tmall_session.py` and the two EnsureLogin
wrappers. Differences between the macOS and Windows hosts are handled inside
`tmall_session.py`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    override = os.environ.get("TMALL_SKILL_VENDOR")
    if override:
        dirs.append(Path(override).expanduser())
    here = Path(__file__).resolve().parent
    # <skills-root>/.vendor when scripts live in <skills-root>/<skill>/scripts.
    dirs.append(here.parents[1] / ".vendor")
    dirs.append(here.parent / "vendor")
    return dirs


def ensure_vendor_path() -> list[Path]:
    """Prepend existing vendored library directories to sys.path (idempotent)."""
    added: list[Path] = []
    for directory in _candidate_dirs():
        if not directory.is_dir():
            continue
        text = str(directory)
        if text not in sys.path:
            sys.path.insert(0, text)
            added.append(directory)
    return added


ensure_vendor_path()
