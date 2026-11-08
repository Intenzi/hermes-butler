"""Static checks for the desktop half of the plugin."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JS = ROOT / "desktop" / "plugin.js"


def test_plugin_registers_a_status_bar_right_entry() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert re.search(r"area\s*:\s*['\"]statusBar\.right['\"]", source)
    assert "launcher-chip" in source


def test_plugin_uses_session_create_or_focus_rpc() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "session.list" in source
    assert "session.create" in source
    assert "openSession" in source


def test_plugin_registers_the_ask_the_butler_keybind() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "KEYBINDS_AREA" in source
    assert "label: 'Ask the butler'" in source
    assert "butler.summon" in source


def test_plugin_registers_a_fixed_summon_bar_and_pet_click_toggle() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "Ask the butler for a guide…" in source
    assert "TITLEBAR_AREAS" in source
    assert "SummonBar" in source
    assert "Click the pet to ask the butler" in source
    assert "pet_click_claim" in source
    assert "canvas[aria-label$=\" pet\"]" in source


def test_plugin_uses_the_composer_submit_send_path_with_a_honest_draft_fallback() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "host.composer.submit" in source
    assert "setDraft" in source
    assert "no_programmatic_send" in source


def test_plugin_has_a_persistent_destination_picker_and_sends_the_slash_form() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "Butler chat" in source
    assert "New session" in source
    assert "butler.summon.destination" in source
    assert "localStorage" in source
    assert "/butler " in source
    assert "Summoned → fresh Butler session" in source


def test_plugin_has_the_one_shot_in_window_pet_hint() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "No in-window pet found" in source
    assert "Cmd+Shift+A" in source
    assert "subscribePetVisibilityHint" in source
    assert "petVisibilityHint" in source
    assert "PET_SCAN_TIMEOUT_MS = 10000" in source
