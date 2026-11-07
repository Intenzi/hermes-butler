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

from . import capture as bp_capture
from . import overlay as bp_overlay

_OVERLAY = bp_overlay.Overlay()

DATA_DIR = None
DEFAULT_POLL = 1.5
DEFAULT_TIMEOUT = 120.0
ERROR_STREAK_NOTE = 3  # consecutive poll errors before the note surfaces

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


def _normalize_step(step):
    """Accept the documented shape, or a flat-key typo of it.

    Returns a clean step dict, or None when the step can never verify.
    Flat keys like "find.label" / "expect.app" are folded into nested dicts so
    a malformed call degrades into the step it meant to be instead of a
    KeyError half-way through startup, leaving an orphan guide running.
    """
    if not isinstance(step, dict):
        return None
    say = step.get("say")
    if not isinstance(say, str) or not say.strip():
        return None
    expect: dict = dict(step.get("expect") or {})
    find: dict = dict(step.get("find") or {})
    for key, value in step.items():
        if key.startswith("expect."):
            expect[key[len("expect."):]] = value
        elif key.startswith("find."):
            find[key[len("find."):]] = value
    clean: dict = {"say": say.strip()}
    label = find.get("label")
    if isinstance(label, str) and label.strip():
        clean["find"] = {"label": label.strip()}
        if isinstance(find.get("role"), str) and find["role"].strip():
            clean["find"]["role"] = find["role"].strip()
    # No key whitelist here: expect keys evolve (app/visible/window/focus...)
    # and silently dropping an unknown but valid key starves a step of its
    # verification (expect=no_expect_given). Anything present passes through,
    # invalid keys simply never match in expect_pending.
    kept_expect = {k: str(v) for k, v in expect.items() if v}
    if kept_expect:
        clean["expect"] = kept_expect
    # A step the butler can neither locate nor verify is a dead step.
    if not (clean.get("find") or clean.get("expect")):
        return None
    return clean


def _normalize_steps(args_steps):
    steps = []
    for st in args_steps:
        clean = _normalize_step(st)
        if clean is not None:
            steps.append(clean)
    return steps


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
    poll = poll_seconds()
    while not _stop_flag:
        time.sleep(poll)
        with _lock:
            s = _session
            if not s or s.get("state", {}).get("status") != "running":
                continue
            try:
                _poll_once(s)
            except Exception as exc:
                # Never swallow silently: surface repeated failures in the
                # session note so agents can see the loop is impaired.
                streak = s.get("poll_errors", 0) + 1
                s["poll_errors"] = streak
                if streak == ERROR_STREAK_NOTE:
                    s["state"] = dict(
                        s["state"],
                        note="butler poll failing: %r" % exc)
                _save_session()
                _write_state()
                continue
            s["poll_errors"] = 0
            _save_session()
            _write_state()


