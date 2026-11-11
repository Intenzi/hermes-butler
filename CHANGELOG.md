## 0.4.0

- WATCH can scan content: `scan: {author, roles}` waits for a NEW message from a
  named author, matched against author-role nodes only (never message bodies),
  with "new since arming" decided from a baseline of match signatures.
- `scan.text` is the weaker explicit-content mode; `until` and `scan` are
  mutually exclusive.
- The build script is native by default with per-arch warm module caches: a
  Swift change takes ~7 s instead of ~90 s, and a no-op rebuild is free.

## 0.3.0

- LOOK: pointer-driven read of one control — right-click the hat for the menu,
  drag it to look, `Esc`/right-click to cancel. Drag and click share one inspect
  mode; clicks land on the overlay, never on the app underneath.
- WATCH: Accessibility-tree monitoring with a content condition, a persistent
  `⏲ Hit · …` line that survives the watch ending, acknowledgement from the hat,
  and a one-shot default.
- The hat exists without a popped-out pet (free anchor, remembered position).
- The companion is fetched from this repository's attested release, verified by
  SHA-256, and compiled only as a fallback.
- Doctor names the real remedy, never the wrong permission, and never "Xcode".

# Changelog

All notable changes to this plugin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
`plugin.yaml`.

## [Unreleased]

### Added

- LOOK door: ⌘⇧A hit-tests the element under the cursor through the
  system-wide Accessibility API and opens the ask field with that control as
  context (`Button · "Export" — Preview`). Submitting sends the question plus
  the app/role/label/window/box to the Butler chat without planning a guide.
- `butler_look` tool: a live capture of the app a LOOK question named, so the
  agent can answer with fresh neighbours without asking the user again.
- A watch hit now publishes a top-level `watch_hit` row that outlives the watch
  row: the pet keeps saying `⏲ Hit · <app>: <label>` every 25 s until the user
  acknowledges it with `butler_watch_ack`, `POST /watch/ack`, or the hat menu's
  new **Acknowledge watch hit** item.

### Changed

- Doctor names the right overlay remedy: a missing/non-executable helper says
  reinstall the plugin (never a compiler), while a local source-hash mismatch
  says rebuild with Command Line Tools. It no longer asks for a Screen Recording
  grant and reports `ocr` as `optional, not used`.

## [0.2.0] - 2026-11-09

### Added

- Look: drag the hat onto any control to read its role, label, app and window in
  place, with no chat and no typing.
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
