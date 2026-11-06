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
- `expect`: any observable change; `{"app": "System Settings"}` for app opens,
  `{"visible": "..."}` for panes, `{"focus": "..."}` for selection changes.
- Status `capture_failed`: the OS read itself failed (missing grant). Fix the
  grant (doctor --ask) and restart; do NOT rewrite the plan.
- If `butler_guide_status` reports stalled: rewrite the stalled step with a
  better `find.label` and start a fresh short guide; never repeat the same
  failing step verbatim.
- When the guide ends: summarize what was completed; offer the next step.
