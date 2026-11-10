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


def test_summon_text_event_reaches_the_engine_callback() -> None:
    controller = overlay.Overlay()
    seen = []
    controller.on_event = seen.append

    controller._handle_message({"op": "summon_text", "text": "hi", "id": "abc"})

    assert seen == [{"op": "summon_text", "text": "hi", "id": "abc"}]


def test_look_text_event_reaches_the_engine_callback() -> None:
    """⌘⇧L must take the same event door as the ask field, target included."""
    controller = overlay.Overlay()
    seen = []
    controller.on_event = seen.append
    message = {
        "op": "look_text",
        "text": "what is this?",
        "id": "look1",
        "destination": "recent",
        "target": {"app": "Preview", "role": "Button", "label": "Export"},
    }

    controller._handle_message(message)

    assert seen == [message]


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


def test_watch_event_stream_never_starves_the_start_ack() -> None:
    """Watch lines are unsolicited: only the ack may reach the request inbox.

    If focus/value events queued behind an ack, every `watch_start` would read
    the wrong line (or time out). The reader must hand events to on_event and
    leave the inbox for the one-shot reply.
    """
    controller = overlay.Overlay()
    seen = []
    controller.on_event = seen.append

    controller._handle_message({"op": "watch-event", "seq": 1})
    controller._handle_message({"op": "watch-hit", "label": "done"})
    controller._handle_message({"op": "watch-stopped", "events": 2})
    controller._handle_message({"op": "watch-started", "app": "Slack"})

    assert [message["op"] for message in seen] == [
        "watch-event", "watch-hit", "watch-stopped"]
    ack = controller._inbox.get_nowait()
    assert ack["op"] == "watch-started", "the ack is the only inbox row"
    assert controller._inbox.empty()


def test_watch_stop_is_fire_and_forget() -> None:
    """The stop reply rides on_event, so the op must not wait on request()."""
    proc = _FakeProc()
    controller = overlay.Overlay()
    controller.proc = proc

    controller.send({"op": "watch_stop"})

    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    assert [json.loads(line) for line in lines] == [{"op": "watch_stop"}]


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
    # Clickability affordance: pointing-hand cursor + a hover highlight, which
    # must keep working while a background process owns the window.
    assert "NSCursor.pointingHand.set()" in chip_view
    assert "addCursorRect(bounds, cursor: .pointingHand)" in chip_view
    assert ".activeAlways" in chip_view, "hover must not depend on app activation"
    assert "cursorUpdate(with" in chip_view

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


