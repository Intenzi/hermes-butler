"""Tests for overlay pet resolution, rebuild freshness, and pet-op startup."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butler_core import overlay  # noqa: E402


def _make_pet(home: Path, slug: str) -> Path:
    pet_dir = home / "pets" / slug
    pet_dir.mkdir(parents=True)
    (pet_dir / "spritesheet.webp").write_bytes(b"RIFF")
    (pet_dir / "pet.json").write_text(
        json.dumps({"id": slug, "displayName": slug, "spritesheetPath": "spritesheet.webp"}),
        encoding="utf-8",
    )
    return pet_dir


def _write_config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_frames_dir_uses_configured_slug(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    wanted = _make_pet(tmp_path, "hei-mao-butler")
    _make_pet(tmp_path, "aaa-fallback")
    _write_config(
        tmp_path,
        "display:\n  pet:\n    enabled: true\n    slug: hei-mao-butler\n    scale: 0.42\n",
    )

    assert overlay.resolve_frames_dir() == str(wanted)
    op = overlay.resolve_pet_op()
    assert op == {"op": "pet", "frames_dir": str(wanted), "scale": 0.42,
                  "pet_enabled": True}


def test_pet_op_marks_disabled_pet(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    wanted = _make_pet(tmp_path, "robotocat")
    _write_config(
        tmp_path,
        "display:\n  pet:\n    enabled: false\n    slug: robotocat\n    scale: 0.5\n",
    )

    op = overlay.resolve_pet_op()

    assert op["frames_dir"] == str(wanted)
    assert op["pet_enabled"] is False


def test_pet_op_defaults_enabled_when_config_is_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_pet(tmp_path, "robotocat")

    op = overlay.resolve_pet_op()

    assert op["pet_enabled"] is True


def test_frames_dir_falls_back_to_first_valid_without_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_pet(tmp_path, "second")
    first = _make_pet(tmp_path, "first")
    # A directory without a spritesheet must be skipped.
    (tmp_path / "pets" / "empty").mkdir()

    assert overlay.resolve_frames_dir() == str(first)


def test_frames_dir_returns_none_without_pets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "pets").mkdir()

    assert overlay.resolve_frames_dir() is None
    assert overlay.resolve_pet_op() is None


def test_build_if_needed_recompiles_stale_binary(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "ButlerOverlay"
    source = tmp_path / "ButlerOverlay.swift"
    binary.write_bytes(b"old")
    binary.chmod(0o755)
    source.write_text("// new source", encoding="utf-8")
    os.utime(binary, (1_000, 1_000))
    os.utime(source, (2_000, 2_000))

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(overlay, "binary_path", lambda: str(binary))
    monkeypatch.setattr(overlay, "binary_source_path", lambda: str(source))
    monkeypatch.setattr(overlay.shutil, "which", lambda name: "/usr/bin/swiftc")
    monkeypatch.setattr(overlay.subprocess, "run", fake_run)

    assert overlay.build_if_needed() == (True, "compiled")
    assert len(calls) == 1
    assert calls[0][0] == ["swiftc", "-O", "-o", str(binary), str(source)]


def test_build_if_needed_skips_fresh_binary(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "ButlerOverlay"
    source = tmp_path / "ButlerOverlay.swift"
    binary.write_bytes(b"new")
    binary.chmod(0o755)
    source.write_text("// old source", encoding="utf-8")
    os.utime(source, (1_000, 1_000))
    os.utime(binary, (2_000, 2_000))

    def fail_run(*args, **kwargs):
        raise AssertionError("fresh binary must not compile")

    monkeypatch.setattr(overlay, "binary_path", lambda: str(binary))
    monkeypatch.setattr(overlay, "binary_source_path", lambda: str(source))
    monkeypatch.setattr(overlay.subprocess, "run", fail_run)

    assert overlay.build_if_needed() == (True, "present")


class _FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, chunk):
        self.data += chunk
        return len(chunk)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    pid = 4242

    def __init__(self):
        self.stdin = _FakeStdin()


def test_start_sends_one_pet_op_with_resolved_payload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    wanted = _make_pet(tmp_path, "robotocat")
    _write_config(tmp_path, "display:\n  pet:\n    slug: robotocat\n    scale: 0.5\n")

    proc = _FakeProc()
    monkeypatch.setattr(overlay, "build_if_needed", lambda: (True, "present"))
    monkeypatch.setattr(overlay.subprocess, "Popen", lambda *args, **kwargs: proc)

    ok, detail = overlay.Overlay().start()

    assert ok is True
    assert detail == "spawned pid 4242"
    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "op": "pet",
        "frames_dir": str(wanted),
        "scale": 0.5,
        "pet_enabled": True,
    }


def test_done_sends_the_farewell_op() -> None:
    proc = _FakeProc()
    controller = overlay.Overlay()
    controller.proc = proc

    controller.done()

    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    assert [json.loads(line) for line in lines] == [{"op": "done"}]


def test_claim_chip_op_is_sent_when_armed() -> None:
    proc = _FakeProc()
    controller = overlay.Overlay()
    controller.proc = proc

    controller.claim_chip(True)
    controller.claim_chip(False)

    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    assert [json.loads(line) for line in lines] == [
        {"op": "claim_chip", "enabled": True},
        {"op": "claim_chip", "enabled": False},
    ]


def test_claim_click_event_reaches_the_engine_callback() -> None:
    controller = overlay.Overlay()
    seen = []
    controller.on_event = seen.append

    controller._handle_message({"op": "claim_click"})

    assert seen == [{"op": "claim_click"}]


def test_reader_dispatches_click_events_and_queues_ax_responses() -> None:
    read_fd, write_fd = os.pipe()
    proc = _FakeProc()
    proc.stdout = os.fdopen(read_fd, "rb", buffering=0)
    controller = overlay.Overlay()
    controller.proc = proc
    seen = []
    controller.on_event = seen.append
    try:
        controller._start_reader()
        os.write(write_fd, b'{"op":"claim_click"}\n')
        os.write(write_fd, b'{"trusted": true, "count": 0}\n')
        reply = controller._inbox.get(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        os.close(write_fd)
        proc.stdout.close()

    assert seen == [{"op": "claim_click"}]
    assert reply == {"trusted": True, "count": 0}


def test_companion_source_has_the_popout_claim_chip_contract() -> None:
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert 'case "claim_chip"' in source
    assert '{"op":"claim_click"}' in source
    assert "CGWindowListCopyWindowInfo" in source
    assert "hermesPetWindowFrame" in source
    assert "!frame.intersects(pet)" in source


def _swift_function_body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    assert start != -1, f"missing Swift marker: {start_marker}"
    end = source.find(end_marker, start + len(start_marker))
    assert end != -1, f"missing Swift marker: {end_marker}"
    return source[start:end]


def test_pet_anchor_is_pid_and_shape_based_not_title_or_layer() -> None:
    """The pop-out pet's AX title is "Hermes" and its panel is on layer 3.

    An empty-title gate or a layer-0 CGWindow filter both make the claim chip
    invisible, so this pins identification by owner pid + shape only.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    # A CGWindow fallback that does not require AX exists at all.
    assert "cgWindowFrameList" in source
    assert "if title.isEmpty, isPetShaped(" not in source

    pet_body = _swift_function_body(
        source,
        "private func hermesPetWindowFrame(",
        "private func claimChipSize(",
    )
    assert "title.isEmpty" not in pet_body
    assert "axTopmostLayer0Windows" not in pet_body
    assert "let pid = hermesPid()" in pet_body
    assert "lastPetFrame" in pet_body
    # Both sources must feed the union, not just exist as helpers.
    assert "axWindowFrameList(pid: pid)" in pet_body
    assert "cgWindowFrameList(pid: pid)" in pet_body
    # Sticky anchoring breaks near ties by area and drops stale anchors.
    assert "anchorSlack" in pet_body
    assert "lastPetFrame = nil" in pet_body

    cg_body = _swift_function_body(
        source,
        "private func cgWindowFrameList(",
        "private func axTopmostLayer0Windows(",
    )
    assert "kCGWindowOwnerPID" in cg_body
    assert "kCGWindowLayer" not in cg_body
    assert "layer == 0" not in cg_body
    assert "window[kCGWindowName" not in cg_body


