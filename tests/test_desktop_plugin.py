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


def test_plugin_no_longer_registers_the_shadowed_web_summon_keybind() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    # The companion's Carbon chord owns ⌘⇧A; the app never sees it, so a keybind
    # contribution here would only shadow it and open a component that is gone.
    assert "KEYBINDS_AREA" not in source
    assert "label: 'Ask the butler'" not in source
    assert "id: 'butler.summon'" not in source


def test_plugin_ships_no_web_summon_bar_and_still_never_touches_the_dom() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "Ask the butler for a guide…" not in source
    assert "TITLEBAR_AREAS" not in source
    assert "SummonBar" not in source
    assert "summon-bar" not in source
    # Installing the plugin is the opt-in: no checkbox/switch may render.
    assert "type: 'checkbox'" not in source
    assert "Click the pet to ask the butler" not in source
    assert "writePetClickClaim" not in source
    assert "pet_click_claim" in source
    # The plugin never locates or overlays the app page.
    assert "PET_CANVAS_SELECTORS" not in source
    assert "document.querySelector" not in source
    assert "document.body.appendChild" not in source
    assert "addEventListener('pointerdown'" not in source


def test_plugin_has_an_honest_chip_status_row_and_popout_bridge() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "chip: ${claim ? 'on' : 'off'}" in source
    assert "petStatusRowText" in source
    assert "syncCompanionClaim(true)" in source
    assert "setPetClickClaim" not in source
    assert "/summon/claim" in source
    assert "summon_pending" in source
    assert "/summon/ack" in source
    # The LOOK answer path: the module-scope consumer still sends a text row,
    # over the two doors that can reach a chat the pet field cannot see.
    assert "startSummonPendingPolling" in source
    assert "sendSummonInjection" in source
    assert "sendSummonRpc" in source
    assert "sendPendingSummonText" not in source


def test_plugin_surfaces_unroutable_session_and_the_resolved_route() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "unroutable_session" in source
    assert "session not routable through the gateway" in source
    assert "session_key" in source


def test_plugin_has_no_pane_header_pet_visibility_hint() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "No in-window pet found" not in source
    assert "subscribePetVisibilityHint" not in source
    assert "petVisibilityHint" not in source
    assert "PET_SCAN_TIMEOUT_MS" not in source


def test_plugin_posts_the_summon_send_route_with_a_session_id_and_keeps_the_fallback() -> None:
    source = PLUGIN_JS.read_text(encoding="utf-8")

    assert "/summon/send" in source
    assert "session_id" in source
    assert "injection_denied" in source
    assert "injection_unavailable" in source
    assert "butlerCommandText" in source
