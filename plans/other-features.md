# Other Interesting Features from the Move Remote Script

## Priority Order

### 1. Transport Controls (Very High Value, Very Low Effort)
Play, stop, record, metronome, loop toggle. The v3 base `TransportComponent` provides ready-made controls. Almost zero custom code on Ableton side — just wire commands to `song().is_playing`, etc. Map to shift+button combos on Move.

~50 lines Ableton, ~80 lines Move. 1-2 hours.

### 2. Recording (Very High Value, Low Effort)
Session record/overdub into clips. Completes the record-from-pads workflow. The v3 `ViewBasedRecordingComponent` handles the logic. Two commands: toggle session record, toggle arrangement record.

Depends on: Transport. ~40 lines each side. 1 hour.

### 3. Clip Actions — Duplicate/Delete (High Value, Very Low Effort)
Quick duplicate or delete of current clip. Uses `ableton.v3.live.action` module. Two commands, map to shift+delete and shift+[button].

~30 lines each side. 30 minutes.

### 4. Drum Group (Very High Value, Moderate Effort)
When target track has a Drum Rack, lay out pads as drum pads instead of keyboard. The v3 base `DrumGroupComponent` handles pad-to-note mapping and scrolling. Add `'drum'` mode to `Note_Modes`. Auto-detect via `instrument_finder.drum_group`.

Depends on: Note mode (exists). ~60 lines Ableton, ~40 lines Move. 2-3 hours.

### 5. Note Repeat (Medium-High Value, Moderate Effort)
Hold pad to retrigger at configurable rate. Uses `c_instance.note_repeat` API. Create `NoteRepeatModel` in dependencies. Toggle button + rate selection via step buttons.

Depends on: Note mode. ~80 lines Ableton, ~60 lines Move. 2-3 hours.

### 6. Quantization (Medium Value, Low Effort)
Quantize clip notes to grid resolution. Simple: `clip.quantize(resolution, strength)`. Shares `GridResolutionComponent` with step sequencer.

Depends on: GridResolutionComponent. ~40 lines Ableton, ~20 lines Move. 1 hour.

### 7. Loop Length (Medium Value, Low Effort)
Adjust clip loop length via encoder. Read/write `clip.loop_end`. Shift for fine-tune.

~40 lines Ableton, ~20 lines Move. 1 hour.

### 8. Track List/Navigation (Medium-High Value, Moderate Effort)
Select tracks, show arm/mute/solo state. Uses session ring. Simple version: left/right track nav commands. Full version: step buttons show 8 tracks with colors.

Depends on: session_ring (num_tracks > 0). ~40-150 lines. 1-4 hours.

### 9. Sliced Simpler (Low-Medium Value, Very Low Effort)
Map pads to sample slices. Near-free once drum group exists — just add `'simpler'` mode to `Note_Modes` with `sliced_simpler_changed` detection.

Depends on: Drum group infrastructure. ~20 lines. 30 minutes.

## Shared Infrastructure

- **`session_ring`**: Track list + session mode need `num_tracks > 0`
- **`SequencerClip`**: Loop length + quantization + step sequencer share clip tracking
- **`GridResolutionComponent`**: Quantization + step sequencer share grid resolution
- **`instrument_finder`**: Drum group + sliced simpler detection (already enabled by `include_auto_arming`)
- **`Note_Modes` expansion**: Drum group and sliced simpler add modes alongside `'keyboard'`

## Pattern for Command-Protocol Features
1. Define commands in MIDI protocol (ch16 note-on)
2. Handle in `_process_note_command()` on Ableton side
3. Send state feedback via note-on or SysEx back to Move
4. Map to buttons/combos on Move side in `ui.js`

## Pattern for v3 Component Features
1. Add to `component_map` in Specification
2. Add element definitions in `elements.py`
3. Wire in `mappings.py`
4. Framework handles the heavy lifting
