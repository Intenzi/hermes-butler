"""Pre-submission surface: README verbs, guide-start schema, plugin.yaml knobs.

These three are what a reviewer or the host reads before any code runs, so each
one must agree with the engine: the README must document all three verbs and
point at no missing image, the tool schema must accept the flat/hands-off steps
``butler:follow`` documents, and the manifest must advertise only config keys
the engine actually reads.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butler_core import engine  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "butler_plugin_release_surface_under_test", ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
plugin = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin
_SPEC.loader.exec_module(plugin)

README = ROOT / "README.md"
PLUGIN_YAML = ROOT / "plugin.yaml"

#: The keys the engine actually reads from plugin-data/butler/config.json.
LIVE_CONFIG_KEYS = ("poll_seconds", "step_timeout_seconds", "stall_auto_stop_seconds")
#: Keys the old manifest advertised that nothing reads.
DEAD_CONFIG_KEYS = (
    "guide_poll_seconds",
    "guide_step_timeout_seconds",
    "overlay_enabled",
    "ocr_fallback_enabled",
)


# ------------------------------------------------------------------- README

def test_readme_documents_look_and_watch() -> None:
    text = README.read_text(encoding="utf-8")

    assert re.search(r"^## Look$", text, re.M), "Look has its own section"
    assert re.search(r"^## Watch$", text, re.M), "Watch has its own section"
    assert "butler_watch_start" in text
    assert "butler_watch_status" in text
    assert "butler_watch_stop" in text
    assert "Watching" in text
    # The three prompt frames are named, and typed watch requests route to one.
    for frame in ("[butler summon]", "[butler look]", "[butler watch]"):
        assert frame in text
    assert "watch <app> for" in text


def test_look_section_states_the_picker_and_the_pop_out_requirement() -> None:
    text = README.read_text(encoding="utf-8")
    look = text.split("## Look", 1)[1].split("\n## ", 1)[0]

    assert "Right-click" in look
    assert "crosshair" in look
    assert "Esc" in look
    assert "never reaches the app" in look
    assert "popped out" in look


def test_readme_images_resolve() -> None:
    """Every image the README embeds must exist: a broken figure renders as an
    alt-text box on the docs page, which is worse than no figure at all."""
    import re

    text = README.read_text(encoding="utf-8")
    sources = re.findall(r'<img[^>]+src="([^"]+)"', text) + re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    for src in sources:
        if src.startswith(("http://", "https://")):
            continue
        assert (ROOT / src).is_file(), f"README embeds {src}, which is not in the repo"


def test_readme_points_at_the_screenshot_folder() -> None:
    assert "docs/img" in README.read_text(encoding="utf-8")


# ------------------------------------------------------------------ schema

def _valid(schema: dict, value) -> bool:
    """Validate the JSON-Schema slice this plugin's tool schema uses."""
    kind = schema.get("type")
    if kind == "object" and not isinstance(value, dict):
        return False
    if kind == "string" and not isinstance(value, str):
        return False
    if kind == "array" and not isinstance(value, list):
        return False
    if kind == "number" and not isinstance(value, (int, float)):
        return False
    if kind == "integer" and not isinstance(value, int):
        return False
    if kind == "boolean" and not isinstance(value, bool):
        return False
    if isinstance(value, dict):
        if any(key not in value for key in schema.get("required") or []):
            return False
        for key, child in (schema.get("properties") or {}).items():
            if key in value and not _valid(child, value[key]):
                return False
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return all(_valid(schema["items"], item) for item in value)
    return True


def _accepts_step(step: dict) -> bool:
    steps = plugin.GUIDE_START_SCHEMA["properties"]["steps"]
    return _valid(steps, [step])


def test_schema_accepts_a_hands_off_step_with_an_empty_find() -> None:
    assert _accepts_step(
        {"say": "Open System Settings first", "find": {},
         "expect": {"app": "System Settings"}})


def test_schema_accepts_a_step_that_only_expects_a_window() -> None:
    assert _accepts_step({"say": "Click General", "expect": {"window": "General"}})


def test_schema_accepts_app_frontmost_and_a_plain_find_label() -> None:
    assert _accepts_step(
        {"say": "Focus System Settings", "find": {"label": "System Settings"},
         "expect": {"app_frontmost": "System Settings"}})


def test_the_step_description_names_the_one_key_rule() -> None:
    item = plugin.GUIDE_START_SCHEMA["properties"]["steps"]["items"]

    assert item["required"] == ["say"], "find is optional; say is the only required key"
    assert not (item["properties"]["find"].get("required") or []), \
        "a find label is optional (hands-off steps pass {})"
    for key in ("app", "visible", "focus", "window", "app_frontmost"):
        assert key in item["properties"]["expect"]["properties"]
    assert "find/expect key" in item["description"]


def test_the_engine_accepts_the_step_shapes_the_schema_allows() -> None:
    """The schema fix must follow the engine, not the other way round."""
    hands_off = engine._normalize_step(
        {"say": "Open System Settings first", "find": {},
         "expect": {"app": "System Settings"}})
    assert hands_off == {"say": "Open System Settings first",
                         "expect": {"app": "System Settings"}}

    window_only = engine._normalize_step(
        {"say": "Click General", "expect": {"window": "General"}})
    assert window_only["expect"] == {"window": "General"}

    flat = engine._normalize_step(
        {"say": "Click Network", "find.label": "Network",
         "expect.window": "Network"})
    assert flat["find"] == {"label": "Network"}
    assert flat["expect"] == {"window": "Network"}


def test_the_engine_verifies_window_and_app_frontmost() -> None:
    from butler_core import capture

    snap = {"app": "System Settings", "window": "General — System Settings",
            "elements": [], "focused": None}
    assert capture.expect_pending({"window": "General"}, snap, None)["verified"] is True
    assert capture.expect_pending(
        {"app_frontmost": "System Settings"}, snap, None)["verified"] is True
    assert capture.expect_pending({"window": "Privacy"}, snap, None)["verified"] is False
    assert capture.expect_pending(
        {"app_frontmost": "Finder"}, snap, None)["verified"] is False


# -------------------------------------------------------------- plugin.yaml

def _manifest_text() -> str:
    return PLUGIN_YAML.read_text(encoding="utf-8")


def _manifest_default(text: str, key: str) -> float:
    match = re.search(
        rf"^  {key}:\n(?:    [^\n]*\n)*?    default: ([^\n]+)$", text, re.M)
    assert match, f"plugin.yaml has no default for {key}"
    return float(match.group(1))


def test_manifest_declares_the_three_live_config_keys_only() -> None:
    text = _manifest_text()

    for key in LIVE_CONFIG_KEYS:
        assert re.search(rf"^  {key}:$", text, re.M), f"{key} is declared"
        assert "plugin-data/butler/config.json" in _block(text, key)
    for key in DEAD_CONFIG_KEYS:
        assert not re.search(rf"^  {key}:$", text, re.M), f"{key} is not real"


def _block(text: str, key: str) -> str:
    match = re.search(rf"^  {key}:\n((?:    [^\n]*\n)*)", text, re.M)
    assert match, f"plugin.yaml has no block for {key}"
    return match.group(1)


def test_manifest_defaults_match_the_engine() -> None:
    text = _manifest_text()

    assert _manifest_default(text, "poll_seconds") == engine.DEFAULT_POLL
    assert _manifest_default(text, "step_timeout_seconds") == engine.DEFAULT_TIMEOUT
    assert _manifest_default(text, "stall_auto_stop_seconds") == engine.DEFAULT_STALL_AUTO_STOP
