# Demo script (90 seconds, no network, no screen recording)

Recording order matters: every scene must show something a screenshot cannot.
Keep pet lines short; the bubble truncates past 120 characters.

Setup before recording:

```bash
# a clean sheet: no stale guide, no stale watch
hermes butler doctor          # expect overlay: present, accessibility_granted: true
rm -f ~/.hermes/plugin-data/butler/state.json
```

Pop the pet out. Close every other window except the one the scene is about.

---

## 1. GUIDE — it refuses to lie (25 s)

**Say:** "guide me through turning Wi-Fi off and back on"

**What happens:** the pet rings the Wi-Fi row in System Settings, the checklist
shows `Step 1 of 3`. Deliberately click the **wrong** row (Bluetooth).

**On screen:** the ring stays on Wi-Fi, the checklist does not tick, the pet
keeps repeating the same step. Then click Wi-Fi correctly — only then does step 1
tick.

**Why it lands:** demos of this kind always show the happy path. This shows a
verification that *fails*, which is the whole difference between a walkthrough
and an animated list of instructions.

---

## 2. LOOK — state, not pixels (20 s)

**Say:** nothing. Right-click the hat → `Look at this…` → click the **disabled**
Export/Share button in a PDF in Preview.

**On screen:** screen dims, crosshair, badge reads `Button · "Export"`, then the
ask field opens pre-filled with
`App: Preview · Window: receipt.pdf · Control: Button "Export" (disabled)`.
Type "why can't I click this?" → pet answers in one line.

**Why it lands:** a screenshot cannot tell you a control is disabled, and the HUD
reads windows, not controls. This is the element-level read.

---

## 3. WATCH — it calls you back (25 s)

**Say:** "watch Terminal for BUILD OK"

```bash
sleep 12; echo BUILD OK      # run this in the watched Terminal window
```

**What happens:** switch away immediately (another app, not Terminal). At ~12 s
the pet says `⏲ Hit · Terminal: BUILD OK` and keeps saying it every 25 s.

**On screen:** the persistent hit line while you are in another app.

**Then:** right-click the hat → `Acknowledge watch hit` → the line clears, and
the watch is over.

**Why it lands:** it proves three things at once — content (not window titles),
background observation (you were elsewhere) and an acknowledgement that survives
being away.

---

## 4. THE HABIT — offer, don't spring it (15 s)

**Say:** "how do I allow screen recording for an app?"

**On screen:** the reply is steps, and it ends with
*"Want me to walk you through this on screen instead? Reply yes…"* plus the pet
saying `Need a walkthrough? Reply yes.`

**Why it lands:** it shows the product noticing its own moment to help, without
taking over the pointer uninvited.

---

## Cut list (if the video must be shorter)

- Scene 1 is load-bearing: keep it.
- Scene 4 can go if the voiceover says the line instead.
- Scenes 2 and 3 are the two capabilities nobody else has; keep both.

## What NOT to show

- The pane (looks like a settings panel).
- Any scene where a guide stalls — record only stalls you have caused on purpose.
- Terminal commands pasted into chat: the video is about the screen, not the CLI.
