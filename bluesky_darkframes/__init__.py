import time

import event_model
import bluesky.preprocessors
import bluesky.plan_stubs as bps
import numpy
from ophyd import Device

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions


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
        return super().__init__(name=device.name, parent=device.parent)

    def __repr__(self):
        return f"<SnapshotDevice of {self.name} at {self.snapshot_capture_time}>"

    def capture(self, device):
        self._describe = device.describe()
        self._describe_configuration = device.describe_configuration()
        self._read = device.read()
        self._read_configuration = device.read_configuration()
        self._read_attrs = list(device.read())
        self._configuration_attrs = list(device.read_configuration())
        self._asset_docs_cache = list(device.collect_asset_docs())
        self.snapshot_capture_time = time.time()

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


class SnapshotShell:
    def __init__(self):
        self.snapshot = None

    def __getattr__(self, key):
        return getattr(self.snapshot, key)


class InsertReferenceToDarkFrame:
    """
    A plan preprocessor that ensures one 'dark' Event is created per run.

    Parameters
    ----------
    get_snapshot : callable
        Expected signature: ``f() -> SnapshotDevice``
    stream_name: string, optional
        Default is ``'dark'``
    """
    def __init__(self, get_snapshot, stream_name='dark'):
        self.get_snapshot = get_snapshot
        self.stream_name = stream_name

    def __call__(self, plan):
        print("I am Insert")

        def insert_reference_to_dark_frame(msg):
            print('insert_reference_to_dark_frame is processing', msg)
            if msg.command == 'open_run':
                snapshot = self.get_snapshot()
                return (
                    bluesky.preprocessors.pchain(
                        bluesky.preprocessors.single_gen(msg),
                        bps.stage(snapshot),
                        bps.trigger_and_read([snapshot], name='dark'),
                        bps.unstage(snapshot)
                    ),
                    None
                )
            else:
                return None, None

        return (yield from bluesky.preprocessors.plan_mutator(
            plan, insert_reference_to_dark_frame))


class TakeDarkFrames:
    """
    A plan preprocessor that inserts instructions to take a fresh dark frame.

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
        self.locked_signals = tuple(locked_signals or ())
        self._current_snapshot = SnapshotShell()
        self._locked_signals_state = ()

    def new_snapshot_needed(self):
        print('new_snapshot_needed is running')
        if self._current_snapshot.snapshot is None:
            # No snapshot yet. Must take one.
            return True
        if self.max_age > time.time() - self._current_snapshot.snapshot_capture_time:
            # Snapshot is too old.
            return True

        # Check whether any of the signals have changed since the last
        # snapshot.
        self._locked_signals_state
        current_state = []
        for signal in self.locked_signals:
            current_state.append(signal.read())
        current_state = tuple(current_state)
        if current_state != self._locked_signals_state:
            self._locked_signals_state = current_state
            return False
        else:
            return True

    def get_snapshot(self):
        return self._current_snapshot

    def __call__(self, plan):
        print("I am Take")

        def tail():
            print("TAIL TAIL TAIL TAIL TAIL")
            self._current_snapshot.snapshot = yield from self.dark_plan()
            print('Got new snapshot', self._current_snapshot)

        def insert_take_dark(msg):
            print('insert_take_dark is processing', msg)
            if (msg.command == 'open_run' and self.new_snapshot_needed()):
                print('new snapshot IS needed')
                return None, tail()
            else:
                print('new snapshot is NOT needed')
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
