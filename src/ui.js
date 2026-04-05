/*
 * Schwung Device Control - Overtake Module
 *
 * Two-way Ableton Live device parameter control with learn mode.
 * Communicates with SchwungDeviceControl Remote Script via MIDI cable 2.
 */

import {
  decodeDelta,
  decodeAcceleratedDelta,
  setLED,
  setButtonLED,
  isNoiseMessage,
} from "/data/UserData/move-anything/shared/input_filter.mjs";
import {
  MoveBack,
  MoveMenu,
  MoveShift,
  MoveUp,
  MoveDown,
  MoveLeft,
  MoveRight,
  MoveKnob1,
  MoveKnob2,
  MoveKnob3,
  MoveKnob4,
  MoveKnob5,
  MoveKnob6,
  MoveKnob7,
  MoveKnob8,
  MoveMainKnob,
  MoveMainButton,
  MoveDelete,
  MoveRow3,
  MoveRow4,
  White,
  Black,
  BrightGreen,
  BrightRed,
  Cyan,
  DarkGrey,
  WhiteLedBright,
} from "/data/UserData/move-anything/shared/constants.mjs";

/* ============================================================================
 * Constants
 * ============================================================================ */

const SCREEN_WIDTH = 128;
const SCREEN_HEIGHT = 64;
const CABLE = 2;
const MIDI_CHANNEL = 0x0f; // channel 16

// Knob CCs (same as Move hardware knobs, used for LED addressing)
const KNOB_CCS = [71, 72, 73, 74, 75, 76, 77, 78];

// SysEx protocol (for variable-length string/bulk data only)
const SYSEX_HEADER = [0xf0, 0x00, 0x7d, 0x01];

// SysEx commands: Live -> Move (string/bulk data — must stay SysEx)
const CMD_DEVICE_INFO = 0x01;
const CMD_PARAM_INFO = 0x02;
const CMD_LEARN_ACK = 0x06;
const CMD_PAGE_INFO = 0x09;
const CMD_PAGE_NAME = 0x0a;
const CMD_PARAM_VALUE_STRING = 0x0b;
const CMD_PARAM_STEPS = 0x0c;
const CMD_SLOT_SUBPAGE_INFO = 0x0d;
const CMD_DEVICE_LIST_RESPONSE = 0x0e;
const CMD_TRACK_LIST_RESPONSE = 0x12;

// Note commands: Live -> Move (simple data — note=cmd, velocity=value)
const CMD_HEARTBEAT = 0x07;       // vel = 1
const CMD_FAV_ADD_ACK = 0x0f;     // vel = fav_index * 16 + result + 1
const CMD_SET_ADD_ACK = 0x20;     // vel = set_index * 16 + result + 1

// Note commands: Move -> Live (simple triggers — note=cmd, velocity=value)
const CMD_HELLO = 0x10;
const CMD_LEARN_START = 0x11;
const CMD_LEARN_STOP = 0x12;
const CMD_LEARN_KNOB = 0x13;          // vel = knob_idx + 1
const CMD_REQUEST_STATE = 0x15;
const CMD_UNMAP_KNOB = 0x16;          // vel = knob_idx + 1
const CMD_NAV_DEVICE = 0x17;          // vel = direction + 1 (0x00→1, 0x01→2)
const CMD_PAGE_CHANGE = 0x18;         // vel = slotIdx + 1
const CMD_REQUEST_VALUE_STRING = 0x19; // vel = knob_idx + 1
const CMD_PAGE_SEQUENTIAL = 0x1a;     // vel = direction + 1
const CMD_RESET_PARAM = 0x1b;         // vel = knob_idx + 1
const CMD_DEVICE_LIST_REQUEST = 0x1c;  // vel = offset + 1
const CMD_DEVICE_SELECT = 0x1d;        // vel = device_index + 1
const CMD_FAV_ADD = 0x1e;              // vel = fav_index * 16 + knob_idx + 1
const CMD_SET_ADD = 0x1f;              // vel = set_index * 16 + knob_idx + 1

// Track browser commands
const CMD_TRACK_LIST_REQUEST = 0x23;   // vel = offset + 1
const CMD_TRACK_SELECT = 0x24;         // vel = track_index + 1

// Note mode commands
const CMD_NOTE_MODE = 0x21;            // vel = 1+1 (on) or 0+1 (off)
const CMD_OCTAVE = 0x22;               // vel = 1+1 (up) or 0+1 (down)
const CMD_NOTE_LAYOUT_INFO = 0x11;     // SysEx: root_note, is_in_key, interval, scale_notes...

// Pad note range (same as Move hardware)
const PAD_NOTE_START = 68;
const PAD_NOTE_END = 99;
const PAD_CHANNEL = 0x00;  // channel 1 for pad MIDI (separate from command ch16)

// Timing
const HEARTBEAT_TIMEOUT_TICKS = 720; // ~3 seconds at ~240fps (tick rate is faster than expected)
const RECONNECT_INTERVAL_TICKS = 480; // ~2 seconds between HELLO retries when disconnected
const LEDS_PER_FRAME = 8;
const HOLD_THRESHOLD_TICKS = 72; // ~300ms at ~240fps — short press vs hold

/* ============================================================================
 * HoldToggle — short press toggles, long press is momentary
 *
 * Usage:
 *   const ht = new HoldToggle(onActivate, onDeactivate);
 *   // on button press:   ht.press()
 *   // on button release:  ht.release()
 *   // every tick:         ht.tick()
 *   // read state:         ht.active
 * ============================================================================ */

class HoldToggle {
  constructor(onActivate, onDeactivate) {
    this.active = false;
    this._held = false;
    this._holdTicks = 0;
    this._wasHold = false;
    this._onActivate = onActivate;
    this._onDeactivate = onDeactivate;
  }

  press() {
    this._held = true;
    this._holdTicks = 0;
    this._wasHold = false;
    if (!this.active) {
      this.active = true;
      this._onActivate();
    }
  }

  release() {
    this._held = false;
    if (this._wasHold) {
      // long press — deactivate on release (momentary)
      this.active = false;
      this._onDeactivate();
    }
    // short press — stay toggled on, next short press will toggle off
  }

  tick() {
    if (this._held) {
      this._holdTicks++;
      if (this._holdTicks >= HOLD_THRESHOLD_TICKS) {
        this._wasHold = true;
      }
    }
  }

  /** Programmatically deactivate (e.g. on disconnect). */
  deactivate() {
    if (this.active) {
      this.active = false;
      this._held = false;
      this._onDeactivate();
    }
  }

  /** Short press while already active — toggle off. */
  toggleOff() {
    this.active = false;
    this._onDeactivate();
  }

  /** Close if toggled on (not held). For auto-close after item selection. */
  closeIfToggled() {
    if (this.active && !this._held) {
      this.active = false;
      this._onDeactivate();
    }
  }
}

/* ============================================================================
 * State
 * ============================================================================ */

let connected = false;
let heartbeatTimer = 0;
let reconnectTimer = 0;
let learnMode = false;
let noteMode = true;

// Note layout info (from Ableton, for pad coloring)
let noteLayoutRoot = 0;
let noteLayoutInKey = true;
let noteLayoutInterval = 0;
let noteLayoutScaleNotes = [0, 2, 4, 5, 7, 9, 11];  // default major
let shiftHeld = false;
let deleteHeld = false;
let needsRedraw = true;
let tickCount = 0;
let touchedKnob = -1; // -1 = none, 0-7 = knob index (derived from touchStack)
let touchStack = []; // stack of currently touched knob indices, most recent last
let marqueeOffset = 0; // pixel scroll offset for touched knob name
let marqueeKnob = -1; // which knob marquee is active for
// marqueeDirection no longer used (loop mode, not bounce)

