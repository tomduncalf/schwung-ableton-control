# Move-Side Mapping Persistence

## Context

Mappings are currently stored on the Ableton side (`bindings.json` in Remote Scripts folder). This is fragile - can be wiped on Ableton updates. Move should be the source of truth, following the `move-anything-control` pattern (`host_write_file`/`host_read_file`).

## Current Flow
1. Learn: Move sends LEARN_KNOB → Ableton grabs param, stores in `self._bindings`, saves to `bindings.json`, sends ACK
2. Connect: Ableton loads `bindings.json`, applies bindings for current device, pushes state to Move
3. Move has no persistent state — all param names/values come from Ableton

## New Flow
1. Learn: Move sends LEARN_KNOB → Ableton grabs param, sends **BINDING_DATA** SysEx to Move (device_hash, knob_idx, param_index, param_hash, param_name) → Move stores to file
2. Connect: Move loads mappings from file, sends **STORED_BINDINGS** to Ableton → Ableton applies them
3. Move is source of truth for which knob maps to which param

## Files to Modify

- `src/ui.js` — Add load/save config, store bindings, send stored bindings on connect
- `ableton_remote_script/schwung_device.py` — Remove `bindings.json` persistence, send binding data to Move after learn, receive stored bindings from Move on connect

## SysEx Protocol Additions

### Live → Move: CMD_BINDING_DATA (0x09)
Sent after a successful learn. Move stores this.
```
F0 00 7D 01 09
  <knob_idx>                    // 1 byte: 0-7
  <device_hash bytes 0-7>       // 8 bytes (7-bit safe)  
  <param_index_msb>             // 1 byte
  <param_index_lsb>             // 1 byte
  <param_hash bytes 0-5>        // 6 bytes (7-bit safe)
  <param_name bytes> 00         // null-terminated string
  <device_name bytes> 00        // null-terminated string
F7
```

### Move → Live: CMD_STORED_BINDINGS (0x18)
Sent on connect (after HELLO/HEARTBEAT handshake). One message per binding.
```
F0 00 7D 01 18
  <knob_idx>
  <device_hash bytes 0-7>
  <param_index_msb>
  <param_index_lsb>
  <param_hash bytes 0-5>
  <param_name bytes> 00
F7
```

### Move → Live: CMD_BINDINGS_DONE (0x19)
Sent after all stored bindings have been transmitted.
```
F0 00 7D 01 19 F7
```

## Move-Side Implementation (ui.js)

### Storage
```
/data/UserData/schwung/modules/tools/device-control/mappings.json
```

Format:
```json
{
  "device_hash_hex": {
    "0": { "param_index": 5, "param_hash": "abcdef", "param_name": "Frequency", "device_name": "AutoFilter" },
    "1": { "param_index": 2, "param_hash": "123456", "param_name": "Resonance", "device_name": "AutoFilter" }
  }
}
```

### Changes to ui.js
1. Add `let mappings = {}` state and `MAPPINGS_PATH` constant
2. Add `loadMappings()` — call `host_read_file`, parse JSON on init
3. Add `saveMappings()` — call `host_write_file` with JSON.stringify
4. Handle CMD_BINDING_DATA: store binding in `mappings`, call `saveMappings()`
5. On connect (after heartbeat received): send all stored bindings for the **current device** via CMD_STORED_BINDINGS, then CMD_BINDINGS_DONE
6. Track current device hash (sent via CMD_DEVICE_INFO — need to also send device hash from Ableton)

### Device hash in DEVICE_INFO
Currently CMD_DEVICE_INFO only sends device name. Add device hash so Move knows which device is active and can look up stored bindings.

Update CMD_DEVICE_INFO (0x01):
```
F0 00 7D 01 01 <device_hash 8 bytes> <device_name bytes> 00 F7
```

## Ableton-Side Implementation (schwung_device.py)

### Changes
1. Remove `_save_bindings()` and `_load_bindings()` — no more `bindings.json`
2. Remove `BINDINGS_FILE` constant
3. After learn: send CMD_BINDING_DATA to Move (instead of saving locally)
4. Keep `self._bindings` as in-memory cache for the current session
5. Handle CMD_STORED_BINDINGS: populate `self._bindings` from Move data
6. Handle CMD_BINDINGS_DONE: apply bindings for current device
7. Update `_send_full_state` to include device hash in CMD_DEVICE_INFO

## Connection Sequence (Updated)

1. Move init → sends CMD_HELLO
2. Ableton receives HELLO → sends CMD_HEARTBEAT, then full device state (CMD_DEVICE_INFO with hash, PARAM_INFO, etc.)
3. Move receives DEVICE_INFO with hash → looks up stored bindings for that device hash → sends CMD_STORED_BINDINGS for each binding → sends CMD_BINDINGS_DONE
4. Ableton receives stored bindings → populates `self._bindings` → applies for current device → pushes updated param info back to Move

## Verification

1. Learn a few params, restart Ableton → mappings should restore from Move
2. Learn params on two different devices, switch between them → each device shows its own mappings
3. Remove a device and re-add → hash-based matching should rebind
4. Check `/data/UserData/schwung/.../mappings.json` on Move via SSH
