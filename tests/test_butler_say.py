"""The agent's on-screen answer: ``butler_say`` writes ``say.json``.

A LOOK question is answered in the chat, but the user is looking at the screen.
``butler_say`` publishes the short answer to the plugin data dir; ``/state``
merges it and the desktop speaks it once on the pet. This module pins the engine
half: the row shape, the 280-character bubble cap, and that the handler never
raises.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# conftest.py reserves the synthetic name "__init__" for pytest collection, so
# load the plugin under its own package name to execute its relative imports.
_SPEC = importlib.util.spec_from_file_location(
    "butler_plugin_say_under_test", ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
plugin = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin
_SPEC.loader.exec_module(plugin)

from butler_core import engine  # noqa: E402  (after package import setup)


class RegisterCtx:
    """The small slice of PluginContext register(ctx) touches."""

    def __init__(self, tmp_path: Path):
        self._data = tmp_path
        self.tools = []
        self.commands = []
        self.skills = []
        self.cli = []

    def plugin_data_dir(self):
        return str(self._data)

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands.append({"name": name, "handler": handler})

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)

    def register_skill(self, **kwargs):
        self.skills.append(kwargs)


def _registered(tmp_path: Path) -> RegisterCtx:
    ctx = RegisterCtx(tmp_path)
    plugin.register(ctx)
    return ctx


def _say_tool(ctx: RegisterCtx) -> dict:
    return next(row for row in ctx.tools if row["name"] == "butler_say")


def _call(ctx: RegisterCtx, args: dict) -> dict:
    return json.loads(_say_tool(ctx)["handler"](args))


def test_register_installs_butler_say_with_a_required_text_schema(tmp_path: Path) -> None:
    ctx = _registered(tmp_path)

    tool = _say_tool(ctx)
    assert tool["schema"]["required"] == ["text"]
    assert tool["schema"]["properties"]["text"]["type"] == "string"
    assert tool["toolset"] == "butler"
    # The tool set stays discoverable after adding the answer and watch tools.
    assert [row["name"] for row in ctx.tools] == [
        "butler_guide_start",
        "butler_guide_status",
        "butler_capture",
        "butler_look",
        "butler_say",
        "butler_stop",
        "butler_watch_start",
        "butler_watch_status",
        "butler_watch_stop",
        "butler_watch_ack",
    ]


def test_butler_say_writes_say_json_with_id_text_and_at(tmp_path: Path) -> None:
    ctx = _registered(tmp_path)

    payload = _call(ctx, {"text": "  It exports the page as a PDF.  "})

    assert payload == {"ok": True, "shown": "It exports the page as a PDF."}
    row = json.loads((tmp_path / "say.json").read_text(encoding="utf-8"))
    assert row["text"] == "It exports the page as a PDF."
    assert re.fullmatch(r"[0-9a-f]{8}", row["id"])
    assert isinstance(row["at"], float)
    assert row["at"] > 0


def test_each_answer_mints_a_new_id_so_the_pet_speaks_again(tmp_path: Path) -> None:
    ctx = _registered(tmp_path)

    _call(ctx, {"text": "First answer."})
    first = json.loads((tmp_path / "say.json").read_text(encoding="utf-8"))
    _call(ctx, {"text": "Second answer."})
    second = json.loads((tmp_path / "say.json").read_text(encoding="utf-8"))

    assert second["id"] != first["id"]
    assert second["text"] == "Second answer."
    # The file is replaced atomically, never appended or half-written.
    assert list(tmp_path.glob("say.json*")) == [tmp_path / "say.json"]


def test_butler_say_truncates_inside_the_pet_bubble_cap(tmp_path: Path) -> None:
    ctx = _registered(tmp_path)
    long_text = "alpha " * 100  # 600 characters, every boundary is a word gap

    payload = _call(ctx, {"text": long_text})

    shown = payload["shown"]
    assert len(shown) <= engine.SAY_MAX_CHARS == 110
    assert shown.endswith("…")
    # The cut lands between words: no half "alph" before the ellipsis.
    assert shown[:-1].endswith("alpha")
    assert json.loads((tmp_path / "say.json").read_text(encoding="utf-8"))["text"] == shown


def test_an_unbroken_token_keeps_a_hard_cut_at_the_cap(tmp_path: Path) -> None:
    ctx = _registered(tmp_path)

    payload = _call(ctx, {"text": "x" * 400})

    shown = payload["shown"]
    assert len(shown) <= engine.SAY_MAX_CHARS
    assert shown.endswith("…")
    assert shown == "x" * (engine.SAY_MAX_CHARS - 1) + "…"


def test_a_short_answer_is_stored_verbatim(tmp_path: Path) -> None:
    ctx = _registered(tmp_path)
    text = "That button exports the current page."

    assert _call(ctx, {"text": text})["shown"] == text


def test_blank_text_is_refused_without_writing_or_raising(tmp_path: Path) -> None:
    ctx = _registered(tmp_path)

    payload = _call(ctx, {})

    assert payload["ok"] is False
    assert "text" in payload["error"].lower()
    assert not (tmp_path / "say.json").exists()


def test_a_failed_write_reports_it_and_leaves_no_temp_file(
        tmp_path: Path, monkeypatch) -> None:
    """A publish failure must not orphan its per-writer temp file."""
    ctx = _registered(tmp_path)

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(engine.os, "replace", refuse)

    payload = _call(ctx, {"text": "a lost bubble"})

    assert payload["ok"] is False
    assert "disk full" in payload["error"]
    assert list(tmp_path.glob("say.json*")) == []


def test_engine_say_never_raises_without_an_init(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(engine, "DATA_DIR", None)

    payload = engine.say("still honest")

    assert payload["ok"] is False
    assert "init" in payload["error"]


def test_the_say_cap_fits_the_pet_bubble() -> None:
    """The host truncates every plugin line at PET_MESSAGE_MAX_CHARS (120) with an
    ellipsis, so a 280-char cap guaranteed a "…" in the bubble."""
    from butler_core import engine
    assert engine.SAY_MAX_CHARS <= 110
