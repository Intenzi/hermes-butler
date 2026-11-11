"""WATCH: passive Accessibility monitoring for one app.

The companion owns the AX observers (single TCC subject, conventions §4); the
engine keeps a bounded, human-readable tail in memory + ``state.json`` and a
durable ``watch.jsonl``, announces a condition hit through the existing
``butler_say`` path, and never treats a missing app or grant as an exception.

These tests pin the Python half: the state merge, the wire op, the ring/state
shapes, one ``say`` per hit, the status text, the JSONL cap, and the tools.
The Swift side is asserted from source in ``test_overlay_pet.py``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butler_core import engine  # noqa: E402


class FakeOverlay:
    """The slice of ``Overlay`` the watch engine touches."""

    def __init__(self, replies=None, running=True):
        self.replies = list(replies or [])
        self.requests = []
        self.timeouts = []
        self.sends = []
        self.starts = []
        self.running = running
        self.is_running_calls = 0

    def start(self, show_pet=True):
        self.starts.append({"show_pet": show_pet})
        return True, "spawned pid 4242"

    def is_running(self):
        self.is_running_calls += 1
        return self.running

    def send(self, op):
        self.sends.append(op)

    def request(self, op, timeout=None):
        self.requests.append(op)
        self.timeouts.append(timeout)
        if not self.replies:
            return None
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _reset() -> None:
    engine._session = None
    engine._summon_pending = None
    engine._watch = None
    engine._watch_hit_id = None
    engine._watch_ring.clear()


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path):
    _reset()
    engine.init(tmp_path)
    yield
    _reset()


def _start(tmp_path: Path, monkeypatch, until=None, replies=None, once=True,
           scan=None):
    overlay = FakeOverlay(replies if replies is not None
                          else [{"op": "watch-started", "app": "Slack"}])
    monkeypatch.setattr(engine, "_OVERLAY", overlay)
    result = engine.start_watch("Slack", until=until, scan=scan, once=once)
    return overlay, result


def _scan_hit(**extra) -> dict:
    row = {"op": "watch-hit", "app": "Slack", "kind": "author",
           "label": "Adolan", "window": "general — Slack",
           "at": 1_794_330_012.0}
    row.update(extra)
    return row


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


def _event(seq: int, kind: str = "focus", **extra) -> dict:
    row = {
        "op": "watch-event",
        "seq": seq,
        "at": 1_794_330_000.0 + seq,
        "kind": kind,
        "app": "Slack",
        "role": "AXTextArea",
        "label": "message input",
        "window": "general — Slack",
    }
    row.update(extra)
    return row


# --------------------------------------------------------------- state merge

def test_watch_row_merges_with_the_guide_and_summon_blocks(
        tmp_path: Path, monkeypatch) -> None:
    engine._session = {"id": "g1", "overlay": "running",
                       "state": {"status": "running"}}
    engine._summon_pending = {"origin": "pet", "id": "p1", "at": 1.0}

    _overlay, result = _start(tmp_path, monkeypatch)

    assert result["ok"] is True
    state = _state(tmp_path)
    assert state["guide"]["id"] == "g1", "the watch write never erases a guide"
    assert state["summon_pending"]["id"] == "p1", "nor a pending chip click"
    assert state["watch"]["app"] == "Slack"
    assert state["watch"]["events"] == 0
    assert state["watch"]["hit"] is None


def test_a_guide_write_preserves_a_watch_row_it_does_not_own(tmp_path: Path) -> None:
    """Two processes share state.json: the guide half merges the watch block."""
    (tmp_path / "state.json").write_text(
        json.dumps({"watch": {"app": "Slack", "events": 3, "recent": []}}),
        encoding="utf-8",
    )
    engine._session = {"id": "g2", "overlay": "running",
                       "state": {"status": "running"}}

    engine._write_state()

    state = _state(tmp_path)
    assert state["guide"]["id"] == "g2"
    assert state["watch"]["app"] == "Slack"


# ------------------------------------------------------------------ the wire

def test_start_watch_sends_one_watch_start_op(tmp_path: Path, monkeypatch) -> None:
    overlay, result = _start(tmp_path, monkeypatch, until="Export complete")

    assert result["ok"] is True
    assert overlay.starts == [{"show_pet": False}], (
        "a passive watch must not park a pet double on the desktop")
    assert len(overlay.requests) == 1
    sent = overlay.requests[0]
    assert sent["op"] == "watch_start"
    assert sent["app"] == "Slack"
    assert sent["until"] == "Export complete"
    # The token is what lets a late line from a REPLACED watch be told apart.
    assert sent["token"]
    assert sent["token"] == engine._watch["token"]


def test_start_watch_omits_until_when_not_asked(tmp_path: Path, monkeypatch) -> None:
    overlay, _result = _start(tmp_path, monkeypatch)

    sent = overlay.requests[0]
    assert "until" not in sent, "no condition means no until KEY, never a null"
    assert set(sent) == {"op", "app", "token"}


def test_a_late_line_from_the_replaced_watch_is_dropped(
        tmp_path: Path, monkeypatch) -> None:
    """A queued stop/event from the OLD watch must not poison the new row."""
    overlay, _result = _start(tmp_path, monkeypatch)
    old_token = engine._watch["token"]
    overlay.replies.append({"op": "watch-started", "app": "Notes"})
    said = []
    monkeypatch.setattr(engine, "say", lambda text: said.append(text) or {"ok": True})

    assert engine.start_watch("Notes")["ok"] is True
    new_token = engine._watch["token"]
    assert new_token and new_token != old_token

    # Both lines were written by the watch that was just replaced.
    engine._on_overlay_event({"op": "watch-stopped", "token": old_token,
                              "events": 9, "reason": "app-quit"})
    engine._on_overlay_event({"op": "watch-event", "token": old_token,
                              "seq": 40, "kind": "value", "label": "old"})
    engine._on_overlay_event({"op": "watch-hit", "token": old_token,
                              "label": "old hit", "at": 5.0})

    assert engine._watch["stopped"] is None, "the new watch is not stopped"
    assert engine._watch["events"] == 0, "old events are not counted"
    assert engine._watch["hit"] is None, "an old hit does not announce"
    assert said == []

    # The current watch still flows.
    engine._on_overlay_event({"op": "watch-event", "token": new_token,
                              "seq": 1, "kind": "focus"})
    assert engine._watch["events"] == 1


def test_an_unknown_app_is_an_error_string_not_an_exception(
        tmp_path: Path, monkeypatch) -> None:
    overlay = FakeOverlay([{"watch_error": "app-not-running"}])
    monkeypatch.setattr(engine, "_OVERLAY", overlay)

    result = engine.start_watch("NoSuchApp")

    assert result["error"] == "app-not-running"
    assert "NoSuchApp" in result["note"]
    assert engine._watch is None
    sent = overlay.requests[0]
    assert sent["op"] == "watch_start"
    assert sent["app"] == "NoSuchApp"


def test_a_rejected_replacement_keeps_the_running_watch(
        tmp_path: Path, monkeypatch) -> None:
    """The companion validates the new app BEFORE dropping the old observers."""
    overlay, _result = _start(tmp_path, monkeypatch)
    engine._on_overlay_event(_event(1))
    assert engine._watch is not None

    overlay.replies.append({"watch_error": "app-not-running"})
    result = engine.start_watch("NoSuchApp")

    assert result["error"] == "app-not-running"
    assert engine._watch["app"] == "Slack", (
        "a rejected start must not kill the watch that is still running")
    assert engine._watch["events"] == 1
    assert _state(tmp_path)["watch"]["app"] == "Slack"


def test_a_missing_grant_names_the_companion_and_the_fix(
        tmp_path: Path, monkeypatch) -> None:
    overlay = FakeOverlay([{"watch_error": "app-not-trusted"}])
    monkeypatch.setattr(engine, "_OVERLAY", overlay)

    result = engine.start_watch("Slack")

    assert result["error"] == "app-not-trusted"
    assert "Butler companion" in result["fix"]


def test_a_transport_that_raises_still_answers(tmp_path: Path, monkeypatch) -> None:
    overlay = FakeOverlay([RuntimeError("pipe gone")])
    monkeypatch.setattr(engine, "_OVERLAY", overlay)

    result = engine.start_watch("Slack")

    assert "pipe gone" in result["error"]
    assert engine._watch is None


def test_a_stale_reply_token_cannot_answer_a_later_start(
        tmp_path: Path, monkeypatch) -> None:
    """A timed-out start's late reply must not decide a newer start."""
    overlay, _result = _start(tmp_path, monkeypatch)   # Slack, token A
    overlay.replies.append({"op": "watch-started", "app": "Notes",
                            "token": "an-older-start"})

    result = engine.start_watch("Notes")

    assert result["error"] == "stale watch reply"
    assert engine._watch["app"] == "Slack", "the running watch is restored"


