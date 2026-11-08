"""Contract tests for the agent-side ``/butler`` slash command.

Hermes dispatches a plugin slash command as a handler call, not as an agent
turn, so these tests pin the deterministic replacement: butler:follow
instructions + a live observation, one validated flat-step plan, then the real
``butler_guide_start`` tool (or exactly one clarifying question).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# conftest.py reserves the synthetic name "__init__" for pytest collection, so
# load the plugin under its own package name to execute its relative imports.
_SPEC = importlib.util.spec_from_file_location(
    "butler_plugin_under_test", ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
plugin = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin
_SPEC.loader.exec_module(plugin)


class FakeLlm:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed=self.parsed, text=json.dumps(self.parsed))


class FakeCtx:
    def __init__(self, parsed=None, guide_result=None):
        self.llm = FakeLlm(parsed)
        self.dispatched = []
        self.commands = []
        self.guide_result = guide_result if guide_result is not None else {
            "guide_id": "g1", "title": "Open Network", "state": {"status": "running"},
            "overlay": "ringing", "total_steps": 2,
        }

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands.append({
            "name": name, "handler": handler,
            "description": description, "args_hint": args_hint,
        })

    def dispatch_tool(self, name, args, **kwargs):
        self.dispatched.append((name, args))
        return json.dumps(self.guide_result)


class RegisterCtx(FakeCtx):
    """The small slice of PluginContext register(ctx) touches."""

    def __init__(self, tmp_path: Path):
        super().__init__(parsed={"outcome": "ask", "question": "Which app?"})
        self._data = tmp_path
        self.tools = []
        self.skills = []
        self.cli = []

    def plugin_data_dir(self):
        return str(self._data)

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)

    def register_skill(self, **kwargs):
        self.skills.append(kwargs)


def _snapshot():
    return {
        "app": "System Settings",
        "window": "Network",
        "trusted": True,
        "elements": [
            {"role": "AXButton", "label": "Wi-Fi"},
            {"role": "AXButton", "label": "Bluetooth"},
        ],
    }


def _plan(steps=None, title="Open Network"):
    return {
        "outcome": "start",
        "title": title,
        "steps": steps if steps is not None else [
            {"say": "Click Wi-Fi", "find.label": "Wi-Fi", "expect.window": "Wi-Fi"},
            {"say": "Turn Wi-Fi on", "find.label": "Wi-Fi", "expect.visible": "Wi-Fi"},
        ],
    }


def test_register_installs_the_butler_slash_command(tmp_path: Path) -> None:
    ctx = RegisterCtx(tmp_path)

    plugin.register(ctx)

    command = next(row for row in ctx.commands if row["name"] == "butler")
    assert command["args_hint"] == "<request>"
    assert "guide" in command["description"].lower()
    assert callable(command["handler"])


def test_registered_handler_runs_the_same_observe_plan_start_pipeline(
        tmp_path: Path, monkeypatch) -> None:
    ctx = RegisterCtx(tmp_path)
    ctx.llm.parsed = _plan()
    plugin.register(ctx)
    monkeypatch.setattr(plugin.bp_capture, "capture", lambda **kwargs: _snapshot())

    command = next(row for row in ctx.commands if row["name"] == "butler")
    reply = command["handler"]("turn off wifi")

    assert "walking the guide now" in reply
    assert [name for name, _args in ctx.dispatched] == ["butler_guide_start"]


def test_slash_plans_from_a_live_observation_then_starts_the_guide(
        monkeypatch) -> None:
    ctx = FakeCtx(parsed=_plan())
    monkeypatch.setattr(plugin.bp_capture, "capture", lambda **kwargs: _snapshot())

    reply = plugin._handle_butler_slash(ctx, "turn off wifi")

    assert "walking the guide now" in reply
    assert "Step 1: Click Wi-Fi" in reply
    assert [name for name, _args in ctx.dispatched] == ["butler_guide_start"]
    title, steps = ctx.dispatched[0][1]["title"], ctx.dispatched[0][1]["steps"]
    assert title == "Open Network"
    # Flat keys survive untouched; the engine's normalizer consumes them.
    assert steps == [
        {"say": "Click Wi-Fi", "find.label": "Wi-Fi", "expect.window": "Wi-Fi"},
        {"say": "Turn Wi-Fi on", "find.label": "Wi-Fi", "expect.visible": "Wi-Fi"},
    ]
    assert all("find" not in step or isinstance(step["find"], dict) for step in steps)

    call = ctx.llm.calls[0]
    assert "BUTLER GUIDANCE REQUEST" in call["instructions"]
    assert "Grounding rule" in call["instructions"], "butler:follow must be loaded"
    assert call["json_schema"]["properties"]["steps"]["maxItems"] == 6
    prompt = call["input"][0]["text"]
    assert "turn off wifi" in prompt
    assert "System Settings" in prompt and "Wi-Fi" in prompt


def test_vague_request_asks_one_question_and_starts_nothing(monkeypatch) -> None:
    ctx = FakeCtx(parsed={
        "outcome": "ask",
        "question": "Which app are you in right now? I can then look at it.",
    })
    monkeypatch.setattr(plugin.bp_capture, "capture", lambda **kwargs: _snapshot())

    reply = plugin._handle_butler_slash(ctx, "fix it")

    assert reply == "Which app are you in right now?"
    assert ctx.dispatched == []


def test_plan_over_six_steps_is_refused_with_one_question(monkeypatch) -> None:
    steps = [{"say": f"Step {i}", "find.label": "Wi-Fi"} for i in range(7)]
    ctx = FakeCtx(parsed=_plan(steps=steps))
    monkeypatch.setattr(plugin.bp_capture, "capture", lambda **kwargs: _snapshot())

    reply = plugin._handle_butler_slash(ctx, "do everything")

    assert "more than six steps" in reply
    assert reply.count("?") == 1
    assert ctx.dispatched == []


def test_ungrounded_plan_asks_instead_of_starting_a_dead_guide(monkeypatch) -> None:
    ctx = FakeCtx(parsed=_plan(steps=[{"say": "Click the mystery button"}]))
    monkeypatch.setattr(plugin.bp_capture, "capture", lambda **kwargs: _snapshot())

    reply = plugin._handle_butler_slash(ctx, "clear the cache")

    assert "could not ground a verifiable first step" in reply
    assert ctx.dispatched == []


def test_capture_failure_still_reaches_the_planner_and_reports_the_grant(
        monkeypatch) -> None:
    ctx = FakeCtx(parsed=_plan(), guide_result={
        "guide_id": "g1", "title": "Open Network",
        "state": {"status": "capture_failed"},
        "capture_note": "accessibility grant missing",
    })
    monkeypatch.setattr(plugin.bp_capture, "capture", lambda **kwargs: {
        "trusted": False, "error": "accessibility grant missing",
    })

    reply = plugin._handle_butler_slash(ctx, "open network settings")

    assert "hermes butler doctor --ask" in reply
    assert "accessibility grant missing" in reply


def test_empty_or_help_argument_returns_usage_without_planning() -> None:
    ctx = FakeCtx(parsed=_plan())

    assert plugin._handle_butler_slash(ctx, "") == plugin.BUTLER_SLASH_HELP
    assert plugin._handle_butler_slash(ctx, "help") == plugin.BUTLER_SLASH_HELP
    assert ctx.llm.calls == []
