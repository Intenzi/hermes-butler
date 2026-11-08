"""One source of truth for the plugin's runtime data directory.

The engine, the dashboard API, and the desktop config watcher must all read
and write the SAME ``config.json``/``state.json``. Hermes reserves
``<hermes home>/plugin-data/<plugin>/`` for this (the plugin install dir is
git-pulled/removed by updates), so resolve it the same way the host does.
"""
from __future__ import annotations

import os
from pathlib import Path

PLUGIN_NAME = "butler"


def hermes_home() -> Path:
    """The active profile home, honoring HERMES_HOME for tests/profiles."""
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override).expanduser()
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def plugin_data_dir(name: str = PLUGIN_NAME) -> Path:
    """Return (and create) the host-owned per-plugin data directory."""
    try:
        from plugins.plugin_storage import plugin_data_dir as host_dir  # type: ignore

        return Path(host_dir(name))
    except Exception:
        root = hermes_home() / "plugin-data" / name
        root.mkdir(parents=True, exist_ok=True)
        return root


def config_path() -> Path:
    return plugin_data_dir() / "config.json"


def state_path() -> Path:
    return plugin_data_dir() / "state.json"


def guides_dir() -> Path:
    return plugin_data_dir() / "guides"
