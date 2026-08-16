#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Exception types raised by baresip-python."""


class BaresipError(Exception):
    """Base class for all baresip-python errors."""


class CommandQueueFull(BaresipError):
    """The command queue to the SIP thread is full; the command was NOT sent.

    The queue is a fixed-size pipe. Overflowing it means commands are being
    issued far faster than the SIP thread can process them; the caller must
    back off and retry, and must not assume the rejected command had any
    effect.
    """