// UI layout: 'A' = original (2 cols x 4 rows with bar), 'B' = compact (4 cols x 2 rows with pixel line)
let uiLayout = "B";

// Device state (from Ableton)
let deviceName = "";
let deviceIndex = 0;
let deviceCount = 0;
let paramNames = new Array(8).fill("");
let paramValues = new Array(8).fill(0);
let paramSteps = new Array(8).fill(0); // 0 = continuous, N = discrete steps
let paramAccum = new Array(8).fill(0); // fractional accumulator for discrete knobs

// Page state
let currentPage = 0;
let pageCount = 1;
let pageNames = ["1", "2", "3", "4", "5", "6", "7", "8"];
let slotSubpageCounts = new Array(8).fill(1);
let slotActiveSubpage = new Array(8).fill(0);

// Favourite page state (single slot 8 with subpages)
let favState = 0;              // 0=empty, 1=has bindings, 2=active
let favSubCount = 0;           // number of fav subpages
let favActiveSub = 0;          // which subpage is active (0-based)
let favHeld = -1;              // which fav button is held (-1/0/1)
let favUsedAsModifier = false; // true if knob tapped during fav hold
let favFeedbackTimer = 0;
let favFeedbackText = "";

// Set page state (per-set cross-device favourites, slot 9)
let setState = 0;              // 0=empty, 1=has bindings, 2=active
let setSubCount = 0;
let setActiveSub = 0;
let setHeld = -1;              // which set button is held (-1/0/1)
let setUsedAsModifier = false;

// Step button notes (step 1-8 = notes 16-23)
const STEP_NOTE_BASE = 16;
const FAV_STEP_NOTE_BASE = 24; // step 9-10 = notes 24-25
const SET_STEP_NOTE_BASE = 28; // step 13-14 = notes 28-29

// Value overlay state
let overlayKnob = -1; // which knob's overlay is showing (-1 = none)
let overlayValueStr = ""; // formatted value string from Ableton
let overlayTimer = 0; // ticks remaining before overlay auto-hides
const OVERLAY_HOLD_TICKS = 1; // dismiss immediately on release

// Device browser state
let deviceBrowseOffset = 0;  // first device index on current page
let deviceBrowseTotal = 0;   // total device count
let deviceBrowseNames = new Array(8).fill(""); // names for current page of 8

function enterDeviceBrowse() {
  trackBrowseToggle.deactivate();
  deviceBrowseOffset = 0;
  sendNote(CMD_DEVICE_LIST_REQUEST, 0 + 1);
  needsRedraw = true;
}

function exitDeviceBrowse() {
  updateStepLEDs();
  updateKnobLEDs();
  updateNavLEDs();
  setButtonLED(MoveMenu, WhiteLedBright);
  setButtonLED(MoveBack, WhiteLedBright);
  needsRedraw = true;
}

const deviceBrowseToggle = new HoldToggle(enterDeviceBrowse, exitDeviceBrowse);

// Track browser state
let trackBrowseOffset = 0;
let trackBrowseTotal = 0;
let trackBrowseNames = new Array(8).fill("");
let trackBrowseCurrentIndex = -1; // index of currently selected track

function enterTrackBrowse() {
  deviceBrowseToggle.deactivate();
  trackBrowseOffset = 0;
  sendNote(CMD_TRACK_LIST_REQUEST, 0 + 1);
  needsRedraw = true;
}

function exitTrackBrowse() {
  updateStepLEDs();
  updateKnobLEDs();
  updateNavLEDs();
  setButtonLED(MoveMenu, WhiteLedBright);
  setButtonLED(MoveBack, WhiteLedBright);
  needsRedraw = true;
}

const trackBrowseToggle = new HoldToggle(enterTrackBrowse, exitTrackBrowse);

// Progressive LED init
let ledInitPending = true;
let ledInitIndex = 0;

function resetUIState() {
  deviceName = "";
  deviceIndex = 0;
  deviceCount = 0;
  paramNames = new Array(8).fill("");
  paramValues = new Array(8).fill(0);
  paramSteps = new Array(8).fill(0);
  paramAccum = new Array(8).fill(0);
  currentPage = 0;
  pageCount = 1;
  pageNames = ["1", "2", "3", "4", "5", "6", "7", "8"];
  slotSubpageCounts = new Array(8).fill(1);
  slotActiveSubpage = new Array(8).fill(0);
  favState = 0;
  favSubCount = 0;
  favActiveSub = 0;
  setState = 0;
  setSubCount = 0;
  setActiveSub = 0;
  learnMode = false;
  noteMode = true;
  overlayKnob = -1;
  overlayValueStr = "";
  overlayTimer = 0;
  deviceBrowseToggle.deactivate();
  trackBrowseToggle.deactivate();
  touchStack = [];
  touchedKnob = -1;
  marqueeOffset = 0;
  marqueeKnob = -1;
}

/* ============================================================================
 * USB-MIDI SysEx Framing
 *
 * move_midi_external_send expects raw USB-MIDI packets (4 bytes each).
 * SysEx needs proper CIN codes:
 *   0x4 = SysEx start or continue (3 data bytes)
 *   0x5 = SysEx end with 1 byte
 *   0x6 = SysEx end with 2 bytes
 *   0x7 = SysEx end with 3 bytes
 * ============================================================================ */

function sendSysEx(data) {
  // data = [F0, ..., F7] complete SysEx message
  let i = 0;
  while (i < data.length) {
    const remaining = data.length - i;
    if (remaining > 3) {
      // Start or continue: CIN 0x4, 3 data bytes
      move_midi_external_send([
        (CABLE << 4) | 0x4,
        data[i],
        data[i + 1],
        data[i + 2],
      ]);
      i += 3;
    } else if (remaining === 3) {
      // End with 3 bytes: CIN 0x7
      move_midi_external_send([
        (CABLE << 4) | 0x7,
        data[i],
        data[i + 1],
        data[i + 2],
      ]);
      i += 3;
    } else if (remaining === 2) {
      // End with 2 bytes: CIN 0x6
      move_midi_external_send([(CABLE << 4) | 0x6, data[i], data[i + 1], 0]);
      i += 2;
    } else {
      // End with 1 byte: CIN 0x5
      move_midi_external_send([(CABLE << 4) | 0x5, data[i], 0, 0]);
      i += 1;
    }
  }
}

function sendNote(note, velocity) {
  move_midi_external_send([
    (CABLE << 4) | 0x09, // CIN for Note On
    0x90 | MIDI_CHANNEL,
    note,
    velocity,
  ]);
}

function sendCC(cc, value) {
  move_midi_external_send([
    (CABLE << 4) | 0x0b, // CIN for CC
    0xb0 | MIDI_CHANNEL,
    cc,
    value,
  ]);
}

function sendPadNoteOn(note, velocity) {
  move_midi_external_send([
    (CABLE << 4) | 0x09, // CIN for Note On
    0x90 | PAD_CHANNEL,
    note,
    velocity,
  ]);
}

function sendPadNoteOff(note) {
  move_midi_external_send([
    (CABLE << 4) | 0x08, // CIN for Note Off
    0x80 | PAD_CHANNEL,
    note,
    0,
  ]);
}

