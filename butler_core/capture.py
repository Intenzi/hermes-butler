"""macOS Accessibility capture for the butler.

The Accessibility walk lives in the bundled Swift companion
(``overlay_swift/ButlerOverlay.swift``) so the plugin has exactly one stable
TCC grant subject: the compiled helper binary. This module is the thin Python
transport over that already-running companion's stdin/stdout.

Python 3.9-safe. The pure matching helpers at the bottom (best_match /
expect_pending) still run in-process; engine.py keeps the same public API and
snapshot shape.
"""
import sys

try:  # package import
    from . import overlay as bp_overlay
except ImportError:  # pragma: no cover - direct module import
    import overlay as bp_overlay

DEFAULT_MAX = 150
DEFAULT_DEPTH = 10
REQUEST_TIMEOUT = 5.0
GRANT_SUBJECT = "Butler companion (the butler plugin's bundled helper)"
GRANT_FIX = (
    "Grant Accessibility to \"%s\" in System Settings -> Privacy & Security -> "
    "Accessibility, then try the guide again. The companion binary keeps the "
    "same path across Hermes updates, so the grant survives updates." % GRANT_SUBJECT
)

# The helper is the only thing that needs Accessibility; the Python host is not
# a grant subject anymore.
HAS_MAC = sys.platform == "darwin"


def mac_supported():
    return HAS_MAC


def _helper():
    """Return the shared companion handle (the same process the overlay uses)."""
    return bp_overlay.shared_overlay()


def _request(op, timeout=None):
    """Send one op to the companion and return its parsed JSON line, or None."""
    if timeout is None:
        timeout = REQUEST_TIMEOUT
    try:
        helper = _helper()
        response = helper.request(op, timeout=timeout)
        return response if isinstance(response, dict) else None
    except Exception:  # noqa: BLE001 - transport must never raise
        return None


def _failed_snapshot(note, fix=GRANT_FIX, prompted=False):
    return {
        "trusted": False,
        "app": None,
        "window": None,
        "count": 0,
        "with_box": 0,
        "elements": [],
        "prompted": prompted,
        "note": note,
        "fix": fix,
    }


def trusted():
    """Silent Accessibility trust check via the companion (no dialog)."""
    if not HAS_MAC:
        return False
    reply = _request({"op": "axtrusted"})
    if not isinstance(reply, dict):
        return False
    return bool(reply.get("trusted"))


def _ask_accessibility():
    """Raise macOS' own Accessibility grant dialog for the companion. Best-effort."""
    if not HAS_MAC:
        return False
    reply = _request({"op": "axtrusted", "prompt": True})
    if not isinstance(reply, dict):
        return False
    return bool(reply.get("prompted"))


def _capture_op(target, max_elements, depth):
    try:
        limit = int(max_elements)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX
    try:
        level = int(depth)
    except (TypeError, ValueError):
        level = DEFAULT_DEPTH
    return {
        "op": "axcapture",
        "target": target if isinstance(target, str) else None,
        "max_elements": limit,
        "depth": level,
    }


def capture(target=None, max_elements=DEFAULT_MAX, depth=DEFAULT_DEPTH):
    """Element list of the target/frontmost app. Never raises for OS errors.

    The returned dict is the companion's snapshot, unchanged; on silence or a
    dead helper it degrades to the untrusted/failed snapshot shape.
    """
    if not HAS_MAC:
        return {"platform": "macos_only", "error": "Butler companion capture is macOS-only"}
    op = _capture_op(target, max_elements, depth)
    reply = _request(op)
    if isinstance(reply, dict):
        return reply
    return _failed_snapshot(
        "Butler companion did not return a capture within %gs" % REQUEST_TIMEOUT)


def best_match(elements, label_needle, role_needle=None):
    """Score best element for a step find. Returns element dict or None.

    Window roots are ranked below ordinary controls on purpose: native app
    windows carry the pane name as their title (System Settings' window is
    literally "Network" when that pane opens), so a plain label match would
    ring the entire window instead of the sidebar/control the user should
    touch. A window only rings when no other element matches.
    """
    if not label_needle:
        return None
    needle = label_needle.lower()
    scores = []
    for e in elements:
        label = (e.get("label") or "").lower()
        role = (e.get("role") or "").lower()
        if not label:
            continue
        if needle in label:
            s = 3 if needle == label else (2 if label.startswith(needle) else 1)
            if role_needle and role_needle.lower() in role:
                s += 2
            # keep window roots from winning ties over real controls
            if role and "axwindow" in role:
                s -= 4
            if e.get("box") and e["box"][0] is not None:
                s += 1
            scores.append((s, e))
    if not scores:
        return None
    scores.sort(key=lambda t: t[0], reverse=True)
    return scores[0][1]


def expect_pending(expect, snapshot, best):
    """Evaluate a step's expect dict against a fresh capture. Returns dict verdict."""
    if not expect:
        return {"verified": False, "reason": "no_expect_given"}
    app = expect.get("app")
    if app and snapshot.get("app") and app.lower() in snapshot["app"].lower():
        return {"verified": True, "reason": "app_focused"}
    window = expect.get("window")
    if window and snapshot.get("window") and window.lower() in snapshot["window"].lower():
        # the pane the user landed on (its root window title carries the pane
        # name in apps like System Settings) — a click-observable: it only
        # becomes true after the user navigates, unlike "visible".
        return {"verified": True, "reason": "pane_reached"}
    visible = expect.get("visible")
    if visible:
        for e in snapshot.get("elements", []):
            if e.get("label") and visible.lower() in e["label"].lower():
                return {"verified": True, "reason": "element_visible"}
    focus = expect.get("focus")
    if focus:
        # "focus" means the user acted on the control: verify against the
        # snapshot's focused element label, not mere presence (which is
        # already true the moment the control exists).
        focused = snapshot.get("focused")
        if isinstance(focused, str) and focus.lower() in focused.lower():
            return {"verified": True, "reason": "element_focused"}
    return {"verified": False, "reason": "expectation_unmet"}
