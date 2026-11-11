"""Butler doctor: grants, capabilities, companion build, configuration report.

`ask=True` pops macOS's own Accessibility dialog
(AXIsProcessTrustedWithOptions(prompt=True)) and deep-links the pane, so the
user never hunts for the toggle. The final click in System Settings stays a
user action - macOS does not permit self-granting TCC consent.

The companion overlay is checked FIRST. A companion that cannot run is not a
permission problem: reporting "grant Accessibility" would send the user to a
toggle that cannot help when the real cause is a missing helper. In that case
the accessibility answer is ``None`` (unknown), never ``False``.

Two different companion failures get two different remedies, and neither is a
compiler for a user who never had one:

* the binary is missing or not executable -> the plugin install is incomplete
  or macOS blocked it; reinstall the plugin.
* the binary runs but the local source hash no longer matches ``.buildinfo``
  (a developer edited the Swift) -> rebuild with Command Line Tools, or
  reinstall.

The word "Xcode" is banned from doctor output: the toolchain the user needs is
Command Line Tools.
"""
import os
import shutil
import subprocess

from . import capture as bp_capture
from . import overlay as bp_overlay
from . import ocr as bp_ocr

PANEL_ACCESSIBILITY = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"

GRANT_SUBJECT = "Butler companion - the butler plugin's bundled helper"
GRANT_WHY = ("Because the butler reads the screen through its own small helper, "
             "macOS asks once and remembers across updates.")
GRANT_FIX = (
    "Grant Accessibility to \"%s\" in System Settings -> Privacy & Security -> "
    "Accessibility, then try the guide again. %s" % (GRANT_SUBJECT, GRANT_WHY)
)
# The helper itself is gone (uninstalled, quarantined, or not executable). No
# compiler and no grant can fix an install that is missing its binary.
OVERLAY_REINSTALL_FIX = (
    "The butler helper is missing or blocked — reinstall the plugin: "
    "hermes plugins install butler"
)
# The developer case: a working helper whose local Swift source changed. Build
# tooling is the remedy, and only here does the compiler belong in the answer.
OVERLAY_REBUILD_FIX = (
    "Local source changed: rebuild the helper with Command Line Tools "
    "(xcode-select --install), or reinstall the plugin"
)
#: Back-compat spelling: the unusable-overlay remedy is a reinstall.
OVERLAY_FIX = OVERLAY_REINSTALL_FIX
#: The plugin has no OCR fallback wired in; the grant is reported for
#: transparency only and never becomes a fix.
OCR_STATUS = "optional, not used"


def _open_panel(pane_url):
    try:
        subprocess.Popen(["open", pane_url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _doctor_detail(detail):
    """Doctor's own wording: never "Xcode", always Command Line Tools."""
    return str(detail).replace("Xcode", "Command Line Tools")


def _overlay_remedy():
    """Name the RIGHT overlay fix: a reinstall when the helper itself is gone.

    A missing or non-executable binary cannot be compiled into existence for a
    user who never installed a toolchain; the plugin install is the remedy.
    Only an installed, runnable helper whose source hash differs (a developer's
    edit that failed to compile) earns the Command Line Tools sentence.
    """
    exe = bp_overlay.binary_path()
    if not os.path.exists(exe) or not os.access(exe, os.X_OK):
        return OVERLAY_REINSTALL_FIX
    return OVERLAY_REBUILD_FIX


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
        "ocr": OCR_STATUS,
    }

    # Check the companion before probing a grant: if the overlay itself is
    # unusable, a failed grant probe would just mirror that and send the user
    # to Accessibility - a toggle that cannot make a missing helper appear.
    overlay_ok, overlay_detail = bp_overlay.build_if_needed()
    detail = _doctor_detail(overlay_detail)
    exe = bp_overlay.binary_path()
    out["overlay"] = detail
    out["overlay_binary"] = exe if os.path.exists(exe) else None
    out["overlay_archs"] = _overlay_archs(exe)

    # Reported for transparency only: nothing in this plugin uses the OCR
    # fallback, so the grant never becomes a fix or a prompt.
    screen_recording_granted = bool(bp_ocr.preflight_screen_record())
    out["screen_recording_granted"] = screen_recording_granted
    out["prompted"] = {}

    if not overlay_ok:
        # Unknown is not false: the companion never got to answer the grant
        # question, so neither grant claim nor grant instruction is honest.
        out["accessibility_granted"] = None
        out["capture"] = "unavailable: %s" % detail
        fix = _overlay_remedy()
        out["fix"] = fix
        out["fixes"] = [fix]
        # Nothing to prompt: Accessibility cannot heal a missing helper and the
        # plugin does not use Screen Recording. The reinstall sentence is the
        # whole answer.
        return out

    accessibility_granted = bool(bp_capture.trusted())
    out["accessibility_granted"] = accessibility_granted
    out["capture"] = "missing grant" if not accessibility_granted else "ok"

    fixes = []
    if not accessibility_granted:
        fixes.append(GRANT_FIX)
    if fixes:
        out["fix"] = fixes[0]
    out["fixes"] = fixes

    if ask and not accessibility_granted:
        out["prompted"]["accessibility"] = bp_capture._ask_accessibility()
        out["prompted"]["opened_panels"] = {
            "accessibility": _open_panel(PANEL_ACCESSIBILITY),
        }
    return out
