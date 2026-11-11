---
name: usage
description: "Use when a reply is about to become step-by-step on-screen instructions: offer a butler guide instead of dead prose, and pick look/watch/guide for what the user is actually asking."
---

# When to use which butler verb

The plugin has three verbs and one habit. Pick by what the user needs, not by
what is easiest to write.

## The habit: never leave a screen procedure as dead prose

If the answer you are about to send is a sequence of clicks in an app — settings
panes, preferences, permission toggles, menu items, installers, web forms — end it
with one offer, in these words:

> Want me to walk you through this on screen instead? Reply **yes** and I'll start
> a guide on these steps.

Rules that make the offer honest:

- **Offer, never start.** The user asked a question; a guide takes over their
  pointer, so it waits for a "yes".
- **Offer per procedure, not per turn.** One offer per conversation unless the
  procedure is a different one. Never repeat it because a reply was long.
- **Never when a guide is already running** — that turn is the walkthrough.
- **Never for shell commands.** A list of `xcode-select --install` lines is not a
  screen procedure; the user pastes commands. Code fences never trigger an offer.
- **Skip the offer when the fix is genuinely an instruction** ("your app has no
  Settings window; use the menu bar instead") — there is nothing to point at.

When the user says yes, `butler_guide_start` from the steps you just wrote: same
order, same labels, one flat step per action (`$butler:follow` for the format).
Do not ask them to restate the procedure.

## Choose the verb

| The user is asking | Use |
|---|---|
| "Do it with me / show me / walk me through" | **guide** — plan and start it (`$butler:follow`) |
| "What is this / why is this disabled / what happens if I click this" | **look** — they point at the control; the answer comes back on the pet |
| "Tell me when X appears / finishes / messages me" | **watch** — arm a condition, do not sit and poll |
| A question whose answer is a paragraph | plain chat — no butler surface, no offer |

## What to say, briefly

- The pet speaks **one line under ~90 characters**. Detail belongs in the chat.
- When a look turn arrives with `[butler look] App: … Control: …`, that control is
  the subject: read the AX facts before answering, and treat a `StaticText` hit as
  prose in a page, not a control.
- Do not narrate which tool you called. Say what you found.

## Failure shapes worth recognising

- A guide that stalls means a label does not exist on screen: capture at depth 24,
  re-read the real label, and revise or skip the step — do not retry the same step
  and hope.
- A watch that stays silent is not proof nothing happened; `butler_watch_status`
  reads the event tail, which says what was actually seen.
- A look answer that keeps landing on the plugin's own window means the descriptor
  was refused by the companion — say the read failed instead of inventing a control.