def test_a_tokened_line_never_lands_on_a_tokenless_row(tmp_path: Path) -> None:
    """A legacy row (no token) still accepts legacy tokenless lines only."""
    engine._watch = {"app": "Slack", "until": None, "token": None,
                     "started_at": 1.0, "events": 0, "hit": None,
                     "stopped": None, "recent": []}

    engine._on_overlay_event({"op": "watch-event", "token": "foreign",
                              "seq": 1, "kind": "focus"})
    assert engine._watch["events"] == 0

    engine._on_overlay_event({"op": "watch-event", "seq": 2, "kind": "focus"})
    assert engine._watch["events"] == 1, "a tokenless legacy line is accepted"


# ------------------------------------------------------- events, ring, state

def test_events_fill_the_ring_and_the_state_tail(tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch)

    for seq in range(1, 16):
        engine._on_overlay_event(_event(seq, label="message %d" % seq))

    assert engine._watch["events"] == 15
    assert len(engine._watch["recent"]) == engine.WATCH_STATE_RECENT, (
        "state.json carries only the newest 12 events")
    assert engine._watch["recent"][-1]["label"] == "message 15"
    assert len(engine._watch_ring) == 15

    state = _state(tmp_path)
    assert state["watch"]["events"] == 15
    assert len(state["watch"]["recent"]) == 12
    assert state["watch"]["recent"][0]["label"] == "message 4"
    assert engine.summon_pending() is None, "a watch event is not a summon"


def test_the_in_memory_ring_is_capped_at_forty(tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch)

    for seq in range(1, 46):
        engine._on_overlay_event(_event(seq))

    assert len(engine._watch_ring) == engine.WATCH_RING == 40
    assert engine._watch_ring[-1]["seq"] == 45
    assert engine._watch_ring[0]["seq"] == 6


def test_events_for_a_stopped_watch_are_dropped(tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch)
    engine.stop_watch()

    engine._on_overlay_event(_event(1))

    assert engine._watch is None
    assert engine._watch_ring == []


# ------------------------------------------------------------ condition hit

def test_watch_hit_calls_say_exactly_once(tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch, until="Export complete")
    said = []
    monkeypatch.setattr(engine, "say", lambda text: said.append(text) or {"ok": True})

    hit = {"op": "watch-hit", "app": "Slack", "until": "Export complete",
           "label": "Export complete", "window": "general — Slack",
           "at": 1_794_330_012.0}
    engine._on_overlay_event(hit)
    engine._on_overlay_event(hit)  # a duplicate line must not speak twice

    assert said == ["Slack: Export complete appeared"]
    assert engine._watch["hit"]["label"] == "Export complete"
    state = _state(tmp_path)
    assert state["watch"]["hit"]["label"] == "Export complete"


def test_a_hit_without_a_condition_still_records(
        tmp_path: Path, monkeypatch) -> None:
    """The companion only emits watch-hit for a set condition; the engine
    records what it can read instead of inventing a label."""
    engine._watch = {"app": "Slack", "until": None, "started_at": 1.0,
                     "events": 0, "hit": None, "stopped": None, "recent": []}
    said = []
    monkeypatch.setattr(engine, "say",
                        lambda text: said.append(text) or {"ok": True})

    engine._on_overlay_event({"op": "watch-hit", "app": "Slack", "at": 2.0})

    assert said == ["Slack:  appeared"]
    assert engine._watch["hit"]["label"] == ""


# ----------------------------------------------------- hit lifecycle (round 20)

def test_a_hit_ends_a_one_shot_watch_by_default(
        tmp_path: Path, monkeypatch) -> None:
    """The request was "tell me when X appears"; X appeared, so the watch ends.

    One announcement, a terminal ``stopped.reason == "hit"`` row, one
    ``watch_stop`` to the companion, and no second announcement — ever.
    """
    overlay, result = _start(tmp_path, monkeypatch, until="Export complete")
    assert result["once"] is True
    assert engine._watch["once"] is True
    said = []
    monkeypatch.setattr(engine, "say", lambda text: said.append(text) or {"ok": True})
    engine._on_overlay_event(_event(1))
    hit = {"op": "watch-hit", "app": "Slack", "until": "Export complete",
           "label": "Export complete", "window": "general — Slack",
           "at": 1_794_330_012.0}

    engine._on_overlay_event(hit)

    assert said == ["Slack: Export complete appeared"], "announced exactly once"
    assert engine._watch["hit"]["label"] == "Export complete"
    assert engine._watch["stopped"]["reason"] == "hit"
    assert engine._watch["stopped"]["events"] == 1
    assert overlay.sends == [{"op": "watch_stop"}], (
        "the engine detaches the observers itself; no Stop hunting")
    state = _state(tmp_path)
    assert state["watch"]["stopped"]["reason"] == "hit"
    assert state["watch"]["once"] is True

    # The companion's answer to OUR watch_stop must not rewrite the reason.
    engine._on_overlay_event({"op": "watch-stopped",
                              "token": engine._watch["token"],
                              "events": 1, "reason": "stopped"})
    assert engine._watch["stopped"]["reason"] == "hit"

    # A second event and a duplicate hit line cannot re-open or re-say it.
    engine._on_overlay_event(_event(2))
    engine._on_overlay_event(hit)
    assert engine._watch["events"] == 1, "a stopped watch stays inert"
    assert said == ["Slack: Export complete appeared"]
    assert overlay.sends == [{"op": "watch_stop"}]


