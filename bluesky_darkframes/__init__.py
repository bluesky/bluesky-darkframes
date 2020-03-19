import collections
import copy
import functools
import inspect
import logging
import time
import uuid
import warnings

import event_model
from frozendict import frozendict
import bluesky.preprocessors
import bluesky.plan_stubs as bps
from bluesky.utils import short_uid
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
        self._read_attrs = list(self._read)
        self._configuration_attrs = list(self._read_configuration)
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
            self._assets_collected = True
            yield from self._asset_docs_cache

    def stage(self):
        self._assets_collected = False
        return super().stage()

    def unstage(self):
        self._remake_docs()
        return super().unstage()

    def _remake_docs(self):
        """
        Avoid re-emitting documents with the same unique identifiers.

        - Make shallow copies of Resource and Datum docs with new unique identifiers.
        - Update the return value of read() with the new datum_ids.
        """
        resources = {}  # map old uid to new uid
        new_asset_docs_cache = []
        for name, doc in self._asset_docs_cache:
            if name == 'resource':
                new_uid = str(uuid.uuid4())
                resources[doc['uid']] = new_uid
                new_doc = doc.copy()
                new_doc['uid'] = new_uid
                new_asset_docs_cache.append((name, new_doc))
            elif name == 'datum':
                new_doc = doc.copy()
                old_resource_uid = doc['resource']
                new_resource_uid = resources[old_resource_uid]
                new_doc['resource'] = new_resource_uid
                # Some existing code in other libraries looks for the
                # {resource_uid}/{integer} pattern in Event documents and uses that
                # to take a fast path. The changes to Datum proposed in
                # https://github.com/bluesky/event-model/issues/156
                # would make this less fragile.
                old_datum_id = doc['datum_id']
                if old_datum_id.startswith(f"{old_resource_uid}/"):
                    _, suffix = old_datum_id.split('/', 1)
                    new_datum_id = f"{new_resource_uid}/{suffix}"
                else:
                    new_datum_id = str(uuid.uuid4())
                new_doc['datum_id'] = new_datum_id
                new_asset_docs_cache.append((name, new_doc))
                # Update the return value of read() to replace the old datum_id
                # with the new one.
                for k, v in list(self._read.items()):
                    if v['value'] == old_datum_id:
                        self._read[k]['value'] = new_datum_id
            else:
                raise BlueskyDarkframesValueError(f"Unexpected name {name}")
        self._asset_docs_cache = new_asset_docs_cache


class _SnapshotShell:
    # This enables us to hot-swap Snapshot instances in the middle of a Run.
    # We hand this object to the RunEngine, so it sees one consistent
    # instance throughout the Run.
    def __init__(self):
        self.__snapshot = None

    def set_snaphsot(self, snapshot):
        self.__snapshot = snapshot

    def get_snapshot(self):
        return self.__snapshot

    def __getattr__(self, key):
        return getattr(self.__snapshot, key)