/* ============================================================================
 * Incoming SysEx Parser
 * ============================================================================ */

// SysEx accumulator (messages may arrive split across multiple calls)
let sysexBuffer = null;

let extMidiLogCount = 0;
function processMidiExternal(data) {
  if (!data || data.length < 1) return;
  if (extMidiLogCount < 30) {
    extMidiLogCount++;
    const hex = Array.from(data).map(b => b.toString(16).padStart(2, '0')).join(' ');
    console.log(`[DC] ext midi: [${hex}] len=${data.length}`);
  }
  const status = data[0] & 0xf0;

  // SysEx start
  if (data[0] === 0xf0) {
    sysexBuffer = Array.from(data);
    // Check if complete (ends with F7)
    if (data[data.length - 1] === 0xf7) {
      handleSysEx(sysexBuffer);
      sysexBuffer = null;
    }
    return;
  }

  // SysEx continuation
  if (sysexBuffer !== null) {
    for (let i = 0; i < data.length; i++) {
      sysexBuffer.push(data[i]);
      if (data[i] === 0xf7) {
        handleSysEx(sysexBuffer);
        sysexBuffer = null;
        return;
      }
    }
    return;
  }

  // Note On on our channel — command dispatch
  if (status === 0x90 && (data[0] & 0x0f) === MIDI_CHANNEL) {
    handleNoteFromAbleton(data[1], data[2]);
    return;
  }

  // CC on our channel
  if (status === 0xb0 && (data[0] & 0x0f) === MIDI_CHANNEL) {
    handleCCFromAbleton(data[1], data[2]);
  }
}

function handleSysEx(msg) {
  // Validate header: F0 00 7D 01 <cmd> ... F7
  if (msg.length < 6) {
    return;
  }
  if (
    msg[0] !== 0xf0 ||
    msg[1] !== 0x00 ||
    msg[2] !== 0x7d ||
    msg[3] !== 0x01
  ) {
    return;
  }

  const cmd = msg[4];
  const payload = msg.slice(5, -1); // strip F7

  switch (cmd) {
    case CMD_DEVICE_INFO: {
      deviceName = decodeString(payload);
      // Find null terminator, count and index follow
      const nullIdx = payload.indexOf(0);
      if (nullIdx >= 0 && nullIdx + 2 < payload.length) {
        deviceCount = payload[nullIdx + 1];
        deviceIndex = payload[nullIdx + 2];
      }
      updateNavLEDs();
      needsRedraw = true;
      break;
    }

    case CMD_PARAM_INFO:
      if (payload.length >= 2) {
        const idx = payload[0];
        if (idx >= 0 && idx < 8) {
          paramNames[idx] = decodeString(payload.slice(1));
          needsRedraw = true;
        }
      }
      break;

    case CMD_PAGE_INFO:
      if (payload.length >= 2) {
        currentPage = payload[0];
        pageCount = payload[1];
        if (payload.length >= 5) {
          favState = Math.max(0, payload[2] - 1);
          favSubCount = Math.max(0, payload[3] - 1);
          favActiveSub = Math.max(0, payload[4] - 1);
        }
        if (payload.length >= 8) {
          setState = Math.max(0, payload[5] - 1);
          setSubCount = Math.max(0, payload[6] - 1);
          setActiveSub = Math.max(0, payload[7] - 1);
        }
        updateStepLEDs();
        needsRedraw = true;
      }
      break;

    case CMD_PAGE_NAME:
      if (payload.length >= 2) {
        const pi = payload[0];
        if (pi >= 0 && pi < 8) {
          pageNames[pi] = decodeString(payload.slice(1));
        }
        needsRedraw = true;
      }
      break;

    case CMD_LEARN_ACK:
      if (payload.length >= 2) {
        const idx = payload[0];
        if (idx >= 0 && idx < 8) {
          paramNames[idx] = decodeString(payload.slice(1));
          needsRedraw = true;
        }
      }
      break;

    case CMD_PARAM_VALUE_STRING:
      if (payload.length >= 2) {
        const vi = payload[0];
        if (vi >= 0 && vi < 8) {
          overlayValueStr = decodeString(payload.slice(1));
          overlayKnob = vi;
          overlayTimer = OVERLAY_HOLD_TICKS;
          needsRedraw = true;
        }
      }
      break;

    case CMD_PARAM_STEPS:
      // Values are offset by +1 to avoid 0x00 in SysEx transport
      for (let i = 0; i < Math.min(8, payload.length); i++) {
        paramSteps[i] = Math.max(0, payload[i] - 1);
      }
      break;

    case CMD_SLOT_SUBPAGE_INFO:
      // Per-slot [subpage_count, active_subpage], offset by +1
      for (let i = 0; i < Math.min(8, Math.floor(payload.length / 2)); i++) {
        slotSubpageCounts[i] = Math.max(0, payload[i * 2] - 1);
        slotActiveSubpage[i] = Math.max(0, payload[i * 2 + 1] - 1);
      }
      needsRedraw = true;
      break;

    case CMD_DEVICE_LIST_RESPONSE:
      // [offset, total, name1\0, name2\0, ...]
      if (payload.length >= 2) {
        deviceBrowseOffset = payload[0];
        deviceBrowseTotal = payload[1];
        deviceBrowseNames.fill("");
        let nameIdx = 0;
        let strStart = 2;
        for (let i = 2; i < payload.length && nameIdx < 8; i++) {
          if (payload[i] === 0) {
            deviceBrowseNames[nameIdx] = decodeString(payload.slice(strStart, i + 1));
            nameIdx++;
            strStart = i + 1;
          }
        }
        updateDeviceBrowseLEDs();
        needsRedraw = true;
      }
      break;

    case CMD_TRACK_LIST_RESPONSE:
      // [offset, total, current_track_index, name1\0, name2\0, ...]
      if (payload.length >= 3) {
        trackBrowseOffset = payload[0];
        trackBrowseTotal = payload[1];
        trackBrowseCurrentIndex = payload[2];
        trackBrowseNames.fill("");
        let trackNameIdx = 0;
        let trackStrStart = 3;
        for (let i = 3; i < payload.length && trackNameIdx < 8; i++) {
          if (payload[i] === 0) {
            trackBrowseNames[trackNameIdx] = decodeString(payload.slice(trackStrStart, i + 1));
            trackNameIdx++;
            trackStrStart = i + 1;
          }
        }
        updateTrackBrowseLEDs();
        needsRedraw = true;
      }
      break;

    case CMD_NOTE_LAYOUT_INFO:
      // [root_note, is_in_key, interval, scale_note0, scale_note1, ...]
      if (payload.length >= 3) {
        noteLayoutRoot = payload[0];
        noteLayoutInKey = payload[1] > 0;
        noteLayoutInterval = payload[2];
        noteLayoutScaleNotes = [];
        for (let i = 3; i < payload.length; i++) {
          noteLayoutScaleNotes.push(payload[i]);
        }
        if (noteMode) {
          updatePadLEDs();
        }
        needsRedraw = true;
      }
      break;
  }
}

