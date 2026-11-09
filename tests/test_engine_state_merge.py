"""Two processes write state.json: the guide half must never eat the click half.

The agent runtime owns `guide`, the plugin API (which owns the companion) owns
`summon_pending`. A write from either side has to merge the other's block, and
an ack has to clear only its own row.
"""
from __future__ import annotations

import json
from pathlib import Path

from butler_core import engine


def _reset() -> None:
    engine._session = None
    engine._summon_pending = None


def test_a_click_preserves_a_running_guide_and_the_ack_clears_only_the_row(
        tmp_path: Path) -> None:
    engine.init(tmp_path)
    _reset()
    (tmp_path / "state.json").write_text(
        json.dumps({
            "guide": {"id": "g1", "state": {"status": "running"}},
            "overlay": "running",
            "generated_at": 123.0,
        }),
        encoding="utf-8",
    )

    engine._on_overlay_event({"op": "claim_click"})

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["guide"]["id"] == "g1"
    assert state["overlay"] == "running"
    pending = state["summon_pending"]
    assert pending["origin"] == "pet"

    assert engine.consume_summon_pending(pending["id"]) is True

    after = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "summon_pending" not in after, "a consumed row must not be resurrected"
    assert after["guide"]["id"] == "g1", "the guide half survives the ack"


def test_a_guide_write_preserves_an_unconsumed_click_row(tmp_path: Path) -> None:
    engine.init(tmp_path)
    _reset()
    (tmp_path / "state.json").write_text(
        json.dumps({"summon_pending": {"origin": "pet", "id": "p1", "at": 1.0}}),
        encoding="utf-8",
    )

    engine._session = {"id": "g2", "overlay": "running", "state": {"status": "running"}}
    engine._write_state()

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["guide"]["id"] == "g2"
    assert state["summon_pending"]["id"] == "p1"
    _reset()
