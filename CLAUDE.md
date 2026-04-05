# Schwung Device Control

Two-way Ableton Live device parameter control for Ableton Move, with learn mode and note-playing grid.

## Architecture

Two components communicate over MIDI cable 2 (USB-C):

```
Move (Schwung Tool Module)          Ableton Live (Remote Script, v3)
src/ui.js                           ableton_remote_script/
  - Display, knobs, LEDs              - schwung_device.py: ControlSurface, device/param mgmt
  - Learn mode UI                     - keyboard.py: InstrumentComponent (PlayableComponent)
  - Note mode (pad forwarding)        - melodic_pattern.py: scale-aware note grid math
  - SysEx/CC send/receive             - elements.py: pad matrix, v3 ElementsBase
                                       - mappings.py: v3 declarative wiring
```

## Framework

The remote script uses **Ableton v3 control surface framework** (`ableton.v3.control_surface.ControlSurface`) with a `Specification` class in `__init__.py`. This enables:
- `PlayableComponent` / `InstrumentComponent` for scale-aware note playing
- Declarative element + mapping system
- Auto-arming for note input to armed tracks
- Future reuse of other v3 components (session, step sequencer, etc.)

The existing device control logic (bindings, learn mode, persistence) runs via method overrides (`receive_midi`, `build_midi_map`) on top of the v3 base class.

## MIDI Protocol

Two channels on "Ableton Move (Standalone Port)":

### Channel 16 (command protocol)
- **CC 0-7** — bidirectional knob values. Move sends on encoder turn, Ableton sends for LED feedback.
- **Note On (0x90)** — command protocol. Note number = command ID, velocity = value (+1 offset to avoid zero). Used for heartbeat, learn mode, device nav, page changes, note mode toggle, octave, etc.
- **SysEx (`F0 00 7D 01`)** — variable-length data (device/param names, page info, value strings, device lists, note layout info). Ableton→Move only.

### Channel 1 (pad mode: note or session)
- **Note On/Off (68-99)** — pad press/release forwarded from Move to Ableton. In note mode, routed to PlayableComponent (scale-aware notes to armed track). In session mode, routed to SessionComponent (clip launch/stop).

Tick rate on Move is ~240fps (not 44 or 60), affects all timing constants.

**SysEx 0x00 bytes are unsafe.** USB-MIDI SysEx transport strips or truncates `0x00` bytes in payloads. All SysEx data values must be offset by +1 on send and -1 on receive to avoid zeros. This applies to Note On velocity too (already done). If adding new SysEx commands with numeric payloads, always apply the +1 offset.

## Key Files

- `src/module.json` — Overtake module metadata
- `src/ui.js` — Move-side JS: display, input handling, note mode, LED control
- `ableton_remote_script/__init__.py` — v3 entry point, Specification, capabilities
- `ableton_remote_script/schwung_device.py` — ControlSurface: device control, bindings, pad modes
- `ableton_remote_script/elements.py` — v3 ElementsBase: 4x8 pad matrix (notes 68-99, ch1)
- `ableton_remote_script/keyboard.py` — InstrumentComponent + NoteLayout (scale-aware grid)
- `ableton_remote_script/melodic_pattern.py` — MelodicPattern, Scale, NoteInfo (ported from Move)
- `ableton_remote_script/mappings.py` — v3 create_mappings (Note_Modes → Instrument/Session)
- `ableton_remote_script/skin.py` — Skin for pad colors
- `ableton_remote_script/colors.py` — RGB color constants
- `scripts/build.sh` — Package module tarball
- `scripts/install.sh` — Deploy to Move via SSH

## Pad Modes

Press **Up arrow** to cycle pad modes: **off → note → session → off**.

All device control (knobs, pages, learn) continues working in all pad modes.

### Note Mode (padMode=1)
- Pads (4x8 grid) play scale-aware notes via Ableton's PlayableComponent
- **Shift+Up** = octave up, **Shift+Down** = octave down
- Pad LEDs show scale coloring (green=root, grey=in-scale, off=out)
- Notes route to the auto-armed track in Live

