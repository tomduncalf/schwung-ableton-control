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

## Build & Deploy

```bash
# Build module tarball
./scripts/build.sh

# Deploy to Move
./scripts/install.sh

# Ableton Remote Script: copy ableton_remote_script/ to
# ~/Music/Ableton/User Library/Remote Scripts/SchwungDeviceControl/
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
rm -rf /Users/td/Production/Ableton/User\ Library/Remote\ Scripts/SchwungDeviceControl/ && cp -R ./ableton_remote_script /Users/td/Production/Ableton/User\ Library/Remote\ Scripts/SchwungDeviceControl
```
Then restart Ableton.
