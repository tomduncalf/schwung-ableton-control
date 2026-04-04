# Session Grid Port Plan

Port the session grid clip-launching functionality from `ableton_script/` into `schwung-device-control/`, adding clip launch/stop alongside the existing device parameter control.

## Context & Constraints

**What the original does**: The Move control surface script uses Ableton's v3 `SessionComponent` framework to manage a 7-track × 4-scene clip grid. Clip states (stopped, playing, recording, triggered) are reflected on Move's hardware pads via MIDI note colors. The heavy lifting is in Ableton's base classes — the Move script mostly configures skin colors and wires elements.

**What we have**: schwung-device-control already has a working SysEx protocol (`F0 00 7D 01 <CMD> ...`) over the Standalone Port, a 128×64 1-bit OLED display on Move, 32 pads (4×8 grid), 8 step buttons, nav buttons, and 8 knob encoders. The existing module handles device parameter control with learn mode.

**Key constraints**:
- Standalone Port only passes SysEx (no CC/Note) — all state must be SysEx-framed
- Move display is 128×64 monochrome — no color pads, so clip state must be conveyed via display graphics and pad LED colors
- The existing device-control functionality should remain — session grid is an additional mode
- Move pads send/receive on specific MIDI note numbers — need to check what's available from Schwung's `raw_midi` API
- Pad LEDs on Move support color (same color indices as `ableton_script/colors.py`)

## Architecture Overview

```
┌─ Move (ui.js) ──────────────────┐     ┌─ Ableton (schwung_device.py) ──┐
│                                  │     │                                 │
│  Mode: DEVICE | SESSION          │     │  Session tracking:              │
│                                  │     │  - Listen to clip slots         │
│  SESSION mode:                   │     │  - Track playing/recording/     │
│  - Display: grid of clip names   │     │    triggered/stopped states     │
│  - Pads: launch/stop clips       │     │  - Track colors                 │
│  - Step buttons: scene launch    │     │  - Clip names                   │
│  - Nav: scroll tracks/scenes     │     │  - Scene names                  │
│  - Knobs: unused or volume/send  │     │                                 │
│                                  │     │  Send state snapshots via SysEx │
│  Pad LEDs: clip state colors     │     │  Receive launch/stop commands   │
│  (green=playing, red=recording,  │     │                                 │
│   track color=stopped, etc.)     │     │                                 │
└──────────────────────────────────┘     └─────────────────────────────────┘
```

## Phase 1: Ableton-Side Session State Tracking

**Goal**: Add session state observation to `schwung_device.py` and expose it over SysEx.

### 1a. Track the session grid

Use the Live API (`song().tracks`, `song().scenes`, `track.clip_slots`) to observe a window of the session:

