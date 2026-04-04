# Tool Hide/Show Toggle for Schwung Device Control

## Context

We want Device Control to be able to temporarily hide itself and let normal Move functionality show through, while staying loaded and connected to Ableton. A specific button combo would toggle it back. This avoids the overhead of reloading the tool every time the user wants to switch between normal Move and device control.

## What Schwung Supports

### `shadow_set_overtake_mode(mode)` — runtime toggle
- **Mode 0** = normal Move (firmware UI visible, hardware responsive)
- **Mode 1** = menu (only jog/click/back forwarded)
- **Mode 2** = module (full overtake, current behavior)

Calling `shadow_set_overtake_mode(0)` from our module:
- `tick()` keeps running (can maintain heartbeat, timers)
- `onMidiMessageInternal` stays alive but **only receives**: jog (CC 14), click (CC 3), back (CC 51), track buttons (40-43), knobs (71-78)
- Pads, step buttons, shift, menu, arrows — NOT forwarded in mode 0
- `onMidiMessageExternal` — unclear if SysEx from Ableton continues in mode 0 (needs testing)
- Move's display and LEDs fully restored to normal

### `host_hide_module()` — full teardown
- Tears down JS callbacks entirely (tick, MIDI handlers)
- Returns to tools menu with "Resume" option
- DSP stays loaded but UI is gone
- NOT suitable for our use case (can't watch for a key to come back)

## Recommended Approach

Use `shadow_set_overtake_mode()` to toggle between mode 0 (hidden) and mode 2 (active).

### Toggle trigger: double-tap Back
Since mode 0 only forwards jog, click, back, track buttons, and knobs — the toggle must use one of these.

**Double-tap Back** (two presses within ~400ms):
- In mode 2 (active): double-tap Back → hide (mode 0)
- In mode 0 (hidden): double-tap Back → show (mode 2)
- Single Back press: normal behavior (exit learn mode, or exit tool)

Implementation: track `lastBackTime`. On Back press, if `Date.now() - lastBackTime < 400`, it's a double-tap → toggle. Otherwise store time and let single press handle normally.

### Hidden state behavior
- `tick()` continues running — maintain heartbeat with Ableton
- Display: Move's native UI shows (we stop drawing)
- LEDs: Move's native LEDs show (we stop setting them)
- When toggling back to mode 2: redraw display, restore LEDs, send REQUEST_STATE to Ableton

## Critical Unknown

**Does `onMidiMessageExternal` still work in mode 0?** Our SysEx communication with Ableton (heartbeat, value updates) flows through this handler. If it stops, we'd lose the connection and need to re-handshake on toggle back.

This MUST be tested before implementing. If external MIDI stops:
- We can still maintain minimal state (the Ableton script keeps running independently)
- On toggle back: send HELLO/REQUEST_STATE to reconnect

## Implementation

### Changes to `src/ui.js`:

```
State:
  let hidden = false;
  let lastBackTime = 0;
  const DOUBLE_TAP_MS = 400;

Back button handler (in handleInternalCC):
  const now = Date.now();
  if (now - lastBackTime < DOUBLE_TAP_MS) {
    // Double-tap: toggle hidden
    if (hidden) → unhide()
    else → hide()
    lastBackTime = 0;  // reset so triple-tap doesn't re-trigger
    return;
  }
  lastBackTime = now;
  // Single press: existing behavior (exit learn, or exit tool)
  // Use a short delay (~400ms) before executing single-press action
  // to avoid triggering exit on the first tap of a double-tap

hide():
  shadow_set_overtake_mode(0);
  hidden = true;

unhide():
  shadow_set_overtake_mode(2);
  hidden = false;
  initLEDs();
  updateKnobLEDs();
  updateNavLEDs();
  sendCommand(CMD_REQUEST_STATE, []);
  needsRedraw = true;

tick():
  If hidden: only maintain heartbeat timer, skip display/LED updates

onMidiMessageInternal():
  If hidden: only check for Back button double-tap, ignore everything else
```

### No changes needed to Ableton script
The Ableton side keeps sending heartbeats and value updates. If the Move module misses some during hidden mode, REQUEST_STATE on unhide will resync.

## Files to modify
- `src/ui.js` — add hidden state, toggle on Back, skip drawing when hidden

## Verification
1. Load tool, connect to Ableton, learn some params
2. Double-tap Back — Move's normal UI should appear, pads/knobs work normally
3. Double-tap Back again — tool UI returns with device/params intact
4. Check Ableton log — no errors during toggle
5. Test: change a param in Ableton while hidden, verify value updates on unhide
