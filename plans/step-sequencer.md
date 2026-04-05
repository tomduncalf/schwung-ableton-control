# Step Sequencer Plan

## Overview

Add a step sequencer that uses the 8 step buttons (notes 16-23) for step toggling when note mode is active. The 4x8 pad grid selects pitch (via InstrumentComponent), and step buttons toggle notes on/off for the selected pitch.

## Core Design: 8 Steps with Paging

- 8 step buttons show 8 steps at a time
- Left/Right arrows page between step groups (1-8, 9-16, etc.)
- Main wheel changes grid resolution (1/32, 1/16, 1/8, 1/4)
- Manual implementation (not v3 StepSequenceComponent) to avoid dual-purpose button conflicts

## Implementation Approach (Manual)

Use the existing command protocol. Step buttons always send `CMD_PAGE_CHANGE` — Ableton checks `_step_seq_mode` to decide routing:
- If False: page navigation (existing)
- If True: step toggle

### Ableton Side (`schwung_device.py`)

**New state:**
```python
self._step_seq_mode = False
self._step_seq_page = 0
self._step_seq_resolution_index = 1  # 1/16
self._step_seq_clip = None
self._step_seq_pitches = [36]
```

**Step toggle:** On step button press, query clip for notes at that step position for the current pitch. Toggle note on/off using `clip.get_notes_extended()`, `clip.add_new_notes()`, `clip.remove_notes_extended()`.

**Pitch integration:** Listen to InstrumentComponent's `pitches` property. When pitch changes, re-query and update step LEDs.

**Clip lifecycle:** Auto-create 1-bar clip if none exists. Track `detail_clip` changes. Register `notes_listener` for external edits.

### Move Side (`ui.js`)

**Toggle:** Down arrow (when note mode is ON) toggles step seq mode.

**Step LEDs:** Green = has note, Grey = empty. Updated via `CMD_STEP_SEQ_STATE` SysEx.

**Arrow routing:** In step seq mode, Left/Right = step page nav. Main wheel = resolution change.

**Display:** "SEQ 1/16" indicator + page info.

## New MIDI Commands

| Command | Direction | Encoding |
|---------|-----------|----------|
| `CMD_STEP_SEQ_MODE (0x23)` | Move->Live | vel = enabled+1 |
| `CMD_STEP_SEQ_PAGE_NAV (0x24)` | Move->Live | vel = direction+1 |
| `CMD_STEP_SEQ_RESOLUTION_CHANGE (0x25)` | Move->Live | vel = direction+1 |
| `CMD_STEP_SEQ_STATE (SysEx 0x12)` | Live->Move | [bitmask, page, total_pages] |
| `CMD_STEP_SEQ_RESOLUTION (SysEx 0x13)` | Live->Move | resolution name chars |

## Implementation Sequence

### Phase 1: Core
1. Add commands and state to both sides
2. Implement `_handle_step_press()` with clip note manipulation
3. Implement `_send_step_states()` for LED feedback
4. Add Down arrow toggle on Move side
5. Add step LED update handler

### Phase 2: Navigation
6. Arrow-based step page navigation
7. Main wheel grid resolution change
8. Display indicators

### Phase 3: Pitch Integration
9. Listen to InstrumentComponent pitch changes
10. Update step states when pitch changes
11. Show current pitch on display

### Phase 4: Polish
12. Clip auto-creation
13. External edit listener
14. Edge cases (audio tracks, no clip)

## Key Challenges

- **Dual-purpose step buttons:** Routing via `_step_seq_mode` check in existing `CMD_PAGE_CHANGE` handler
- **Pitch selection:** InstrumentComponent's `pitches` listenable property feeds step seq
- **Playhead:** Skip real-time playhead for v1 (would require high-rate polling)
- **Knobs continue working:** Device control is unaffected by step seq mode
