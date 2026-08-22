#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Calls: dial, answer, reject, hang up — with state advanced only by events.

A :class:`Call` is born one of two ways: from a ``CALL_INCOMING`` stack
event (via :meth:`UserAgent.on_incoming <baresip.ua.UserAgent.on_incoming>`),
or from :meth:`UserAgent.dial <baresip.ua.UserAgent.dial>`. Either way its
:attr:`~Call.state` never changes because a method was called — only
because the stack reported a transition. A method that names a call the
stack has already torn down fails with
:class:`~baresip.errors.StaleHandleError`; racing a hangup against the far
end doing the same is normal telephony, not an error to prevent.
"""

import asyncio
import enum
import json
import logging
import os

from baresip._native import lib
from baresip.errors import (
    BaresipError,
    CallFailed,
    CallTimeout,
    StaleHandleError,
    UnsupportedFeatureError,
)
from baresip.events import Event, StackEvent
from baresip.runtime import Runtime

logger = logging.getLogger("baresip.call")

_ESTABLISH_TIMEOUT = 30.0


class CallState(enum.Enum):
    """Where a call is in its life; advanced only by stack events."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"
    ESTABLISHED = "established"
    CLOSED = "closed"


class Call:
    """One call, either direction.

    Constructed by the library — from the ``CALL_INCOMING`` event or by
    ``dial()`` — never by applications. Methods issue the action; the
    resulting state change arrives as an event, observable via :meth:`on`
    or the :attr:`state` property.
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        handle: int,
        ua_handle: int,
        state: CallState,
        peer: str | None = None,
        call_id: str | None = None,
        headers: dict | None = None,
    ):
        """Bind to a native call. Library-internal; see the class docstring.

        Args:
            runtime: The running runtime that owns the native call.
            handle: The native handle naming the call.
            ua_handle: The owning user agent's handle.
            state: The state the call is born in.
            peer: The far end's URI, when already known.
            call_id: The SIP Call-ID, when already known.
            headers: Allowlisted INVITE headers, for inbound calls.
        """
        self._runtime = runtime
        self._handle = handle
        self._ua_handle = ua_handle
        self._state = state
        self._close_reason: str | None = None
        self._listeners: list = []
        self.peer = peer
        self.call_id = call_id
        self.headers = dict(headers) if headers else {}
        runtime.subscribe(self._on_stack_event)

    @classmethod
    def _from_incoming(cls, runtime: Runtime, event: StackEvent) -> "Call":
        """The inbound birth: adopt a CALL_INCOMING event's snapshot."""
        return cls(
            runtime,
            handle=event.call,
            ua_handle=event.ua,
            state=CallState.INCOMING,
            peer=event.peer,
            call_id=event.call_id,
            headers=event.headers,
        )

    def __repr__(self) -> str:
        return f"<Call handle={self._handle:#x} state={self._state.value} peer={self.peer!r}>"

    @property
    def handle(self) -> int:
        """The native handle naming this call (matches ``StackEvent.call``)."""
        return self._handle

    @property
    def state(self) -> CallState:
        """Current state, as last reported by the stack."""
        return self._state

    def _on_stack_event(self, event: StackEvent) -> None:
        # The library's own listener: keeps state truthful and fans out to
        # this call's listeners. Runs for every stack event; filters here.
        if event.call != self._handle:
            return
        if self.call_id is None and event.call_id:
            self.call_id = event.call_id
        if self.peer is None and event.peer:
            self.peer = event.peer
        if event.event is Event.CALL_ESTABLISHED:
            self._state = CallState.ESTABLISHED
        elif event.event is Event.CALL_CLOSED:
            self._state = CallState.CLOSED
            self._close_reason = event.text
            self._runtime.unsubscribe(self._on_stack_event)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                logger.exception("call listener raised; continuing")

    async def wait_established(self, timeout: float = _ESTABLISH_TIMEOUT) -> None:
        """Wait until the call is answered and media is set up.

        Args:
            timeout: Seconds to wait. On expiry the pending call is hung
                up (best effort) before raising.

        Raises:
            CallBusy: the far end answered 486.
            CallDeclined: the far end answered 603.
            CallTimeout: not established within ``timeout``.
            CallFailed: any other terminal answer or transport failure.
        """
        if self._state is CallState.ESTABLISHED:
            return
        if self._state is CallState.CLOSED:
            raise CallFailed.from_close_reason(self._close_reason or "")

        future = asyncio.get_running_loop().create_future()

        def listener(event: StackEvent) -> None:
            if future.done():
                return
            if event.event is Event.CALL_ESTABLISHED:
                future.set_result(None)
            elif event.event is Event.CALL_CLOSED:
                future.set_exception(CallFailed.from_close_reason(event.text or ""))

        self.on(listener)
        try:
            await asyncio.wait_for(future, timeout)
        except TimeoutError:
            try:
                await self.hangup()
            except BaresipError as exc:
                logger.debug("hangup after timeout: %s", exc)  # gone already
            raise CallTimeout(f"call not established within {timeout:g} s") from None
        except CallFailed as exc:
            # Ordinary telephony outcomes, deliberately not ERROR-level.
            logger.info("outbound call failed: %s", exc, extra={"call": self._handle})
            raise
        finally:
            self.off(listener)

    async def answer(self, *, video: bool = False, headers: dict | None = None) -> None:
        """Accept the call with 200 OK.

        Establishment is confirmed by the ``CALL_ESTABLISHED`` event, not
        by this method returning.

        Args:
            video: Accept with video. Inert until video support ships.
            headers: Reserved for response headers.

        Raises:
            UnsupportedFeatureError: ``headers`` was passed; sending custom
                response headers is not supported yet.
            StaleHandleError: the call is already gone.
            BaresipError: the stack refused to answer.
        """
        if headers is not None:
            raise UnsupportedFeatureError("custom response headers are not supported yet")
        ev, payload = await self._runtime.cmd(
            lib.BP_CMD_CALL_ANSWER, args=f"{self._handle} {int(video)}"
        )
        if ev == lib.BP_EV_STALE_HANDLE:
            raise StaleHandleError("call no longer exists")
        if payload is not None:
            errno = json.loads(payload).get("errno", 0)
            raise BaresipError(f"answer failed: {os.strerror(errno)}")

    async def reject(self) -> None:
        """Decline the call with 486 Busy Here.

        Raises:
            StaleHandleError: the call is already gone.
        """
        await self._end(lib.BP_CMD_CALL_REJECT)

    async def hangup(self) -> None:
        """Hang up the call.

        Valid in any state; on a not-yet-answered call it declines.

        Raises:
            StaleHandleError: the call is already gone.
        """
        await self._end(lib.BP_CMD_CALL_HANGUP)

    async def _end(self, cmd_id: int) -> None:
        ev, _ = await self._runtime.cmd(cmd_id, args=str(self._handle))
        if ev == lib.BP_EV_STALE_HANDLE:
            raise StaleHandleError("call no longer exists")

    def on(self, listener) -> None:
        """Deliver this call's stack events (RINGING, PROGRESS, ESTABLISHED,
        RTPESTAB, DTMF, CLOSED, ...) to ``listener``. Same delivery
        contract as :meth:`Runtime.subscribe`."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def off(self, listener) -> None:
        """Stop delivering events to ``listener``. Unknown listeners are ignored."""
        if listener in self._listeners:
            self._listeners.remove(listener)
