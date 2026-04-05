"""
InstrumentComponent — scale-aware note grid for the 4x8 pad matrix.
Ported from Ableton Move's instrument.py, adapted for Schwung Device Control.
"""
from __future__ import absolute_import, print_function, unicode_literals

from ableton.v3.base import EventObject, depends, find_if, index_if, listenable_property, listens
from ableton.v3.control_surface import LiveObjSkinEntry
from ableton.v3.control_surface.components import Pageable, PageComponent, PitchProvider, PlayableComponent
from ableton.v3.control_surface.display import Renderable
from .melodic_pattern import CHROMATIC_MODE_OFFSET, SCALES, MelodicPattern, NOTE_MODE_FEEDBACK_CHANNELS

DEFAULT_SCALE = SCALES[0]


class NoteLayout(EventObject, Renderable):
    """Tracks song scale/root and user preferences for in-key / interval."""

    @depends(song=None)
    def __init__(self, song=None, preferences=None, *a, **k):
        super().__init__(*a, **k)
        self._song = song
        self._scale = self._get_scale_from_name(self._song.scale_name)
        self._preferences = preferences if preferences is not None else {}
        self._is_in_key = self._preferences.setdefault('is_in_key', True)
        self._interval = self._preferences.setdefault('interval', None)
        self.__on_root_note_changed.subject = self._song
        self.__on_scale_name_changed.subject = self._song

    @property
    def notes(self):
        return self.scale.to_root_note(self.root_note).notes

    @listenable_property
    def root_note(self):
        return self._song.root_note

    @root_note.setter
    def root_note(self, root_note):
        self._song.root_note = root_note

    @listenable_property
    def scale(self):
        return self._scale

    @scale.setter
    def scale(self, scale):
        self._scale = scale
        self._song.scale_name = scale.name
        self.notify_scale(self._scale)

    @listenable_property
    def is_in_key(self):
        return self._is_in_key

    @is_in_key.setter
    def is_in_key(self, is_in_key):
        self._is_in_key = is_in_key
        self._preferences['is_in_key'] = self._is_in_key
        self.notify_is_in_key(self._is_in_key)

    def toggle_is_in_key(self):
        self.is_in_key = not self._is_in_key

    @listenable_property
    def interval(self):
        return self._interval

    @interval.setter
    def interval(self, interval):
        self._interval = interval
        self._preferences['interval'] = self._interval
        self.notify_interval(self._interval)

    def toggle_interval(self):
        self.interval = 3 if self._interval is None else None

    @staticmethod
    def _get_scale_from_name(name):
        return find_if(lambda scale: scale.name == name, SCALES) or DEFAULT_SCALE

    @listens('root_note')
    def __on_root_note_changed(self):
        self.notify_root_note(self._song.root_note)

    @listens('scale_name')
    def __on_scale_name_changed(self):
        self._scale = self._get_scale_from_name(self._song.scale_name)
        self.notify_scale(self._scale)


