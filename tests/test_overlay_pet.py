"""Tests for overlay pet resolution, rebuild freshness, and pet-op startup."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import types
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
    # The shipped URL template points at a live release: a test that means to
    # exercise the COMPILE path must refuse the download first.
    monkeypatch.setenv("BUTLER_NO_DOWNLOAD", "1")
    binary = tmp_path / "ButlerOverlay"
    source = tmp_path / "ButlerOverlay.swift"
    binary.write_bytes(b"old")
    binary.chmod(0o755)
    source.write_text("// new source", encoding="utf-8")
    os.utime(binary, (1_000, 1_000))
    os.utime(source, (2_000, 2_000))

    # Staleness is a source-hash question now: a sidecar recording a different
    # hash is what schedules the rebuild.
    buildinfo = tmp_path / ".buildinfo"
    buildinfo.write_text(json.dumps({
        "source_sha256": "0" * 64, "archs": ["arm64"], "built_at": 1,
    }), encoding="utf-8")

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        # build_if_needed compiles to a staged path and renames it over the
        # real binary only when swiftc succeeds.
        Path(args[3]).write_bytes(b"rebuilt")
        Path(args[3]).chmod(0o755)

    monkeypatch.setattr(overlay, "binary_path", lambda: str(binary))
    monkeypatch.setattr(overlay, "binary_source_path", lambda: str(source))
    monkeypatch.setattr(overlay, "buildinfo_path", lambda: str(buildinfo))
    monkeypatch.setattr(overlay, "shutil", types.SimpleNamespace(
        which=lambda name: "/usr/bin/swiftc"))
    monkeypatch.setattr(overlay.subprocess, "run", fake_run)
    monkeypatch.setattr(overlay, "_write_buildinfo_best_effort", lambda *a, **k: None)

    assert overlay.build_if_needed() == (True, "compiled")
    assert len(calls) == 1
    assert calls[0][0] == ["swiftc", "-O", "-o", str(binary) + ".new", str(source)]
    assert binary.read_bytes() == b"rebuilt"


def test_build_if_needed_skips_fresh_binary(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "ButlerOverlay"
    source = tmp_path / "ButlerOverlay.swift"
    binary.write_bytes(b"new")
    binary.chmod(0o755)
    source.write_text("// old source", encoding="utf-8")
    # The source mtime is NEWER than the binary's: that is exactly the fresh
    # checkout case, and the matching content hash must still win.
    os.utime(source, (1_000, 1_000))
    os.utime(binary, (2_000, 2_000))

    buildinfo = tmp_path / ".buildinfo"
    buildinfo.write_text(json.dumps({
        "source_sha256": overlay.source_sha256(str(source)),
        "archs": ["arm64", "x86_64"],
        "built_at": 1,
    }), encoding="utf-8")

    def fail_run(*args, **kwargs):
        raise AssertionError("matching source hash must not compile")

    monkeypatch.setattr(overlay, "binary_path", lambda: str(binary))
    monkeypatch.setattr(overlay, "binary_source_path", lambda: str(source))
    monkeypatch.setattr(overlay, "buildinfo_path", lambda: str(buildinfo))
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


def test_guide_end_sends_the_terminal_op_with_its_reason() -> None:
    proc = _FakeProc()
    controller = overlay.Overlay()
    controller.proc = proc

    controller.guide_end("abandoned")

    lines = [line for line in proc.stdin.data.decode("utf-8").splitlines() if line]
    assert [json.loads(line) for line in lines] == [
        {"op": "guide_end", "reason": "abandoned"},
    ]


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


def test_guide_end_fades_the_double_and_a_later_pet_op_rearms_it() -> None:
    """The terminal guide op must exist, fade, be idempotent, and re-arm.

    The measured ghost was the walker double still drawn after abandonment:
    "second ghost pet butler is still on my screen". The companion owns the
    fade, so these source facts pin the contract the engine's ``guide_end``
    op relies on.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    # 1. The op is parsed and dispatched (not an unknown-op rejection).
    assert 'case "guide_end"' in source
    assert "case guideEnd" in source
    assert "case .guideEnd(let reason):" in source

    # 2. It fades the double through the existing farewell path and journals
    #    the hop so a ghost traces to a missed end.
    end = _swift_function_body(source, "private func petGuideEnd(",
                               "private func petDone(")
    assert "petGuideEnded" in end
    assert "petDone()" in end
    assert 'chipJournal("guide-end:\\(reason)")' in end

    # 3. Idempotent: a second end returns before touching the double.
    assert "guard !isShuttingDown, !petGuideEnded else" in end

    # 4. A later guide start (the pet op) clears the flag and the farewell
    #    state, re-arming the walk exactly as before.
    show = _swift_function_body(source, "private func showPet(",
                                "private func createPetWindow(")
    assert "petGuideEnded = false" in show
    assert "petFarewell = false" in show
    assert "petFading = false" in show

    # 5. No other work means no lingering: the post-farewell exit and the
    #    idle watchdog both refuse to retire a companion that still owns a
    #    watch, look card or ask field.
    exit_body = _swift_function_body(source, "private func scheduleExitAfterFarewell(",
                                     "// MARK: Teardown")
    assert "lookCardWindow == nil" in exit_body
    assert "!summonFieldOpen" in exit_body
    watchdog = _swift_function_body(source, "private func watchdogFired(",
                                    "/// Invalidate any scheduled post-farewell exit.")
    assert "lookCardWindow == nil" in watchdog
    assert "!summonFieldOpen" in watchdog


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
    """The drop card never takes focus and never emits.

    Round 16 leaves it with no caller (the drag arms INSPECT MODE instead); the
    class and its helpers are kept, marked dead, only until the demo is over.
    """
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
        "/// Park the overlays, then resolve on the next run-loop turn")
    present = _swift_function_body(
        source, "private func presentLookCard(",
        "private func installLookCardMonitors()")
    for body in (end, present, window, card):
        assert "summon_text" not in body
        assert "look_text" not in body
        assert "openSummonField()" not in body
    # Round 16: the drag release goes through INSPECT MODE's resolver now; the
    # card is dead code kept only until the demo is over.
    assert "resolveInspectRelease(at: screenPoint)" in end
    assert "presentLookCard(" not in end
    assert "resolveLookDrop(" not in end

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
    assert "NSWindow.Level.screenSaver.rawValue + 1" in opened, (
        "the field must outrank INSPECT MODE overlays or keystrokes leak to the app below")
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


