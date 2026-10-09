"""Optional config file → environment variables.

Reads KEY=VALUE lines from $OWN_READER_CONFIG, or <repo>/config.conf if it exists, and sets them with
os.environ.setdefault (so real environment variables always win). See config.example.conf for every key.
Imported first by app.py / llm.py, before they read the environment.
"""
from __future__ import annotations

import os
from pathlib import Path


def load(path: str | os.PathLike | None = None) -> None:
    p = Path(path or os.environ.get("OWN_READER_CONFIG") or Path(__file__).resolve().parent.parent / "config.conf")
    p = p.expanduser()
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def path(key: str, default: str | None = None) -> Path | None:
    """Env var as an expanded Path; None when unset and no default."""
    v = os.environ.get(key) or default
    return Path(v).expanduser() if v else None


load()