function handleNoteFromAbleton(note, vel) {
  console.log(`[DC] note from ableton: note=${note} vel=${vel}`);
  switch (note) {
    case CMD_HEARTBEAT:
      console.log("[DC] heartbeat received!");
      if (!connected) {
        // Reconnecting — reset stale state and send HELLO so Ableton pushes fresh state
        resetUIState();
        connected = true;
        updateStepLEDs();
        updateNavLEDs();
        updatePadLEDs();
        sendNote(CMD_HELLO, 1);
        sendNote(CMD_NOTE_MODE, 1 + 1);
        console.log("[DC] reconnected, sent HELLO");
      }
      heartbeatTimer = 0;
      needsRedraw = true;
      break;

    // CMD_DEVICE_COUNT and CMD_DEVICE_INDEX now packed into CMD_DEVICE_INFO sysex

    case CMD_FAV_ADD_ACK:
    case CMD_SET_ADD_ACK: {
      const result = (vel - 1) & 0x0f;
      if (result === 0) favFeedbackText = "Added";
      else if (result === 1) favFeedbackText = "Full";
      else favFeedbackText = "Empty";
      favFeedbackTimer = 180;
      needsRedraw = true;
      break;
    }
  }
}

function handleCCFromAbleton(cc, value) {
  // Knob value feedback from Ableton (CC 0-7)
  if (cc >= 0 && cc < 8) {
    paramValues[cc] = value;
    setButtonLED(KNOB_CCS[cc], valueToKnobColor(value, learnMode));
    needsRedraw = true;
  }
}

function decodeString(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) {
    if (bytes[i] === 0) break;
    s += String.fromCharCode(bytes[i] & 0x7f);
  }
  return s;
}

/* ============================================================================
 * Hardware Input (from Move pads/knobs/buttons)
 * ============================================================================ */

function handleMidiInternal(data) {
  if (!data || data.length < 3) return;
  if (isNoiseMessage(data)) return;

  const status = data[0] & 0xf0;
  const d1 = data[1];
  const d2 = data[2];

  if (status === 0xb0) {
    handleInternalCC(d1, d2);
  } else if (status === 0x90 && d2 > 0) {
    handleInternalNoteOn(d1, d2);
  } else if (status === 0x80 || (status === 0x90 && d2 === 0)) {
    handleInternalNoteOff(d1);
  }
}

function handleInternalCC(cc, value) {
  // Shift state
  if (cc === MoveShift) {
    shiftHeld = value > 63;
    return;
  }

  // Delete (X) button state
  if (cc === MoveDelete) {
    deleteHeld = value > 63;
    return;
  }

  // Track 4 button (MoveRow4 = CC 40) — device browser (short press = toggle, long press = momentary)
  if (cc === MoveRow4) {
    if (value > 63) {
      if (deviceBrowseToggle.active) {
        deviceBrowseToggle.toggleOff();
      } else {
        deviceBrowseToggle.press();
      }
    } else {
      deviceBrowseToggle.release();
    }
    return;
  }

  // Row 3 button (MoveRow3 = CC 41) — track browser (short press = toggle, long press = momentary)
  if (cc === MoveRow3) {
    if (value > 63) {
      if (trackBrowseToggle.active) {
        trackBrowseToggle.toggleOff();
      } else {
        trackBrowseToggle.press();
      }
    } else {
      trackBrowseToggle.release();
    }
    return;
  }

  // In device browse mode, intercept arrows for paging
  if (deviceBrowseToggle.active) {
    if (cc === MoveLeft && value > 63) {
      if (deviceBrowseOffset > 0) {
        const newOffset = Math.max(0, deviceBrowseOffset - 8);
        sendNote(CMD_DEVICE_LIST_REQUEST, newOffset + 1);
      }
      return;
    }
    if (cc === MoveRight && value > 63) {
      if (deviceBrowseOffset + 8 < deviceBrowseTotal) {
        sendNote(CMD_DEVICE_LIST_REQUEST, deviceBrowseOffset + 8 + 1);
      }
      return;
    }
    // Absorb all other CCs in browse mode (knobs, back, menu, etc.)
    return;
  }

  // In track browse mode, intercept arrows for paging
  if (trackBrowseToggle.active) {
    if (cc === MoveLeft && value > 63) {
      if (trackBrowseOffset > 0) {
        const newOffset = Math.max(0, trackBrowseOffset - 8);
        sendNote(CMD_TRACK_LIST_REQUEST, newOffset + 1);
      }
      return;
    }
    if (cc === MoveRight && value > 63) {
      if (trackBrowseOffset + 8 < trackBrowseTotal) {
        sendNote(CMD_TRACK_LIST_REQUEST, trackBrowseOffset + 8 + 1);
      }
      return;
    }
    // Absorb all other CCs in browse mode
    return;
  }

  // Back button
  if (cc === MoveBack && value > 63) {
    if (learnMode) {
      learnMode = false;
      sendNote(CMD_LEARN_STOP, 1);
      needsRedraw = true;
      return;
    }
    host_return_to_menu();
    return;
  }

  // Menu button - toggle learn mode
  if (cc === MoveMenu && value > 63) {
    learnMode = !learnMode;
    sendNote(learnMode ? CMD_LEARN_START : CMD_LEARN_STOP, 1);
    needsRedraw = true;
    return;
  }

  // Arrow keys - device navigation
  if (cc === MoveLeft && value > 63) {
    sendNote(CMD_NAV_DEVICE, 0x00 + 1); // left = -1
    return;
  }
  if (cc === MoveRight && value > 63) {
    sendNote(CMD_NAV_DEVICE, 0x01 + 1); // right = +1
    return;
  }
  // Up arrow — octave up
  if (cc === MoveUp && value > 63) {
    if (noteMode) {
      sendNote(CMD_OCTAVE, 1 + 1);
    }
    return;
  }

  // Down arrow — octave down
  if (cc === MoveDown && value > 63) {
    if (noteMode) {
      sendNote(CMD_OCTAVE, 0 + 1);
    }
    return;
  }

  // Main wheel — sequential page/subpage navigation
  if (cc === MoveMainKnob) {
    const delta = decodeDelta(value);
    if (delta > 0) {
      sendNote(CMD_PAGE_SEQUENTIAL, 0x01 + 1);
    } else if (delta < 0) {
      sendNote(CMD_PAGE_SEQUENTIAL, 0x00 + 1);
    }
    return;
  }

  // Knob turns
  const knobIdx = KNOB_CCS.indexOf(cc);
  if (knobIdx >= 0) {
    handleKnobTurn(knobIdx, value);
    return;
  }
}

function handleKnobTurn(idx, rawValue) {
  if (!connected) return;

  if (learnMode) {
    // In learn mode, twist a knob to bind it
    sendNote(CMD_LEARN_KNOB, idx + 1);
    return;
  }

  const numSteps = paramSteps[idx];

  if (numSteps >= 2) {
    // Discrete parameter: accumulate fractional deltas, snap for output
    const rawDelta = decodeAcceleratedDelta(rawValue, idx);
    const scale = Math.min(0.3, 1.2 / numSteps);
    paramAccum[idx] += rawDelta * scale;
    // Only advance when accumulator crosses a full step
    const stepSize = 127 / (numSteps - 1);
    const currentStep = Math.round(paramValues[idx] / stepSize);
    const accumSteps = Math.trunc(paramAccum[idx] / stepSize);
    if (accumSteps === 0) return;
    paramAccum[idx] -= accumSteps * stepSize;
    const newStep = Math.max(0, Math.min(numSteps - 1, currentStep + accumSteps));
    paramValues[idx] = Math.round(newStep * stepSize);
  } else {
    // Continuous parameter: accelerated delta
    const rawDelta = decodeAcceleratedDelta(rawValue, idx);
    const delta =
      Math.round(rawDelta * 0.2) || (rawDelta > 0 ? 1 : rawDelta < 0 ? -1 : 0);
    paramValues[idx] = Math.max(0, Math.min(127, paramValues[idx] + delta));
  }

  // Show overlay (will be populated when Ableton sends back the value string)
  overlayKnob = idx;
  overlayTimer = OVERLAY_HOLD_TICKS;

  // Send absolute value to Ableton
  sendCC(idx, paramValues[idx]);

  // Update this knob's LED immediately
  const colour = valueToKnobColor(paramValues[idx], learnMode);
  setButtonLED(KNOB_CCS[idx], colour);

  needsRedraw = true;
}