def test_look_drag_release_reads_the_element_under_the_hat() -> None:
    """The release point must be read from the app underneath, never from us.

    Round 16 folds the drag into INSPECT MODE: at release every overlay is
    parked off-screen, the AX read runs on the next run-loop turn, and the drag
    goes through `resolveInspectClick` — the same resolver the click-armed mode
    uses. A press under the drag threshold is still the plain ask door.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    release = _swift_function_body(
        source, "private func resolveInspectRelease(at screenPoint: NSPoint) {",
        "/// The release point when the chip's mouseUp is lost")
    assert "-20000.0" in release, "every overlay is parked off-screen before the hit-test"
    assert "DispatchQueue.main.async" in release, "the read runs after the window server catches up"
    assert "resolveInspectClick(at: point, retries: 1)" in release

    click = _swift_function_body(source, "private func claimChipClicked()",
                                 "// MARK: Pointer LOOK gesture")
    assert "toggleSummonField()" in click, "a plain click still opens the ask field"


def test_look_never_answers_with_our_own_window() -> None:
    """Both symptoms so far were self-hits: "Button · Ask butler" then "Window ·
    ButlerOverlay". The resolver retries with the overlays parked and then falls
    back to the active (non-self) app's window — for BOTH confirmations, since
    the drag and the right-click share it.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "private func lookDescriptorIsSelf(" in source
    assert "private func lookDescriptorForActiveApp(" in source
    assert "menuBarOwningApplication" in source, "the active app is the menu-bar owner, never an accessory"

    resolve = _swift_function_body(source, "private func resolveInspectClick(",
                                   "/// The hat's right-click")
    assert "lookDescriptorIsSelf(descriptor)" in resolve
    assert "retries - 1" in resolve, "one short retry while the parked overlay settles"
    assert "lookDescriptorForActiveApp()" in resolve
    assert 'chipJournal("inspect-self-hit-fallback")' in resolve
    assert 'cancelInspectMode(reason: "self-after-fallback")' in resolve

    # The old drag-only resolver is requiem code now: kept for the demo, never
    # called from the release path.
    drop = _swift_function_body(
        source, "private func endLookDrag(at screenPoint: NSPoint) {",
        "/// Park the overlays, then resolve on the next run-loop turn")
    assert "resolveLookDrop(" not in drop
    assert "resolveInspectRelease(at: screenPoint)" in drop


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


def test_watch_emits_a_baseline_event_on_start() -> None:
    """A watch opened on a condition that is already true must fire at once.

    Measured: start_watch("Finder","Finder") sat at "0 events · no hit" until
    something in the app changed. The start now emits one focus event describing
    the app's current focused element, so the hit fires immediately and the
    status text has a first line to show.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    start = _swift_function_body(source, "private func startWatch(_ spec: WatchStartSpec)",
                                 "/// Clear every in-memory watch field")
    assert "writeWatchLine(started)" in start
    baseline = start.split("writeWatchLine(started)", 1)[1]
    assert "AXFocusedUIElement" in baseline
    assert 'emitWatchEvent(kind: "focus"' in baseline


def test_watch_matches_on_the_window_title_too() -> None:
    """Measured miss: watch("Finder", until: "Downloads") reported "no hit" while
    every event carried window "Downloads" — the condition only looked at label
    and value. The window title is the most common thing a user names.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")
    emit = _swift_function_body(source, "private func emitWatchEvent(",
                                "/// Find the core Hermes pet pop-out")
    assert "windowText" in emit
    assert "windowText.contains(needle)" in emit


def test_hat_option_right_click_menu_keeps_the_look_and_watch_doors() -> None:
    """Option-right-clicking the hat opens the discoverability menu.

    The three watch rows are prefilled into the SAME ask field, so they ride the
    normal agent turn; the watch rows go disabled when no watch is active. A
    PLAIN right-click arms INSPECT MODE instead (tested below).
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    menu = _swift_function_body(source, "private func presentHatMenu(",
                                "/// The app a watch would attach to")
    assert "NSMenu()" in menu
    assert "NSMenuItem(title: title" in menu
    assert '"Look at this…"' in menu
    assert '"Watch \\(app)…"' in menu
    assert '"What did the watch see?"' in menu
    assert '"Acknowledge watch hit"' in menu
    assert '"Stop watching"' in menu
    # The acknowledgement row sits ABOVE Stop watching: acknowledging a hit is
    # not stopping the watch (round 21: a one-shot hit already stopped it).
    assert menu.index('"Acknowledge watch hit"') < menu.index('"Stop watching"')
    assert "unacknowledgedWatchHit()" in menu, "gated by the top-level hit block"
    assert "acknowledgeWatchHit()" in menu
    assert "menu.popUp(positioning: nil, at: point, in: view)" in menu
    assert "hatMenuTargets = targets" in menu, "menu action boxes outlive tracking"
    # The watch doors are prefilled, not a new endpoint.
    assert 'openSummonField(prefill: "watch \\(app) for ")' in menu
    assert 'openSummonField(prefill: "what did the watch see?")' in menu
    assert 'openSummonField(prefill: "stop watching")' in menu
    assert "summon_text" not in menu
    assert "look_text" not in menu
    # Acknowledgement is one stdout line, not an HTTP call or a new window.
    ack_action = _swift_function_body(source,
                                      "private func acknowledgeWatchHit()",
                                      "/// The `watch` row of the shared state")
    assert 'writeEventLine(["op": "watch_ack"])' in ack_action
    assert 'chipJournal("watch-ack:emit")' in ack_action
    assert "URLSession" not in ack_action
    # Enabled only when state.json carries watch_hit.acknowledged == false.
    acknowledged_gate = _swift_function_body(source,
                                             "private func unacknowledgedWatchHit() -> Bool {",
                                             "/// The shared state snapshot")
    assert 'object["watch_hit"]' in acknowledged_gate
    assert 'hit["acknowledged"]' in acknowledged_gate
    assert "acknowledged is NSNull" in acknowledged_gate
    assert "return !flag" in acknowledged_gate
    frontmost = _swift_function_body(source,
                                     "private func hatMenuFrontmostAppName()",
                                     "/// Is a watch active?")
    # The frontmost app is the menu-bar owner, never this accessory helper.
    assert "NSWorkspace.shared.menuBarOwningApplication" in frontmost
    assert "app.processIdentifier != me" in frontmost
    # No active watch → the two watch-reading rows are disabled honestly.
    assert "watchActive" in menu
    assert "isEnabled = enabled" in menu
    assert "watchIsActive()" in menu

    watch_state = _swift_function_body(source, "private func watchIsActive() -> Bool {",
                                       "/// The `watch` row of the shared state")
    # The engine writes "stopped": null while a watch runs, and JSON null is
    # NSNull in Swift, not nil: both must count as active.
    assert 'let stopped = row["stopped"]' in watch_state
    assert "stopped == nil || stopped is NSNull" in watch_state
    assert "watchObserver != nil" in watch_state
    read = _swift_function_body(source, "private func readWatchStateRow()",
                                "// MARK: System-wide summon hot key")
    assert "state.json" in read
    assert 'object["watch"]' in read

    # The right-click reaches the controller from the chip view.
    chip_view = _swift_function_body(source,
                                     "private final class ClaimChipView: NSView {",
                                     "// MARK: - Pointer LOOK card")
    assert "override func rightMouseDown(with event: NSEvent)" in chip_view
    assert "onRightClick?(point)" in chip_view
    assert "var onRightClick: ((NSPoint) -> Void)?" in chip_view
    wired = _swift_function_body(source, "private func applyClaimChipFrame(",
                                 "private func hideClaimChip(")
    assert "view.onRightClick" in wired
    assert "claimChipRightClicked(at: point)" in wired

    right = _swift_function_body(source, "private func claimChipRightClicked(at point: NSPoint) {",
                                 "/// `Look at this…` arms INSPECT MODE")
    assert 'cancelInspectMode(reason: "right-click")' in right, (
        "a right-click while the mode is up exits it")
    assert "presentHatMenu(at: point)" in right


# --- INSPECT MODE: right-click the hat → a screen-sized click catcher --------

def test_inspect_mode_builds_one_click_catcher_per_screen() -> None:
    """A follower with a fixed offset can never be clicked, so the mode is a
    screen-sized overlay window per display: the click the user means to make on
    the control lands on US instead. Nothing becomes key or main, so the user's
    app keeps focus and the keyboard.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "private final class InspectOverlayWindow: NSPanel {" in source
    window = _swift_function_body(source,
                                  "private final class InspectOverlayWindow: NSPanel {",
                                  "/// The click-catching surface")
    assert "override var canBecomeKey: Bool" in window
    assert "override var canBecomeMain: Bool" in window
    assert window.count("return false") >= 2, "neither key nor main"

    arm = _swift_function_body(source,
                               "private func armInspectMode(releaseToAsk: Bool = false) {",
                               "/// One click catcher per screen")
    # Never behind an open ask field: every door into the field disarms the mode.
    assert "!inspectMode" in arm
    assert "!summonFieldOpen" in arm
    assert "dismissLookCard()" in arm, "our own drop card is not part of the read path"
    assert "NSCursor.crosshair.push()" in arm
    assert "claimChipWindow?.orderOut(nil)" in arm, (
        "the hat is an indicator while the overlay owns the pointer")
    assert 'chipJournal("inspect-arm")' in arm
    assert "raiseInspectOverlays()" in arm
    assert "installInspectTimers()" in arm
    # Round 16: the drag arms the SAME function with the release confirmation.
    assert "inspectReleaseToAsk = releaseToAsk" in arm
    assert "installInspectMonitors(releaseToAsk: releaseToAsk)" in arm

    overlays = _swift_function_body(source, "private func raiseInspectOverlays() {",
                                    "private func installInspectTimers()")
    # One borderless overlay per NSScreen, covering that screen's full frame.
    assert "for screen in NSScreen.screens" in overlays
    assert "contentRect: frame" in overlays
    assert "styleMask: [.borderless, .nonactivatingPanel]" in overlays
    assert "window.level = .screenSaver" in overlays
    assert "window.isOpaque = false" in overlays
    assert "NSColor.black.withAlphaComponent(0.05)" in overlays
    # It catches clicks; it never becomes key or main and never activates us.
    assert "window.ignoresMouseEvents = false" in overlays
    assert "InspectOverlayView(frame:" in overlays
    assert "inspectWindows.append(window)" in overlays
    assert "inspectViews.append(view)" in overlays
    assert "makeKey" not in overlays
    assert "NSApp.activate" not in overlays

    view = _swift_function_body(source,
                                "private final class InspectOverlayView: NSView {",
                                "/// NSMenu needs an NSObject target/action pair")
    assert "override var acceptsFirstResponder: Bool" in view
    assert "override func acceptsFirstMouse" in view