### Session Mode (padMode=2)
- Pads launch/stop clips in an 8-track x 4-scene grid via SessionComponent
- Auto-arm is disabled (clip launching shouldn't change armed track)
- Session ring (8x4) is visible in Live's session view

**Session grid rendering is custom** — it does not use the v3 framework's skin/color system. The flow:
1. `_send_session_grid_colors()` in `schwung_device.py` iterates `song.tracks` (cols 0-7) × `song.scenes` (rows 0-3), checks each clip slot's state (playing/recording/stopped/triggered/armed-empty), and packs 32 color index bytes into a `CMD_SESSION_GRID_COLORS` SysEx message (values +1 offset for SysEx safety).
2. `ui.js` receives the SysEx, subtracts 1 from each byte, and stores in `sessionGridColors[0..31]`.
3. `updateSessionPadLEDs()` maps each color index through `SESSION_LED_MAP` (index→LED color) and calls `setLED()`. Rows are flipped vertically so scene 0 (data row 0) displays on the top physical pad row (notes 92-99).
4. Listeners on `has_clip`, `playing_status`, `is_triggered`, and `arm` trigger `_send_session_grid_colors()` automatically when clip state changes.

### Pad Mode Commands
- `CMD_PAD_MODE (0x21)`: Move→Live, vel=mode+1 (0=off, 1=note, 2=session)
- `CMD_OCTAVE (0x22)`: Move→Live, vel=1+1 up, vel=0+1 down (note mode only)

Set pages (slot 9) are accessible even with no active device — they resolve params cross-device.
- `CMD_NOTE_LAYOUT_INFO (SysEx 0x11)`: Live→Move, root_note + is_in_key + interval + scale_notes
- `CMD_SESSION_GRID_COLORS (SysEx 0x12)`: Live→Move, 32 color index bytes

## Data Model

### bindings.json

Persisted at `ableton_remote_script/bindings.json`. Keyed by device hash (SHA1 of `class_name:device_name`).

```json
{
  "device_hash": {
    "pages": [
      {"name": "Filter", "slot": 0, "knobs": [{"param_index": 5, "param_name": "Cutoff", "short_name": "Cut"}, null, null, null, null, null, null, null]},
      {"name": "LFO", "slot": 1, "knobs": [...]},
      {"name": "Env", "slot": 1, "knobs": [...]}
    ]
  }
}
```

- Each page has a `slot` (0-7) indicating which step button it lives on
- Multiple pages can share a slot — pressing the step button cycles through them
- `param_name` is used for resolution (name match first, then `param_index` fallback)
- `short_name` is what's displayed on Move (editable in JSON, defaults to param_name)
- `deviceName` is stored alongside `pages` for human-readable device identification

### Conditional Bindings

A knob binding can be an array of candidates with `"if"` conditions. First matching condition wins; entry without `"if"` is the default fallback. Conditions compare `str(param)` (Ableton's display string) using `==` or `!=`.

```json
"knobs": [
  [
    {"param_name": "LFO 2 S. Rate", "short_name": "Rate", "param_index": 25, "if": "LFO 2 Sync == On"},
    {"param_name": "LFO 2 Rate", "short_name": "Rate", "param_index": 24}
  ],
  null, null, null, null, null, null, null
]
```

When the condition parameter changes, bindings re-apply automatically.

### Slots vs Pages

**Slots** = step buttons (0-7), what Move sees. **Pages** = entries in the pages array, what Ableton manages internally. Move only knows about slots — Ableton translates.

- `_current_page`: index into the pages array (Ableton internal)
- `_slot_page_memory`: `{device_hash: {slot: page_index}}` — remembers last sub-page per slot
- CMD_PAGE_INFO sends `[current_slot, slot_count]` (not page index/count)
- CMD_PAGE_NAME sends the active sub-page's name per slot position
- CMD_SLOT_SUBPAGE_INFO sends per-slot `[subpage_count, active_subpage_index]` (offset +1 for SysEx safety)

### State (Ableton side — schwung_device.py)

- `_selected_device` / `_device_list` / `_device_index`: current device context
- `_current_page`: active page array index (reset to 0 on device change)
- `_active_params[0..7]`: live parameter objects bound to each knob
- `_active_listeners[0..7]`: value change listeners for Live→Move sync
- `_learn_mode`: whether learn mode is active
- `_pad_mode`: current pad mode (0=off, 1=note, 2=session)
- `_slot_page_memory`: last-visited page per slot per device (runtime only, not persisted)
- `_device_page_memory`: last page/slot per device hash, restored on device re-selection (runtime only)
- `_bindings`: the full bindings dict, loaded from/saved to bindings.json

### State (Move side — ui.js)

- `currentPage` / `pageCount` / `pageNames[0..7]`: slot-space values received from Ableton
- `slotSubpageCounts[0..7]` / `slotActiveSubpage[0..7]`: per-slot subpage info for tab indicators
- `paramNames[0..7]` / `paramValues[0..7]`: current knob labels and values
- `touchStack`: ordered list of currently touched knobs (for multi-touch)
- `connected` / `heartbeatTimer`: connection state (720-tick timeout)
- `padMode`: current pad mode (0=off, 1=note, 2=session)
- `noteLayoutRoot` / `noteLayoutScaleNotes`: scale info from Ableton for pad coloring
- `sessionGridColors[0..31]`: clip state color indices from Ableton

## Input Mapping (Move side)

- **Knobs (CC 71-78, ch16):** parameter control with acceleration + discrete step handling
- **Knob touch (notes 0-7):** select knob for learn mode, show value overlay
- **Step buttons (notes 16-23):** switch slot (0-7), always sends CMD_PAGE_CHANGE to Ableton
- **Fav buttons (notes 24-27):** 4 subpages on slot 8 (* 1 through * 4)
- **Set buttons (notes 28-31):** 4 subpages on slot 9 (S 1 through S 4)
- **Pads (notes 68-99):** in note/session mode, forwarded as Note On/Off on channel 1 (note→PlayableComponent, session→SessionComponent)
- **Menu (CC 118):** toggle learn mode on Move side (also sends CMD_LEARN_START/STOP)
- **Up arrow:** cycle pad mode off→note→session→off (CMD_PAD_MODE)
- **Shift+Up/Down:** octave up/down (CMD_OCTAVE, note mode only)
- **Main wheel (CC 14):** sequential page/subpage navigation (wraps around)
- **Left/Right (CC 119-120):** device navigation
- **Row 4 (CC 40, bottom):** device browser — short press toggles, long press momentary. Step 1-8 selects device, arrows page through 8-device groups
- **Row 3 (CC 41):** track browser — short press toggles, long press momentary. Step 1-8 selects track, arrows page through 8-track groups
- **Back (CC 120):** exit module

## UI Feedback

Use `showFeedback(text, ticks=180)` in `ui.js` to display a brief centered toast message (bordered box, ~0.75s at 240fps). Use it for user-initiated actions that need confirmation — mode changes, saves, adds, errors, etc. It sets `needsRedraw` automatically.

## Device Browser Mode

Press Row 4 (bottom) to enter device browser. Short press toggles it on/off; long press (~300ms) is momentary (active while held). Move requests device names via `CMD_DEVICE_LIST_REQUEST(offset)`, Ableton responds with `CMD_DEVICE_LIST_RESPONSE(offset, total, name1\0, name2\0, ...)`. Step buttons select a device (`CMD_DEVICE_SELECT(index)`). Left/right arrows page through groups of 8.

## Track Browser Mode

Press Row 3 to enter track browser. Same toggle/momentary behavior as device browser (HoldToggle). Move requests track names via `CMD_TRACK_LIST_REQUEST(offset)`, Ableton responds with `CMD_TRACK_LIST_RESPONSE(offset, total, current_track_index, name1\0, name2\0, ...)`. Step buttons select a track (`CMD_TRACK_SELECT(index)`). Left/right arrows page through groups of 8. Device and track browsers are mutually exclusive.

## Learn Mode Flow

1. User presses Menu → learn mode on (both sides notified)
2. User touches knob on Move → CMD_LEARN_KNOB sent with knob index
3. Ableton grabs `song().view.selected_parameter`, stores binding on current page
4. Ableton sends CMD_LEARN_ACK with param name → Move updates display
5. Cycling past last sub-page on a slot creates a provisional empty page
6. On learn mode exit, provisional pages with no bindings are discarded

## Build & Deploy

```bash
# Build module tarball
./scripts/build.sh

# Deploy to Move
./scripts/install.sh

# Ableton Remote Script (preserves bindings.json)
./scripts/install_remote_script.sh
# Then configure in Ableton Preferences > MIDI as control surface
# using "Ableton Move (Standalone Port)" for input and output.
```

## Debugging

```bash
# Move logs (enable first)
ssh ableton@move.local "touch /data/UserData/schwung/debug_log_on"
ssh ableton@move.local "tail -f /data/UserData/schwung/debug.log"

# Ableton logs
grep -i schwung ~/Library/Preferences/Ableton/Live\ */Log.txt | tail -20
```

Module logs are prefixed `[DC]`.

## Quick Deploy

After any change to `src/ui.js` or `src/module.json`, deploy to Move with:
```bash
./scripts/build.sh && ./scripts/install.sh && ssh root@move.local "/etc/init.d/move stop && /etc/init.d/move start"
```
Always run this after editing Move-side code.

After any change to `ableton_remote_script/`, deploy to Ableton with:
```bash
./scripts/install_remote_script.sh
```
Then restart Ableton. This preserves `bindings.json` if present.

## Reference

- **Official Move remote script** (decompiled): `../../ableton_remote_scripts/Move/` — the script we're replicating; shows how Move handles note modes, auto-arm, instrument/drum switching, etc.
- **All built-in MIDI Remote Scripts** (decompiled): `../../ableton_remote_scripts/AbletonLive12_MIDIRemoteScripts/` — reference for v3 framework patterns (elements, mappings, components, etc.). ATOM is a good simple v3 example.
