# Code Review - 2026-04-05

Scope: full review of `my_modules/schwung-device-control`, including Move UI, Ableton remote script, and deploy scripts.

Checks run:
- `python3 -m py_compile ableton_remote_script/schwung_device.py ableton_remote_script/__init__.py`
- `node --check src/ui.js`
- `bash -n scripts/build.sh scripts/install.sh scripts/install_full.sh scripts/install_move.sh scripts/install_remote_script.sh`

## Findings

### 1. High - `_save_bindings()` reorders the page array and invalidates every in-memory page pointer

`_current_page`, `_slot_page_memory`, and `_device_page_memory` are all defined as page-array indexes, not stable IDs (`ableton_remote_script/schwung_device.py:89`, `ableton_remote_script/schwung_device.py:97-98`). Navigation and restore logic also dereference those raw indexes (`ableton_remote_script/schwung_device.py:409-484`, `ableton_remote_script/schwung_device.py:486-522`).

But `_save_bindings()` sorts `entry["pages"]` by slot and writes the sorted array back into `self._bindings[device_hash]` (`ableton_remote_script/schwung_device.py:1553-1561`). As soon as pages have been created out of slot order, every stored page index can now point at a different page.

Practical failure mode:
- Learn on an empty slot after other slots already exist.
- Add favourite pages that are appended late.
- Save happens.
- The active page and remembered per-slot page indexes now reference the wrong page, so later edits, recalls, and subpage cycling can act on the wrong bindings.

This needs stable page IDs, or `_save_bindings()` must preserve array order and only sort a serialized copy.

### 2. High - set bindings load from the newest sidecar in the folder, not from the currently opened set

`_get_set_bindings_path()` already computes the exact sidecar path for the current Live set (`ableton_remote_script/schwung_device.py:770-779`). But `_load_set_bindings()` ignores that and instead calls `_find_most_recent_set_bindings()`, which scans the whole directory and picks the newest `*.schwung-set.json` file (`ableton_remote_script/schwung_device.py:781-801`).

Practical failure mode:
- A project folder contains `Song A.als`, `Song B.als`, and both sidecars.
- `Song B.schwung-set.json` was edited more recently.
- Opening `Song A.als` loads `Song B`'s set bindings.

That is a silent cross-project data corruption bug. Load should prefer the exact basename match for `song().file_path`, and only fall back to discovery if that exact file is missing and such fallback is explicitly desired.

### 3. Medium-High - deleting binding JSON externally does not actually delete it

The external reload path only updates entries for files that still exist. `_check_bindings_file()` walks current files and reloads changed ones, but never removes in-memory bindings whose file disappeared (`ableton_remote_script/schwung_device.py:1470-1499`). `_check_set_bindings_file()` is similar: if the set sidecar is gone, it just returns and keeps the old in-memory data (`ableton_remote_script/schwung_device.py:852-856`).

Practical failure mode:
- An editor or agent deletes a device JSON or `.schwung-set.json` to clear bindings.
- The running script keeps the old bindings in memory.
- The next save writes the deleted data back out again.

Because this module is explicitly optimized for LLM-driven fast editing, external file deletion is part of the intended workflow. The runtime should treat missing files as deletion and clear the corresponding in-memory state.

### 4. Medium - device navigation and browse mode can drift out of sync after device topology changes

The script listens for selected-track and selected-device changes (`ableton_remote_script/schwung_device.py:126-150`), but it does not listen for device-list mutations on the track itself. Meanwhile `_device_list` is cached and reused by navigation and browse responses unless it is empty (`ableton_remote_script/schwung_device.py:161`, `ableton_remote_script/schwung_device.py:369-403`).

Practical failure mode:
- Insert, delete, or reorder devices on the selected track without changing the currently selected device.
- `_device_list`, `_device_index`, browser pages, and `deviceCount` remain based on stale topology.
- Browser selection can point at the wrong device, and left/right navigation can skip or wrap incorrectly.