def test_inspect_click_parks_the_overlays_and_defers_the_hit_test() -> None:
    """At the click point the topmost window is OUR overlay, so an immediate AX
    hit-test would answer with us (§8). Every overlay is parked off-screen and
    the read runs on the next run-loop turn; the pre-arm read-out is never
    reused, even when the pointer never moved.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    click = _swift_function_body(
        source, "private func inspectClicked(at screenPoint: NSPoint) {",
        "/// Read the click point with the overlays parked")
    assert "-20000.0" in click, "every overlay must be parked off-screen"
    assert "setFrameOrigin" in click
    assert "DispatchQueue.main.async" in click, (
        "the hit-test must run after the window server catches up")
    assert "resolveInspectClick(at: point, retries: 1)" in click
    # A click that never left the arming point tests it; any real travel uses
    # the click itself, still resolved fresh after parking.
    assert "inspectArmPoint" in click
    assert "InspectModeSpec.moveThreshold" in click
    assert "lookElementDescriptor" not in click, "the read happens after the defer"

    resolve = _swift_function_body(source, "private func resolveInspectClick(",
                                   "/// The hat's right-click")
    assert "lookElementDescriptor(at: point)" in resolve
    assert "lookDescriptorIsSelf(descriptor)" in resolve
    assert "retries - 1" in resolve, "one short retry while the parked overlay settles"
    assert "lookDescriptorForActiveApp()" in resolve
    assert 'chipJournal("inspect-self-hit-fallback")' in resolve
    # The last resort is re-checked: opening the ask about OUR OWN app is never
    # an answer, so that path cancels instead.
    assert 'cancelInspectMode(reason: "self-after-fallback")' in resolve
    assert 'chipJournal("inspect-click:"' in resolve
    # The existing LOOK path, exactly like the drag route: queued target then
    # the same ask field.
    assert "pendingLookContext" in resolve
    assert "pendingLookTarget" in resolve
    assert "LookTarget(app: descriptor.app" in resolve
    assert "disarmInspectMode()" in resolve
    assert "openSummonField()" in resolve


def test_inspect_badge_follows_the_cursor_and_never_covers_the_point() -> None:
    """The badge is a dark pill with the gesture and the live target. It sits at
    cursor + (18, 26) and flips/clamps so it is never under the tested point.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    # Round 16: the pill's gesture line is honest about the arm mode.
    assert 'static let clickTitle = "👀 click to ask · esc / right-click to cancel"' in source
    assert 'static let releaseTitle = "👀 release to ask · esc to cancel"' in source
    assert "click to ask" in source
    assert "release to ask" in source
    assert "right-click to cancel" in source
    assert "static let badgeOffsetX: CGFloat = 18.0" in source
    assert "static let badgeOffsetY: CGFloat = 26.0" in source
    assert "static let badgeFontSize: CGFloat = 11.0" in source
    assert "static let badgeMaxCharacters = 30" in source

    view = _swift_function_body(source,
                                "private final class InspectOverlayView: NSView {",
                                "/// NSMenu needs an NSObject target/action pair")
    place = _swift_function_body(view,
                                 "private func badgeFrame(cursor: NSPoint, size: NSSize) -> NSRect {",
                                 "/// The controller feeds the pointer")
    assert "cursor.x + dx" in place and "cursor.y + dy" in place
    assert "(InspectModeSpec.badgeOffsetX, InspectModeSpec.badgeOffsetY)" in place
    assert "bounds.minX + 4.0" in place
    assert "bounds.maxX - size.width - 4.0" in place
    assert "bounds.minY + 4.0" in place
    assert "bounds.maxY - size.height - 4.0" in place
    assert "if !frame.contains(cursor)" in place, "never leave the pill on the point"
    assert "(-InspectModeSpec.badgeOffsetX, -InspectModeSpec.badgeOffsetY)" in place
    # Even a corner clamp that lands on the point falls back to pinning the pill
    # entirely to one side of the cursor ...
    assert "cursor.x + InspectModeSpec.badgeOffsetX + size.width" in place
    assert "cursor.x - InspectModeSpec.badgeOffsetX - size.width" in place
    assert "for frame in sideFrames where !frame.contains(cursor)" in place, (
        "the side fallback is re-checked, not returned blind")
    # ... and a panel narrower than the pill clears it vertically instead.
    assert "y: cursor.y + InspectModeSpec.badgeOffsetY" in place
    assert "cursor.y - InspectModeSpec.badgeOffsetY - size.height" in place

    assert "badgeTitle as NSString" in view, "the pill draws the gesture line it was given"
    assert "private var badgeTitle = InspectModeSpec.clickTitle" in view
    assert "title != badgeTitle" in view, "a mode change repaints the gesture line"
    assert "badgeDetail" in view, "the second line is the live target"
    assert "NSBezierPath(roundedRect: badgeFrame" in view

    line = _swift_function_body(source, "private func inspectTargetLine(",
                                "/// A screen-sized click catcher")
    assert "\\u{00B7}" in line
    assert "\\u{201C}" in line
    assert "inspectTargetText(text)" in line
    cap = _swift_function_body(source, "private func inspectTargetText(",
                               "/// The descriptor as one line")
    assert "text.count > limit" in cap
    assert "prefix(limit - 1)" in cap
    assert "\\u{2026}" in cap, "the cap ellipsises instead of cutting mid-word"


