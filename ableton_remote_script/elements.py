"""
Element definitions for SchwungDeviceControl.
Declares the 4x8 pad matrix for note-playing via PlayableComponent.
"""
from __future__ import absolute_import, print_function, unicode_literals

from ableton.v3.control_surface import MIDI_NOTE_TYPE, ElementsBase, create_matrix_identifiers

PAD_CHANNEL = 0  # Pads arrive on channel 1 (index 0), separate from command protocol (ch16)


class Elements(ElementsBase):

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.add_button_matrix(
            create_matrix_identifiers(68, 100, 8, flip_rows=True),
            'Pads',
            msg_type=MIDI_NOTE_TYPE,
            is_rgb=True,
            channel=PAD_CHANNEL,
        )
