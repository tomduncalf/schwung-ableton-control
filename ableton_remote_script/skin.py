from __future__ import absolute_import, print_function, unicode_literals
from .colors import Rgb


class Skin:

    class DefaultButton:
        On = Rgb.WHITE
        Off = Rgb.OFF
        Pressed = Rgb.WHITE

    class Instrument:
        PadAction = Rgb.WHITE
        NoteBase = Rgb.GREEN
        NoteScale = Rgb.DIM_WHITE
        NoteNotScale = Rgb.OFF
        NoteInvalid = Rgb.OFF
        NoteSelected = Rgb.WHITE
        NoteInStep = Rgb.WHITE

    class Session:
        # All OFF — we send grid colors via custom SysEx, not the v3 skin system.
        # Non-OFF values here cause the framework to send RGB MIDI that Move
        # misinterprets as LED data, producing "noise" on the pad grid.
        Slot = Rgb.OFF
        SlotRecordButton = Rgb.OFF
        NoSlot = Rgb.OFF
        ClipStopped = Rgb.OFF
        ClipTriggeredPlay = Rgb.OFF
        ClipTriggeredRecord = Rgb.OFF
        ClipPlaying = Rgb.OFF
        ClipRecording = Rgb.OFF
        StopClip = Rgb.OFF
        StopClipDisabled = Rgb.OFF
        StopClipTriggered = Rgb.OFF
