from bluesky import RunEngine
from databroker import Broker
from event_model import RunRouter
from suitcase.msgpack import Serializer
from ophyd.sim import NumpySeqHandler
from pathlib import Path

import bluesky_darkframes
from bluesky_darkframes.sim import Shutter, DiffractionDetector
det = DiffractionDetector(name='det')
shutter = Shutter(name='shutter', value='open')
db = Broker.named('temp')
import bluesky.plan_stubs as bps
from bluesky.plans import count
import bluesky.utils
def dark_plan():
    yield from bps.mv(shutter, 'closed')
    group = bluesky.utils.short_uid('trigger')
    yield from bps.trigger(det, group=group)
    yield from bps.wait(group)
    yield from bps.mv(shutter, 'open')
    snapshot = bluesky_darkframes.SnapshotDevice(det)
    return snapshot

dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
    dark_plan=dark_plan, max_age=3)
RE = RunEngine()
RE.preprocessors.append(dark_frame_preprocessor)
RE.subscribe(db.insert)

RE(count([det]))
db.reg.register_handler('NPY_SEQ', NumpySeqHandler)
light = list(db[-1].data('image'))[0]
dark = list(db[-1].data('image', stream_name='dark'))[0]
