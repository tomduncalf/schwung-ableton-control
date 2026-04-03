"""
SchwungDeviceControl - Ableton Remote Script for two-way device parameter control
with Schwung Device Control overtake module on Ableton Move.

Communicates over MIDI cable 2 (USB-C) using CC on channel 16 for real-time values
and SysEx with header F0 00 7D 01 for structured data.
"""

import hashlib
import json
import os
from time import sleep

from _Framework.ControlSurface import ControlSurface

# MIDI protocol constants
MIDI_CHANNEL = 15  # channel 16 (0-indexed)
KNOB_CCS = [71, 72, 73, 74, 75, 76, 77, 78]
CC_DEVICE_LEFT = 80
CC_DEVICE_RIGHT = 81
CC_BANK_UP = 82
CC_BANK_DOWN = 83
CC_LEARN_TOGGLE = 84
NAV_CCS = {CC_DEVICE_LEFT, CC_DEVICE_RIGHT, CC_BANK_UP, CC_BANK_DOWN, CC_LEARN_TOGGLE}

# SysEx protocol
SYSEX_START = 0xF0
SYSEX_END = 0xF7
SYSEX_HEADER = (SYSEX_START, 0x00, 0x7D, 0x01)

# SysEx commands: Live -> Move
CMD_DEVICE_INFO = 0x01
CMD_PARAM_INFO = 0x02
CMD_DEVICE_COUNT = 0x03
CMD_DEVICE_INDEX = 0x04
CMD_BANK_INFO = 0x05
CMD_LEARN_ACK = 0x06
CMD_HEARTBEAT = 0x07
CMD_ALL_VALUES = 0x08

# SysEx commands: Move -> Live
CMD_HELLO = 0x10
CMD_LEARN_START = 0x11
CMD_LEARN_STOP = 0x12
CMD_LEARN_KNOB = 0x13
CMD_KNOB_VALUE = 0x14   # Move -> Live: knob_idx, value (0-127)
CMD_REQUEST_STATE = 0x15
CMD_UNMAP_KNOB = 0x16
CMD_NAV_DEVICE = 0x17   # Move -> Live: 0x00=left, 0x01=right

# Persistence
BINDINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bindings.json')

# Timing
HEARTBEAT_TICKS = 10  # ~1 second at 100ms/tick
SYSEX_DELAY = 0.003   # 3ms between sysex sends

MAX_PARAM_NAME_LEN = 12


