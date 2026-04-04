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
CC_LEARN_TOGGLE = 84
NAV_CCS = {CC_DEVICE_LEFT, CC_DEVICE_RIGHT, CC_LEARN_TOGGLE}

# SysEx protocol
SYSEX_START = 0xF0
SYSEX_END = 0xF7
SYSEX_HEADER = (SYSEX_START, 0x00, 0x7D, 0x01)

# SysEx commands: Live -> Move
CMD_DEVICE_INFO = 0x01
CMD_PARAM_INFO = 0x02
CMD_DEVICE_COUNT = 0x03
CMD_DEVICE_INDEX = 0x04
# 0x05 was CMD_BANK_INFO (removed)
CMD_LEARN_ACK = 0x06
CMD_HEARTBEAT = 0x07
CMD_ALL_VALUES = 0x08
CMD_PAGE_INFO = 0x09
CMD_PAGE_NAME = 0x0A
CMD_PARAM_VALUE_STRING = 0x0B  # Live -> Move: knob_idx, value string (e.g. "3.5 kHz")
CMD_PARAM_STEPS = 0x0C         # Live -> Move: 8 step counts (0=continuous, N=discrete steps)

# SysEx commands: Move -> Live
CMD_HELLO = 0x10
CMD_LEARN_START = 0x11
CMD_LEARN_STOP = 0x12
CMD_LEARN_KNOB = 0x13
CMD_KNOB_VALUE = 0x14   # Move -> Live: knob_idx, value (0-127)
CMD_REQUEST_STATE = 0x15
CMD_UNMAP_KNOB = 0x16
CMD_NAV_DEVICE = 0x17   # Move -> Live: 0x00=left, 0x01=right
CMD_PAGE_CHANGE = 0x18  # Move -> Live: pageIndex
CMD_REQUEST_VALUE_STRING = 0x19  # Move -> Live: knob_idx

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
        self._current_page = 0   # index into pages array, or -1 if on empty slot
        self._current_slot = 0   # always valid slot index (0-7)
        self._active_params = [None] * 8
        self._active_listeners = [None] * 8
        self._bindings = {}
        self._suppressing_feedback = [False] * 8
        self._slot_page_memory = {}  # {device_hash: {slot_idx: page_array_index}}
        self._heartbeat_counter = 0
        self._connected = False
        self._selected_device = None
        self._track_device_listener_installed = False

        self._bindings_mtime = 0
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
            self._cleanup_provisional_page()
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
        elif cmd == CMD_PAGE_CHANGE:
            if len(data) >= 1:
                self._handle_page_change(data[0])
        elif cmd == CMD_REQUEST_VALUE_STRING:
            if len(data) >= 1:
                self._send_param_value_string(data[0])

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

        # Suppress feedback to avoid echo loop
        self._suppressing_feedback[knob_idx] = True
        try:
            param.value = new_value
        except:
            pass
        self._suppressing_feedback[knob_idx] = False

        # Send formatted value string for overlay display
        self._send_param_value_string(knob_idx)

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
        elif cc == CC_LEARN_TOGGLE:
            self._learn_mode = not self._learn_mode
            if not self._learn_mode:
                self._cleanup_provisional_page()
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

    def _handle_page_change(self, slot_idx):
        if slot_idx < 0 or slot_idx >= 8:
            return
        device = self._selected_device
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

        if slot_idx == current_slot:
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
                and self._bindings[device_hash]['pages'][self._current_page].get('knobs', {})
            )
            if self._learn_mode and pos == len(pages_in_slot) - 1 and current_page_has_knobs:
                # Cycled past last sub-page in learn mode: create provisional page
                pages = self._bindings[device_hash]['pages']
                new_page = {'name': '{}'.format(len(pages) + 1), 'slot': slot_idx, 'knobs': {}}
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

    def _get_current_slot(self, device_hash=None):
        """Return the current slot index."""
        return self._current_slot

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

        device_hash = self._get_device_hash(device)

        # Store binding on current page
        if device_hash not in self._bindings:
            self._bindings[device_hash] = {'pages': []}
        device_entry = self._bindings[device_hash]
        if 'pages' not in device_entry:
            device_entry['pages'] = []
        pages = device_entry['pages']
        # If on empty slot (current_page == -1), create a page for this slot
        if self._current_page < 0:
            new_page = {'name': '{}'.format(self._current_slot + 1), 'slot': self._current_slot, 'knobs': {}}
            pages.append(new_page)
            self._current_page = len(pages) - 1
        # Auto-create pages up to current_page
        while len(pages) <= self._current_page:
            pages.append({'name': '{}'.format(len(pages) + 1), 'slot': self._current_slot, 'knobs': {}})
        pages[self._current_page]['knobs'][str(knob_idx)] = {
            'param_index': param_index,
            'param_name': param.name,
            'short_name': param.name
        }

        # Activate immediately
        self._bind_param_to_knob(knob_idx, param)

        # Send ACK to Move (use short_name for display)
        display_name = pages[self._current_page]['knobs'][str(knob_idx)].get('short_name', param.name)
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
            device_entry = self._bindings.get(device_hash, {})
            pages = device_entry.get('pages', [])
            if self._current_page < len(pages):
                pages[self._current_page]['knobs'].pop(str(knob_idx), None)
                self._save_bindings()

        # Send updated param info (empty)
        self._send_sysex(CMD_PARAM_INFO, [knob_idx] + self._encode_string('', MAX_PARAM_NAME_LEN) + [0])
        self._send_param_value(knob_idx)

    def _cleanup_provisional_page(self):
        """Remove current page if it has no knob bindings (provisional from learn mode)."""
        device = self._selected_device
        if device is None:
            return
        device_hash = self._get_device_hash(device)
        pages = self._bindings.get(device_hash, {}).get('pages', [])
        if self._current_page < 0 or self._current_page >= len(pages):
            return
        page = pages[self._current_page]
        if page.get('knobs', {}):
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

        self._save_bindings()
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
            if not self._suppressing_feedback[knob_idx]:
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
        self._remove_all_param_listeners()

        device_hash = self._get_device_hash(device)
        device_entry = self._bindings.get(device_hash, {})
        pages = device_entry.get('pages', [])

        if 0 <= self._current_page < len(pages):
            page = pages[self._current_page]
            for knob_key, binding in page.get('knobs', {}).items():
                knob_idx = int(knob_key)
                if 0 <= knob_idx < 8:
                    param = self._resolve_param(device, binding)
                    if param is not None:
                        self._bind_param_to_knob(knob_idx, param)

    def _get_display_name(self, device_hash, knob_idx, fallback):
        """Get short_name from binding for display, falling back to the full param name."""
        device_entry = self._bindings.get(device_hash, {})
        pages = device_entry.get('pages', [])
        if 0 <= self._current_page < len(pages):
            binding = pages[self._current_page].get('knobs', {}).get(str(knob_idx))
            if binding:
                return binding.get('short_name', fallback)
        return fallback

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

        # Page info (slot-based)
        device_hash = self._get_device_hash(device)
        device_entry = self._bindings.get(device_hash, {})
        pages = device_entry.get('pages', [])
        current_slot = self._get_current_slot(device_hash)
        slot_count = max(1, self._get_slot_count(device_hash))
        self._send_sysex(CMD_PAGE_INFO, [current_slot, slot_count])
        sleep(SYSEX_DELAY)

        # Page names (one per slot, showing active sub-page name)
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
        self._send_sysex(CMD_ALL_VALUES, values)
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
        self._check_bindings_file()
        self._schedule_heartbeat()

    def _check_bindings_file(self):
        """Reload bindings.json if it was modified externally."""
        try:
            mtime = os.path.getmtime(BINDINGS_FILE)
        except OSError:
            return
        if mtime == self._bindings_mtime:
            return
        self.log_message('SchwungDeviceControl: bindings.json changed, reloading')
        self._load_bindings()
        device = self._selected_device
        if device:
            self._apply_bindings_for_device(device)
            if self._connected:
                self._send_full_state()

    # =========================================================================
    # Hashing (adapted from roto_control)
    # =========================================================================


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
            # Sort pages by slot and knobs by index for readable JSON
            sorted_bindings = {}
            for device_hash, entry in self._bindings.items():
                pages = entry.get('pages', [])
                sorted_pages = sorted(pages, key=lambda p: p.get('slot', 0))
                for page in sorted_pages:
                    knobs = page.get('knobs', {})
                    page['knobs'] = dict(sorted(knobs.items(), key=lambda kv: int(kv[0])))
                sorted_bindings[device_hash] = {'pages': sorted_pages}
            self._bindings = sorted_bindings
            with open(BINDINGS_FILE, 'w') as f:
                json.dump(self._bindings, f, indent=2)
            self._bindings_mtime = os.path.getmtime(BINDINGS_FILE)
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
            # Migrate old format: device_hash -> {knob_idx: binding} to pages format
            migrated = False
            for device_hash, entry in self._bindings.items():
                if 'pages' not in entry:
                    # Old format — wrap all knobs into page 1
                    self._bindings[device_hash] = {
                        'pages': [{'name': '1', 'knobs': entry}]
                    }
                    migrated = True
            # Migrate pages without slot field
            for device_hash, entry in self._bindings.items():
                for i, page in enumerate(entry.get('pages', [])):
                    if 'slot' not in page:
                        page['slot'] = i
                        migrated = True
            if migrated:
                self._save_bindings()
                self.log_message('SchwungDeviceControl: migrated bindings format')
            self._bindings_mtime = os.path.getmtime(BINDINGS_FILE)
            self.log_message('SchwungDeviceControl: loaded {} device bindings'.format(
                len(self._bindings)))
        except Exception as e:
            self.log_message('SchwungDeviceControl: load error: {}'.format(e))
            self._bindings = {}