def _poll_once(s):
    idx = s["state"]["idx"]
    step = s["steps"][idx]
    snap = bp_capture.capture(max_elements=200, depth=12)
    if snap.get("error") or (snap.get("trusted") is False):
        s["state"] = dict(s["state"], status="capture_failed")
        s["capture_note"] = snap.get("note") or snap.get("error")
        s["fix"] = snap.get("fix")
        return
    find = step.get("find") or {}
    label_needle = find.get("label")
    best = None
    if label_needle:
        best = bp_capture.best_match(snap.get("elements", []), label_needle,
                                     find.get("role"))
    verdict = bp_capture.expect_pending(step.get("expect") or {}, snap, best)
    s["last_capture_app"] = snap.get("app")
    s["last_captured_at"] = time.time()
    if not verdict["verified"]:
        # "Open the app" counts when the app is OPEN, not only frontmost:
        # users follow the pet side-by-side without chasing focus.
        want_app = (step.get("expect") or {}).get("app")
        if want_app:
            app_snap = bp_capture.capture(target=want_app, max_elements=40, depth=8)
            if app_snap.get("app") and (app_snap.get("window")
                                        or int(app_snap.get("count") or 0) > 0):
                verdict = {"verified": True, "reason": "app_open_targeted"}
    if best is not None and best.get("box"):
        s["current_box"] = best["box"]
        s["current_label"] = best.get("label")
        s["overlay"] = "ringing"
        _OVERLAY.publish(s)
    elif not label_needle:
        # hands-off step (e.g. open an app): nothing to ring, just verify
        s["current_box"] = None
        s["current_label"] = None
        s["overlay"] = "waiting"
    else:
        # target not visible right now: un-park any ring so a highlight never
        # stays welded over a window the user left. It re-rings when the
        # target is in view again.
        if s.get("current_box") is not None or s.get("current_label") is not None:
            s["current_box"] = None
            s["current_label"] = None
            s["overlay"] = "waiting"
            _OVERLAY.idle()
    if verdict["verified"]:
        s["verifications"].append({"step": idx, "reason": verdict["reason"],
                                   "verified_at": time.time()})
        idx += 1
        s["state"]["idx"] = idx
        s["state"]["note"] = None
        s["state"]["step_opened_at"] = time.time()
        if idx >= len(s["steps"]):
            s["state"]["status"] = "done"
            s["state"]["say"] = "Guide complete"
            s["current_box"] = None
            s["current_label"] = None
            _OVERLAY.idle()
        else:
            nxt = s["steps"][idx]
            s["state"]["say"] = nxt["say"]
            s["current_box"] = None
            s["current_label"] = None
            # clear any stale ring now; the next step re-rings when found.
            _OVERLAY.idle()
        return
    opened_at = s["state"].get("step_opened_at") or s.get("started_at") or time.time()
    held = time.time() - opened_at
    if held > step_timeout():
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
        steps = _normalize_steps(args.get("steps") or [])
        if not steps:
            return {"error": "guide start rejected: no verifiable steps "
                             "(each step needs say + find.label or an expect key)"}
        guide = {
            "id": uuid.uuid4().hex[:10],
            "title": args.get("title") or "Guide",
            "steps": steps,
            "state": {"idx": 0, "status": "running",
                      "say": steps[0]["say"],
                      "step_opened_at": time.time()},
            "verifications": [],
            "overlay": "starting",
            "started_at": time.time(),
        }
        _session = guide
        _stop_flag = False
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="butler-poll", daemon=True)
            _thread.start()
        ok, detail = _OVERLAY.start()
        guide["overlay"] = ("unavailable:%s" % detail) if not ok else "starting"
        try:
            first = bp_capture.capture(max_elements=200, depth=12)
        except Exception as exc:  # capture must never orphan a live guide
            guide["state"] = dict(guide["state"], status="capture_failed")
            guide["capture_note"] = "startup capture raised: %r" % exc
            _save_session()
            _write_state()
            return {"guide_id": guide["id"], "title": guide["title"],
                    "state": guide["state"], "capture_note": guide["capture_note"],
                    "total_steps": len(steps)}
        if first.get("error") or (first.get("trusted") is False):
            guide["state"] = dict(guide["state"], status="capture_failed")
            guide["capture_note"] = first.get("note") or first.get("error")
            guide["fix"] = first.get("fix")
            _save_session()
            _write_state()
            return {"guide_id": guide["id"], "title": guide["title"],
                    "state": guide["state"], "capture_note": guide["capture_note"],
                    "fix": guide["fix"], "total_steps": len(steps)}
        find0 = steps[0].get("find") or {}
        best = bp_capture.best_match(first.get("elements", []),
                                     find0.get("label"), find0.get("role"))
        guide["last_capture_app"] = first.get("app")
        guide["last_captured_at"] = time.time()
        if best is not None and best.get("box"):
            guide["current_box"] = best["box"]
            guide["current_label"] = best.get("label")
            guide["overlay"] = "ringing"
            _OVERLAY.publish(guide)
        elif find0.get("label"):
            guide["overlay"] = "no_match"
            guide["state"]["note"] = "target not found in the first capture; the agent should re-check the plan"
        else:
            # hands-off step: the butler is waiting on the user, not missing a target
            guide["overlay"] = "waiting"
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
