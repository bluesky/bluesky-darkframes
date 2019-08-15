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


logger = logging.getLogger('bluesky.darkframes')


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


class _SnapshotShell:
    # This enables us to hot-swap Snapshot instances in the middle of a Run.
    # We hand this object to the RunEngine, so it sees one consistent
    # instance throughout the Run.
    def __init__(self):
        self.__snapshot = None

    def set_snaphsot(self, snapshot):
        self.__snapshot = snapshot

    def __getattr__(self, key):
        return getattr(self.__snapshot, key)


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
    detector : Device
    max_age: float
        Time after which a fresh dark frame should be acquired
    locked_signals: Iterable, optional
        Any changes to these signals invalidate the current dark frame and
        prompt us to take a new one.
    limit: integer or None, optional
        Number of dark frames to cache. If None, do not limit.
    stream_name: string, optional
        Event stream name for dark frames. Default is 'dark'.
    """
    def __init__(self, *, dark_plan, detector, max_age,
                 locked_signals=None, limit=None, stream_name='dark'):
        self.dark_plan = dark_plan
        self.detector = detector
        self.max_age = max_age
        # The signals have to have unique names for this to work.
        names = [signal.name for signal in locked_signals or ()]
        if len(names) != len(set(names)):
            raise BlueskyDarkframesValueError(
                f"The signals in locked_signals need to have unique names. "
                f"The names given were: {names}")
        self.locked_signals = tuple(locked_signals or ())
        self._limit = limit
        self.stream_name = stream_name
        # Map state to (creation_time, snapshot).
        self._cache = collections.OrderedDict()
        self._current_snapshot = _SnapshotShell()
        self._current_state = None
        self._force_read_before_next_event = True
        self._latch = False

    @property
    def cache(self):
        """
        A read-only view of the cached dark frames.
        """
        return self._cache

    def add_snapshot(self, snapshot, state=None):
        """
        Add a darkframe.

        Parameters
        ----------
        snapshot: SnapshotDevice
        state: dict, optional
        """
        logger.debug("Captured snapshot for state %r", state)
        state = state or {}
        self._evict_old_entries()
        if self._limit is not None and len(self._cache) >= self._limit:
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
        """
        Access a darkframe.

        Parameters
        ----------
        state: dict
        """
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
        """
        Clear all cached darkframes.
        """
        self._cache.clear()

    def __call__(self, plan):
        "Preprocessor: Takes in a plan and creates a modified plan."

        def insert_dark_frame(force_read, msg=None):
            # Acquire a fresh Snapshot if we need one, or retrieve a cached one.
            state = {}
            for signal in self.locked_signals:
                reading = yield from bluesky.plan_stubs.read(signal)
                # Restructure
                # {'data_key': {'value': <value>, 'timestamp': <timestamp>}, ...}
                # into (('data_key', <value>) ...).
                values_only = tuple((k, v['value']) for k, v in reading.items())
                state[signal.name] = values_only
            if self._current_state != state:
                self._current_state = state
                snapshot_changed = True
            else:
                snapshot_changed = False
            try:
                snapshot = self.get_snapshot(state)
            except NoMatchingSnapshot:
                logger.info(f"Taking a new dark frame for state=%r", state)
                snapshot = yield from self.dark_plan()
                self.add_snapshot(snapshot, state)
            if snapshot_changed or force_read:
                logger.info(f"Creating a 'dark' Event for state=%r", state)
                self._current_snapshot.set_snaphsot(snapshot)
                # Read the Snapshot into the 'dark' Event stream.
                yield from bps.stage(self._current_snapshot)
                yield from bps.trigger_and_read([self._current_snapshot],
                                                name=self.stream_name)
                yield from bps.unstage(self._current_snapshot)
            self._latch = False
            if msg is not None:
                return (yield msg)

        def maybe_insert_dark_frame(msg):
            if msg.command == 'trigger' and msg.obj is self.detector and not self._latch:
                force_read = self._force_read_before_next_event
                self._force_read_before_next_event = False
                self._latch = True
                return insert_dark_frame(force_read=force_read, msg=msg), None
            elif msg.command == 'open_run':
                self._force_read_before_next_event = True
                return None, None
            else:
                return None, None

        return (yield from bluesky.preprocessors.plan_mutator(
            plan, maybe_insert_dark_frame))


class DarkSubtraction(event_model.DocumentRouter):
    """Document router to do in-place background subtraction.

    Expects that the events are filled.

    The values in `(light_stream_name, field)` are replaced with ::

        np.clip(light - np.clip(dark - pedestal, 0), 0)


    Adds the key f'{self.field}_is_background_subtracted' to the
    'light_stream_name' stream and a configuration key for the
    pedestal value.


    .. warning

       This mutates the document stream in-place!


    Parameters
    ----------
    field : str
        The name of the field to do the background subtraction on.

        This field must contain the light-field values in the
        'light-stream' and the background images in the 'dark-stream'

    light_stream_name : str, optional
         The stream that contains the exposed images that need to be
         background subtracted.

         defaults to 'primary'

    dark_stream_name : str, optional
         The stream that contains the background dark images.

         defaults to 'dark'

    pedestal : int, optional
         Pedestal to add to the data to make sure subtracted result does not
         fall below 0.

         This is actually pre subtracted from the dark frame for efficiency.

         defaults to 100

    """
    def __init__(self,
                 field,
                 light_stream_name='primary',
                 dark_stream_name='dark',
                 pedestal=0):
        self.field = field
        self.light_stream_name = light_stream_name
        self.dark_stream_name = dark_stream_name
        self.light_descriptor = None
        self.dark_descriptor = None
        self.dark_frame = None
        self.pedestal = pedestal

    def descriptor(self, doc):
        if doc['name'] == self.light_stream_name:
            self.light_descriptor = doc['uid']
            # add flag that we did the background subtraction
            doc['data_keys'][f'{self.field}_is_background_subtracted'] = {
                'dtype': 'number',
                'shape': [],
                'precsion': 0,
                'object_name': f'{self.field}_DarkSubtraction'}
            doc['configuration'][f'{self.field}_DarkSubtraction'] = {
                'data': {'pedestal': self.pedestal},
                'timestamp': {'pedestal': time.time()},
                'data_keys': {
                    'pedestal': {
                        'dtype': 'number',
                        'shape': [],
                        'precsion': 0,
                    }
                }
            }
            doc['object_keys'][f'{self.field}_DarkSubtraction'] = [
                f'{self.field}_is_background_subtracted']

        elif doc['name'] == self.dark_stream_name:
            self.dark_descriptor = doc['uid']
        return super().descriptor(doc)

    def event_page(self, doc):
        if doc['descriptor'] == self.dark_descriptor:
            self.dark_frame, = doc['data'][self.field]
            self.dark_frame -= self.pedestal
            numpy.clip(self.dark_frame, a_min=0, a_max=None, out=self.dark_frame)
        elif doc['descriptor'] == self.light_descriptor:
            if self.dark_frame is None:
                raise NoDarkFrame(
                    "DarkSubtraction has not received a 'dark' Event yet, so "
                    "it has nothing to subtract.")
            doc = copy.deepcopy(dict(doc))
            light = numpy.asarray(doc['data'][self.field])
            subtracted = self.subtract(light, self.dark_frame)
            doc['data'][self.field] = subtracted
            doc['data'][f'{self.field}_is_background_subtracted'] = [True]
            doc['timestamps'][f'{self.field}_is_background_subtracted'] = [time.time()]
        return super().event_page(doc)

    def subtract(self, light, dark):
        return numpy.clip(light - dark, a_min=0, a_max=None).astype(light.dtype)


class BlueskyDarkframesException(Exception):
    ...


class BlueskyDarkframesValueError(ValueError, BlueskyDarkframesException):
    ...


class NoDarkFrame(RuntimeError, BlueskyDarkframesException):
    ...


class NoMatchingSnapshot(KeyError, BlueskyDarkframesException):
    ...