function handleInternalNoteOn(note, velocity) {
  // Pad press in note mode — forward to Ableton via PlayableComponent
  if (noteMode && note >= PAD_NOTE_START && note <= PAD_NOTE_END) {
    sendPadNoteOn(note, velocity);
    setLED(note, White);
    return;
  }

  // Capacitive knob touch (notes 0-7)
  if (note < 8) {
    // Remove if already in stack (re-touch), then push to top
    const si = touchStack.indexOf(note);
    if (si >= 0) touchStack.splice(si, 1);
    touchStack.push(note);
    touchedKnob = note;
    if (marqueeKnob !== note) {
      marqueeKnob = note;
      marqueeOffset = 0;
    }
    // Fav held + touch = add binding to favourite page
    if (favHeld >= 0 && connected && paramNames[note]) {
      sendNote(CMD_FAV_ADD, favHeld * 16 + note + 1);
      favUsedAsModifier = true;
      overlayKnob = note;
      overlayTimer = OVERLAY_HOLD_TICKS;
      needsRedraw = true;
      return;
    }
    // Set held + touch = add binding to set page
    if (setHeld >= 0 && connected && paramNames[note]) {
      sendNote(CMD_SET_ADD, setHeld * 16 + note + 1);
      setUsedAsModifier = true;
      overlayKnob = note;
      overlayTimer = OVERLAY_HOLD_TICKS;
      needsRedraw = true;
      return;
    }
    // Delete held + touch = unmap knob
    if (deleteHeld && !learnMode && connected && paramNames[note]) {
      sendNote(CMD_UNMAP_KNOB, note + 1);
      needsRedraw = true;
      return;
    }
    // Shift held + touch = reset param to default
    if (shiftHeld && !learnMode && connected && paramNames[note]) {
      sendNote(CMD_RESET_PARAM, note + 1);
      overlayKnob = note;
      overlayTimer = OVERLAY_HOLD_TICKS;
      needsRedraw = true;
      return;
    }
    // In learn mode, touching a knob is enough to bind it
    if (learnMode && connected) {
      sendNote(CMD_LEARN_KNOB, note + 1);
    }
    // Show overlay on touch if param is mapped
    if (!learnMode && connected && paramNames[note]) {
      overlayKnob = note;
      overlayTimer = OVERLAY_HOLD_TICKS;
      sendNote(CMD_REQUEST_VALUE_STRING, note + 1);
    }
    needsRedraw = true;
    return;
  }

  // Step buttons 1-8 (notes 16-23)
  if (note >= STEP_NOTE_BASE && note < STEP_NOTE_BASE + 8) {
    const slotIdx = note - STEP_NOTE_BASE;
    // In device browse mode, select device
    if (deviceBrowseToggle.active) {
      const devIdx = deviceBrowseOffset + slotIdx;
      if (devIdx < deviceBrowseTotal && deviceBrowseNames[slotIdx]) {
        sendNote(CMD_DEVICE_SELECT, devIdx + 1);
        deviceBrowseToggle.closeIfToggled();
      }
      return;
    }
    // In track browse mode, select track
    if (trackBrowseToggle.active) {
      const trkIdx = trackBrowseOffset + slotIdx;
      if (trkIdx < trackBrowseTotal && trackBrowseNames[slotIdx]) {
        sendNote(CMD_TRACK_SELECT, trkIdx + 1);
        trackBrowseCurrentIndex = trkIdx;
        updateTrackBrowseLEDs();
        needsRedraw = true;
        trackBrowseToggle.closeIfToggled();
      }
      return;
    }
    // Normal mode: switch slot (Ableton resolves to page)
    sendNote(CMD_PAGE_CHANGE, slotIdx + 1);
    return;
  }

  // Fav step buttons 9-10 (notes 24-25)
  if (note >= FAV_STEP_NOTE_BASE && note < FAV_STEP_NOTE_BASE + 2) {
    favHeld = note - FAV_STEP_NOTE_BASE;
    favUsedAsModifier = false;
    return;
  }

  // Set step buttons 13-14 (notes 28-29)
  if (note >= SET_STEP_NOTE_BASE && note < SET_STEP_NOTE_BASE + 2) {
    setHeld = note - SET_STEP_NOTE_BASE;
    setUsedAsModifier = false;
    return;
  }
}

function handleInternalNoteOff(note) {
  // Pad release in note mode
  if (noteMode && note >= PAD_NOTE_START && note <= PAD_NOTE_END) {
    sendPadNoteOff(note);
    updateSinglePadLED(note);
    return;
  }

  if (note < 8) {
    const si = touchStack.indexOf(note);
    if (si >= 0) {
      touchStack.splice(si, 1);
      const prev = touchStack.length > 0 ? touchStack[touchStack.length - 1] : -1;
      touchedKnob = prev;
      if (prev >= 0) {
        if (marqueeKnob !== prev) {
          marqueeKnob = prev;
          marqueeOffset = 0;
        }
        // Restore overlay to the knob we're falling back to
        if (connected && paramNames[prev]) {
          overlayKnob = prev;
          overlayTimer = OVERLAY_HOLD_TICKS;
          sendNote(CMD_REQUEST_VALUE_STRING, prev + 1);
        }
      } else {
        marqueeKnob = -1;
        marqueeOffset = 0;
        overlayKnob = -1;
        overlayTimer = 0;
      }
      needsRedraw = true;
    }
  }

  // Fav step button release (notes 24-25)
  if (note >= FAV_STEP_NOTE_BASE && note < FAV_STEP_NOTE_BASE + 2) {
    const fi = note - FAV_STEP_NOTE_BASE;
    if (fi === favHeld && !favUsedAsModifier) {
      sendNote(CMD_PAGE_CHANGE, 8 + fi + 1);
    }
    favHeld = -1;
    favUsedAsModifier = false;
    return;
  }

  // Set step button release (notes 28-29)
  if (note >= SET_STEP_NOTE_BASE && note < SET_STEP_NOTE_BASE + 2) {
    const si = note - SET_STEP_NOTE_BASE;
    if (si === setHeld && !setUsedAsModifier) {
      sendNote(CMD_PAGE_CHANGE, 10 + si + 1);
    }
    setHeld = -1;
    setUsedAsModifier = false;
    return;
  }
}

/* ============================================================================
 * Display Drawing (128x64, 1-bit)
 * ============================================================================ */

function drawScreen() {
  clear_screen();

  if (!connected) {
    drawDisconnected();
    return;
  }

  if (deviceBrowseToggle.active) {
    drawDeviceBrowser();
    return;
  }

  if (trackBrowseToggle.active) {
    drawTrackBrowser();
    return;
  }

  drawParams();
  drawFooter();


  if (favFeedbackTimer > 0) {
    drawFavFeedback();
  }

  if (overlayKnob >= 0 && overlayTimer > 0) {
    drawValueOverlay();
  }
}

