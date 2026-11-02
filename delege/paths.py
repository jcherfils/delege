"""Where delege keeps its files on a machine (never in a project repository).

- configuration: ``~/.config/delege/`` (``$XDG_CONFIG_HOME/delege``): ``config.json`` (hosts, parallel, cloud
  switch, models on / off, model overrides, identity) and ``secrets.json`` (API keys when no OS keyring; mode 0600);
- state: ``~/.local/share/delege/`` (``$XDG_DATA_HOME/delege``): ``worktrees/``, ``reports/``, ``usage.json``.

``$DELEGE_HOME`` puts both in one folder (configuration at its root, state under ``data/``): tests, or a machine
with several owners. Everything is computed on each call, so a changed environment is followed.
"""
from __future__ import annotations

import os
from pathlib import Path

REMOTE_DATA = ".local/share/delege"         # on an ssh machine, relative to its home


def _xdg(var: str, default: str) -> Path:
    v = os.environ.get(var)
    return Path(v) if v else Path.home() / default


def config_dir() -> Path:
    h = os.environ.get("DELEGE_HOME")
    return Path(h) if h else _xdg("XDG_CONFIG_HOME", ".config") / "delege"


def data_dir() -> Path:
    h = os.environ.get("DELEGE_HOME")
    return Path(h) / "data" if h else _xdg("XDG_DATA_HOME", ".local/share") / "delege"


def config_file() -> Path:
    return config_dir() / "config.json"


def secrets_file() -> Path:
    return config_dir() / "secrets.json"


def usage_file() -> Path:
    return data_dir() / "usage.json"


def worktrees_dir() -> Path:
    return data_dir() / "worktrees"


def reports_dir() -> Path:
    return data_dir() / "reports"


def opencode_auth() -> Path:
    """OpenCode's own key store (``/connect``), read only."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "opencode" / "auth.json"


def legacy_dir() -> Path:
    """FiFoo's old folder (``$FIFOO_HOME`` or ``~/.fifoo``): read once, as a migration."""
    h = os.environ.get("FIFOO_HOME")
    return Path(h) if h else Path.home() / ".fifoo"
