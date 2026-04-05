from __future__ import absolute_import, print_function, unicode_literals


class Skin:

    class DefaultButton:
        On = 'DefaultButton.On'
        Off = 'DefaultButton.Off'
        Pressed = 'DefaultButton.Pressed'

    class Instrument:
        NoteBase = 'Instrument.NoteBase'
        NoteScale = 'Instrument.NoteScale'
        NoteNotScale = 'Instrument.NoteNotScale'
        NoteInvalid = 'Instrument.NoteInvalid'
        NoteSelected = 'Instrument.NoteSelected'
        NoteInStep = 'Instrument.NoteInStep'
        PadAction = 'Instrument.PadAction'

    class Keyboard:
        Natural = 'Keyboard.Natural'
        Sharp = 'Keyboard.Sharp'