function drawDeviceBrowser() {
  // Device list: 2 columns x 4 rows
  const colW = 63;
  const startY = 1;
  const rowH = 12;

  for (let i = 0; i < 8; i++) {
    const name = deviceBrowseNames[i];
    if (!name) continue;

    const col = i % 2;
    const row = Math.floor(i / 2);
    const x = col * (colW + 2);
    const y = startY + row * rowH;

    const label = (deviceBrowseOffset + i + 1) + " " + name;
    const isCurrentDevice = (deviceBrowseOffset + i) === deviceIndex;
    if (isCurrentDevice) {
      fill_rect(x, y - 1, colW, rowH - 1, 1);
      set_clip_rect(x, y - 1, colW, rowH - 1);
      print(x + 2, y, label, 0);
      clear_clip_rect();
    } else {
      set_clip_rect(x, y - 1, colW, rowH - 1);
      print(x + 2, y, label, 1);
      clear_clip_rect();
    }
  }

  // Footer with step label hint
  draw_rect(0, 53, SCREEN_WIDTH, 1, 1);
  const hint = "Step 1-8 to select";
  print(1, 56, hint, 1);
}

function updateDeviceBrowseLEDs() {
  if (!deviceBrowseToggle.active) return;
  // Step LEDs: light up for available devices
  for (let i = 0; i < 8; i++) {
    const note = STEP_NOTE_BASE + i;
    const devIdx = deviceBrowseOffset + i;
    if (devIdx < deviceBrowseTotal && deviceBrowseNames[i]) {
      if (devIdx === deviceIndex) {
        setLED(note, White);
      } else {
        setLED(note, DarkGrey);
      }
    } else {
      setLED(note, Black);
    }
  }
  // Arrow LEDs: show if more pages exist
  setButtonLED(MoveLeft, deviceBrowseOffset > 0 ? WhiteLedBright : Black);
  setButtonLED(MoveRight, deviceBrowseOffset + 8 < deviceBrowseTotal ? WhiteLedBright : Black);
}

function drawTrackBrowser() {
  const colW = 63;
  const startY = 1;
  const rowH = 12;

  for (let i = 0; i < 8; i++) {
    const name = trackBrowseNames[i];
    if (!name) continue;

    const col = i % 2;
    const row = Math.floor(i / 2);
    const x = col * (colW + 2);
    const y = startY + row * rowH;

    const label = (trackBrowseOffset + i + 1) + " " + name;
    const isCurrentTrack = (trackBrowseOffset + i) === trackBrowseCurrentIndex;
    if (isCurrentTrack) {
      fill_rect(x, y - 1, colW, rowH - 1, 1);
      set_clip_rect(x, y - 1, colW, rowH - 1);
      print(x + 2, y, label, 0);
      clear_clip_rect();
    } else {
      set_clip_rect(x, y - 1, colW, rowH - 1);
      print(x + 2, y, label, 1);
      clear_clip_rect();
    }
  }

  draw_rect(0, 53, SCREEN_WIDTH, 1, 1);
  const hint = "Step 1-8 to select";
  print(1, 56, hint, 1);
}

function updateTrackBrowseLEDs() {
  if (!trackBrowseToggle.active) return;
  for (let i = 0; i < 8; i++) {
    const note = STEP_NOTE_BASE + i;
    const trkIdx = trackBrowseOffset + i;
    if (trkIdx < trackBrowseTotal && trackBrowseNames[i]) {
      if (trkIdx === trackBrowseCurrentIndex) {
        setLED(note, White);
      } else {
        setLED(note, DarkGrey);
      }
    } else {
      setLED(note, Black);
    }
  }
  setButtonLED(MoveLeft, trackBrowseOffset > 0 ? WhiteLedBright : Black);
  setButtonLED(MoveRight, trackBrowseOffset + 8 < trackBrowseTotal ? WhiteLedBright : Black);
}

function drawDisconnected() {
  const msg = "Waiting for Ableton...";
  const w = text_width(msg);
  print(Math.floor((SCREEN_WIDTH - w) / 2), 24, msg, 1);

  // Animated dots
  const dots = ".".repeat((Math.floor(tickCount / 30) % 3) + 1);
  const dotsW = text_width(dots);
  print(Math.floor((SCREEN_WIDTH - dotsW) / 2), 36, dots, 1);
}

function drawHeader() {
  // Device name (left) and index (right)
  const name = deviceName || "No Device";
  print(1, 1, name, 1);

  if (deviceCount > 0) {
    const idx = `${deviceIndex + 1}/${deviceCount}`;
    const w = text_width(idx);
    print(SCREEN_WIDTH - w - 1, 1, idx, 1);
  }

  // Separator line
  draw_rect(0, 10, SCREEN_WIDTH, 1, 1);
}

function drawParams() {
  if (uiLayout === "B") {
    drawParamsCompact();
    drawPageTabs();
  } else {
    drawParamsClassic();
  }
}

// Layout A: 2 columns x 4 rows, name + value bar side by side
function drawParamsClassic() {
  const colWidth = 63;
  const startY = 1;
  const rowHeight = 10;

  for (let i = 0; i < 8; i++) {
    const col = i < 4 ? 0 : 1;
    const row = i % 4;
    const x = col * (colWidth + 2);
    const y = startY + row * rowHeight;

    const name = paramNames[i] || "";
    const value = paramValues[i];
    const touched = touchedKnob === i;

    if (touched && name) {
      fill_rect(x, y - 1, colWidth, rowHeight, 1);
    }

    if (name) {
      const fg = touched ? 0 : 1;
      let displayName = name.length > 8 ? name.substring(0, 7) + "." : name;
      print(x + 1, y, displayName, fg);

      const barX = x + 44;
      const barW = 18;
      const barH = 5;
      const barY = y + 1;
      const ns = paramSteps[i];
      let fillW;
      if (ns >= 2) {
        const stepSize = 127 / (ns - 1);
        const step = Math.round(value / stepSize);
        fillW = Math.round((step / (ns - 1)) * barW);
      } else {
        fillW = Math.round((value / 127) * barW);
      }

      draw_rect(barX, barY, barW, barH, fg);
      if (touched) {
        fill_rect(barX, barY, barW, barH, 0);
        if (fillW > 0) {
          fill_rect(barX, barY, fillW, barH, 1);
        }
        draw_rect(barX, barY, barW, barH, 0);
      } else {
        if (fillW > 0) {
          fill_rect(barX, barY, fillW, barH, 1);
        }
      }
    }
  }
}

// Layout B: 4 columns x 2 rows, name above, single pixel row for value
function drawParamsCompact() {
  const colW = 31;
  const startY = 0;
  const rowHeight = 14;

  // Show hint when no params are mapped on this page
  const hasAnyParam = paramNames.some((n) => n);
  if (!hasAnyParam) {
    const msg = "Press Menu to learn";
    const w = text_width(msg);
    print(Math.floor((SCREEN_WIDTH - w) / 2), 10, msg, 1);
    return;
  }

  for (let i = 0; i < 8; i++) {
    const col = i % 4;
    const row = Math.floor(i / 4);
    const x = col * (colW + 1);
    const y = startY + row * rowHeight;

    const name = paramNames[i] || "";
    const value = paramValues[i];
    const touched = touchedKnob === i;

    if (!name) continue;

    // Clip all drawing to this column
    set_clip_rect(x, y - 1, colW, 12);

    if (touched) {
      const nameW = text_width(name);
      const maxScroll = nameW - (colW - 4);
      const scrollX =
        maxScroll > 0 ? Math.max(0, Math.min(marqueeOffset, maxScroll)) : 0;
      fill_rect(x, y - 1, colW, 9, 1);
      print(x + 1 - scrollX, y, name, 0);
    } else {
      fill_rect(x, y - 1, colW, 9, 0);
      print(x + 1, y, name, 1);
    }

    // Single pixel row for value
    const barY = y + 9;
    const barW = colW - 1;
    const ns = paramSteps[i];
    let fillW;
    if (ns >= 2) {
      const stepSize = 127 / (ns - 1);
      const step = Math.round(value / stepSize);
      fillW = Math.round((step / (ns - 1)) * barW);
    } else {
      fillW = Math.round((value / 127) * barW);
    }
    if (fillW > 0) {
      fill_rect(x, barY, fillW, 2, 1);
    }

    clear_clip_rect();
  }
}

