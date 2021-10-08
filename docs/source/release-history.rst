===============
Release History
===============

v0.5.0 (2021-10-08)
------------------

Fixed
+++++
* support detectors not defining method ``collect_asset_docs``
* resolve jupyter_sphinx deprecation warning

Changed
+++++++
* add scikit-image to ``requirements-dev.txt`` to resolve a CI error

v0.4.0 (2020-03-18)
-------------------

Added
+++++
* The :class:`~bluesky_darkframes.DarkFramePreprocessor` has new methods
  :meth:`~bluesky_darkframes.DarkFramePreprocessor.disable` and
  :meth:`~bluesky_darkframes.DarkFramePreprocessor.enable` to conveniently turn
  it off and on interactively.

Fixed
+++++

* It is now possible to have multiple
  :class:`~bluesky_darkframes.DarkFramePreprocessor` instances watching the same
  detector with different ``dark_plan`` and ``stream_name`` parameters.
  Previously, these would interact badly and raise errors.
* When the same Snapshot is used multiple times, fresh unique identifiers are
  assigned to the Resource and Datum documents before they are reissued. That
  is, we never issue a document with a given unique ID more than once, even if
  the content is the same.

Changed
+++++++

* The ``dark_plan`` passed to :class:`~bluesky_darkframes.DarkFramePreprocessor`
  is now expected to accept a ``detector`` argument. Previously the
  ``dark_plan`` referred to a specific detector instance in its body, as in:

  .. code:: python

     def dark_plan():
         # Do stuff with some_detector.
         ...

     dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
          dark_plan=dark_plan, detector=some_detector)

  Now, the ``dark_plan`` can be written to accept a generic ``detector``
  argument, and  :class:`~bluesky_darkframes.DarkFramePreprocessor` will pass
  ``some_detector`` in, as in:

  .. code:: python

     def dark_plan(detector):
         # Do stuff with detector.
         ...

     dark_frame_preprocessor = bluesky_darkframes.DarkFramePreprocessor(
          dark_plan=dark_plan, detector=some_detector)

  The old signature ``dark_plan()`` is still supported for
  backward-compatbility, but a warning will be issued that this will not be
  supported in a future release.

* Version v0.2.0 introduced a ``pedestal`` parameter in
  :class:`~bluesky_darkframes.DarkSubtraction` to help avoid overflow
  wrap-around. The *documented* default value was ``100`` but the *actual*
  default in the code was ``0``. The actual default has been changed to ``100``
  to match the documentation.

v0.3.0 (2019-08-15)
-------------------

This release fixes a critical off-by-one issue in v0.2.0. All users are
recommended to upgrade.

* Associate a given :class:`~bluesky_darkframes.DarkFramePreprocessor` instance
  with a specific detector. This enables it to *only* intercede when that
  specific detector is triggered and to ignore all other acquisitions.
* Change the timing of when the conditions for a new dark frame are checked:
  the check now occurs just before the detector of interest is triggered.

v0.2.0 (2019-08-08)
-------------------

Thie release adds two features that change the default behavior:

* Check whether a new dark frame is needed and, if so, take one after each
  Event is closed (i.e. after each 'save' message) in addition to after each
  Run is opened (i.e. after each 'open_run' message).
* Support a ``pedestal`` parameter.
  :class:`~bluesky_darkframes.DarkSubtraction`, which defaults to ``100``. This
  helps avoid negative values in the subtracted image. See docstring for
  details.

v0.1.3 (2019-08-05)
-------------------

This release mostly consists of documentation and small usability improvements.

* Expose ``cache`` as a public properly.
* Raise more specific Exception types.

v0.1.2 (2019-07-31)
-------------------

* Fix critical bug in ``locked_signals`` feature and one-by-one bug in
  ``limit`` feature.

v0.1.1 (2019-07-31)
-------------------

* Critical fix to :class:`~bluesky_darkframes.DarkSubtraction`.
* Added example of streaming export of subtracted frames as TIFF.

v0.1.0 (2019-07-29)
-------------------

Initial release
