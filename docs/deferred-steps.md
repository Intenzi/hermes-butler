# Deferred steps

Status: design note, not implemented. Proposed for after the hackathon
submission. Nothing in this document ships in 0.2.0.

## Problem

A guide is planned from one capture, and that capture is sometimes blind: a
native app whose AX tree is thin, or a login-gated page where the plan is made
before the user is signed in. The plan then names a control the butler cannot
find. Today the step stalls, which is honest but leaves the user stuck and
costs a round trip back to chat to re-plan.

## Mechanism

After the plan capture, every step is classified:

- **GROUNDED** — a control was actually seen: a label and a box.
- **DEFERRED** — intent only: the step says what to do, but no control matched
  in the capture.

A deferred step resolves at run time through a local ladder, with no model
call:

1. exact label match;
2. fuzzy label match constrained by role;
3. OCR text when the Accessibility tree is empty;
4. window or panel title as the last anchor.

Only a full miss on that ladder, or a step that fails verification twice,
escalates — once — to the agent, with the freshly captured screen. The agent
then re-plans the next one or two steps, not the whole guide.

## Hard limits

- At most two escalations per guide.
- The step index never rewinds; a re-plan replaces upcoming steps only.
- When the escalation budget is exhausted, the guide stops and hands back to
  chat with the current screen attached, rather than looping.

## Why it does not raise compute

A correct plan pays nothing: grounded steps resolve exactly as they do now.
A wrong plan pays one bounded model call, instead of a blind re-plan after the
user has already stalled.

A route memo removes repeat planning entirely: keyed on app bundle, window
signature, and step intent, it stores the resolved `find`/`expect` from the
last successful run. The memo is invalidated when the app version changes, so
a stale route cannot outlive the UI it described.

## Status

Not implemented. No code, schema, or config key exists for deferred steps in
this release.