def test_the_one_shot_row_is_published_before_the_stop_is_sent(
        tmp_path: Path, monkeypatch) -> None:
    """A /state poll must never see `hit` without its terminal `stopped`.

    Also: the companion is told OUTSIDE ``_watch_lock``. A blocking request
    transport (and a synchronous test double) under the lock would stall or
    re-enter the row it is mutating.
    """
    overlay, _result = _start(tmp_path, monkeypatch, until="Export complete")
    seen = []

    def send(op):
        overlay.sends.append(op)
        seen.append({
            "locked": engine._watch_lock._is_owned(),
            "reason": _state(tmp_path)["watch"]["stopped"]["reason"],
        })

    overlay.send = send
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})

    engine._on_overlay_event({"op": "watch-hit", "app": "Slack",
                              "label": "Export complete", "at": 5.0})

    assert overlay.sends == [{"op": "watch_stop"}]
    assert seen == [{"locked": False, "reason": "hit"}]


def test_an_empty_or_unknown_once_spelling_stays_one_shot(
        tmp_path: Path, monkeypatch) -> None:
    """Sticky is the opt-in: a blank/unknown flag must not select it silently."""
    overlay = FakeOverlay([{"op": "watch-started", "app": "Slack"}])
    monkeypatch.setattr(engine, "_OVERLAY", overlay)

    result = engine.start_watch("Slack", until="Export complete", once="")

    assert result["once"] is True
    assert engine._watch["once"] is True

    overlay.replies.append({"op": "watch-started", "app": "Slack"})
    sticky = engine.start_watch("Slack", until="Export complete", once="false")

    assert sticky["once"] is False
    assert engine._watch["once"] is False


def test_a_sticky_hit_keeps_observing_and_never_reannounces(
        tmp_path: Path, monkeypatch) -> None:
    overlay, _result = _start(tmp_path, monkeypatch, until="Export complete",
                              once=False)
    assert engine._watch["once"] is False
    said = []
    monkeypatch.setattr(engine, "say", lambda text: said.append(text) or {"ok": True})
    hit = {"op": "watch-hit", "app": "Slack", "label": "Export complete",
           "window": "general — Slack", "at": 1_794_330_012.0}

    engine._on_overlay_event(hit)

    assert said == ["Slack: Export complete appeared"]
    assert engine._watch["stopped"] is None, "sticky keeps watching"
    assert engine._watch["hit"]["acknowledged"] is False
    assert overlay.sends == [], "no stop request in sticky mode"
    assert _state(tmp_path)["watch"]["hit"]["acknowledged"] is False

    # Further matches and changes still flow, but the hit speaks only once.
    engine._on_overlay_event(_event(1))
    engine._on_overlay_event(dict(hit, at=1_794_330_020.0))
    assert engine._watch["events"] == 1
    assert engine._watch["stopped"] is None
    assert said == ["Slack: Export complete appeared"]


def test_watch_ack_clears_the_hit_and_keeps_the_watch(
        tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch, until="Export complete", once=False)
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    engine._on_overlay_event({"op": "watch-hit", "app": "Slack",
                              "label": "Export complete", "at": 5.0})

    result = engine.acknowledge_watch_hit()

    assert result == {"status": "acknowledged", "acknowledged": True,
                      "app": "Slack", "events": 0}
    assert engine._watch["hit"] is None, "the hit is cleared, not remembered"
    assert engine._watch["stopped"] is None, "the watch is still live"
    state = _state(tmp_path)
    assert state["watch"]["hit"] is None
    assert "watch_hit" not in state, "the announcement is gone too"
    # The watch still observes after the acknowledgement.
    engine._on_overlay_event(_event(1))
    assert engine._watch["events"] == 1
    # A second ack is an honest false, not an exception.
    again = engine.acknowledge_watch_hit()
    assert again["acknowledged"] is False
    assert again["status"] == "no-hit"


# ------------------------------------- the hit outlives the watch (round 21)


def _hit(when: float = 1_794_330_012.0) -> dict:
    return {"op": "watch-hit", "app": "Slack", "until": "Export complete",
            "label": "Export complete", "window": "general — Slack",
            "at": when}


def test_a_hit_writes_a_top_level_watch_hit_that_outlives_the_watch(
        tmp_path: Path, monkeypatch) -> None:
    """The announcement is its own row: a one-shot watch stops, the row stays."""
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})

    engine._on_overlay_event(_hit())

    state = _state(tmp_path)
    block = state["watch_hit"]
    assert block["app"] == "Slack"
    assert block["label"] == "Export complete"
    assert block["window"] == "general — Slack"
    assert block["at"] == 1_794_330_012.0
    assert block["acknowledged"] is False
    assert re.fullmatch(r"[0-9a-f]{8}", block["id"]), "8 hex id"
    assert state["watch"]["stopped"]["reason"] == "hit", "the watch still ends"

    # The desktop/pane clearing the ended watch row must not take the
    # announcement with it.
    engine.stop_watch()
    state = _state(tmp_path)
    assert "watch" not in state
    assert state["watch_hit"] == block, "same row, same id: it survived"


def test_the_outliving_hit_merges_with_the_other_state_blocks(
        tmp_path: Path, monkeypatch) -> None:
    """A later guide/watch write carries the hit row forward, never clobbers it."""
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    engine._on_overlay_event(_hit())
    block = _state(tmp_path)["watch_hit"]

    engine._session = {
        "id": "g1", "overlay": "ringing",
        "state": {"status": "running", "idx": 0},
        "steps": [{"say": "Click Export"}], "verifications": [],
    }
    engine._write_state()

    state = _state(tmp_path)
    assert state["watch_hit"] == block
    assert state["guide"]["id"] == "g1"
    assert state["watch"]["stopped"]["reason"] == "hit"


def test_a_sticky_hit_also_writes_the_top_level_announcement(
        tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch, until="Export complete", once=False)
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})

    engine._on_overlay_event(_hit())

    state = _state(tmp_path)
    assert state["watch_hit"]["acknowledged"] is False
    assert state["watch"]["hit"]["acknowledged"] is False
    assert state["watch"]["stopped"] is None, "sticky keeps watching"


