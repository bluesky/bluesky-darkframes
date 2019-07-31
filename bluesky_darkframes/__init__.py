import collections
import copy
import logging
import time

import event_model
from frozendict import frozendict
import bluesky.preprocessors
import bluesky.plan_stubs as bps
import numpy
from ophyd import Device

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions


logger = logging.getLogger('bluesky_darkframe')


class SnapshotDevice(Device):
    """
    A mock Device that stashes a snapshot of another Device for later reading

    Parameters
    ----------
    device: Device
    """
    def __init__(self, device):
        super().__init__(name=device.name, parent=device.parent)

        self._describe = device.describe()
        self._describe_configuration = device.describe_configuration()
        self._read = device.read()
        self._read_configuration = device.read_configuration()
        self._read_attrs = list(device.read())
        self._configuration_attrs = list(device.read_configuration())
        self._asset_docs_cache = list(device.collect_asset_docs())
        self._assets_collected = False

    def __repr__(self):
        return f"<SnapshotDevice of {self.name}>"

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


class NoMatchingSnapshot(KeyError):
    ...


class DarkFramePreprocessor:
    """
    A plan preprocessor that ensures each Run records a dark frame.

    Specifically this adds a new Event stream, named 'dark' by default. It
    inserts one Event with a reading that contains a 'dark' frame. The same
    reading may be used across multiple runs, depending on the rules for when a
    dark frame is taken.

    Parameters
    ----------
    dark_plan: callable
        Expected siganture: ``dark_plan() -> snapshot_device``
    max_age: float
        Time after which a fresh dark frame should be acquired
    locked_signals: Iterable, optional
        Any changes to these signals invalidate the current dark frame and
        prompt us to take a new one.
    limit: integer or None, optional
        Number of dark frames to cache. If None, do not limit.
    stream_name : string, optional
        Event stream name for dark frames. Default is 'dark'.
    """
    def __init__(self, *, dark_plan, max_age,
                 locked_signals=None, limit=None, stream_name='dark'):
        self.dark_plan = dark_plan
        self.max_age = max_age
        # The signals have to have unique names for this to work.
        names = [signal.name for signal in locked_signals or ()]
        if len(names) != len(set(names)):
            raise ValueError(
                f"The signals in locked_signals need to have unique names. "
                f"The names given were: {names}")
        self.locked_signals = tuple(locked_signals or ())
        self._limit = limit
        self.stream_name = stream_name
        # Map state to (creation_time, snapshot).
        self._cache = collections.OrderedDict()

    def add_snapshot(self, snapshot, state=None):
        logger.debug("Captured snapshot for state %r", state)
        state = state or {}
        self._evict_old_entries()
        if self._limit is not None and len(self._cache) > self._limit:
            self._cache.popitem()
        self._cache[frozendict(state)] = (time.monotonic(), snapshot)

    def _evict_old_entries(self):
        now = time.monotonic()
        for key, (creation_time, snapshot) in list(self._cache.items()):
            if now - creation_time > self.max_age:
                logger.debug("Evicted old snapshot for state %r", key)
                # Too old. Evict from cache.
                del self._cache[key]

    def get_snapshot(self, state):
        self._evict_old_entries()
        key = frozendict(state)
        try:
            creation_time, snapshot = self._cache[key]
        except KeyError as err:
            raise NoMatchingSnapshot(
                f"No Snapshot matches the state {state}. Perhaps there *was* "
                f"match but it has aged out of the cache.") from err
        else:
            self._cache.move_to_end(key, last=False)
            return snapshot

    def clear(self):
        self._cache.clear()

    def __call__(self, plan):
        "Preprocessor: Takes in a plan and creates a modified plan."

        def tail():
            # Acquire a fresh Snapshot if we need one, or retrieve a cached one.
            state = {}
            for signal in self.locked_signals:
                reading = yield bluesky.plan_stubs.read(signal)
                state[signal.name] = reading
            try:
                snapshot = self.get_snapshot(state)
            except NoMatchingSnapshot:
                snapshot = yield from self.dark_plan()
                self.add_snapshot(snapshot, state)
            # Read the Snapshot into the 'dark' Event stream.
            yield from bps.stage(snapshot)
            yield from bps.trigger_and_read([snapshot], name=self.stream_name)
            yield from bps.unstage(snapshot)

        def insert(msg):
            if msg.command == 'open_run':
                return None, tail()
            else:
                return None, None

        return (yield from bluesky.preprocessors.plan_mutator(plan, insert))


class DarkSubtraction(event_model.DocumentRouter):
    def __init__(self,
                 field,
                 light_stream_name='primary',
                 dark_stream_name='dark'):
        self.field = field
        self.light_stream_name = light_stream_name
        self.dark_stream_name = dark_stream_name
        self.light_descriptor = None
        self.dark_descriptor = None
        self.dark_frame = None

    def descriptor(self, doc):
        if doc['name'] == self.light_stream_name:
            self.light_descriptor = doc['uid']
        elif doc['name'] == self.dark_stream_name:
            self.dark_descriptor = doc['uid']
        return super().descriptor(doc)

    def event_page(self, doc):
        if doc['descriptor'] == self.dark_descriptor:
            self.dark_frame, = doc['data'][self.field]
        if doc['descriptor'] == self.light_descriptor:
            doc = copy.deepcopy(dict(doc))
            light = numpy.asarray(doc['data'][self.field])
            subtracted = self.subtract(light, self.dark_frame)
            doc['data'][self.field] = subtracted
        return super().event_page(doc)

    def subtract(self, light, dark):
        return numpy.clip(light - dark, a_min=0, a_max=None).astype(light.dtype)
