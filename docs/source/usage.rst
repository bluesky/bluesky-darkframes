=====
Usage
=====

Data Model
==========

A typical bluesky Run has an Event Stream named ``'primary'``, an Event Stream
named ``'baseline`'``, and potentially other Event Streams for signals that are
monitored asynchronously during the Run. The names of these Event Streams are
just convention, encoded by the built-in bluesky plans. Plans can define any
Event Streams that they like.

A natural way to include dark frames with a Run is to add a ``'dark'`` Event
Stream. Because Events are timestamped, the ``'dark'`` Events can be associated
with ``'primary'`` Events to produce dark-subtracted images. Each Run should
have at least one ``'dark'`` Event, and it may have more than one if a
fresh dark frame is needed mid-run. The most direct way to achieve this is to
write ``trigger_and_read(..., name='dark')`` into a custom plan:

.. code:: python

   import bluesky.plan_stubs as bps

   def count_with_darkframe(detector, md=None):
       yield from bps.stage(detector)
       yield from bps.open_run(md=md)
       yield from bps.mv(shutter, 'closed')
       yield from bps.trigger_and_read([detector], name='dark')
       yield from bps.mv(shutter, 'open')
       yield from bps.trigger_and_read([detector])  # name='primary' by default
       yield from bps.close_run()
       yield from bps.unstage(detector)

This direct solution is best one for some circumstances. However, if you find
yourself looking at the prospect of rewriting a large number of plans just to
add this dark frame logic, it may be simpler to use a bluesky *preprocessor*. A
preprocessor can augment or modify the steps in a plan. The
:class:`DarkFramePreprocessor` watches for a given detector to be triggered and
inserts steps in the plan to acquire and/or record a dark frame when needed.
Depending on how you configure it, it can reuse a given dark frame multiple
times. Thus, it will not necessarily *acquire* a dark frame for every Run, but
it will ensure that at least one 'dark' Event is *recorded* in every Run.

The preprocessor can be applied to specific plans, using Python's decorator
syntax

.. code:: python

   from bluesky.preprocessors import make_decorator

   # Do this just once.
   dark_frame_preprocessor = ... # See next section.
   do_dark_frames = make_decorator(dark_frame_preprocessor)()

   # And apply it to as many plans as you like.
   @do_dark_frames
   def my_custom_plan(...):
       ...

   @do_dark_frames
   def another_custom_plan(...):
       ...

or it can be applied to *all* plans.

.. code:: python

   # Do this just once.
   dark_frame_preprocessor = ... # See next section.
   RE.preprocessors.append(dark_frame_preprocessor)

This enables the user to use any built-in or user-defined plan and know that
dark frames will automatically be included in the logic of the plan. Note that
preprocessor will only have an effect is the detector of interest is used
during the plan.

Initial Configuration
=====================

We need to know:

#. How do you take a dark frame? Specifically.... What's the relevant shutter?
   How do you close it? (Some think "0" is closed; others think "1" is closed;
   still others need a multi-step dance to open or close.) What's the relevant
   detector?
#. What are the rules for when to take a fresh dark frame and when to reuse one
   that has already been taken?
#. If you would like to compute subtracted frames on the fly, where should the
   results go?

To address (1) define a bluesky plan that closes the shutter, takes an
acquistion, and reopens the shutter. The last two lines in this example use a
special mechanism, :class:`bluesky_darkframes.SnapshotDevice`, to stash the
acquisition where it can potentially be reused. (Later on we'll set the rules
for whether/how dark frames can be reused.)

.. jupyter-execute::

   import bluesky.plan_stubs as bps
   import bluesky_darkframes

   # This is some simulated hardware for demo purposes.
   from bluesky_darkframes.sim import Shutter, DiffractionDetector
   det = DiffractionDetector(name='det')
   shutter = Shutter(name='shutter', value='open')

   def dark_plan(detector):
       # Restage to ensure that dark frames goes into a separate file.
       yield from bps.unstage(detector)
       yield from bps.stage(detector)
       yield from bps.mv(shutter, 'closed')
       # The `group` parameter passed to trigger MUST start with
       # bluesky-darkframes-trigger.
       yield from bps.trigger(detector, group='bluesky-darkframes-trigger')
       yield from bps.wait('bluesky-darkframes-trigger')
       snapshot = bluesky_darkframes.SnapshotDevice(detector)
       yield from bps.mv(shutter, 'open')
       # Restage.
       yield from bps.unstage(detector)
       yield from bps.stage(detector)
       return snapshot

