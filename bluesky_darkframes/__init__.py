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
        self._describe = None
        self._describe_configuration = None
        self._read = None
        self._read_configuration = None
        self._read_attrs = None
        self._configuration_attrs = None
        self._asset_docs_cache = None
        self._assets_collected = False
        super().__init__(name=device.name, parent=device.parent)

        self._describe = device.describe()
        self._describe_configuration = device.describe_configuration()
        self._read = device.read()
        self._read_configuration = device.read_configuration()
        self._read_attrs = list(device.read())
        self._configuration_attrs = list(device.read_configuration())
        self._asset_docs_cache = list(device.collect_asset_docs())

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
    dark_plan : callable
        Expected siganture: ``dark_plan() -> snapshot_device``
    max_age : float
        Time after which a fresh dark frame should be acquired
    locked_signals : Iterable
        Any changes to these signals invalidate the current dark frame and
        prompt us to take a new one.
    """
    def __init__(self, *, dark_plan, max_age, locked_signals=None):
        self.dark_plan = dark_plan
        self.max_age = max_age
        # The signals have to have unique names for this to work.
        names = [signal.name for signal in locked_signals or ()]
        if len(names) != len(set(names)):
            raise ValueError(
                f"The signals in locked_signals need to have unique names. "
                f"The names given were: {names}")
        self.locked_signals = tuple(locked_signals or ())
        self._cache = {}  # map state to (creation_time, snapshot)

    def add_snapshot(self, snapshot, state=None):
        logger.debug("Captured snapshot for state %r", state)
        state = state or {}
        self._cache[frozendict(state)] = (time.monotonic(), snapshot)

    def get_snapshot(self, state):
        # First, evict any cache entries that are too old.
        now = time.monotonic()
        for key, (creation_time, snapshot) in list(state.items()):
            if now - creation_time > self.max_age:
                logger.debug("Evicted old snapshot for state %r", state)
                # Too old. Evict from cache.
                del self._cache[key]
        try:
            creation_time, snapshot = self._cache[frozendict(state)]
            return snapshot
        except KeyError as err:
            raise NoMatchingSnapshot(
                f"No Snapshot matches the state {state}. Perhaps there *was* "
                f"match but it has aged out of the cache.") from err

    def __call__(self, plan):

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
            yield from bps.trigger_and_read([snapshot], name='dark')
            yield from bps.unstage(snapshot)

        def insert(msg):
            if msg.command == 'open_run':
                return None, tail()
            else:
                return None, None

        return (yield from bluesky.preprocessors.plan_mutator(plan, insert))


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
