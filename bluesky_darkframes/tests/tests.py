import time
import os

import bluesky.plan_stubs as bps
import bluesky_darkframes
import bluesky_darkframes.sim
from bluesky.plans import count
from event_model import RunRouter
from ophyd.sim import NumpySeqHandler
import pytest
from suitcase.tiff_series import Serializer


# This is some simulated hardware for demo purposes.
det = bluesky_darkframes.sim.DiffractionDetector(name='det')
det.exposure_time.put(0.01)
shutter = bluesky_darkframes.sim.Shutter(name='shutter', value='open')


def dark_plan(detector):
    yield from bps.unstage(detector)
    yield from bps.mv(shutter, 'closed')
    yield from bps.stage(detector)
    yield from bps.trigger(detector, group='bluesky-darkframes-trigger')
    yield from bps.wait('darkframe-trigger')
    snapshot = bluesky_darkframes.SnapshotDevice(detector)
    yield from bps.unstage(detector)
    yield from bps.mv(shutter, 'open')
    yield from bps.stage(detector)
    return snapshot


def test_one_dark_event_emitted(RE):
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=3)
    RE.preprocessors.append(dark_frame_preprocessor)

    def verify_one_dark_frame(name, doc):
        if name == 'stop':
            assert doc['num_events']['dark'] == 1

    RE(count([det]), verify_one_dark_frame)
    RE(count([det], 3), verify_one_dark_frame)


def test_disable(RE):
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=3)
    RE.preprocessors.append(dark_frame_preprocessor)
    dark_frame_preprocessor.disable()

    def verify_no_dark_stream(name, doc):
        if name == 'stop':
            assert 'dark' not in doc['num_events']

    RE(count([det]), verify_no_dark_stream)


def test_mid_scan_dark_frames(RE):
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=0)
    RE.preprocessors.append(dark_frame_preprocessor)

    def verify_three_dark_frames(name, doc):
        if name == 'stop':
            assert doc['num_events']['dark'] == 3

    RE(count([det], 3), verify_three_dark_frames)


def test_max_age(RE):
    """
    Test the a dark frame is reused until it expires, and then re-taken.
    """
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=1)
    RE.preprocessors.append(dark_frame_preprocessor)
    # The first executation adds something to the cache.
    RE(count([det]))
    assert len(dark_frame_preprocessor.cache) == 1
    state, = dark_frame_preprocessor.cache
    # A second execution reuses the cache entry, adds nothing.
    RE(count([det]))
    assert len(dark_frame_preprocessor.cache) == 1
    dark_frame_preprocessor.get_snapshot(state)
    # Wait for it to age out.
    time.sleep(1.01)
    with pytest.raises(bluesky_darkframes.NoMatchingSnapshot):
        dark_frame_preprocessor.get_snapshot(state)


def test_locked_signals(RE):
    """
    Test that if a 'locked signal' is changed, a new dark frame is taken, but
    if the locked signal goes back to the original value, the original dark
    frame is reused.
    """
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=100,
        locked_signals=[det.exposure_time])
    RE.preprocessors.append(dark_frame_preprocessor)
    RE(count([det]))
    assert len(dark_frame_preprocessor.cache) == 1
    RE(bps.mv(det.exposure_time, 0.02))
    # This should take a new dark frame.
    RE(count([det]))
    assert len(dark_frame_preprocessor.cache) == 2
    # This should reuse the first one.
    RE(bps.mv(det.exposure_time, 0.01))
    RE(count([det]))
    assert len(dark_frame_preprocessor.cache) == 2


def test_locked_signals_event_output(RE):
    """
    Changing the locked_signals (e.g. exposure time) and then changing them
    back should cause a new dark *Event* to be emitted each time but it should
    only require actually *triggering* to get a new snapshot twice.

    That is, if we change the state of the locked_signals like A -> B -> A, we
    only need one snapshot for A and one snapshot for B, but there will be two
    Events containing the reading from A.
    """
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=100,
        locked_signals=[det.exposure_time])
    RE.preprocessors.append(dark_frame_preprocessor)

    def plan():
        yield from bps.open_run()
        yield from bps.stage(det)
        yield from bps.mv(det.exposure_time, 0.01)
        yield from bps.trigger_and_read([det])  # should prompt new dark Event
        yield from bps.trigger_and_read([det])
        yield from bps.mv(det.exposure_time, 0.02)
        yield from bps.trigger_and_read([det])  # should prompt new dark Event
        yield from bps.trigger_and_read([det])
        yield from bps.mv(det.exposure_time, 0.01)
        yield from bps.trigger_and_read([det])  # should prompt new dark Event
        yield from bps.trigger_and_read([det])
        yield from bps.trigger_and_read([det])
        yield from bps.unstage(det)
        yield from bps.close_run()

    def verify_event_count(name, doc):
        if name == 'stop':
            assert doc['num_events']['dark'] == 3
            assert doc['num_events']['primary'] == 7

    RE(plan(), verify_event_count)

    assert len(dark_frame_preprocessor.cache) == 2


