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
        Slot = Rgb.OFF
        SlotRecordButton = Rgb.RED
        NoSlot = Rgb.OFF
        ClipStopped = Rgb.YELLOW
        ClipTriggeredPlay = Rgb.DIM_GREEN
        ClipTriggeredRecord = Rgb.DIM_RED
        ClipPlaying = Rgb.GREEN
        ClipRecording = Rgb.RED
        StopClip = Rgb.RED
        StopClipDisabled = Rgb.OFF
        StopClipTriggered = Rgb.DIM_RED
