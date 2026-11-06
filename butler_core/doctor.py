"""Butler doctor: grants, capabilities, companion build, configuration report."""
import os

from butler_core import capture as bp_capture
from butler_core import overlay as bp_overlay
from butler_core import ocr as bp_ocr


def report():
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
    if not out["accessibility_granted"]:
        out["fix"] = ("System Settings > Privacy & Security > Accessibility - add the "
                      "process running Hermes (the gateway/CLI host binary). "
                      "Screen Recording lives in the same pane, one list lower.")
    elif not out["screen_recording_granted"]:
        out["fix"] = ("System Settings > Privacy & Security > Screen Recording - add "
                      "the same host process, for OCR fallback captures.")
    return out
