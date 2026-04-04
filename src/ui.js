/*
 * Schwung Device Control - Overtake Module
 *
 * Two-way Ableton Live device parameter control with learn mode.
 * Communicates with SchwungDeviceControl Remote Script via MIDI cable 2.
 */

import {
  decodeDelta,
  decodeAcceleratedDelta,
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

// Knob CCs (same as Move hardware knobs)
const KNOB_CCS = [71, 72, 73, 74, 75, 76, 77, 78];

// Navigation CCs sent to Ableton
const CC_DEVICE_LEFT = 80;
const CC_DEVICE_RIGHT = 81;
const CC_LEARN_TOGGLE = 84;

// SysEx protocol
const SYSEX_HEADER = [0xf0, 0x00, 0x7d, 0x01];

// SysEx commands: Live -> Move
const CMD_DEVICE_INFO = 0x01;
const CMD_PARAM_INFO = 0x02;
const CMD_DEVICE_COUNT = 0x03;
const CMD_DEVICE_INDEX = 0x04;
// 0x05 was CMD_BANK_INFO (removed)
const CMD_LEARN_ACK = 0x06;
const CMD_HEARTBEAT = 0x07;
const CMD_ALL_VALUES = 0x08;
const CMD_PAGE_INFO = 0x09;
const CMD_PAGE_NAME = 0x0a;

// SysEx commands: Move -> Live
const CMD_HELLO = 0x10;
const CMD_LEARN_KNOB = 0x13;
const CMD_KNOB_VALUE = 0x14; // Move -> Live: knob_idx, value (0-127)
const CMD_NAV_DEVICE = 0x17; // Move -> Live: direction (-1 or +1 as 0x00/0x01)
const CMD_REQUEST_STATE = 0x15;
const CMD_UNMAP_KNOB = 0x16;
const CMD_PAGE_CHANGE = 0x18; // Move -> Live: pageIndex

// Timing
const HEARTBEAT_TIMEOUT_TICKS = 720; // ~3 seconds at ~240fps (tick rate is faster than expected)
const LED_MSGS_PER_TICK = 8;

/* ============================================================================
 * State
 * ============================================================================ */

let connected = false;
let heartbeatTimer = 0;
let learnMode = false;
let shiftHeld = false;
let needsRedraw = true;
let tickCount = 0;
let touchedKnob = -1; // -1 = none, 0-7 = knob index
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

// Page state
let currentPage = 0;
let pageCount = 1;
let pageNames = ["1", "2", "3", "4", "5", "6", "7", "8"];

// Step button notes (step 1-8 = notes 16-23)
const STEP_NOTE_BASE = 16;

// LED queue for progressive updates (no cache — raw move_midi_internal_send)
let ledQueue = [];
let ledQueueIdx = 0;
let ledsInitialized = false;

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

function sendCommand(cmd, dataBytes) {
  const msg = [...SYSEX_HEADER, cmd, ...dataBytes, 0xf7];
  sendSysEx(msg);
}

function sendCC(cc, value) {
  const packet = [
    (CABLE << 4) | 0x0b, // CIN for CC
    0xb0 | MIDI_CHANNEL,
    cc,
    value,
  ];
  move_midi_external_send(packet);
}

/* ============================================================================
 * Incoming SysEx Parser
 * ============================================================================ */

// SysEx accumulator (messages may arrive split across multiple calls)
let sysexBuffer = null;

function processMidiExternal(data) {
  if (!data || data.length < 1) return;
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

  // CC on our channel
  if (status === 0xb0 && (data[0] & 0x0f) === MIDI_CHANNEL) {
    const cc = data[1];
    const value = data[2];
    handleCCFromAbleton(cc, value);
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
    case CMD_DEVICE_INFO:
      deviceName = decodeString(payload);
      needsRedraw = true;
      break;

    case CMD_PARAM_INFO:
      if (payload.length >= 2) {
        const idx = payload[0];
        if (idx >= 0 && idx < 8) {
          paramNames[idx] = decodeString(payload.slice(1));
          needsRedraw = true;
        }
      }
      break;

    case CMD_DEVICE_COUNT:
      if (payload.length >= 1) {
        deviceCount = payload[0];
        updateNavLEDs();
        needsRedraw = true;
      }
      break;

    case CMD_DEVICE_INDEX:
      if (payload.length >= 1) {
        deviceIndex = payload[0];
        updateNavLEDs();
        needsRedraw = true;
      }
      break;

    case CMD_PAGE_INFO:
      if (payload.length >= 2) {
        currentPage = payload[0];
        pageCount = payload[1];
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

    case CMD_HEARTBEAT:
      connected = true;
      heartbeatTimer = 0;
      needsRedraw = true;
      break;

    case CMD_KNOB_VALUE:
      // Single knob value update from Ableton
      if (payload.length >= 2) {
        const ki = payload[0];
        if (ki >= 0 && ki < 8) {
          paramValues[ki] = payload[1];
          buttonLed(KNOB_CCS[ki], valueToKnobColor(paramValues[ki], learnMode));
          needsRedraw = true;
        }
      }
      break;

    case CMD_ALL_VALUES:
      for (let i = 0; i < Math.min(8, payload.length); i++) {
        paramValues[i] = payload[i];
      }
      updateKnobLEDs();
      needsRedraw = true;
      break;
  }
}

function handleCCFromAbleton(cc, value) {
  // Parameter value feedback from Ableton
  const idx = KNOB_CCS.indexOf(cc);
  if (idx >= 0) {
    paramValues[idx] = value;
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

  // Back button
  if (cc === MoveBack && value > 63) {
    if (learnMode) {
      learnMode = false;
      needsRedraw = true;
      return;
    }
    // Clear step LEDs before exit (queue won't drain after exit)
    for (let i = 0; i < 8; i++) {
      padLed(STEP_NOTE_BASE + i, Black);
    }
    host_exit_module();
    return;
  }

  // Menu button - toggle learn mode
  if (cc === MoveMenu && value > 63) {
    learnMode = !learnMode;
    sendCC(CC_LEARN_TOGGLE, learnMode ? 127 : 0);
    needsRedraw = true;
    return;
  }

  // Arrow keys - device navigation
  if (cc === MoveLeft && value > 63) {
    sendCommand(CMD_NAV_DEVICE, [0x00]); // left = -1
    return;
  }
  if (cc === MoveRight && value > 63) {
    sendCommand(CMD_NAV_DEVICE, [0x01]); // right = +1
    return;
  }
  // Up/Down arrows currently unused (banks removed, pages use step buttons)

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
    sendCommand(CMD_LEARN_KNOB, [idx]);
    return;
  }

  const rawDelta = decodeAcceleratedDelta(rawValue, idx);
  const delta =
    Math.round(rawDelta * 0.2) || (rawDelta > 0 ? 1 : rawDelta < 0 ? -1 : 0);
  paramValues[idx] = Math.max(0, Math.min(127, paramValues[idx] + delta));

  // Send absolute value to Ableton via SysEx (CC doesn't pass through Standalone Port)
  sendCommand(CMD_KNOB_VALUE, [idx, paramValues[idx]]);

  // Update this knob's LED immediately
  const colour = valueToKnobColor(paramValues[idx], learnMode);
  buttonLed(KNOB_CCS[idx], colour);

  needsRedraw = true;
}

function handleInternalNoteOn(note, velocity) {
  // Capacitive knob touch (notes 0-7)
  if (note < 8) {
    touchedKnob = note;
    if (marqueeKnob !== note) {
      marqueeKnob = note;
      marqueeOffset = 0;
    }
    needsRedraw = true;
    return;
  }

  // Step buttons 1-8 (notes 16-23) — switch page
  if (note >= STEP_NOTE_BASE && note < STEP_NOTE_BASE + 8) {
    const pageIdx = note - STEP_NOTE_BASE;
    if (pageIdx !== currentPage) {
      currentPage = pageIdx;
      sendCommand(CMD_PAGE_CHANGE, [pageIdx]);
      updateStepLEDs();
      needsRedraw = true;
    }
    return;
  }
}

function handleInternalNoteOff(note) {
  if (note < 8 && touchedKnob === note) {
    touchedKnob = -1;
    marqueeKnob = -1;
    marqueeOffset = 0;
    needsRedraw = true;
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

  drawParams();
  drawFooter();
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
      const fillW = Math.round((value / 127) * barW);

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
    set_clip_rect(x, y - 1, colW, 10);

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
    const barY = y + 8;
    const barW = colW - 1;
    const fillW = Math.round((value / 127) * barW);
    if (fillW > 0) {
      fill_rect(x, barY, fillW, 1, 1);
    }

    clear_clip_rect();
  }
}

function drawPageTabs() {
  const tabY = 30;
  const tabH = 10;
  const colW = 31;

  for (let i = 0; i < 8; i++) {
    const col = i % 4;
    const row = Math.floor(i / 4);
    const x = col * (colW + 1);
    const y = tabY + row * tabH;

    const hasControls = i < pageCount;

    if (i === currentPage) {
      // Active page: always render inverted block
      fill_rect(x, y, colW, tabH - 1, 1);
      if (hasControls) {
        const name = pageNames[i] || `${i + 1}`;
        const maxChars = Math.max(1, Math.floor((colW - 3) / 6));
        const display = name.length > maxChars ? name.substring(0, maxChars) : name;
        print(x + 1, y + 1, display, 0);
      }
    } else if (hasControls) {
      const name = pageNames[i] || `${i + 1}`;
      const maxChars = Math.max(1, Math.floor((colW - 3) / 6));
      const display = name.length > maxChars ? name.substring(0, maxChars) : name;
      print(x + 1, y + 1, display, 1);
    }
  }
}

function drawFooter() {
  // Separator line
  draw_rect(0, 53, SCREEN_WIDTH, 1, 1);

  // Device name (left) with index
  const name = deviceName || "No Device";
  const idx = deviceCount > 0 ? ` ${deviceIndex + 1}/${deviceCount}` : "";
  print(1, 56, name + idx, 1);

  // Learn mode indicator (right)
  if (learnMode) {
    const learnText = "LEARN";
    // Blink effect
    if (Math.floor(tickCount / 15) % 2 === 0) {
      const w = text_width(learnText);
      fill_rect(SCREEN_WIDTH - w - 4, 55, w + 3, 9, 1);
      print(SCREEN_WIDTH - w - 2, 56, learnText, 0);
    } else {
      const w = text_width(learnText);
      print(SCREEN_WIDTH - w - 2, 56, learnText, 1);
    }
  }
}

/* ============================================================================
 * LED Control
 *
 * Tool modules must use raw move_midi_internal_send — NOT input_filter.mjs's
 * cached setLED/setButtonLED. Move's firmware keeps overwriting LEDs, so:
 *   1. No cache (every send hits hardware)
 *   2. No LEDs in init() (MIDI HW not ready yet)
 *   3. Periodic re-send to fight firmware overwrites
 * ============================================================================ */

function padLed(note, color) {
  move_midi_internal_send([0x09, 0x90, note, color]);
}

function buttonLed(cc, color) {
  move_midi_internal_send([0x0b, 0xb0, cc, color]);
}

function scheduleLEDs() {
  ledQueue = [];

  // Step LEDs (page indicators)
  for (let i = 0; i < 8; i++) {
    const note = STEP_NOTE_BASE + i;
    if (i === currentPage) {
      ledQueue.push(["pad", note, White]);
    } else if (i < pageCount) {
      ledQueue.push(["pad", note, DarkGrey]);
    } else {
      ledQueue.push(["pad", note, Black]);
    }
  }

  // Clear pad grid (notes 68-99) — Move firmware keeps re-lighting these
  for (let note = 68; note < 100; note++) {
    ledQueue.push(["pad", note, Black]);
  }

  // Knob LEDs
  for (let i = 0; i < 8; i++) {
    if (paramNames[i]) {
      ledQueue.push(["button", KNOB_CCS[i], valueToKnobColor(paramValues[i], learnMode)]);
    } else {
      ledQueue.push(["button", KNOB_CCS[i], Black]);
    }
  }

  // Nav and function button LEDs
  const hasNav = deviceCount > 1;
  ledQueue.push(["button", MoveLeft, hasNav ? WhiteLedBright : Black]);
  ledQueue.push(["button", MoveRight, hasNav ? WhiteLedBright : Black]);
  ledQueue.push(["button", MoveMenu, WhiteLedBright]);
  ledQueue.push(["button", MoveBack, WhiteLedBright]);

  ledQueueIdx = 0;
}

function flushLEDQueue() {
  if (ledQueueIdx >= ledQueue.length) return;
  const end = Math.min(ledQueueIdx + LED_MSGS_PER_TICK, ledQueue.length);
  for (let i = ledQueueIdx; i < end; i++) {
    const msg = ledQueue[i];
    if (msg[0] === "pad") {
      padLed(msg[1], msg[2]);
    } else {
      buttonLed(msg[1], msg[2]);
    }
  }
  ledQueueIdx = end;
}

function updateNavLEDs() {
  const hasNav = deviceCount > 1;
  buttonLed(MoveLeft, hasNav ? WhiteLedBright : Black);
  buttonLed(MoveRight, hasNav ? WhiteLedBright : Black);
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
  scheduleLEDs();
}

function updateKnobLEDs() {
  scheduleLEDs();
}

/* ============================================================================
 * Lifecycle
 * ============================================================================ */

function init() {
  // Don't set LEDs here — MIDI HW not ready yet, defer to first tick
  drawScreen();

  // Say hello to Ableton
  console.log("[DC] Module initialized");
  sendCommand(CMD_HELLO, []);
}

function tick() {
  tickCount++;

  // Initialize LEDs on first tick (MIDI HW not ready in init)
  if (!ledsInitialized) {
    ledsInitialized = true;
    scheduleLEDs();
  }

  // Heartbeat watchdog
  heartbeatTimer++;
  if (heartbeatTimer > HEARTBEAT_TIMEOUT_TICKS && connected) {
    connected = false;
    needsRedraw = true;
  }

  // Marquee loop with pause for touched knob
  if (touchedKnob >= 0 && uiLayout === "B" && tickCount % 12 === 0) {
    const name = paramNames[touchedKnob] || "";
    const nameW = text_width(name);
    const colW = 31;
    const maxScroll = nameW - (colW - 4);
    if (maxScroll > 0) {
      marqueeOffset++;
      // Pause at end (negative values = pausing at start)
      const pauseTicks = 8;
      if (marqueeOffset > maxScroll + pauseTicks) {
        marqueeOffset = -pauseTicks; // pause at start before scrolling again
      }
      needsRedraw = true;
    }
  }

  // Flush LED queue (8 per tick)
  flushLEDQueue();

  // Periodic full LED refresh — fight Move firmware overwriting our LEDs
  if (tickCount % 120 === 0) {
    scheduleLEDs();
  }

  // Redraw display
  if (needsRedraw || tickCount % 24 === 0) {
    drawScreen();
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
