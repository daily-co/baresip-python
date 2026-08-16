#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""baresip-python: Python bindings for the baresip SIP stack.

A complete, embeddable SIP user agent for Python applications: registration,
inbound and outbound calls, programmatic PCM audio access, and DTMF — built on
baresip/libre (BSD-3) with an asyncio-native API.

The public API is exactly what ``__all__`` exports; anything else is internal
and may change without notice. See the README for the stability policy.
"""

from baresip.errors import BaresipError, CommandQueueFull

__version__ = "0.0.0.dev0"

# The public-API definition. Names are added here — and only here — as their
# implementations land.
__all__: list[str] = [
    "BaresipError",
    "CommandQueueFull",
]
