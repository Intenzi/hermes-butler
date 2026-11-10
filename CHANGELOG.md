# Changelog

All notable changes to this plugin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
`plugin.yaml`.

## [Unreleased]

### Added

- LOOK door: ⌘⇧L hit-tests the element under the cursor through the
  system-wide Accessibility API and opens the ask field with that control as
  context (`Button · "Export" — Preview`). Submitting sends the question plus
  the app/role/label/window/box to the Butler chat without planning a guide.
- `butler_look` tool: a live capture of the app a LOOK question named, so the
  agent can answer with fresh neighbours without asking the user again.

## [0.2.0] - 2026-11-09

### Added

- Summon door: a ⌘⇧A floating bar and a 26x24 hat badge on the popped-out pet.
  Both open the same "Ask the butler" bar and destination picker.
- The hat badge is always on once the plugin is installed; there is no separate
  opt-in toggle.
- Event-driven badge placement from AX notifications (`AXWindowMoved`,
  `AXWindowResized`), coalesced to one placement per 40 ms.
- Two-pass capture: a shallow AX walk first, then one re-walk to depth 24 when
  the tree looks thin, for Electron/Chromium content.
- The plugin API process initialises the engine, so a badge click produces a
  summon row instead of a dead click.
- `state.json` writes merge across the two host processes instead of one
  clobbering the other.

### Changed

- The "Butler chat" destination resolves to the most recent chat titled
  `Butler`; it used to pick the oldest.
- The badge is a 26x24 icon at the pet's upper right, anchored to the sprite.
- `hermes butler doctor`, `capture`, and `status` now print their JSON reports;
  they were silently empty.

### Fixed

- The badge never appeared: the pet panel is titled "Hermes" and sits on
  CGWindow layer 3, so title-based and default-layer lookups missed it.
- The badge anchored to the empty pop-out window instead of the pet sprite,
  which put it well away from the pet.
- The borderless badge window could not become key, so clicks were swallowed.
- The badge covered the pet's speech bubble.
