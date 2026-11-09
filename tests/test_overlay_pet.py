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


def test_claim_chip_op_carries_the_sprite_anchor_when_known() -> None:
    """The companion must be told the sprite so the chip lands next to the pet.

    ``claim_chip(True)`` stays byte-identical to the pre-sprite wire format so
    an older companion (or a petless install) keeps working.
    """
    proc = _FakeProc()
    controller = overlay.Overlay()
    controller.proc = proc

    controller.claim_chip(True, sprite=(63.36, 68.64))
    controller.claim_chip(True)

    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    assert [json.loads(line) for line in lines] == [
        {"op": "claim_chip", "enabled": True,
         "sprite_w": 63, "sprite_h": 69, "pad_bottom": 24},
        {"op": "claim_chip", "enabled": True},
    ]


def test_claim_chip_malformed_sprite_falls_back_to_the_window_op() -> None:
    proc = _FakeProc()
    controller = overlay.Overlay()
    controller.proc = proc

    controller.claim_chip(True, sprite="not-a-size")

    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    assert [json.loads(line) for line in lines] == [
        {"op": "claim_chip", "enabled": True},
    ]


def test_resolve_pet_sprite_scales_the_192x208_frame(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_pet(tmp_path, "robotocat")
    _write_config(tmp_path, "display:\n  pet:\n    slug: robotocat\n    scale: 0.5\n")

    assert overlay.resolve_pet_sprite() == (96.0, 104.0)


def test_resolve_pet_sprite_uses_the_default_scale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_pet(tmp_path, "robotocat")

    sprite = overlay.resolve_pet_sprite()

    # 192x208 * default 0.33 rounds to the anchored 63x69 sprite box.
    assert overlay._rounded_sprite(sprite) == (63, 69)


def test_resolve_pet_sprite_is_none_without_a_pet(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "pets").mkdir()

    assert overlay.resolve_pet_sprite() is None


def test_start_reasserts_the_chip_with_the_sprite(tmp_path: Path, monkeypatch) -> None:
    """A companion first spawned petless (capture) must get the sprite on re-arm."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_pet(tmp_path, "robotocat")
    _write_config(tmp_path, "display:\n  pet:\n    slug: robotocat\n    scale: 0.5\n")

    proc = _FakeProc()
    controller = overlay.Overlay()
    controller.proc = proc
    controller._claim_chip_enabled = True

    ok, _detail = controller.start(show_pet=False)

    assert ok is True
    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    assert [json.loads(line) for line in lines] == [{
        "op": "claim_chip",
        "enabled": True,
        "sprite_w": 96,
        "sprite_h": 104,
        "pad_bottom": 24,
    }]


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


def test_claim_chip_anchors_the_chip_to_the_sprite_not_the_window() -> None:
    """The pop-out window is 240x300 around a ~63x69 sprite: anchor the sprite.

    The companion is spawned petless, so the op must carry the sprite size and
    the sprite's 24 pt bottom padding; without them the chip is pinned to the
    empty window frame (the live defect: chip at 1081,365 while the pet is not
    near that point).
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    decoder = _swift_function_body(source, 'case "claim_chip":', 'case "launcher":')
    assert '"sprite_w"' in decoder
    assert '"sprite_h"' in decoder
    assert '"pad_bottom"' in decoder
    # Malformed geometry degrades to the window anchor, never drops the op.
    assert "logMalformedOnce" in decoder
    assert "return .claimChip(" in decoder
    assert "static let defaultPadBottom: CGFloat = 24" in source

    anchor = _swift_function_body(source, "private func claimChipAnchor(",
                                  "private func claimChipSize(")
    assert "window.minX + (window.width - spriteSize.width) / 2.0" in anchor
    assert "window.maxY - padBottom - spriteSize.height" in anchor
    assert "spriteSize.width > 0.0, spriteSize.height > 0.0" in anchor
    assert "return window" in anchor, "no sprite size must keep the window frame"

    # Half a pair is unusable: one dimension alone must not centre the anchor.
    setter = _swift_function_body(
        source,
        "private func setClaimChip(_ spec: ClaimChipSpec)",
        "/// Find the core Hermes pet pop-out.",
    )
    assert "if let width = spec.spriteW, let height = spec.spriteH" in setter
    assert "claimChipSpriteSize = nil" in setter

    position = _swift_function_body(source, "private func positionClaimChip(",
                                    "private func hideClaimChip(")
    assert "let petWindow = hermesPetWindowFrame(" in position
    assert "claimChipAnchor(window: petWindow" in position


def test_claim_chip_is_a_hat_icon_at_the_pets_top_right() -> None:
    """The chip is a 26x24 hat badge in the sprite's upper-right quadrant.

    Text made it a wide pill beside the pet; the pinned candidate order puts the
    upper-right placement first. It sits BESIDE the head (``pet.minY + gap``)
    rather than above it, because the strip above the sprite is where the pet's
    own speech bubble renders and the chip used to cover those lines.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    chip_view = _swift_function_body(source,
                                     "private final class ClaimChipView: NSView {",
                                     "// MARK: - Pet sprite sheet")
    assert "\U0001f3a9" in chip_view, "the chip draws the hat emoji"
    # The glyph is the whole label; the wording survives only as the AX label.
    without_label = chip_view.split('setAccessibilityLabel("Ask butler")')
    assert len(without_label) == 2, "the AX label keeps the wording exactly once"
    assert "Ask butler" not in "".join(without_label)
    assert "setAccessibilityElement(true)" in chip_view
    assert "setAccessibilityRole(.button)" in chip_view
    assert "NSFont.systemFont(ofSize: 13.0)" in chip_view

    size_body = _swift_function_body(source, "private func claimChipSize(",
                                     "private func claimChipFrame(")
    assert "CGSize(width: 26.0, height: 24.0)" in size_body
    assert "Ask butler" not in size_body
    assert "max(84.0" not in size_body

    frame_body = _swift_function_body(source, "private func claimChipFrame(",
                                      "private func positionClaimChip(")
    upper_right = frame_body.find("CGRect(x: pet.maxX + gap, y: pet.minY + gap")
    lower_right = frame_body.find("y: pet.midY - size.height / 2.0")
    below = frame_body.find("pet.maxY + gap")
    assert upper_right != -1 and lower_right != -1 and below != -1
    assert upper_right < lower_right, "the head-height placement comes first"
    assert upper_right < below, "upper-right must precede the below placement"
    # The chip must never be placed back over the bubble strip above the sprite.
    assert "pet.minY - gap - size.height" not in frame_body
    # Keep the clamping + no-overlap guard that makes fallbacks skip.
    assert "!frame.intersects(pet)" in frame_body
    assert "return nil" in frame_body


def test_claim_chip_window_can_become_key_so_clicks_land() -> None:
    """A borderless NSWindow is not key, so its view never sees mouseDown."""
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    window_body = _swift_function_body(source,
                                       "private final class ClaimChipWindow: NSWindow {",
                                       "private final class ClaimChipView: NSView {")
    assert "override var canBecomeKey: Bool" in window_body
    assert "return true" in window_body
    assert "override var canBecomeMain: Bool" in window_body
    assert "return false" in window_body

    chip_view = _swift_function_body(source,
                                     "private final class ClaimChipView: NSView {",
                                     "// MARK: - Pet sprite sheet")
    assert "override var acceptsFirstResponder: Bool" in chip_view
    assert "return true" in chip_view

    position = _swift_function_body(source, "private func positionClaimChip(",
                                    "private func hideClaimChip(")
    assert "ClaimChipWindow(contentRect: appKitFrame" in position
    assert "NSWindow(contentRect: appKitFrame" not in position
    assert "styleMask: [.borderless]" in position
    assert "ignoresMouseEvents = false" in position


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