- **Grid size**: 8 tracks × 4 scenes (matches Move's 4×8 pad grid, or 8×4 depending on orientation)
- **Track offset / scene offset**: scrollable via nav commands
- Listen to:
  - `clip_slot.has_clip` — whether a slot contains a clip
  - `clip_slot.clip.name` — clip name (if has_clip)
  - `clip_slot.clip.color_index` — clip/track color
  - `clip_slot.clip.is_playing` — currently playing
  - `clip_slot.clip.is_recording` — currently recording
  - `clip_slot.clip.is_triggered` — queued to play/record
  - `clip_slot.is_triggered` — slot-level trigger (for empty recording)
  - `track.color_index` — track color for empty slots
  - `track.fired_slot_index` — which slot is playing/triggered
  - `song().view.selected_track` / `song().view.selected_scene` — selection tracking

### 1b. Define new SysEx commands

Extend the existing command set with session-specific messages:

**Ableton → Move**:
| CMD | Name | Data | Description |
|-----|------|------|-------------|
| `0x20` | CMD_SESSION_GRID | 32 × (state, color_idx) | Full grid snapshot: 8 tracks × 4 scenes, each slot = 1 byte state + 1 byte color |
| `0x21` | CMD_CLIP_NAME | track, scene, string | Name of a specific clip |
| `0x22` | CMD_SCENE_NAME | scene, string | Scene name for display |
| `0x23` | CMD_TRACK_NAME | track, string | Track name for display |
| `0x24` | CMD_SESSION_UPDATE | track, scene, state, color | Single slot state change (incremental) |
| `0x25` | CMD_GRID_OFFSET | track_offset, scene_offset | Current scroll position |

**Move → Ableton**:
| CMD | Name | Data | Description |
|-----|------|------|-------------|
| `0x30` | CMD_LAUNCH_CLIP | track, scene | Launch clip at grid position |
| `0x31` | CMD_STOP_TRACK | track | Stop track's playing clip |
| `0x32` | CMD_LAUNCH_SCENE | scene | Launch entire scene |
| `0x33` | CMD_STOP_ALL | — | Stop all clips |
| `0x34` | CMD_SCROLL_GRID | direction | Scroll grid (0=up, 1=down, 2=left, 3=right) |
| `0x35` | CMD_SESSION_MODE | 0/1 | Enter/exit session mode |

**Clip state byte encoding**:
```
0x00 = empty (no clip)
0x01 = stopped (has clip, not playing)
0x02 = playing
0x03 = recording
0x04 = triggered_play (queued)
0x05 = triggered_record (queued)
```

### 1c. Listener architecture

```python
class SessionTracker:
    GRID_TRACKS = 8
    GRID_SCENES = 4
    
    def __init__(self, song, send_sysex_fn):
        self._song = song
        self._send = send_sysex_fn
        self._track_offset = 0
        self._scene_offset = 0
        self._grid_state = [[0]*4 for _ in range(8)]  # cached state
        self._listeners = []  # cleanup list
    
    def attach(self):
        # Listen to track list changes, scene list changes
        # For each visible slot, attach clip listeners
        # On any change, compute new state, diff against cache, send updates
    
    def detach(self):
        # Remove all listeners
    
    def scroll(self, direction):
        # Adjust offset, re-attach listeners, send full grid
    
    def _slot_state(self, clip_slot):
        # Return state byte for a clip slot
    
    def _send_full_grid(self):
        # Send CMD_SESSION_GRID with all 32 slots
    
    def _send_slot_update(self, track, scene, state, color):
        # Send CMD_SESSION_UPDATE for single slot change
```

### 1d. Incremental updates

Rather than sending the full grid on every change, diff against the cached state and send `CMD_SESSION_UPDATE` for individual slot changes. Send `CMD_SESSION_GRID` only on:
- Initial connection (CMD_HELLO)
- Grid scroll
- Track/scene list changes (structural changes)

This keeps SysEx traffic manageable.

## Phase 2: Move-Side Session Grid UI

**Goal**: Render the session grid on Move's OLED and handle pad input for clip launching.

### 2a. Mode switching

Add a mode toggle between DEVICE mode (existing) and SESSION mode:

- **Toggle mechanism**: One of the step buttons (e.g. step button 8 / rightmost) or a dedicated button combo
- When entering SESSION mode, send `CMD_SESSION_MODE(1)` to Ableton → triggers full grid send
- When exiting, send `CMD_SESSION_MODE(0)` → Ableton can stop sending grid updates

### 2b. Display layout

128×64 pixel monochrome display. Proposed layout:

```
┌────────────────────────────────┐
│ Track1  Track2  Track3  Track4 │  ← track names (row 0, 10px)
│ ┌────┐ ┌────┐ ┌────┐ ┌────┐  │
│ │clip│ │clip│ │    │ │clip│  │  ← scene 1 (row ~12-24)
│ └────┘ └────┘ └────┘ └────┘  │
│ ┌────┐ ┌────┐ ┌────┐ ┌────┐  │
│ │clip│ │▶cls│ │    │ │clip│  │  ← scene 2 (row ~26-38)
│ └────┘ └────┘ └────┘ └────┘  │
│ ┌────┐ ┌────┐ ┌────┐ ┌────┐  │
│ │clip│ │clip│ │    │ │clip│  │  ← scene 3 (row ~40-52)
│ └────┘ └────┘ └────┘ └────┘  │
│ Sc1   Sc2   Sc3   Sc4   ◀▶  │  ← scene names / scroll indicators
└────────────────────────────────┘
```

**4 tracks × 4 scenes** visible at once (fits 32px per column, 4 rows of ~12px cells). Each cell:
- Empty slot: empty box (thin border)
- Has clip, stopped: filled box or clip name text
- Playing: inverted (white bg, black text) + playhead indicator (▶)
- Recording: inverted + "●" indicator
- Triggered: blinking (alternate frames)

Alternatively, **8 tracks × 4 scenes** if using the full pad grid and showing minimal text — just state indicators in a tighter grid.

### 2c. Pad mapping

Move has 32 pads in a 4-row × 8-column physical layout. Map to session grid:

**Option A — 4×8 (4 scenes × 8 tracks)**:
- Row 0 (pads 0-7) = Scene 1, tracks 1-8
- Row 1 (pads 8-15) = Scene 2, tracks 1-8
- Row 2 (pads 16-23) = Scene 3, tracks 1-8
- Row 3 (pads 24-31) = Scene 4, tracks 1-8
- Matches Ableton's session view orientation

**Option B — 8×4 (8 scenes × 4 tracks)** — more scenes visible, fewer tracks

Recommend **Option A** (4 scenes × 8 tracks) — matches Push/Live layout conventions and Move's physical grid.

### 2d. Pad LED colors

Use Move's LED color system (same indices as `ableton_script/colors.py`):

| Clip State | Pad LED |
|-----------|---------|
| Empty | Off (0) |
| Stopped (has clip) | Track/clip color (static) |
| Playing | White/green pulsing |
| Recording | Red pulsing |
| Triggered play | Green blinking |
| Triggered record | Red blinking |

The existing `colors.py` color indices and animation channels (pulse channel 6, blink channel 11) should work from Schwung's raw MIDI — need to verify pad LED control via `move_pad_set_color()` or raw note output.

### 2e. Pad input handling

```javascript
// In SESSION mode, pad press → launch clip
function onPadPress(padIndex) {
    const track = padIndex % 8;
    const scene = Math.floor(padIndex / 8);
    
    if (gridState[track][scene].state === STATE_EMPTY) {
        // Could trigger record, or ignore
        return;
    }
    
    if (gridState[track][scene].state === STATE_PLAYING) {
        // Stop this track
        sendSysex(CMD_STOP_TRACK, track);
    } else {
        // Launch clip
        sendSysex(CMD_LAUNCH_CLIP, track, scene);
    }
}
```

### 2f. Navigation

- **Left/Right nav buttons**: Scroll track offset (±1 or ±8)
- **Up/Down** (plus/minus buttons or shift+nav): Scroll scene offset
- **Step buttons 1-4**: Scene launch (launch entire scene row)
- **Step buttons 5-7**: Could map to stop track, stop all, etc.

## Phase 3: Ableton-Side Launch/Stop Handling

### 3a. Receive and execute commands

```python
def _handle_session_command(self, cmd, data):
    if cmd == CMD_LAUNCH_CLIP:
        track_idx = data[0] + self._track_offset
        scene_idx = data[1] + self._scene_offset
        track = song.tracks[track_idx]
        slot = track.clip_slots[scene_idx]
        slot.fire()  # Launch clip (respects global quantization)
    
    elif cmd == CMD_STOP_TRACK:
        track_idx = data[0] + self._track_offset
        song.tracks[track_idx].stop_all_clips()
    
    elif cmd == CMD_LAUNCH_SCENE:
        scene_idx = data[0] + self._scene_offset
        song.scenes[scene_idx].fire()
    
    elif cmd == CMD_STOP_ALL:
        song.stop_all_clips()
    
    elif cmd == CMD_SCROLL_GRID:
        self._session_tracker.scroll(data[0])
```

### 3b. Quantization

`clip_slot.fire()` respects Live's global launch quantization. No special handling needed — this matches how the original session component works.

## Phase 4: Polish & Edge Cases

### 4a. State synchronization

- Handle track/scene additions and deletions (re-attach listeners)
- Handle selected track changes in Live (optionally scroll to follow)
- Ensure grid doesn't scroll beyond track/scene bounds
- Handle return tracks (skip them, or include them)

### 4b. Visual refinements

- Show scroll position indicator (e.g. "Tracks 1-8 / Scenes 5-8")
- Highlight the currently selected track/scene in Live
- Show playing position (beat counter) for playing clips
- Clip name truncation for long names

### 4c. Mode integration

- Clean handoff between DEVICE and SESSION modes
- Ensure pad LEDs reset correctly on mode switch
- Step buttons change meaning between modes (page select → scene launch)
- Consider: session mode could use knobs for track volume/pan

### 4d. Performance

- Debounce grid updates — batch multiple changes within a single tick
- Limit SysEx rate (existing 3ms delay between messages)
- For the full grid message (CMD_SESSION_GRID), 64 bytes of data fits in one SysEx easily

## Implementation Order

1. **Ableton: SessionTracker class** — clip state observation, grid snapshot/diff, new SysEx commands
2. **Ableton: Command routing** — receive launch/stop/scroll from Move, wire to Live API
3. **Move: SysEx handlers** — parse new session commands, maintain grid state array
4. **Move: Pad mapping** — translate pad press to launch/stop commands
5. **Move: Display** — render grid on OLED
6. **Move: Pad LEDs** — set pad colors based on clip state
7. **Mode switching** — toggle between device control and session grid
8. **Navigation** — scroll grid, scene launch buttons
9. **Polish** — edge cases, visual refinements, debouncing

## Open Questions

1. **Pad LED API**: Does Schwung's `raw_midi` / pad API support setting individual pad colors with animation (pulse/blink)? Or only static colors? Need to check Schwung API docs.
2. **Grid orientation**: 4 scenes × 8 tracks (Push-style) or flip it? User preference.
3. **Mode toggle**: Which button for switching? Step button, shift combo, or nav button?
4. **Scope**: Should this replace or coexist with Move's built-in session mode? The built-in one requires switching Move to Session View — this would work from Schwung's shadow mode.
5. **Recording**: Should empty slot presses trigger recording (like the original), or just be ignored?
6. **Track selection**: Should tapping a clip also select that track in Live? Useful for device control mode.