class SchwungDeviceControl(ControlSurface):

    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        with self.component_guard():
            self.log_message('SchwungDeviceControl: initializing')

        self._learn_mode = False
        self._device_list = []
        self._device_index = 0
        self._bank_index = 0
        self._total_banks = 1
        self._active_params = [None] * 8
        self._active_listeners = [None] * 8
        self._bindings = {}
        self._suppressing_feedback = [False] * 8
        self._heartbeat_counter = 0
        self._connected = False
        self._selected_device = None
        self._track_device_listener_installed = False

        self._load_bindings()
        self._setup_listeners()
        self._schedule_heartbeat()

    def disconnect(self):
        self._remove_all_param_listeners()
        self._remove_device_listeners()
        ControlSurface.disconnect(self)

    # =========================================================================
    # Listeners setup
    # =========================================================================

    def _setup_listeners(self):
        self.song().view.add_selected_track_listener(self._on_track_changed)
        self._install_device_listener()

    def _install_device_listener(self):
        if self._track_device_listener_installed:
            return
        track = self.song().view.selected_track
        if track:
            track.view.add_selected_device_listener(self._on_device_changed)
            self._track_device_listener_installed = True

    def _remove_device_listeners(self):
        try:
            track = self.song().view.selected_track
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
        device = self._get_selected_device()
        self._selected_device = device
        if device is not None:
            self._device_list = self._get_device_list()
            try:
                self._device_index = self._device_list.index(device)
            except ValueError:
                self._device_index = 0
            self._bank_index = 0
            self._apply_bindings_for_device(device)
        else:
            self._device_list = []
            self._device_index = 0
            self._remove_all_param_listeners()
        if self._connected:
            self._send_full_state()

    # =========================================================================
    # Device traversal (adapted from roto_control)
    # =========================================================================

    def _get_device_list(self):
        devices = []
        track = self.song().view.selected_track
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
        track = self.song().view.selected_track
        if track:
            return track.view.selected_device
        return None

    # =========================================================================
    # MIDI receive
    # =========================================================================

    def receive_midi(self, midi_bytes):
        if len(midi_bytes) < 1:
            return

        self.log_message('SchwungDeviceControl RX: {}'.format(
            ' '.join('{:02X}'.format(b) for b in midi_bytes)))

        # SysEx
        if midi_bytes[0] == SYSEX_START:
            self._process_sysex(midi_bytes)
            return

        # CC on our channel
        if len(midi_bytes) >= 3:
            status = midi_bytes[0] & 0xF0
            channel = midi_bytes[0] & 0x0F
            if status == 0xB0 and channel == MIDI_CHANNEL:
                cc = midi_bytes[1]
                value = midi_bytes[2]
                if cc in KNOB_CCS:
                    self._handle_knob_cc(cc, value)
                    return
                if cc in NAV_CCS:
                    self._handle_nav_cc(cc, value)
                    return

        # Pass through everything else
        super(SchwungDeviceControl, self).receive_midi(midi_bytes)

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

        if cmd == CMD_HELLO:
            self._on_hello()
        elif cmd == CMD_LEARN_START:
            self._learn_mode = True
            self.log_message('SchwungDeviceControl: learn mode ON')
        elif cmd == CMD_LEARN_STOP:
            self._learn_mode = False
            self.log_message('SchwungDeviceControl: learn mode OFF')
        elif cmd == CMD_LEARN_KNOB:
            if len(data) >= 1:
                self._learn_knob(data[0])
        elif cmd == CMD_KNOB_VALUE:
            if len(data) >= 2:
                self._handle_knob_value(data[0], data[1])
        elif cmd == CMD_REQUEST_STATE:
            self._send_full_state()
        elif cmd == CMD_UNMAP_KNOB:
            if len(data) >= 1:
                self._unmap_knob(data[0])
        elif cmd == CMD_NAV_DEVICE:
            if len(data) >= 1:
                direction = 1 if data[0] == 0x01 else -1
                self._navigate_device(direction)

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

        # Suppress feedback to avoid echo loop
        self._suppressing_feedback[knob_idx] = True
        try:
            param.value = new_value
        except:
            pass
        self._suppressing_feedback[knob_idx] = False

    # =========================================================================
    # Navigation CC handling
    # =========================================================================

    def _handle_nav_cc(self, cc, value):
        if value == 0:
            return  # only act on press

        if cc == CC_DEVICE_LEFT:
            self._navigate_device(-1)
        elif cc == CC_DEVICE_RIGHT:
            self._navigate_device(1)
        elif cc == CC_BANK_UP:
            self._navigate_bank(1)
        elif cc == CC_BANK_DOWN:
            self._navigate_bank(-1)
        elif cc == CC_LEARN_TOGGLE:
            self._learn_mode = not self._learn_mode
            self.log_message('SchwungDeviceControl: learn mode {}'.format(
                'ON' if self._learn_mode else 'OFF'))

    def _navigate_device(self, direction):
        if not self._device_list:
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
        self.song().view.select_device(device)

    def _navigate_bank(self, direction):
        new_bank = self._bank_index + direction
        if new_bank < 0 or new_bank >= self._total_banks:
            return
        self._bank_index = new_bank
        device = self._selected_device
        if device:
            self._apply_bindings_for_device(device)
            self._send_full_state()

    # =========================================================================
    # Learn mode
    # =========================================================================

    def _learn_knob(self, knob_idx):
        self.log_message('SchwungDeviceControl: _learn_knob({})'.format(knob_idx))
        if knob_idx < 0 or knob_idx >= 8:
            return

        # Get the currently selected/focused parameter in Live
        param = self.song().view.selected_parameter
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

        # Compute hashes for persistence
        param_hash = self._get_param_hash(param.name)
        device_hash = self._get_device_hash(device)

        # Store binding
        if device_hash not in self._bindings:
            self._bindings[device_hash] = {}
        self._bindings[device_hash][str(knob_idx)] = {
            'param_index': param_index,
            'param_hash': param_hash.hex(),
            'param_name': param.name
        }

        # Activate immediately
        self._bind_param_to_knob(knob_idx, param)

        # Send ACK to Move
        name_bytes = self._encode_string(param.name, MAX_PARAM_NAME_LEN)
        self._send_sysex(CMD_LEARN_ACK, [knob_idx] + name_bytes + [0])

        self._save_bindings()
        self.log_message('SchwungDeviceControl: learned knob {} -> {} ({})'.format(
            knob_idx, param.name, device.name))

    def _unmap_knob(self, knob_idx):
        if knob_idx < 0 or knob_idx >= 8:
            return

        self._unbind_knob(knob_idx)

        device = self._selected_device
        if device:
            device_hash = self._get_device_hash(device)
            if device_hash in self._bindings:
                self._bindings[device_hash].pop(str(knob_idx), None)
                self._save_bindings()

        # Send updated param info (empty)
        self._send_sysex(CMD_PARAM_INFO, [knob_idx] + self._encode_string('', MAX_PARAM_NAME_LEN) + [0])
        self._send_param_value(knob_idx)

    # =========================================================================
    # Parameter binding and value listeners
    # =========================================================================

    def _bind_param_to_knob(self, knob_idx, param):
        self._unbind_knob(knob_idx)
        self._active_params[knob_idx] = param

        # Create value listener for Live -> Move sync
        def on_value_changed():
            if not self._suppressing_feedback[knob_idx]:
                self._send_param_value(knob_idx)

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

    def _send_param_value(self, knob_idx):
        param = self._active_params[knob_idx]
        if param is None:
            self._send_sysex(CMD_KNOB_VALUE, [knob_idx, 0])
            return
        val_range = param.max - param.min
        if val_range == 0:
            midi_val = 0
        else:
            midi_val = int(127 * (param.value - param.min) / val_range)
        midi_val = max(0, min(127, midi_val))
        self._send_sysex(CMD_KNOB_VALUE, [knob_idx, midi_val])

    # =========================================================================
    # Apply bindings for current device
    # =========================================================================

    def _apply_bindings_for_device(self, device):
        self._remove_all_param_listeners()

        device_hash = self._get_device_hash(device)
        bindings = self._bindings.get(device_hash, {})

        # Gather all mapped knob indices, sorted
        mapped_knobs = sorted(bindings.keys(), key=lambda k: int(k))

        if len(mapped_knobs) <= 8:
            self._total_banks = 1
            self._bank_index = 0
            bank_knobs = mapped_knobs
        else:
            self._total_banks = (len(mapped_knobs) + 7) // 8
            self._bank_index = min(self._bank_index, self._total_banks - 1)
            start = self._bank_index * 8
            bank_knobs = mapped_knobs[start:start + 8]

        for slot, knob_key in enumerate(bank_knobs):
            binding = bindings[knob_key]
            param = self._resolve_param(device, binding)
            if param is not None:
                self._bind_param_to_knob(slot, param)

    def _resolve_param(self, device, binding):
        """Resolve a binding to a live parameter, using hash then falling back to index."""
        param_hash_hex = binding['param_hash']
        param_index = binding['param_index']

        # Try hash match first
        for i, p in enumerate(device.parameters):
            if self._get_param_hash(p.name).hex() == param_hash_hex:
                return p

        # Fall back to index
        if param_index < len(device.parameters):
            return device.parameters[param_index]

        return None

    # =========================================================================
    # State push to Move
    # =========================================================================

    def _send_full_state(self):
        device = self._selected_device
        if device is None:
            self._send_sysex(CMD_DEVICE_INFO, self._encode_string('No Device', MAX_PARAM_NAME_LEN) + [0])
            return

        # Device info
        self._send_sysex(CMD_DEVICE_INFO, self._encode_string(device.name, 20) + [0])
        sleep(SYSEX_DELAY)

        # Device count and index
        self._send_sysex(CMD_DEVICE_COUNT, [min(127, len(self._device_list))])
        sleep(SYSEX_DELAY)
        self._send_sysex(CMD_DEVICE_INDEX, [min(127, self._device_index)])
        sleep(SYSEX_DELAY)

        # Bank info
        self._send_sysex(CMD_BANK_INFO, [self._bank_index, self._total_banks])
        sleep(SYSEX_DELAY)

        # Parameter names
        for i in range(8):
            param = self._active_params[i]
            name = param.name if param else ''
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
        self._send_sysex(CMD_ALL_VALUES, values)

    # =========================================================================
    # Connection / heartbeat
    # =========================================================================

    def _on_hello(self):
        self.log_message('SchwungDeviceControl: HELLO received from Move module')
        self._connected = True
        self._send_sysex(CMD_HEARTBEAT, [])
        # Trigger device detection and full state push
        self._on_device_changed()

    def _schedule_heartbeat(self):
        self.schedule_message(HEARTBEAT_TICKS, self._heartbeat_tick)

    def _heartbeat_tick(self):
        if self._connected:
            self._send_sysex(CMD_HEARTBEAT, [])
        self._schedule_heartbeat()

    # =========================================================================
    # Hashing (adapted from roto_control)
    # =========================================================================

    def _get_param_hash(self, param_name):
        h = hashlib.sha1(param_name.encode('utf-8')).digest()
        return bytes([b & 0x7F for b in h[:6]])

    def _get_device_hash(self, device):
        key = '{}:{}'.format(device.class_name, device.name)
        h = hashlib.sha1(key.encode('utf-8')).digest()
        return h[:8].hex()

    # =========================================================================
    # MIDI send helpers
    # =========================================================================

    def _send_cc(self, cc, value):
        self._send_midi((0xB0 | MIDI_CHANNEL, cc, value))

    def _send_sysex(self, cmd, data):
        msg = (SYSEX_START, 0x00, 0x7D, 0x01, cmd) + tuple(data) + (SYSEX_END,)
        self._send_midi(msg)

    def _encode_string(self, s, max_len):
        """Encode string as 7-bit safe ASCII bytes for SysEx."""
        return [ord(c) & 0x7F for c in s[:max_len]]

    # =========================================================================
    # Persistence
    # =========================================================================

    def _save_bindings(self):
        try:
            with open(BINDINGS_FILE, 'w') as f:
                json.dump(self._bindings, f, indent=2)
            self.log_message('SchwungDeviceControl: bindings saved')
        except Exception as e:
            self.log_message('SchwungDeviceControl: save error: {}'.format(e))

    def _load_bindings(self):
        if not os.path.exists(BINDINGS_FILE):
            self._bindings = {}
            return
        try:
            with open(BINDINGS_FILE, 'r') as f:
                self._bindings = json.load(f)
            self.log_message('SchwungDeviceControl: loaded {} device bindings'.format(
                len(self._bindings)))
        except Exception as e:
            self.log_message('SchwungDeviceControl: load error: {}'.format(e))
            self._bindings = {}
