"""
Simulated devices for documentation and testing
"""
import collections
import itertools
import os
import tempfile
import threading
import time

from bluesky.utils import short_uid
import numpy as np
from ophyd import Signal, Device, Component, DeviceStatus, Staged
from ophyd.sim import new_uid
import scipy.special


x, y = np.mgrid[-100:100, -100:100] * 1/200
r = np.hypot(x, y)
r *= 20
r -= 15
diffraction_pattern = scipy.special.airy(r)[0]
diffraction_pattern -= diffraction_pattern.min()
diffraction_pattern *= np.ptp(diffraction_pattern) * 0.5 * (2 ** 16)
diffraction_pattern = diffraction_pattern.astype('uint16')

shutter_state = {'state': 'open'}


class Shutter(Signal):
    def put(self, value):
        shutter_state['state'] = value
        super().put(value)


def generate_dark_frame():
    values = (np.random.RandomState(0).randint(0, 2**16, 10) * 0.2).astype('uint16')
    # Tile values into bands.
    return np.broadcast_to(np.repeat(values, 20), (200, 200)).copy()


def generate_image(dark=False):
    # TODO Add noise, zingers, and other nondeterministic things.
    output = generate_dark_frame()
    if not dark:
        output += diffraction_pattern
    return output


class TimerStatus(DeviceStatus):
    """Simulate the time it takes for a detector to acquire an image."""
    def __init__(self, device, delay):
        super().__init__(device)
        self.delay = delay  # for introspection purposes
        threading.Timer(delay, self._finished).start()


class DiffractionDetector(Device):
    exposure_time = Component(Signal, value=1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._resource_uid = None
        self._datum_counter = None
        self._asset_docs_cache = collections.deque()
        self.save_path = tempfile.mkdtemp()

        self._path_stem = None
        self._stashed_image_reading = None
        self._stashed_image_data_key = None

    def stage(self):
        file_stem = short_uid()
        self._datum_counter = itertools.count()
        self._path_stem = os.path.join(self.save_path, file_stem)

        self._resource_uid = new_uid()
        resource = {'spec': 'NPY_SEQ',
                    'root': self.save_path,
                    'resource_path': file_stem,
                    'resource_kwargs': {},
                    'uid': self._resource_uid,
                    'path_semantics': {'posix': 'posix', 'nt': 'windows'}[os.name]}
        self._asset_docs_cache.append(('resource', resource))
        return super().stage()

    def trigger(self):
        if not self._staged == Staged.yes:
            raise RuntimeError("Device must be staged before it is triggered.")
        image = generate_image(dark=shutter_state['state'] == 'closed')
        # Save the actual reading['value'] to disk. For a real detector,
        # this part would be done by the detector IOC, not by ophyd.
        data_counter = next(self._datum_counter)
        np.save(f'{self._path_stem}_{data_counter}.npy', image,
                allow_pickle=False)
        # Generate a stash and Datum document.
        datum_id = '{}/{}'.format(self._resource_uid, data_counter)
        datum = {'resource': self._resource_uid,
                 'datum_kwargs': dict(index=data_counter),
                 'datum_id': datum_id}
        self._asset_docs_cache.append(('datum', datum))
        self._stashed_image_reading = {'value': datum_id,
                                       'timestamp': time.time()}
        self._stashed_image_data_key = {'source': 'SIM:image',
                                        'shape': image.shape,
                                        'dtype': 'array',
                                        'external': 'FILESTORE'}
        return TimerStatus(self, self.exposure_time.get())

    def read(self):
        ret = super().read()
        ret[f'{self.name}_image'] = self._stashed_image_reading
        return ret

    def describe(self):
        ret = super().describe()
        ret[f'{self.name}_image'] = self._stashed_image_data_key
        return ret

    def collect_asset_docs(self):
        items = list(self._asset_docs_cache)
        self._asset_docs_cache.clear()
        for item in items:
            yield item

    def unstage(self):
        self._resource_uid = None
        self._datum_counter = None
        self._asset_docs_cache.clear()
        self._path_stem = None
        return super().unstage()
