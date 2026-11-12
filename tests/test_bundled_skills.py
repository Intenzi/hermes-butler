"""Bundled plugin skills must ship: every skills/<name>/ dir is a valid
on-demand skill with frontmatter and the conventions the board runs on."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_SKILLS = {
    "butler-follow": {"when to use", "step format", "grounding rule", "follow"},
    "butler-usage": {"offer", "choose the verb", "never for shell commands"},
}


def _frontmatter(skill_md: Path) -> dict[str, str]:
    """Parse the simple scalar frontmatter block (starts at byte 0)."""
    text = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{skill_md} must open with a --- frontmatter block"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        if key and value:
            fields[key.strip()] = value.strip().strip('"')
    return fields


def test_bundled_skill_dirs_exist_and_ship() -> None:
    skills_dir = ROOT / "skills"
    names = {p.name for p in skills_dir.iterdir() if p.is_dir()}
    assert names == set(EXPECTED_SKILLS)


def test_each_bundled_skill_manifest_loads_on_demand() -> None:
    for name in EXPECTED_SKILLS:
        skill_md = ROOT / "skills" / name / "SKILL.md"
        assert skill_md.is_file(), f"missing {skill_md}"
        front = _frontmatter(skill_md)
        assert front.get("name") == skill_md.parent.name.removeprefix("butler-")
        description = front.get("description", "")
        assert len(description) >= 20, f"{skill_md}: description too short to trigger loads"


def test_plugin_registers_every_bundled_skill() -> None:
    """A skill dir the loader never registers via ctx.register_skill is dead
    weight: this is a Python (plugin.yaml) plugin, not a portable plugin.json
    package, so skills do NOT auto-discover from the skills/ dir."""
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'ctx\.register_skill\(\s*\n?\s*name="([^"]+)"', source))
    expected = {name.removeprefix("butler-") for name in EXPECTED_SKILLS}
    assert registered >= expected, f"plugin register(ctx) must register {expected}, found {registered}"