At minimum, `_send_device_list()`, `_navigate_device()`, and `_select_device_by_index()` should refresh `_device_list` from Live each time, or the script should add listeners for device collection changes.

### 5. Medium-Low - `scripts/install.sh` can deploy stale code from `dist/`

`scripts/install.sh` only runs `build.sh` when `dist/device-control-module.tar.gz` does not exist (`scripts/install.sh:12-15`). The actual deploy then copies `dist/device-control/module.json` and `dist/device-control/ui.js`, not the source files (`scripts/install.sh:21-22`).

Practical failure mode:
- Edit `src/ui.js`.
- `dist/` still exists from an earlier build.
- Run `./scripts/install.sh`.
- The old built files are copied to Move, not the current working tree.

For an iteration-heavy workflow this is a bad footgun. `install.sh` should always rebuild, or compare timestamps and fail loudly when `dist/` is stale.

## Testing Gaps

There is no automated coverage around the failure-prone state machinery:
- page/index persistence across save and reload
- set-sidecar selection when multiple `.schwung-set.json` files coexist
- external delete/rename flows for device and set bindings
- device browser correctness after device insertion/removal

Those are the areas most likely to regress again because the implementation relies on mutable in-memory indexes and filesystem polling.

## Structure / Encapsulation Notes

These are lower priority than the functional bugs above, but they are still real maintenance costs.

### 6. Medium - protocol definition is duplicated manually on both sides and has already started to drift

The command constants and payload contracts are duplicated in `src/ui.js` and `ableton_remote_script/schwung_device.py` (`src/ui.js:60-92`, `ableton_remote_script/schwung_device.py:28-61`). There is no shared schema, no generated contract, and no version check.

You can already see protocol drift:
- `CMD_DEVICE_COUNT` and `CMD_DEVICE_INDEX` still exist as first-class commands on both sides (`src/ui.js:71-72`, `ableton_remote_script/schwung_device.py:40-41`).
- The actual implementation has moved those values into the `CMD_DEVICE_INFO` SysEx payload instead (`src/ui.js:338-347`, `src/ui.js:468`, `ableton_remote_script/schwung_device.py:1317-1319`).

That kind of change is easy to make half-way and hard to validate. For this module, a tiny shared protocol table would buy more safety than most style cleanup.

### 7. Medium - the state model overloads "page" and "slot" terminology in ways that are easy to misuse

On the Ableton side, `_current_page` is a page-array index and `_current_slot` is a slot index (`ableton_remote_script/schwung_device.py:89-90`). On the Move side, `currentPage` is actually loaded from `CMD_PAGE_INFO[0]`, which is the current slot, not a page-array index (`src/ui.js:130`, `src/ui.js:361-376`, `ableton_remote_script/schwung_device.py:1359-1361`).

That naming mismatch leaks everywhere:
- `drawPageTabs()` compares `i === currentPage` even though those are slot tabs, not pages (`src/ui.js:1046-1084`).
- Footer fav/set activation also keys off `currentPage === 8/9` even though those are special slots (`src/ui.js:1094-1127`).
- The Python side simultaneously reasons about slot order, page-array order, remembered page indexes, and set-page indexes in the same methods (`ableton_remote_script/schwung_device.py:409-522`, `ableton_remote_script/schwung_device.py:632-646`).

The current design works only because the author has to remember several hidden invariants. A more explicit naming split like `currentSlot`, `currentRegularSlotCount`, `currentDevicePageIndex`, `currentSetPageIndex` would reduce change-risk substantially.

### 8. Medium-Low - favourite and set flows are implemented as parallel special cases instead of one reusable page-family abstraction

The JS side keeps separate state for fav and set pages (`src/ui.js:136-150`), separate button handling (`src/ui.js:743-805`), separate footer rendering (`src/ui.js:1094-1127`), and separate LED branches (`src/ui.js:1211-1233`, `src/ui.js:1291-1302`).

The Python side mirrors that duplication with `_handle_fav_add()`, `_handle_set_page_change()`, `_handle_set_add()`, and `_apply_set_page_bindings()` (`ableton_remote_script/schwung_device.py:566-738`).