def test_start_sends_no_pet_op_when_no_pets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "pets").mkdir()
    proc = _FakeProc()
    monkeypatch.setattr(overlay, "build_if_needed", lambda: (True, "present"))
    monkeypatch.setattr(overlay.subprocess, "Popen", lambda *args, **kwargs: proc)

    assert overlay.Overlay().start()[0] is True
    assert proc.stdin.data == b""


def test_capture_request_spawns_without_a_pet_double(tmp_path: Path, monkeypatch) -> None:
    """A companion spawned only to read the screen must not park a pet double."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_pet(tmp_path, "robotocat")
    _write_config(tmp_path, "display:\n  pet:\n    slug: robotocat\n    scale: 0.5\n")

    proc = _FakeProc()
    monkeypatch.setattr(overlay, "build_if_needed", lambda: (True, "present"))
    monkeypatch.setattr(overlay.subprocess, "Popen", lambda *args, **kwargs: proc)

    # The capture transport restarts a dead companion on demand.
    overlay.Overlay().request({"op": "axcapture", "target": None})

    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    ops = [json.loads(line)["op"] for line in lines]
    assert "pet" not in ops
    assert ops == ["axcapture"]


def test_guide_start_reasserts_the_pet_on_a_capture_spawned_companion(tmp_path: Path, monkeypatch) -> None:
    """A petless companion left by a capture must still show the double for a guide."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    wanted = _make_pet(tmp_path, "robotocat")
    _write_config(tmp_path, "display:\n  pet:\n    slug: robotocat\n    scale: 0.5\n")

    proc = _FakeProc()
    controller = overlay.Overlay()
    controller.proc = proc

    ok, detail = controller.start()

    assert ok is True
    assert detail == "already running pid 4242"
    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    assert [json.loads(line) for line in lines] == [{
        "op": "pet",
        "frames_dir": str(wanted),
        "scale": 0.5,
        "pet_enabled": True,
    }]


