"""Butler backend — mounted at ``/api/plugins/butler/``.

``GET /state`` — the live guide state the desktop pane renders.
``GET /health`` — mount probe.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter


router = APIRouter()


def _state_candidates() -> list[Path]:
    out = []
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    out.append(Path(hermes_home) / "plugin-data" / "butler" / "state.json")
    out.append(Path(hermes_home) / "plugins" / "butler" / "data" / "state.json")
    return out


def _read_state() -> dict[str, Any]:
    for path in _state_candidates():
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return {}


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "plugin": "butler"}


@router.get("/state")
def state() -> dict[str, Any]:
    return _read_state()
