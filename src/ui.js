/*
 * Schwung Device Control - Overtake Module
 *
 * Two-way Ableton Live device parameter control with learn mode.
 * Communicates with SchwungDeviceControl Remote Script via MIDI cable 2.
 */

import { setButtonLED, setLED, clearAllLEDs } from '/data/UserData/move-anything/shared/input_filter.mjs';
import { decodeDelta, decodeAcceleratedDelta } from '/data/UserData/move-anything/shared/input_filter.mjs';
import {
    MoveBack, MoveMenu, MoveShift, MoveUp, MoveDown, MoveLeft, MoveRight,
    MoveKnob1, MoveKnob2, MoveKnob3, MoveKnob4,
    MoveKnob5, MoveKnob6, MoveKnob7, MoveKnob8,
    MoveMainKnob, MoveMainButton,
    White, Black, BrightGreen, BrightRed, Cyan, DarkGrey, WhiteLedBright
} from '/data/UserData/move-anything/shared/constants.mjs';
import * as os from 'os';

/* ============================================================================
 * Constants
 * ============================================================================ */

const SCREEN_WIDTH = 128;
const SCREEN_HEIGHT = 64;
const CABLE = 2;
const MIDI_CHANNEL = 0x0F; // channel 16

// Knob CCs (same as Move hardware knobs)
const KNOB_CCS = [71, 72, 73, 74, 75, 76, 77, 78];

// Navigation CCs sent to Ableton
const CC_DEVICE_LEFT = 80;
const CC_DEVICE_RIGHT = 81;
const CC_BANK_UP = 82;
const CC_BANK_DOWN = 83;
const CC_LEARN_TOGGLE = 84;

// SysEx protocol
const SYSEX_HEADER = [0xF0, 0x00, 0x7D, 0x01];

// SysEx commands: Live -> Move
const CMD_DEVICE_INFO = 0x01;
const CMD_PARAM_INFO = 0x02;
const CMD_DEVICE_COUNT = 0x03;
const CMD_DEVICE_INDEX = 0x04;
const CMD_BANK_INFO = 0x05;
const CMD_LEARN_ACK = 0x06;
const CMD_HEARTBEAT = 0x07;
const CMD_ALL_VALUES = 0x08;

// SysEx commands: Move -> Live
const CMD_HELLO = 0x10;
const CMD_LEARN_KNOB = 0x13;
const CMD_KNOB_VALUE = 0x14;  // Move -> Live: knob_idx, value (0-127)
const CMD_REQUEST_STATE = 0x15;
const CMD_UNMAP_KNOB = 0x16;

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

// Device state (from Ableton)
let deviceName = '';
let deviceIndex = 0;
let deviceCount = 0;
let bankIndex = 0;
let totalBanks = 1;
let paramNames = new Array(8).fill('');
let paramValues = new Array(8).fill(0);

// LED queue for progressive updates
const ledQueue = [];
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
                data[i], data[i + 1], data[i + 2]
            ]);
            i += 3;
        } else if (remaining === 3) {
            // End with 3 bytes: CIN 0x7
            move_midi_external_send([
                (CABLE << 4) | 0x7,
                data[i], data[i + 1], data[i + 2]
            ]);
            i += 3;
        } else if (remaining === 2) {
            // End with 2 bytes: CIN 0x6
            move_midi_external_send([
                (CABLE << 4) | 0x6,
                data[i], data[i + 1], 0
            ]);
            i += 2;
        } else {
            // End with 1 byte: CIN 0x5
            move_midi_external_send([
                (CABLE << 4) | 0x5,
                data[i], 0, 0
            ]);
            i += 1;
        }
    }
}

function sendCommand(cmd, dataBytes) {
    const msg = [...SYSEX_HEADER, cmd, ...dataBytes, 0xF7];
    console.log(`[DC] sendCommand cmd=0x${cmd.toString(16)} data=[${dataBytes}] full=[${msg.map(b => '0x'+b.toString(16)).join(',')}]`);
    sendSysEx(msg);
}

function sendCC(cc, value) {
    const packet = [
        (CABLE << 4) | 0x0B,  // CIN for CC
        0xB0 | MIDI_CHANNEL,
        cc,
        value
    ];
    console.log(`[DC] sendCC cc=${cc} val=${value} packet=[${packet.map(b => '0x'+b.toString(16)).join(',')}]`);
    move_midi_external_send(packet);
}

/* ============================================================================
 * Incoming SysEx Parser
 * ============================================================================ */

// SysEx accumulator (messages may arrive split across multiple calls)
let sysexBuffer = null;