def test_limit(RE):
    """
    Test that if a 'locked signal' is changed, a new dark frame is taken, but
    if the locked signal goes back to the original value, the original dark
    frame is reused.
    """
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=100,
        locked_signals=[det.exposure_time],
        limit=1)
    RE.preprocessors.append(dark_frame_preprocessor)
    RE(count([det]))
    assert len(dark_frame_preprocessor.cache) == 1
    state, = dark_frame_preprocessor.cache
    previous_state = state
    RE(bps.mv(det.exposure_time, 0.02))
    # This should take a new dark frame and evict the last one.
    RE(count([det]))
    assert len(dark_frame_preprocessor.cache) == 1
    state, = dark_frame_preprocessor.cache
    assert state != previous_state
    previous_state = state
    # This should take a new dark frame and evict the last one.
    RE(bps.mv(det.exposure_time, 0.01))
    RE(count([det]))
    assert len(dark_frame_preprocessor.cache) == 1
    state, = dark_frame_preprocessor.cache
    assert state != previous_state
    previous_state = state


def test_non_colliding_uids(RE):
    """
    Tests that when the same Snapshot is used multiple times it issues distinct
    Resource and Datum documents with non-colliding uids.
    """

    class LocalException(Exception):
        ...

    cache = set()

    def check_uniqueness(name, doc):
        if name == 'datum':
            key = (name, doc['datum_id'])
        else:
            key = (name, doc['uid'])
        if key in cache:
            raise LocalException(f"Collision {key}")
        cache.add(key)

    RE.subscribe(check_uniqueness)

    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=100)
    RE.preprocessors.append(dark_frame_preprocessor)

    RE(count([det]), check_uniqueness)
    RE(count([det]), check_uniqueness)
    RE(count([det]), check_uniqueness)


@pytest.mark.parametrize('pedestal', [None, 0, 100])
def test_streaming_export(RE, tmp_path, pedestal):
    """
    Test that DarkSubtractor generates files when subscribed to RE.
    """
    def factory(name, doc):
        # The problem this is solving is to store documents from this run long
        # enough to cross-reference them (e.g. light frames and dark frames),
        # and then tearing it down when we're done with this run.
        kwargs = {}
        if pedestal is not None:
            kwargs['pedestal'] = pedestal
        subtractor = bluesky_darkframes.DarkSubtraction('det_image', **kwargs)
        serializer = Serializer(tmp_path)

        # And by returning this function below, we are routing all other
        # documents *for this run* through here.
        def subtract_and_serialize(name, doc):
            name, doc = subtractor(name, doc)
            serializer(name, doc)

        return [subtract_and_serialize], []

    rr = RunRouter([factory], {'NPY_SEQ': NumpySeqHandler})
    RE.subscribe(rr)

    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=100)
    RE.preprocessors.append(dark_frame_preprocessor)

    RE(count([det]))
    exported_files = os.listdir(tmp_path)

    assert len(exported_files) == 2


def test_no_dark_frames(RE, tmp_path):
    """
    Test that a readable error is raised if no 'dark' frame is received.
    """
    def factory(name, doc):
        # The problem this is solving is to store documents from this run long
        # enough to cross-reference them (e.g. light frames and dark frames),
        # and then tearing it down when we're done with this run.
        subtractor = bluesky_darkframes.DarkSubtraction('det_image')
        serializer = Serializer(tmp_path)

        # And by returning this function below, we are routing all other
        # documents *for this run* through here.
        def subtract_and_serialize(name, doc):
            name, doc = subtractor(name, doc)
            serializer(name, doc)

        return [subtract_and_serialize], []

    rr = RunRouter([factory], {'NPY_SEQ': NumpySeqHandler})
    RE.subscribe(rr)

    # We intentionally 'forget' to set up a dark_frame_preprocessor for this
    # test.

    with pytest.raises(bluesky_darkframes.NoDarkFrame):
        RE(count([det]))


def test_nested_preprocessors(RE):
    N = 3
    for i in range(N):
        dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
            dark_plan=dark_plan, detector=det, max_age=0,
            stream_name=f'dark_{i}')
        RE.preprocessors.append(dark_frame_preprocessor)

    def verify_event_count(name, doc):
        if name == 'stop':
            for i in range(N):
                assert doc['num_events'][f'dark_{i}'] == 3

    RE(count([det], 3), verify_event_count)


def test_old_dark_plan_signature(RE):
    """
    In bluesky-darkfarmes < 0.4.0, we expected dark_plan to take no args.
    Now, we expect it to accept the detector as an argument.

    Check that the old usage still works, but warns.
    """

    def old_dark_plan():
        return (yield from dark_plan(det))

    with pytest.warns(UserWarning, match="dark_plan"):
        dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
            dark_plan=old_dark_plan, detector=det, max_age=3)
    RE.preprocessors.append(dark_frame_preprocessor)

    def verify_one_dark_frame(name, doc):
        if name == 'stop':
            assert doc['num_events']['dark'] == 1

    RE(count([det]), verify_one_dark_frame)
    RE(count([det], 3), verify_one_dark_frame)
