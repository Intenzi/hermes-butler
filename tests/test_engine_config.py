"""Engine/plugin-data config tests for the summon door."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butler_core import engine  # noqa: E402


def _write_config(data_dir: Path, payload: dict) -> None:
    (data_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def test_pet_click_claim_defaults_on_when_the_key_is_absent(tmp_path: Path) -> None:
    engine.init(tmp_path)

    assert engine.pet_click_claim() is True


def test_pet_click_claim_explicit_false_is_the_manual_escape_hatch(
        tmp_path: Path) -> None:
    engine.init(tmp_path)
    _write_config(tmp_path, {"summon": {"pet_click_claim": False}})
    assert engine.pet_click_claim() is False

    # Removing the key again means the installed plugin is back on.
    _write_config(tmp_path, {"summon": {}})
    assert engine.pet_click_claim() is True


def test_pet_click_claim_reads_nested_knob_and_hot_reloads(tmp_path: Path) -> None:
    engine.init(tmp_path)
    _write_config(tmp_path, {"summon": {"pet_click_claim": True}})
    assert engine.pet_click_claim() is True

    # Same process, no re-init/restart: the next read sees the new file.
    _write_config(tmp_path, {"summon": {"pet_click_claim": False}})
    assert engine.pet_click_claim() is False


def test_pet_click_claim_accepts_flat_and_text_spellings(tmp_path: Path) -> None:
    engine.init(tmp_path)
    _write_config(tmp_path, {"summon.pet_click_claim": True})
    assert engine.pet_click_claim() is True

    _write_config(tmp_path, {"summon": {"pet_click_claim": "off"}})
    assert engine.pet_click_claim() is False

    # Unreadable junk falls back to the installed default, not to off.
    _write_config(tmp_path, {"summon": {"pet_click_claim": "garbage"}})
    assert engine.pet_click_claim() is True


def test_claim_click_sets_summon_pending_and_ack_clears_it(tmp_path: Path) -> None:
    engine.init(tmp_path)

    engine._on_overlay_event({"op": "something_else"})
    assert engine.summon_pending() is None

    engine._on_overlay_event({"op": "claim_click"})
    pending = engine.summon_pending()
    assert pending["origin"] == "pet"
    assert pending["id"]
    snap = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert snap["summon_pending"] == pending

    assert engine.consume_summon_pending("not-the-id") is False
    assert engine.consume_summon_pending(pending["id"]) is True
    assert engine.summon_pending() is None
    snap = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "summon_pending" not in snap


def test_summon_text_sets_a_text_pending_row_for_the_desktop(
        tmp_path: Path) -> None:
    engine.init(tmp_path)
    engine._summon_pending = None

    engine._on_overlay_event({"op": "summon_text", "text": "hi", "id": "abc"})

    pending = engine.summon_pending()
    assert pending["origin"] == "chip"
    assert pending["id"] == "abc"
    assert pending["text"] == "hi"
    assert pending["at"]
    snap = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert snap["summon_pending"] == pending


def test_summon_text_carries_the_ask_field_destination(
        tmp_path: Path) -> None:
    """⌘↵ asks for a fresh chat; every other value means the newest one."""
    engine.init(tmp_path)
    engine._summon_pending = None

    engine._on_overlay_event(
        {"op": "summon_text", "text": "start fresh", "id": "new1",
         "destination": "new"})
    assert engine.summon_pending()["destination"] == "new"
    snap = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert snap["summon_pending"]["destination"] == "new"

    # A missing or unknown value is the safe default, never an error.
    engine._on_overlay_event(
        {"op": "summon_text", "text": "continue", "id": "recent1"})
    assert engine.summon_pending()["destination"] == "recent"

    engine._on_overlay_event(
        {"op": "summon_text", "text": "bogus", "id": "recent2",
         "destination": "somewhere"})
    assert engine.summon_pending()["destination"] == "recent"


def test_look_text_sets_a_look_pending_row_with_the_target_intact(
        tmp_path: Path) -> None:
    """⌘⇧L carries the hit-tested element with the question."""
    engine.init(tmp_path)
    engine._summon_pending = None
    target = {
        "app": "Preview",
        "role": "Button",
        "label": "Export",
        "window": "receipt.pdf",
        "doc": "file:///tmp/receipt.pdf",
        "enabled": True,
        "box": [10, 20, 30, 40],
    }

    engine._on_overlay_event({
        "op": "look_text",
        "text": "what does this export?",
        "id": "look1",
        "destination": "new",
        "target": target,
    })

    pending = engine.summon_pending()
    assert pending["origin"] == "look"
    assert pending["id"] == "look1"
    assert pending["text"] == "what does this export?"
    assert pending["target"] == target
    assert pending["destination"] == "new"
    assert pending["at"]
    snap = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert snap["summon_pending"] == pending

    # An empty question is still a cancel, never an empty turn.
    engine._on_overlay_event({"op": "look_text", "text": "  ", "id": "look2"})
    assert engine.summon_pending()["id"] == "look1"


def test_summon_text_without_text_writes_no_row(tmp_path: Path) -> None:
    engine.init(tmp_path)
    engine._summon_pending = None

    engine._on_overlay_event({"op": "summon_text", "id": "abc"})
    assert engine.summon_pending() is None
    engine._on_overlay_event({"op": "summon_text", "text": "   ", "id": "abc"})
    assert engine.summon_pending() is None
    assert not (tmp_path / "state.json").exists()


class _FakeOverlay:
    def __init__(self):
        self.running = False
        self.calls = []
        self.sprites = []
        self.on_event = None

    def start(self, show_pet=True):
        self.calls.append(("start", show_pet))
        self.running = True
        return True, "spawned"

    def claim_chip(self, enabled, sprite=None):
        self.calls.append(("claim_chip", enabled))
        self.sprites.append(sprite)

    def is_running(self):
        return self.running


def test_set_claim_chip_starts_a_petless_companion_and_toggles_the_chip(
        tmp_path: Path, monkeypatch) -> None:
    fake = _FakeOverlay()
    monkeypatch.setattr(engine, "_OVERLAY", fake)
    engine.init(tmp_path)

    result = engine.set_claim_chip(True)

    assert result["enabled"] is True
    assert result["running"] is True
    assert fake.calls == [("start", False), ("claim_chip", True)]

    result = engine.set_claim_chip(False)
    assert result["enabled"] is False
    assert fake.calls[-1] == ("claim_chip", False)


def test_set_claim_chip_forwards_the_live_sprite_to_the_companion(
        tmp_path: Path, monkeypatch) -> None:
    fake = _FakeOverlay()
    monkeypatch.setattr(engine, "_OVERLAY", fake)
    monkeypatch.setattr(engine.bp_overlay, "resolve_pet_sprite", lambda: (63, 69))
    engine.init(tmp_path)

    engine.set_claim_chip(True)

    assert fake.calls == [("start", False), ("claim_chip", True)]
    assert fake.sprites[-1] == (63, 69)


def test_nested_claim_key_wins_over_the_flat_spelling(tmp_path: Path) -> None:
    """Engine and dashboard must agree on precedence when both keys exist."""
    from dashboard import plugin_api

    engine.init(tmp_path)
    _write_config(tmp_path, {
        "summon": {"pet_click_claim": True},
        "summon.pet_click_claim": False,
    })

    assert engine.pet_click_claim() is True
    assert plugin_api._pet_click_claim({
        "summon": {"pet_click_claim": True},
        "summon.pet_click_claim": False,
    }) is True