function processMidiExternal(data) {
    if (!data || data.length < 1) return;
    console.log(`[DC] MIDI EXT received: [${Array.from(data).map(b => '0x'+b.toString(16)).join(',')}]`);

    const status = data[0] & 0xF0;

    // SysEx start
    if (data[0] === 0xF0) {
        sysexBuffer = Array.from(data);
        // Check if complete (ends with F7)
        if (data[data.length - 1] === 0xF7) {
            handleSysEx(sysexBuffer);
            sysexBuffer = null;
        }
        return;
    }

    // SysEx continuation
    if (sysexBuffer !== null) {
        for (let i = 0; i < data.length; i++) {
            sysexBuffer.push(data[i]);
            if (data[i] === 0xF7) {
                handleSysEx(sysexBuffer);
                sysexBuffer = null;
                return;
            }
        }
        return;
    }

    // CC on our channel
    if (status === 0xB0 && (data[0] & 0x0F) === MIDI_CHANNEL) {
        const cc = data[1];
        const value = data[2];
        handleCCFromAbleton(cc, value);
    }
}

function handleSysEx(msg) {
    // Validate header: F0 00 7D 01 <cmd> ... F7
    if (msg.length < 6) {
        console.log(`[DC] SysEx too short: ${msg.length}`);
        return;
    }
    if (msg[0] !== 0xF0 || msg[1] !== 0x00 || msg[2] !== 0x7D || msg[3] !== 0x01) {
        console.log(`[DC] SysEx header mismatch: [${msg.slice(0,4).map(b=>'0x'+b.toString(16)).join(',')}]`);
        return;
    }

    const cmd = msg[4];
    const payload = msg.slice(5, -1); // strip F7
    console.log(`[DC] SysEx parsed: cmd=0x${cmd.toString(16)} payload=[${payload}]`);

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
                needsRedraw = true;
            }
            break;

        case CMD_DEVICE_INDEX:
            if (payload.length >= 1) {
                deviceIndex = payload[0];
                needsRedraw = true;
            }
            break;

        case CMD_BANK_INFO:
            if (payload.length >= 2) {
                bankIndex = payload[0];
                totalBanks = payload[1];
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
            console.log(`[DC] HEARTBEAT: timer reset to ${heartbeatTimer}, connected=${connected}`);
            needsRedraw = true;
            break;

        case CMD_ALL_VALUES:
            for (let i = 0; i < Math.min(8, payload.length); i++) {
                paramValues[i] = payload[i];
            }
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
    let s = '';
    for (let i = 0; i < bytes.length; i++) {
        if (bytes[i] === 0) break;
        s += String.fromCharCode(bytes[i] & 0x7F);
    }
    return s;
}

/* ============================================================================
 * Hardware Input (from Move pads/knobs/buttons)
 * ============================================================================ */

