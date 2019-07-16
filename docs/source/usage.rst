=====
Usage
=====

.. code-block:: python

    import bluesky_darkframes

    dark_frame_cache = bluesky_darkframes.DarkFrameCache(name='dark_frame_cache')

    def dark_plan(dark_frame_cache):
        yield from bps.mv(shutter, 0)
        yield from bps.trigger(detector, group='cam')
        yield from bps.wait('cam')
        yield from bps.mv(shutter, init_shutter_state)
        dark_frame_cache.capture(detector)
