---
name: follow
description: "Use when the user asks Hermes to fix/configure/oper-ate something on their Mac that needs clicking: drive a butler guide instead of listing instructions."
---

# Butler Follow mode

Hermes plans; the butler walks the plan on the user's screen. Every step is a
real control on their real display; the plugin verifies arrival from the OS
before advancing.

## When to use

- The user asks to change something accessible on macOS UI: settings, app
  preferences, browser pages, web forms, installer flows.
- The user asks "what am I looking at" style questions - use butler_capture
  first, answer, optionally offer a guide for the fix.

## When NOT to use

- Terminal/server work: no visible screen, the butler stays idle.
- Anything needing action *for* the user: typing passwords, confirming
  payments. The butler guides, the user's hands act.
- Content-finding tasks on web pages (find a section, read the docs, pull
  data from a portal): the agent should DRIVE THE BROWSER ITSELF instead -
  open the URL, read the DOM, locate the content, verify it - and only then
  ask the user whether they want a butler walkthrough of the route or a
  direct handover (page already open where it needs to be, or the exact
  URL). Butler walks take the user's hands and attention; for "where is X
  on this site" they are a slower, worse default than pointing at the answer.
- NEVER write `find.label` values for controls you have not observed live -
  not from memory, not from "typical Moodle layout". Observe in a DOM read
  (web) or butler_capture (native) FIRST, then plan. A guide whose labels
  were invented stalls the user at step 1 and costs two restart cycles.
  OCR is a fallback for the butler reading AX-gated content - never the
  planning input; for Chromium pages browser automation is the readable
  layer, not the butler.

## Step format (required quality)

- keep to a maximum of six steps; rewrite longer plans into a first guide plus
  a follow-up after user confirmation.
- `say`: imperative, one line, name the control exactly as it will look: not
  "open settings", but "Click Network".
- `find.label`: the exact expected control text; fall back with a second
  short guide if the capture reports a stall.
- Launch steps ("open System Settings first"): use `"find": {}` with no
  label - a hands-off step. The butler does not ring anything; it verifies
  the transition through `expect` (`{"app": "System Settings"}`) once the app
  is actually open. Never hunt for app icons inside other apps' windows.
- `expect`: any observable change (`expect` keys pass through unfiltered):
  `{"app": "..."}` for app opens (open counts, focus not required),
  `{"window": "..."}` for pane navigation — the frontmost window's TITLE
  taking the pane's name is the click-observable for settings-style panes
  (fires only after the user actually navigates),
  `{"visible": "..."}` for controls that should merely EXIST afterward
  (blink-risky for click steps: it verifies the moment the control appears,
  before the user acts - prefer `window` for those),
  `{"focus": "..."}` for keyboard-selection changes (AX focus is unreliable
  on sidebar rows and menu cells - prefer `window` there too).
- Status `capture_failed`: the OS read itself failed (missing grant). Fix the
  grant (doctor --ask) and restart; do NOT rewrite the plan.
- If `butler_guide_status` reports stalled: rewrite the stalled step with a
  better `find.label` and start a fresh short guide; never repeat the same
  failing step verbatim.
- ## Grounding rule (do not violate)

- Never guess `find.label`/`expect` text from memory or inference: read the
  real screen first with butler_capture (OCR fallback covers gated Chromium),
  or ask the user for the exact control/course name before starting a guide.
  Assumed names stall the guide and waste the user's time (an entire guide was
  wasted on a wrong course name: 'Final Year Project' was assumed when the
  actual Moodle card read 'Individual Project').
- If a capture cannot read the page, confirm names with the user before
  starting, not after a stall.
- When the guide ends: summarize what was completed; offer the next step.
