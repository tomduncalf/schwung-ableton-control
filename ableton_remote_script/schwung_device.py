"""
SchwungDeviceControl - Ableton Remote Script for two-way device parameter control
with Schwung Device Control overtake module on Ableton Move.

Communicates over MIDI cable 2 (USB-C) using CC on channel 16 for real-time values
and SysEx with header F0 00 7D 01 for structured data.
"""

import copy
import hashlib
import json
import logging
import os
import sys
from time import sleep

import Live
from ableton.v3.base import const
from ableton.v3.control_surface import ControlSurface
from ableton.v3.live import liveobj_valid

logger = logging.getLogger(__name__)
from .keyboard import NoteLayout

# MIDI protocol constants
MIDI_CHANNEL = 15  # channel 16 (0-indexed)
KNOB_VALUE_CCS = [0, 1, 2, 3, 4, 5, 6, 7]  # bidirectional knob values

# SysEx protocol (for variable-length string/bulk data only)
SYSEX_START = 0xF0
SYSEX_END = 0xF7
SYSEX_HEADER = (SYSEX_START, 0x00, 0x7D, 0x01)

# SysEx commands: Live -> Move (string/bulk data)
CMD_DEVICE_INFO = 0x01
CMD_PARAM_INFO = 0x02
CMD_LEARN_ACK = 0x06
CMD_PAGE_INFO = 0x09
CMD_PAGE_NAME = 0x0A
CMD_PARAM_VALUE_STRING = 0x0B
CMD_PARAM_STEPS = 0x0C
CMD_SLOT_SUBPAGE_INFO = 0x0D
CMD_DEVICE_LIST_RESPONSE = 0x0E

# Note commands: Live -> Move (note=cmd, velocity=value)
CMD_HEARTBEAT = 0x07
CMD_FAV_ADD_ACK = 0x0F     # vel = fav_index * 16 + result + 1
CMD_SET_ADD_ACK = 0x20      # vel = set_index * 16 + result + 1

# Note commands: Move -> Live (note=cmd, velocity=value, all +1 offset)
CMD_HELLO = 0x10
CMD_LEARN_START = 0x11
CMD_LEARN_STOP = 0x12
CMD_LEARN_KNOB = 0x13
CMD_REQUEST_STATE = 0x15
CMD_UNMAP_KNOB = 0x16
CMD_NAV_DEVICE = 0x17
CMD_PAGE_CHANGE = 0x18
CMD_REQUEST_VALUE_STRING = 0x19
CMD_PAGE_SEQUENTIAL = 0x1A
CMD_RESET_PARAM = 0x1B
CMD_DEVICE_LIST_REQUEST = 0x1C
CMD_DEVICE_SELECT = 0x1D
CMD_FAV_ADD = 0x1E          # vel = fav_index * 16 + knob_idx + 1
CMD_SET_ADD = 0x1F          # vel = set_index * 16 + knob_idx + 1

# Track browser commands
CMD_TRACK_LIST_REQUEST = 0x23   # Move -> Live: vel = offset + 1
CMD_TRACK_SELECT = 0x24         # Move -> Live: vel = track_index + 1
CMD_TRACK_LIST_RESPONSE = 0x12  # SysEx: Live -> Move

# Pad mode commands
CMD_PAD_MODE = 0x21         # Move -> Live: vel = mode+1 (0=off, 1=note, 2=session)
CMD_OCTAVE = 0x22           # Move -> Live: vel = 1+1 (up) or 0+1 (down)

# Snapshot commands
CMD_SNAPSHOT_STORE = 0x25    # Move -> Live: capture all set param values
CMD_SNAPSHOT_RECALL = 0x26   # Move -> Live: restore captured values

# Pad mode constants
PAD_MODE_OFF = 0
PAD_MODE_NOTE = 1
PAD_MODE_SESSION = 2

# SysEx commands: Live -> Move (note layout / session info)
CMD_NOTE_LAYOUT_INFO = 0x11  # root_note, is_in_key, interval, scale_notes...
CMD_SESSION_GRID_COLORS = 0x13  # 32 bytes, one color index per pad

# Session grid color indices (sent to Move for LED mapping)
SCLR_OFF = 0
SCLR_PLAYING = 1
SCLR_RECORDING = 2
SCLR_STOPPED = 3
SCLR_TRIGGERED_PLAY = 4
SCLR_TRIGGERED_RECORD = 5
SCLR_ARMED_EMPTY = 6

# Persistence
if sys.platform == 'darwin':
    _CONFIG_DIR = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'SchwungDeviceControl')
elif sys.platform == 'win32':
    _CONFIG_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'SchwungDeviceControl')
else:
    _CONFIG_DIR = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')), 'SchwungDeviceControl')
_DEVICES_DIR = os.path.join(_CONFIG_DIR, 'devices')

# Timing
HEARTBEAT_TICKS = 10  # ~1 second at 100ms/tick
SYSEX_DELAY = 0.003   # 3ms between sysex sends

MAX_PARAM_NAME_LEN = 12