def test_inspect_hover_preview_is_app_scoped_and_movement_gated() -> None:
    """The overlay is topmost everywhere, so a system-wide hit-test would answer
    with us. The preview asks the app UNDER the overlay instead (an
    application-scoped AXUIElementCopyElementAtPosition), on a 10 Hz tick gated
    by 6 pt of pointer movement, and never tests a point inside the badge.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "static let tickInterval: TimeInterval = 1.0 / 10.0" in source
    assert "static let moveThreshold: CGFloat = 6.0" in source

    tick = _swift_function_body(source, "private func inspectTick() {",
                                "/// Feed each overlay view the pointer")
    assert "NSEvent.mouseLocation" in tick
    assert "InspectModeSpec.moveThreshold" in tick
    assert "inspectBadgeContains(quartz, primaryHeight: primaryHeight)" in tick
    assert "inspectDescriptorUnderCursor(at: quartz)" in tick
    assert "lookElementDescriptor(at: quartz)" not in tick, (
        "the system-wide read would answer with our own overlay")
    assert "lookDescriptorIsSelf(descriptor)" in tick
    # No readable target clears the line instead of keeping a stale control.
    assert 'inspectBadgeDetail = ""' in tick
    assert "updateInspectBadges(cursor: mouse)" in tick

    scoped = _swift_function_body(
        source,
        "private func inspectDescriptorUnderCursor(at point: CGPoint) -> LookDescriptor? {",
        "/// Left click on the overlay")
    assert "AXUIElementCreateApplication(pid)" in scoped
    assert "AXUIElementCopyElementAtPosition(app" in scoped
    assert "lookDescriptor(for: hit, at: point)" in scoped

    pid = _swift_function_body(source,
                               "private func inspectUnderlyingPid(at point: CGPoint) -> pid_t? {",
                               "/// Read the control under `point`")
    assert "CGWindowListCopyWindowInfo" in pid
    assert "kCGWindowOwnerPID" in pid
    assert "pid_t(rawPid) != me" in pid, "our own overlay is never a target"
    assert "bounds.contains(point)" in pid

    badges = _swift_function_body(source,
                                  "private func updateInspectBadges(cursor: NSPoint) {",
                                  "/// True when a Quartz point lands inside the badge")
    assert "view.convert(" in badges
    assert "view.updateBadge(cursor: local" in badges


def test_inspect_cursor_is_the_crosshair_and_is_popped_on_exit() -> None:
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    view = _swift_function_body(source,
                                "private final class InspectOverlayView: NSView {",
                                "/// NSMenu needs an NSObject target/action pair")
    assert "override func resetCursorRects()" in view
    assert "addCursorRect(bounds, cursor: .crosshair)" in view
    assert "override func cursorUpdate(with event: NSEvent)" in view
    assert "NSCursor.crosshair.set()" in view

    arm = _swift_function_body(source,
                               "private func armInspectMode(releaseToAsk: Bool = false) {",
                               "/// One click catcher per screen")
    assert "NSCursor.crosshair.push()" in arm

    disarm = _swift_function_body(source, "private func disarmInspectMode() {",
                                  "/// The 10 Hz loop")
    # Round 16: the Esc monitors (and the drag release catcher) never outlive
    # the mode.
    assert "for monitor in inspectMonitors" in disarm
    assert "NSEvent.removeMonitor(monitor)" in disarm
    assert "inspectMonitors.removeAll()" in disarm
    assert "inspectReleaseToAsk = false" in disarm
    assert "NSCursor.pop()" in disarm
    assert "NSCursor.arrow.set()" in disarm
    assert "window.orderOut(nil)" in disarm
    assert "window.close()" in disarm
    assert "inspectWindows.removeAll()" in disarm
    assert "inspectViews.removeAll()" in disarm
    assert 'chipJournal("inspect-disarm")' in disarm
    # The hat returns to the pet.
    assert "positionClaimChip()" in disarm
    assert "claimChipWindow?.orderFrontRegardless()" in disarm


def test_inspect_mode_times_out_and_journals_every_hop() -> None:
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "static let timeout: TimeInterval = 60.0" in source

    timers = _swift_function_body(source, "private func installInspectTimers() {",
                                  "/// Right-click, a badge click")
    assert "Timer(timeInterval: InspectModeSpec.tickInterval" in timers
    assert "repeats: true" in timers
    assert "Timer(timeInterval: InspectModeSpec.timeout" in timers
    assert "repeats: false" in timers
    assert "inspectTimedOut()" in timers

    timed_out = _swift_function_body(source, "private func inspectTimedOut() {",
                                     "private func disarmInspectMode()")
    assert 'chipJournal("inspect-timeout")' in timed_out
    assert 'cancelInspectMode(reason: "timeout")' in timed_out

    cancel = _swift_function_body(source, "private func cancelInspectMode(reason: String) {",
                                  "private func inspectTimedOut()")
    assert 'chipJournal("inspect-cancel:" + reason)' in cancel
    assert "disarmInspectMode()" in cancel

    for event in ("inspect-arm", "drag-arm", "drag-arm-refused", "drag-release:",
                  "inspect-escape", "inspect-click:", "inspect-cancel:",
                  "inspect-timeout"):
        assert f'chipJournal("{event}' in source, f"journal line {event} is missing"


def test_inspect_cancel_doors_are_right_click_the_badge_and_escape() -> None:
    """Right click anywhere, a click inside the badge and Esc all cancel.

    Round 16 makes Escape universal across the LOOK paths, so the mode now does
    read one key — and only one — through the arm/disarm monitor pair.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    view = _swift_function_body(source,
                                "private final class InspectOverlayView: NSView {",
                                "/// NSMenu needs an NSObject target/action pair")
    down = _swift_function_body(
        view, "override func mouseDown(with event: NSEvent) {",
        "override func rightMouseDown(with event: NSEvent) {")
    assert "if badgeFrame.contains(local)" in down
    assert "onBadgeClick?()" in down
    assert "onInspectClick?(window.convertPoint(toScreen:" in down
    right = _swift_function_body(
        view, "override func rightMouseDown(with event: NSEvent) {",
        "/// Two 11 pt lines plus air")
    assert "onCancel?()" in right

    overlays = _swift_function_body(source, "private func raiseInspectOverlays() {",
                                    "private func installInspectTimers()")
    assert 'cancelInspectMode(reason: "right-click")' in overlays
    assert 'cancelInspectMode(reason: "badge")' in overlays
    assert "keyDown" not in overlays, "the overlays themselves never read a key"

    monitors = _swift_function_body(source, "private func installInspectMonitors(",
                                    "/// One Esc path for both arm modes")
    assert "NSEvent.addGlobalMonitorForEvents(matching: .keyDown" in monitors
    escape = _swift_function_body(source, "private func escapeInspectMode() -> Bool {",
                                  "private func disarmInspectMode()")
    assert 'chipJournal("inspect-escape")' in escape
    assert 'cancelInspectMode(reason: "escape")' in escape


def test_the_round_thirteen_follower_and_eye_click_machinery_is_gone() -> None:
    """Round 13's follower (and its clickable eye) is removed, not hidden: a
    control that tracks the cursor can never be clicked on purpose."""
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    for gone in ("answerEyeClick", "eyeTick", "EyeModeSpec", "EyeLabelView",
                 "EyeLabelWindow", "eyeLabelText", "installEyeMonitors",
                 "removeEyeMonitors", "eyeLatchedDescriptor", "eyePointIsOurs",
                 "eyeDescriptorIsSelf", "eyeFollowFrame", "positionEyeWindow",
                 "positionEyeLabel", "ensureEyeLabel", "updateEyeLabel",
                 "dismissEyeLabel", "armEyeMode", "disarmEyeMode",
                 "claimChipView?.eyeMode", "EyeMenuItemTarget", "1.0 / 15.0"):
        assert gone not in source, f"round-13 eye machinery must be gone: {gone}"
    assert "private func armInspectMode(releaseToAsk: Bool = false)" in source
    assert "private func disarmInspectMode()" in source
    assert "private final class InspectOverlayView: NSView" in source