def test_claim_chip_is_driven_by_hermes_ax_notifications() -> None:
    """The chip follows the pop-out from AX move events, not a 0.5 s poll.

    The probe logged 86 ``AXWindowMoved`` events in one drag (~17 ms apart),
    each carrying the live ``AXPosition``/``AXSize``, so the observer must
    exist on both the app element (window lifecycle) and every window element
    (move/resize/destroyed), rooted on the main run loop.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "AXObserverCreate" in source
    assert "kAXWindowMovedNotification" in source
    assert "kAXWindowResizedNotification" in source
    assert "kAXWindowCreatedNotification" in source
    assert "kAXFocusedWindowChangedNotification" in source
    assert "kAXMainWindowChangedNotification" in source
    assert "kAXUIElementDestroyedNotification" in source
    assert "AXObserverGetRunLoopSource" in source
    assert "CFRunLoopAddSource" in source

    start = _swift_function_body(source, "private func startPetObservation()",
                                 "private func attachPetWindowObservers()")
    assert "guard let pid = hermesPid()" in start
    assert "observerUnavailable = true" in start
    assert "AXObserverCreate(pid" in start
    assert "DispatchQueue.main.async" in source

    # Re-attach on window lifecycle events, and tear down with the chip.
    assert ("private var observedPetWindows = Set<Int>()" in source)
    assert ("private var observerUnavailable = false" in source)
    assert ("private var petObserver: AXObserver?" in source)

    attach = _swift_function_body(source, "private func attachPetWindowObservers()",
                                  "private func stopPetObservation()")
    # The refcon is how the C trampoline finds the controller back.
    assert "Unmanaged.passUnretained(self).toOpaque()" in attach
    assert ("Unmanaged<OverlayController>\n        .fromOpaque(refcon)"
            in source)
    assert "AXUIElementCreateApplication(pid)" in attach
    assert "Int(CFHash(window))" in attach
    assert "observedPetWindows.insert(key)" in attach
    assert "kAXWindowCreatedNotification" in attach
    assert "kAXWindowMovedNotification" in attach
    assert "kAXWindowResizedNotification" in attach

    hide = _swift_function_body(source, "private func hideClaimChip()",
                                "private func claimChipClicked()")
    # Hiding must NOT rebuild the observer: the slow tick hides the chip
    # whenever the pop-out is closed or on another Space, and tearing the
    # observer down there would recreate it every few seconds.
    assert "stopPetObservation()" not in hide
    assert "startPetObservation()" not in hide

    stop = _swift_function_body(source, "private func stopPetObservation()",
                                "private func updateClaimChipTimer()")
    assert "CFRunLoopRemoveSource" in stop
    assert "petObserver = nil" in stop
    assert "observedPetWindows.removeAll()" in stop
    assert "observerUnavailable = false" in stop

    disarm = _swift_function_body(
        source,
        "private func setClaimChip(_ spec: ClaimChipSpec)",
        "// MARK: Pet AX observation",
    )
    assert "stopPetObservation()" in disarm
    assert "startPetObservation()" in disarm

    shutdown = _swift_function_body(source, "func shutdown() {",
                                    "// MARK: - Application delegate")
    assert "stopPetObservation()" in shutdown


def test_claim_chip_repositions_from_the_notified_element() -> None:
    """The move/resize hot path reads the frame off the event's element.

    A 60 Hz drag must not re-run ``CGWindowListCopyWindowInfo`` (0.73 ms) or
    re-walk ``AXWindows`` (0.16 ms); only the two attributes on the element the
    notification named.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    hot = _swift_function_body(source, "private func processPetAXNotification(",
                               "private func scheduleEventDrivenReposition(")
    assert "axWindowFrame(of: element)" in hot
    assert "kAXWindowMovedNotification" in hot
    assert "kAXWindowResizedNotification" in hot
    assert "isPetShapedFrame(frame)" in hot
    assert "attachPetWindowObservers()" in hot
    assert "positionClaimChip()" in hot
    # The hot path never re-enumerates windows.
    assert "cgWindowFrameList" not in hot
    assert "hermesPetWindowFrame" not in hot
    assert "axWindowFrameList" not in hot

    reader = _swift_function_body(source, "private func axWindowFrame(of element:",
                                  "private func isPetShapedFrame(")
    assert "AXPosition" in reader
    assert "AXSize" in reader
    assert "cgWindowFrameList" not in reader

    # Main-window resizes must be dropped by the same 60...480 / 0.6...1.67
    # pet-shape filter the polled anchor uses.
    shape = _swift_function_body(source, "private func isPetShapedFrame(",
                                 "private func hermesPid()")
    assert "bounds.width >= 60.0" in shape
    assert "bounds.width <= 480.0" in shape
    assert "aspect >= 0.6 && aspect <= 1.67" in shape


