from __future__ import absolute_import, print_function, unicode_literals

from ableton.v3.control_surface import ControlSurfaceSpecification, create_skin
from ableton.v3.control_surface.capabilities import NOTES_CC, PORTS_KEY, REMOTE, SCRIPT, inport, outport
from .colors import Rgb
from .elements import Elements
from .keyboard import InstrumentComponent
from .mappings import create_mappings
from .melodic_pattern import NOTE_MODE_FEEDBACK_CHANNELS
from .schwung_device import SchwungDeviceControl
from .skin import Skin


def get_capabilities():
    return {
        PORTS_KEY: [
            inport(props=[NOTES_CC, SCRIPT, REMOTE]),
            outport(props=[NOTES_CC, SCRIPT, REMOTE]),
        ]
    }


class Specification(ControlSurfaceSpecification):
    elements_type = Elements
    control_surface_skin = create_skin(skin=Skin, colors=Rgb)
    create_mappings_function = create_mappings
    feedback_channels = NOTE_MODE_FEEDBACK_CHANNELS
    include_auto_arming = True
    num_tracks = 8
    num_scenes = 4
    component_map = {
        'Instrument': InstrumentComponent,
    }


def create_instance(c_instance):
    return SchwungDeviceControl(c_instance=c_instance, specification=Specification)