def test_a_second_arm_replaces_the_announcement_id(
        tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    engine._on_overlay_event(_hit())
    first = _state(tmp_path)["watch_hit"]["id"]

    overlay, _result = _start(tmp_path, monkeypatch, until="Done")
    overlay.replies = [{"op": "watch-started", "app": "Slack"}]
    engine._on_overlay_event(dict(_hit(1_794_330_099.0), label="Done"))

    second = _state(tmp_path)["watch_hit"]["id"]
    assert second != first, "a fresh hit is a fresh announcement"
    assert _state(tmp_path)["watch_hit"]["label"] == "Done"


def test_ack_clears_the_announcement_when_no_watch_runs(
        tmp_path: Path, monkeypatch) -> None:
    """The normal case after a one-shot hit: the watch row is already gone."""
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    engine._on_overlay_event(_hit())
    engine.stop_watch()
    assert "watch" not in _state(tmp_path)
    assert _state(tmp_path)["watch_hit"]["acknowledged"] is False

    result = engine.acknowledge_watch_hit()

    assert result["acknowledged"] is True
    assert result["status"] == "acknowledged"
    assert result["app"] == "Slack"
    assert "watch_hit" not in _state(tmp_path)
    # A second ack is a no-op that never raises.
    again = engine.acknowledge_watch_hit()
    assert again == {"status": "idle", "acknowledged": False}
    assert "watch_hit" not in _state(tmp_path)


def test_ack_publishes_acknowledged_before_removing_the_block(
        tmp_path: Path, monkeypatch) -> None:
    """A poll between the two writes can never see an unacked hit."""
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    engine._on_overlay_event(_hit())
    engine.stop_watch()
    seen = []
    real = engine._write_state

    def recording(*args, **kwargs):
        real(*args, **kwargs)
        seen.append((_state(tmp_path) or {}).get("watch_hit"))

    monkeypatch.setattr(engine, "_write_state", recording)

    assert engine.acknowledge_watch_hit()["acknowledged"] is True

    assert seen, "the ack wrote the shared snapshot"
    assert seen[0]["acknowledged"] is True, "marked before removed"
    assert seen[-1] is None, "then the block is gone"


def test_ack_works_with_no_watch_and_no_file_row(
        tmp_path: Path, monkeypatch) -> None:
    """An ack on an idle engine is honest, never an exception."""
    _reset()
    engine.init(tmp_path)

    assert engine.acknowledge_watch_hit() == {"status": "idle",
                                              "acknowledged": False}


def test_an_ack_in_another_process_is_reconciled_by_the_owner(
        tmp_path: Path, monkeypatch) -> None:
    """A sticky row acked elsewhere must not leave a stale hit (or swallow hits)."""
    _start(tmp_path, monkeypatch, until="Export complete", once=False)
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    engine._on_overlay_event(_hit())
    assert engine._watch["hit"] is not None
    # Another process acknowledges: the top-level block disappears from the
    # shared file, while this process still holds row.hit in memory.
    path = tmp_path / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    del state["watch_hit"]
    path.write_text(json.dumps(state), encoding="utf-8")

    # The owner's next write reconciles the row.
    engine._on_overlay_event(_event(1))

    assert engine._watch["hit"] is None, "the stale sticky hit is dropped"
    assert _state(tmp_path)["watch"]["hit"] is None
    assert "watch_hit" not in _state(tmp_path)
    # The duplicate guard is re-armed: the next occurrence is announced again.
    said = []
    monkeypatch.setattr(engine, "say",
                        lambda text: said.append(text) or {"ok": True})
    engine._on_overlay_event(dict(_hit(1_794_330_099.0), label="Second"))
    assert said == ["Slack: Second appeared"]
    assert _state(tmp_path)["watch_hit"]["label"] == "Second"


def test_ack_acknowledges_the_block_but_leaves_a_live_foreign_sticky_hit(
        tmp_path: Path) -> None:
    """The other process owns the row: ack the announcement, report honestly."""
    _reset()
    engine.init(tmp_path)
    row = {"app": "Slack", "once": False, "owner_pid": 1, "events": 3,
           "hit": {"label": "Export complete", "at": 5.0,
                   "acknowledged": False},
           "stopped": None, "recent": []}
    block = {"app": "Slack", "label": "Export complete", "window": None,
             "at": 5.0, "acknowledged": False, "id": "a1b2c3d4"}
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"watch": row, "watch_hit": block}),
                    encoding="utf-8")

    first = engine.acknowledge_watch_hit()

    assert first["acknowledged"] is True
    state = json.loads(path.read_text(encoding="utf-8"))
    assert "watch_hit" not in state
    assert state["watch"]["hit"] is not None, (
        "the live owner reconciles its own row; this process must not lie about it")

    # With the announcement gone, the legacy sticky row cannot be acked here:
    # a second ack is an honest false and writes nothing.
    second = engine.acknowledge_watch_hit()
    assert second == {"status": "elsewhere", "acknowledged": False,
                      "app": "Slack", "owner_pid": 1,
                      "note": "another butler process owns this watch; "
                              "acknowledge it from that process"}


