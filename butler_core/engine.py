"""Butler guide engine: step state machine + background verification thread.

State lives under the plugin data dir:
  guides/<id>.json   one session
  state.json         snapshot for the desktop surface / plugin_api

Fail-closed: unattended runs are rejected at the tool layer (kwargs has no
approval context), always. This module only mutates session files; the poll
thread refreshes them from live captures.
"""
import json
import os
import threading
import time
import uuid

from butler_core import capture as bp_capture
from butler_core import overlay as bp_overlay

_OVERLAY = bp_overlay.Overlay()

DATA_DIR = None
DEFAULT_POLL = 1.5
DEFAULT_TIMEOUT = 120.0

_lock = threading.Lock()
_session = None           # latest guide dict
_thread = None
_stop_flag = False


def init(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir
    os.makedirs(os.path.join(str(data_dir), "guides"), exist_ok=True)


def _guides_dir():
    return os.path.join(str(DATA_DIR), "guides")


def _cfg():
    cfg = {}
    try:
        with open(os.path.join(os.path.dirname(_guides_dir()), "config.json")) as f:
            cfg = json.load(f)
    except Exception:
        pass
    return cfg


def poll_seconds():
    return float(_cfg().get("poll_seconds", DEFAULT_POLL))


def step_timeout():
    return float(_cfg().get("step_timeout_seconds", DEFAULT_TIMEOUT))


def _write_state():
    if not DATA_DIR:
        return
    snap = {}
    if _session is not None:
        snap = {
            "guide": _session,
            "overlay": _session.get("overlay", "unavailable"),
            "generated_at": time.time(),
        }
    path = os.path.join(str(DATA_DIR), "state.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _save_session():
    if not _session:
        return
    path = os.path.join(_guides_dir(), _session["id"] + ".json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_session, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _loop():
    while not _stop_flag:
        time.sleep(poll_seconds())
        with _lock:
            s = _session
            if not s or s.get("state", {}).get("status") != "running":
                continue
            try:
                _poll_once(s)
            except Exception:
                pass
            _save_session()
            _write_state()


def _poll_once(s):
    idx = s["state"]["idx"]
    step = s["steps"][idx]
    snap = bp_capture.capture(max_elements=200, depth=12)
    best = bp_capture.best_match(snap.get("elements", []), step["find"]["label"],
                                 step["find"].get("role"))
    verdict = bp_capture.expect_pending(step.get("expect") or {}, snap, best)
    s["last_capture_app"] = snap.get("app")
    s["last_captured_at"] = time.time()
    if best is not None and best.get("box"):
        s["current_box"] = best["box"]
        s["current_label"] = best.get("label")
        s["overlay"] = "ringing"
        _OVERLAY.publish(s)
    if verdict["verified"]:
        s["verifications"].append({"step": idx, "reason": verdict["reason"],
                                   "verified_at": time.time()})
        idx += 1
        s["state"]["idx"] = idx
        s["state"]["note"] = None
        if idx >= len(s["steps"]):
            s["state"]["status"] = "done"
            s["current_box"] = None
        else:
            nxt = s["steps"][idx]
            s["state"]["say"] = nxt["say"]
        _OVERLAY.publish(s)
        return
    opened_at = s["state"].get("opened_at") or s["steps"][0].get("_t0") or time.time()
    if time.time() - opened_at > step_timeout():
        s["state"]["status"] = "stalled"
        s["state"]["note"] = ("step %d unverified for %.0fs; expect=%s" % (
            idx + 1, step_timeout(), verdict["reason"]))


def guide_start(args, unattended=False):
    if unattended:
        return {"error": "butler guides require an interactive session; refusing under unattended runs",
                "refusal": "unattended_run"}
    global _session, _thread, _stop_flag
    with _lock:
        if _session and _session.get("state", {}).get("status") in ("running", "stalled"):
            return {"error": "a guide is already active",
                    "active": _session["id"], "step": _session["state"]["idx"] + 1}
        steps = args.get("steps") or []
        guide = {
            "id": uuid.uuid4().hex[:10],
            "title": args.get("title") or "Guide",
            "steps": steps,
            "state": {"idx": 0, "status": "running", "say": steps[0]["say"] if steps else None},
            "verifications": [],
            "overlay": "starting",
            "started_at": time.time(),
        }
        _session = guide
        for st in guide["steps"]:
            st.setdefault("expect", {})
        _stop_flag = False
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="butler-poll", daemon=True)
            _thread.start()
        ok, detail = _OVERLAY.start()
        guide["overlay"] = ("unavailable:%s" % detail) if not ok else "ringing"
        first = bp_capture.capture(max_elements=200, depth=12)
        best = bp_capture.best_match(first.get("elements", []), steps[0]["find"]["label"],
                                     steps[0]["find"].get("role"))
        guide["last_capture_app"] = first.get("app")
        if best is not None and best.get("box"):
            guide["current_box"] = best["box"]
            guide["current_label"] = best.get("label")
            guide["overlay"] = "ringing"
        else:
            guide["overlay"] = "no_match"
            guide["state"]["note"] = "target not found in the first capture; the agent should re-check the plan"
        _save_session()
        _write_state()
        return {"guide_id": guide["id"], "title": guide["title"],
                "step": {"idx": 1, "say": guide["state"]["say"]},
                "overlay": guide["overlay"],
                "note": guide["state"].get("note"),
                "total_steps": len(steps)}


def guide_status():
    with _lock:
        if not _session:
            return {"status": "idle"}
        s = dict(_session)
        s.pop("steps", None)
        return s


def guide_stop(reason="stopped"):
    global _session
    with _lock:
        if not _session:
            return {"status": "idle"}
        _session["state"]["status"] = "stopped"
        _session["state"]["note"] = reason
        _session["current_box"] = None
        stopped = dict(_session)
        _OVERLAY.quit()
        stopped["overlay"] = "stopped"
        _save_session()
        _session = None
        _write_state()
        return {"status": "stopped", "reason": reason,
                "verified": stopped.get("verifications", [])}
