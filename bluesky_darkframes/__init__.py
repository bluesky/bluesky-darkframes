import time

import event_model
import bluesky.preprocessors
import bluesky.plan_stubs as bps
import numpy
from ophyd import Device

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions


class DarkFrameCache(Device):
    """
    A mock Device that stashes a dark frame and returns it when read.

    Parameters
    ----------
    *args
        Passed through to base class, ophyd.Device
    **kwargs
        Passed through to base class, ophyd.Device
    """
    def __init__(self, *args, **kwargs):
        # self.det = det
        self.last_collected = None
        self.just_started = True
        self._assets_collected = True
        self._describe = None
        self._describe_configuration = None
        self._read = None
        self._read_configuration = None
        self._read_attrs = None
        self._configuration_attrs = None
        self._asset_docs_cache = None
        return super().__init__(*args, **kwargs)

    def read(self):
        return self._read

    def read_configuration(self):
        return self._read_configuration

    @property
    def configuration_attrs(self):
        return self._configuration_attrs

    @property
    def read_attrs(self):
        return self._read_attrs

    def describe(self):
        return self._describe

    def describe_configuration(self):
        return self._describe_configuration

    def collect_asset_docs(self):
        if self._assets_collected:
            yield from []
        else:
            yield from self._asset_docs_cache

    def stage(self):
        self._assets_collected = False

    def capture(self, camera):
        self._describe = camera.describe()
        self._describe_configuration = camera.describe_configuration()
        self._read = camera.read()
        self._read_configuration = camera.read_configuration()
        self._read_attrs = list(camera.read())
        self._configuration_attrs = list(camera.read_configuration())
        self._asset_docs_cache = list(camera.collect_asset_docs())
        self.last_collected = time.monotonic()


class InsertReferenceToDarkFrame:
    """
    A plan preprocessor that ensures one 'dark' Event is created per run.

    Parameters
    ----------
    dark_frame_cache: DarkFrameCache
        A mock Device that caches a dark frame and returns it when read.
    stream_name: string, optional
        Default is ``'dark'``
    """
    def __init__(self, dark_frame_cache, stream_name='dark'):
        self.dark_frame_cache = dark_frame_cache
        self.stream_name = stream_name

    def __call__(self, plan):

        def insert_reference_to_dark_frame(msg):
            if msg.command == 'open_run':
                return (
                    bluesky.preprocessors.pchain(
                        bluesky.preprocessors.single_gen(msg),
                        bps.stage(self.dark_frame_cache),
                        bps.trigger_and_read([self.dark_frame_cache], name='dark'),
                        bps.unstage(self.dark_frame_cache)
                    ),
                    None,
                )
            else:
                return None, None

        return (yield from bluesky.preprocessors.plan_mutator(
            plan, insert_reference_to_dark_frame))


class TakeDarkFrames:
    """
    A plan preprocessor that inserts instructions to take a fresh dark frame.
    """
    def __init__(self, *, dark_frame_cache, max_age, dark_plan):
        self.dark_frame_cache = dark_frame_cache
        self.max_age = max_age
        self.dark_plan = dark_plan

    def __call__(self, plan):

        def insert_take_dark(msg):
            if (msg.command == 'open_run' and
                    (self.dark_frame_cache.last_collected is None or
                     self.max_age <
                     time.monotonic() - self.dark_frame_cache.last_collected)):
                return (
                    bluesky.preprocessors.pchain(
                        self.dark_plan(),
                        bluesky.preprocessors.single_gen(msg),
                    ),
                    None,
                )
            else:
                return None, None

        return (yield from bluesky.preprocessors.plan_mutator(plan, insert_take_dark))


class DarkSubtraction(event_model.DocumentRouter):
    def __init__(self, *args, **kwargs):
        self.dark_descriptor = None
        self.primary_descriptor = None
        self.dark_frame = None
        super().__init__(*args, **kwargs)

    def descriptor(self, doc):
        if doc['name'] == 'dark':
            self.dark_descriptor = doc['uid']
        elif doc['name'] == 'primary':
            self.primary_descriptor = doc['uid']
        return super().descriptor(doc)

    def event_page(self, doc):
        event = self.event  # Avoid attribute lookup in hot loop.
        filled_events = []

        for event_doc in event_model.unpack_event_page(doc):
            filled_events.append(event(event_doc))
        new_event_page = event_model.pack_event_page(*filled_events)
        # Modify original doc in place, as we do with 'event'.
        doc['data'] = new_event_page['data']
        return doc

    def event(self, doc):
        FIELD = 'det_img'  # TODO Do not hard-code this.
        if doc['descriptor'] == self.dark_descriptor:
            self.dark_frame = doc['data']['det_img']
        if doc['descriptor'] == self.primary_descriptor:
            doc['data'][FIELD] = self.subtract(doc['data'][FIELD], self.dark_frame)
        return doc

    def subtract(self, light, dark):
        return numpy.clip(light - dark, a_min=0, a_max=None).astype(numpy.uint16)
