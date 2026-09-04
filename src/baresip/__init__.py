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

import importlib
import logging

from baresip.config import Account, Config
from baresip.errors import (
    AudioNotActive,
    AudioRestarted,
    BaresipError,
    CallBusy,
    CallDeclined,
    CallFailed,
    CallTimeout,
    CommandQueueFull,
    CommandTimeout,
    DrainingError,
    NoLocalAddressError,
    RegistrationError,
    RuntimeDead,
    StaleHandleError,
    TransferFailed,
    UnsupportedFeatureError,
    VideoNotActive,
    VideoRestarted,
)
from baresip.events import Event, StackEvent
from baresip.stats import CallStats

# Library convention: log into the "baresip" hierarchy, emit nothing unless
# the application configures handlers.
logging.getLogger("baresip").addHandler(logging.NullHandler())

__version__ = "0.4.0a1"

# Names that pull in the native extension are resolved lazily (PEP 562), so
# that `import baresip` — and with it the version lookup done by build
# tooling — works before the extension is compiled.
_NATIVE_BACKED = {
    "AudioInfo": "baresip.audio",
    "AudioStats": "baresip.audio",
    "AudioWarning": "baresip.audio",
    "Call": "baresip.call",
    "CallAudio": "baresip.audio",
    "CallState": "baresip.call",
    "DigitEvent": "baresip.call",
    "Runtime": "baresip.runtime",
    "TransferRequest": "baresip.call",
    "UserAgent": "baresip.ua",
    "CallVideo": "baresip.video",
    "VideoFrame": "baresip.video",
    "VideoInfo": "baresip.video",
}


def __getattr__(name: str):
    module_name = _NATIVE_BACKED.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


# The public-API definition. Names are added here — and only here — as their
# implementations land.
__all__: list[str] = [
    "Account",
    "AudioInfo",
    "AudioNotActive",
    "AudioRestarted",
    "AudioStats",
    "AudioWarning",
    "BaresipError",
    "Call",
    "CallAudio",
    "CallBusy",
    "CallDeclined",
    "CallFailed",
    "CallState",
    "CallStats",
    "CallTimeout",
    "CallVideo",
    "CommandQueueFull",
    "CommandTimeout",
    "Config",
    "DigitEvent",
    "DrainingError",
    "Event",
    "NoLocalAddressError",
    "RegistrationError",
    "Runtime",
    "RuntimeDead",
    "StackEvent",
    "StaleHandleError",
    "TransferFailed",
    "TransferRequest",
    "UnsupportedFeatureError",
    "UserAgent",
    "VideoFrame",
    "VideoInfo",
    "VideoNotActive",
    "VideoRestarted",
]
