"""macOS Accessibility capture for the butler.

Python 3.9-safe. Import-guarded so non-macOS installs load cleanly.
"""
import json
import re

try:
    import ApplicationServices as AS
    from AppKit import NSWorkspace
    HAS_MAC = True
except Exception:  # pragma: no cover - non-macOS
    AS = None
    NSWorkspace = None
    HAS_MAC = False

DEFAULT_MAX = 150
DEFAULT_DEPTH = 10
_ACCESS_ERR = 252


def mac_supported():
    return HAS_MAC


def trusted():
    if not HAS_MAC:
        return False
    return bool(AS.AXIsProcessTrusted())


def _window_count(app):
    try:
        return len(app.windows())
    except Exception:
        return 0


def find_running_app(target=None):
    """Frontmost app if target is None; else best name match with open windows."""
    if not HAS_MAC:
        return None, None
    ws = NSWorkspace.sharedWorkspace()
    if target is None:
        app = ws.frontmostApplication()
        if app is None:
            return None, None
        return int(app.processIdentifier()), app.localizedName()
    cands = []
    for a in ws.runningApplications():
        name = a.localizedName()
        try:
            alive = not a.isTerminated()
        except Exception:
            alive = True
        if name and target.lower() in name.lower() and alive:
            cands.append(a)
    if not cands:
        return None, None
    cands.sort(key=_window_count, reverse=True)
    return int(cands[0].processIdentifier()), cands[0].localizedName()


def copy_attr(el, name):
    err, value = AS.AXUIElementCopyAttributeValue(el, name, None)
    if err != 0 or value is None:
        return None
    return value


def _ax_point_size(v, kind):
    try:
        t = AS.AXValueGetType(v)
        if t == AS.kAXValueCGPointType and kind == "point":
            ok, pt = AS.AXValueGetValue(v, AS.kAXValueCGPointType, None)
            if ok and hasattr(pt, "x"):
                return [int(pt.x), int(pt.y)]
        if t == AS.kAXValueCGSizeType and kind == "size":
            ok, sz = AS.AXValueGetValue(v, AS.kAXValueCGSizeType, None)
            if ok and hasattr(sz, "width"):
                return [int(sz.width), int(sz.height)]
    except Exception:
        pass
    return None


def read_fields(el):
    role = copy_attr(el, "AXRole")
    title = copy_attr(el, "AXTitle")
    desc = copy_attr(el, "AXDescription")
    val = copy_attr(el, "AXValue")
    pos = copy_attr(el, "AXPosition")
    size = copy_attr(el, "AXSize")
    label = None
    for cand in (title, desc):
        if isinstance(cand, str) and cand.strip():
            label = cand.strip()
            break
    if label is None and isinstance(val, str) and val.strip():
        label = val.strip()[:80]
    box = [None, None, None, None]
    p = _ax_point_size(pos, "point")
    s = _ax_point_size(size, "size")
    if isinstance(p, list) and isinstance(s, list):
        box = [p[0], p[1], s[0], s[1]]
    role_s = str(role) if role is not None else "unknown"
    return role_s, label, box


def _walk(el, depth, out, budget):
    if el is None or depth < 0 or len(out) >= budget:
        return
    role, label, box = read_fields(el)
    if label is not None or box is not None:
        out.append({"role": role, "label": label, "box": box})
    kids = copy_attr(el, "AXChildren")
    if kids is not None and hasattr(kids, "__iter__"):
        for kid in kids:
            if len(out) >= budget:
                return
            _walk(kid, depth - 1, out, budget)


def _windows_of(root):
    win = copy_attr(root, "AXFocusedWindow")
    if win is not None:
        return [win]
    wins = copy_attr(root, "AXWindows")
    if wins is not None and hasattr(wins, "__iter__"):
        return list(wins)
    return [root]


def capture(target=None, max_elements=DEFAULT_MAX, depth=DEFAULT_DEPTH):
    """Element list of the target/frontmost app. Never raises for OS errors."""
    if not HAS_MAC:
        return {"platform": "macos_only", "error": "pyobjc not available on this platform"}
    result = {"trusted": trusted(), "app": None, "window": None,
              "count": 0, "with_box": 0, "elements": []}
    if not result["trusted"]:
        result["note"] = "Accessibility not granted to this process"
        result["fix"] = ("System Settings > Privacy & Security > Accessibility: "
                         "grant to the host process running Hermes; run `hermes butler doctor` after granting")
        return result
    pid, name = find_running_app(target)
    if pid is None:
        result["error"] = "no running app matches target" if target else "no frontmost app"
        return result
    root = AS.AXUIElementCreateApplication(pid)
    result["app"] = name
    elements = []
    for win in _windows_of(root):
        title = copy_attr(win, "AXTitle") or copy_attr(win, "AXDescription")
        if result["window"] is None and isinstance(title, str):
            result["window"] = title
        before = len(elements)
        _walk(win, depth, elements, max_elements)
        if len(elements) >= max_elements:
            break
    for i, e in enumerate(elements):
        e["id"] = i
        if e["box"][0] is not None:
            result["with_box"] += 1
    result["elements"] = elements[:max_elements]
    result["count"] = len(result["elements"])
    return result


def best_match(elements, label_needle, role_needle=None):
    """Score best element for a step find. Returns element dict or None."""
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
    visible = expect.get("visible")
    if visible:
        for e in snapshot.get("elements", []):
            if e.get("label") and visible.lower() in e["label"].lower():
                return {"verified": True, "reason": "element_visible"}
    focus = expect.get("focus")
    if focus and best is not None and best.get("label"):
        return {"verified": True, "reason": "target_present"}
    return {"verified": False, "reason": "expectation_unmet"}
