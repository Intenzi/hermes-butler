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


def test_pet_click_claim_defaults_false(tmp_path: Path) -> None:
    engine.init(tmp_path)

    assert engine.pet_click_claim() is False


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

    _write_config(tmp_path, {"summon": {"pet_click_claim": "garbage"}})
    assert engine.pet_click_claim() is False


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


class _FakeOverlay:
    def __init__(self):
        self.running = False
        self.calls = []
        self.on_event = None

    def start(self, show_pet=True):
        self.calls.append(("start", show_pet))
        self.running = True
        return True, "spawned"

    def claim_chip(self, enabled):
        self.calls.append(("claim_chip", enabled))

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