function drawPageTabs() {
  const tabY = 30;
  const tabVisH = 9;
  const rowStep = 11; // 9px tab + 1px indicator + 1px gap
  const colW = 31;

  for (let i = 0; i < 8; i++) {
    const col = i % 4;
    const row = Math.floor(i / 4);
    const x = col * (colW + 1);
    const y = tabY + row * rowStep;

    const hasControls = i < pageCount;

    if (i === currentPage) {
      fill_rect(x, y, colW, tabVisH, 1);
      if (hasControls) {
        const name = pageNames[i] || `${i + 1}`;
        set_clip_rect(x, y, colW, tabVisH);
        print(x + 1, y + 1, name, 0);
        clear_clip_rect();
      }
    } else if (hasControls) {
      const name = pageNames[i] || `${i + 1}`;
      set_clip_rect(x, y, colW, tabVisH);
      print(x + 1, y + 1, name, 1);
      clear_clip_rect();
    }

    // Subpage indicator: partial line under tab
    const subCount = slotSubpageCounts[i];
    if (subCount > 1 && hasControls) {
      const activeSub = slotActiveSubpage[i];
      const indicatorY = y + tabVisH;
      const segW = Math.floor(colW / subCount);
      const segX = x + activeSub * segW;
      fill_rect(segX, indicatorY, segW, 1, 1);
    }
  }
}

function drawFooter() {
  const tabW = 19;
  const tabY = 54;
  const tabH = 9;
  const tabGap = 1;
  let tabX = 0;

  // Fav tab
  const favLabel = `* ${favActiveSub + 1}`;
  if (currentPage === 8) {
    fill_rect(tabX, tabY, tabW, tabH, 1);
    print(tabX + 1, tabY + 1, favLabel, 0);
  } else if (favState >= 1) {
    print(tabX + 1, tabY + 1, favLabel, 1);
  } else {
    print(tabX + 1, tabY + 1, favLabel, 1);
  }
  {
    const indicatorY = tabY + tabH;
    const segW = Math.floor(tabW / 2);
    const segX = favActiveSub * segW;
    fill_rect(segX, indicatorY, segW, 1, 1);
  }
  tabX += tabW + tabGap;

  // Set tab
  const setLabel = `S ${setActiveSub + 1}`;
  if (currentPage === 9) {
    fill_rect(tabX, tabY, tabW, tabH, 1);
    print(tabX + 1, tabY + 1, setLabel, 0);
  } else if (setState >= 1) {
    print(tabX + 1, tabY + 1, setLabel, 1);
  } else {
    print(tabX + 1, tabY + 1, setLabel, 1);
  }
  {
    const indicatorY = tabY + tabH;
    const segW = Math.floor(tabW / 2);
    const segX = tabX + setActiveSub * segW;
    fill_rect(segX, indicatorY, segW, 1, 1);
  }
  tabX += tabW + tabGap;

  // Device name section: vertical line on left, horizontal line above
  const nameLeft = tabX + 4;
  draw_rect(nameLeft, 53, 1, SCREEN_HEIGHT - 53, 1);            // vertical left edge
  draw_rect(nameLeft, 53, SCREEN_WIDTH - nameLeft, 1, 1);       // horizontal top edge
  const name = deviceName || "No Device";
  set_clip_rect(nameLeft + 2, tabY, SCREEN_WIDTH - nameLeft - 3, tabH);
  print(nameLeft + 3, tabY + 1, name, 1);
  clear_clip_rect();

  // Learn mode indicator (overlays right side)
  if (learnMode) {
    const learnText = "LEARN";
    if (Math.floor(tickCount / 15) % 2 === 0) {
      const w = text_width(learnText);
      fill_rect(SCREEN_WIDTH - w - 4, tabY, w + 3, tabH, 1);
      print(SCREEN_WIDTH - w - 2, tabY + 1, learnText, 0);
    } else {
      const w = text_width(learnText);
      print(SCREEN_WIDTH - w - 2, tabY + 1, learnText, 1);
    }
  }
}

function drawFavFeedback() {
  const w = text_width(favFeedbackText) + 8;
  const h = 12;
  const x = Math.floor((SCREEN_WIDTH - w) / 2);
  const y = 24;
  fill_rect(x, y, w, h, 0);
  draw_rect(x, y, w, h, 1);
  print(x + 4, y + 2, favFeedbackText, 1);
}

function drawValueOverlay() {
  const name = paramNames[overlayKnob] || "";
  const valStr = overlayValueStr || "";
  if (!name && !valStr) return;

  // Fixed size, centered, over page tabs area
  const boxW = 120;
  const boxH = 21;
  const boxX = Math.floor((SCREEN_WIDTH - boxW) / 2);
  const boxY = 29;

  // Background with border
  fill_rect(boxX, boxY, boxW, boxH, 0);
  draw_rect(boxX, boxY, boxW, boxH, 1);

  // Param name (centered)
  const nameW = text_width(name);
  print(boxX + Math.floor((boxW - nameW) / 2), boxY + 3, name, 1);

  // Value string (centered, bold via inverted bar)
  const valBarY = boxY + 12;
  const valW = text_width(valStr);
  fill_rect(boxX + 1, valBarY - 1, boxW - 2, 9, 1);
  print(boxX + Math.floor((boxW - valW) / 2), valBarY, valStr, 0);
}

/* ============================================================================
 * LED Control
 *
 * Overtake modules get exclusive LED control — use cached setLED/setButtonLED
 * from input_filter.mjs. Progressive init prevents MIDI buffer overflow.
 * ============================================================================ */

// Pad LED colors for note mode — root=bright, in-scale=dim, out=off
const PAD_COLOR_ROOT = BrightGreen;
const PAD_COLOR_SCALE = DarkGrey;
const PAD_COLOR_OFF = Black;

function getPadNoteColor(padNote) {
  // Map pad grid position to scale note to determine color
  // Simplified: we use the layout info from Ableton to color pads
  const padIdx = padNote - PAD_NOTE_START;
  const col = padIdx % 8;
  const row = Math.floor(padIdx / 8);
  // Approximate note calculation matching MelodicPattern logic
  const interval = noteLayoutInterval || noteLayoutScaleNotes.length;
  const scaleLen = noteLayoutScaleNotes.length;
  const index = col + interval * row;
  const octave = Math.floor(index / scaleLen);
  const scaleIdx = ((index % scaleLen) + scaleLen) % scaleLen;
  const semitone = noteLayoutScaleNotes[scaleIdx] % 12;
  const rootSemitone = noteLayoutRoot % 12;
  if (semitone === rootSemitone) return PAD_COLOR_ROOT;
  // Check if this note is in the base scale (relative to root)
  const relNote = (semitone - rootSemitone + 12) % 12;
  const baseScale = noteLayoutScaleNotes.map(n => (n - noteLayoutRoot + 12) % 12);
  if (baseScale.includes(relNote)) return PAD_COLOR_SCALE;
  return PAD_COLOR_OFF;
}

