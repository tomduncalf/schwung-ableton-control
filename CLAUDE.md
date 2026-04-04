# Schwung Device Control

Two-way Ableton Live device parameter control for Ableton Move, with learn mode.

## Architecture

Two components communicate over MIDI cable 2 (USB-C):

```
Move (Schwung Tool Module)          Ableton Live (Remote Script)
src/ui.js                           ableton_remote_script/schwung_device.py
  - Display, knobs, LEDs              - Device traversal, param listeners
  - Learn mode UI                     - Learn mode (grabs selected_parameter)
  - SysEx/CC send/receive             - SHA1 param hashing, persistence
```

## MIDI Protocol

- **SysEx only** — the Standalone Port only passes SysEx, not CC/Note messages
- **SysEx header `F0 00 7D 01`** for all communication (values, names, learn, heartbeat)
- **Ableton MIDI port**: "Ableton Move (Standalone Port)" for both input and output
- Tick rate on Move is ~240fps (not 44 or 60), affects all timing constants

## Key Files

- `src/module.json` — Tool module metadata (`component_type: "tool"`)
- `src/ui.js` — Move-side JS: display drawing, encoder handling, SysEx framing, LED control
- `ableton_remote_script/__init__.py` — Ableton entry point
- `ableton_remote_script/schwung_device.py` — Ableton-side Python: ControlSurface subclass, device/param management
- `scripts/build.sh` — Package module tarball
- `scripts/install.sh` — Deploy to Move via SSH

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
- `_slot_page_memory`: last-visited page per slot per device (runtime only, not persisted)
- `_device_page_memory`: last page/slot per device hash, restored on device re-selection (runtime only)
- `_bindings`: the full bindings dict, loaded from/saved to bindings.json

### State (Move side — ui.js)

- `currentPage` / `pageCount` / `pageNames[0..7]`: slot-space values received from Ableton
- `slotSubpageCounts[0..7]` / `slotActiveSubpage[0..7]`: per-slot subpage info for tab indicators
- `paramNames[0..7]` / `paramValues[0..7]`: current knob labels and values
- `touchStack`: ordered list of currently touched knobs (for multi-touch)
- `connected` / `heartbeatTimer`: connection state (720-tick timeout)

## Input Mapping (Move side)

- **Knobs (CC 71-78, ch16):** parameter control with acceleration + discrete step handling
- **Knob touch (notes 0-7):** select knob for learn mode, show value overlay
- **Step buttons (notes 16-23):** switch slot, always sends CMD_PAGE_CHANGE to Ableton
- **Menu (CC 118):** toggle learn mode on Move side (also sends CMD_LEARN_START/STOP)
- **Main wheel (CC 14):** sequential page/subpage navigation (wraps around)
- **Left/Right (CC 119-120):** device navigation
- **Track 4 hold (CC 43):** device browser modifier — shows device list, step 1-8 selects device, arrows page through 8-device pages
- **Back (CC 120):** exit module

## Device Browser Mode

Hold Track 4 to enter device browser. Move requests device names via `CMD_DEVICE_LIST_REQUEST(offset)`, Ableton responds with `CMD_DEVICE_LIST_RESPONSE(offset, total, name1\0, name2\0, ...)`. Step buttons select a device (`CMD_DEVICE_SELECT(index)`). Left/right arrows page through groups of 8. Releasing Track 4 exits browse mode and restores normal display/LEDs.

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