def test_claim_chip_throttles_events_and_reconciles_slowly() -> None:
    """40 ms coalescing on the hot path; 3.0 s tick with a live observer.

    Without an observer the legacy 0.5 s poll must stay in charge, so the chip
    can never be left unpositioned.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    coalescer = _swift_function_body(source,
                                     "private func scheduleEventDrivenReposition(",
                                     "/// Find the core Hermes pet pop-out.")
    assert "0.040" in coalescer, "the 40 ms coalescing interval"
    assert "asyncAfter" in coalescer, "a trailing update still runs"
    assert "pendingRepositionWork" in coalescer
    assert "pendingPetFrame" in coalescer

    timer = _swift_function_body(source, "private func updateClaimChipTimer()",
                                 "/// Entry point from the C callback.")
    assert "petObserver != nil && !observerUnavailable" in timer
    assert "interval = 3.0" in timer
    assert "interval = 0.5" in timer
    assert "claimChipTimerInterval == interval" in timer
    # Both cadences are conditional on a live observer, never a bare 0.5 s.
    assert "0.5, repeats: true" not in source, (
        "the unconditional 0.5 s poll must be gone")


def test_claim_chip_window_still_short_circuits_on_an_unchanged_frame() -> None:
    """The event-driven path keeps the no-op guard the poll had: no re-set."""
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    apply_body = _swift_function_body(source, "private func applyClaimChipFrame(",
                                      "private func hideClaimChip(")
    assert "if window.frame != appKitFrame" in apply_body
    assert "window.setFrame(appKitFrame, display: true)" in apply_body

    position = _swift_function_body(source, "private func positionClaimChip(",
                                    "private func applyClaimChipFrame(")
    assert "let petWindow = hermesPetWindowFrame(" in position
    assert "applyClaimChipFrame(petWindow: petWindow" in position


def test_summon_works_outside_hermes_without_raising_the_window() -> None:
    """⌘⇧A and the chip must work when Hermes is behind another app.

    The desktop keybind only fires while Hermes is frontmost, so the companion
    owns a Carbon global hot key. It opens the local ask field and takes key
    focus itself; the Hermes window is never activated.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "import Carbon.HIToolbox" in source
    assert "RegisterEventHotKey" in source
    assert "kEventHotKeyPressed" in source
    assert "kVK_ANSI_A" in source
    assert "UnregisterEventHotKey" in source
    assert "installSummonHotKey()" in source
    assert "removeSummonHotKey()" in source
    # The 034e922 activation door is gone for good.
    assert "activateHermes()" not in source

    hot = _swift_function_body(source, "private func summonHotKeyFired()",
                               "// MARK: Pet double")
    # A Carbon hot key swallows the chord system-wide, so stepping aside when
    # Hermes is frontmost made Cmd+Shift+A DEAD in-app. It always opens the
    # local field; a second press closes it.
    assert "isActive" not in hot
    assert "summonFieldOpen" in hot
    assert "openSummonField()" in hot
    assert "closeSummonField(journal: \"ask-cancel\")" in hot
    assert "activate" not in hot

    click = _swift_function_body(source, "private func claimChipClicked()",
                                 "// MARK: In-place ask field")
    assert "toggleSummonField()" in click
    assert "emitSummon" not in click


