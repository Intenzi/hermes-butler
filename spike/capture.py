#!/usr/bin/env python3
"""Butler spike 1 — capture the focused app's Accessibility element list.

Output: JSON {trusted, app, window, count, elements[]} to stdout + spike/out.json.
Pass criteria: >= 20 elements with valid boxes for the focused app.

Usage:
  python capture.py                # frontmost app
  python capture.py --target chrome
  python capture.py --max 200 --depth 14
"""
import argparse
import json
import os
import sys

import ApplicationServices as AS
from Foundation import NSApplication

MAX = 150
DEPTH = 10

KNOWN_ERRORS = {
    252: "kAXErrorAPIDisabled — Accessibility not granted to this process",
}


def trusted() -> bool:
    return bool(AS.AXIsProcessTrusted())


def find_pid(target=None):
    """Return (pid, name) for frontmost app, or best name match."""
    ws = QuartzWorkaround()
    if target is None:
        app = ws.frontmostApplication()
    else:
        pool = ws.runningApplications()
        cands = [a for a in pool
                 if a.localizedName() and target.lower() in a.localizedName().lower() and not a.isTerminated()]
        # prefer ones that actually have windows open
        cands.sort(key=lambda a: pid_window_count(a), reverse=True)
        app = cands[0] if cands else None
    if app is None:
        return None, None
    return int(app.processIdentifier()), app.localizedName()


def pid_window_count(a):
    try:
        return len(a.windows())
    except Exception:
        return 0


from AppKit import NSWorkspace  # noqa: E402


def QuartzWorkaround():
    return NSWorkspace.sharedWorkspace()


def copy_attr(el, name):
    err, value = AS.AXUIElementCopyAttributeValue(el, name, None)
    if err != 0 or value is None:
        return None
    return value


def element_to_val(v):
    # AXValue (CGPoint/CGSize) -> [x, y] / [w, h]
    try:
        t = AS.AXValueGetType(v)
        if t == AS.kAXValueCGPointType:
            ok, pt = AS.AXValueGetValue(v, AS.kAXValueCGPointType, None)
            if ok and hasattr(pt, "x"):
                return [int(pt.x), int(pt.y)]
        if t == AS.kAXValueCGSizeType:
            ok, sz = AS.AXValueGetValue(v, AS.kAXValueCGSizeType, None)
            if ok and hasattr(sz, "width"):
                return [int(sz.width), int(sz.height)]
    except Exception:
        pass
    try:
        return str(v)
    except Exception:
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
    box = None
    p, s = element_to_val(pos), element_to_val(size)
    if isinstance(p, list) and isinstance(s, list) and len(p) == 2 and len(s) == 2:
        box = [int(p[0]), int(p[1]), int(s[0]), int(s[1])]
    role_s = str(role) if role is not None else "unknown"
    return role_s, label, box


def walk(el, depth, out, budget):
    if depth > DEPTH or len(out) >= budget or el is None:
        return
    role, label, box = read_fields(el)
    if role not in ("unknown",) and (label is not None or box is not None):
        out.append({"role": role, "label": label, "box": box})
    kids = copy_attr(el, "AXChildren")
    if kids is not None and hasattr(kids, "__iter__"):
        for k in kids:
            walk(k, depth + 1, out, budget)
            if len(out) >= budget:
                return


def focused_element(root):
    # walk from AXFocusedWindow if present, else all windows
    win = copy_attr(root, "AXFocusedWindow")
    if win is not None:
        return [win]
    wins = copy_attr(root, "AXWindows")
    if wins is not None and hasattr(wins, "__iter__"):
        return list(wins)
    return [root]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="app name substring (default: frontmost)")
    ap.add_argument("--max", type=int, default=MAX)
    ap.add_argument("--depth", type=int, default=DEPTH)
    args = ap.parse_args()

    result = {"trusted": trusted(), "app": None, "window": None,
              "count": 0, "elements": []}

    if not result["trusted"]:
        print(json.dumps({"trusted": False,
                          "note": KNOWN_ERRORS.get(252, "Accessibility not granted"),
                          "fix": "System Settings > Privacy & Security > Accessibility: grant to the host running this script"}))
        sys.exit(2)

    pid, name = find_pid(args.target)
    if pid is None:
        print(json.dumps({"error": "no matching running app"}))
        sys.exit(3)
    root = AS.AXUIElementCreateApplication(pid)
    result["app"] = name
    elements = []
    for win in focused_element(root):
        win_title = copy_attr(win, "AXTitle") or copy_attr(win, "AXDescription")
        if result["window"] is None and isinstance(win_title, str):
            result["window"] = win_title
        walk(win, 0, elements, args.max)
        if len(elements) >= args.max:
            break
    boxeded = 0
    for i, e in enumerate(elements):
        e["id"] = i
        if e["box"] is not None:
            boxeded += 1
    result["elements"] = elements
    result["count"] = len(elements)
    result["with_box"] = boxeded

    print(json.dumps(result, indent=1))
    out_path = os.path.join(os.path.dirname(__file__), "out.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=1)


if __name__ == "__main__":
    main()
