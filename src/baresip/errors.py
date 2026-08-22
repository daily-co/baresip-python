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


class CommandTimeout(BaresipError):
    """A command was sent but no completion arrived within the timeout.

    Either the SIP thread is stalled (the runtime's watchdog will report
    that separately) or the command's completion was lost. The command may
    or may not have taken effect.
    """


class RuntimeDead(BaresipError):
    """The SIP thread has exited and the runtime can no longer operate.

    Raised on all commands pending at the time of death and on any later
    attempt to use the runtime. There is no in-process recovery: start a
    new process.
    """


class StaleHandleError(BaresipError):
    """The operation named a stack object that no longer exists.

    Handles outlive the objects they name, so this is expected in races —
    a hangup crossing the far end's own hangup, for example. Callers
    should treat it as "already gone", not as a failure.
    """


class RegistrationError(BaresipError):
    """Registration (or unregistration) failed or was never confirmed."""

    def __init__(self, message: str, *, status: int | None = None, reason: str = ""):
        """Initialize the error.

        Args:
            message: Full human-readable description.
            status: SIP status code when the registrar answered (e.g. 401);
                None for transport-level failures and timeouts.
            reason: The registrar's reason phrase, or the transport error text.
        """
        super().__init__(message)
        self.status = status
        self.reason = reason


class UnsupportedFeatureError(BaresipError):
    """The requested feature is not available in this version.

    Raised eagerly — before any request goes on the wire — when an API
    accepts a parameter whose support has not shipped yet, so the caller
    finds out immediately rather than through silent omission.
    """
