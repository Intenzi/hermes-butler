"""Doctor attribution: the right remedy, and never a grant it does not use.

An unusable companion is not a permission problem: reporting ``False`` would
send the user to grant Accessibility, which cannot help. Doctor reports ``None``
(unknown) and names the remedy that matches the failure:

* the helper binary is missing/not executable -> reinstall the plugin (never a
  compiler: a user who never had one cannot compile it);
* the helper runs but the local source hash differs from ``.buildinfo`` -> the
  developer case, rebuild with Command Line Tools.

The plugin has no OCR fallback wired in, so ``ocr`` is reported as optional and
the Screen Recording grant is reported (for transparency) but never becomes a
fix or a prompt. "Xcode" must not appear in doctor output.
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

REINSTALL = ("The butler helper is missing or blocked — reinstall the plugin: "
             "hermes plugins install butler")
REBUILD = ("Local source changed: rebuild the helper with Command Line Tools "
           "(xcode-select --install), or reinstall the plugin")


def _patch(monkeypatch, *, overlay_result, trusted, screen_recording=True,
           binary="/nonexistent/ButlerOverlay"):
    monkeypatch.setattr(doctor.bp_capture, "mac_supported", lambda: True)
    monkeypatch.setattr(doctor.bp_overlay, "build_if_needed", lambda: overlay_result)
    # No real binary/lipo work in a unit test.
    monkeypatch.setattr(doctor.bp_overlay, "binary_path", lambda: binary)
    monkeypatch.setattr(doctor.bp_ocr, "preflight_screen_record",
                        lambda: screen_recording)
    monkeypatch.setattr(doctor.bp_capture, "trusted", trusted)


@pytest.mark.parametrize("detail", [
    "swiftc_not_found",
    "compile_failed: swiftc: command failed",
    "source_missing",
])
def test_a_missing_helper_names_reinstall_and_no_compiler(monkeypatch, detail):
    def _no_grant_probe():
        raise AssertionError(
            "the accessibility probe must not run when the companion cannot answer")

    _patch(monkeypatch, overlay_result=(False, detail), trusted=_no_grant_probe)

    out = doctor.report(ask=False)

    # Unknown is not false: the companion never answered the grant question.
    assert out["accessibility_granted"] is None
    assert out["capture"] == "unavailable: %s" % detail
    assert out["capture"].startswith("unavailable:")
    assert out["overlay"] == detail
    assert out["overlay_archs"] is None
    assert out["fixes"] == [doctor.OVERLAY_REINSTALL_FIX]
    assert out["fixes"] == [REINSTALL]
    assert out["fix"] == REINSTALL
    # A missing helper cannot be fixed by a compiler or a grant.
    assert "xcode-select" not in out["fix"]
    assert "Command Line Tools" not in out["fix"]
    assert "Xcode" not in out["fix"]
    assert "Grant Accessibility" not in out["fix"]
    assert "Privacy & Security" not in out["fix"]
    assert "Accessibility" not in out["fix"]
    assert out["ocr"] == "optional, not used"


def test_a_non_executable_helper_is_also_a_reinstall(monkeypatch, tmp_path):
    binary = tmp_path / "ButlerOverlay"
    binary.write_bytes(b"not chmod +x")
    # The helper exists but cannot run: still an install problem.
    _patch(monkeypatch, overlay_result=(False, "compile_failed: blocked"),
           trusted=lambda: pytest.fail("grant probe must not run"),
           binary=str(binary))

    out = doctor.report(ask=False)

    assert doctor._overlay_remedy() == REINSTALL
    assert out["fixes"] == [REINSTALL]
    assert out["overlay_binary"] == str(binary), "reported for transparency"


def test_a_source_hash_mismatch_names_command_line_tools(monkeypatch, tmp_path):
    """The developer case: a runnable helper whose Swift source changed."""
    binary = tmp_path / "ButlerOverlay"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    _patch(monkeypatch, overlay_result=(False, "swiftc_not_found"),
           trusted=lambda: pytest.fail("grant probe must not run"),
           binary=str(binary))

    out = doctor.report(ask=False)

    assert out["fixes"] == [doctor.OVERLAY_REBUILD_FIX]
    assert out["fixes"] == [REBUILD]
    assert "Command Line Tools" in out["fix"]
    assert "xcode-select --install" in out["fix"]
    assert "Xcode" not in out["fix"], "the term is Command Line Tools"
    assert "Grant Accessibility" not in out["fix"]


def test_no_fix_string_ever_says_xcode(monkeypatch):
    for overlay_result, trusted, screen_recording in (
        ((False, "swiftc_not_found"), lambda: pytest.fail("no probe"), True),
        ((False, "compile_failed: requires Xcode"), lambda: pytest.fail("no probe"), True),
        ((True, "present"), lambda: False, True),
        ((True, "present"), lambda: True, False),
    ):
        _patch(monkeypatch, overlay_result=overlay_result, trusted=trusted,
               screen_recording=screen_recording)
        out = doctor.report(ask=False)
        for fix in out["fixes"]:
            assert "Xcode" not in fix
            assert "Screen Recording" not in fix
    # A raw compiler detail is sanitized too: the banned word cannot leak.
    assert "Xcode" not in doctor._doctor_detail("requires Xcode to build")


def test_unusable_overlay_ask_never_prompts_a_grant_it_cannot_use(monkeypatch):
    _patch(monkeypatch, overlay_result=(False, "swiftc_not_found"),
           trusted=lambda: pytest.fail("grant probe must not run"),
           screen_recording=False)
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
    _patch(monkeypatch, overlay_result=(True, "present"), trusted=lambda: False,
           screen_recording=False)
    monkeypatch.setattr(doctor.bp_capture, "_ask_accessibility", lambda: True)
    monkeypatch.setattr(doctor.bp_ocr, "request_screen_record",
                        lambda: pytest.fail("no Screen Recording prompt: OCR is not used"))
    monkeypatch.setattr(doctor, "_open_panel", lambda url: True)

    out = doctor.report(ask=True)

    assert out["prompted"]["accessibility"] is True
    assert "screen_recording" not in out["prompted"]
    assert out["prompted"]["opened_panels"] == {"accessibility": True}


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