def test_look_is_a_pointer_drag_not_a_hot_key() -> None:
    """LOOK is press-and-drag the hat, never a chord.

    A press under the threshold still opens the ask field; movement past it
    turns the same press into a pointer read-out, so the key path is gone end
    to end.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    # The chord, its Carbon id and its journal lines are gone for good.
    assert "kVK_ANSI_L" not in source
    assert "lookHotKeyFired" not in source
    assert "installLookHotKey" not in source
    assert "look-hotkey-installed" not in source
    assert "look-hotkey-failed" not in source
    assert "EventHotKeyID(signature: OSType(0x42544C52), id: 2)" not in source

    chip_view = _swift_function_body(source,
                                     "private final class ClaimChipView: NSView {",
                                     "// MARK: - Pointer LOOK card")
    assert "static let dragThreshold: CGFloat = 4.0" in chip_view
    assert "👀" in chip_view, "the drag state draws the eye glyph"

    down = _swift_function_body(
        chip_view, "override func mouseDown(with event: NSEvent) {",
        "override func mouseDragged(with event: NSEvent) {")
    dragged = _swift_function_body(
        chip_view, "override func mouseDragged(with event: NSEvent) {",
        "override func mouseUp(with event: NSEvent) {")
    up = _swift_function_body(
        chip_view, "override func mouseUp(with event: NSEvent) {",
        "override func draw(")

    # A plain press latches placement off but does not open anything yet.
    assert "pressStart" in down
    assert "onPressBegan?(point)" in down
    assert "onClick?()" not in down
    # Movement past the 4 pt threshold promotes the press to a drag.
    assert "movement > ClaimChipView.dragThreshold" in dragged
    assert "onDragBegan?(start)" in dragged
    assert "onDragMoved?(point)" in dragged
    # Release without movement is the click that still opens the ask field.
    assert "onDragEnded?(point)" in up
    assert "onClick?()" in up
    # A lost mouseUp must not pin placement forever.
    assert "recoverLostPressIfNeeded()" in source
    assert "NSEvent.pressedMouseButtons == 0" in source
    assert "func cancelPendingPress()" in chip_view


def test_look_card_is_a_non_activating_pointer_readout() -> None:
    """The drop shows a 320 pt card that never takes focus and never emits."""
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    window = _swift_function_body(
        source,
        "private final class LookCardWindow: NSPanel {",
        "private final class LookCardView: NSView {")
    assert "override var canBecomeKey: Bool" in window
    assert "return false" in window
    assert "override var canBecomeMain: Bool" in window
    assert "autoDismissInterval: TimeInterval = 6.0" in window

    card = _swift_function_body(
        source,
        "private final class LookCardView: NSView {",
        "// MARK: - In-place ask field")
    assert "static let cardWidth: CGFloat = 320.0" in card
    assert "static let maxLines = 5" in card
    assert ".byTruncatingTail" in card
    assert "maximumNumberOfLines = 1" in card
    # No buttons, no footer, no chat link: nothing to click or type.
    assert "NSButton" not in window + card

    # Dismissal paths: click monitors, Esc (keyCode 53), the 6 s timer, hat.
    controller = _swift_function_body(
        source, "private func presentLookCard(",
        "// MARK: In-place ask field")
    assert "NSEvent.addLocalMonitorForEvents" in controller
    assert "NSEvent.addGlobalMonitorForEvents" in controller
    assert "keyCode == escape" in controller
    assert "let escape: UInt16 = 53" in controller
    assert "dismissLookCard()" in controller
    assert "RunLoop.main.add(timer, forMode: .common)" in controller
    assert "dismissLookCard()" in _swift_function_body(
        source, "private func claimChipClicked() {",
        "// MARK: Pointer LOOK gesture")

    end = _swift_function_body(
        source, "private func endLookDrag(at screenPoint: NSPoint) {",
        "private func lookCardFrame(")
    present = _swift_function_body(
        source, "private func presentLookCard(",
        "private func installLookCardMonitors()")
    for body in (end, present, window, card):
        assert "summon_text" not in body
        assert "look_text" not in body
        assert "openSummonField()" not in body
    assert "lookElementDescriptor(at: point)" in end

    # Anchored away from the pointer so the card never covers it.
    frame = _swift_function_body(
        source, "private func lookCardFrame(at point: CGPoint,",
        "private func presentLookCard(")
    assert "point.x + gap" in frame
    assert "point.y + gap" in frame
    assert "point.x - gap - size.width" in frame
    assert "point.y - gap - size.height" in frame


def test_look_resolution_walks_children_then_parents_and_falls_back_honestly() -> None:
    """Chromium/Electron returns a bare AXGroup: descend, then ascend, then say
    'Window · <app>' instead of printing Group alone.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "private func lookElementDescriptor(at point: CGPoint) -> LookDescriptor" in source
    helper = _swift_function_body(
        source,
        "private func lookElementDescriptor(at point: CGPoint) -> LookDescriptor {",
        "/// Hit-test the cursor and compose the LOOK context")
    assert "axElementUnderCursor(point)" in helper
    # The hit-test must use the drop point, not the live cursor.
    hit = _swift_function_body(
        source, "private func axElementUnderCursor(", "private func axRoleName(")
    assert "Float(point.x)" in hit
    assert "Float(point.y)" in hit
    assert "cursorQuartzPoint()" not in hit
    assert '"AXChildren"' in helper
    assert '"AXParent"' in helper
    assert "labelledDescendant(hit, levels: 2)" in helper
    assert "labelledAncestor(hit, levels: 4)" in helper
    # The measured defect: an unnamed container resolves to its window/app.
    assert "isWindowFallback: true" in helper
    assert '"Window"' in helper

    descriptor = _swift_function_body(
        source, "private struct LookDescriptor {",
        "private let axContainerRoles")
    # Hard cap, state line and the string-only value line.
    assert "prefix(5)" in descriptor
    assert '"disabled"' in descriptor
    assert 'Value: ' in descriptor
    assert "isWindowFallback" in descriptor

    # The retained typed path now resolves through the same helper.
    target = _swift_function_body(
        source, "private func axLookTargetUnderCursor()",
        "private func walkAX(")
    assert "lookElementDescriptor(at: point)" in target