GROUP_PREFIX = 'bluesky-darkframes-trigger'


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
        Expected siganture: ``dark_plan(detector) -> snapshot_device``

        The old signature ``dark_plan() -> snapshot_device`` is accepted but
        will issue a warning.
    detector: Device
    max_age: float
        Time after which a fresh dark frame should be acquired
    locked_signals: Iterable, optional
        Any changes to these signals invalidate the current dark frame and
        prompt us to take a new one. Typical examples would be exposure time or
        gain, anything that changes the expected dark frame.
    limit: integer or None, optional
        Number of dark frames to cache. If None, do not limit.
    stream_name: string, optional
        Event stream name for dark frames. Default is 'dark'.
    """
    def __init__(self, *, dark_plan, detector, max_age,
                 locked_signals=None, limit=None, stream_name='dark'):
        self._dark_plan = dark_plan
        if not inspect.signature(dark_plan).parameters:
            warnings.warn(
                f"The dark_plan {dark_plan} is now expected to accept the "
                f"detector as an argument. A dark_plan that accepts no "
                f"arguments is still supported, but it may not be supported "
                f"in future releases.")
            self._partialed_dark_plan = dark_plan
        else:
            self._partialed_dark_plan = functools.partial(dark_plan, detector)
        self._detector = detector
        self.max_age = max_age
        # The signals have to have unique names for this to work.
        names = [signal.name for signal in locked_signals or ()]
        if len(names) != len(set(names)):
            raise BlueskyDarkframesValueError(
                f"The signals in locked_signals need to have unique names. "
                f"The names given were: {names}")
        self._locked_signals = tuple(locked_signals or ())
        self._limit = limit
        self.stream_name = stream_name
        # Map state to (creation_time, snapshot).
        self._cache = collections.OrderedDict()
        self._current_snapshot = _SnapshotShell()
        self._force_read_before_next_event = True
        self._latch = False
        self._disabled = False

    @property
    def detector(self):
        return self._detector

    @property
    def dark_plan(self):
        return self._dark_plan

    @property
    def locked_signals(self):
        return self._locked_signals

    @property
    def cache(self):
        """
        A read-only view of the cached dark frames.

        Each key is a frozendict mapping each of the names of the
        ``locked_signals`` (if any) to its value at the time the dark frame was
        taken.

        Each value has the structure ``(creation_time, snapshot)`` where
        creation_time is the UNIX epoch time that the dark frame was taken and
        snapshot is a :class:`SnapshotDevice` instance.

        The cache is ordered. When an item is accessed, it is moved to the
        front. If ``limit`` is set, items will be removed from the end as
        needed to abide by the limit.

        Whenver the cache is updated or accessed, any items whose
        ``creation_time`` is more than ``max_age`` seconds ago are culled.
        """
        return self._cache

    def add_snapshot(self, snapshot, state=None):
        """
        Add a darkframe.

        Parameters
        ----------
        snapshot: SnapshotDevice
        state: dict, optional
            Mapping each of the names of the locked_signals (if any) to its
            value when the snapshot was taken. When snapshots are accessed via
            ``get_snapshot(state)``, the states will be compared via ``==``.
        """
        logger.debug("Captured snapshot for state %r", state)
        state = state or {}
        self._evict_old_entries()
        if self._limit is not None and len(self._cache) >= self._limit:
            self._cache.popitem()
        self._cache[frozendict(state)] = (time.monotonic(), snapshot)

    def _evict_old_entries(self):
        now = time.monotonic()
        for key, (creation_time, _snapshot) in list(self._cache.items()):
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
            Mapping each of the names of the locked_signals (if any) to its
            value.
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
        """
        Preprocessor: Takes in a plan and creates a modified plan.

        This inserts messages to add extra readings to the plan. First, it
        decides whether it needs to trigger the detector to get a fresh reading
        or whether it can use a cached reading.
        """

        if self._disabled:
            logger.info("%r is disabled, will act as a no-op", self)
            return (yield from plan)

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
            try:
                snapshot = self.get_snapshot(state)
            except NoMatchingSnapshot:
                # If we are here, we either haven't taken a reading when the
                # locked_signals were in this state, or the last such reading
                # we took has aged out of the cache. We have to trigger the
                # hardware and get a fresh snapshot.
                logger.info("Taking a new %r reading for state=%r",
                            self.stream_name, state)
                snapshot = yield from self._partialed_dark_plan()
                self.add_snapshot(snapshot, state)
            # If the Snapshot is the same as the one we most recently inserted,
            # then we don't need to create a new Event. The previous Event
            # still holds.
            snapshot_changed = snapshot is not self._current_snapshot.get_snapshot()
            if snapshot_changed or force_read:
                logger.info("Creating a %r Event for state=%r",
                            self.stream_name, state)
                self._current_snapshot.set_snaphsot(snapshot)
                # Read the Snapshot. This does not actually trigger hardware,
                # but it goes through all the bluesky steps to generate new
                # Event.
                # The reason we handle self._current_snapshot here instead of
                # snapshot itself is the bluesky RunEngine notices if you give
                # it a different object than you had given it earlier. Thus,
                # bluesky will always see the "Device" self._current_snapshot
                # here, and it will be satisfied.
                yield from bps.stage(self._current_snapshot)
                yield from trigger_and_read(
                    [self._current_snapshot],
                    name=self.stream_name,
                    group=short_uid(GROUP_PREFIX))
                yield from bps.unstage(self._current_snapshot)
            self._latch = False
            if msg is not None:
                return (yield msg)

        def maybe_insert_dark_frame(msg):
            # * Is this a 'trigger' message?
            # * Does it refer to the detector that this preprocessor cares about?
            # * Is it *not* from another DarkFramePreprocessor. (There can be
            # multiple nested DarkFramePreprocessors watching the same detector
            # by applying different dark_plans / recording Events in different
            # streams.)
            if (msg.command == 'trigger'
                    and msg.obj is self.detector
                    and not msg.kwargs['group'].startswith(GROUP_PREFIX)
                    and not self._latch):
                force_read = self._force_read_before_next_event
                self._force_read_before_next_event = False
                self._latch = True  # prevents infinite recursion
                return insert_dark_frame(force_read=force_read, msg=msg), None
            elif msg.command == 'open_run':
                # Make sure we get a new Event because we have just started a
                # new Run.
                self._force_read_before_next_event = True
                return None, None
            else:
                return None, None

        return (yield from bluesky.preprocessors.plan_mutator(
            plan, maybe_insert_dark_frame))

    def __repr__(self):
        return f"<{self.__class__.__name__} {len(self.cache)} snapshots cached>"

    def disable(self):
        """
        Make this preprocessor a no-op.

        See Also
        --------
        :meth:`DarkFramePreprocessor.enable`
        """
        self._disabled = True

    def enable(self):
        """
        Counterpart to ``diasble()``.

        See Also
        --------
        :meth:`DarkFramePreprocessor.disable`
        """
        self._disabled = False


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

         Defaults to 100.
    """
    def __init__(self,
                 field,
                 light_stream_name='primary',
                 dark_stream_name='dark',
                 pedestal=100):
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
            doc = copy.deepcopy(dict(doc))
            doc['data_keys'][f'{self.field}_is_background_subtracted'] = {
                'source': 'DarkSubtraction',
                'dtype': 'number',
                'shape': [],
                'precsion': 0,
                'object_name': f'{self.field}_DarkSubtraction'}
            doc['configuration'][f'{self.field}_DarkSubtraction'] = {
                'data': {'pedestal': self.pedestal},
                'timestamps': {'pedestal': time.time()},
                'data_keys': {
                    'pedestal': {
                        'source': 'DarkSubtraction',
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
        return doc

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
        return doc

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


# Vendored from bluesky.plan_stubs, added `group` parameter


def trigger_and_read(devices, name='primary', group=None):
    """
    Trigger and read a list of detectors and bundle readings into one Event.

    Parameters
    ----------
    devices : iterable
        devices to trigger (if they have a trigger method) and then read
    name : string, optional
        event stream name, a convenient human-friendly identifier; default
        name is 'primary'
    group : string (or any hashable object), optional
        identifier used by 'trigger' and 'wait'

    Yields
    ------
    msg : Msg
        messages to 'trigger', 'wait' and 'read'
    """
    # If devices is empty, don't emit 'create'/'save' messages.
    if not devices:
        yield from bps.null()
    devices = bps.separate_devices(devices)  # remove redundant entries
    rewindable = bps.all_safe_rewind(devices)  # if devices can be re-triggered

    def inner_trigger_and_read():
        nonlocal group
        if group is None:
            group = short_uid('trigger')
        no_wait = True
        for obj in devices:
            if hasattr(obj, 'trigger'):
                no_wait = False
                yield from bps.trigger(obj, group=group)
        # Skip 'wait' if none of the devices implemented a trigger method.
        if not no_wait:
            yield from bps.wait(group=group)
        yield from bps.create(name)
        ret = {}  # collect and return readings to give plan access to them
        for obj in devices:
            reading = (yield from bps.read(obj))
            if reading is not None:
                ret.update(reading)
        yield from bps.save()
        return ret
    from bluesky.preprocessors import rewindable_wrapper
    return (yield from rewindable_wrapper(inner_trigger_and_read(),
                                          rewindable))
