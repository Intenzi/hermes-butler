"""Guide lifecycle: stable stall notes, auto-stop, and explicit takeover.

A stalled guide used to repeat a note with a changing elapsed counter, so the
pet's speech dedupe could never suppress it. The note is now stable and a
stall past the deadline stops the guide once; a new guide may take over only
when the caller explicitly passes ``replace=true``.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# conftest.py reserves the synthetic name "__init__" for pytest collection, so
# load the plugin under its own package name to execute its relative imports.
_SPEC = importlib.util.spec_from_file_location(
    "butler_plugin_lifecycle_under_test", ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
plugin = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin
_SPEC.loader.exec_module(plugin)

from butler_core import engine, summon  # noqa: E402


class _FakeOverlay:
    def __init__(self) -> None:
        self.published = []
        self.idled = 0
        self.done_calls = 0
        self.quit_calls = 0

    def start(self):
        return True, "fake"

    def publish(self, state):
        self.published.append(dict(state))

    def idle(self):
        self.idled += 1

    def done(self):
        self.done_calls += 1

    def quit(self):
        self.quit_calls += 1


def _snapshot(elements, app="System Settings", window="Keyboard"):
    return {
        "trusted": True,
        "app": app,
        "window": window,
        "count": len(elements),
        "with_box": len(elements),
        "elements": list(elements),
    }


def _element(label, box=(10, 20, 30, 40), role="AXButton"):
    return {"role": role, "label": label, "box": list(box), "id": 0}


def _step(find_label="Keyboard", expect_visible="Keyboard"):
    return {
        "say": "Click Keyboard",
        "find": {"label": find_label},
        "expect": {"visible": expect_visible},
    }


def _session(steps=None, *, status="running", opened_at=None, stalled_at=None,
             session_id="guide-lifecycle"):
    now = time.time()
    steps = steps or [_step()]
    state = {
        "idx": 0,
        "status": status,
        "say": steps[0]["say"],
        "step_opened_at": opened_at if opened_at is not None else now,
    }
    if stalled_at is not None:
        state["stalled_at"] = stalled_at
    return {
        "id": session_id,
        "title": "Lifecycle",
        "steps": steps,
        "state": state,
        "verifications": [],
        "overlay": "waiting",
        "started_at": now,
    }


def _write_config(data_dir: Path, payload: dict) -> None:
    (data_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def engine_state(tmp_path, monkeypatch):
    engine.init(tmp_path)
    fake = _FakeOverlay()
    monkeypatch.setattr(engine, "_OVERLAY", fake)
    monkeypatch.setattr(engine, "_loop", lambda: None)
    engine._session = None
    engine._stop_flag = False
    engine._last_deep_capture_at = None
    yield engine, fake, tmp_path
    engine._stop_flag = True
    engine._session = None
    engine._last_deep_capture_at = None


def test_stall_note_is_stable_and_carries_the_reason(
        engine_state, monkeypatch) -> None:
    eng, _fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot([]))

    session = _session(opened_at=time.time() - eng.step_timeout() - 5)
    eng._poll_once(session)

    assert session["state"]["status"] == "stalled"
    note = session["state"]["note"]
    assert note == "step 1 unverified — expect=expectation_unmet"
    # No elapsed counter: the pet's dedupe must be able to suppress the line.
    assert not re.search(r"\d+\s*s\b", note)
    assert "600s" not in note


def test_stall_past_the_deadline_auto_stops_once(
        engine_state, monkeypatch) -> None:
    eng, fake, tmp_path = engine_state
    _write_config(tmp_path, {"step_timeout_seconds": 120,
                             "stall_auto_stop_seconds": 5})
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot([]))
    says = []
    monkeypatch.setattr(eng, "say",
                        lambda text: says.append(text) or {"ok": True})

    session = _session(status="stalled",
                       opened_at=time.time() - 200,
                       stalled_at=time.time() - 6)
    eng._session = session
    eng._poll_once(session)

    assert session["state"]["status"] == "abandoned"
    assert session["state"]["note"] == "step 1 was never verified"
    assert session["reason"] == "step 1 was never verified"
    assert fake.idled >= 1
    # The loop must not keep polling the abandoned session.
    assert eng._stop_flag is True
    # It is written to history (the per-guide JSON is what /history reads).
    saved = json.loads((tmp_path / "guides" / (session["id"] + ".json"))
                       .read_text(encoding="utf-8"))
    assert saved["state"]["status"] == "abandoned"
    assert saved["reason"] == "step 1 was never verified"
    assert says == ["Guide stopped — step 1 never verified"]


def test_stalled_guide_that_verifies_before_the_deadline_resumes(
        engine_state, monkeypatch) -> None:
    eng, _fake, tmp_path = engine_state
    _write_config(tmp_path, {"stall_auto_stop_seconds": 180})
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot([_element("Keyboard")]))

    steps = [_step(), _step("Next", "Next")]
    session = _session(steps, status="stalled",
                       opened_at=time.time() - 200,
                       stalled_at=time.time() - 1)
    eng._poll_once(session)

    assert session["state"]["status"] == "running"
    assert session["state"]["note"] is None
    assert "stalled_at" not in session["state"]
    assert session["state"]["idx"] == 1


def test_guide_start_without_replace_refuses_with_a_hint(
        engine_state, monkeypatch) -> None:
    eng, _fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot([_element("Keyboard")]))

    first = eng.guide_start({"title": "First",
                             "steps": [_step()]})
    assert first["guide_id"]

    second = eng.guide_start({"title": "Second",
                              "steps": [_step()]})

    assert second["error"] == "a guide is already active"
    assert second["active"] == first["guide_id"]
    assert second["hint"] == (
        "a guide is already active — pass replace=true to take over, "
        "or butler_stop first")
    assert eng._session["id"] == first["guide_id"]


def test_guide_start_with_replace_supersedes_and_starts_the_new_guide(
        engine_state, monkeypatch) -> None:
    eng, fake, tmp_path = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot([_element("Keyboard")]))
    says = []
    monkeypatch.setattr(eng, "say",
                        lambda text: says.append(text) or {"ok": True})

    first = eng.guide_start({"title": "First", "steps": [_step()]})
    old_id = first["guide_id"]

    second = eng.guide_start({"title": "Second", "steps": [_step()],
                              "replace": True})

    assert second["replaced"] == old_id
    assert second["guide_id"] != old_id
    assert eng._session["id"] == second["guide_id"]
    assert eng.guide_status()["state"]["status"] == "running"

    old = json.loads((tmp_path / "guides" / (old_id + ".json"))
                     .read_text(encoding="utf-8"))
    assert old["state"]["status"] == "superseded"
    assert old["state"]["note"] == "replaced by a new guide"
    assert old["reason"] == "replaced by a new guide"
    assert says == ["Replaced the previous guide"]
    assert fake.idled >= 1


def test_stall_auto_stop_seconds_reads_config_and_defaults_to_180(
        tmp_path) -> None:
    engine.init(tmp_path)

    assert engine.DEFAULT_STALL_AUTO_STOP == 180.0
    assert engine.stall_auto_stop_seconds() == 180.0
    assert engine.stall_auto_stop() == 180.0

    _write_config(tmp_path, {"stall_auto_stop_seconds": 42})
    assert engine.stall_auto_stop_seconds() == 42.0


def test_both_summon_frames_carry_the_one_guide_policy() -> None:
    sentence = ("Only one guide runs at a time: if a guide is already active, "
                "stop it with butler_stop or pass replace=true — never leave two "
                "running.")

    summon_frame = summon.frame_summon_text("fix wifi")
    look_frame = summon.frame_look_text("what is this?")

    assert sentence in summon_frame
    assert "butler_guide_start" in summon_frame
    assert sentence in look_frame
    assert "butler_say" in look_frame
    assert "at most 90 characters" in look_frame


def test_history_reason_is_only_exposed_for_terminal_rows() -> None:
    from dashboard import plugin_api

    running = {
        "id": "running-1",
        "title": "Running",
        "state": {"status": "running", "note": "target not found in the first capture"},
        "steps": [],
        "started_at": 1.0,
    }
    assert "reason" not in plugin_api._history_entry(running, "running-1")

    superseded = {
        "id": "old-1",
        "title": "Old",
        "state": {"status": "superseded", "note": "replaced by a new guide"},
        "steps": [],
        "started_at": 1.0,
    }
    entry = plugin_api._history_entry(superseded, "old-1")
    assert entry["status"] == "superseded"
    assert entry["reason"] == "replaced by a new guide"


def test_stop_guide_terminalises_the_shared_state_and_keeps_the_watch(
        engine_state, tmp_path) -> None:
    eng, fake, _tmp = engine_state
    watched = {"app": "Opera GX", "events": 596, "stopped": None,
               "started_at": time.time()}
    session = _session(status="stalled", session_id="stale-guide")
    session["state"]["note"] = "step 1 unverified for 600s; expect=expectation_unmet"
    session["state"]["stalled_at"] = time.time() - 5
    eng._session = session
    # Isolate from every other watch/summon row a test may have left behind.
    eng._watch = None
    eng._watch_ring.clear()
    eng._summon_pending = None
    # The row on disk is the stale stalled block the defect measured; a
    # concurrent process owns the watch block.
    (tmp_path / "state.json").write_text(json.dumps({
        "guide": session,
        "overlay": "waiting",
        "watch": watched,
    }), encoding="utf-8")

    result = eng.stop_guide("stopped by agent")

    assert result["status"] == "stopped"
    assert fake.quit_calls == 1
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["guide"]["state"]["status"] == "stopped"
    assert state["guide"]["state"]["note"] is None
    assert state["guide"]["state"]["say"] is None
    assert isinstance(state["guide"]["state"]["stopped_at"], float)
    assert state["guide"]["state"]["stopped_at"] <= time.time()
    assert state["guide"]["id"] == "stale-guide"
    # The stop writes a terminal block; it must not clobber the other owner's
    # watch row (or the summon row, tested elsewhere).
    assert state["watch"] == watched
    # The guide stays in history, in the same terminal shape.
    saved = json.loads((tmp_path / "guides" / "stale-guide.json")
                       .read_text(encoding="utf-8"))
    assert saved["state"]["status"] == "stopped"
    assert saved["state"]["note"] is None
    assert saved["state"]["say"] is None
    assert eng._session is None


def test_get_state_drops_a_stale_terminal_guide_and_keeps_a_fresh_one(
        tmp_path, monkeypatch) -> None:
    from dashboard import plugin_api

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "plugin-data" / "butler" / "state.json"
    path.parent.mkdir(parents=True)
    now = time.time()
    watch = {"app": "Opera GX", "events": 596, "stopped": None}

    def _guide(stopped_at: float) -> dict:
        return {
            "id": "old-guide",
            "title": "Old",
            "state": {"status": "stopped", "note": None, "say": None,
                      "stopped_at": stopped_at},
            "steps": [{"say": "Click Settings"}],
            "verifications": [],
        }

    path.write_text(json.dumps({"guide": _guide(now - 601),
                                "watch": watch,
                                # A republished envelope must not keep an old
                                # block alive: the block's own stamp governs.
                                "generated_at": now}), encoding="utf-8")
    stale = plugin_api._read_state()
    assert "guide" not in stale, "a terminal block older than 10 min is swept"
    assert stale["watch"] == watch, "the sweep never touches the watch block"

    path.write_text(json.dumps({"guide": _guide(now - 60),
                                "watch": watch}), encoding="utf-8")
    fresh = plugin_api._read_state()
    assert fresh["guide"]["state"]["status"] == "stopped"
    assert fresh["watch"] == watch

    # A legacy block with no stamp of its own falls back to the envelope's
    # last publication time.
    legacy = {"id": "legacy", "title": "Legacy", "state": {"status": "done"}}
    path.write_text(json.dumps({"guide": legacy, "generated_at": now - 601}),
                    encoding="utf-8")
    assert "guide" not in plugin_api._read_state()


def test_guide_start_schema_documents_replace(tmp_path) -> None:
    class _Ctx:
        def __init__(self):
            self.tools = []

        def plugin_data_dir(self):
            return str(tmp_path)

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_command(self, *args, **kwargs):
            pass

        def register_cli_command(self, **kwargs):
            pass

        def register_skill(self, **kwargs):
            pass

    ctx = _Ctx()
    plugin.register(ctx)
    tool = next(row for row in ctx.tools if row["name"] == "butler_guide_start")

    assert tool["schema"]["properties"]["replace"]["type"] == "boolean"
    assert tool["schema"]["properties"]["replace"]["default"] is False
    assert "replace" not in tool["schema"].get("required", [])
    assert "replace=true" in tool["description"]
