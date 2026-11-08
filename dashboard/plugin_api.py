"""Butler backend — mounted at ``/api/plugins/butler/``.

``GET /state`` — the live guide state the desktop pane renders.
``GET /history`` — finished guide rows, newest first (cap 50).
``POST /summon/send`` — inject a framed butler turn into a selected session.
``GET /health`` — mount probe.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butler_core import paths as bp_paths  # noqa: E402  (import after sys.path setup)


router = APIRouter()
logger = logging.getLogger("hermes.plugins.butler.api")

HISTORY_LIMIT = 50
CONFIG_KEY = "summon.pet_click_claim"


def _state_candidates() -> list[Path]:
    """The ONE host-owned state file (never the plugin install dir)."""
    return [bp_paths.state_path()]


def _guides_candidates() -> list[Path]:
    """The guides folder beside the canonical state file."""
    return [bp_paths.guides_dir()]


def _config_candidates() -> list[Path]:
    """The ONE host-owned config file the engine also polls."""
    return [bp_paths.config_path()]


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


def _plugin_context():
    """The register()-time PluginContext for this plugin, wherever it loaded.

    In-process, the plugin module owns the context as a module global; under
    ``plugins.isolation: host`` the same module runs in the plugin host child and
    the route is forwarded there. Scan sys.modules by the module's real file so
    neither host needs a package-name assumption.
    """
    root = Path(__file__).resolve().parents[1]
    try:
        init_path = (root / "__init__.py").resolve()
    except OSError:
        return None
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        try:
            if Path(filename).resolve() != init_path:
                continue
        except OSError:
            continue
        getter = getattr(module, "get_plugin_context", None)
        if not callable(getter):
            continue
        try:
            context = getter()
        except Exception:
            continue
        if context is not None:
            return context
    return None


def _summon_frame(text: str) -> str:
    """The shared summon frame (imported from the plugin body, not copied)."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from butler_core.summon import frame_summon_text

    return frame_summon_text(text)


def _load_overlay_module():
    """Load the plugin's own overlay controller without importing core."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from butler_core import overlay as bp_overlay

    return bp_overlay


def _engine_module():
    """Load the plugin's engine (claim-chip + summon-pending owner)."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from butler_core import engine as bp_engine

    return bp_engine


_CLAIM_SYNCING = False
_CLAIM_SYNC_LOCK = threading.Lock()


def _sync_claim_chip(enabled: bool) -> dict[str, Any]:
    """Best-effort: arm/disarm the companion's pop-out click chip.

    Arming may compile/start the companion on first use, so it runs off the
    gateway request thread; a disabled chip is hidden synchronously. The
    desktop polls this every few seconds, so a repeated arm while the first
    start is in flight is a no-op rather than another process/build.
    """
    global _CLAIM_SYNCING
    try:
        bp_engine = _engine_module()
    except Exception:
        logger.exception("butler claim-chip engine unavailable")
        return {"enabled": bool(enabled), "running": False, "detail": "engine unavailable"}

    if not enabled:
        with _CLAIM_SYNC_LOCK:
            _CLAIM_SYNCING = False
        try:
            return bp_engine.set_claim_chip(False)
        except Exception:
            logger.exception("butler claim-chip disarm failed")
            return {"enabled": False, "running": False, "detail": "engine unavailable"}

    with _CLAIM_SYNC_LOCK:
        if _CLAIM_SYNCING:
            return {"enabled": True, "running": False, "detail": "starting"}
        _CLAIM_SYNCING = True

    def _arm() -> None:
        global _CLAIM_SYNCING
        try:
            bp_engine.set_claim_chip(True)
        except Exception:
            logger.exception("butler claim-chip arm failed")
        finally:
            with _CLAIM_SYNC_LOCK:
                _CLAIM_SYNCING = False

    threading.Thread(target=_arm, name="butler-claim-chip", daemon=True).start()
    return {"enabled": True, "running": False, "detail": "starting"}


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


def _gateway_runner(ctx):
    """The live gateway runner published for plugin injection, or None.

    ``PluginContext._manager`` carries the same owner/injector tuple
    ``hermes_cli.plugins.publish_gateway_message_host`` stamps, so the runner
    (and its ``session_store``) is reachable without importing gateway state.
    """
    manager = getattr(ctx, "_manager", None)
    host = getattr(manager, "_gateway_message_injector", None)
    if not (isinstance(host, tuple) and len(host) == 2 and host[0] is not None):
        try:
            from hermes_cli import plugins as hermes_plugins

            host = getattr(hermes_plugins, "_published_gateway_message_injector", None)
        except Exception:
            host = None
    if isinstance(host, tuple) and len(host) == 2 and host[0] is not None:
        return host[0]
    return None


def _entry_session_key(entry: Any) -> str | None:
    if isinstance(entry, dict):
        value = entry.get("session_key")
    else:
        value = getattr(entry, "session_key", None)
    return value if isinstance(value, str) and value.strip() else None


