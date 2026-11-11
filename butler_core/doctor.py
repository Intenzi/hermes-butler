"""Butler doctor: grants, capabilities, companion build, configuration report.

`ask=True` pops macOS's own grant dialogs (Accessibility via
AXIsProcessTrustedWithOptions(prompt=True), Screen Recording via
CGRequestScreenCaptureAccess) and deep-links the two System Settings panes, so
the user never hunts for the toggle. The final click in System Settings stays a
user action - macOS does not permit self-granting TCC consent.

The companion overlay is checked FIRST. A companion that cannot run is not a
permission problem: reporting "grant Accessibility" would send the user to a
toggle that cannot help when the real cause is a missing compiler. In that case
the accessibility answer is ``None`` (unknown), never ``False``.
"""
import os
import shutil
import subprocess

from . import capture as bp_capture
from . import overlay as bp_overlay
from . import ocr as bp_ocr

PANEL_ACCESSIBILITY = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
PANEL_SCREENCAPTURE = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"

GRANT_SUBJECT = "Butler companion - the butler plugin's bundled helper"
GRANT_WHY = ("Because the butler reads the screen through its own small helper, "
             "macOS asks once and remembers across updates.")
GRANT_FIX = (
    "Grant Accessibility to \"%s\" in System Settings -> Privacy & Security -> "
    "Accessibility, then try the guide again. %s" % (GRANT_SUBJECT, GRANT_WHY)
)
SCREEN_RECORDING_FIX = (
    "Run `hermes butler doctor --ask` to raise the Screen Recording dialog; grant to the "
    "Hermes host, then restart Hermes (the OCR fallback waits on this grant)."
)
# The companion cannot run at all; no Accessibility toggle can fix that.
OVERLAY_FIX = ("Install Xcode Command Line Tools: xcode-select --install, "
               "then run: hermes butler doctor")


def _open_panel(pane_url):
    try:
        subprocess.Popen(["open", pane_url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _overlay_archs(exe):
    """`lipo -archs` for the shipped companion, or None when unavailable."""
    if not exe or not os.path.exists(exe):
        return None
    lipo = shutil.which("lipo")
    if lipo is None:
        return None
    try:
        proc = subprocess.run([lipo, "-archs", exe], timeout=10, check=True,
                              capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return None
    archs = proc.stdout.strip()
    return archs or None


def report(ask=False):
    out = {
        "platform_ok": bp_capture.mac_supported(),
        # One grant subject: axtrusted is answered by the bundled companion
        # binary, never by the varying Python host. Unknown (None) when the
        # companion itself cannot run, because then nothing can answer.
        "accessibility_granted": None,
        "accessibility_grant_subject": GRANT_SUBJECT,
        "screen_recording_granted": False,
        "capture": "unavailable: companion not checked",
        "ocr": "missing grant",
    }

    # Check the companion before probing a grant: if the overlay itself is
    # unusable, a failed grant probe would just mirror that and send the user
    # to Accessibility - a toggle that cannot make a missing compiler appear.
    overlay_ok, overlay_detail = bp_overlay.build_if_needed()
    exe = bp_overlay.binary_path()
    out["overlay"] = overlay_detail
    out["overlay_binary"] = exe if os.path.exists(exe) else None
    out["overlay_archs"] = _overlay_archs(exe)

    screen_recording_granted = bool(bp_ocr.preflight_screen_record())
    out["screen_recording_granted"] = screen_recording_granted
    out["ocr"] = "missing grant" if not screen_recording_granted else "ok"
    out["prompted"] = {}

    if not overlay_ok:
        # Unknown is not false: the companion never got to answer the grant
        # question, so neither grant claim nor grant instruction is honest.
        out["accessibility_granted"] = None
        out["capture"] = "unavailable: %s" % overlay_detail
        out["fix"] = OVERLAY_FIX
        out["fixes"] = [OVERLAY_FIX]
        if ask and not screen_recording_granted:
            out["prompted"]["screen_recording"] = bp_ocr.request_screen_record()
            # Only the Screen Recording pane: opening Accessibility here would
            # repeat the wrong instruction this branch exists to avoid.
            out["prompted"]["opened_panels"] = {
                "screen_capture": _open_panel(PANEL_SCREENCAPTURE),
            }
        return out

    accessibility_granted = bool(bp_capture.trusted())
    out["accessibility_granted"] = accessibility_granted
    out["capture"] = "missing grant" if not accessibility_granted else "ok"

    fixes = []
    if not accessibility_granted:
        fixes.append(GRANT_FIX)
    elif not screen_recording_granted:
        fixes.append(SCREEN_RECORDING_FIX)
    if fixes:
        out["fix"] = fixes[0]
    out["fixes"] = fixes

    if ask:
        if not accessibility_granted:
            out["prompted"]["accessibility"] = bp_capture._ask_accessibility()
        if not screen_recording_granted:
            out["prompted"]["screen_recording"] = bp_ocr.request_screen_record()
            out["prompted"]["opened_panels"] = {
                "screen_capture": _open_panel(PANEL_SCREENCAPTURE),
                "accessibility": _open_panel(PANEL_ACCESSIBILITY),
            }
    return out