def test_summon_field_is_a_local_in_place_text_entry() -> None:
    """Clicking the hat / pressing ⌘⇧A types next to the pet, never in Hermes."""
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "private final class SummonFieldWindow: NSWindow" in source
    assert "private final class SummonFieldView: NSView" in source
    assert "NSTextField" in source
    assert "Ask the butler…" in source
    assert "NSFont.systemFont(ofSize: 13.0)" in source
    assert "JSONSerialization.data(withJSONObject: payload" in source
    assert '"op": "summon_text"' in source
    assert "windowDidResignKey" in source
    assert "insertNewline" in source, "Enter/Return submits"
    assert "insertNewlineIgnoringFieldEditor" in source
    assert "cancelOperation" in source, "Escape cancels"
    assert "trimmingCharacters(in: .whitespacesAndNewlines)" in source
    assert "NSWorkspace.shared.frontmostApplication" in source
    # Journal the new hop end to end.
    assert '"ask-open"' in source
    assert '"ask-submit:' in source
    assert '"ask-cancel"' in source

    window = _swift_function_body(
        source,
        "private final class SummonFieldWindow: NSWindow",
        "private final class SummonFieldView")
    assert "override var canBecomeKey: Bool" in window
    assert "return true" in window
    assert "override var canBecomeMain: Bool" in window
    assert "return false" in window

    view = _swift_function_body(source,
                                "private final class SummonFieldView: NSView",
                                "// MARK: - Pet sprite sheet")
    assert "static let fieldWidth: CGFloat = 280.0" in view
    assert "static let fieldHeight: CGFloat = 34.0" in view
    assert "isBordered = false" in view
    assert "placeholderString = \"Ask the butler…\"" in view

    frame = _swift_function_body(source, "private func summonFieldFrame(",
                                 "private func openSummonField()")
    # Centred on the chip, or under the sprite without one; 8 pt gap, clamped.
    assert "anchor.midX - size.width / 2.0" in frame
    assert "anchor.maxY + gap" in frame
    assert "anchor.minY - gap - size.height" in frame
    assert "visible.minX + 4.0" in frame
    assert "visible.maxX - frame.width - 4.0" in frame
    assert "summonFieldGap" in frame

    opened = _swift_function_body(source, "private func openSummonField()",
                                  "private func closeSummonField(")
    assert ".borderless" in opened
    assert ".statusBar" in opened
    assert "canJoinAllSpaces" in opened
    assert "isMovable = false" in opened
    assert "NSApp.activate(ignoringOtherApps: true)" in opened
    assert "window.makeKey()" in opened
    assert "makeFirstResponder(view.field)" in opened

    submit = _swift_function_body(source, "private func submitSummonText(",
                                  "private func cancelSummonField(")
    assert "JSONSerialization.data(withJSONObject: payload" in submit
    assert '"summon_text"' in submit
    assert "closeSummonField(journal: nil)" in submit
    assert '"ask-submit:' in submit
    # Empty text closes without emitting.
    assert "closeSummonField(journal: \"ask-cancel\")" in submit
    assert "FileHandle.standardOutput.write" in submit


