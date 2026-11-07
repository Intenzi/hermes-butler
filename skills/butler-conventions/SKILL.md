---
name: conventions
description: "Butler plugin dev conventions: flat step keys, restart batching, checklist page shape, TCC single-subject design. Load for butler plugin code work."
---

# Butler implementation conventions

Working rules accumulated across the butler plugin's development (guides,
engine, overlay, kanban board). Each section is a hard convention with a
concrete example; do not "improve" them without the user's explicit go-ahead.
Load this skill when a fresh session picks up a butler task from the kanban
`butler` board or edits butler plugin code — the sibling skill `butler:follow`
covers guide *authoring*; this one covers *how we build and coordinate*.

## 1. Flat step keys (no hierarchical step identifiers)

A guide step is ONE flat dict: `say` plus `find.*` and `expect.*` prefixed
keys, in a single level. Never invent nesting (`find: {label: ...}` is
tolerated for the hands-off special case only), never add step-id or
tree-shaped keys (`id`, `id: "1a"`, `step: "1a.i"`). Order and index carry
all structure — the checklist renders `Step 1:`, `Step 2:` from array order,
so hierarchical identifiers buy nothing and break the renderer.

```jsonc
// DO — one flat mapping per step
{"say": "Click Network",  "find.label": "Network", "expect.window": "Network"}
{"say": "Open System Settings first", "find": {}, "expect.app": "System Settings"} // hands-off: no ring, no label

// DON'T — nested ids / hierarchical shapes
{"id": "step-1a", "find": {"label": {"equals": "Network"}}}          // invented depth
{"say": "...", "substeps": [{"say": "..."}]}                        // hierarchy the engine never reads
```

Why it is hard: before the 2026-11-06 `_normalize_step` fix, a malformed
nested shape crashed `guide_start` at `steps[0]["find"]` and left an orphan
guide running silently (no say, no ring, no verify, no stall — the "pet said
nothing" failure). The normalizer folds flat `find.*` / `expect.*` keys and
rejects a step that could never verify ("no verifiable steps"), but it does
not read sub-keys two levels deep. Keep depth at exactly one.

## 2. Restart batching (never fix one-by-one)

A gateway restart loads ONLY what was on disk before it began. Dribbling
fixes across restart cycles makes the user re-test the old behavior and
report the bug as still broken — each restart costs a fresh grant-laden
boot and a user test cycle. Therefore: land the WHOLE changeset (code +
tests), run the test suite, then request exactly ONE restart and say what
it carries.

- Batch example — good: engine fix + overlay fix + skill copy edit on disk,
  tests green, one restart, REPORT before restart...
- One-by-one — bad: engine fix... restart... overlay fix... restart...
- Not a restart: plugin-data runtime knobs (`<plugin data
  dir>/config.json`: `poll_seconds`, `step_timeout_seconds`) are read fresh
  every poll tick with no restart; the desktop half (`desktop/plugin.js`)
  hot-reloads via the linked checkout (the app re-syncs on save — verify the
  copy actually refreshed with `cmp` instead of assuming).
- After one batched restart, re-verify before claiming: the restart that
  was supposed to load the fix can blow the 10 s plugin load budget
  (`plugins.load_timeout_seconds`, raised to 30 in config).

## 3. Checklist page shape (canonical)

The butler surface renders every guide as a vertical task checklist. This
shape is frozen by user decision — do not regress it to chip strips or an
always-expanded report.

LIVE guide:
- One row per step: 18px box + `Step N: <say>` text. Box states: `✓`
  verified (green outline), bright-bordered highlight = current step,
  empty = pending.
- The instruction line (current step's say) and any stall note render BELOW
  the checklist; the smoke counter ("pet.say smoke: N says fired (last
  HH:MM)") stays on the pane to distinguish plugin-side from
  renderer-side failure.

DONE / STOPPED guide:
- Collapses into a closed `<details>` dropdown per guide, newest first:
  `▸ <title> · N/M verified · <status>`. Up to 8 visible + a
  "show N more" footer; expanding shows the full completed checklist.

Empty state: exact string "No butler task running".

Mapping: a guide plan (max six steps per `butler:follow`) maps 1:1 onto
rows — flat step keys from convention 1 are what this shape consumes.

## 4. TCC single-subject design

macOS TCC trust attaches per binary, so a one-off fix that performs screen
access in whichever Python/Node binary happens to host it re-breaks grants
on every Hermes update or install (binary paths drift: command-tools python,
runtime venv, hash-built sugar). Launcher chains are worse — macOS names
the WRONG subject in the grant prompt.

Rules:
- ALL screen access (capture, AX reads) lives in the plugin's own bundled
  compiled Swift companion (`butler_core/services/ButlerOverlay` source →
  compiled at first use); Python `capture()` is a transport that talks to
  it. Port new screen-driven work INTO the companion, never into a second
  binary location.
- Grants name exactly one stable, plugin-relative subject: "Butler
  companion - the butler plugin's bundled helper", with no file paths, plus
  the one reason sentence ("Because the butler reads the screen through its
  own small helper, macOS asks once and remembers across updates.").
  Same wording in doctor fix strings and README.
- A probe running outside the granted subject LIES ABOUT ABSENCE — no
  Accessibility grant also yields empty window lists and "trusted: False".
  Check grants (doctor) before concluding a UI element does not exist.
- Shippability gate for every TCC or grant change: the walkthrough must
  survive `plugin install → grant → walkthrough` on a machine that has
  never seen the repo.