That duplication is not mainly a readability problem. The real cost is semantic drift: every tweak to page navigation, activation state, persistence, or UI feedback now has to be repeated across regular, fav, and set paths. That is part of why slot/page logic has become fragile.

### 9. Medium-Low - both main runtime files are large stateful controllers with weak internal boundaries

`src/ui.js` is 1402 lines and mixes protocol constants, transport framing, connection management, input handling, state mutation, rendering, and LED control in one global module (`src/ui.js:48-171`, `src/ui.js:214-520`, `src/ui.js:522-807`, `src/ui.js:813-1393`).

`ableton_remote_script/schwung_device.py` is 1600+ lines and keeps traversal, persistence, conditional binding resolution, connection state, page navigation, set/fav behavior, and transport all inside one `ControlSurface` subclass (`ableton_remote_script/schwung_device.py:79-1602`).

For LLM editing, single-file locality can be useful, so this is not automatically wrong. The issue is that there are almost no hard seams. A change in one concern can mutate shared state that many distant sections rely on. Small extracted units would help here only if they isolate invariants, for example:
- protocol encode/decode helpers
- a page/slot state object
- a persistence adapter
- a single page-family helper for regular/fav/set behavior

### 10. Low - script layer is inconsistent and lightly hardened

`build.sh`, `install.sh`, and `install_remote_script.sh` use shebangs and `set -e`, but `install_full.sh` and `install_move.sh` are one-line wrappers without either (`scripts/install_full.sh:1`, `scripts/install_move.sh:1`).

This is minor compared with the runtime issues, but it fits the same pattern: the project has a few duplicated operational entry points and no single authoritative deploy path.

## Overall Structural Take

The code is not bad in the "too clever for humans" sense. It is mostly explicit and flat, which is actually useful for LLM editing. The problem is different:
- too many protocol and state invariants are implicit
- page/slot identity is not modeled cleanly
- special cases are copy-expanded instead of parameterized
- runtime state and persistence state are entangled

So I would not prioritize cosmetic cleanup. I would prioritize making the state model and protocol contract harder to misuse.

## Latest Commit Additions

Reviewing commit `153efb4` adds two more points that are specific to that reconnect/reinit change set.

### 11. Medium-High - the new "No Device" push still leaves stale knob/value state on Move

The commit fixes empty parameter names by sending `CMD_PARAM_INFO` as `[index, 0]` in the no-device branch (`ableton_remote_script/schwung_device.py:1312-1316`). But `_send_full_state()` still returns before clearing the remaining state that the Move UI uses:
- no zero CCs are sent for knob values
- no `CMD_PARAM_STEPS` reset is sent
- no `CMD_SLOT_SUBPAGE_INFO` reset is sent

Relevant code: `ableton_remote_script/schwung_device.py:1307-1316`.

On the Move side, knob LEDs are driven by incoming CCs (`src/ui.js:483-489`), and slot subpage indicators are driven by `CMD_SLOT_SUBPAGE_INFO` (`src/ui.js:419-426`). So after switching to "no device", the text clears but the old value LEDs and some tab/subpage state can remain stale.

This means the commit fixes only part of the stale-state problem it set out to address.

### 12. Medium - reconnect reset can silently desync learn mode after a heartbeat miss

The new reconnect path resets the whole UI state on the first heartbeat after disconnect (`src/ui.js:455-462`), and `resetUIState()` unconditionally clears `learnMode` (`src/ui.js:173-201`).

But `_send_full_state()` does not include learn-mode state anywhere (`ableton_remote_script/schwung_device.py:1307-1449`). So if the Move side marks itself disconnected after a heartbeat gap while the Ableton script is still alive and still in learn mode, the next heartbeat will clear Move's local learn indicator/state and there is no protocol message that restores it.

That may not matter for the exact "Ableton reinitialized" case from the commit message, but it is a real regression for transient heartbeat loss.
