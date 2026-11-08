"""Butler backend — mounted at ``/api/plugins/butler/``.

``GET /state`` — the live guide state the desktop pane renders.
``GET /history`` — finished guide rows, newest first (cap 50).
``GET /health`` — mount probe.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException


router = APIRouter()

HISTORY_LIMIT = 50
CONFIG_KEY = "summon.pet_click_claim"


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


def _config_candidates() -> list[Path]:
    """plugin-data config first, mirroring the engine's data-dir order."""
    return [state_path.parent / "config.json" for state_path in _state_candidates()]


def _config_path() -> Path:
    """The path the engine polls (plugin-data/butler/config.json)."""
    return _config_candidates()[0]


def _read_config() -> dict[str, Any]:
    for path in _config_candidates():
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


def _pet_click_claim(cfg: dict[str, Any]) -> bool:
    """Read the nested knob, with the flat dotted spelling as a fallback."""
    value: Any = None
    summon = cfg.get("summon")
    if isinstance(summon, dict):
        value = summon.get("pet_click_claim")
    if value is None:
        value = cfg.get(CONFIG_KEY)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1")
    return bool(value)


def _write_config(pet_click_claim: bool) -> dict[str, Any] | None:
    """Merge and atomically persist the summon knob in the plugin-data config."""
    path = _config_path()
    cfg = _read_config()
    summon = cfg.get("summon") if isinstance(cfg.get("summon"), dict) else {}
    summon = dict(summon)
    summon["pet_click_claim"] = bool(pet_click_claim)
    cfg["summon"] = summon
    # Keep the flat spelling out of the file; the nested one is canonical.
    cfg.pop(CONFIG_KEY, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        return None
    return cfg


def _load_overlay_module():
    """Load the plugin's own overlay controller without importing core."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from butler_core import overlay as bp_overlay

    return bp_overlay


def _summon_pet_double() -> bool:
    try:
        bp_overlay = _load_overlay_module()
        if bp_overlay.resolve_frames_dir() is None:
            # Petless: the bar and the send still worked; skip the double.
            return False
        threading.Thread(target=bp_overlay.summon_double,
                         name="butler-summon-double", daemon=True).start()
        return True
    except Exception:
        return False


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


@router.get("/config")
def config() -> dict[str, Any]:
    return {"summon": {"pet_click_claim": _pet_click_claim(_read_config())}}


@router.post("/config")
def set_config(body: dict[str, Any]) -> dict[str, Any]:
    requested = None
    summon = body.get("summon")
    if isinstance(summon, dict) and "pet_click_claim" in summon:
        requested = summon.get("pet_click_claim")
    elif "pet_click_claim" in body:
        requested = body.get("pet_click_claim")
    elif CONFIG_KEY in body:
        requested = body.get(CONFIG_KEY)
    if isinstance(requested, str):
        enabled = requested.strip().lower() in ("true", "yes", "on", "1")
    elif requested is None:
        enabled = _pet_click_claim(_read_config())
    else:
        enabled = bool(requested)
    cfg = _write_config(enabled)
    if cfg is None:
        raise HTTPException(status_code=500, detail="could not write the butler config")
    return {"summon": {"pet_click_claim": _pet_click_claim(cfg)}}


@router.post("/summon/pet")
def summon_pet() -> dict[str, Any]:
    spawned = _summon_pet_double()
    return {"ok": True, "pet": bool(spawned)}


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "plugin": "butler"}


@router.get("/state")
def state() -> dict[str, Any]:
    return _read_state()


@router.get("/history")
def history() -> list[dict[str, Any]]:
    return _read_history()
