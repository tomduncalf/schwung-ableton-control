# Code Review Response — 2026-04-05

Second-pass review of the findings in `2026-04-05-code-review.md`, plus independent observations from reading the full source.

## Verdict on each finding

### 1. `_save_bindings()` reorders pages — AGREE, FIX

This is real and the fix is simple. `_save_bindings()` at line 1573 does `sorted_pages = sorted(pages, key=lambda p: p.get('slot', 0))` and then writes the sorted list back into `self._bindings[device_hash]`. Every in-memory page index (`_current_page`, `_slot_page_memory`, `_device_page_memory`) is now wrong.

**Fix:** Sort only the serialized copy, not the in-memory dict. Three-line change:

```python
# In _save_bindings(), replace:
self._bindings[device_hash] = sorted_entry
# With:
# Don't mutate in-memory bindings — only sort for readable JSON output
```

Write `sorted_entry` to disk but keep `self._bindings[device_hash]` unchanged. The reload path (`_load_bindings`, `_check_bindings_file`) already reads from disk, so the sorted order will be picked up on next load.

### 2. Set bindings load from newest sidecar — AGREE, FIX

`_load_set_bindings()` calls `_find_most_recent_set_bindings()` instead of `_get_set_bindings_path()`. The exact-match path already exists and works. The "most recent" fallback is actively harmful when multiple `.als` files share a folder.

**Fix:** Load from `_get_set_bindings_path()` first. Fall back to `_find_most_recent_set_bindings()` only if the exact path doesn't exist (for the case where the set hasn't been saved yet, or for legacy sidecars with a different name). Add a log message when falling back so it's visible.

### 3. External delete doesn't clear in-memory bindings — AGREE, FIX

`_check_bindings_file()` only processes files that exist. If a file is deleted, the device hash stays in `self._bindings` and the next `_save_bindings()` call writes it back.

**Fix:** In `_check_bindings_file()`, after scanning existing files, compare the set of on-disk hashes against `self._bindings` keys and remove any that are no longer on disk. Same for `_check_set_bindings_file()` — if the sidecar file is gone, clear `self._set_bindings`.

For `_check_set_bindings_file()`, the existing early return at line 857 (`if not path or not os.path.isfile(path): return`) should instead clear set bindings and push state if the file was previously loaded but is now missing.

### 4. Device list cache stale after topology changes — SKIP FOR NOW

The review is correct that `_device_list` can go stale if devices are added/removed without changing the selected device. But adding a device-list listener is non-trivial in the Ableton framework (you'd need to listen on `track.devices` for every track, and handle chain nesting). The practical impact is low: you'd have to add/remove a device while browsing device-control, and even then the next device change or track change refreshes it.

The cheapest mitigation that's worth doing: refresh `_device_list` at the top of `_send_device_list()` and `_navigate_device()` unconditionally (remove the `if not self._device_list` guard). This costs one device traversal per nav press — negligible.

### 5. `install.sh` deploys stale `dist/` — AGREE, FIX

The `CLAUDE.md` already documents the correct deploy command (`./scripts/build.sh && ./scripts/install.sh`), but `install.sh` itself silently uses stale builds.

**Fix:** Always run `build.sh` in `install.sh`, unconditionally. Remove the `if [ ! -f ... ]` guard. The build is fast enough that this adds no friction.

### 6. Protocol constants duplicated — ACKNOWLEDGED, SKIP

True but low-risk in practice. The protocol is small, stable, and both sides are in the same repo. A shared schema would add build complexity for a two-file project. The dead `CMD_DEVICE_COUNT`/`CMD_DEVICE_INDEX` constants should be removed or commented as legacy, but that's a 2-line cleanup, not an architecture change.

**Cleanup only:** Remove or comment out unused `CMD_DEVICE_COUNT` and `CMD_DEVICE_INDEX` on both sides.

### 7. "page" vs "slot" naming confusion — ACKNOWLEDGED, SKIP

Real maintenance cost, but a rename refactor across both files is high-risk for a working system. The CLAUDE.md already documents the distinction well. I'd only do this if we're already touching those code paths for another reason.

### 8. Fav/set duplication — ACKNOWLEDGED, SKIP

Same reasoning as #7. The duplication is real but extracting a page-family abstraction is a large refactor. Not worth the regression risk unless we're adding a third page type.

### 9. Large files with weak boundaries — ACKNOWLEDGED, SKIP

For LLM editing, single-file locality is actively useful. The review itself acknowledges this. Extracting modules only helps if the extracted units have clean interfaces. Not worth doing speculatively.

### 10. Script inconsistencies — AGREE, FIX (trivial)

Add `#!/bin/bash` and `set -e` to `install_full.sh` and `install_move.sh`. Two-line fix per file.

### 11. "No Device" leaves stale knob/value state — AGREE, FIX

The no-device branch in `_send_full_state()` (lines 1312-1320) sends empty param names but doesn't zero CCs, reset param steps, or clear slot subpage info. The Move side will show ghost LED states from the previous device.

**Fix:** After the param info loop in the no-device branch, add:
- Zero CCs for all 8 knobs
- Send `CMD_PARAM_STEPS` with all 1s (= 0 after offset)
- Send `CMD_SLOT_SUBPAGE_INFO` with single slot, 1 subpage

### 12. Reconnect clears learn mode — AGREE, LOW PRIORITY

The review is right that `resetUIState()` clears `learnMode` and `_send_full_state()` doesn't include learn mode. But learn mode is a transient interactive state — if the connection dropped, clearing it is arguably the safest default. The "transient heartbeat loss" case is real but the window is small (3 seconds of missed heartbeats). I'd add learn mode to `_send_full_state()` as a note command, but only after the higher-priority fixes.

## Additional observations

### 13. Medium — `_save_bindings()` writes ALL devices on every save

Every call to `_save_bindings()` iterates all device hashes and writes every file (line 1570: `for device_hash, entry in self._bindings.items()`). With many devices, this writes N files on every learn/unmap. Should only write the device that changed. The caller always knows which device hash is active.

### 14. Low — `_cleanup_provisional_page` adjusts `_slot_page_memory` but not `_device_page_memory`

When a provisional page is removed (line 1073: `pages.pop(removed_idx)`), `_slot_page_memory` indices are adjusted (lines 1076-1081) but `_device_page_memory` is not. If another device had a `_device_page_memory` entry pointing at or past the removed index in the same device's pages, it would be wrong. In practice this is unlikely because `_device_page_memory` is keyed by device hash and provisional pages only exist during learn mode, but it's a latent inconsistency.

### 15. Low — knob CC 0-7 conflicts with standard MIDI CC assignments

CCs 0-7 are Bank Select MSB, Mod Wheel, Breath, etc. in the GM spec. This works because the module uses cable 2 (Standalone Port) which is isolated, but if any MIDI routing ever bleeds across cables, these CCs would clash. Not worth changing now, but worth knowing about.

## Implementation plan

Priority order for fixes:

1. **Finding 1** — Stop `_save_bindings()` from mutating in-memory page order (3 lines, high impact)
2. **Finding 11** — Complete the no-device state reset (add CC zeros, param steps, subpage info)
3. **Finding 2** — Use exact set sidecar path with fallback
4. **Finding 3** — Handle external file deletion in both check methods
5. **Finding 5 + 10** — Script fixes (always rebuild, add shebangs)
6. **Finding 4** — Refresh device list unconditionally in nav/browse methods
7. **Finding 13** — Only save the changed device file, not all
8. **Finding 6 cleanup** — Remove dead protocol constants
