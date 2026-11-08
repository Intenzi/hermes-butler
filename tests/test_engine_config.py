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