def test_inspect_path_needs_no_event_tap_flags_or_input_monitoring() -> None:
    """The mode captures pointer clicks with its own windows and reads exactly
    one key: Esc, through a local + global monitor (the look card proves the
    same global monitor works). No event tap, no modifier-key stream, no Input
    Monitoring, no trust probe: the companion's existing Accessibility grant is
    the single subject (§4)."""
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    for forbidden in ("CGEventTap", "flagsChanged", "Input Monitoring",
                      "kCGSessionEventTap"):
        assert forbidden not in source, f"the inspect path must not use {forbidden}"

    inspect_path = (_swift_function_body(source,
                                         "private func armInspectMode(releaseToAsk: Bool = false) {",
                                         "/// One click catcher per screen")
                    + _swift_function_body(source, "private func raiseInspectOverlays() {",
                                           "private func installInspectTimers()")
                    + _swift_function_body(source, "private func inspectTick() {",
                                           "/// Feed each overlay view the pointer")
                    + _swift_function_body(source, "private func inspectDescriptorUnderCursor(",
                                           "/// Left click on the overlay")
                    + _swift_function_body(source, "private func inspectClicked(",
                                           "/// Read the click point with the overlays parked")
                    + _swift_function_body(source, "private func resolveInspectClick(",
                                           "/// The hat's right-click"))
    assert "AXIsProcessTrustedWithOptions" not in inspect_path
    assert "CGEventPost" not in inspect_path

    # The only monitors: Esc (both scopes) and, for a drag arm, the release.
    monitors = _swift_function_body(source, "private func installInspectMonitors(",
                                    "/// One Esc path for both arm modes")
    assert "let escape: UInt16 = 53" in monitors
    assert "NSEvent.addLocalMonitorForEvents(matching: .keyDown" in monitors
    assert "NSEvent.addGlobalMonitorForEvents(matching: .keyDown" in monitors
    assert monitors.count("event.keyCode == escape") == 2, (
        "Esc is checked in both the local and the global handler")
    assert "NSEvent.addLocalMonitorForEvents(matching: .leftMouseUp" in monitors
    assert "flagsChanged" not in monitors


# --- Round 16: one LOOK UX — a drag arms INSPECT MODE too ---------------------

def test_drag_arms_the_same_inspect_mode_and_confirms_on_release() -> None:
    """Dragging the hat and right-clicking it are ONE gesture with one UX.

    The drag enters INSPECT MODE (same overlays, same badge, same resolver) with
    ``releaseToAsk`` set; the only difference is that the target is confirmed
    when the pointer comes up instead of on a click. A press under the threshold
    is still the plain click that opens the ask field.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    arm = _swift_function_body(source,
                               "private func armInspectMode(releaseToAsk: Bool = false) {",
                               "/// One click catcher per screen")
    assert "inspectReleaseToAsk = releaseToAsk" in arm
    assert 'chipJournal("drag-arm")' in arm
    assert 'chipJournal("inspect-arm")' in arm

    drag = _swift_function_body(source,
                                "private func beginLookDrag(at screenPoint: NSPoint) {",
                                "/// Nothing to drag any more")
    assert "armInspectMode(releaseToAsk: true)" in drag, (
        "the drag reuses the right-click arm path, with the release flag")
    assert 'chipJournal("drag-arm-refused")' in drag, (
        "a refused arm is journaled instead of failing silently")

    move = _swift_function_body(source,
                                "private func moveLookDrag(to screenPoint: NSPoint) {",
                                "/// Release: a drag-armed mode confirms HERE")
    assert "guard !inspectMode" in move
    assert move.find("guard !inspectMode") < move.find("setFrameOrigin"), (
        "while INSPECT MODE is up the hidden hat is not dragged")

    mouse_up = _swift_function_body(
        source, "private func endLookDrag(at screenPoint: NSPoint) {",
        "/// Park the overlays, then resolve on the next run-loop turn")
    assert "guard inspectMode, inspectReleaseToAsk else" in mouse_up
    assert "resolveInspectRelease(at: screenPoint)" in mouse_up
    assert "presentLookCard(" not in mouse_up, "the drop card is not raised any more"
    assert "resolveLookDrop(" not in mouse_up

    release = _swift_function_body(
        source, "private func resolveInspectRelease(at screenPoint: NSPoint) {",
        "/// The release point when the chip's mouseUp is lost")
    # The release resolves through INSPECT MODE's resolver: parked, then deferred.
    assert "inspectResolving = true" in release
    assert "-20000.0" in release
    assert "DispatchQueue.main.async" in release
    assert "resolveInspectClick(at: point, retries: 1)" in release
    assert "lookElementDescriptor" not in release, "the read happens after the defer"

    resolve = _swift_function_body(source, "private func resolveInspectClick(",
                                   "/// The hat's right-click")
    assert 'chipJournal("drag-release:" + (descriptor.role ?? "?"))' in resolve
    assert 'chipJournal("inspect-click:" + (descriptor.role ?? "?"))' in resolve
    assert resolve.find("disarmInspectMode()") < resolve.find("openSummonField()"), (
        "the mode, and its Esc monitors, are gone before the field takes key")

    # The two arm modes offer honestly different confirmations, and the badge
    # draws the line it was handed.
    assert 'static let clickTitle = "👀 click to ask · esc / right-click to cancel"' in source
    assert 'static let releaseTitle = "👀 release to ask · esc to cancel"' in source
    assert "inspectReleaseToAsk ? InspectModeSpec.releaseTitle" in source


def test_escape_cancels_inspect_mode_and_its_monitors_die_with_it() -> None:
    """Escape cancels in every LOOK path: both arm modes, the ask field, and
    (already) the read-out card.

    The monitors are installed at arm and removed at disarm, so no key capture
    outlives the mode. Local + global: the global one is how Esc reaches a
    background accessory helper, and the companion already holds the trust the
    look card's global monitor proves.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    arm = _swift_function_body(source,
                               "private func armInspectMode(releaseToAsk: Bool = false) {",
                               "/// One click catcher per screen")
    assert "installInspectMonitors(releaseToAsk: releaseToAsk)" in arm

    monitors = _swift_function_body(source, "private func installInspectMonitors(",
                                    "/// One Esc path for both arm modes")
    assert "NSEvent.addLocalMonitorForEvents(matching: .keyDown" in monitors
    assert "NSEvent.addGlobalMonitorForEvents(matching: .keyDown" in monitors
    assert "let escape: UInt16 = 53" in monitors
    assert monitors.count("event.keyCode == escape") == 2
    # Only a consumed Esc is swallowed; a stray monitor can never eat the key.
    assert "(self?.escapeInspectMode() ?? false) ? nil : event" in monitors

    disarm = _swift_function_body(source, "private func disarmInspectMode() {",
                                  "/// The 10 Hz loop")
    assert "for monitor in inspectMonitors" in disarm
    assert "NSEvent.removeMonitor(monitor)" in disarm
    assert "inspectMonitors.removeAll()" in disarm

    escape = _swift_function_body(source, "private func escapeInspectMode() -> Bool {",
                                  "private func disarmInspectMode()")
    assert 'chipJournal("inspect-escape")' in escape
    assert 'cancelInspectMode(reason: "escape")' in escape

    # The ask field keeps its own Esc door.
    assert "cancelOperation" in source