class InstrumentComponent(PlayableComponent, PageComponent, Pageable, Renderable, PitchProvider):
    """Maps the 4x8 pad grid to scale-aware MIDI notes."""

    is_polyphonic = True

    @depends(note_layout=None, target_track=None)
    def __init__(self, note_layout=None, target_track=None, *a, **k):
        super().__init__(
            *a,
            name='Instrument',
            scroll_skin_name='Instrument.Scroll',
            matrix_always_listenable=True,
            **k
        )
        self._note_layout = note_layout
        self._target_track = target_track
        self._first_note = self.page_length * 5 + self.page_offset
        self._pattern = self._get_pattern()
        self.pitches = [self._pattern.note(0, 0).index]
        self._last_page_length = self.page_length
        self._last_page_offset = self.page_offset
        self._on_note_layout_changed_cb = self._on_note_layout_changed
        for event in ['scale', 'root_note', 'is_in_key', 'interval']:
            self.register_slot(self._note_layout, self._on_note_layout_changed_cb, event)
        self._update_pattern()

    @property
    def note_layout(self):
        return self._note_layout

    @property
    def page_length(self):
        return len(self._note_layout.notes) if self._note_layout.is_in_key else 12

    @property
    def position_count(self):
        if not self._note_layout.is_in_key:
            return 139
        offset = self.page_offset
        octaves = 11 if self._note_layout.notes[0] < 8 else 10
        return offset + len(self._note_layout.notes) * octaves

    def _first_scale_note_offset(self):
        if not self._note_layout.is_in_key:
            return self._note_layout.notes[0]
        if self._note_layout.notes[0] == 0:
            return 0
        return len(self._note_layout.notes) - index_if(lambda n: n >= 12, self._note_layout.notes)

    @property
    def page_offset(self):
        return self._first_scale_note_offset()

    @property
    def position(self):
        return self._first_note

    @position.setter
    def position(self, note):
        self._first_note = note
        self._update_pattern()
        self._update_matrix()
        self.notify_position()

    @property
    def min_pitch(self):
        return self.pattern[0].index

    @property
    def max_pitch(self):
        identifiers = [control.identifier for control in self.matrix if control.identifier is not None]
        return max(identifiers) if len(identifiers) > 0 else 127

    @property
    def pattern(self):
        return self._pattern

    def _on_matrix_pressed(self, button):
        pitch = self._get_note_info_for_coordinate(button.coordinate).index
        if pitch is not None:
            if pitch not in self.pitches:
                self.pitches = [pitch]
            self._update_button_color(button)

    def scroll_page_up(self):
        super().scroll_page_up()

    def scroll_page_down(self):
        super().scroll_page_down()

    def _align_first_note(self):
        self._first_note = self.page_offset + (
            (self._first_note - self._last_page_offset) * (self.page_length / self._last_page_length)
        )
        if self._first_note >= self.position_count:
            self._first_note -= self.page_length
        self._last_page_length = self.page_length
        self._last_page_offset = self.page_offset

    def _on_note_layout_changed(self, _=None):
        self._update_scale()

    def _update_scale(self):
        self._align_first_note()
        self._update_pattern()
        self._update_matrix()
        self.notify_position()

    def update(self):
        super().update()
        if self.is_enabled():
            self._update_matrix()

    def _update_pattern(self):
        self._pattern = self._get_pattern()

    def _invert_and_swap_coordinates(self, coordinates):
        return (coordinates[1], self.height - 1 - coordinates[0])

    def _get_note_info_for_coordinate(self, coordinate):
        x, y = self._invert_and_swap_coordinates(coordinate)
        return self.pattern.note(x, y)

    def _update_button_color(self, button):
        note_info = self._get_note_info_for_coordinate(button.coordinate)
        if self._target_track is not None:
            color = LiveObjSkinEntry(
                'Instrument.{}'.format(note_info.color),
                self._target_track.target_track
            )
        else:
            color = 'Instrument.{}'.format(note_info.color)
        button.color = color

    def _button_should_be_enabled(self, button):
        return self._get_note_info_for_coordinate(button.coordinate).index is not None

    def _note_translation_for_button(self, button):
        note_info = self._get_note_info_for_coordinate(button.coordinate)
        return (note_info.index, note_info.channel)

    def _update_matrix(self):
        self._update_control_from_script()
        self._update_note_translations()
        self._update_led_feedback()

    def _get_pattern(self, first_note=None):
        if first_note is None:
            first_note = int(round(self._first_note))
        interval = self._note_layout.interval or len(self._note_layout.notes)
        notes = self._note_layout.notes
        octave = first_note // self.page_length
        offset = first_note % self.page_length - self._first_scale_note_offset()
        if not self._note_layout.is_in_key:
            interval = 5
            offset -= CHROMATIC_MODE_OFFSET
        return MelodicPattern(
            steps=[1, interval],
            scale=notes,
            origin=[offset, 0],
            root_note=octave * 12,
            chromatic_mode=not self._note_layout.is_in_key
        )