def test_summon_double_skips_entirely_when_no_petdex_pet(
        tmp_path: Path, monkeypatch) -> None:
    """Petless: the summon must not spawn a companion or send a pet op."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "pets").mkdir()
    monkeypatch.setattr(overlay, "_SHARED_OVERLAY", None)
    monkeypatch.setattr(
        overlay.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("petless summon must not spawn the companion")),
    )

    assert overlay.summon_double() is False


def test_summon_double_spawns_pet_then_farewells(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    wanted = _make_pet(tmp_path, "robotocat")
    _write_config(tmp_path, "display:\n  pet:\n    slug: robotocat\n    scale: 0.5\n")
    monkeypatch.setattr(overlay, "_SHARED_OVERLAY", None)

    proc = _FakeProc()
    monkeypatch.setattr(overlay, "build_if_needed", lambda: (True, "present"))
    monkeypatch.setattr(overlay.subprocess, "Popen", lambda *args, **kwargs: proc)

    assert overlay.summon_double() is True
    for _ in range(100):
        if b"done" in proc.stdin.data:
            break
        __import__("time").sleep(0.01)
    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    ops = [json.loads(line) for line in lines]
    assert ops[0] == {
        "op": "pet",
        "frames_dir": str(wanted),
        "scale": 0.5,
        "pet_enabled": True,
    }
    assert {"op": "done"} in ops