def test_inspect_arm_survives_teardown_and_the_ask_field_is_unchanged() -> None:
    """The mode's timers/overlays must die with the chip and shutdown, and every
    door into the ask field leaves the mode first."""
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    disarm_chip = _swift_function_body(
        source, "private func setClaimChip(_ spec: ClaimChipSpec)",
        "// MARK: Pet AX observation")
    assert "disarmInspectMode()" in disarm_chip

    shutdown = _swift_function_body(source, "func shutdown() {",
                                    "// MARK: - Application delegate")
    assert "disarmInspectMode()" in shutdown

    opened = _swift_function_body(source, "private func openSummonField(",
                                  "private func closeSummonField(")
    assert "private func openSummonField(prefill: String?) {" in source
    # The frame is taken first so the field opens where the hat expects it.
    assert "disarmInspectMode()" in opened
    assert opened.find("let appKitFrame = appKitRect") < opened.find("disarmInspectMode()")
    assert "view.field.stringValue = prefill" in opened
    assert "editor.selectedRange = NSRange(location: (prefill as NSString).length" in opened
    assert "makeFirstResponder(view.field)" in opened

    # Both placement paths yield the frame while the mode owns the screens.
    position = _swift_function_body(source, "private func positionClaimChip()",
                                    "private func applyClaimChipFrame(")
    assert "guard !inspectMode else { return }" in position
    apply_body = _swift_function_body(source, "private func applyClaimChipFrame(",
                                      "private func hideClaimChip(")
    assert "guard !inspectMode else { return }" in apply_body


def test_right_click_opens_the_hat_menu() -> None:
    """Right-click shows the menu, because a hidden Option chord is not a UI.

    The direct LOOK gesture is the drag; inspect-by-click remains available from
    the menu's first item, and right-click still cancels an armed picker.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    handler = _swift_function_body(
        source, "private func claimChipRightClicked(at point: NSPoint) {",
        "/// `Look at this…` arms INSPECT MODE")
    assert "presentHatMenu(at: point)" in handler
    assert "modifierFlags.contains(.option)" not in handler, \
        "no hidden chord: the menu is the plain right-click"
    assert 'cancelInspectMode(reason: "right-click")' in handler
    assert "\n        armInspectMode()\n" not in handler

    menu = _swift_function_body(source, "private func presentHatMenu(",
                                "/// The app a watch would attach to")
    for row in ('"Look at this…"', '"Watch \\(app)…"',
                '"What did the watch see?"', '"Stop watching"'):
        assert row in menu, f"the Option menu keeps {row}"
    assert "self?.armInspectMode()" in menu


def test_ask_field_sits_above_inspect_overlays_and_asserts_focus() -> None:
    """Measured: Return typed into the butler field also fired in the app behind.

    Two causes are covered here: the field was at `.statusBar` while INSPECT
    MODE's overlays sit at `.screenSaver` (a leftover overlay hid the field, so
    keystrokes went to the app underneath), and a refused activation was silent.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "NSWindow.Level.screenSaver.rawValue + 1" in source, \
        "the ask field must outrank the inspect overlays"
    assert "ask-focus active=" in source, "focus is asserted, not assumed"
    assert "NSRunningApplication.current.activate(options:" in source


# --- The hat exists even when the pet is docked -----------------------------
#
# Round 18B: the hat used to be anchored ONLY to the popped-out pet panel. In
# the fresh-install default the pet is docked inside the Hermes window, there is
# no 240x300 NSPanel, `hermesPetWindowFrame` returns nil and `positionClaimChip`
# called `hideClaimChip()` every tick — the chip window was created and destroyed
# forever, so LOOK, the ask field and the watch menu were all unreachable. The
# fix is a third placement source: a free-standing remembered point.


def test_hat_falls_back_to_a_free_anchor_after_a_three_second_grace() -> None:
    """Placement order: pet panel, then the free anchor; the hat never hides.

    The grace is what keeps a pet that is being dragged, resized or created from
    flickering the hat to the corner. A hat that was never pet-anchored (fresh
    install, docked pet) goes free at once, because an invisible hat is worse
    than a misplaced one.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    anchor = _swift_function_body(source, "private enum ClaimChipFreeAnchor {",
                                  "/// A borderless window cannot become key")
    assert 'static let defaultsKey = "butlerHatFreeAnchor"' in anchor
    assert "static let petGrace: TimeInterval = 3.0" in anchor
    assert "static let inset: CGFloat = 40.0" in anchor
    assert "UserDefaults" in anchor, "the point lives in the companion's prefs"
    # "x,y" is the stored form, and a malformed value means "never placed".
    assert 'defaults.set("\\(point.x),\\(point.y)"' in anchor
    assert "return nil" in anchor

    position = _swift_function_body(source, "private func positionClaimChip() {",
                                    "private func applyClaimChipFrame(")
    pet_branch = position.find("if let petWindow = hermesPetWindowFrame(")
    free_branch = position.find("applyFreeAnchorFrame()")
    assert pet_branch != -1 and free_branch != -1
    assert pet_branch < free_branch, "the popped-out pet panel is still first"
    # A pet-anchored tick places and RETURNS. Without the return every tick
    # would also run the free placement and yank the hat to the corner while a
    # pet sits on screen (219 tests stayed green when it was deleted).
    pet_place = position.find("applyClaimChipFrame(petWindow: petWindow", pet_branch)
    pet_return = position.find("\n            return\n", pet_place)
    assert pet_place != -1 and pet_place <= pet_return < free_branch, (
        "the pet branch must return before the free fallback")
    assert "ClaimChipFreeAnchor.petGrace" in position
    assert "claimChipPetMissingSince" in position
    assert "claimChipPetMissingSince = nil" in position, (
        "finding a pet clears the miss, or the grace counts from a stale point")
    assert "timeIntervalSince(missingSince) < ClaimChipFreeAnchor.petGrace" in position

    # The event-driven AX path skips positionClaimChip, so the choke point both
    # anchors go through must clear the miss as well.
    present = _swift_function_body(source, "private func presentClaimChipFrame(",
                                   "private func announceClaimChipAnchor(")
    # Bind every find first: `-1 < x` is always true, so an inline comparison
    # would go green when the guard or the reset line was deleted.
    guard_at = present.find("if mode == .pet {")
    reset_at = present.find("claimChipPetMissingSince = nil")
    announce_at = present.find("announceClaimChipAnchor(mode)")
    assert 0 <= guard_at < reset_at < announce_at, (
        "the pet-mode choke point clears the miss before announcing")
    # The old "unknown/renamed pet window: stay hidden" gate is what made the
    # hat unreachable; it must not come back in any form.
    assert "stay hidden rather than guess" not in position
    assert position.count("hideClaimChip()") == 1, (
        "only the disarm/shutdown/no-screen guard may hide the hat")
    assert (position.find("guard claimChipEnabled, !isShuttingDown,")
            < position.find("hideClaimChip()"))

    # A pet frame that resolves to no usable screen also falls back to the free
    # anchor instead of hiding.
    apply_body = _swift_function_body(source, "private func applyClaimChipFrame(",
                                      "private func applyFreeAnchorFrame()")
    assert "applyFreeAnchorFrame()" in apply_body
    assert apply_body.count("hideClaimChip()") == 1
    assert (apply_body.find("guard claimChipEnabled, !isShuttingDown else {")
            < apply_body.find("hideClaimChip()"))

    # The free placement itself: clamp onto a visible screen so a display change
    # cannot strand the hat, and report mode `.free`.
    free = _swift_function_body(source, "private func applyFreeAnchorFrame() {",
                                "private func freeHatOrigin(")
    assert "freeHatOrigin(size: size)" in free
    assert "mode: .free" in free
    # No screen geometry at all leaves the hat alone rather than hiding it.
    assert "guard let origin = freeHatOrigin(size: size) else {" in free
    assert free.count("hideClaimChip()") == 1
    assert (free.find("guard claimChipEnabled, !isShuttingDown else {")
            < free.find("hideClaimChip()"))


def test_free_hat_defaults_to_the_main_screens_bottom_right_corner() -> None:
    """No remembered point: main screen's visibleFrame, 40 pt in on two edges.

    `visibleFrame` (not `frame`) already excludes the menu bar and the Dock, and
    the 40 pt inset keeps the hat clear of the Dock on a default layout.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    anchor = _swift_function_body(source, "private enum ClaimChipFreeAnchor {",
                                  "/// A borderless window cannot become key")
    assert "static let inset: CGFloat = 40.0" in anchor

    default = _swift_function_body(
        source, "static func defaultOrigin(size: CGSize) -> NSPoint? {",
        "/// A borderless window cannot become key")
    assert "primaryScreen()" in default
    assert "screen.visibleFrame" in default
    assert "visible.maxX - inset - size.width" in default, \
        "40 pt in from the right edge (the hat's right edge clears the edge)"
    assert "visible.minY + inset" in default, "40 pt up from the bottom edge"
    assert "screen.frame" not in default, "visibleFrame, never the full frame"

    # A remembered point wins over the default and is persisted on release only.
    origin = _swift_function_body(source, "private func freeHatOrigin(size: CGSize) -> NSPoint? {",
                                  "/// Put the hat window at a resolved AppKit frame")
    assert "ClaimChipFreeAnchor.remembered(from: UserDefaults.standard)" in origin
    assert "ClaimChipFreeAnchor.defaultOrigin(size: size)" in origin
    assert "visible.minX + margin" in origin and "visible.maxY - size.height - margin" in origin


