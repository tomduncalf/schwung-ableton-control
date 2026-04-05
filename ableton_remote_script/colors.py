from __future__ import absolute_import, print_function, unicode_literals

from ableton.v3.control_surface.elements.color import RgbColor


class Rgb:
    GREEN = RgbColor(0, 127, 0)
    RED = RgbColor(127, 0, 0)
    OFF = RgbColor(0, 0, 0)
    WHITE = RgbColor(127, 127, 127)
    YELLOW = RgbColor(127, 127, 0)
    BLUE = RgbColor(0, 0, 127)
    CYAN = RgbColor(0, 127, 127)
    DIM_WHITE = RgbColor(40, 40, 40)
    DIM_GREEN = RgbColor(0, 40, 0)
    DIM_RED = RgbColor(40, 0, 0)
