"""Butler backend — mounted at ``/api/plugins/butler/``.

``GET /state`` — the live guide state the desktop pane renders.
``GET /history`` — finished guide rows, newest first (cap 50).
``POST /summon/send`` — inject a framed butler turn into a selected session.
``POST /watch/stop`` — stop the live watch and clear its pane row.
``POST /watch/ack`` — clear the hit announcement (works after the watch ends).
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
    """Read the nested knob, with the flat dotted spelling as a fallback.

    A missing key means on: installing the plugin is the opt-in. Only a stored
    ``false`` (or an off spelling) disarms the chip as a manual escape hatch.
    """
    value: Any = None
    summon = cfg.get("summon")
    if isinstance(summon, dict):
        value = summon.get("pet_click_claim")
    if value is None:
        value = cfg.get(CONFIG_KEY)
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip().lower()
        return text not in ("false", "no", "off", "0", "")
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


def _summon_frame(text: str, origin: str = "") -> str:
    """The shared frame for this row (imported from the plugin body, not copied).

    A LOOK row gets the frame that also asks the agent for ``butler_say``; every
    other row keeps the plain summon frame.
    """
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from butler_core.summon import frame_for

    return frame_for(text, origin)


def _load_overlay_module():
    """Load the plugin's own overlay controller without importing core."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from butler_core import overlay as bp_overlay

    return bp_overlay


def _engine_module():
    """Load the plugin's engine (claim-chip + summon-pending owner).

    This process serves ``/summon/claim`` and therefore OWNS the companion that
    reports chip clicks, but ``register()`` — which normally calls
    ``engine.init()`` — runs in the agent runtime process. Without an init here
    the chip arms fine and every click is dropped on the floor: no data dir, no
    ``on_event`` handler. Initialise it on first use; ``init`` is idempotent.
    """
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from butler_core import engine as bp_engine

    if getattr(bp_engine, "DATA_DIR", None) is None:
        try:
            from butler_core import paths as bp_paths

            bp_engine.init(bp_paths.plugin_data_dir("butler"))
            logger.info("butler engine initialised in the plugin API process")
        except Exception:
            logger.exception("butler engine init failed in the plugin API process")
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


def _spool_path() -> Path:
    """The ask-field spool the companion appends to.

    File-based on purpose: the companion lives behind a pipe owned by whichever
    plugin host spawned it, and that handle has gone stale more than once (the
    companion writes, nobody reads, no error anywhere). A file cannot lose its
    reader.
    """
    return bp_paths.plugin_data_dir("butler") / "summon-spool.jsonl"


def _spool_pending() -> dict[str, Any] | None:
    """The newest unacknowledged ask-field request, or None."""
    path = _spool_path()
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict) and row.get("id") and isinstance(row.get("text"), str):
            if row["text"].strip():
                # Same contract as the engine's pipe event: only an exact
                # "new" asks for a fresh chat; anything else is the default.
                destination = "new" if row.get("destination") == "new" else "recent"
                # The ask field writes origin "chip" implicitly; the ⌘⇧L door
                # stamps origin "look" (or is recognisable by its op) and
                # carries the hit-tested element. Both pass through unchanged so
                # the desktop can tell them apart.
                origin = row.get("origin")
                if not isinstance(origin, str) or not origin.strip():
                    origin = "look" if row.get("op") == "look_text" else "chip"
                pending = {
                    "origin": origin,
                    "id": str(row["id"]),
                    "text": row["text"],
                    "destination": destination,
                    "at": row.get("at"),
                }
                if "target" in row:
                    pending["target"] = row["target"]
                return pending
    return None


def _spool_ack(pending_id: str) -> bool:
    """Drop one row from the spool. Rewrites atomically; never raises."""
    path = _spool_path()
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return False
    kept: list[str] = []
    removed = False
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict) and str(row.get("id")) == pending_id:
            removed = True
            continue
        kept.append(line)
    if not removed:
        return False
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _say_path() -> Path:
    """The agent's short on-screen answer (butler_say), beside the spool."""
    return bp_paths.plugin_data_dir("butler") / "say.json"


