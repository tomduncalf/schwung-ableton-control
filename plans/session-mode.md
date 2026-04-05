# Session Mode: Clip Launching/Stopping via Pad Grid

## Overview

Add a session mode to schwung-device-control that repurposes the 4x8 pad grid (notes 68-99) for clip launching and stopping, using the v3 framework's built-in `SessionComponent`. The user cycles between three pad modes: **off** (device control, no pads), **note** (PlayableComponent), and **session** (clip grid).

The 4x8 grid maps to **8 tracks x 4 scenes**, matching Move's physical layout (8 columns, 4 rows). The v3 `SessionComponent` handles clip slot assignment, launch on press, and color feedback through the skin system.

## 1. Changes to `elements.py`

**No new MIDI elements needed.** The existing `pads` matrix (notes 68-99, channel 0, 4 rows x 8 cols) is reused. In note mode it routes to `InstrumentComponent`; in session mode it routes to `SessionComponent.clip_launch_buttons`. The v3 modes system handles the swap.

Session navigation can be added later (arrows scroll session ring). Start with fixed 8 tracks x 4 scenes.

## 2. Changes to `__init__.py` (Specification)

```python
class Specification(ControlSurfaceSpecification):
    # ...existing...
    num_tracks = 8   # was 0 -- needed for session ring
    num_scenes = 4   # was 0 -- needed for session ring
```

This makes the v3 framework auto-create `SessionRingComponent` and `SessionComponent`. The session ring creates a highlight rectangle in Live's session view.

## 3. Changes to `mappings.py`

Extend `Note_Modes` to include a session sub-mode:

```python
mappings['Note_Modes'] = dict(
    enable=False,
    keyboard=dict(component='Instrument', matrix='pads'),
    session=dict(component='Session', clip_launch_buttons='pads'),
)
```

## 4. Mode Toggle

Replace boolean `noteMode` / `_note_mode` with tri-state `padMode` / `_pad_mode`:
- 0 = off (pads inactive)
- 1 = note (PlayableComponent)
- 2 = session (SessionComponent)

Up arrow cycles: off -> note -> session -> off.

Rename `CMD_NOTE_MODE (0x21)` to `CMD_PAD_MODE`, velocity encodes mode: vel=0+1 (off), 1+1 (note), 2+1 (session).

### `_set_pad_mode` implementation

```python
def _set_pad_mode(self, mode):
    self._pad_mode = mode
    if mode == 1:  # note
        self.set_can_auto_arm(True)
        self.set_can_update_controlled_track(True)
        self.component_map['Note_Modes'].selected_mode = 'keyboard'
        self._send_note_layout_info()
    elif mode == 2:  # session
        self.set_can_auto_arm(False)
        self.set_can_update_controlled_track(False)
        self.component_map['Note_Modes'].selected_mode = 'session'
    else:  # off
        self.component_map['Note_Modes'].selected_mode = None
        self.set_can_auto_arm(False)
        self.set_can_update_controlled_track(False)
```

## 5. Session Pad Color Feedback

The v3 `SessionComponent` uses the skin system for pad colors. Since the pads are `is_rgb=True` but we control Move LEDs via the existing `setLED()` protocol, we use a custom SysEx approach:

### New SysEx: `CMD_SESSION_GRID_COLORS (0x12)`
- **Direction:** Live -> Move
- **Format:** `F0 00 7D 01 12 <32 bytes> F7`
- Each byte = color index for one pad (0-31, mapping to notes 68-99)
- Color indices: 0=off, 1=green(playing), 2=red(recording), 3=yellow(stopped), 4=blink_green(triggered_play), 5=blink_red(triggered_record), 6=dim_red(armed_empty)

On the Ableton side, override clip state change handlers to send this SysEx. On the Move side, map color indices to LED colors.

## 6. Move-Side UI Changes (`src/ui.js`)

- Replace `noteMode` boolean with `padMode` (0/1/2)
- Up arrow cycles three modes
- Forward pad press/release in both note and session modes
- Handle `CMD_SESSION_GRID_COLORS` SysEx for pad LEDs
- Show "SESSION" indicator on display when padMode=2

## 7. New MIDI Commands

| Command | Direction | Encoding |
|---------|-----------|----------|
| `CMD_PAD_MODE (0x21)` | Move->Live | vel = mode+1 (0=off, 1=note, 2=session) |
| `CMD_SESSION_GRID_COLORS (SysEx 0x12)` | Live->Move | 32 color index bytes |

## 8. Implementation Sequence

### Phase 1: Core
1. Update Specification: `num_tracks=8`, `num_scenes=4`
2. Update mappings: add session sub-mode
3. Replace `_note_mode` with `_pad_mode` tri-state
4. Update Move side: mode cycling, pad forwarding for session
5. Add session grid color SysEx (custom, not via framework skin)

### Phase 2: Polish
6. Clip color support (map Live's color_index to LED colors)
7. Session navigation (arrows scroll tracks)
8. Scene launch (step buttons 1-4 in session mode)
9. Stop-all-clips gesture

## 9. Key Considerations

- **Auto-arm:** Session mode must NOT auto-arm (would change armed track on clip launch)
- **Session ring:** `num_tracks=8` creates a visible highlight in Live's session view
- **Pad sharing:** v3 modes system handles element ownership correctly between modes
- **Device control continues:** Knobs, pages, learn mode all work alongside session mode
- **Debounce color updates:** Don't send grid colors on every clip state change; batch per heartbeat
