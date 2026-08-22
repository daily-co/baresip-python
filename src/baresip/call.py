#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Calls: answer, reject, hang up — with state advanced only by events.

A :class:`Call` is born from a ``CALL_INCOMING`` stack event (via
:meth:`UserAgent.on_incoming <baresip.ua.UserAgent.on_incoming>`) and
carries that event's snapshot: peer, Call-ID, allowlisted headers. Its
:attr:`~Call.state` never changes because a method was called — only
because the stack reported a transition. A method that names a call the
stack has already torn down fails with
:class:`~baresip.errors.StaleHandleError`; racing a hangup against the far
end doing the same is normal telephony, not an error to prevent.
"""

import enum
import json
import logging
import os

from baresip._native import lib
from baresip.errors import BaresipError, StaleHandleError, UnsupportedFeatureError
from baresip.events import Event, StackEvent
from baresip.runtime import Runtime

logger = logging.getLogger("baresip.call")


class CallState(enum.Enum):
    """Where a call is in its life; advanced only by stack events."""

    INCOMING = "incoming"
    ESTABLISHED = "established"
    CLOSED = "closed"


class Call:
    """One call, inbound for now.

    Constructed by the library from the ``CALL_INCOMING`` event — never by
    applications. :meth:`answer`, :meth:`reject`, and :meth:`hangup` issue
    the action; the resulting state change arrives as an event, observable
    via :meth:`on` or the :attr:`state` property.
    """

    def __init__(self, runtime: Runtime, event: StackEvent):
        """Bind to the native call a CALL_INCOMING event announced.

        Args:
            runtime: The running runtime that owns the native call.
            event: The announcing event; peer, Call-ID, and headers are
                kept from it.
        """
        self._runtime = runtime
        self._handle = event.call
        self._ua_handle = event.ua
        self._state = CallState.INCOMING
        self._listeners: list = []
        self.peer = event.peer
        self.call_id = event.call_id
        self.headers = dict(event.headers)
        runtime.subscribe(self._on_stack_event)

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
        if event.event is Event.CALL_ESTABLISHED:
            self._state = CallState.ESTABLISHED
        elif event.event is Event.CALL_CLOSED:
            self._state = CallState.CLOSED
            self._runtime.unsubscribe(self._on_stack_event)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                logger.exception("call listener raised; continuing")

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
        """Deliver this call's stack events (ESTABLISHED, RTPESTAB, DTMF,
        CLOSED, ...) to ``listener``. Same delivery contract as
        :meth:`Runtime.subscribe`."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def off(self, listener) -> None:
        """Stop delivering events to ``listener``. Unknown listeners are ignored."""
        if listener in self._listeners:
            self._listeners.remove(listener)
