"""On-device OCR fallback (Apple Vision) for AX-gated content.

Used when the AX tree returns no structure for the focused app (Chromium tabs
that gate page content, canvas-heavy apps). Extracts text + box in screen
coordinates for the butler's element-list format.
"""
import re
import tempfile

from . import capture as bp_capture

def _quartz():
    try:
        import Quartz
        return Quartz
    except Exception:
        return None


def preflight_screen_record():
    """True if the process can capture screen pixels (Screen Recording grant)."""
    q = _quartz()
    if q is None:
        return False
    try:
        return bool(q.CGPreflightScreenCaptureAccess())
    except Exception:
        return False


def request_screen_record():
    """Pop macOS' Screen Recording grant dialog. Returns preflight after asking."""
    q = _quartz()
    if q is None:
        return False
    try:
        q.CGRequestScreenCaptureAccess()
        return bool(q.CGPreflightScreenCaptureAccess())
    except Exception:
        return False


def _frontmost_window_info(pid):
    q = _quartz()
    if q is None:
        return None
    opts = q.kCGWindowListOptionOnScreenOnly
    infos = q.CGWindowListCopyWindowInfo(opts, q.kCGNullWindowID)
    for info in infos or []:
        try:
            if int(info.get("kCGWindowOwnerPID", -1)) == pid:
                bounds = info.get("kCGWindowBounds") or None
                if bounds and int(bounds.get("Width", 0)) > 100 and int(bounds.get("Height", 0)) > 100:
                    num = info.get("kCGWindowNumber")
                    name = info.get("kCGWindowName")
                    return int(num), [int(bounds["X"]), int(bounds["Y"]),
                                      int(bounds["Width"]), int(bounds["Height"])], name
        except Exception:
            continue
    return None


def window_text_lines(pid=None, target=None):
    """List of {label, box} text observations for the given app's frontmost window."""
    q = _quartz()
    if q is None:
        return {"error": "pyobjc not available"}
    if not bp_capture.trusted() or not preflight_screen_record():
        return {"error": "permissions_missing", "needs": ["accessibility", "screen_recording"]}
    _pid, name = bp_capture.find_running_app(target)
    pid = pid or _pid
    if pid is None:
        return {"error": "no_running_app"}
    win = _frontmost_window_info(pid)
    if not win:
        return {"error": "no_window_found"}
    win_id, frame, title = win
    qx, qy, qw, qh = frame
    rgx = re.compile(r"^[\W_]+|[\W_]+$")
    from Vision import (VNImageRequestHandler, VNRecognizeTextRequest)  # noqa: E402
    img = q.CGWindowListCreateImage(
        q.kCGWindowImageBoundsIgnoreTransparency if hasattr(q, "kCGWindowImageBoundsIgnoreTransparency")
        else q.kCGWindowImageDefault, None, 0, q.kCGWindowListOptionIncludingWindow, win_id)
    if img is None:
        return {"error": "capture_failed"}
    import objc
    handler = VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    req = VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(1)  # accurate
    obs = []
    ok, err = handler.performRequests_error_([req], None)
    if ok:
        obs = req.results() or []
    elements = []
    for o in obs:
        obs_box = o.boundingBox()  # CGRect normalized
        cand = o.topCandidates_(1)
        text = str(cand[0].string()).strip() if cand else None
        if not text:
            continue
        x = int(qx + obs_box.origin.x * qw)
        y = int(qy + (1 - obs_box.origin.y - obs_box.size.height) * qh)
        elements.append({"role": "text(ocr)", "label": text[:120],
                         "box": [x, y, int(obs_box.size.width * qw),
                                 int(obs_box.size.height * qh)], "source": "ocr"})
    return {"app": name, "window": title, "count": len(elements),
            "with_box": len(elements), "elements": elements}
