from bluesky import RunEngine
import tempfile
from event_model import RunRouter
from suitcase.msgpack import Serializer
from ophyd.sim import NumpySeqHandler
from pathlib import Path
STORAGE_DIRECTORY = tempfile.TemporaryDirectory().name
from intake_bluesky.msgpack import BlueskyMsgpackCatalog
catalog = BlueskyMsgpackCatalog(str(Path(STORAGE_DIRECTORY, '*.msgpack')),
        handler_registry={'NPY_SEQ': NumpySeqHandler})
def factory(name, doc):
    serializer = Serializer(STORAGE_DIRECTORY, flush=True)
    serializer('start', doc)
    catalog.force_reload()
    return [serializer], []

rr = RunRouter([factory])

import bluesky_darkframes
from bluesky_darkframes.sim import Shutter, DiffractionDetector
det = DiffractionDetector(name='det')
shutter = Shutter(name='shutter', value='open')
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
RE.subscribe(rr)

RE(count([det]), print)
img = catalog[-1].primary.read()['image'][0]
