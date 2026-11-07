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


def _ask_accessibility():
    """Pop macOS' own Accessibility grant dialog for this process. Best-effort."""
    if not HAS_MAC:
        return False
    try:
        opts = {AS.kAXTrustedCheckOptionPrompt: True}
        return bool(AS.AXIsProcessTrustedWithOptions(opts))
    except Exception:
        return False


def _window_count(app):
    try:
        return len(app.windows())
    except Exception:
        return 0


def find_running_app(target=None):
    """Frontmost app if target is None; else best name match with open windows.

    "Frontmost" is read from the CGWindowList z-order (topmost layer-0 window
    on screen), NOT from NSWorkspace.frontmostApplication(): in a long-lived
    tool host that never pumps an NSApplication runloop, NSWorkspace's
    frontmost cache never refreshes and stays welded to the app that held
    focus at AppKit init - the butler then walks a screen that stopped moving.
    """
    if not HAS_MAC:
        return None, None
    wins = _topmost_layer0_windows()
    if target is None:
        if wins:
            w = wins[0]
            return w["pid"], w["name"]
    else:
        needle = target.lower()
        for w in wins:
            if w["name"] and needle in w["name"].lower():
                return w["pid"], w["name"]
    ws = NSWorkspace.sharedWorkspace()  # cached fallback; usually stale host-side
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


def _topmost_layer0_windows():
    """On-screen normal-level windows in z-order (topmost first). Never raises."""
    try:
        import Quartz as QZ
        opts = getattr(QZ, "kCGWindowListOptionOnScreenOnly") | getattr(
            QZ, "kCGWindowListExcludeDesktopElements")
        info = getattr(QZ, "CGWindowListCopyWindowInfo")(
            opts, getattr(QZ, "kCGNullWindowID"))
        out = []
        for w in (info or []):
            try:
                if w.get("kCGWindowLayer") == 0 and int(w.get("kCGWindowOwnerPID") or 0) > 0:
                    out.append({"pid": int(w["kCGWindowOwnerPID"]),
                                "name": w.get("kCGWindowOwnerName") or "",
                                "title": w.get("kCGWindowName")})
            except Exception:
                continue
        return out
    except Exception:
        return []


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
        prompted = _ask_accessibility()
        if prompted:
            # a dialog requires a user click; this call stays empty this round
            result["trusted"] = trusted()
        result["prompted"] = prompted
    if not result["trusted"]:
        result["note"] = "Accessibility not granted to this process"
        result["fix"] = ("A system dialog was raised (or use `hermes butler doctor --ask`). "
                         "After granting, restart the Hermes host process.")
        return result
    pid, name = find_running_app(target)
    if pid is None:
        result["error"] = "no running app matches target" if target else "no frontmost app"
        return result
    root = AS.AXUIElementCreateApplication(pid)
    result["app"] = name
    # What actually holds keyboard focus right now (click feedback): the
    # expect-engine uses this for the "focus" verdict so a step is completed
    # by acting on the control, not by it merely appearing on screen.
    try:
        focused_el = copy_attr(root, "AXFocusedUIElement")
        if focused_el is not None:
            _role, flabel, _box = read_fields(focused_el)
            if isinstance(flabel, str) and flabel.strip():
                result["focused"] = flabel.strip()[:120]
    except Exception:
        pass
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
