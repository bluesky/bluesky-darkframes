===============
Release History
===============

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