This is boilerplate bluesky and databroker setup not specificially related to
dark-frames.

.. jupyter-execute::

   from bluesky import RunEngine
   from databroker import Broker
   from ophyd.sim import NumpySeqHandler

   db = Broker.named('temp')
   db.reg.register_handler('NPY_SEQ', NumpySeqHandler)
   RE = RunEngine()
   RE.subscribe(db.insert);

Here we set the rules for when to take fresh dark frames, (2). Examples:

.. code:: python

   # Always take a fresh dark frame at the beginning of each run.
   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan, detector=det, max_age=0)

   # Take a dark frame if the last one we took is more than 30 seconds old.
   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan, detector=det, max_age=30)

   # Take a fresh dark frame if the last one we took *with this exposure time*
   # is more than 30 seconds old.
   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan, detector=det, max_age=30,
       locked_signals=[det.exposure_time])

   # Always take a new dark frame if the exposure time was changed from the
   # previous run, even if we took one with this exposure time on some earlier
   # run. Also, re-take if the settings haven't changed but the last dark
   # frame is older than 30 seconds.
   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan, detector=det, max_age=30,
       locked_signals=[det.exposure_time], limit=1)

We'll pick one example and configure the RunEngine to apply it to all plans.
This means that any plan, including user-defined ones, will automatically have
dark frames included.

.. jupyter-execute::

   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan, detector=det, max_age=30)
   RE.preprocessors.append(dark_frame_preprocessor)

Acquire and Access Data
=======================

Let's take some data.

.. jupyter-execute::

   from bluesky.plans import count

   RE(count([det]))

And now let's access the data and plot the raw "light" frame, the dark frame,
and the difference between the two.

.. jupyter-execute::

   import matplotlib.pyplot as plt

   light = list(db[-1].data('det_image'))[0]
   dark = list(db[-1].data('det_image', stream_name='dark'))[0]
   fig, axes = plt.subplots(1, 3)
   titles = ('Light', 'Dark', 'Subtracted')
   for image, ax, title in zip((light, dark, light - dark), axes, titles):
      ax.imshow(image);
      ax.set_title(title);

Export Subtracted Images
========================

In this example we'll export the data to a TIFF series, but it could equally
well be written to any other storage format.

Export saved data
-----------------

First we'll define a convenience function.

.. jupyter-execute::

   from bluesky_darkframes import DarkSubtraction
   from suitcase.tiff_series import Serializer

   def export_subtracted_tiff_series(header, *args, **kwargs):
       subtractor = DarkSubtraction('det_image')
       with Serializer(*args, **kwargs) as serializer:
           for name, doc in header.documents(fill=True):
               name, doc = subtractor(name, doc)
               serializer(name, doc)

And now apply it to the data we just took.

.. jupyter-execute::

   export_subtracted_tiff_series(db[-1], 'exported_files/')

This exports the subtracted images (with 'primary' in the name) and the dark
frames (with 'dark') in the name, which makes it possible to reconstruct the
original if desired.

.. jupyter-execute::

   !ls exported_files

To customize the file name and other output options, see
:class:`suitcase.tiff_series.Serializer`.

Export data during acquisition (streaming)
------------------------------------------

Here we use a :class:`event_model.RunRouter`.

.. jupyter-execute::

   from bluesky_darkframes import DarkSubtraction
   from event_model import RunRouter
   from suitcase.tiff_series import Serializer

   def factory(name, doc):
       # The problem this is solving is to store documents from this run long
       # enough to cross-reference them (e.g. light frames and dark frames),
       # and then tearing it down when we're done with this run.
       subtractor = DarkSubtraction('det_image')
       serializer = Serializer('live_exported_files/')

       # And by returning this function below, we are routing all other
       # documents *for this run* through here.
       def subtract_and_serialize(name, doc):
           name, doc = subtractor(name, doc)
           serializer(name, doc)

       return [subtract_and_serialize], []

   rr = RunRouter([factory], db.reg.handler_reg)
   RE.subscribe(rr);

Now take some data.

.. jupyter-execute::
   :stderr:

   RE(count([det]))

And see that files have been generated.

.. jupyter-execute::

   !ls live_exported_files
