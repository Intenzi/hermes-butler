"""Butler doctor: grants, capabilities, companion helper, configuration report.

`ask=True` pops macOS's own Accessibility dialog
(AXIsProcessTrustedWithOptions(prompt=True)) and deep-links the pane, so the
user never hunts for the toggle. The final click in System Settings stays a
user action - macOS does not permit self-granting TCC consent.

The companion overlay is checked FIRST. A companion that cannot run is not a
permission problem: reporting "grant Accessibility" would send the user to a
toggle that cannot help when the real cause is a missing helper. In that case
the accessibility answer is ``None`` (unknown), never ``False``.

The overlay is reported as one of six statuses: ``present``, ``downloaded``,
``compiled`` (usable), or ``download_failed``, ``swiftc_not_found``,
``helper-unavailable`` (not usable). There is ONE remedy for the unusable
three: reinstall the plugin, because the plugin can download the attested
helper and nobody has to own a compiler. Command Line Tools remain an option
only for someone who chooses to build the Swift source locally.
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
#: Every unusable-overlay state has the same remedy: an incomplete install gets
#: reinstalled. The plugin downloads the attested helper, so a missing binary or
#: a failed download never means "install a compiler".
OVERLAY_FIX = (
    "Butler could not get its screen helper: reinstall the plugin "
    "(hermes plugins install butler). Command Line Tools are only needed if you "
    "build from source."
)
#: Back-compat spelling used by older callers.
OVERLAY_REINSTALL_FIX = OVERLAY_FIX
#: The statuses doctor reports for the overlay; anything else is normalized.
OVERLAY_STATUSES = (
    "present",
    "downloaded",
    "compiled",
    "download_failed",
    "swiftc_not_found",
    "helper-unavailable",
)
#: Statuses that mean the companion cannot answer anything.
OVERLAY_UNUSABLE = ("download_failed", "swiftc_not_found", "helper-unavailable")
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


def _overlay_status(detail):
    """Normalize whatever the resolver logged into one of ``OVERLAY_STATUSES``.

    The resolver's detail strings carry a human reason (a URL, a swiftc error);
    the report keeps the closed status set so a user-facing answer can be
    chosen without pattern-matching English prose.
    """
    text = str(detail or "").strip()
    if text in OVERLAY_STATUSES:
        return text
    if text.startswith("download_failed"):
        return "download_failed"
    return "helper-unavailable"


def _overlay_remedy():
    """The one fix for an unusable helper: reinstall the plugin.

    Downloading the attested release asset is the normal path, so a missing
    binary, a failed download, or no compiler all mean the same thing to the
    user: the install is incomplete, not that they need a toolchain.
    """
    return OVERLAY_FIX


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
    # Doctor is the user asking "is it fixed now": never let a cached download
    # failure answer that question.
    try:
        bp_overlay._remember_download_failure(None)
    except Exception:  # noqa: BLE001
        pass
    overlay_ok, overlay_detail = bp_overlay.build_if_needed()
    status = _overlay_status(overlay_detail)
    exe = bp_overlay.binary_path()
    out["overlay"] = status
    out["overlay_binary"] = exe if os.path.exists(exe) else None
    out["overlay_archs"] = _overlay_archs(exe)

    # Reported for transparency only: nothing in this plugin uses the OCR
    # fallback, so the grant never becomes a fix or a prompt.
    screen_recording_granted = bool(bp_ocr.preflight_screen_record())
    out["screen_recording_granted"] = screen_recording_granted
    out["prompted"] = {}

    if not overlay_ok or status in OVERLAY_UNUSABLE:
        # Unknown is not false: the companion never got to answer the grant
        # question, so neither grant claim nor grant instruction is honest.
        out["accessibility_granted"] = None
        out["capture"] = "unavailable: %s" % status
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
