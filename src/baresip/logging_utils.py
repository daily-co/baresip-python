#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Logging support for baresip-python.

The library logs into the ``baresip`` logger hierarchy (``baresip.runtime``,
``baresip.events``, ...) and never configures handlers itself beyond a
NullHandler — output is the application's decision, per standard library
convention.

Records carry correlation fields in ``extra`` where known: ``seq`` (command
sequence number), ``cmd`` (command id), ``event`` (event name), and — once
calls exist — ``call``, ``sip_call_id``, and ``peer``. A JSON formatter or
log aggregator can pick these up directly off the record.
"""

import logging


class PerCallFilter(logging.Filter):
    """Per-call DEBUG verbosity as a *filter* problem.

    Process-wide DEBUG on a busy server is unusable, but one misbehaving
    call often needs it. Set the ``baresip`` logger (or handler) to DEBUG
    and attach this filter: records at INFO and above always pass; DEBUG
    records pass only when their ``call`` or ``sip_call_id`` extra is in
    the traced set.

    Example::

        f = PerCallFilter()
        handler.addFilter(f)
        logging.getLogger("baresip").setLevel(logging.DEBUG)
        f.trace("a84b4c76e66710@10.0.0.1")   # one call goes verbose
    """

    def __init__(self):
        super().__init__()
        self._traced: set[str] = set()

    def trace(self, call_id: str) -> None:
        """Enable DEBUG records for one call (call id or SIP Call-ID)."""
        self._traced.add(call_id)

    def untrace(self, call_id: str) -> None:
        """Stop tracing a call; unknown ids are ignored."""
        self._traced.discard(call_id)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.DEBUG:
            return True
        for field in ("call", "sip_call_id"):
            value = getattr(record, field, None)
            if value is not None and value in self._traced:
                return True
        return False