function handleMidiInternal(data) {
    if (!data || data.length < 3) return;

    const status = data[0] & 0xF0;
    const d1 = data[1];
    const d2 = data[2];

    if (status === 0xB0) {
        handleInternalCC(d1, d2);
    } else if (status === 0x90 && d2 > 0) {
        handleInternalNoteOn(d1, d2);
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
        clearAllLEDs();
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

    // Arrow keys - device/bank navigation
    if (cc === MoveLeft && value > 63) {
        sendCC(CC_DEVICE_LEFT, 127);
        return;
    }
    if (cc === MoveRight && value > 63) {
        sendCC(CC_DEVICE_RIGHT, 127);
        return;
    }
    if (cc === MoveUp && value > 63) {
        sendCC(CC_BANK_UP, 127);
        return;
    }
    if (cc === MoveDown && value > 63) {
        sendCC(CC_BANK_DOWN, 127);
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
    console.log(`[DC] knobTurn idx=${idx} raw=${rawValue} connected=${connected} learn=${learnMode}`);
    if (!connected) return;

    if (learnMode) {
        // In learn mode, twist a knob to bind it
        console.log(`[DC] LEARN: sending LEARN_KNOB for knob ${idx}`);
        sendCommand(CMD_LEARN_KNOB, [idx]);
        return;
    }

    // Relative to absolute conversion
    // Move sends relative: 1-63 = CW, 65-127 = CCW
    const delta = decodeAcceleratedDelta(rawValue, idx);
    paramValues[idx] = Math.max(0, Math.min(127, paramValues[idx] + delta));

    // Send absolute value to Ableton via SysEx (CC doesn't pass through Standalone Port)
    sendCommand(CMD_KNOB_VALUE, [idx, paramValues[idx]]);
    needsRedraw = true;
}

function handleInternalNoteOn(note, velocity) {
    // Capacitive knob touch (notes 0-8) - ignore
    if (note <= 8) return;

    // Step buttons (notes 16-31) - could use for device quick-select
    // For now, unused
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

    drawHeader();
    drawParams();
    drawFooter();
}

function drawDisconnected() {
    const msg = 'Waiting for Ableton...';
    const w = text_width(msg);
    print(Math.floor((SCREEN_WIDTH - w) / 2), 24, msg, 1);

    // Animated dots
    const dots = '.'.repeat((Math.floor(tickCount / 30) % 3) + 1);
    const dotsW = text_width(dots);
    print(Math.floor((SCREEN_WIDTH - dotsW) / 2), 36, dots, 1);
}

function drawHeader() {
    // Device name (left) and index (right)
    const name = deviceName || 'No Device';
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
    // Two-column layout: knobs 1-4 left, 5-8 right
    const colWidth = 63;
    const startY = 13;
    const rowHeight = 10;

    for (let i = 0; i < 8; i++) {
        const col = i < 4 ? 0 : 1;
        const row = i % 4;
        const x = col * (colWidth + 2);
        const y = startY + row * rowHeight;

        const name = paramNames[i] || '';
        const value = paramValues[i];

        if (name) {
            // Truncate name to fit
            let displayName = name.length > 8 ? name.substring(0, 7) + '.' : name;
            print(x + 1, y, displayName, 1, 'small');

            // Value bar
            const barX = x + 44;
            const barW = 18;
            const barH = 5;
            const barY = y + 1;
            const fillW = Math.round((value / 127) * barW);

            // Bar outline
            draw_rect(barX, barY, barW, barH, 1);
            // Bar fill
            if (fillW > 0) {
                fill_rect(barX, barY, fillW, barH, 1);
            }
        }
    }
}

function drawFooter() {
    // Separator line
    draw_rect(0, 53, SCREEN_WIDTH, 1, 1);

    // Bank info (left)
    if (totalBanks > 1) {
        print(1, 56, `Bank ${bankIndex + 1}/${totalBanks}`, 1, 'small');
    }

    // Learn mode indicator (right)
    if (learnMode) {
        const learnText = 'LEARN';
        // Blink effect
        if (Math.floor(tickCount / 15) % 2 === 0) {
            const w = text_width(learnText, 'small');
            fill_rect(SCREEN_WIDTH - w - 4, 55, w + 3, 9, 1);
            print(SCREEN_WIDTH - w - 2, 56, learnText, 0, 'small');
        } else {
            const w = text_width(learnText, 'small');
            print(SCREEN_WIDTH - w - 2, 56, learnText, 1, 'small');
        }
    } else {
        print(SCREEN_WIDTH - 40, 56, 'Menu:Learn', 1, 'small');
    }
}

/* ============================================================================
 * LED Control
 * ============================================================================ */

function enqueueLED(type, id, colour) {
    ledQueue.push({ type, id, colour });
}

function flushLEDQueue() {
    const count = Math.min(LED_MSGS_PER_TICK, ledQueue.length);
    for (let i = 0; i < count; i++) {
        const msg = ledQueue.shift();
        if (msg.type === 'pad') {
            setLED(msg.id, msg.colour);
        } else {
            setButtonLED(msg.id, msg.colour);
        }
    }
}

function initLEDs() {
    // Knob LEDs off
    for (let i = 0; i < 8; i++) {
        enqueueLED('button', KNOB_CCS[i], Black);
    }
    // Nav button LEDs
    enqueueLED('button', MoveMenu, WhiteLedBright);
    enqueueLED('button', MoveBack, WhiteLedBright);
    enqueueLED('button', MoveLeft, WhiteLedBright);
    enqueueLED('button', MoveRight, WhiteLedBright);
    enqueueLED('button', MoveUp, DarkGrey);
    enqueueLED('button', MoveDown, DarkGrey);
    ledsInitialized = true;
}

function updateKnobLEDs() {
    for (let i = 0; i < 8; i++) {
        if (paramNames[i]) {
            // Color based on value - green intensity
            const value = paramValues[i];
            let colour = Black;
            if (value > 0) {
                colour = learnMode ? Cyan : BrightGreen;
            }
            enqueueLED('button', KNOB_CCS[i], colour);
        } else {
            enqueueLED('button', KNOB_CCS[i], Black);
        }
    }
}

/* ============================================================================
 * Lifecycle
 * ============================================================================ */

function init() {
    clearAllLEDs();
    os.sleep(300);

    initLEDs();
    drawScreen();

    // Say hello to Ableton
    console.log('[DC] Module initialized, sending HELLO');
    sendCommand(CMD_HELLO, []);

    // Also send a plain CC as a simpler test
    console.log('[DC] Sending test CC on ch16');
    sendCC(127, 42);
}

function tick() {
    tickCount++;

    // Heartbeat watchdog
    heartbeatTimer++;
    if (tickCount % 60 === 0) {
        console.log(`[DC] TICK heartbeatTimer=${heartbeatTimer} connected=${connected}`);
    }
    if (heartbeatTimer > HEARTBEAT_TIMEOUT_TICKS && connected) {
        console.log(`[DC] WATCHDOG: disconnecting after ${heartbeatTimer} ticks`);
        connected = false;
        needsRedraw = true;
    }

    // Flush LED queue
    flushLEDQueue();

    // Periodic LED update
    if (tickCount % 120 === 0) {
        updateKnobLEDs();
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