def _generated_local_route(store: Any, session_id: str) -> str | None:
    """Ask the store's own key builder for the local DM route, if it exists.

    This is only a fallback after a direct ``lookup_by_session_id`` miss: the
    key template lives in ``gateway.session`` and the profile resolution lives
    in the store, never in this plugin.
    """
    generate = getattr(store, "_generate_session_key", None)
    if not callable(generate):
        return None
    try:
        from gateway.config import Platform
        from gateway.session import SessionSource

        source = SessionSource(platform=Platform.LOCAL, chat_type="dm", chat_id=session_id)
        route = generate(source)
    except Exception:
        return None
    if not isinstance(route, str) or not route.strip():
        return None
    # A generated key only counts when the same store can resolve it.
    lookup_key = getattr(store, "lookup_by_session_key", None)
    if callable(lookup_key):
        try:
            return route if lookup_key(route) is not None else None
        except Exception:
            return None
    return route


def _resolve_session_route(ctx, session_id: str) -> tuple[str | None, str | None]:
    """Derive the gateway routing key for a desktop session id.

    Returns ``(route, detail)``. ``route`` is None when the gateway cannot
    resolve the id; ``detail`` then carries the honest reason. A live runner's
    store is authoritative; a bare ``local-…`` id is never used as a route.
    """
    runner = _gateway_runner(ctx)
    if runner is None:
        # No gateway host in this process: preserve the old caller-keyed path.
        return session_id, None
    store = getattr(runner, "session_store", None)
    if store is None:
        return None, f"gateway has no session store to resolve {session_id}"

    lookup_id = getattr(store, "lookup_by_session_id", None)
    if callable(lookup_id):
        try:
            route = _entry_session_key(lookup_id(session_id))
        except Exception as exc:  # noqa: BLE001 - report, never 500 the send
            return None, f"gateway session lookup failed for {session_id}: {exc}"
        if route:
            return route, None

    # Already a routing key? Accept it only when the store confirms the row.
    lookup_key = getattr(store, "lookup_by_session_key", None)
    if callable(lookup_key):
        try:
            if lookup_key(session_id) is not None:
                return session_id, None
        except Exception:
            pass

    route = _generated_local_route(store, session_id)
    if route:
        return route, None
    return None, f"no gateway session route for {session_id}"


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


@router.post("/summon/claim")
def summon_claim(body: dict[str, Any]) -> dict[str, Any]:
    """Arm/disarm the companion's pop-out pet click chip.

    The desktop calls this after each ``/config`` sync, so a config-file value
    survives a gateway reload without a second manual toggle.
    """
    requested = body.get("enabled") if isinstance(body, dict) else None
    if requested is None:
        enabled = _pet_click_claim(_read_config())
    elif isinstance(requested, str):
        enabled = requested.strip().lower() in ("true", "yes", "on", "1")
    else:
        enabled = bool(requested)
    return {"ok": True, **_sync_claim_chip(enabled)}


@router.post("/summon/ack")
def summon_ack(body: dict[str, Any]) -> dict[str, Any]:
    """Consume a companion-click summon after the desktop opened the bar."""
    pending_id = body.get("id") if isinstance(body, dict) else None
    pending_id = pending_id.strip() if isinstance(pending_id, str) else ""
    try:
        cleared = _engine_module().consume_summon_pending(pending_id or None)
    except Exception:
        logger.exception("butler summon ack failed")
        cleared = False
    return {"ok": True, "cleared": bool(cleared)}


@router.post("/summon/pet")
def summon_pet() -> dict[str, Any]:
    spawned = _summon_pet_double()
    return {"ok": True, "pet": bool(spawned)}


@router.post("/summon/send")
def summon_send(body: dict[str, Any]) -> dict[str, Any]:
    """Inject a framed butler turn into the session the desktop selected.

    The desktop resolves the destination (oldest Butler chat, or the fresh one it
    just created) and sends its durable id. ``ctx.inject_message`` fails closed
    when gateway injection is not allowed; the contract is a stable
    ``{ok: false, reason: "injection_denied"}`` so the desktop can fall back to
    the honest composer/slash path.
    """
    text = body.get("text") if isinstance(body, dict) else None
    session_id = body.get("session_id") if isinstance(body, dict) else None
    text = text.strip() if isinstance(text, str) else ""
    session_id = session_id.strip() if isinstance(session_id, str) else ""
    if not text or not session_id:
        raise HTTPException(status_code=400, detail="text and session_id are required")

    ctx = _plugin_context()
    inject = getattr(ctx, "inject_message", None)
    if not callable(inject):
        return {"ok": False, "reason": "injection_denied"}
    route, detail = _resolve_session_route(ctx, session_id)
    if route is None:
        return {"ok": False, "reason": "unroutable_session", "detail": detail}
    try:
        accepted = bool(inject(_summon_frame(text), role="user", session_key=route))
    except Exception:
        logger.exception("butler summon injection failed")
        accepted = False
    if not accepted:
        return {"ok": False, "reason": "injection_denied",
                "detail": f"gateway refused session_key {route}"}
    return {"ok": True, "session": session_id, "session_key": route}


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "plugin": "butler"}


@router.get("/state")
def state() -> dict[str, Any]:
    return _read_state()


@router.get("/history")
def history() -> list[dict[str, Any]]:
    return _read_history()
