"""
Declarative mappings for SchwungDeviceControl v3.
Wires elements to components.
"""
from __future__ import absolute_import, print_function, unicode_literals


def create_mappings(control_surface):
    mappings = {}
    mappings['Note_Modes'] = dict(
        enable=False,
        keyboard=dict(
            component='Instrument',
            matrix='pads',
        ),
    )
    return mappings
