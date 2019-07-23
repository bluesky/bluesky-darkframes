=====
Usage
=====

The User's Perspective
----------------------

The user executes plans with the RunEngine is the usual way, with no extra
syntax. An Event stream named 'dark' is added to every Run automatically.

Initial Configuration
---------------------

We need to know:

* How do you take a dark frame? Specifically.... What's the relevant shutter?
  How do you close it? (Some think "0" is closed; others think "1" is closed;
  still others need a multi-step dance to open or close.) What's the relevant
  detector? All of this information can be provided by writing a custom bluesky
  plan that acquires a dark frame.
* What are the rules for whether a given dark frame can be reused? Options
  include always taking a fresh dark frame; taking a fresh dark frame whenever
  the certain settings like exposure time are changed; taking a fresh dark
  frame after some expiration time; and others.
  This can be specified by configuring or, for deep customization, subclassing
  :class:`bluesky_darkframes.DarkFramePreprocessor`.

.. code-block:: python

   import bluesky_darkframes
   from ophyd.sim import img
   import bluesky.plan_stubs as bps
   import bluesky.utils
   
   def dark_plan():
       # Close shutter. Something like:
       # yield from bps.mv(...)

       # Capture a reading (but don't create an Event).
       group = bluesky.utils.short_uid('trigger')
       yield from bps.trigger(img, group=group)
       yield from bps.wait(group)

       # Open the shutter. Something like:
       # yield from bps.mv(...)

       # Create SnapshotDevice to caputre this dark frame and all associated
       # device configuration, and return that.
       snapshot = bluesky_darkframes.SnapshotDevice(img)
       return snapshot
   
   dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
       dark_plan=dark_plan,
       max_age=3)
   
   # Configure the RunEngine to apply this preprocessor.
   from bluesky import RunEngine

   RE = RunEngine()
   RE.preprocessors.append(dark_frame_preprocessor)

   # Use it. It will automatically add an Event stream named 'dark' to every
   # run.
   from bluesky.plans import count

   RE(count([img]))
