"""Companion build freshness and the shipped universal binary.

The committed binary must survive a fresh checkout on a Mac without Xcode. A
``git clone`` gives every file a new mtime, so freshness is decided by the
source hash in ``.buildinfo`` - these tests pin that behaviour, plus the fact
that the shipped binary carries both arm64 and x86_64 slices.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butler_core import overlay  # noqa: E402

SOURCE = b"// ButlerOverlay.swift fixture\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _layout(tmp_path, monkeypatch, *, buildinfo, executable=True):
    """Fake companion layout; build_if_needed() reads only these paths."""
    exe = tmp_path / "ButlerOverlay"
    exe.write_bytes(b"\xcf\xfa\xed\xfe stub")
    if executable:
        exe.chmod(0o755)
    src = tmp_path / "ButlerOverlay.swift"
    src.write_bytes(SOURCE)
    info = tmp_path / ".buildinfo"
    if buildinfo is not None:
        if isinstance(buildinfo, (str, bytes)):
            info.write_text(buildinfo, encoding="utf-8")
        else:
            info.write_text(json.dumps(buildinfo), encoding="utf-8")
    monkeypatch.setattr(overlay, "binary_path", lambda: str(exe))
    monkeypatch.setattr(overlay, "binary_source_path", lambda: str(src))
    monkeypatch.setattr(overlay, "buildinfo_path", lambda: str(info))
    # Keep the tests off the real shutil/subprocess: any rebuild attempt fails
    # loudly instead of silently compiling the fixture source.
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: pytest.fail("unexpected compiler lookup: %s" % name)))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(
        run=lambda *args, **kwargs: pytest.fail("unexpected rebuild attempt")))
    return exe, src, info


def test_matching_source_hash_is_present_even_when_source_mtime_is_newer(
        tmp_path, monkeypatch):
    # The checkout case: binary mtime is old, source mtime is new. Only the
    # content hash may decide freshness.
    buildinfo = {"source_sha256": _sha(SOURCE), "archs": ["arm64", "x86_64"],
                 "built_at": 1}
    exe, src, _ = _layout(tmp_path, monkeypatch, buildinfo=buildinfo)
    old = time.time() - 3600
    os.utime(exe, (old, old))
    os.utime(src, None)

    assert overlay.build_if_needed() == (True, "present")


def test_hash_mismatch_attempts_a_rebuild_and_needs_swiftc(tmp_path, monkeypatch):
    buildinfo = {"source_sha256": "0" * 64, "archs": ["arm64"], "built_at": 1}
    _, _, _ = _layout(tmp_path, monkeypatch, buildinfo=buildinfo)
    lookups = []
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: lookups.append(name) or None))

    # A mismatch is the one case that must go looking for a compiler.
    assert overlay.build_if_needed() == (False, "swiftc_not_found")
    assert lookups == ["swiftc"]


def test_missing_buildinfo_trusts_the_shipped_binary(tmp_path, monkeypatch):
    # A missing sidecar must never cost a judge a compiler.
    exe, _, _ = _layout(tmp_path, monkeypatch, buildinfo=None)
    assert exe.stat().st_mode & 0o111

    assert overlay.build_if_needed() == (True, "present")


def test_unreadable_buildinfo_trusts_the_shipped_binary(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch, buildinfo="{not json")

    assert overlay.build_if_needed() == (True, "present")


def test_non_executable_binary_needs_a_rebuild(tmp_path, monkeypatch):
    buildinfo = {"source_sha256": _sha(SOURCE), "archs": ["arm64"], "built_at": 1}
    _layout(tmp_path, monkeypatch, buildinfo=buildinfo, executable=False)
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: None))

    assert overlay.build_if_needed() == (False, "swiftc_not_found")


def test_successful_rebuild_swaps_atomically_and_refreshes_buildinfo(
        tmp_path, monkeypatch):
    buildinfo = {"source_sha256": "0" * 64, "archs": ["arm64"], "built_at": 1}
    exe, _, info = _layout(tmp_path, monkeypatch, buildinfo=buildinfo)
    seen = []

    def fake_run(args, **kwargs):
        seen.append(list(args))
        # swiftc writes the requested output; the real binary must not be the
        # file the compiler touches.
        Path(args[3]).write_bytes(b"rebuilt companion")
        Path(args[3]).chmod(0o755)

    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: "/usr/bin/swiftc" if name == "swiftc" else None))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(run=fake_run))

    assert overlay.build_if_needed() == (True, "compiled")
    assert seen[0][:3] == ["swiftc", "-O", "-o"]
    assert seen[0][3] == str(exe) + ".new"
    assert seen[0][4] == str(tmp_path / "ButlerOverlay.swift")
    assert exe.read_bytes() == b"rebuilt companion"
    refreshed = json.loads(info.read_text(encoding="utf-8"))
    assert refreshed["source_sha256"] == _sha(SOURCE)
    assert isinstance(refreshed["archs"], list) and refreshed["archs"]
    assert isinstance(refreshed["built_at"], int)

    # The rewritten sidecar makes the next run a no-op: no compiler lookup.
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: pytest.fail("now-fresh binary must not compile")))
    assert overlay.build_if_needed() == (True, "present")


def test_failed_rebuild_leaves_the_working_binary_untouched(tmp_path, monkeypatch):
    buildinfo = {"source_sha256": "0" * 64, "archs": ["arm64"], "built_at": 1}
    exe, _, info = _layout(tmp_path, monkeypatch, buildinfo=buildinfo)
    before_binary = exe.read_bytes()
    before_info = info.read_text(encoding="utf-8")

    def exploding_run(args, **kwargs):
        Path(args[3]).write_bytes(b"half-written slice")
        raise RuntimeError("swiftc exploded")

    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: "/usr/bin/swiftc"))
    monkeypatch.setattr(overlay, "subprocess", types.SimpleNamespace(run=exploding_run))

    ok, detail = overlay.build_if_needed()

    assert ok is False
    assert detail.startswith("compile_failed:")
    assert exe.read_bytes() == before_binary
    assert info.read_text(encoding="utf-8") == before_info
    assert not Path(str(exe) + ".new").exists()


@pytest.mark.skipif(shutil.which("lipo") is None, reason="lipo is not installed")
def test_shipped_binary_is_universal():
    exe = ROOT / "butler_core" / "services" / "ButlerOverlay"
    assert exe.is_file(), "the companion binary is not committed"

    proc = subprocess.run(["lipo", "-archs", str(exe)], check=True,
                          capture_output=True, text=True)
    archs = proc.stdout.split()
    assert "arm64" in archs
    assert "x86_64" in archs


def test_buildinfo_sidecar_matches_the_shipped_source():
    info = json.loads(
        (ROOT / "butler_core" / "services" / ".buildinfo").read_text(encoding="utf-8"))
    source = (ROOT / "butler_core" / "overlay_swift" / "ButlerOverlay.swift").read_bytes()

    assert info["source_sha256"] == _sha(source)
    assert info["archs"] == ["arm64", "x86_64"]
    assert isinstance(info["built_at"], int)
