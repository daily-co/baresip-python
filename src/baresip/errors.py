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


class DrainingError(BaresipError):
    """The runtime is draining; new calls are refused.

    Raised by ``dial()`` after :meth:`Runtime.drain
    <baresip.runtime.Runtime.drain>` has been called. Inbound calls are
    refused with 486 by the stack itself.
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


class NoLocalAddressError(BaresipError):
    """No local interface can reach the dial target.

    The stack's interface discovery found no source address toward the
    target host, so no INVITE was sent. The classic case is a loopback
    target: discovery skips loopback interfaces unless the configuration
    pins one — put ``net_interface 127.0.0.1`` in the runtime config
    (the softphone example shows the pattern).
    """


def split_status(text: str) -> tuple[int | None, str]:
    """Split a status line like ``"486 Busy Here"`` into (486, "Busy Here").

    Transport-level failures carry plain error text with no status code;
    those come back as (None, text).
    """
    head, _, tail = text.partition(" ")
    if len(head) == 3 and head.isdigit():
        return int(head), tail
    return None, text


class CallFailed(BaresipError):
    """An outbound call ended without ever being established.

    Busy, declined, and timeout are ordinary telephony outcomes, not
    program errors — they arrive as the :class:`CallBusy`,
    :class:`CallDeclined`, and :class:`CallTimeout` subclasses so callers
    can handle each without string matching. Anything else (a 404, a
    transport failure) raises this base class directly.
    """

    def __init__(self, message: str, *, status: int | None = None, reason: str = ""):
        """Initialize the error.

        Args:
            message: Full human-readable description.
            status: SIP status code when the far end answered with one;
                None for transport-level failures and timeouts.
            reason: The reason phrase, or the transport error text.
        """
        super().__init__(message)
        self.status = status
        self.reason = reason

    @classmethod
    def from_close_reason(cls, text: str) -> "CallFailed":
        """The right exception for a CALL_CLOSED reason text."""
        if text == "Call transfered":  # the stack's exact transfer-success text
            return cls("call ended: transferred to another party", reason=text)
        status, reason = split_status(text)
        if status == 486:
            return CallBusy(f"call failed: {text}", status=status, reason=reason)
        if status == 603:
            return CallDeclined(f"call failed: {text}", status=status, reason=reason)
        return cls(f"call failed: {text or 'no reason given'}", status=status, reason=reason)


class CallBusy(CallFailed):
    """The far end is busy (486)."""


class CallDeclined(CallFailed):
    """The far end declined the call (603)."""


class CallTimeout(CallFailed):
    """The call was not established within the caller's timeout.

    The pending call is hung up (best effort) before this is raised.
    """


class TransferFailed(BaresipError):
    """A call transfer did not complete.

    The far end refused the REFER, reported a failing outcome (a NOTIFY
    whose sipfrag carries a 3xx-6xx status), or the call closed before
    any outcome arrived. The call itself survives a failed transfer.
    """

    def __init__(self, message: str, *, status: int | None = None, reason: str = ""):
        """Initialize the error.

        Args:
            message: Full human-readable description.
            status: The reported SIP status (e.g. 486) when one exists;
                None for timeouts and transport-level failures.
            reason: The reported reason phrase, or the error text.
        """
        super().__init__(message)
        self.status = status
        self.reason = reason


class AudioNotActive(BaresipError):
    """The call has no audio to exchange.

    Raised by :class:`~baresip.audio.CallAudio` operations before the
    call's media has started, after the call has closed, or once the
    runtime is down.
    """


class AudioRestarted(BaresipError):
    """The call's audio streams were replaced mid-call.

    A re-INVITE renegotiated the media (possibly with a new sample rate
    or channel count), so the streams Python was talking to are gone.
    Audio buffered across the swap is lost by design; the next
    read/write binds to the new streams — probe
    :meth:`~baresip.audio.CallAudio.info` for their parameters.
    """