def test_summon_field_carries_the_destination_per_send() -> None:
    """↵ continues the newest chat, ⌘↵ starts a new one — chosen in the field."""
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    # One per-send enum, two values, no persistence.
    assert "private enum SummonDestination: String" in source
    destination = _swift_function_body(
        source,
        "private enum SummonDestination: String",
        "/// The typing box that appears beside the pet.")
    assert "case recent" in destination
    assert "case new" in destination

    view = _swift_function_body(source,
                                "private final class SummonFieldView: NSView",
                                "// MARK: - Pet sprite sheet")
    # The hint is an inline label inside the same 280x34 box, discoverable
    # without docs: muted, right-aligned, never editable or focusable.
    assert "⌘↵ new" in view
    assert 'NSTextField(labelWithString: "⌘↵ new")' in view
    assert "hint.alignment = .right" in view
    assert "NSFont.systemFont(ofSize: 10.5)" in view
    assert "withAlphaComponent(0.55)" in view
    assert "hint.isEditable = false" in view
    assert "hint.isSelectable = false" in view
    assert "hint.refusesFirstResponder = true" in view
    # The input keeps its placeholder and yields width to the hint.
    assert 'placeholderString = "Ask the butler…"' in view
    assert "inputWidth" in view
    assert "static let fieldWidth: CGFloat = 280.0" in view
    assert "static let fieldHeight: CGFloat = 34.0" in view
    # Return inspects the command modifier on the event ...
    assert "NSApp.currentEvent?.modifierFlags.contains(.command)" in view
    assert "insertNewline" in view
    assert "insertNewlineIgnoringFieldEditor" in view
    assert "emitSubmit(destination: commandHeld ? .new : .recent)" in view
    # ... and a single press still emits once (key equivalent + selector).
    assert "submitInFlight" in view
    assert "resetSubmitGuard()" in view

    window = _swift_function_body(
        source,
        "private final class SummonFieldWindow: NSWindow",
        "private enum SummonDestination")
    # The key-equivalent path: ⌘Return may reach the window before the field.
    assert "override func performKeyEquivalent(with event: NSEvent) -> Bool" in window
    assert "event.modifierFlags.contains(.command)" in window
    assert "event.keyCode == 36" in window
    assert "onCommandReturn?()" in window

    opened = _swift_function_body(source, "private func openSummonField()",
                                  "private func closeSummonField(")
    assert "window.onCommandReturn" in opened
    assert "emitSubmit(destination: .new)" in opened
    assert "submitSummonText(text, destination: destination)" in opened

    submit = _swift_function_body(source, "private func submitSummonText(",
                                  "private func cancelSummonField(")
    # The payload names the destination and the journal records it.
    assert '"destination": destination.rawValue' in submit
    assert "destination: SummonDestination" in submit
    assert "ask-submit:\\(text.count)chars:\\(destination.rawValue)" in submit


def test_look_reads_under_the_hat_and_click_then_asks_about_it() -> None:
    """The drop must read the ELEMENT under the pointer, not our own chip.

    Measured failure: at mouseUp the hat is the topmost window at the cursor, so
    an immediate hit-test answered "Button · Ask butler — ButlerOverlay" wherever
    the user dropped it. The window is parked off-screen and the hit-test runs on
    the next run-loop turn; a click within the ask window then asks about THAT
    element instead of opening a blank request.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    drop = _swift_function_body(source, "private func endLookDrag(at screenPoint: NSPoint)",
                                "/// Place the card next to the drop point")
    assert "-20000.0" in drop, "the hat must be parked off-screen before the hit-test"
    assert "DispatchQueue.main.async" in drop, "the hit-test must run after the window server catches up"
    assert "lastLookDescriptor = descriptor" in drop

    click = _swift_function_body(source, "private func claimChipClicked()",
                                 "// MARK: Pointer LOOK gesture")
    assert "lastLookDescriptor" in click
    assert "pendingLookTarget" in click
    assert 'chipJournal("ask-about-look")' in click
    assert "toggleSummonField()" in click, "a plain click with no recent look still opens the ask field"


def test_look_never_answers_with_our_own_window() -> None:
    """Both symptoms so far were self-hits: "Button · Ask butler" then "Window ·
    ButlerOverlay". The drop must retry with the hat parked and then fall back to
    the active (non-self) app's window.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "private func lookDescriptorIsSelf(" in source
    assert "private func lookDescriptorForActiveApp(" in source
    assert "menuBarOwningApplication" in source, "the active app is the menu-bar owner, never an accessory"

    resolve = _swift_function_body(source, "private func resolveLookDrop(",
                                   "/// Place the card next to the drop point")
    assert "lookDescriptorIsSelf(descriptor)" in resolve
    assert "retries - 1" in resolve, "one short retry while the parked window settles"
    assert "lookDescriptorForActiveApp()" in resolve
    assert 'chipJournal("look-self-hit-fallback")' in resolve

    drop = _swift_function_body(source, "private func endLookDrag(at screenPoint: NSPoint)",
                                "/// Read the drop point")
    assert "resolveLookDrop(" in drop


