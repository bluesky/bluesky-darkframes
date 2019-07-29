=====
Usage
=====

The User's Perspective
======================

The user executes plans with the RunEngine is the usual way, with no extra
syntax. An Event stream named 'dark' is added to every Run automatically.
If desired subtracted frames can be exported to files or a database.

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

   def dark_plan():
       yield from bps.mv(shutter, 'closed')
       yield from bps.trigger(det, group='darkframe-trigger')
       yield from bps.wait('darkframe-trigger')
       yield from bps.mv(shutter, 'open')
       snapshot = bluesky_darkframes.SnapshotDevice(det)
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
   RE.subscribe(db.insert)

Here we set the rules for when to take fresh dark frames, (2). Examples:

.. code:: python

   # Always take a fresh dark frame at the beginning of each run.
   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan, max_age=0)

   # Take a dark frame if the last one we took is more than 30 seconds old.
   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan, max_age=30)

   # Take a fresh dark frame if the last one we took *with this exposure time*
   # is more than 30 seconds old.
   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan, max_age=30, locked_signals=[det.exposure_time])

   # Always take a new dark frame if the exposure time was changed from the
   # previous run, even if we took one with this exposure time on some earlier
   # run. Also, re-take if the settings haven't changed but the last dark
   # frame is older than 30 seconds.
   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan, max_age=30, locked_signals=[det.exposure_time],
       limit=1)

We'll pick one example and configure the RunEngine to apply it to all plans.
This means that any plan, including user-defined ones, will automatically have
dark frames included.

.. jupyter-execute::

   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan, max_age=30)
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

TO DO
