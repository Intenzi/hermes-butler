"""Doctor attribution: the right remedy, and never a grant it does not use.

An unusable companion is not a permission problem: reporting ``False`` would
send the user to grant Accessibility, which cannot help. Doctor reports ``None``
(unknown) and names the one remedy that matches every unusable state: reinstall
the plugin. The helper is downloaded from this repo's attested release, so a
missing binary, a failed download, or a missing compiler never means the user
must install a toolchain.

The plugin has no OCR fallback wired in, so ``ocr`` is reported as optional and
the Screen Recording grant is reported (for transparency) but never becomes a
fix or a prompt. The other IDE's name must not appear in doctor output; Command
Line Tools is only ever a "build from source" option.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butler_core import doctor  # noqa: E402

REINSTALL = ("Butler could not get its screen helper: reinstall the plugin "
             "(hermes plugins install butler). Command Line Tools are only "
             "needed if you build from source.")


def _patch(monkeypatch, *, overlay_result, trusted, screen_recording=True,
           binary="/nonexistent/ButlerOverlay"):
    monkeypatch.setattr(doctor.bp_capture, "mac_supported", lambda: True)
    monkeypatch.setattr(doctor.bp_overlay, "build_if_needed", lambda: overlay_result)
    # No real binary/lipo work in a unit test.
    monkeypatch.setattr(doctor.bp_overlay, "binary_path", lambda: binary)
    monkeypatch.setattr(doctor.bp_ocr, "preflight_screen_record",
                        lambda: screen_recording)
    monkeypatch.setattr(doctor.bp_capture, "trusted", trusted)


def _no_grant_probe():
    raise AssertionError(
        "the accessibility probe must not run when the companion cannot answer")


@pytest.mark.parametrize("detail,status", [
    ("swiftc_not_found", "swiftc_not_found"),
    ("helper-unavailable", "helper-unavailable"),
    ("download_failed: sha256 mismatch", "download_failed"),
    ("compile_failed: swiftc: command failed", "helper-unavailable"),
    ("source_missing", "helper-unavailable"),
])
def test_an_unusable_helper_names_one_reinstall_fix(monkeypatch, detail, status):
    _patch(monkeypatch, overlay_result=(False, detail), trusted=_no_grant_probe)

    out = doctor.report(ask=False)

    # Unknown is not false: the companion never answered the grant question.
    assert out["accessibility_granted"] is None
    assert out["overlay"] == status
    assert out["overlay"] in doctor.OVERLAY_STATUSES
    assert out["capture"] == "unavailable: %s" % status
    assert out["capture"].startswith("unavailable:")
    assert out["overlay_archs"] is None
    assert out["fixes"] == [doctor.OVERLAY_FIX]
    assert out["fixes"] == [REINSTALL]
    assert out["fix"] == REINSTALL
    # One fix, no compiler instruction and no grant instruction.
    assert "Xcode" not in out["fix"]
    assert "xcode-select" not in out["fix"]
    assert "Grant Accessibility" not in out["fix"]
    assert "Privacy & Security" not in out["fix"]
    assert "Accessibility" not in out["fix"]
    assert "Command Line Tools" in out["fix"], "only as the build-from-source caveat"
    assert out["ocr"] == "optional, not used"


def test_a_non_executable_helper_is_also_a_reinstall(monkeypatch, tmp_path):
    binary = tmp_path / "ButlerOverlay"
    binary.write_bytes(b"not chmod +x")
    # The helper exists but cannot run: still an install problem.
    _patch(monkeypatch, overlay_result=(False, "download_failed: timeout"),
           trusted=_no_grant_probe, binary=str(binary))

    out = doctor.report(ask=False)

    assert doctor._overlay_remedy() == REINSTALL
    assert out["fixes"] == [REINSTALL]
    assert out["overlay"] == "download_failed"
    assert out["overlay_binary"] == str(binary), "reported for transparency"


def test_a_downloaded_helper_is_usable_and_keeps_the_grant_path(monkeypatch):
    _patch(monkeypatch, overlay_result=(True, "downloaded"), trusted=lambda: False)
    monkeypatch.setattr(doctor, "_overlay_archs", lambda exe: "arm64 x86_64")

    out = doctor.report(ask=False)

    assert out["overlay"] == "downloaded"
    assert out["accessibility_granted"] is False
    assert out["capture"] == "missing grant"
    assert out["fix"] == doctor.GRANT_FIX
    assert out["fixes"] == [doctor.GRANT_FIX]
    assert "xcode-select --install" not in out["fix"]
    assert out["overlay_archs"] == "arm64 x86_64"
    assert out["ocr"] == "optional, not used"


def test_a_compiled_helper_is_usable_too(monkeypatch):
    _patch(monkeypatch, overlay_result=(True, "compiled"), trusted=lambda: True)

    out = doctor.report(ask=False)

    assert out["overlay"] == "compiled"
    assert out["accessibility_granted"] is True
    assert out["capture"] == "ok"
    assert out["fixes"] == []
    assert "fix" not in out


def test_no_fix_string_ever_says_xcode(monkeypatch):
    for overlay_result, trusted, screen_recording in (
        ((False, "swiftc_not_found"), _no_grant_probe, True),
        ((False, "download_failed: requires Xcode"), _no_grant_probe, True),
        ((False, "compile_failed: requires Xcode"), _no_grant_probe, True),
        ((True, "present"), lambda: False, True),
        ((True, "present"), lambda: True, False),
        ((True, "downloaded"), lambda: False, True),
    ):
        _patch(monkeypatch, overlay_result=overlay_result, trusted=trusted,
               screen_recording=screen_recording)
        out = doctor.report(ask=False)
        rendered = repr(out)
        assert "Xcode" not in rendered, "the term must not reach doctor output"
        for fix in out["fixes"]:
            assert "Screen Recording" not in fix


def test_unusable_overlay_ask_never_prompts_a_grant_it_cannot_use(monkeypatch):
    _patch(monkeypatch, overlay_result=(False, "helper-unavailable"),
           trusted=_no_grant_probe, screen_recording=False)
    monkeypatch.setattr(doctor.bp_capture, "_ask_accessibility",
                        lambda: pytest.fail(
                            "must not prompt for Accessibility when the overlay is unusable"))
    monkeypatch.setattr(doctor.bp_ocr, "request_screen_record",
                        lambda: pytest.fail("no Screen Recording prompt: OCR is not used"))
    monkeypatch.setattr(doctor, "_open_panel", lambda url: True)

    out = doctor.report(ask=True)

    assert out["prompted"] == {}, "a missing helper cannot be fixed by a grant"
    assert "accessibility" not in out["prompted"]
    assert "screen_recording" not in out["prompted"]


def test_usable_overlay_without_grant_keeps_only_the_accessibility_fix(monkeypatch):
    _patch(monkeypatch, overlay_result=(True, "present"), trusted=lambda: False)
    monkeypatch.setattr(doctor, "_overlay_archs", lambda exe: "arm64 x86_64")

    out = doctor.report(ask=False)

    assert out["accessibility_granted"] is False
    assert out["capture"] == "missing grant"
    assert out["fix"] == doctor.GRANT_FIX
    assert out["fixes"] == [doctor.GRANT_FIX]
    assert "xcode-select --install" not in out["fix"]
    assert out["overlay_archs"] == "arm64 x86_64"
    assert out["ocr"] == "optional, not used"


def test_usable_overlay_with_grant_offers_no_fix(monkeypatch):
    _patch(monkeypatch, overlay_result=(True, "present"), trusted=lambda: True)

    out = doctor.report(ask=False)

    assert out["accessibility_granted"] is True
    assert out["capture"] == "ok"
    assert out["fixes"] == []
    assert "fix" not in out
    assert out["ocr"] == "optional, not used"
    assert out["screen_recording_granted"] is True


def test_missing_screen_recording_is_reported_but_never_becomes_a_fix(monkeypatch):
    _patch(monkeypatch, overlay_result=(True, "present"), trusted=lambda: True,
           screen_recording=False)

    out = doctor.report(ask=False)

    assert out["screen_recording_granted"] is False
    assert out["ocr"] == "optional, not used"
    assert out["fixes"] == [], "the plugin has no OCR fallback to fix for"
    assert "fix" not in out


def test_usable_overlay_ask_prompts_accessibility_only(monkeypatch):
    _patch(monkeypatch, overlay_result=(True, "downloaded"), trusted=lambda: False,
           screen_recording=False)
    monkeypatch.setattr(doctor.bp_capture, "_ask_accessibility", lambda: True)
    monkeypatch.setattr(doctor.bp_ocr, "request_screen_record",
                        lambda: pytest.fail("no Screen Recording prompt: OCR is not used"))
    monkeypatch.setattr(doctor, "_open_panel", lambda url: True)

    out = doctor.report(ask=True)

    assert out["prompted"]["accessibility"] is True
    assert "screen_recording" not in out["prompted"]
    assert out["prompted"]["opened_panels"] == {"accessibility": True}


@pytest.mark.parametrize("detail,status", [
    ("present", "present"),
    ("downloaded", "downloaded"),
    ("compiled", "compiled"),
    ("download_failed: timeout", "download_failed"),
    ("swiftc_not_found", "swiftc_not_found"),
    ("helper-unavailable", "helper-unavailable"),
    ("something unexpected", "helper-unavailable"),
    ("", "helper-unavailable"),
])
def test_overlay_status_is_always_one_of_the_closed_set(detail, status):
    assert doctor._overlay_status(detail) == status
    assert status in doctor.OVERLAY_STATUSES


def test_overlay_archs_is_null_without_the_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "shutil", types.SimpleNamespace(
        which=lambda name: pytest.fail("must not look for lipo without a binary")))

    assert doctor._overlay_archs(str(tmp_path / "missing")) is None


def test_overlay_archs_is_null_without_lipo(tmp_path, monkeypatch):
    exe = tmp_path / "ButlerOverlay"
    exe.write_bytes(b"x")
    monkeypatch.setattr(doctor, "shutil", types.SimpleNamespace(
        which=lambda name: None))

    assert doctor._overlay_archs(str(exe)) is None


def test_overlay_archs_is_null_when_lipo_fails(tmp_path, monkeypatch):
    exe = tmp_path / "ButlerOverlay"
    exe.write_bytes(b"x")
    monkeypatch.setattr(doctor, "shutil", types.SimpleNamespace(
        which=lambda name: "/usr/bin/lipo"))

    def explode(*args, **kwargs):
        raise RuntimeError("lipo exploded")

    monkeypatch.setattr(doctor, "subprocess", types.SimpleNamespace(run=explode))

    assert doctor._overlay_archs(str(exe)) is None


def test_overlay_archs_reads_lipo_output(tmp_path, monkeypatch):
    exe = tmp_path / "ButlerOverlay"
    exe.write_bytes(b"x")
    monkeypatch.setattr(doctor, "shutil", types.SimpleNamespace(
        which=lambda name: "/usr/bin/lipo"))
    monkeypatch.setattr(doctor, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: types.SimpleNamespace(stdout="x86_64 arm64\n")))

    assert doctor._overlay_archs(str(exe)) == "x86_64 arm64"


def test_overlay_archs_is_null_when_lipo_prints_nothing(tmp_path, monkeypatch):
    exe = tmp_path / "ButlerOverlay"
    exe.write_bytes(b"x")
    monkeypatch.setattr(doctor, "shutil", types.SimpleNamespace(
        which=lambda name: "/usr/bin/lipo"))
    monkeypatch.setattr(doctor, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: types.SimpleNamespace(stdout=" \n")))

    assert doctor._overlay_archs(str(exe)) is None