def test_watch_observes_the_app_windows_and_focused_control() -> None:
    """WATCH is an AXObserver over the watched app, never a screen capture.

    The brief names the exact notification set. ``kAXWindowTitleChangedNotification``
    is not exposed by this SDK's AXNotificationConstants.h, so windows carry
    ``kAXTitleChangedNotification`` — the notification apps actually post when a
    window title changes.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    # Wire contract: two new stdin ops, both parsed, both dispatched.
    assert 'case "watch_start"' in source
    assert 'case "watch_stop"' in source
    assert "return .watchStart(" in source
    assert "return .watchStop" in source
    assert "case .watchStart(let spec):" in source
    assert "startWatch(spec)" in source
    assert 'stopWatch(reason: "stopped", emit: true)' in source

    install = _swift_function_body(source, "private func startWatch(",
                                   "private func resetWatchState()")
    assert "AXObserverCreate(pid, watchAXObserverCallback" in install
    assert "AXIsProcessTrusted()" in install
    assert 'watchFailure("app-not-running", token: spec.token)' in install
    assert 'watchFailure("app-not-trusted", token: spec.token)' in install
    assert '"op": "watch-started"' in install
    assert "findWatchApp(named: spec.app)" in install
    # Main run loop, same as the claim chip's observer.
    assert "CFRunLoopAddSource" in install
    # The NEW observer is validated before the RUNNING watch is torn down: a
    # rejected start must not kill a watch that is still working.
    teardown_at = install.find('stopWatch(reason: "replaced", emit: false)')
    assert teardown_at != -1
    assert teardown_at > install.find("guard registered > 0 else")
    assert "watchRootElement = root" in install

    # App element notifications (lifecycle).
    for notification in (
        "kAXFocusedUIElementChangedNotification",
        "kAXWindowCreatedNotification",
        "kAXWindowMiniaturizedNotification",
        "kAXWindowDeminiaturizedNotification",
        "kAXApplicationActivatedNotification",
        "kAXApplicationDeactivatedNotification",
        "kAXUIElementDestroyedNotification",
    ):
        assert notification in install, f"app observer missing {notification}"

    # Per-window notifications: title, destroyed, moved (moved is installed but
    # dropped in the handler).
    windows = _swift_function_body(source,
                                   "private func attachWatchWindowObservers()",
                                   "private func attachWatchFocusedObservers()")
    assert "kAXTitleChangedNotification" in windows
    assert "kAXUIElementDestroyedNotification" in windows
    assert "kAXWindowMovedNotification" in windows
    assert "axWindowElements(of: root)" in windows
    assert "observedWatchWindows[key] = window" in windows

    # Focused control: value / title / selection, re-installed on focus change.
    focused = _swift_function_body(source,
                                   "private func attachWatchFocusedObservers()",
                                   "fileprivate func watchAXNotificationFired")
    assert "AXFocusedUIElement" in focused
    for notification in ("kAXValueChangedNotification",
                         "kAXTitleChangedNotification",
                         "kAXSelectedChildrenChangedNotification"):
        assert notification in focused
    assert "AXObserverRemoveNotification(observer, previous" in focused
    process = _swift_function_body(source,
                                   "private func processWatchAXNotification(",
                                   "private func watchRole(")
    assert "case kAXFocusedUIElementChangedNotification:" in process
    assert "attachWatchFocusedObservers()" in process
    assert "if name == kAXWindowMovedNotification" in process, (
        "the move hot path must be dropped before any AX read")

    # Every kind in the contract is produced.
    for kind in ("focus", "window-new", "window-gone", "window-title", "value",
                 "activated", "deactivated"):
        assert '"%s"' % kind in process or 'kind: "%s"' % kind in process


def test_watch_coalesces_events_and_announces_a_hit_once() -> None:
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "private let watchMaxEventsPerSecond = 4" in source
    assert "private let watchDuplicateWindow: TimeInterval = 0.15" in source
    assert "private let watchRateWindow: TimeInterval = 1.0" in source

    emit = _swift_function_body(source, "private func emitWatchEvent(",
                                "/// Find the core Hermes pet pop-out.")
    # The condition check runs BEFORE the rate/duplicate gates.
    assert emit.find('"op": "watch-hit"') < emit.find("watchEmitTimes.removeAll")
    assert "if watchHitArmed, let until = watchUntil" in emit
    assert "watchHitArmed = false" in emit
    assert "labelText.contains(needle) || valueText.contains(needle)" in emit
    assert "watchEmitTimes.count < watchMaxEventsPerSecond" in emit
    assert "now.timeIntervalSince(last) < watchDuplicateWindow" in emit
    # old/new only when both sides are readable strings (empty counts as a
    # readable value: clearing a field is a change).
    assert "if let old = old, let new = new {" in emit
    assert "private func axWatchRawValueText(" in source

    # Labels are title -> description -> value, trimmed to 120.
    label = _swift_function_body(source, "private func watchLabel(",
                                 "private func axWatchValueText(")
    assert "AXTitle" in label and "AXDescription" in label
    assert "axWatchValueText(element)" in label
    assert "private let watchLabelLimit = 120" in source

    # "stop matching until re-armed": only a new watch_start re-arms.
    start = _swift_function_body(source, "private func startWatch(",
                                 "private func resetWatchState()")
    assert "watchHitArmed = spec.until != nil" in start


def test_watch_stop_removes_every_observer_and_the_run_loop_source() -> None:
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    stop = _swift_function_body(source,
                                "private func stopWatch(reason: String, emit: Bool)",
                                "/// A watch must not outlive the app it observes.")
    assert "AXObserverRemoveNotification(observer, root" in stop
    assert "watchRootElement" in stop, "removal uses the element registered on"
    assert "AXObserverRemoveNotification(observer, focused" in stop
    assert "AXObserverRemoveNotification(observer, window" in stop
    assert "CFRunLoopRemoveSource" in stop
    assert "watchObserver = nil" in stop
    assert "resetWatchState()" in stop
    reset = _swift_function_body(source, "private func resetWatchState()",
                                 "private func stopWatch(")
    assert "watchLivenessTimer?.invalidate()" in reset, (
        "the liveness timer is a resource too")
    assert '"op": "watch-stopped"' in stop
    assert '"events": events' in stop

    # App quit is a clean stop with a reason, from either path.
    process = _swift_function_body(source,
                                   "private func processWatchAXNotification(",
                                   "private func watchRole(")
    assert 'stopWatch(reason: "app-quit", emit: true)' in process
    assert "CFEqual(element, root)" in process, (
        "app-quit identity uses the stored root wrapper, never a fresh one")
    liveness = _swift_function_body(source, "private func startWatchLivenessTimer()",
                                    "private func attachWatchWindowObservers()")
    assert "NSRunningApplication(processIdentifier: pid)" in liveness
    assert 'stopWatch(reason: "app-quit", emit: true)' in liveness

    # The idle watchdog and the farewell exit must not retire a live watch.
    watchdog = _swift_function_body(source, "private func watchdogFired()",
                                    "/// Invalidate any scheduled post-farewell exit.")
    assert "watchObserver == nil" in watchdog
    assert "watchObserver == nil" in _swift_function_body(
        source, "private func scheduleExitAfterFarewell()",
        "// MARK: Teardown")

    shutdown = _swift_function_body(source, "func shutdown() {",
                                    "// MARK: - Application delegate")
    assert 'stopWatch(reason: "shutdown", emit: false)' in shutdown