def test_free_hat_drag_moves_the_hat_and_never_arms_inspect_mode() -> None:
    """Free: press-and-drag MOVES the hat. Pet-anchored: it still drags-to-LOOK.

    The mode decides the gesture: the same press cannot mean both "read the
    element under the pointer" and "put the butler where I can see it". The
    plain click and the right-click keep their meanings in both modes.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    begin = _swift_function_body(source,
                                 "private func beginLookDrag(at screenPoint: NSPoint) {",
                                 "/// Nothing to drag any more")
    free_branch = begin.find("if claimChipAnchorMode == .free")
    move_call = begin.find("beginHatFreeMove()")
    branch_return = begin.find("return", move_call)
    arm_call = begin.find("armInspectMode(releaseToAsk: true)")
    assert 0 <= free_branch < move_call < branch_return < arm_call, (
        "the free branch returns before the inspect arm")

    begin_move = _swift_function_body(
        source, "private func beginHatFreeMove() {",
        "/// Move the free hat with the pointer")
    assert "claimChipFreeMoveActive = true" in begin_move
    assert 'chipJournal("hat-free-move")' in begin_move

    move = _swift_function_body(source,
                                "private func moveLookDrag(to screenPoint: NSPoint) {",
                                "/// Release: a drag-armed mode confirms HERE")
    assert "hatFreeMove(to: screenPoint)" in move
    assert move.find("claimChipFreeMoveActive") < move.find("guard !inspectMode"), (
        "the free hat moves before the inspect-mode guard can swallow the drag")

    free_move = _swift_function_body(source,
                                     "private func hatFreeMove(to screenPoint: NSPoint) {",
                                     "/// The release persisted the free hat")
    assert "window.setFrameOrigin(origin)" in free_move
    assert "claimChipDragOffset" in free_move, "the grab offset stops a jump"

    end = _swift_function_body(source,
                               "private func endLookDrag(at screenPoint: NSPoint) {",
                               "/// Park the overlays, then resolve on the next run-loop turn")
    assert "endHatFreeMove()" in end
    assert "resolveInspectRelease(at: screenPoint)" in end, (
        "the pet-anchored drag still confirms LOOK on release")
    assert end.find("endHatFreeMove()") < end.find(
        "guard inspectMode, inspectReleaseToAsk else")

    free_end = _swift_function_body(
        source, "private func endHatFreeMove() {",
        "/// Park the overlays, then resolve on the next run-loop turn")
    assert "ClaimChipFreeAnchor.remember(origin, in: UserDefaults.standard)" in free_end
    assert 'chipJournal("hat-free-anchored")' in free_end
    assert "window.frame.origin" in free_end

    # The doors the user needs are mode-independent: neither the plain click nor
    # the right-click asks which anchor the hat is on.
    handler = _swift_function_body(source,
                                   "private func claimChipRightClicked(at point: NSPoint) {",
                                   "/// `Look at this…` arms INSPECT MODE")
    assert "claimChipAnchorMode" not in handler
    assert "presentHatMenu(at: point)" in handler
    click = _swift_function_body(source, "private func claimChipClicked() {",
                                 "// MARK: Pointer LOOK gesture")
    assert "claimChipAnchorMode" not in click


def test_no_free_hat_drag_path_arms_inspect_mode() -> None:
    """The free-hat move path must never reach the inspect arm.

    Round 18B's regression risk is exactly this: a drag on the free hat that
    still raises the screen-sized click catchers would make the hat unplaceable
    and LOOK unreachable from the pet at the same time.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    for marker, end_marker in (
        ("private func beginHatFreeMove() {",
         "/// Move the free hat with the pointer"),
        ("private func hatFreeMove(to screenPoint: NSPoint) {",
         "/// The release persisted the free hat"),
        ("private func endHatFreeMove() {",
         "/// Park the overlays, then resolve on the next run-loop turn"),
    ):
        body = _swift_function_body(source, marker, end_marker)
        assert "armInspectMode" not in body, f"{marker} arms INSPECT MODE"
        assert "inspectReleaseToAsk = true" not in body, f"{marker} fakes a drag arm"

    # And the view's drag state is what tells the glyph the truth: a free hat
    # keeps the hat glyph instead of claiming to read the pointer.
    chip_view = _swift_function_body(source,
                                     "private final class ClaimChipView: NSView {",
                                     "// MARK: - Pointer LOOK card")
    assert "var dragMovesTheHat = false" in chip_view
    assert "lookMode = !dragMovesTheHat" in chip_view


