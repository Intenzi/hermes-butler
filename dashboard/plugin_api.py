"""Butler backend — mounted at ``/api/plugins/butler/``.

``GET /state`` — the live guide state the desktop pane renders.
``GET /history`` — finished guide rows, newest first (cap 50).
``GET /health`` — mount probe.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter


router = APIRouter()

HISTORY_LIMIT = 50


def _state_candidates() -> list[Path]:
    out = []
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    out.append(Path(hermes_home) / "plugin-data" / "butler" / "state.json")
    out.append(Path(hermes_home) / "plugins" / "butler" / "data" / "state.json")
    return out


def _guides_candidates() -> list[Path]:
    """The guides folder beside each state candidate, in /state's order."""
    out = []
    for state_path in _state_candidates():
        guides = state_path.parent / "guides"
        if guides not in out:
            out.append(guides)
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


def _started_at_key(value: Any) -> float:
    """Sort mixed epoch seconds/milliseconds/ISO strings without raising."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return number / 1000.0 if number > 100_000_000_000 else number
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            try:
                from datetime import datetime

                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0
        return number / 1000.0 if number > 100_000_000_000 else number
    return 0.0


def _history_entry(data: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    verifications = (data.get("verifications")
                     if isinstance(data.get("verifications"), list) else [])
    return {
        "id": str(data.get("id") or fallback_id),
        "title": data.get("title") or "Guide",
        "status": state.get("status") or data.get("status") or "unknown",
        "steps": steps,
        "verifications": verifications,
        "started_at": data.get("started_at"),
    }


def _read_history() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for guides_dir in _guides_candidates():
        if not guides_dir.is_dir():
            continue
        try:
            paths = sorted(guides_dir.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            entry = _history_entry(data, path.stem)
            if entry["id"] in seen:
                continue
            seen.add(entry["id"])
            rows.append(entry)
    rows.sort(key=lambda row: _started_at_key(row.get("started_at")), reverse=True)
    return rows[:HISTORY_LIMIT]


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "plugin": "butler"}


@router.get("/state")
def state() -> dict[str, Any]:
    return _read_state()


@router.get("/history")
def history() -> list[dict[str, Any]]:
    return _read_history()