class SchwungDeviceControl(ControlSurface):
    preferences_key = 'SchwungDeviceControl'

    def log_message(self, *msg):
        logger.info(' '.join(str(m) for m in msg))

    def __init__(self, *a, **k):
        # Init attributes BEFORE super().__init__() because setup() runs inside it
        self._pad_mode = PAD_MODE_NOTE
        self._learn_mode = False
        self._device_list = []
        self._device_index = 0
        self._current_page = 0   # index into pages array, or -1 if on empty slot
        self._current_slot = 0   # always valid slot index (0-9, 8=fav, 9=set)
        self._active_params = [None] * 8
        self._active_listeners = [None] * 8
        self._condition_listeners = []  # [(param, callback), ...]
        self._applying_bindings = False
        self._bindings = {}
        self._suppressing_feedback = [False] * 8
        self._slot_page_memory = {}  # {device_hash: {slot_idx: page_array_index}}
        self._device_page_memory = {}  # {device_hash: (page_index, slot_index)}
        self._heartbeat_counter = 0
        self._connected = False
        self._pending_reinit = False
        self._selected_device = None
        self._track_device_listener_installed = False

        # Set bindings (cross-device, per-set)
        self._set_bindings = {}  # {"pages": [...]}
        self._set_bindings_source_path = None
        self._set_bindings_mtime = None  # mtime of loaded set bindings file
        self._set_file_path_cache = None  # last known song file_path

        self._bindings_mtimes = {}  # {device_hash: mtime}
        self._session_listeners = []  # [(obj, attr, callback), ...]

        # Snapshot: list of (param_ref, value) for set page recall
        self._snapshot = None

        super().__init__(*a, **k)
        self.log_message('SchwungDeviceControl: initializing (v3)')

        self._load_bindings()
        self._load_set_bindings()
        self._setup_listeners()
        self._schedule_heartbeat()

    def _get_additional_dependencies(self):
        note_layout = self.register_disconnectable(
            NoteLayout(preferences=self.preferences)
        )
        return {'note_layout': const(note_layout)}

    def setup(self):
        super().setup()
        self._set_pad_mode(PAD_MODE_NOTE)

    def disconnect(self):
        self.log_message('SchwungDeviceControl: disconnect() called')
        self._remove_all_param_listeners()
        self._remove_device_listeners()
        self._remove_session_listeners()
        super().disconnect()

    def _get_valid_device(self):
        """Return _selected_device if still valid, else clear and return None."""
        device = self._selected_device
        if device is not None and not liveobj_valid(device):
            self.log_message('SchwungDeviceControl: stale device ref, clearing')
            self._selected_device = None
            return None
        return device

    # =========================================================================
    # Pad mode (off / note / session)
    # =========================================================================

    def _set_pad_mode(self, mode):
        self._pad_mode = mode
        mode_names = {PAD_MODE_OFF: 'OFF', PAD_MODE_NOTE: 'NOTE', PAD_MODE_SESSION: 'SESSION'}
        self.log_message('SchwungDeviceControl: pad mode {}'.format(mode_names.get(mode, mode)))
        note_modes = self.component_map['Note_Modes']
        try:
            if mode == PAD_MODE_NOTE:
                self._remove_session_listeners()
                self.set_can_auto_arm(True)
                self.set_can_update_controlled_track(True)
                if note_modes.selected_mode != 'keyboard':
                    note_modes.selected_mode = 'keyboard'
                self._send_note_layout_info()
            elif mode == PAD_MODE_SESSION:
                self.set_can_auto_arm(False)
                self.set_can_update_controlled_track(False)
                if note_modes.selected_mode is not None:
                    note_modes.selected_mode = None
                self._install_session_listeners()
                self._send_session_grid_colors()
            else:
                self._remove_session_listeners()
                if note_modes.selected_mode is not None:
                    note_modes.selected_mode = None
                self.set_can_auto_arm(False)
                self.set_can_update_controlled_track(False)
        except Exception as e:
            self.log_message('SchwungDeviceControl: pad mode error: {}'.format(e))

    def _handle_octave(self, direction):
        try:
            instrument = self.component_map['Instrument']
            if direction > 0:
                instrument.scroll_page_up()
            else:
                instrument.scroll_page_down()
            self._send_note_layout_info()
        except Exception as e:
            self.log_message('SchwungDeviceControl: octave error: {}'.format(e))

    def _send_note_layout_info(self):
        """Send scale/root info to Move for pad coloring."""
        try:
            instrument = self.component_map['Instrument']
            layout = instrument.note_layout
            root = layout.root_note
            is_in_key = 1 if layout.is_in_key else 0
            interval = layout.interval if layout.interval is not None else 0
            scale_notes = layout.scale.notes if layout.scale else list(range(12))
            data = [root, is_in_key, interval] + [n & 0x7F for n in scale_notes]
            self._send_sysex(CMD_NOTE_LAYOUT_INFO, data)
        except Exception as e:
            self.log_message('SchwungDeviceControl: send note layout error: {}'.format(e))

    def _send_session_grid_colors(self):
        """Send clip slot colors for the 4x8 session grid to Move."""
        if not self._connected:
            return
        try:
            colors = []
            tracks = self.song.tracks
            scenes = self.song.scenes
            # Grid: 8 columns (tracks) x 4 rows (scenes)
            # Pad layout: row 0 = bottom (notes 68-75), row 3 = top (notes 92-99)
            # But elements use flip_rows=True, so matrix row 0 = top physical row = scene 0
            for row in range(4):
                scene_idx = row
                for col in range(8):
                    track_idx = col
                    if track_idx < len(tracks) and scene_idx < len(scenes):
                        track = tracks[track_idx]
                        if not track.has_midi_input and not track.has_audio_input:
                            colors.append(SCLR_OFF)
                            continue
                        slot = track.clip_slots[scene_idx]
                        if slot.has_clip:
                            clip = slot.clip
                            if clip.is_playing:
                                colors.append(SCLR_PLAYING)
                            elif clip.is_recording:
                                colors.append(SCLR_RECORDING)
                            elif clip.is_triggered:
                                colors.append(SCLR_TRIGGERED_PLAY)
                            else:
                                colors.append(SCLR_STOPPED)
                        else:
                            if track.arm:
                                colors.append(SCLR_ARMED_EMPTY)
                            else:
                                colors.append(SCLR_OFF)
                    else:
                        colors.append(SCLR_OFF)
            # Offset by +1 to avoid 0x00 bytes in SysEx transport
            self._send_sysex(CMD_SESSION_GRID_COLORS, [c + 1 for c in colors])
        except Exception as e:
            self.log_message('SchwungDeviceControl: session grid colors error: {}'.format(e))

    def _handle_session_pad(self, note):
        """Launch or stop clip at the pad's grid position."""
        idx = note - 68
        phys_row = idx // 8   # 0=bottom, 3=top
        col = idx % 8
        scene_idx = 3 - phys_row  # top row = scene 0
        track_idx = col
        try:
            tracks = self.song.tracks
            scenes = self.song.scenes
            if track_idx < len(tracks) and scene_idx < len(scenes):
                slot = tracks[track_idx].clip_slots[scene_idx]
                if slot.has_clip:
                    if slot.clip.is_playing:
                        slot.clip.stop()
                    else:
                        slot.clip.fire()
                elif tracks[track_idx].arm:
                    slot.fire()  # record into empty armed slot
        except Exception as e:
            self.log_message('SchwungDeviceControl: session pad error: {}'.format(e))

    def _install_session_listeners(self):
        """Add listeners on clip slots in the 8x4 grid for real-time color updates."""
        self._remove_session_listeners()
        tracks = self.song.tracks
        scenes = self.song.scenes
        num_tracks = min(8, len(tracks))
        num_scenes = min(4, len(scenes))
        self.log_message('SchwungDeviceControl: installing session listeners: {}t x {}s'.format(num_tracks, num_scenes))
        for col in range(num_tracks):
            track = tracks[col]
            self._add_session_listener(track, 'arm', self._on_session_grid_changed)
            for row in range(num_scenes):
                slot = track.clip_slots[row]
                self._add_session_listener(slot, 'has_clip', self._on_session_grid_changed)
                self._add_session_listener(slot, 'is_triggered', self._on_session_grid_changed)
                if slot.has_clip:
                    self._add_session_listener(slot.clip, 'playing_status', self._on_session_grid_changed)
        self.log_message('SchwungDeviceControl: installed {} session listeners'.format(len(self._session_listeners)))

    def _remove_session_listeners(self):
        for obj, attr, cb in self._session_listeners:
            try:
                getattr(obj, 'remove_{}_listener'.format(attr))(cb)
            except (RuntimeError, AttributeError):
                pass
        self._session_listeners = []

    def _add_session_listener(self, obj, attr, callback):
        try:
            getattr(obj, 'add_{}_listener'.format(attr))(callback)
            self._session_listeners.append((obj, attr, callback))
        except (RuntimeError, AttributeError):
            pass

    def _on_session_grid_changed(self):
        self.log_message('SchwungDeviceControl: session grid changed (pad_mode={}, connected={})'.format(self._pad_mode, self._connected))
        if self._pad_mode == PAD_MODE_SESSION and self._connected:
            self._send_session_grid_colors()
            # Re-install listeners since clip objects may have changed
            self._install_session_listeners()

    # =========================================================================
    # Listeners setup
    # =========================================================================

    def _setup_listeners(self):
        self.song.view.add_selected_track_listener(self._on_track_changed)
        self._install_device_listener()

    def _install_device_listener(self):
        if self._track_device_listener_installed:
            return
        track = self.song.view.selected_track
        if track:
            track.view.add_selected_device_listener(self._on_device_changed)
            self._track_device_listener_installed = True

    def _remove_device_listeners(self):
        try:
            track = self.song.view.selected_track
            if track and self._track_device_listener_installed:
                track.view.remove_selected_device_listener(self._on_device_changed)
        except:
            pass
        self._track_device_listener_installed = False

    def _on_track_changed(self):
        self._remove_device_listeners()
        self._install_device_listener()
        self._on_device_changed()

    def _on_device_changed(self):
        # Save current page/slot for the device we're leaving
        if self._selected_device is not None and liveobj_valid(self._selected_device):
            prev_hash = self._get_device_hash(self._selected_device)
            self._device_page_memory[prev_hash] = (self._current_page, self._current_slot)

        device = self._get_selected_device()
        self.log_message('SchwungDeviceControl: _on_device_changed device={} connected={}'.format(
            device.name if device else 'None', self._connected))
        self._selected_device = device
        if device is not None:
            self._device_list = self._get_device_list()
            try:
                self._device_index = self._device_list.index(device)
            except ValueError:
                self._device_index = 0
            # If on a set page (slot 9), stay on it across device changes
            device_hash = self._get_device_hash(device)
            if self._current_slot == 9:
                pass  # keep current set page/slot
            else:
                # Restore remembered page/slot or default to 0
                remembered = self._device_page_memory.get(device_hash)
                if remembered is not None:
                    self._current_page, self._current_slot = remembered
                else:
                    self._current_page = 0
                    self._current_slot = 0
            self._apply_bindings_for_device(device)
        else:
            self._device_list = []
            self._device_index = 0
            self._remove_all_param_listeners()
        if self._connected:
            self._send_full_state()

    # =========================================================================
    # Device traversal
    # =========================================================================

    def _get_device_list(self):
        devices = []
        track = self.song.view.selected_track
        if track:
            for device in track.devices:
                self._traverse_chains(device, devices)
        return devices

    def _traverse_chains(self, device, devices):
        devices.append(device)
        if device.can_have_chains:
            for chain in device.chains:
                for nested in chain.devices:
                    self._traverse_chains(nested, devices)

    def _get_selected_device(self):
        track = self.song.view.selected_track
        if track:
            return track.view.selected_device
        return None

    # =========================================================================
    # MIDI map — register notes/CCs so the C++ layer forwards them to Python
    # =========================================================================

    def build_midi_map(self, midi_map_handle):
        # Let v3 framework install element mappings (pad matrix for PlayableComponent)
        super().build_midi_map(midi_map_handle)
        self.log_message('SchwungDeviceControl: build_midi_map() called')
        # Forward all note commands from Move (ch16)
        for note in [CMD_HELLO, CMD_LEARN_START, CMD_LEARN_STOP, CMD_LEARN_KNOB,
                      CMD_REQUEST_STATE, CMD_UNMAP_KNOB, CMD_NAV_DEVICE,
                      CMD_PAGE_CHANGE, CMD_REQUEST_VALUE_STRING, CMD_PAGE_SEQUENTIAL,
                      CMD_RESET_PARAM, CMD_DEVICE_LIST_REQUEST, CMD_DEVICE_SELECT,
                      CMD_FAV_ADD, CMD_SET_ADD, CMD_PAD_MODE, CMD_OCTAVE,
                      CMD_TRACK_LIST_REQUEST, CMD_TRACK_SELECT,
                      CMD_SNAPSHOT_STORE, CMD_SNAPSHOT_RECALL]:
            Live.MidiMap.forward_midi_note(self._c_instance.handle(), midi_map_handle, MIDI_CHANNEL, note)
        # Forward knob value CCs 0-7 (ch16)
        for cc in KNOB_VALUE_CCS:
            Live.MidiMap.forward_midi_cc(self._c_instance.handle(), midi_map_handle, MIDI_CHANNEL, cc)
        # MIDI is now ready — schedule a single debounced state push
        # (build_midi_map is called multiple times during init; we only need one push)
        self._send_note(CMD_HEARTBEAT)
        self._pending_reinit = True
        self.schedule_message(1, self._deferred_reinit)

    # =========================================================================
    # MIDI receive
    # =========================================================================

    def process_midi_bytes(self, midi_bytes, midi_processor):
        if len(midi_bytes) < 1:
            return

        self.log_message('SchwungDeviceControl RX: {}'.format(
            ' '.join('{:02X}'.format(b) for b in midi_bytes)))

        # SysEx
        if midi_bytes[0] == SYSEX_START:
            self._process_sysex(midi_bytes)
            return

        if len(midi_bytes) >= 3:
            status = midi_bytes[0] & 0xF0
            channel = midi_bytes[0] & 0x0F
            if channel == MIDI_CHANNEL:
                # Note On — command dispatch
                if status == 0x90:
                    self._process_note_command(midi_bytes[1], midi_bytes[2])
                    return
                # CC — knob values (CC 0-7)
                if status == 0xB0:
                    cc = midi_bytes[1]
                    value = midi_bytes[2]
                    if cc in KNOB_VALUE_CCS:
                        self._handle_knob_value(cc, value)
                        return

        # Session mode: handle pad notes for clip launch/stop
        if self._pad_mode == PAD_MODE_SESSION and len(midi_bytes) >= 3:
            status = midi_bytes[0] & 0xF0
            channel = midi_bytes[0] & 0x0F
            note = midi_bytes[1]
            vel = midi_bytes[2]
            if channel == 0 and 68 <= note <= 99 and status == 0x90 and vel > 0:
                self._handle_session_pad(note)
                return

        # Pass through everything else (pad notes, etc.)
        super().process_midi_bytes(midi_bytes, midi_processor)

    def _process_note_command(self, note, vel):
        """Dispatch Note On messages as commands (note=cmd, vel=value with +1 offset)."""
        self.log_message('SchwungDeviceControl NOTE cmd=0x{:02X} vel={}'.format(note, vel))
        v = vel - 1  # undo +1 offset

        if note == CMD_HELLO:
            self._on_hello()
        elif note == CMD_LEARN_START:
            self._learn_mode = True
            self.log_message('SchwungDeviceControl: learn mode ON')
        elif note == CMD_LEARN_STOP:
            self._learn_mode = False
            self._cleanup_provisional_page()
            self.log_message('SchwungDeviceControl: learn mode OFF')
        elif note == CMD_LEARN_KNOB:
            self._learn_knob(v)
        elif note == CMD_REQUEST_STATE:
            self._send_full_state()
        elif note == CMD_UNMAP_KNOB:
            self._unmap_knob(v)
        elif note == CMD_NAV_DEVICE:
            direction = 1 if v == 0x01 else -1
            self._navigate_device(direction)
        elif note == CMD_PAGE_CHANGE:
            self._handle_page_change(v)
        elif note == CMD_PAGE_SEQUENTIAL:
            direction = 1 if v == 0x01 else -1
            self._handle_page_sequential(direction)
        elif note == CMD_REQUEST_VALUE_STRING:
            self._send_param_value_string(v)
        elif note == CMD_RESET_PARAM:
            self._reset_param_to_default(v)
        elif note == CMD_DEVICE_LIST_REQUEST:
            self._send_device_list(v)
        elif note == CMD_DEVICE_SELECT:
            self._select_device_by_index(v)
        elif note == CMD_FAV_ADD:
            fav_index = v >> 4
            knob_idx = v & 0x0F
            self._handle_fav_add(fav_index, knob_idx)
        elif note == CMD_SET_ADD:
            set_index = v >> 4
            knob_idx = v & 0x0F
            self._handle_set_add(set_index, knob_idx)
        elif note == CMD_PAD_MODE:
            self._set_pad_mode(v)
        elif note == CMD_OCTAVE:
            self._handle_octave(v)
        elif note == CMD_TRACK_LIST_REQUEST:
            self._send_track_list(v)
        elif note == CMD_TRACK_SELECT:
            self._select_track_by_index(v)
        elif note == CMD_SNAPSHOT_STORE:
            self._snapshot_store()
        elif note == CMD_SNAPSHOT_RECALL:
            self._snapshot_recall()

    def _process_sysex(self, midi_bytes):
        self.log_message('SchwungDeviceControl SYSEX len={}: {}'.format(
            len(midi_bytes), ' '.join('{:02X}'.format(b) for b in midi_bytes[:12])))
        if len(midi_bytes) < 6:
            self.log_message('SchwungDeviceControl SYSEX too short')
            return
        if tuple(midi_bytes[0:4]) != SYSEX_HEADER:
            self.log_message('SchwungDeviceControl SYSEX header mismatch')
            return

        cmd = midi_bytes[4]
        data = midi_bytes[5:-1]  # strip F7
        self.log_message('SchwungDeviceControl SYSEX cmd=0x{:02X} data={}'.format(cmd, list(data)))

    # =========================================================================
    # Knob value handling (Move -> Live parameter changes)
    # =========================================================================

    def _handle_knob_value(self, knob_idx, value):
        if knob_idx < 0 or knob_idx >= 8:
            return
        param = self._active_params[knob_idx]
        if param is None:
            return

        # Map 0-127 to parameter range
        normalized = value / 127.0
        new_value = param.min + normalized * (param.max - param.min)

        # Quantize to valid step for discrete parameters
        if param.is_quantized:
            new_value = round(new_value)

        # Suppress feedback to avoid echo loop — flag stays set until the
        # listener fires (async), so the echoed value_listener call is skipped.
        self._suppressing_feedback[knob_idx] = True
        try:
            param.value = new_value
        except:
            pass

        # Send formatted value string for overlay display
        self._send_param_value_string(knob_idx)

    def _reset_param_to_default(self, knob_idx):
        if knob_idx < 0 or knob_idx >= 8:
            return
        param = self._active_params[knob_idx]
        if param is None:
            return
        self._suppressing_feedback[knob_idx] = True
        try:
            param.value = param.default_value
        except:
            pass
        self._send_param_value(knob_idx)
        self._send_param_value_string(knob_idx)
        self.log_message('SchwungDeviceControl: reset knob {} to default'.format(knob_idx))

    def _navigate_device(self, direction):
        self._device_list = self._get_device_list()
        if not self._device_list:
            return

        new_index = self._device_index + direction
        if new_index < 0:
            new_index = len(self._device_list) - 1
        elif new_index >= len(self._device_list):
            new_index = 0

        self._device_index = new_index
        device = self._device_list[self._device_index]

        # Select the device in Live
        self.song.view.select_device(device)

    def _send_device_list(self, offset):
        """Send up to 8 device names starting at offset."""
        self._device_list = self._get_device_list()
        total = len(self._device_list)
        payload = [min(127, offset), min(127, total)]
        for i in range(8):
            idx = offset + i
            if idx < total:
                name = self._device_list[idx].name
                payload += self._encode_string(name, 14) + [0]
        self._send_sysex(CMD_DEVICE_LIST_RESPONSE, payload)

    def _select_device_by_index(self, index):
        """Select a device by its flat index in the device list."""
        self._device_list = self._get_device_list()
        if index < 0 or index >= len(self._device_list):
            return
        device = self._device_list[index]
        self.song.view.select_device(device)

    # =========================================================================
    # Track traversal
    # =========================================================================

    def _get_track_list(self):
        """Return visible tracks (audio, MIDI, return, master)."""
        tracks = []
        for track in self.song.visible_tracks:
            tracks.append(track)
        for track in self.song.return_tracks:
            tracks.append(track)
        tracks.append(self.song.master_track)
        return tracks

    def _send_track_list(self, offset):
        """Send up to 8 track names starting at offset."""
        tracks = self._get_track_list()
        total = len(tracks)
        selected = self.song.view.selected_track
        current_index = 0
        try:
            current_index = tracks.index(selected)
        except ValueError:
            pass
        payload = [min(127, offset), min(127, total), min(127, current_index)]
        for i in range(8):
            idx = offset + i
            if idx < total:
                name = tracks[idx].name
                payload += self._encode_string(name, 14) + [0]
        self._send_sysex(CMD_TRACK_LIST_RESPONSE, payload)

    def _select_track_by_index(self, index):
        """Select a track by its index in the track list."""
        tracks = self._get_track_list()
        if index < 0 or index >= len(tracks):
            return
        self.song.view.selected_track = tracks[index]

    def _handle_page_change(self, slot_idx):
        if slot_idx < 0 or slot_idx >= 12:
            return
        # Slot 8/9 = jump to fav slot 8 subpage 0/1 (dedicated buttons, no cycling)
        fav_target_sub = None
        if slot_idx == 9:
            fav_target_sub = 1
            slot_idx = 8
        elif slot_idx == 8:
            fav_target_sub = 0
        # Slot 10/11 = jump to set slot 9 subpage 0/1
        if slot_idx == 11 or slot_idx == 10:
            target_sub = 1 if slot_idx == 11 else 0
            self._handle_set_page_change(target_sub)
            return
        device = self._get_valid_device()
        if device is None:
            return
        device_hash = self._get_device_hash(device)
        current_slot = self._get_current_slot(device_hash)
        pages_in_slot = self._get_pages_for_slot(device_hash, slot_idx)

        if not pages_in_slot:
            # Empty slot — navigate here, show empty knobs, no page entry created
            if device_hash not in self._slot_page_memory:
                self._slot_page_memory[device_hash] = {}
            self._slot_page_memory[device_hash][current_slot] = self._current_page
            self._current_page = -1
            self._current_slot = slot_idx
            self._apply_bindings_for_device(device)
            self._send_full_state()
            return

        # Save current page as memory for the slot we're leaving
        if device_hash not in self._slot_page_memory:
            self._slot_page_memory[device_hash] = {}
        self._slot_page_memory[device_hash][current_slot] = self._current_page

        if fav_target_sub is not None and fav_target_sub < len(pages_in_slot):
            # Direct jump to a specific subpage (e.g. step 10 -> fav subpage 1)
            self._current_page = pages_in_slot[fav_target_sub]
        elif slot_idx == current_slot:
            # Same slot pressed again: cycle to next sub-page
            pos = -1
            try:
                pos = pages_in_slot.index(self._current_page)
                next_pos = (pos + 1) % len(pages_in_slot)
            except ValueError:
                next_pos = 0

            self.log_message('SchwungDeviceControl: same slot {} pressed, pos={} len={} learn={}'.format(
                slot_idx, pos, len(pages_in_slot), self._learn_mode))

            current_page_has_knobs = (
                0 <= self._current_page < len(self._bindings.get(device_hash, {}).get('pages', []))
                and any(k is not None for k in self._bindings[device_hash]['pages'][self._current_page].get('knobs', []))
            )
            if self._learn_mode and pos == len(pages_in_slot) - 1 and current_page_has_knobs:
                # Cycled past last sub-page in learn mode: create provisional page
                pages = self._bindings[device_hash]['pages']
                new_page = {'name': '{}'.format(len(pages) + 1), 'slot': slot_idx, 'knobs': [None] * 8}
                pages.append(new_page)
                self._current_page = len(pages) - 1
            else:
                self._current_page = pages_in_slot[next_pos]
        else:
            # Different slot: restore remembered page or first page in slot
            remembered = self._slot_page_memory.get(device_hash, {}).get(slot_idx)
            if remembered is not None and remembered in pages_in_slot:
                self._current_page = remembered
            else:
                self._current_page = pages_in_slot[0]

        self._current_slot = slot_idx
        self._apply_bindings_for_device(device)
        self._send_full_state()

    def _handle_page_sequential(self, direction):
        """Walk through all pages in slot order: slot 0 sub 0, slot 0 sub 1, slot 1 sub 0, etc."""
        device = self._get_valid_device()
        if device is None:
            return
        device_hash = self._get_device_hash(device)

        # Build ordered list of (slot, page_index) pairs — regular slots only (0-7)
        slot_count = self._get_regular_slot_count(device_hash)
        ordered = []
        for slot in range(slot_count):
            for page_idx in self._get_pages_for_slot(device_hash, slot):
                ordered.append((slot, page_idx))
        if not ordered:
            return

        # Find current position
        current = (self._current_slot, self._current_page)
        try:
            pos = ordered.index(current)
        except ValueError:
            pos = 0

        # Move to next/prev with wrapping
        new_pos = (pos + direction) % len(ordered)
        new_slot, new_page = ordered[new_pos]

        # Save slot page memory for the slot we're leaving
        if new_slot != self._current_slot:
            if device_hash not in self._slot_page_memory:
                self._slot_page_memory[device_hash] = {}
            self._slot_page_memory[device_hash][self._current_slot] = self._current_page

        self._current_slot = new_slot
        self._current_page = new_page
        self._apply_bindings_for_device(device)
        self._send_full_state()

    # =========================================================================
    # Slot helpers
    # =========================================================================

    def _get_slot_for_page(self, device_hash, page_idx):
        """Return the slot index for a page (defaults to array index)."""
        pages = self._bindings.get(device_hash, {}).get('pages', [])
        if page_idx < len(pages):
            return pages[page_idx].get('slot', page_idx)
        return page_idx

    def _get_pages_for_slot(self, device_hash, slot_idx):
        """Return list of page array indices assigned to a slot, in order."""
        pages = self._bindings.get(device_hash, {}).get('pages', [])
        return [i for i, p in enumerate(pages) if p.get('slot', i) == slot_idx]

    def _get_slot_count(self, device_hash):
        """Return number of slots (max slot + 1)."""
        pages = self._bindings.get(device_hash, {}).get('pages', [])
        if not pages:
            return 1
        max_slot = max(p.get('slot', i) for i, p in enumerate(pages))
        return max_slot + 1

    def _get_regular_slot_count(self, device_hash):
        """Return number of regular slots (max slot < 8, plus 1)."""
        pages = self._bindings.get(device_hash, {}).get('pages', [])
        if not pages:
            return 1
        regular_slots = [p.get('slot', i) for i, p in enumerate(pages) if p.get('slot', i) < 8]
        if not regular_slots:
            return 1
        return max(regular_slots) + 1

    def _get_current_slot(self, device_hash=None):
        """Return the current slot index."""
        return self._current_slot

    # =========================================================================
    # Favourites
    # =========================================================================

    def _handle_fav_add(self, fav_index, knob_idx):
        """Copy binding from current page's knob to a favourite page (both on slot 8)."""
        if fav_index < 0 or fav_index > 1 or knob_idx < 0 or knob_idx >= 8:
            return
        device = self._get_valid_device()
        if device is None:
            return
        device_hash = self._get_device_hash(device)
        pages = self._bindings.get(device_hash, {}).get('pages', [])

        # Get source binding from current page
        if self._current_page < 0 or self._current_page >= len(pages):
            self._send_note(CMD_FAV_ADD_ACK, fav_index * 16 + 2 + 1)  # no binding
            return
        source_knobs = pages[self._current_page].get('knobs', [None] * 8)
        source_binding = source_knobs[knob_idx]
        if source_binding is None:
            self._send_note(CMD_FAV_ADD_ACK, fav_index * 16 + 2 + 1)  # no binding
            return

        # Find or create the target fav subpage (both on slot 8)
        fav_name = '* {}'.format(fav_index + 1)
        fav_pages = self._get_pages_for_slot(device_hash, 8)
        fav_page_idx = None
        if len(fav_pages) > fav_index:
            fav_page_idx = fav_pages[fav_index]
        else:
            # Create missing fav subpages up to the target index
            while len(self._get_pages_for_slot(device_hash, 8)) <= fav_index:
                idx = len(self._get_pages_for_slot(device_hash, 8))
                new_page = {'name': '* {}'.format(idx + 1), 'slot': 8, 'knobs': [None] * 8}
                pages.append(new_page)
            fav_pages = self._get_pages_for_slot(device_hash, 8)
            fav_page_idx = fav_pages[fav_index]

        fav_knobs = pages[fav_page_idx].get('knobs', [None] * 8)

        # Find first free knob slot (0-7)
        free_slot = None
        for i in range(8):
            if fav_knobs[i] is None:
                free_slot = i
                break
        if free_slot is None:
            self._send_note(CMD_FAV_ADD_ACK, fav_index * 16 + 1 + 1)  # full
            return

        # Copy binding
        fav_knobs[free_slot] = copy.deepcopy(source_binding)
        self._save_bindings(device_hash)

        self._send_note(CMD_FAV_ADD_ACK, fav_index * 16 + 0 + 1)  # success
        self.log_message('SchwungDeviceControl: fav add slot={} knob={} -> fav {} knob {}'.format(
            self._current_slot, knob_idx, fav_index, free_slot))

        # If currently on the fav slot, re-apply bindings to show the new param
        if self._current_slot == 8:
            self._apply_bindings_for_device(device)
        self._send_full_state()

    # =========================================================================
    # Set pages (cross-device per-set favourites)
    # =========================================================================

    def _handle_set_page_change(self, target_sub):
        """Switch to a set page subpage."""
        device = self._get_valid_device()
        set_pages = self._set_bindings.get('pages', [])
        # Save current slot memory
        if device:
            device_hash = self._get_device_hash(device)
            if device_hash not in self._slot_page_memory:
                self._slot_page_memory[device_hash] = {}
            self._slot_page_memory[device_hash][self._current_slot] = self._current_page
        if target_sub < len(set_pages):
            self._current_page = target_sub  # index into _set_bindings['pages']
        else:
            self._current_page = -1  # empty
        self._current_slot = 9
        self._apply_bindings_for_device(device)
        self._send_full_state()

    def _handle_set_add(self, set_index, knob_idx):
        """Copy binding from current page's knob to a set page, including device reference."""
        if set_index < 0 or set_index > 1 or knob_idx < 0 or knob_idx >= 8:
            return
        device = self._get_valid_device()
        if device is None:
            return
        device_hash = self._get_device_hash(device)
        pages = self._bindings.get(device_hash, {}).get('pages', [])

        # Get source binding — could be from device page, fav page, or even another set page
        if self._current_slot == 9:
            # On set page: source from set bindings
            set_pages = self._set_bindings.get('pages', [])
            if self._current_page < 0 or self._current_page >= len(set_pages):
                self._send_note(CMD_SET_ADD_ACK, set_index * 16 + 2 + 1)
                return
            source_knobs = set_pages[self._current_page].get('knobs', [None] * 8)
            source_binding = source_knobs[knob_idx]
            if source_binding is None:
                self._send_note(CMD_SET_ADD_ACK, set_index * 16 + 2 + 1)
                return
            # Already has device_hash
            new_binding = copy.deepcopy(source_binding)
        else:
            # On device/fav page: source from device bindings
            if self._current_page < 0 or self._current_page >= len(pages):
                self._send_note(CMD_SET_ADD_ACK, set_index * 16 + 2 + 1)
                return
            source_knobs = pages[self._current_page].get('knobs', [None] * 8)
            source_binding = source_knobs[knob_idx]
            if source_binding is None:
                self._send_note(CMD_SET_ADD_ACK, set_index * 16 + 2 + 1)
                return
            # Add device reference
            binding_copy = copy.deepcopy(source_binding)
            if isinstance(binding_copy, list):
                # Conditional binding — take first entry for set page (simplify)
                binding_copy = binding_copy[0] if binding_copy else None
                if binding_copy is None:
                    self._send_note(CMD_SET_ADD_ACK, set_index * 16 + 2 + 1)
                    return
            new_binding = binding_copy
            new_binding['device_hash'] = device_hash
            new_binding['device_name'] = device.name

        # Ensure set pages exist
        if 'pages' not in self._set_bindings:
            self._set_bindings['pages'] = []
        set_pages = self._set_bindings['pages']
        while len(set_pages) <= set_index:
            set_pages.append({'name': 'S {}'.format(len(set_pages) + 1), 'knobs': [None] * 8})
        target_page = set_pages[set_index]

        # Find first free knob (0-7)
        knobs = target_page.get('knobs', [None] * 8)
        free_slot = None
        for i in range(8):
            if knobs[i] is None:
                free_slot = i
                break
        if free_slot is None:
            self._send_note(CMD_SET_ADD_ACK, set_index * 16 + 1 + 1)  # full
            return

        knobs[free_slot] = new_binding
        self._save_set_bindings()

        self._send_note(CMD_SET_ADD_ACK, set_index * 16 + 0 + 1)  # success
        self.log_message('SchwungDeviceControl: set add knob={} -> set {} knob {}'.format(
            knob_idx, set_index, free_slot))

        if self._current_slot == 9:
            self._apply_bindings_for_device(device)
        self._send_full_state()

    def _apply_set_page_bindings(self):
        """Apply cross-device bindings from the current set page."""
        set_pages = self._set_bindings.get('pages', [])
        if self._current_page < 0 or self._current_page >= len(set_pages):
            return
        page = set_pages[self._current_page]
        for knob_idx, binding in enumerate(page.get('knobs', [None] * 8)):
            if binding is not None and 0 <= knob_idx < 8:
                device_hash = binding.get('device_hash')
                if device_hash:
                    device = self._find_device_by_hash(device_hash)
                    if device:
                        param = self._resolve_param(device, binding)
                        if param is not None:
                            self._bind_param_to_knob(knob_idx, param)

    def _find_device_by_hash(self, device_hash):
        """Find a device anywhere in the set by its hash."""
        for track in list(self.song.tracks) + list(self.song.return_tracks) + [self.song.master_track]:
            for device in track.devices:
                found = self._search_device_recursive(device, device_hash)
                if found:
                    return found
        return None

    def _search_device_recursive(self, device, device_hash):
        if self._get_device_hash(device) == device_hash:
            return device
        if device.can_have_chains:
            for chain in device.chains:
                for nested in chain.devices:
                    found = self._search_device_recursive(nested, device_hash)
                    if found:
                        return found
        return None

    def _get_set_dir(self):
        """Get the project directory for the current set."""
        try:
            file_path = self.song.file_path
            if file_path:
                return os.path.dirname(file_path)
        except:
            pass
        return None

    def _get_set_bindings_path(self):
        """Get the bindings path for the current set file."""
        try:
            file_path = self.song.file_path
            if not file_path:
                return None
            base = os.path.splitext(os.path.basename(file_path))[0]
            return os.path.join(os.path.dirname(file_path), base + '.schwung-set.json')
        except:
            return None

    def _find_most_recent_set_bindings(self):
        """Find the most recent .schwung-set.json in the project directory."""
        set_dir = self._get_set_dir()
        if not set_dir or not os.path.isdir(set_dir):
            return None
        candidates = []
        try:
            for fname in os.listdir(set_dir):
                if fname.endswith('.schwung-set.json'):
                    fpath = os.path.join(set_dir, fname)
                    candidates.append((os.path.getmtime(fpath), fpath))
        except:
            return None
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def _load_set_bindings(self):
        """Load set bindings — prefer exact sidecar for current set, fall back to most recent."""
        path = self._get_set_bindings_path()
        if path and not os.path.isfile(path):
            path = self._find_most_recent_set_bindings()
            if path:
                self.log_message('SchwungDeviceControl: exact sidecar not found, falling back to {}'.format(path))
        if not path:
            path = self._find_most_recent_set_bindings()
        if path:
            try:
                with open(path, 'r') as f:
                    self._set_bindings = json.load(f)
                self._migrate_knobs(self._set_bindings.get('pages', []))
                self._set_bindings_source_path = path
                self._set_bindings_mtime = os.path.getmtime(path)
                self.log_message('SchwungDeviceControl: loaded set bindings from {}'.format(path))
            except Exception as e:
                self._set_bindings = {}
                self._set_bindings_source_path = None
                self._set_bindings_mtime = None
                self.log_message('SchwungDeviceControl: set bindings load error: {}'.format(e))
        else:
            self._set_bindings = {}
            self._set_bindings_source_path = None
            self._set_bindings_mtime = None
        try:
            self._set_file_path_cache = self.song.file_path
        except:
            self._set_file_path_cache = None

    def _save_set_bindings(self):
        """Save set bindings (copy-on-write: creates new file if set was saved-as)."""
        target = self._get_set_bindings_path()
        if not target:
            self.log_message('SchwungDeviceControl: set bindings not saved (unsaved set)')
            return
        try:
            pages = self._set_bindings.get('pages', [])
            with open(target, 'w') as f:
                json.dump(self._set_bindings, f, indent=2)
            self._set_bindings_source_path = target
            self._set_bindings_mtime = os.path.getmtime(target)
            self.log_message('SchwungDeviceControl: set bindings saved to {}'.format(target))
        except Exception as e:
            self.log_message('SchwungDeviceControl: set bindings save error: {}'.format(e))

    def _check_set_file_changed(self):
        """Check if the song file changed (e.g. opened new set) and reload."""
        try:
            current = self.song.file_path
        except:
            current = None
        if current != self._set_file_path_cache:
            self._set_file_path_cache = current
            self._load_set_bindings()
            return True
        return False

    def _check_set_bindings_file(self):
        """Reload set bindings file if modified externally, clear if deleted."""
        path = self._set_bindings_source_path
        if not path:
            return
        if not os.path.isfile(path):
            # File was deleted externally — clear in-memory set bindings
            if self._set_bindings:
                self._set_bindings = {}
                self._set_bindings_source_path = None
                self._set_bindings_mtime = None
                self.log_message('SchwungDeviceControl: set bindings file deleted, cleared in-memory state')
                if self._connected and self._current_slot == 9:
                    self._remove_all_param_listeners()
                    self._send_full_state()
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if mtime == self._set_bindings_mtime:
            return
        try:
            with open(path, 'r') as f:
                self._set_bindings = json.load(f)
            self._migrate_knobs(self._set_bindings.get('pages', []))
            self._set_bindings_mtime = mtime
            self.log_message('SchwungDeviceControl: reloaded set bindings from {}'.format(path))
        except Exception as e:
            self.log_message('SchwungDeviceControl: set bindings reload error: {}'.format(e))
            return
        if self._connected and self._current_slot == 9:
            self._apply_set_page_bindings()
            self._send_full_state()

    # =========================================================================
    # Learn mode
    # =========================================================================

    def _learn_knob(self, knob_idx):
        self.log_message('SchwungDeviceControl: _learn_knob({})'.format(knob_idx))
        if knob_idx < 0 or knob_idx >= 8:
            return

        # Delegate to set page learn if on set page
        if self._current_slot == 9:
            self._learn_set_knob(knob_idx)
            return

        # Get the currently selected/focused parameter in Live
        param = self.song.view.selected_parameter
        device = self._get_selected_device()
        self.log_message('SchwungDeviceControl: param={} device={}'.format(
            param.name if param else None,
            device.name if device else None))

        if param is None or device is None:
            self.log_message('SchwungDeviceControl: learn failed - no param or device selected')
            return

        # Find param index in device
        param_index = None
        for i, p in enumerate(device.parameters):
            if p == param:
                param_index = i
                break

        if param_index is None:
            self.log_message('SchwungDeviceControl: param not found in device')
            return

        device_hash = self._get_device_hash(device)

        # Store binding on current page
        if device_hash not in self._bindings:
            self._bindings[device_hash] = {'deviceName': device.name, 'pages': []}
        device_entry = self._bindings[device_hash]
        if 'deviceName' not in device_entry:
            device_entry['deviceName'] = device.name
        if 'pages' not in device_entry:
            device_entry['pages'] = []
        pages = device_entry['pages']
        # If on empty slot (current_page == -1), create a page for this slot
        if self._current_page < 0:
            new_page = {'name': '{}'.format(self._current_slot + 1), 'slot': self._current_slot, 'knobs': [None] * 8}
            pages.append(new_page)
            self._current_page = len(pages) - 1
        # Auto-create pages up to current_page
        while len(pages) <= self._current_page:
            pages.append({'name': '{}'.format(len(pages) + 1), 'slot': self._current_slot, 'knobs': [None] * 8})
        knobs = pages[self._current_page]['knobs']
        new_binding = {
            'param_index': param_index,
            'param_name': param.name,
            'short_name': param.name
        }
        existing = knobs[knob_idx]
        if existing is not None:
            # Preserve existing bindings (possibly conditional) as an array
            if isinstance(existing, list):
                existing.append(new_binding)
                knobs[knob_idx] = existing
            else:
                knobs[knob_idx] = [existing, new_binding]
        else:
            knobs[knob_idx] = new_binding

        # Activate immediately
        self._bind_param_to_knob(knob_idx, param)

        # Send ACK to Move (use short_name for display)
        display_name = pages[self._current_page]['knobs'][knob_idx].get('short_name', param.name)
        name_bytes = self._encode_string(display_name, MAX_PARAM_NAME_LEN)
        self._send_sysex(CMD_LEARN_ACK, [knob_idx] + name_bytes + [0])

        # Send step count (offset by +1 to avoid 0x00 in SysEx)
        steps = []
        for i in range(8):
            p = self._active_params[i]
            if p is None:
                steps.append(1)
            else:
                steps.append(min(127, self._get_param_num_steps(p)) + 1)
        self._send_sysex(CMD_PARAM_STEPS, steps)

        self._save_bindings(device_hash)
        self.log_message('SchwungDeviceControl: learned knob {} -> {} ({})'.format(
            knob_idx, param.name, device.name))

    def _learn_set_knob(self, knob_idx):
        """Learn a knob on the current set page (cross-device binding)."""
        param = self.song.view.selected_parameter
        if param is None:
            return

        # Derive device from the parameter itself, so we can learn from any device in the set
        device = param.canonical_parent
        if device is None:
            return

        param_index = None
        for i, p in enumerate(device.parameters):
            if p == param:
                param_index = i
                break
        if param_index is None:
            return

        device_hash = self._get_device_hash(device)

        if 'pages' not in self._set_bindings:
            self._set_bindings['pages'] = []
        set_pages = self._set_bindings['pages']
        # Create set page if needed
        if self._current_page < 0:
            self._current_page = 0
        while len(set_pages) <= self._current_page:
            set_pages.append({'name': 'S {}'.format(len(set_pages) + 1), 'knobs': [None] * 8})
        knobs = set_pages[self._current_page].get('knobs', [None] * 8)
        knobs[knob_idx] = {
            'device_hash': device_hash,
            'device_name': device.name,
            'param_index': param_index,
            'param_name': param.name,
            'short_name': param.name
        }
        set_pages[self._current_page]['knobs'] = knobs

        self._bind_param_to_knob(knob_idx, param)

        display_name = param.name
        name_bytes = self._encode_string(display_name, MAX_PARAM_NAME_LEN)
        self._send_sysex(CMD_LEARN_ACK, [knob_idx] + name_bytes + [0])

        steps = []
        for i in range(8):
            p = self._active_params[i]
            if p is None:
                steps.append(1)
            else:
                steps.append(min(127, self._get_param_num_steps(p)) + 1)
        self._send_sysex(CMD_PARAM_STEPS, steps)

        self._save_set_bindings()
        self.log_message('SchwungDeviceControl: set learned knob {} -> {} ({})'.format(
            knob_idx, param.name, device.name))

    def _unmap_knob(self, knob_idx):
        if knob_idx < 0 or knob_idx >= 8:
            return

        self._unbind_knob(knob_idx)

        if self._current_slot == 9:
            # Set page: unmap from set bindings
            set_pages = self._set_bindings.get('pages', [])
            if 0 <= self._current_page < len(set_pages):
                set_pages[self._current_page].get('knobs', [None] * 8)[knob_idx] = None
                self._save_set_bindings()
        else:
            device = self._get_valid_device()
            if device:
                device_hash = self._get_device_hash(device)
                device_entry = self._bindings.get(device_hash, {})
                pages = device_entry.get('pages', [])
                if self._current_page < len(pages):
                    pages[self._current_page]['knobs'][knob_idx] = None
                    self._save_bindings(device_hash)

        # Send updated param info (empty)
        self._send_sysex(CMD_PARAM_INFO, [knob_idx] + self._encode_string('', MAX_PARAM_NAME_LEN) + [0])
        self._send_param_value(knob_idx)

    def _cleanup_provisional_page(self):
        """Remove current page if it has no knob bindings (provisional from learn mode)."""
        device = self._get_valid_device()
        if device is None:
            return
        device_hash = self._get_device_hash(device)
        pages = self._bindings.get(device_hash, {}).get('pages', [])
        if self._current_page < 0 or self._current_page >= len(pages):
            return
        page = pages[self._current_page]
        if any(k is not None for k in page.get('knobs', [None] * 8)):
            return  # has bindings, keep it

        current_slot = self._get_current_slot(device_hash)
        pages_in_slot = self._get_pages_for_slot(device_hash, current_slot)

        # Only remove if there are other pages on this slot to fall back to
        if len(pages_in_slot) <= 1:
            return

        removed_idx = self._current_page
        pages.pop(removed_idx)

        # Update slot_page_memory: adjust any indices >= removed_idx
        mem = self._slot_page_memory.get(device_hash, {})
        for slot, pidx in list(mem.items()):
            if pidx == removed_idx:
                del mem[slot]
            elif pidx > removed_idx:
                mem[slot] = pidx - 1

        # Switch to previous sub-page on this slot
        pages_in_slot = self._get_pages_for_slot(device_hash, current_slot)
        if pages_in_slot:
            self._current_page = pages_in_slot[-1]
        else:
            self._current_page = 0

        self._save_bindings(device_hash)
        self._apply_bindings_for_device(device)
        if self._connected:
            self._send_full_state()
        self.log_message('SchwungDeviceControl: removed empty provisional page')

    # =========================================================================
    # Parameter binding and value listeners
    # =========================================================================

    def _bind_param_to_knob(self, knob_idx, param):
        self._unbind_knob(knob_idx)
        self._active_params[knob_idx] = param

        # Create value listener for Live -> Move sync
        def on_value_changed():
            if self._suppressing_feedback[knob_idx]:
                self._suppressing_feedback[knob_idx] = False
                return
            self._send_param_value(knob_idx)
            self._send_param_value_string(knob_idx)

        param.add_value_listener(on_value_changed)
        self._active_listeners[knob_idx] = (param, on_value_changed)

        # Send initial value
        self._send_param_value(knob_idx)

    def _unbind_knob(self, knob_idx):
        self._active_params[knob_idx] = None
        listener = self._active_listeners[knob_idx]
        if listener:
            param, callback = listener
            try:
                param.remove_value_listener(callback)
            except:
                pass
            self._active_listeners[knob_idx] = None

    def _remove_all_param_listeners(self):
        for i in range(8):
            self._unbind_knob(i)
        for param, callback in self._condition_listeners:
            try:
                param.remove_value_listener(callback)
            except:
                pass
        self._condition_listeners = []

    def _send_param_value(self, knob_idx):
        param = self._active_params[knob_idx]
        if param is None:
            self._send_cc(knob_idx, 0)
            return
        val_range = param.max - param.min
        if val_range == 0:
            midi_val = 0
        else:
            midi_val = int(127 * (param.value - param.min) / val_range)
        midi_val = max(0, min(127, midi_val))
        self._send_cc(knob_idx, midi_val)

    def _get_param_num_steps(self, param):
        """Return number of discrete steps (0 = continuous)."""
        if not param.is_quantized:
            return 0
        return len(param.value_items) if param.value_items else int(param.max - param.min) + 1

    def _send_param_value_string(self, knob_idx):
        param = self._active_params[knob_idx]
        if param is None:
            return
        value_str = str(param)
        self._send_sysex(CMD_PARAM_VALUE_STRING,
                         [knob_idx] + self._encode_string(value_str, 20) + [0])

    # =========================================================================
    # Apply bindings for current device
    # =========================================================================

    def _apply_bindings_for_device(self, device):
        if self._applying_bindings:
            return
        self._applying_bindings = True
        try:
            self._remove_all_param_listeners()

            if self._current_slot == 9:
                self._apply_set_page_bindings()
                return

            if device is None:
                return
            device_hash = self._get_device_hash(device)
            device_entry = self._bindings.get(device_hash, {})
            pages = device_entry.get('pages', [])

            if 0 <= self._current_page < len(pages):
                page = pages[self._current_page]
                seen_condition_params = set()
                for knob_idx, binding_or_list in enumerate(page.get('knobs', [None] * 8)):
                    if binding_or_list is not None and 0 <= knob_idx < 8:
                        binding, cond_params = self._resolve_conditional_binding(device, binding_or_list)
                        for cp in cond_params:
                            if id(cp) not in seen_condition_params:
                                seen_condition_params.add(id(cp))
                                def on_condition_changed(d=device):
                                    self._apply_bindings_for_device(d)
                                    self._send_full_state()
                                cp.add_value_listener(on_condition_changed)
                                self._condition_listeners.append((cp, on_condition_changed))
                        if binding is not None:
                            param = self._resolve_param(device, binding)
                            if param is not None:
                                self._bind_param_to_knob(knob_idx, param)
        finally:
            self._applying_bindings = False

    def _get_set_display_name(self, knob_idx, fallback):
        """Get short_name from set binding for display."""
        set_pages = self._set_bindings.get('pages', [])
        if 0 <= self._current_page < len(set_pages):
            binding = set_pages[self._current_page].get('knobs', [None] * 8)[knob_idx]
            if binding:
                return binding.get('short_name', fallback)
        return fallback

    def _get_display_name(self, device_hash, knob_idx, fallback):
        """Get short_name from binding for display, falling back to the full param name."""
        device_entry = self._bindings.get(device_hash, {})
        pages = device_entry.get('pages', [])
        if 0 <= self._current_page < len(pages):
            binding_or_list = pages[self._current_page].get('knobs', [None] * 8)[knob_idx]
            if binding_or_list:
                if isinstance(binding_or_list, list):
                    device = self._get_valid_device()
                    if device:
                        binding, _ = self._resolve_conditional_binding(device, binding_or_list)
                        if binding:
                            return binding.get('short_name', fallback)
                else:
                    return binding_or_list.get('short_name', fallback)
        return fallback

    def _resolve_conditional_binding(self, device, binding_or_list):
        """If binding is a list, evaluate conditions and return the active binding dict.
        If it's a plain dict, return it as-is. Returns (binding_dict, [condition_params])."""
        if isinstance(binding_or_list, dict):
            cond = binding_or_list.get('if')
            if cond is not None:
                result, cond_param = self._evaluate_condition(device, cond)
                cond_params = [cond_param] if cond_param else []
                return (binding_or_list if result else None), cond_params
            return binding_or_list, []
        if not isinstance(binding_or_list, list) or not binding_or_list:
            return None, []

        condition_params = []
        fallback = None
        for candidate in binding_or_list:
            cond = candidate.get('if')
            if cond is None:
                fallback = candidate
                continue
            result, cond_param = self._evaluate_condition(device, cond)
            if cond_param is not None:
                condition_params.append(cond_param)
            if result:
                return candidate, condition_params
        return fallback, condition_params

    def _evaluate_condition(self, device, condition_str):
        """Parse and evaluate a condition like 'ParamName == Value' or 'ParamName != Value'.
        Returns (bool, param_or_None)."""
        for op in ('!=', '=='):
            if op in condition_str:
                parts = condition_str.split(op, 1)
                param_name = parts[0].strip()
                expected = parts[1].strip()
                matches = [p for p in device.parameters if p.name == param_name]
                if matches:
                    param = matches[0]
                    actual = str(param)
                    result = (actual == expected) if op == '==' else (actual != expected)
                    self.log_message('[DC] condition: "{}" {} "{}" -> {} (param name: "{}")'.format(
                        actual, op, expected, result, param.name))
                    return result, param
                self.log_message('[DC] condition param not found: "{}" (available: {})'.format(
                    param_name, ', '.join(p.name for p in device.parameters)))
                return False, None
        return False, None

    def _resolve_param(self, device, binding):
        """Resolve a binding to a live parameter, using name then falling back to index."""
        param_name = binding.get('param_name')
        param_index = binding.get('param_index')

        # Try name match first
        if param_name:
            matches = [p for p in device.parameters if p.name == param_name]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1 and param_index is not None:
                # Ambiguous name — use index to disambiguate
                if param_index < len(device.parameters):
                    return device.parameters[param_index]

        # Fall back to index
        if param_index is not None and param_index < len(device.parameters):
            return device.parameters[param_index]

        return None

    # =========================================================================
    # State push to Move
    # =========================================================================

    def _send_full_state(self):
        device = self._get_valid_device()
        self.log_message('SchwungDeviceControl: _send_full_state device={} page={} slot={} params={}'.format(
            device.name if device else 'None', self._current_page, self._current_slot,
            [p.name if p else '-' for p in self._active_params]))
        if device is None:
            self._send_sysex(CMD_DEVICE_INFO, self._encode_string('No Device', MAX_PARAM_NAME_LEN) + [0, 0, 0])
            sleep(SYSEX_DELAY)
            self._send_sysex(CMD_PAGE_INFO, [0, 1, 1, 1, 1, 1, 1, 1])
            sleep(SYSEX_DELAY)
            for i in range(8):
                self._send_sysex(CMD_PARAM_INFO, [i, 0])  # index + null terminator (empty name)
                sleep(SYSEX_DELAY)
            # Zero knob values, step counts, and subpage info so Move clears stale LEDs
            for i in range(8):
                self._send_cc(i, 0)
            self._send_sysex(CMD_PARAM_STEPS, [1] * 8)
            self._send_sysex(CMD_SLOT_SUBPAGE_INFO, [2, 1])  # 1 slot, 1 subpage (offset +1)
            return

        # Device info: name + null + count + index
        self._send_sysex(CMD_DEVICE_INFO, self._encode_string(device.name, 20) + [0, min(127, len(self._device_list)), min(127, self._device_index)])
        sleep(SYSEX_DELAY)

        # Page info (slot-based)
        device_hash = self._get_device_hash(device)
        device_entry = self._bindings.get(device_hash, {})
        pages = device_entry.get('pages', [])
        current_slot = self._get_current_slot(device_hash)
        slot_count = max(1, self._get_regular_slot_count(device_hash))

        # Fav slot info (slot 8, with subpages): state, subpage count, active subpage
        # State: 0=empty, 1=has bindings, 2=active (offset +1 for SysEx safety)
        fav_pages = self._get_pages_for_slot(device_hash, 8)
        if current_slot == 8:
            fav_state = 2
        elif fav_pages and any(any(k is not None for k in pages[p].get('knobs', [None] * 8)) for p in fav_pages):
            fav_state = 1
        else:
            fav_state = 0
        fav_sub_count = len(fav_pages)
        fav_active_sub = 0
        if current_slot == 8 and self._current_page >= 0 and self._current_page in fav_pages:
            fav_active_sub = fav_pages.index(self._current_page)
        elif current_slot != 8 and fav_pages:
            remembered = self._slot_page_memory.get(device_hash, {}).get(8)
            if remembered is not None and remembered in fav_pages:
                fav_active_sub = fav_pages.index(remembered)
        # Set slot info (slot 9, with subpages)
        set_pages = self._set_bindings.get('pages', [])
        if current_slot == 9:
            set_state = 2
        elif set_pages and any(any(k is not None for k in p.get('knobs', [None] * 8)) for p in set_pages):
            set_state = 1
        else:
            set_state = 0
        set_sub_count = len(set_pages)
        if current_slot == 9:
            set_active_sub = max(0, self._current_page)
        else:
            remembered = self._slot_page_memory.get(device_hash, {}).get(9)
            set_active_sub = max(0, remembered) if remembered is not None else 0
        self._send_sysex(CMD_PAGE_INFO, [current_slot, slot_count,
                                         fav_state + 1, fav_sub_count + 1, fav_active_sub + 1,
                                         set_state + 1, set_sub_count + 1, set_active_sub + 1])
        sleep(SYSEX_DELAY)

        # Page names (one per regular slot, showing active sub-page name)
        for slot in range(slot_count):
            slot_pages = self._get_pages_for_slot(device_hash, slot)
            if slot_pages:
                if slot == current_slot and self._current_page >= 0:
                    active_page = self._current_page
                elif slot == current_slot:
                    active_page = slot_pages[0]  # on empty slot, shouldn't reach here
                else:
                    remembered = self._slot_page_memory.get(device_hash, {}).get(slot)
                    if remembered is not None and remembered in slot_pages:
                        active_page = remembered
                    else:
                        active_page = slot_pages[0]
                name = pages[active_page].get('name', '{}'.format(slot + 1))
            else:
                name = '{}'.format(slot + 1)
            self._send_sysex(CMD_PAGE_NAME, [slot] + self._encode_string(name, MAX_PARAM_NAME_LEN) + [0])
            sleep(SYSEX_DELAY)

        # Parameter names (use short_name from binding if available)
        for i in range(8):
            param = self._active_params[i]
            if param:
                if current_slot == 9:
                    name = self._get_set_display_name(i, param.name)
                else:
                    name = self._get_display_name(device_hash, i, param.name)
            else:
                name = ''
            self._send_sysex(CMD_PARAM_INFO, [i] + self._encode_string(name, MAX_PARAM_NAME_LEN) + [0])
            sleep(SYSEX_DELAY)

        # All parameter values in one message
        values = []
        for i in range(8):
            param = self._active_params[i]
            if param is None:
                values.append(0)
            else:
                val_range = param.max - param.min
                if val_range == 0:
                    values.append(0)
                else:
                    v = int(127 * (param.value - param.min) / val_range)
                    values.append(max(0, min(127, v)))
        for i, v in enumerate(values):
            self._send_cc(i, v)
        sleep(SYSEX_DELAY)

        # Step counts for discrete/quantized parameters
        # Offset by +1 to avoid 0x00 bytes in SysEx (0x00 may be stripped in transport)
        # Move side subtracts 1 on receive
        steps = []
        for i in range(8):
            param = self._active_params[i]
            if param is None:
                steps.append(1)  # 1 means 0 (continuous) after -1
            else:
                steps.append(min(127, self._get_param_num_steps(param)) + 1)
        self._send_sysex(CMD_PARAM_STEPS, steps)

        # Slot subpage info: per-slot [subpage_count, active_subpage_index], offset +1
        subpage_info = []
        for slot in range(slot_count):
            slot_pages = self._get_pages_for_slot(device_hash, slot)
            num_subpages = len(slot_pages)
            if slot == current_slot:
                if self._current_page >= 0 and self._current_page in slot_pages:
                    active_sub = slot_pages.index(self._current_page)
                else:
                    active_sub = 0
            else:
                remembered = self._slot_page_memory.get(device_hash, {}).get(slot)
                if remembered is not None and remembered in slot_pages:
                    active_sub = slot_pages.index(remembered)
                else:
                    active_sub = 0
            subpage_info.append(num_subpages + 1)
            subpage_info.append(active_sub + 1)
        self._send_sysex(CMD_SLOT_SUBPAGE_INFO, subpage_info)

    # =========================================================================
    # Connection / heartbeat
    # =========================================================================

    def _on_hello(self):
        self.log_message('SchwungDeviceControl: HELLO received from Move module')
        self._connected = True
        self._set_pad_mode(PAD_MODE_NOTE)
        self._send_note(CMD_HEARTBEAT)
        self._on_device_changed()

    def _deferred_reinit(self):
        """Called once after build_midi_map settles. Resets connection state and pushes fresh state to Move."""
        if not self._pending_reinit:
            return
        self._pending_reinit = False
        self.log_message('SchwungDeviceControl: deferred reinit — pushing full state')
        self._connected = True
        self._slot_page_memory = {}
        self._device_page_memory = {}
        self._current_page = 0
        self._current_slot = 0
        self._set_pad_mode(self._pad_mode)
        self._on_device_changed()

    def _schedule_heartbeat(self):
        self.schedule_message(HEARTBEAT_TICKS, self._heartbeat_tick)

    def _heartbeat_tick(self):
        # Always send heartbeat so Move knows Ableton is alive, even before HELLO handshake
        self._send_note(CMD_HEARTBEAT)
        self._check_bindings_file()
        self._check_set_bindings_file()
        if self._check_set_file_changed() and self._connected:
            self._send_full_state()
        # Refresh session grid colors periodically when in session mode
        if self._pad_mode == PAD_MODE_SESSION and self._connected:
            self._send_session_grid_colors()
        self._schedule_heartbeat()

    def _check_bindings_file(self):
        """Reload any device binding files modified externally, remove deleted ones."""
        if not os.path.isdir(_DEVICES_DIR):
            return
        changed = False
        seen_hashes = set()
        for fname in os.listdir(_DEVICES_DIR):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(_DEVICES_DIR, fname)
            try:
                mtime = os.path.getmtime(fpath)
            except OSError:
                continue
            device_hash = fname.rsplit('_', 1)[-1].replace('.json', '')
            seen_hashes.add(device_hash)
            if self._bindings_mtimes.get(device_hash) == mtime:
                continue
            changed = True
            try:
                with open(fpath, 'r') as f:
                    self._bindings[device_hash] = json.load(f)
                self._migrate_knobs(self._bindings[device_hash].get('pages', []))
                self._bindings_mtimes[device_hash] = mtime
                self.log_message('SchwungDeviceControl: reloaded {}'.format(fname))
            except Exception as e:
                self.log_message('SchwungDeviceControl: reload error {}: {}'.format(fname, e))
        # Remove in-memory bindings whose files were deleted
        for device_hash in list(self._bindings.keys()):
            if device_hash not in seen_hashes:
                del self._bindings[device_hash]
                self._bindings_mtimes.pop(device_hash, None)
                self.log_message('SchwungDeviceControl: cleared deleted bindings for {}'.format(device_hash))
                changed = True
        if changed:
            device = self._get_valid_device()
            if device and liveobj_valid(device):
                self._apply_bindings_for_device(device)
                if self._connected:
                    self._send_full_state()

    # =========================================================================
    # Snapshot (set page capture/recall)
    # =========================================================================

    def _snapshot_store(self):
        """Capture current values of all params across all set pages."""
        set_pages = self._set_bindings.get('pages', [])
        snapshot = []
        for page in set_pages:
            for binding in page.get('knobs', [None] * 8):
                if binding is None:
                    continue
                device_hash = binding.get('device_hash')
                if not device_hash:
                    continue
                device = self._find_device_by_hash(device_hash)
                if not device:
                    continue
                param = self._resolve_param(device, binding)
                if param is not None:
                    snapshot.append((param, param.value))
        self._snapshot = snapshot
        self.log_message('SchwungDeviceControl: snapshot stored ({} params)'.format(len(snapshot)))

    def _snapshot_recall(self):
        """Restore all params to their snapshot values."""
        if not self._snapshot:
            self.log_message('SchwungDeviceControl: no snapshot to recall')
            return
        count = 0
        for param, value in self._snapshot:
            try:
                param.value = value
                count += 1
            except:
                pass
        self.log_message('SchwungDeviceControl: snapshot recalled ({} params)'.format(count))
        # Update Move's knob values/LEDs for the active page
        for i in range(8):
            self._suppressing_feedback[i] = True
            self._send_param_value(i)
            self._send_param_value_string(i)

    # =========================================================================
    # Hashing
    # =========================================================================

    def _get_device_hash(self, device):
        key = '{}:{}'.format(device.class_name, device.name)
        h = hashlib.sha1(key.encode('utf-8')).digest()
        return h[:8].hex()

    # =========================================================================
    # MIDI send helpers
    # =========================================================================

    def _send_note(self, note, velocity=1):
        self._send_midi((0x90 | MIDI_CHANNEL, note, velocity))

    def _send_cc(self, cc, value):
        self._send_midi((0xB0 | MIDI_CHANNEL, cc, value))

    def _send_sysex(self, cmd, data):
        msg = (SYSEX_START, 0x00, 0x7D, 0x01, cmd) + tuple(data) + (SYSEX_END,)
        self._send_midi(msg)

    def _encode_string(self, s, max_len):
        """Encode string as 7-bit safe ASCII bytes for SysEx."""
        return [ord(c) & 0x7F for c in s[:max_len]]

    # =========================================================================
    # Persistence — per-device files in _DEVICES_DIR
    # =========================================================================

    @staticmethod
    def _sanitize_filename(name):
        """Replace filesystem-unsafe characters with underscores."""
        out = []
        for c in name:
            out.append(c if c.isalnum() or c in ' -.' else '_')
        return ''.join(out).strip()

    def _device_file_path(self, device_hash, device_name=''):
        """Return path for a device binding file: <name>_<hash>.json"""
        if not device_name:
            entry = self._bindings.get(device_hash, {})
            device_name = entry.get('deviceName', 'Unknown')
        safe = self._sanitize_filename(device_name)
        return os.path.join(_DEVICES_DIR, '{}_{}.json'.format(safe, device_hash))

    def _save_bindings(self, only_hash=None):
        if not os.path.isdir(_DEVICES_DIR):
            os.makedirs(_DEVICES_DIR)
        try:
            items = [(only_hash, self._bindings[only_hash])] if only_hash else self._bindings.items()
            for device_hash, entry in items:
                # Sort pages by slot for readable JSON only — don't mutate in-memory order
                pages = entry.get('pages', [])
                sorted_pages = sorted(pages, key=lambda p: p.get('slot', 0))
                serialized = {}
                if 'deviceName' in entry:
                    serialized['deviceName'] = entry['deviceName']
                serialized['pages'] = sorted_pages
                fpath = self._device_file_path(device_hash)
                with open(fpath, 'w') as f:
                    json.dump(serialized, f, indent=2)
                self._bindings_mtimes[device_hash] = os.path.getmtime(fpath)
            self.log_message('SchwungDeviceControl: bindings saved')
        except Exception as e:
            self.log_message('SchwungDeviceControl: save error: {}'.format(e))

    @staticmethod
    def _migrate_knobs(pages):
        """Convert old dict-keyed knobs format to array format."""
        for page in pages:
            knobs = page.get('knobs')
            if isinstance(knobs, dict):
                arr = [None] * 8
                for k, v in knobs.items():
                    idx = int(k)
                    if 0 <= idx < 8:
                        arr[idx] = v
                page['knobs'] = arr
            elif not isinstance(knobs, list):
                page['knobs'] = [None] * 8

    def _load_bindings(self):
        self._bindings = {}
        if not os.path.isdir(_DEVICES_DIR):
            return
        for fname in os.listdir(_DEVICES_DIR):
            if not fname.endswith('.json'):
                continue
            device_hash = fname.rsplit('_', 1)[-1].replace('.json', '')
            fpath = os.path.join(_DEVICES_DIR, fname)
            try:
                with open(fpath, 'r') as f:
                    self._bindings[device_hash] = json.load(f)
                self._migrate_knobs(self._bindings[device_hash].get('pages', []))
                self._bindings_mtimes[device_hash] = os.path.getmtime(fpath)
            except Exception as e:
                self.log_message('SchwungDeviceControl: load error {}: {}'.format(fname, e))
        self.log_message('SchwungDeviceControl: loaded {} device bindings'.format(
            len(self._bindings)))
