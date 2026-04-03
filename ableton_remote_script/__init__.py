from .schwung_device import SchwungDeviceControl


def create_instance(c_instance):
    return SchwungDeviceControl(c_instance)