def _read_say() -> dict[str, Any] | None:
    """The last answer the agent asked the pet to speak, read fresh each call.

    A separate file on purpose: the engine owns the write from whichever process
    ran the tool, and a state.json write from the guide half can never drop it.
    """
    try:
        row = json.loads(_say_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    say_id = row.get("id")
    text = row.get("text")
    if not isinstance(say_id, str) or not say_id.strip():
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    return {"id": say_id.strip(), "text": text, "at": row.get("at")}


def _drop_stale_guide(data: dict[str, Any]) -> dict[str, Any]:
    """Drop a terminal guide block left behind by a crashed/old process.

    The guide itself stays in the guides folder (and therefore in /history);
    only the live-status block stops talking after the engine's sweep age.
    A block with no readable timestamp is kept: there is no evidence it is old.
    """
    guide = data.get("guide")
    if not isinstance(guide, dict):
        return data
    try:
        from butler_core import engine as bp_engine

        stale = bp_engine.guide_block_is_stale(
            guide, updated_at=data.get("generated_at"))
    except Exception:  # never let the sweep break GET /state
        logger.exception("butler stale-guide sweep failed")
        return data
    if stale:
        data.pop("guide", None)
    return data


def _read_state() -> dict[str, Any]:
    data: dict[str, Any] = {}
    for path in _state_candidates():
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            continue
        if isinstance(state, dict):
            data = state
            break
    # A terminal block older than the sweep age must not outlive the process
    # that wrote it. Read-time sweep, so no writer has to run for the pet to
    # stop carrying a dead guide's line.
    data = _drop_stale_guide(data)
    # The companion's ask field is a file spool, not a pipe event: merge the
    # newest unacked row in so the desktop sees it through the same /state row
    # it already polls.
    if not data.get("summon_pending"):
        spooled = _spool_pending()
        if spooled is not None:
            data["summon_pending"] = spooled
    # The agent's on-screen answer rides the same snapshot. The desktop speaks
    # it once per id, then the guide status line resumes.
    said = _read_say()
    if said is not None:
        data["say"] = said
    return data


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
    entry = {
        "id": str(data.get("id") or fallback_id),
        "title": data.get("title") or "Guide",
        "status": state.get("status") or data.get("status") or "unknown",
        "steps": steps,
        "verifications": verifications,
        "started_at": data.get("started_at"),
    }
    # Terminal rows keep their reason (stopped / superseded / abandoned), so
    # history can explain why a guide ended. Only terminal rows: a running or
    # stalled guide's state.note is a transient instruction, not an ending.
    if entry["status"] in ("stopped", "superseded", "abandoned"):
        reason = data.get("reason") or state.get("note")
        if isinstance(reason, str) and reason.strip():
            entry["reason"] = reason.strip()
    return entry


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

    The desktop calls this after each ``/config`` sync. A missing ``enabled``
    means on (installing the plugin is the opt-in); only an explicit stored
    ``false`` disarms, and that escape hatch survives a gateway reload.
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
    # The ask-field row lives in the file spool; clearing it is what stops the
    # same text being sent twice.
    if pending_id:
        cleared = _spool_ack(pending_id) or cleared
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
    origin = body.get("origin") if isinstance(body, dict) else None
    text = text.strip() if isinstance(text, str) else ""
    session_id = session_id.strip() if isinstance(session_id, str) else ""
    origin = origin.strip().lower() if isinstance(origin, str) else ""
    if not text or not session_id:
        raise HTTPException(status_code=400, detail="text and session_id are required")

    ctx = _plugin_context()
    inject = getattr(ctx, "inject_message", None)
    if not callable(inject):
        return {"ok": False, "reason": "injection_denied"}
    route, detail = _resolve_session_route(ctx, session_id)
    if route is None:
        return {"ok": False, "reason": "unroutable_session", "detail": detail}
    # A LOOK row came from the ⌘⇧L door: its turn also asks for butler_say, so
    # the short answer lands beside the pointer instead of only in the chat.
    frame = _summon_frame(text, origin)
    try:
        accepted = bool(inject(frame, role="user", session_key=route))
    except Exception:
        logger.exception("butler summon injection failed")
        accepted = False
    if not accepted:
        return {"ok": False, "reason": "injection_denied",
                "detail": f"gateway refused session_key {route}"}
    return {"ok": True, "session": session_id, "session_key": route}


@router.post("/watch/ack")
def watch_ack() -> dict[str, Any]:
    """Acknowledge the hit announcement, which outlives its watch.

    The user's acknowledgement door: a hit writes a top-level ``watch_hit``
    block, so this works when no watch is running — the normal case after a
    one-shot hit (the watch ended, the announcement did not). Delegates to the
    same ``engine.acknowledge_watch_hit()`` the ``butler_watch_ack`` tool and the
    hat menu use. In sticky mode it also clears the live row's hit.
    ``acknowledged`` is true only when an announcement (or sticky hit) was
    actually cleared; false (never a 500) when there is nothing to clear — the
    engine's ``status`` says which (``no-hit``, ``stopped``, ``idle``).
    """
    try:
        acked = _engine_module().acknowledge_watch_hit()
    except Exception:
        logger.exception("butler watch ack failed")
        return {"ok": True, "acknowledged": False, "status": "unavailable"}
    payload = acked if isinstance(acked, dict) else {}
    return {"ok": True, **payload,
            "acknowledged": bool(payload.get("acknowledged"))}


@router.post("/watch/stop")
def watch_stop() -> dict[str, Any]:
    """Stop the live watch and clear its state row (the pane's Stop button).

    Delegates to the same ``engine.stop_watch()`` the ``butler_watch_stop`` tool
    calls, so the button and the tool cannot drift. ``ok`` means the request was
    answered; the engine's own ``status`` says what happened (``stopped``,
    ``cleared``, ``idle``, or ``elsewhere`` when another live plugin process
    owns the watch). Never raises: a missing companion is reported, not 500'd.
    """
    try:
        stopped = _engine_module().stop_watch()
    except Exception:
        logger.exception("butler watch stop failed")
        return {"ok": False, "detail": "engine unavailable"}
    payload = stopped if isinstance(stopped, dict) else {}
    return {"ok": True, **payload}


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "plugin": "butler"}


@router.get("/state")
def state() -> dict[str, Any]:
    return _read_state()


@router.get("/history")
def history() -> list[dict[str, Any]]:
    return _read_history()
