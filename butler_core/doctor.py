"""Butler doctor: grants, capabilities, companion build, configuration report.

`ask=True` pops macOS's own grant dialogs (Accessibility via
AXIsProcessTrustedWithOptions(prompt=True), Screen Recording via
CGRequestScreenCaptureAccess) and deep-links the two System Settings panes, so
the user never hunts for the toggle. The final click in System Settings stays a
user action - macOS does not permit self-granting TCC consent.
"""
import os
import subprocess

from . import capture as bp_capture
from . import overlay as bp_overlay
from . import ocr as bp_ocr

PANEL_ACCESSIBILITY = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
PANEL_SCREENCAPTURE = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"


def _open_panel(pane_url):
    try:
        subprocess.Popen(["open", pane_url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def report(ask=False):
    out = {
        "platform_ok": bp_capture.mac_supported(),
        "accessibility_granted": bp_capture.trusted(),
        "screen_recording_granted": bp_ocr.preflight_screen_record(),
        "capture": "missing grant" if not bp_capture.trusted() else "ok",
        "ocr": "missing grant" if not bp_ocr.preflight_screen_record() else "ok",
    }
    ok, detail = bp_overlay.build_if_needed()
    out["overlay"] = detail
    exe = bp_overlay.binary_path()
    out["overlay_binary"] = exe if os.path.exists(exe) else None
    out["prompted"] = {}
    if ask:
        if not bp_capture.trusted():
            out["prompted"]["accessibility"] = bp_capture._ask_accessibility()
        if not bp_ocr.preflight_screen_record():
            out["prompted"]["screen_recording"] = bp_ocr.request_screen_record()
            out["prompted"]["opened_panels"] = {
                "screen_capture": _open_panel(PANEL_SCREENCAPTURE),
                "accessibility": _open_panel(PANEL_ACCESSIBILITY),
            }
    if not out["accessibility_granted"]:
        out["fix"] = ("Run `hermes butler doctor --ask` to raise the grant dialogs; grant to the "
                      "Hermes host listed there, then restart Hermes. The pane deep-links to the toggle.")
    elif not out["screen_recording_granted"]:
        out["fix"] = ("Run `hermes butler doctor --ask` to raise the Screen Recording dialog; grant to the "
                      "same host, then restart Hermes (OCR fallback waits on this grant).")
    return out
