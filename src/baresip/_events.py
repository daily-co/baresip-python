#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""The single registration point for the native event callback.

cffi allows exactly one Python function per ``extern "Python"`` name — a
second ``@ffi.def_extern()`` for the same name silently replaces the first.
To keep that from ever biting, ``bp_event_h`` is registered here and nowhere
else; consumers plug in through :func:`set_sink`.

The callback runs on the re thread. It must never raise into C and must not
keep references to the C string, so the payload is copied to ``bytes`` before
the sink sees it.
"""

import logging

from baresip._native import ffi, lib  # noqa: F401  (lib re-exported for consumers)

logger = logging.getLogger("baresip.events")

# Read by bp_event_h on the re thread, written by set_sink on any thread.
# A single attribute load/store is atomic under the GIL — which the cffi
# trampoline must hold before this module's code runs at all — so no lock
# is needed (revisit for free-threaded builds).
_sink = None


def set_sink(sink):
    """Install ``sink(ev, handle, payload: bytes | None)`` as the event
    consumer; pass None to detach. Returns the previous sink."""
    global _sink
    previous = _sink
    _sink = sink
    return previous


@ffi.def_extern()
def bp_event_h(ev, handle, json):
    sink = _sink
    payload = ffi.string(json) if json != ffi.NULL else None
    if sink is None:
        logger.debug("native event %d dropped: no sink attached", ev)
        return
    try:
        sink(ev, handle, payload)
    except Exception:
        # Raising into the C caller would be undefined behavior.
        logger.exception("event sink raised; event %d dropped", ev)
