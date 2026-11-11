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
    engine._watch_ring.clear()


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path):
    _reset()
    engine.init(tmp_path)
    yield
    _reset()


def _start(tmp_path: Path, monkeypatch, until=None, replies=None, once=True):
    overlay = FakeOverlay(replies if replies is not None
                          else [{"op": "watch-started", "app": "Slack"}])
    monkeypatch.setattr(engine, "_OVERLAY", overlay)
    result = engine.start_watch("Slack", until=until, once=once)
    return overlay, result


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
    assert _state(tmp_path)["watch"]["hit"] is None
    # The watch still observes after the acknowledgement.
    engine._on_overlay_event(_event(1))
    assert engine._watch["events"] == 1
    # A second ack is an honest false, not an exception.
    again = engine.acknowledge_watch_hit()
    assert again["acknowledged"] is False
    assert again["status"] == "no-hit"


def test_watch_ack_is_false_for_a_one_shot_hit_and_when_idle(
        tmp_path: Path, monkeypatch) -> None:
    _start(tmp_path, monkeypatch, until="Export complete")
    monkeypatch.setattr(engine, "say", lambda text: {"ok": True})
    engine._on_overlay_event({"op": "watch-hit", "app": "Slack",
                              "label": "Export complete", "at": 5.0})

    assert engine.acknowledge_watch_hit()["acknowledged"] is False
    assert engine._watch["stopped"]["reason"] == "hit", "the ending still stands"

    engine.stop_watch()
    assert engine.acknowledge_watch_hit() == {"status": "idle",
                                              "acknowledged": False}


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
