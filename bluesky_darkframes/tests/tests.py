import bluesky_darkframes
from ophyd.sim import img
from bluesky.plans import count
import bluesky.plan_stubs
import bluesky.utils


def dark_plan():
    group = bluesky.utils.short_uid('trigger')
    yield from bluesky.plan_stubs.trigger(img, group=group)
    yield from bluesky.plan_stubs.wait(group)
    snapshot = bluesky_darkframes.SnapshotDevice(img)
    return snapshot


def test_one_dark_event_emitted(RE):
    dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
        dark_plan=dark_plan, max_age=3)
    RE.preprocessors.append(dark_frame_preprocessor)

    def verify_one_dark_frame(name, doc):
        if name == 'stop':
            doc['num_events']['dark'] == 1

    RE(count([img]), verify_one_dark_frame)
    RE(count([img], 3), verify_one_dark_frame)