function updatePadLEDs() {
  for (let i = PAD_NOTE_START; i <= PAD_NOTE_END; i++) {
    setLED(i, getPadNoteColor(i));
  }
}

function updateSinglePadLED(note) {
  setLED(note, getPadNoteColor(note));
}

function clearPadLEDs() {
  for (let i = PAD_NOTE_START; i <= PAD_NOTE_END; i++) {
    setLED(i, Black);
  }
}

function buildLedList() {
  const leds = [];

  // Step LEDs (page indicators)
  for (let i = 0; i < 8; i++) {
    const note = STEP_NOTE_BASE + i;
    if (i === currentPage) {
      leds.push({ note, color: White });
    } else if (i < pageCount) {
      leds.push({ note, color: DarkGrey });
    } else {
      leds.push({ note, color: Black });
    }
  }

  // Fav step LEDs (step 9=*1, step 10=*2)
  for (let fi = 0; fi < 2; fi++) {
    const note = FAV_STEP_NOTE_BASE + fi;
    if (currentPage === 8 && favActiveSub === fi) {
      leds.push({ note, color: White });
    } else if (favState >= 1) {
      leds.push({ note, color: DarkGrey });
    } else {
      leds.push({ note, color: Black });
    }
  }

  // Set step LEDs (step 13=S1, step 14=S2)
  for (let si = 0; si < 2; si++) {
    const note = SET_STEP_NOTE_BASE + si;
    if (currentPage === 9 && setActiveSub === si) {
      leds.push({ note, color: White });
    } else if (setState >= 1) {
      leds.push({ note, color: DarkGrey });
    } else {
      leds.push({ note, color: Black });
    }
  }

  return leds;
}

function setupLedBatch() {
  const leds = buildLedList();
  const start = ledInitIndex;
  const end = Math.min(start + LEDS_PER_FRAME, leds.length);

  for (let i = start; i < end; i++) {
    setLED(leds[i].note, leds[i].color);
  }

  ledInitIndex = end;
  if (ledInitIndex >= leds.length) {
    ledInitPending = false;

    // Button LEDs (CC-based, set after pad LEDs are done)
    for (let i = 0; i < 8; i++) {
      if (paramNames[i]) {
        setButtonLED(KNOB_CCS[i], valueToKnobColor(paramValues[i], learnMode));
      } else {
        setButtonLED(KNOB_CCS[i], Black);
      }
    }
    const hasNav = deviceCount > 1;
    setButtonLED(MoveLeft, hasNav ? WhiteLedBright : Black);
    setButtonLED(MoveRight, hasNav ? WhiteLedBright : Black);
    setButtonLED(MoveMenu, WhiteLedBright);
    setButtonLED(MoveBack, WhiteLedBright);
  }
}

function updateNavLEDs() {
  const hasNav = deviceCount > 1;
  setButtonLED(MoveLeft, hasNav ? WhiteLedBright : Black);
  setButtonLED(MoveRight, hasNav ? WhiteLedBright : Black);
}

// Color sweep from dim to bright for knob value display
const knobColorSweep = [Black, 117, 124, 119, 123, 118, 121, 122, White];
const learnColorSweep = [Black, 5, 5, 5, 5, 5, 5, 5, Cyan]; // cyan sweep

function valueToKnobColor(value, isLearn) {
  const sweep = isLearn ? learnColorSweep : knobColorSweep;
  const level = Math.min(value, 127) / 127;
  const index = Math.round(level * (sweep.length - 1));
  return sweep[index];
}

function updateStepLEDs() {
  for (let i = 0; i < 8; i++) {
    const note = STEP_NOTE_BASE + i;
    if (i === currentPage) setLED(note, White);
    else if (i < pageCount) setLED(note, DarkGrey);
    else setLED(note, Black);
  }
  for (let fi = 0; fi < 2; fi++) {
    const note = FAV_STEP_NOTE_BASE + fi;
    if (currentPage === 8 && favActiveSub === fi) setLED(note, White);
    else if (favState >= 1) setLED(note, DarkGrey);
    else setLED(note, Black);
  }
  for (let si = 0; si < 2; si++) {
    const note = SET_STEP_NOTE_BASE + si;
    if (currentPage === 9 && setActiveSub === si) setLED(note, White);
    else if (setState >= 1) setLED(note, DarkGrey);
    else setLED(note, Black);
  }
}

function updateKnobLEDs() {
  for (let i = 0; i < 8; i++) {
    if (paramNames[i]) {
      setButtonLED(KNOB_CCS[i], valueToKnobColor(paramValues[i], learnMode));
    } else {
      setButtonLED(KNOB_CCS[i], Black);
    }
  }
}

/* ============================================================================
 * Lifecycle
 * ============================================================================ */

function init() {
  ledInitPending = true;
  ledInitIndex = 0;
  drawScreen();

  console.log("[DC] Module initialized");
  sendNote(CMD_HELLO, 1);
}

function tick() {
  tickCount++;

  if (ledInitPending) {
    setupLedBatch();
    return;
  }

  // HoldToggle timers
  deviceBrowseToggle.tick();
  trackBrowseToggle.tick();

  // Heartbeat watchdog
  heartbeatTimer++;
  if (heartbeatTimer > HEARTBEAT_TIMEOUT_TICKS && connected) {
    connected = false;
    reconnectTimer = 0;
    needsRedraw = true;
  }

  // Re-send HELLO periodically when disconnected so a new remote script instance can pick us up
  if (!connected) {
    reconnectTimer++;
    if (reconnectTimer >= RECONNECT_INTERVAL_TICKS) {
      reconnectTimer = 0;
      sendNote(CMD_HELLO, 1);
    }
  }

  // Fav feedback countdown
  if (favFeedbackTimer > 0) {
    favFeedbackTimer--;
    if (favFeedbackTimer === 0) {
      favFeedbackText = "";
      needsRedraw = true;
    }
  }

  // Overlay auto-hide countdown (only when knob not touched)
  if (overlayTimer > 0 && touchedKnob !== overlayKnob) {
    overlayTimer--;
    if (overlayTimer === 0) {
      overlayKnob = -1;
      needsRedraw = true;
    }
  }

  // Marquee loop with pause for touched knob
  if (touchedKnob >= 0 && uiLayout === "B" && tickCount % 12 === 0) {
    const name = paramNames[touchedKnob] || "";
    const nameW = text_width(name);
    const colW = 31;
    const maxScroll = nameW - (colW - 4);
    if (maxScroll > 0) {
      marqueeOffset++;
      const pauseTicks = 8;
      if (marqueeOffset > maxScroll + pauseTicks) {
        marqueeOffset = -pauseTicks;
      }
      needsRedraw = true;
    }
  }

  // Redraw display
  if (needsRedraw || tickCount % 24 === 0) {
    drawScreen();
    host_flush_display();
    needsRedraw = false;
  }
}

/* ============================================================================
 * Module exports
 * ============================================================================ */

globalThis.init = init;
globalThis.tick = tick;
globalThis.onMidiMessageInternal = handleMidiInternal;
globalThis.onMidiMessageExternal = processMidiExternal;
