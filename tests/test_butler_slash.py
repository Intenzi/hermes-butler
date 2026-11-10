"""Contract tests for the agent-side ``/butler`` slash command.

Hermes dispatches a plugin slash command as a handler call, not as an agent
turn, so the handler now injects the request back into the CURRENT chat as a
framed agent turn and returns only a short acknowledgement. The real plan (or
the agent's one clarifying question) is a normal chat reply.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

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

from butler_core.summon import (  # noqa: E402  (after package import setup)
    SUMMON_ACK,
    SUMMON_DENIED,
    frame_summon_text,
)


class FakeCtx:
    def __init__(self, accepted=True, raises=False):
        self.accepted = accepted
        self.raises = raises
        self.injected = []
        self.commands = []

    def inject_message(self, text, **kwargs):
        self.injected.append({"text": text, **kwargs})
        if self.raises:
            raise RuntimeError("injection host is down")
        return self.accepted

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands.append({
            "name": name, "handler": handler,
            "description": description, "args_hint": args_hint,
        })


class RegisterCtx(FakeCtx):
    """The small slice of PluginContext register(ctx) touches."""

    def __init__(self, tmp_path: Path):
        super().__init__()
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


def test_register_installs_the_butler_slash_command(tmp_path: Path) -> None:
    ctx = RegisterCtx(tmp_path)

    plugin.register(ctx)

    command = next(row for row in ctx.commands if row["name"] == "butler")
    assert command["args_hint"] == "<request>"
    assert "chat" in command["description"].lower()
    assert callable(command["handler"])
    assert plugin.get_plugin_context() is ctx


def test_register_installs_butler_look_and_it_calls_the_capture_path(
        tmp_path: Path, monkeypatch) -> None:
    """The agent's LOOK tool re-captures the named app instead of re-asking."""
    ctx = RegisterCtx(tmp_path)
    plugin.register(ctx)

    tool = next(row for row in ctx.tools if row["name"] == "butler_look")
    assert "target" in tool["schema"]["properties"]
    assert tool["schema"]["required"] == ["target"]

    seen = {}

    def fake_capture(**kwargs):
        seen.update(kwargs)
        return {"app": "Preview", "count": 1, "elements": [{"role": "AXButton"}]}

    monkeypatch.setattr(plugin.bp_capture, "capture", fake_capture)
    payload = json.loads(tool["handler"]({"target": "Preview"}))

    assert payload["app"] == "Preview"
    assert seen["target"] == "Preview"
    assert seen["max_elements"] == 150
    assert seen["depth"] == 10


def test_registered_handler_injects_the_same_frame_and_returns_the_short_ack(
        tmp_path: Path) -> None:
    ctx = RegisterCtx(tmp_path)
    plugin.register(ctx)

    command = next(row for row in ctx.commands if row["name"] == "butler")
    reply = command["handler"]("turn off wifi")

    assert reply == SUMMON_ACK
    assert ctx.injected == [
        {"text": frame_summon_text("turn off wifi"), "role": "user"}
    ]


def test_slash_injects_into_the_current_session_with_the_exact_frame() -> None:
    ctx = FakeCtx()

    reply = plugin._handle_butler_slash(ctx, "turn off wifi")

    assert reply == "Butler request injected — answering in this chat."
    assert len(ctx.injected) == 1
    call = ctx.injected[0]
    assert call["text"] == (
        "[butler summon] turn off wifi\n\n"
        "Treat the request above as a butler guidance task: plan a follow-mode guide "
        "(butler:follow skill) and start it with butler_guide_start; if it is too vague "
        "to ground one first step, ask exactly ONE short clarifying question and wait."
    )
    assert call["role"] == "user"
    # Current-session injection: no explicit session_key is invented by the handler.
    assert "session_key" not in call


def test_denied_injection_returns_an_honest_slash_reply() -> None:
    ctx = FakeCtx(accepted=False)

    reply = plugin._handle_butler_slash(ctx, "fix it")

    assert reply == SUMMON_DENIED
    assert reply != SUMMON_ACK
    assert "could not inject" in reply.lower()
    assert len(ctx.injected) == 1


def test_injection_exception_fails_closed_without_raising() -> None:
    ctx = FakeCtx(raises=True)

    reply = plugin._handle_butler_slash(ctx, "fix it")

    assert reply == SUMMON_DENIED


def test_missing_injection_api_is_reported_not_planned() -> None:
    class NoInjection:
        pass

    reply = plugin._handle_butler_slash(NoInjection(), "fix it")

    assert reply == SUMMON_DENIED


def test_empty_or_help_argument_returns_usage_without_injecting() -> None:
    ctx = FakeCtx()

    assert plugin._handle_butler_slash(ctx, "") == plugin.BUTLER_SLASH_HELP
    assert plugin._handle_butler_slash(ctx, "help") == plugin.BUTLER_SLASH_HELP
    assert plugin._handle_butler_slash(ctx, "?") == plugin.BUTLER_SLASH_HELP
    assert ctx.injected == []