def test_watch_ack_is_false_when_the_one_shot_hit_was_already_acked(
        tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    engine._on_overlay_event(_hit())

    assert engine.acknowledge_watch_hit()["acknowledged"] is True
    assert engine._watch["stopped"]["reason"] == "hit", "the ending still stands"
    again = engine.acknowledge_watch_hit()
    assert again["acknowledged"] is False
    assert again["status"] == "stopped"


# ------------------------------------------------------------------ status

def test_watch_status_has_the_header_and_ten_event_lines(
        tmp_path: Path, monkeypatch) -> None:
    stamp = 1_794_330_000.42
    _start(tmp_path, monkeypatch, until="Export complete")
    for seq in range(1, 13):
        engine._on_overlay_event(_event(seq, at=stamp + seq))

    text = engine.watch_status()
    lines = text.splitlines()

    assert lines[0] == "watching Slack for Export complete · 12 events · no hit"
    assert len(lines) == 1 + 10, "header plus the last 10 events"
    first_clock = time.strftime("%H:%M:%S", time.localtime(stamp + 3))
    assert lines[1] == (
        '%s focus AXTextArea "message input" — general — Slack' % first_clock)
    assert re.match(r"^\d\d:\d\d:\d\d focus AXTextArea ", lines[1])


def test_watch_status_reports_the_hit(tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    engine._on_overlay_event({
        "op": "watch-hit", "app": "Slack", "until": "Export complete",
        "label": "Export complete", "window": "general — Slack",
        "at": 1_794_330_012.0,
    })

    lines = engine.watch_status().splitlines()

    assert lines[0].startswith("watching Slack for Export complete · 0 events · hit")
    assert lines[1].startswith("hit: Export complete — general — Slack at ")


def test_watch_status_without_a_watch_says_so() -> None:
    assert engine.watch_status() == "no watch active"


def test_a_stopped_watch_keeps_the_reason_visible(
        tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch)
    engine._on_overlay_event({"op": "watch-stopped", "events": 4,
                              "reason": "app-quit"})

    lines = engine.watch_status().splitlines()

    assert lines[-1] == "stopped: app-quit"
    stopped = _state(tmp_path)["watch"]["stopped"]
    assert stopped["reason"] == "app-quit"
    # The desktop dedupes its one "Watch ended" line on (app, at, reason), so a
    # stop line without a timestamp would collapse two endings of the same app.
    assert isinstance(stopped["at"], float)


# -------------------------------------------------------------- stop_watch

def test_stop_watch_sends_watch_stop_and_clears_the_row(
        tmp_path: Path, monkeypatch) -> None:
    overlay, _result = _start(tmp_path, monkeypatch)
    engine._session = {"id": "g1", "overlay": "running",
                       "state": {"status": "running"}}
    engine._summon_pending = {"origin": "pet", "id": "p1", "at": 1.0}
    engine._on_overlay_event(_event(1))

    result = engine.stop_watch()

    assert result["status"] == "stopped"
    assert result["app"] == "Slack"
    assert result["events"] == 1
    # Fire-and-forget: the companion's `watch-stopped` reply rides on_event,
    # so waiting on request() would only burn its timeout.
    assert overlay.sends == [{"op": "watch_stop"}]
    assert engine._watch is None
    state = _state(tmp_path)
    assert "watch" not in state, "the state row is cleared, not merged"
    assert state["guide"]["id"] == "g1", "clearing the watch keeps the guide"
    assert state["summon_pending"]["id"] == "p1", "and the pending click"


def test_stop_watch_when_idle_sends_nothing(tmp_path: Path, monkeypatch) -> None:
    overlay = FakeOverlay()
    monkeypatch.setattr(engine, "_OVERLAY", overlay)

    assert engine.stop_watch() == {"status": "idle"}
    assert overlay.requests == []
    assert overlay.is_running_calls == 0


# ------------------------------------------------------------- watch.jsonl

def test_watch_jsonl_is_a_durable_tail_capped_at_500(tmp_path: Path) -> None:
    for seq in range(1, 521):
        engine._append_watch_jsonl({"seq": seq, "kind": "focus"})

    lines = (tmp_path / "watch.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == engine.WATCH_JSONL_MAX == 500
    assert json.loads(lines[0])["seq"] == 21, "only the oldest lines are dropped"
    assert json.loads(lines[-1])["seq"] == 520


def test_a_stopped_watch_ignores_stragglers(tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch, until="Export complete")
    said = []
    monkeypatch.setattr(engine, "say", lambda text: said.append(text) or {"ok": True})
    engine._on_overlay_event({"op": "watch-stopped", "events": 0,
                              "reason": "app-quit"})

    engine._on_overlay_event(_event(99))
    engine._on_overlay_event({"op": "watch-hit", "label": "Export complete"})

    assert engine._watch["events"] == 0, "a stopped watch stays inert"
    assert engine._watch["hit"] is None
    assert said == []


def _dead_pid() -> int:
    """A pid that certainly is not running anymore (an exited child)."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def test_status_reads_a_row_published_by_another_process(
        tmp_path: Path) -> None:
    """A gateway restart must not turn a real watch into "no watch active"."""
    (tmp_path / "state.json").write_text(json.dumps({"watch": {
        "app": "Slack", "until": "Export complete", "owner_pid": _dead_pid(),
        "events": 1, "hit": None, "stopped": None,
        "recent": [{"at": 1_794_330_000.0, "kind": "focus",
                    "role": "AXTextArea", "label": "message input",
                    "window": "general — Slack"}],
    }}), encoding="utf-8")

    text = engine.watch_status()
    lines = text.splitlines()

    assert lines[0] == "watching Slack for Export complete · 1 events · no hit"
    assert lines[1].endswith('focus AXTextArea "message input" — general — Slack')
    assert lines[-1].startswith("note: stale row from a process that exited")


def test_stop_watch_clears_a_stale_row_from_a_dead_process(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(json.dumps({"watch": {
        "app": "Slack", "owner_pid": _dead_pid(), "events": 2,
        "hit": None, "stopped": None, "recent": [],
    }}), encoding="utf-8")

    result = engine.stop_watch()

    assert result["status"] == "cleared"
    assert result["app"] == "Slack"
    assert "watch" not in _state(tmp_path)


def test_stop_watch_leaves_a_live_foreign_watch_alone(tmp_path: Path) -> None:
    """Another plugin host owns the row: do not clear what it is still writing."""
    (tmp_path / "state.json").write_text(json.dumps({"watch": {
        "app": "Slack", "owner_pid": os.getppid(), "events": 2,
        "hit": None, "stopped": None, "recent": [],
    }}), encoding="utf-8")

    result = engine.stop_watch()

    assert result["status"] == "elsewhere"
    assert result["owner_pid"] == os.getppid()
    assert _state(tmp_path)["watch"]["app"] == "Slack"


# -------------------------------------------------------------------- tools

_SPEC = importlib.util.spec_from_file_location(
    "butler_plugin_watch_under_test", ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
plugin = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin
_SPEC.loader.exec_module(plugin)


class RegisterCtx:
    def __init__(self, tmp_path: Path):
        self._data = tmp_path
        self.tools = []

    def plugin_data_dir(self):
        return str(self._data)

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_command(self, *args, **kwargs):
        pass

    def register_cli_command(self, **kwargs):
        pass

    def register_skill(self, **kwargs):
        pass


def _registered(tmp_path: Path) -> dict:
    ctx = RegisterCtx(tmp_path)
    plugin.register(ctx)
    return {row["name"]: row for row in ctx.tools}


def test_register_installs_the_four_watch_tools(tmp_path: Path) -> None:
    tools = _registered(tmp_path)

    assert tools["butler_watch_start"]["schema"]["required"] == ["app"]
    assert tools["butler_watch_start"]["schema"]["properties"]["until"]["type"] == "string"
    assert tools["butler_watch_start"]["schema"]["properties"]["once"]["type"] == "boolean"
    assert tools["butler_watch_start"]["schema"]["properties"]["once"]["default"] is True
    assert tools["butler_watch_status"]["schema"]["properties"] == {}
    assert tools["butler_watch_stop"]["schema"]["properties"] == {}
    assert tools["butler_watch_ack"]["schema"]["properties"] == {}
    for name in ("butler_watch_start", "butler_watch_status",
                 "butler_watch_stop", "butler_watch_ack"):
        assert tools[name]["toolset"] == "butler"


def test_the_watch_tools_answer_even_without_a_companion(tmp_path: Path) -> None:
    tools = _registered(tmp_path)

    assert json.loads(tools["butler_watch_status"]["handler"]({})) == {
        "status": "no watch active"}
    assert json.loads(tools["butler_watch_stop"]["handler"]({})) == {
        "status": "idle"}
    assert json.loads(tools["butler_watch_ack"]["handler"]({})) == {
        "status": "idle", "acknowledged": False}
    assert json.loads(tools["butler_watch_start"]["handler"]({})) == {
        "error": "app is required"}


def test_a_watch_request_gets_its_own_frame() -> None:
    """"watch <app> for …" must not be framed as a guide.

    Measured: the Option-right-click menu prefills exactly that sentence and it
    went through the summon frame, which orders a follow-mode guide — the agent
    was told to do the opposite of what the user asked.
    """
    from butler_core.summon import frame_for

    framed = frame_for("watch Preview for Export complete", "")
    assert framed.startswith("[butler watch] watch Preview for Export complete")
    assert "butler_watch_start" in framed
    assert "[butler summon]" not in framed
    assert "butler_guide_start" not in framed

    # An explicit origin wins even without the prefix.
    assert frame_for("Preview, tell me when the export lands", "watch").startswith("[butler watch]")
    # Ordinary requests keep the guidance frame.
    assert frame_for("open wifi settings", "").startswith("[butler summon]")


def test_both_other_frames_know_watch_exists() -> None:
    from butler_core.summon import frame_for

    summon = frame_for("tell me when the build finishes", "")
    assert "butler_watch_start" in summon

    look = frame_for("[butler look] App: Preview\nQuestion: is it done?", "look")
    assert "butler_watch_start" in look


# ------------------------------------------------ content scan (round 26)
#
# `scan` is the content watch: it re-walks the app and compares match
# signatures with the arm-time baseline. The author matcher is role-filtered by
# the companion, so a message BODY that merely contains the name cannot fire;
# a text matcher is the explicitly weaker mode and is reported as "text".

def test_a_scan_alone_arms_and_reaches_the_companion(
        tmp_path: Path, monkeypatch) -> None:
    overlay, result = _start(
        tmp_path, monkeypatch,
        replies=[{"op": "watch-started", "app": "Slack", "baseline": 4}],
        scan={"author": "Adolan"})

    assert result["ok"] is True
    sent = overlay.requests[0]
    assert sent["op"] == "watch_start"
    assert "until" not in sent, "one matcher per watch"
    assert sent["scan"] == {"author": "Adolan",
                            "roles": ["AXImage", "AXButton", "AXLink"],
                            "interval": 3, "max_nodes": 1500}
    assert engine._watch["scan"]["author"] == "Adolan"
    assert engine._watch["scan_baseline"] == 4, "the arm-time count is kept"
    assert isinstance(engine._watch["scan_baseline_at"], float)
    assert engine._watch["scan_ticks"] == 0
    assert _state(tmp_path)["watch"]["scan"]["author"] == "Adolan"
    # The arm-time baseline walk happens BEFORE the ack, so a scan start gets
    # the longer round-trip budget instead of the ordinary 5 s one.
    assert overlay.timeouts == [engine.WATCH_SCAN_REQUEST_TIMEOUT]
    assert engine.WATCH_SCAN_REQUEST_TIMEOUT > engine.WATCH_REQUEST_TIMEOUT


def test_until_and_scan_together_are_refused(
        tmp_path: Path, monkeypatch) -> None:
    overlay = FakeOverlay()
    monkeypatch.setattr(engine, "_OVERLAY", overlay)

    result = engine.start_watch("Slack", until="Export complete",
                                scan={"author": "Adolan"})

    assert result == {"error": "pass until or scan, not both"}
    assert overlay.requests == [], "never reaches the companion"
    assert overlay.starts == []
    assert engine._watch is None


@pytest.mark.parametrize("scan", [
    {},
    {"roles": ["AXImage"]},
    {"author": ""},
    {"author": "   "},
    {"author": "Adolan", "text": "invoice"},
])
def test_an_empty_or_double_matcher_scan_is_refused(
        scan, tmp_path: Path, monkeypatch) -> None:
    overlay = FakeOverlay()
    monkeypatch.setattr(engine, "_OVERLAY", overlay)

    result = engine.start_watch("Slack", scan=scan)

    assert "error" in result
    assert overlay.requests == []
    assert overlay.starts == []
    assert engine._watch is None


def test_scan_interval_and_node_budget_are_clamped(
        tmp_path: Path, monkeypatch) -> None:
    overlay, result = _start(
        tmp_path, monkeypatch,
        replies=[{"op": "watch-started", "app": "Slack", "baseline": 0}],
        scan={"author": "Adolan", "interval_seconds": 1, "max_nodes": 99_999,
              "roles": ["AXImage"]})

    assert result["ok"] is True
    sent = overlay.requests[0]["scan"]
    assert sent["interval"] == 2, "floor is 2s"
    assert sent["max_nodes"] == 4000, "cap is 4000 nodes"
    assert sent["roles"] == ["AXImage"], "explicit roles win over the default"

    overlay = FakeOverlay([{"op": "watch-started", "app": "Slack"}])
    monkeypatch.setattr(engine, "_OVERLAY", overlay)
    engine.start_watch("Slack", scan={"author": "Adolan",
                                      "interval_seconds": 30})
    assert overlay.requests[0]["scan"]["interval"] == 10, "ceiling is 10s"
    assert overlay.requests[0]["scan"]["max_nodes"] == 1500, "default budget"


def test_an_author_hit_announces_once_and_ends_the_one_shot_watch(
        tmp_path: Path, monkeypatch) -> None:
    overlay, _result = _start(
        tmp_path, monkeypatch,
        replies=[{"op": "watch-started", "app": "Slack", "baseline": 4}],
        scan={"author": "Adolan"})
    said = []
    monkeypatch.setattr(engine, "say",
                        lambda text: said.append(text) or {"ok": True})

    engine._on_overlay_event(_scan_hit())

    assert said == ["Slack: Adolan appeared"], "announced exactly once"
    assert engine._watch["hit"]["kind"] == "author"
    assert engine._watch["hit"]["label"] == "Adolan"
    assert engine._watch["stopped"]["reason"] == "hit", "one-shot semantics"
    assert overlay.sends == [{"op": "watch_stop"}]
    state = _state(tmp_path)
    assert state["watch_hit"]["kind"] == "author"
    assert state["watch_hit"]["label"] == "Adolan"

    # A duplicate hit line cannot announce twice.
    engine._on_overlay_event(_scan_hit())
    assert said == ["Slack: Adolan appeared"]


def test_a_text_scan_hit_is_reported_as_text(
        tmp_path: Path, monkeypatch) -> None:
    overlay, _result = _start(
        tmp_path, monkeypatch,
        replies=[{"op": "watch-started", "app": "Slack", "baseline": 0}],
        scan={"text": "invoice"})
    said = []
    monkeypatch.setattr(engine, "say",
                        lambda text: said.append(text) or {"ok": True})

    engine._on_overlay_event(_scan_hit(kind="text", label="new invoice ready"))

    assert said == ["Slack: new invoice ready appeared"]
    assert engine._watch["hit"]["kind"] == "text", (
        "a text match is never dressed up as an author")
    assert _state(tmp_path)["watch_hit"]["kind"] == "text"
    lines = engine.watch_status().splitlines()
    assert lines[1] == (
        'scan: text "invoice" · roles any · baseline 0 · 0 ticks · hit')


def test_a_scan_tick_updates_the_counters_without_inflating_events(
        tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch,
           replies=[{"op": "watch-started", "app": "Slack", "baseline": 4}],
           scan={"author": "Adolan"})

    engine._on_overlay_event({"op": "watch-scan", "baseline": 4,
                              "matches": 7, "new": 1,
                              "at": 1_794_330_100.0})

    assert engine._watch["scan_ticks"] == 1
    assert engine._watch["scan"]["matches"] == 7
    assert engine._watch["scan"]["new"] == 1
    assert engine._watch["scan_baseline"] == 4
    assert engine._watch["events"] == 0, "a tick is accounting, not an event"
    assert engine._watch_ring == [], "the ring stays a change log"
    assert _state(tmp_path)["watch"]["scan_ticks"] == 1


def test_status_shows_the_scan_line(tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch,
           replies=[{"op": "watch-started", "app": "Slack", "baseline": 4}],
           scan={"author": "Adolan"})

    lines = engine.watch_status().splitlines()

    assert lines[0] == "watching Slack for content scan · 0 events · no hit"
    assert lines[1] == (
        'scan: author "Adolan" · roles AXImage,AXButton,AXLink '
        '· baseline 4 · 0 ticks · no hit')


def test_the_watch_start_schema_offers_the_scan_object(tmp_path: Path) -> None:
    schema = _registered(tmp_path)["butler_watch_start"]["schema"]
    scan = schema["properties"]["scan"]

    assert scan["type"] == "object"
    assert {"author", "text", "roles", "interval_seconds",
            "max_nodes"} <= set(scan["properties"])
    assert scan["properties"]["interval_seconds"]["default"] == 3
    assert scan["properties"]["max_nodes"]["default"] == 1500


def test_a_scan_line_cannot_invent_a_scan_on_an_until_watch(
        tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch, until="Export complete")

    engine._on_overlay_event({"op": "watch-scan", "baseline": 4,
                              "matches": 4, "new": 0})

    assert engine._watch["scan"] is None
    assert "scan_ticks" not in engine._watch
    assert "scan:" not in engine.watch_status()


def test_status_says_baseline_unknown_until_the_ack_lands(
        tmp_path: Path) -> None:
    """Before the companion acks, the arm-time count is unknown, not zero.

    "baseline 0" is a real claim (the app exposes no author-role markup);
    printing it while the baseline walk is still in flight would be a lie.
    """
    (tmp_path / "state.json").write_text(json.dumps({
        "watch": {
            "app": "Discord",
            "until": None,
            "scan": {"author": "Adolan",
                     "roles": ["AXImage", "AXButton", "AXLink"],
                     "interval": 3, "max_nodes": 1500},
            "once": True,
            "events": 0, "hit": None, "stopped": None, "recent": [],
            "scan_baseline": 0, "scan_baseline_at": None, "scan_ticks": 0,
        },
    }), encoding="utf-8")

    line = engine.watch_status().splitlines()[1]

    assert line == ('scan: author "Adolan" · roles AXImage,AXButton,AXLink '
                    '· baseline ? · 0 ticks · no hit')


# ------------------------------------- WHICH surface matched (round 15)
#
# A hit used to be a bare "something contained the needle". A Terminal's
# window title IS its command line, so `until: "BUILD OK"` matched the typing
# of `echo BUILD OK` immediately, consumed the one-shot hit, and nothing was
# watching when the output actually landed. The companion now records which
# surfaces already contained the needle at arm (`baseline_had`) and only fires
# those on a transition; the hit says WHICH surface fired (`matched`).

def _terminal(tmp_path: Path, monkeypatch, until="BUILD OK"):
    overlay = FakeOverlay([{
        "op": "watch-started", "app": "Terminal", "until": until,
        "baseline_had": ["window"], "window_title_disabled": True,
    }])
    monkeypatch.setattr(engine, "_OVERLAY", overlay)
    return overlay, engine.start_watch("Terminal", until=until)


def test_the_arm_reply_records_the_preexisting_surfaces(
        tmp_path: Path, monkeypatch) -> None:
    """`baseline_had` names the trap, and the status says why it is waiting."""
    _overlay, result = _terminal(tmp_path, monkeypatch)

    assert result["baseline_had"] == ["window"]
    assert result["window_title_disabled"] is True
    row = _state(tmp_path)["watch"]
    assert row["baseline_had"] == ["window"]
    assert row["window_title_disabled"] is True

    lines = engine.watch_status().splitlines()
    assert lines[0] == "watching Terminal for BUILD OK · 0 events · no hit"
    assert lines[1] == (
        'at arm: window title already contained "BUILD OK" (command echo) '
        '— waiting for the value to change')
    assert lines[2] == "title matching disabled for terminal app"


def test_a_value_transition_hit_names_the_value_surface(
        tmp_path: Path, monkeypatch) -> None:
    """Exactly one announcement, and it says the OUTPUT matched (not the title)."""
    _overlay, _result = _terminal(tmp_path, monkeypatch)
    said = []
    monkeypatch.setattr(engine, "say",
                        lambda text: said.append(text) or {"ok": True})

    hit = {"op": "watch-hit", "app": "Terminal", "until": "BUILD OK",
           "label": "BUILD OK", "window": "sleep 70; echo BUILD OK",
           "matched": "value", "baseline_had": ["window"],
           "at": 1_794_330_019.0}
    engine._on_overlay_event(hit)
    engine._on_overlay_event(hit)  # a duplicate line must not speak twice

    assert said == ["Terminal: BUILD OK appeared"]
    state = _state(tmp_path)
    assert state["watch"]["hit"]["matched"] == "value"
    assert state["watch"]["hit"]["baseline_had"] == ["window"]
    assert state["watch_hit"]["matched"] == "value"
    assert state["watch_hit"]["baseline_had"] == ["window"]
    assert state["watch"]["stopped"]["reason"] == "hit"

    lines = engine.watch_status().splitlines()
    assert lines[1].startswith('hit: value — "BUILD OK" (output) at ')
    assert all("at arm:" not in line for line in lines), (
        "a hit is not still described as waiting")


def test_a_window_surface_hit_says_window(tmp_path: Path, monkeypatch) -> None:
    """Everyone else keeps window-first behaviour, and the status names it."""
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})

    engine._on_overlay_event({
        "op": "watch-hit", "app": "Slack", "until": "Export complete",
        "label": "Export complete", "window": "Export complete — Preview",
        "matched": "window", "baseline_had": [],
        "at": 1_794_330_012.0})

    lines = engine.watch_status().splitlines()
    assert lines[1].startswith(
        'hit: window — "Export complete" (window title) at ')


def test_a_hit_without_a_surface_keeps_the_legacy_line(
        tmp_path: Path, monkeypatch) -> None:
    """An older companion's hit payload still renders the old sentence."""
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})

    engine._on_overlay_event({
        "op": "watch-hit", "app": "Slack", "until": "Export complete",
        "label": "Export complete", "window": "general — Slack",
        "at": 1_794_330_012.0})

    assert engine.watch_status().splitlines()[1].startswith(
        "hit: Export complete — general — Slack at ")


# ------------------------------------- the value surface fires on the DELTA
#
# A terminal's AXValue is the WHOLE scrollback, not a per-line field. Round 15
# made a pre-existing value wait for "new contains the needle AND old does
# not", which can never fire again: the next run appends another `BUILD OK`,
# so every later `old` still contains it and the watch stays silent forever.
# The companion now matches what the buffer GAINED and sends that delta as
# `new_text`; the status line reads the matched line, not the whole buffer.

def _value_hit(**extra) -> dict:
    row = {"op": "watch-hit", "app": "Terminal", "until": "BUILD OK",
           "label": "BUILD OK", "window": "sleep 70; echo BUILD OK",
           "matched": "value", "baseline_had": ["window"],
           "at": 1_794_330_019.0}
    row.update(extra)
    return row


def test_a_value_hit_keeps_the_delta_and_the_status_reads_it(
        tmp_path: Path, monkeypatch) -> None:
    """The hit carries `new_text`, and one hit line names the matched line."""
    _overlay, _result = _terminal(tmp_path, monkeypatch)
    said = []
    monkeypatch.setattr(engine, "say",
                        lambda text: said.append(text) or {"ok": True})

    hit = _value_hit(new_text="BUILD OK")
    engine._on_overlay_event(hit)
    engine._on_overlay_event(hit)  # a duplicate line must not speak twice

    assert said == ["Terminal: BUILD OK appeared"]
    state = _state(tmp_path)
    assert state["watch"]["hit"]["new_text"] == "BUILD OK"
    assert state["watch"]["hit"]["matched"] == "value"
    assert state["watch_hit"]["new_text"] == "BUILD OK", (
        "the matched line outlives the ended watch row")
    assert state["watch_hit"]["matched"] == "value"

    lines = engine.watch_status().splitlines()
    assert lines[1].startswith('hit: value — "BUILD OK" (output) at ')


def test_a_value_hit_reads_the_delta_not_the_scrollback_label(
        tmp_path: Path, monkeypatch) -> None:
    """The label may be the whole buffer; the hit line must not repeat it."""
    _overlay, _result = _terminal(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    scrollback = "➜  ~ sleep 70; echo BUILD OK\nBUILD OK\n➜  ~ "
    engine._on_overlay_event(_value_hit(label=scrollback,
                                        new_text="BUILD OK\n"))

    line = engine.watch_status().splitlines()[1]

    assert line.startswith('hit: value — "BUILD OK" (output) at ')
    assert "sleep 70" not in line, "the status line is not the whole buffer"


def test_a_multiline_value_delta_status_reads_the_matched_line(
        tmp_path: Path, monkeypatch) -> None:
    """`new_text` is the whole delta; the hit line is the line that matched."""
    _overlay, _result = _terminal(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    engine._on_overlay_event(_value_hit(new_text="step 1 done\nBUILD OK\n"))

    line = engine.watch_status().splitlines()[1]

    assert line.startswith('hit: value — "BUILD OK" (output) at ')
    assert "step 1" not in line
    assert "\n" not in line


def test_an_overlong_value_delta_is_capped_like_a_capture(
        tmp_path: Path, monkeypatch) -> None:
    """The `new_text` cap is the capture cap: 200 chars plus `…`."""
    _overlay, _result = _terminal(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    long_delta = "x" * 230

    engine._on_overlay_event(_value_hit(new_text=long_delta))

    kept = _state(tmp_path)["watch"]["hit"]["new_text"]
    assert kept == "x" * engine.WATCH_NEW_TEXT_LIMIT + "…"
    assert len(kept) == engine.WATCH_NEW_TEXT_LIMIT + 1


def test_a_non_string_value_delta_is_ignored(
        tmp_path: Path, monkeypatch) -> None:
    """`new_text` is a value-surface string; a malformed row cannot invent one."""
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})

    engine._on_overlay_event({
        "op": "watch-hit", "app": "Slack", "until": "Export complete",
        "label": "Export complete", "new_text": 42,
        "at": 1_794_330_012.0})

    assert "new_text" not in _state(tmp_path)["watch"]["hit"]


# ------------------------------------------------------- capture value field

def _capture_reply(elements):
    return {"trusted": True, "app": "Terminal", "window": "shell",
            "count": len(elements), "with_box": 1, "elements": elements}


def test_capture_carries_the_ax_value_of_a_text_area(monkeypatch) -> None:
    """`value` is its own field: a terminal's content reaches the payload."""
    from butler_core import capture as bp_capture

    reply = _capture_reply([{
        "role": "AXTextArea", "label": "shell", "value": "BUILD OK\n➜",
        "box": [10, 20, 30, 40], "id": 0,
    }])
    monkeypatch.setattr(bp_capture, "HAS_MAC", True)
    monkeypatch.setattr(bp_capture, "_request", lambda op, timeout=None: reply)

    snap = bp_capture.capture(target="Terminal")

    element = snap["elements"][0]
    assert element["value"] == "BUILD OK\n➜"
    assert element["label"] == "shell", "label keeps its derivation"
    assert "truncated" not in element


def test_capture_keeps_the_optional_value_fields_optional(
        monkeypatch) -> None:
    """Absent stays absent (no null keys), and a cut value keeps its marker."""
    from butler_core import capture as bp_capture

    long_value = "x" * 200 + "…"
    reply = _capture_reply([
        {"role": "AXTextArea", "label": "shell", "value": long_value,
         "truncated": True, "box": [0, 0, 10, 10], "id": 0},
        {"role": "AXButton", "label": "Export", "box": [0, 0, 10, 10], "id": 1},
    ])
    monkeypatch.setattr(bp_capture, "HAS_MAC", True)
    monkeypatch.setattr(bp_capture, "_request", lambda op, timeout=None: reply)

    elements = bp_capture.capture()["elements"]

    assert elements[0]["value"] == long_value
    assert elements[0]["truncated"] is True
    assert elements[0]["label"] == "shell"
    assert "value" not in elements[1]
    assert "truncated" not in elements[1]
