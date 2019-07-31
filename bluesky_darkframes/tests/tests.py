import time

import bluesky_darkframes
import bluesky_darkframes.sim
from bluesky.plans import count
from ophyd.sim import img
import pytest


import bluesky.plan_stubs as bps
import bluesky_darkframes

# This is some simulated hardware for demo purposes.
det = bluesky_darkframes.sim.DiffractionDetector(name='det')
det.exposure_time.put(0.01)
shutter = bluesky_darkframes.sim.Shutter(name='shutter', value='open')


def dark_plan():
    yield from bps.mv(shutter, 'closed')
    yield from bps.unstage(det)
    yield from bps.stage(det)
    yield from bps.trigger(det, group='darkframe-trigger')
    yield from bps.wait('darkframe-trigger')
    snapshot = bluesky_darkframes.SnapshotDevice(det)
    yield from bps.unstage(det)
    yield from bps.stage(det)
    yield from bps.mv(shutter, 'open')
    return snapshot


def test_one_dark_event_emitted(RE):
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, max_age=3)
    RE.preprocessors.append(dark_frame_preprocessor)

    def verify_one_dark_frame(name, doc):
        if name == 'stop':
            doc['num_events']['dark'] == 1

    RE(count([det]), verify_one_dark_frame)
    RE(count([det], 3), verify_one_dark_frame)


def test_max_age(RE):
    """
    Test the a dark frame is reused until it expires, and then re-taken.
    """
    # This tests an internal detail.
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, max_age=1)
    RE.preprocessors.append(dark_frame_preprocessor)
    # The first executation adds something to the cache.
    RE(count([img]))
    assert len(dark_frame_preprocessor._cache) == 1
    state, = dark_frame_preprocessor._cache
    # A second execution reuses the cache entry, adds nothing.
    RE(count([img]))
    assert len(dark_frame_preprocessor._cache) == 1
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
    # This tests an internal detail.
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, max_age=100,
        locked_signals=[det.exposure_time])
    RE.preprocessors.append(dark_frame_preprocessor)
    RE(count([img]))
    assert len(dark_frame_preprocessor._cache) == 1
    RE(bps.mv(det.exposure_time, 0.02))
    # This should take a new dark frame.
    RE(count([img]))
    assert len(dark_frame_preprocessor._cache) == 2
    # This should reuse the first one.
    RE(bps.mv(det.exposure_time, 0.01))
    RE(count([img]))
    assert len(dark_frame_preprocessor._cache) == 2


def test_limit(RE):
    """
    Test that if a 'locked signal' is changed, a new dark frame is taken, but
    if the locked signal goes back to the original value, the original dark
    frame is reused.
    """
    # This tests an internal detail.
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, max_age=100,
        locked_signals=[det.exposure_time],
        limit=1)
    RE.preprocessors.append(dark_frame_preprocessor)
    RE(count([img]))
    assert len(dark_frame_preprocessor._cache) == 1
    state, = dark_frame_preprocessor._cache
    previous_state = state
    RE(bps.mv(det.exposure_time, 0.02))
    # This should take a new dark frame and evict the last one.
    RE(count([img]))
    assert len(dark_frame_preprocessor._cache) == 1
    state, = dark_frame_preprocessor._cache
    assert state != previous_state
    previous_state = state
    # This should take a new dark frame and evict the last one.
    RE(bps.mv(det.exposure_time, 0.01))
    RE(count([img]))
    assert len(dark_frame_preprocessor._cache) == 1
    state, = dark_frame_preprocessor._cache
    assert state != previous_state
    previous_state = state
