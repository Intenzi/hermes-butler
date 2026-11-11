"""Doctor attribution: an unusable companion is not a missing grant.

When the overlay cannot run (no compiler, failed compile, missing source), the
accessibility probe never got an answer. Reporting ``False`` would send the
user to grant Accessibility, which cannot help; doctor must report ``None``
(unknown) and the compiler fix instead.
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


def _patch(monkeypatch, *, overlay_result, trusted, screen_recording=True):
    monkeypatch.setattr(doctor.bp_capture, "mac_supported", lambda: True)
    monkeypatch.setattr(doctor.bp_overlay, "build_if_needed", lambda: overlay_result)
    # No real binary/lipo work in a unit test.
    monkeypatch.setattr(doctor.bp_overlay, "binary_path", lambda: "/nonexistent/ButlerOverlay")
    monkeypatch.setattr(doctor.bp_ocr, "preflight_screen_record",
                        lambda: screen_recording)
    monkeypatch.setattr(doctor.bp_capture, "trusted", trusted)


@pytest.mark.parametrize("detail", [
    "swiftc_not_found",
    "compile_failed: swiftc: command failed",
    "source_missing",
])
def test_unusable_overlay_reports_unknown_grant_and_compiler_fix(monkeypatch, detail):
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
    assert len(out["fixes"]) == 1
    assert out["fixes"][0] == doctor.OVERLAY_FIX
    assert "xcode-select --install" in out["fixes"][0]
    assert out["fix"] == doctor.OVERLAY_FIX
    # No grant instruction at all - that toggle cannot install a compiler.
    assert "Grant Accessibility" not in out["fix"]
    assert "Privacy & Security" not in out["fix"]
    assert "Accessibility" not in out["fix"]


def test_unusable_overlay_ask_never_prompts_accessibility(monkeypatch):
    _patch(monkeypatch, overlay_result=(False, "swiftc_not_found"),
           trusted=lambda: pytest.fail("grant probe must not run"),
           screen_recording=False)
    monkeypatch.setattr(doctor.bp_capture, "_ask_accessibility", lambda: pytest.fail(
        "must not prompt for Accessibility when the overlay is unusable"))
    monkeypatch.setattr(doctor.bp_ocr, "request_screen_record", lambda: True)
    opened = []
    monkeypatch.setattr(doctor, "_open_panel", lambda url: opened.append(url) or True)

    out = doctor.report(ask=True)

    assert "accessibility" not in out["prompted"]
    assert out["prompted"]["screen_recording"] is True
    assert out["prompted"]["opened_panels"] == {"screen_capture": True}
    assert opened == [doctor.PANEL_SCREENCAPTURE]


def test_usable_overlay_without_grant_keeps_the_existing_grant_fix(monkeypatch):
    _patch(monkeypatch, overlay_result=(True, "present"), trusted=lambda: False)
    monkeypatch.setattr(doctor, "_overlay_archs", lambda exe: "arm64 x86_64")

    out = doctor.report(ask=False)

    assert out["accessibility_granted"] is False
    assert out["capture"] == "missing grant"
    assert out["fix"] == doctor.GRANT_FIX
    assert out["fixes"] == [doctor.GRANT_FIX]
    assert "xcode-select --install" not in out["fix"]
    assert out["overlay_archs"] == "arm64 x86_64"


def test_usable_overlay_with_grant_offers_no_fix(monkeypatch):
    _patch(monkeypatch, overlay_result=(True, "present"), trusted=lambda: True)

    out = doctor.report(ask=False)

    assert out["accessibility_granted"] is True
    assert out["capture"] == "ok"
    assert out["fixes"] == []
    assert "fix" not in out


def test_usable_overlay_without_screen_recording_offers_only_that_fix(monkeypatch):
    _patch(monkeypatch, overlay_result=(True, "present"), trusted=lambda: True,
           screen_recording=False)

    out = doctor.report(ask=False)

    assert out["accessibility_granted"] is True
    assert out["ocr"] == "missing grant"
    assert out["fixes"] == [doctor.SCREEN_RECORDING_FIX]
    assert out["fix"] == doctor.SCREEN_RECORDING_FIX


def test_usable_overlay_ask_still_prompts_both_grants(monkeypatch):
    _patch(monkeypatch, overlay_result=(True, "present"), trusted=lambda: False,
           screen_recording=False)
    monkeypatch.setattr(doctor.bp_capture, "_ask_accessibility", lambda: True)
    monkeypatch.setattr(doctor.bp_ocr, "request_screen_record", lambda: True)
    monkeypatch.setattr(doctor, "_open_panel", lambda url: True)

    out = doctor.report(ask=True)

    assert out["prompted"]["accessibility"] is True
    assert out["prompted"]["screen_recording"] is True
    assert out["prompted"]["opened_panels"] == {
        "screen_capture": True, "accessibility": True,
    }


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