def test_hat_announces_its_anchor_mode_on_stdout() -> None:
    """`{"op":"chip-anchor","mode":"pet"|"free"}` once per placement change.

    The Python side reads every stdout line, so the companion says which anchor
    it is on instead of the plugin guessing. Docs/reporting live on the Python
    side; this half only emits the line.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    assert "private enum ClaimChipAnchorMode: String {" in source
    assert "case pet" in source
    assert "case free" in source
    assert '["op": "chip-anchor", "mode": mode.rawValue]' in source

    announce = _swift_function_body(source,
                                    "private func announceClaimChipAnchor(",
                                    "private func hideClaimChip()")
    assert "claimChipAnnouncedMode != mode" in announce, "one line per CHANGE"
    assert "claimChipAnnouncedMode = mode" in announce
    assert "writeEventLine(" in announce

    # Both placement sources feed the one announcer, so a pet appearing or
    # disappearing is reported exactly once.
    present = _swift_function_body(source,
                                   "private func presentClaimChipFrame(",
                                   "private func announceClaimChipAnchor(")
    assert "announceClaimChipAnchor(mode)" in present
    assert "claimChipAnchorMode = mode" in present
    assert "mode: .free" in source
    assert "mode: .pet" in source

    # A fresh arm starts from a cold anchor, so the first placement announces
    # again instead of staying silent from the previous arm.
    disarm = _swift_function_body(
        source, "private func setClaimChip(_ spec: ClaimChipSpec)",
        "// MARK: Pet AX observation")
    for reset in ("claimChipAnchorMode = nil", "claimChipAnnouncedMode = nil",
                  "claimChipPetMissingSince = nil"):
        assert reset in disarm, f"an explicit disarm resets {reset}"

    # The event line rides the shared writer, not a private print path.
    writer = _swift_function_body(source, "private func writeEventLine(",
                                  "private func writeWatchLine(")
    assert "JSONSerialization.data(withJSONObject: payload" in writer
    # One write per line: two writes could split the JSON line on the pipe.
    assert "FileHandle.standardOutput.write(data + Data([0x0a]))" in writer


def test_watch_scan_has_a_baseline_set_and_a_role_filtered_tick() -> None:
    """The content scan: author-role nodes only, "new" means new since arming.

    AX carries no timestamps, so the companion implements "newer than now" as
    "a match signature that was not in the set recorded at arm". The role
    filter is what distinguishes an author from a message BODY: a node that
    merely contains the name (AXStaticText, the body) can never fire an author
    hit because it is not an author role.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    # Defaults from the brief: 3s tick (floor 2, ceiling 10), 1500 nodes
    # (cap 4000), depth 24, and author roles that never include AXStaticText.
    assert "private let watchScanDefaultInterval: TimeInterval = 3.0" in source
    assert "private let watchScanMinInterval: TimeInterval = 2.0" in source
    assert "private let watchScanMaxInterval: TimeInterval = 10.0" in source
    assert "private let watchScanDefaultMaxNodes = 1500" in source
    assert "private let watchScanMaxNodes = 4000" in source
    assert "private let watchScanDepth = 24" in source
    assert ('private let watchScanDefaultRoles = ["AXImage", "AXButton", "AXLink"]'
            in source)
    roles_line = next(line for line in source.splitlines()
                      if "watchScanDefaultRoles =" in line)
    assert "AXStaticText" not in roles_line, (
        "AXStaticText is the message body, not an author node")

    # Wire: the existing watch_start gains an optional scan object, one matcher.
    parse = _swift_function_body(source, 'case "watch_start":',
                                 'case "watch_stop":')
    assert 'object["scan"]' in parse
    assert "parseWatchScan(" in parse
    assert "until != nil && scan != nil" in parse
    scanner = _swift_function_body(source, "private func parseWatchScan(",
                                   "private func parseCommand(")
    assert 'trimmedString("author")' in scanner
    assert 'trimmedString("text")' in scanner
    assert "watchScanDefaultRoles" in scanner
    assert "watchScanMinInterval" in scanner
    assert "watchScanMaxInterval" in scanner
    assert "watchScanMaxNodes" in scanner

    # State: a baseline signature set plus the repeating tick.
    assert "private var watchScan: WatchScanSpec?" in source
    assert "private var watchScanTimer: Timer?" in source
    assert "private var watchScanBaseline: Set<String> = []" in source

    timer = _swift_function_body(source, "private func startWatchScanTimer()",
                                 "private func armWatchScanBaseline()")
    assert "Timer(timeInterval: scan.interval, repeats: true)" in timer
    assert "RunLoop.main.add(timer, forMode: .common)" in timer

    baseline = _swift_function_body(source, "private func armWatchScanBaseline()",
                                    "private func runWatchScanTick()")
    assert "watchScanBaseline = Set(matches.map { $0.signature })" in baseline
    assert "watchScanBaselineCount = watchScanBaseline.count" in baseline

    tick = _swift_function_body(source, "private func runWatchScanTick()",
                                "private struct WatchScanMatch")
    assert '"op": "watch-scan"' in tick
    assert '"baseline": watchScanBaselineCount' in tick
    assert '"matches": matches.count' in tick
    assert '"new": fresh.count' in tick
    assert "collectWatchScanMatches(scan)" in tick
    assert "!watchScanBaseline.contains($0.signature)" in tick
    # Growth stops at the walk budget instead of re-seeding from the CURRENT
    # matches: dropping an arm-time signature would let a message that
    # scrolled away and back fire again as "new".
    assert "watchScanBaseline.count < watchScanMaxNodes" in tick
    assert "watchScanBaseline = Set(matches.map" not in tick

    # Signature: role + label + whole-point box, so sub-pixel relayout does not
    # make the same message look new on every tick.
    match = _swift_function_body(source, "private func watchScanMatch(",
                                 "private func watchScanWholeWord(")
    assert "readAXFields(element)" in match
    assert "scan.roleSet.contains(role.uppercased())" in match, (
        "author mode is role-filtered: the body cannot match")
    assert "width > 1.0, height > 1.0" in match, (
        "degenerate boxes are transcript artefacts / off-screen rows")
    assert "signature" in match
    assert 'scan.kind' in match

    walk = _swift_function_body(source, "private func walkWatchScan(",
                                "private func watchScanMatch(")
    assert "depth > watchScanDepth" in walk
    assert "visited >= scan.maxNodes" in walk

    # The whole-word matcher fails CLOSED on a matcher that would not compile:
    # author mode must never degrade silently into the weaker substring mode.
    word = _swift_function_body(
        source, "private func watchScanWholeWord(",
        "/// Entry point from the C callback (mirrors petAXNotificationFired)")
    assert "return false" in word
    assert "contains(needle)" not in word
    assert "firstMatch(in: label" in word

    # A scan hit is a watch-hit with kind, once per arm like the event matcher.
    hit = _swift_function_body(source, "private func announceWatchScanHit(",
                               "private func collectWatchScanMatches(")
    assert '"op": "watch-hit"' in hit
    assert '"kind": match.kind' in hit
    assert "watchHitArmed = false" in hit


def test_watch_scan_arms_before_the_ack_and_stops_with_the_watch() -> None:
    """The baseline is recorded at arm (nothing fires), and teardown kills it.

    A match already on screen at arm is the state the user asked about, not
    news. The reply carries the arm-time count so the producer can say
    "baseline 0" honestly when the app exposes no author-role markup.
    """
    source = Path(overlay.binary_source_path()).read_text(encoding="utf-8")

    start = _swift_function_body(source, "private func startWatch(",
                                 "private func resetWatchState()")
    assert "armWatchScanBaseline()" in start
    assert 'started["baseline"] = scanBaseline' in start
    assert "startWatchScanTimer()" in start
    assert "watchHitArmed = spec.until != nil" in start, (
        "the event matcher keeps its own arm path")
    # The scan is armed before the ack, and its tick starts after the ack so a
    # stream of scan lines can never starve watch_start's reply.
    assert start.find("startWatchScanTimer()") > start.find("writeWatchLine(started)")

    reset = _swift_function_body(source, "private func resetWatchState()",
                                 "private func stopWatch(")
    assert "watchScan = nil" in reset
    assert "watchScanTimer?.invalidate()" in reset
    assert "watchScanBaseline.removeAll()" in reset
    assert "watchScanTicks = 0" in reset

    # The dispatch path still routes the one watch_start op.
    assert "startWatch(spec)" in source
    assert "case .watchStart(let spec):" in source
