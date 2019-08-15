import time
import os

import bluesky.plan_stubs as bps
import bluesky_darkframes
import bluesky_darkframes.sim
from bluesky.plans import count
from event_model import RunRouter, Filler
from ophyd.sim import NumpySeqHandler
import pytest
from suitcase.tiff_series import Serializer


# This is some simulated hardware for demo purposes.
det = bluesky_darkframes.sim.DiffractionDetector(name='det')
det.exposure_time.put(0.01)
shutter = bluesky_darkframes.sim.Shutter(name='shutter', value='open')


def dark_plan():
    yield from bps.mv(shutter, 'closed')
    yield from bps.trigger(det, group='darkframe-trigger')
    yield from bps.wait('darkframe-trigger')
    snapshot = bluesky_darkframes.SnapshotDevice(det)
    yield from bps.mv(shutter, 'open')
    return snapshot


def test_one_dark_event_emitted(RE):
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=3)
    RE.preprocessors.append(dark_frame_preprocessor)

    def verify_one_dark_frame(name, doc):
        if name == 'stop':
            doc['num_events']['dark'] == 1

    RE(count([det]), verify_one_dark_frame)
    RE(count([det], 3), verify_one_dark_frame)


def test_mid_scan_dark_frames(RE):
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, detector=det, max_age=0)
    RE.preprocessors.append(dark_frame_preprocessor)

    def verify_four_dark_frames(name, doc):
        if name == 'stop':
            doc['num_events']['dark'] == 4

    RE(count([det], 3), verify_four_dark_frames)


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
        filler = Filler({'NPY_SEQ': NumpySeqHandler}, inplace=False)

        # Here we push the run 'start' doc through.
        subtractor(name, doc)
        serializer(name, doc)
        filler(name, doc)

        # And by returning this function below, we are routing all other
        # documents *for this run* through here.
        def fill_subtract_and_serialize(name, doc):
            name, doc = filler(name, doc)
            name, doc = subtractor(name, doc)
            serializer(name, doc)

        return [fill_subtract_and_serialize], []

    rr = RunRouter([factory])
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
        filler = Filler({'NPY_SEQ': NumpySeqHandler}, inplace=False)

        # Here we push the run 'start' doc through.
        subtractor(name, doc)
        serializer(name, doc)
        filler(name, doc)

        # And by returning this function below, we are routing all other
        # documents *for this run* through here.
        def fill_subtract_and_serialize(name, doc):
            name, doc = filler(name, doc)
            name, doc = subtractor(name, doc)
            serializer(name, doc)

        return [fill_subtract_and_serialize], []

    rr = RunRouter([factory])
    RE.subscribe(rr)

    # We intentionally 'forget' to set up a dark_frame_preprocessor for this
    # test.

    with pytest.raises(bluesky_darkframes.NoDarkFrame):
        RE(count([det]))
