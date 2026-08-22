#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""User agents: SIP identities that can register with a server.

A :class:`UserAgent` is the Python name for one native user agent — one
account, one registration. It is created through a running
:class:`~baresip.runtime.Runtime` and holds only an integer handle; the
native object lives on the SIP thread and is released when the runtime
closes.
"""

import asyncio
import json
import logging
import os

from baresip._native import lib
from baresip.config import Account
from baresip.errors import BaresipError, RegistrationError, StaleHandleError
from baresip.events import Event, StackEvent
from baresip.runtime import Runtime

logger = logging.getLogger("baresip.ua")

_REGISTER_TIMEOUT = 10.0


def _parse_status(text: str) -> tuple[int | None, str]:
    """Split an event text like ``"401 Unauthorized"`` into (401, "Unauthorized").

    Transport-level failures carry plain error text with no status code;
    those come back as (None, text).
    """
    head, _, tail = text.partition(" ")
    if len(head) == 3 and head.isdigit():
        return int(head), tail
    return None, text


class UserAgent:
    """One SIP identity: an account that can register and receive calls.

    Create with :meth:`create`; the constructor only binds to an existing
    native handle. Registration state changes arrive as stack events
    (:attr:`Event.REGISTER_OK` and friends) — :meth:`on` subscribes a
    listener to this agent's events specifically.

    Example::

        ua = await UserAgent.create(runtime, account)
        await ua.register()
        ...
        await ua.unregister()
    """

    def __init__(self, runtime: Runtime, handle: int):
        """Bind to an already-allocated native user agent.

        Args:
            runtime: The running runtime that owns the native object.
            handle: The native handle naming it.
        """
        self._runtime = runtime
        self._handle = handle
        self._registered = False
        self._listeners: dict = {}

    def __repr__(self) -> str:
        return f"<UserAgent handle={self._handle:#x} registered={self._registered}>"

    @property
    def handle(self) -> int:
        """The native handle naming this agent (matches ``StackEvent.ua``)."""
        return self._handle

    @classmethod
    async def create(cls, runtime: Runtime, account: Account | str) -> "UserAgent":
        """Allocate a native user agent for an account.

        Args:
            runtime: A running runtime.
            account: The account to embody, or a raw AOR string.

        Returns:
            The bound user agent. Not yet registered.

        Raises:
            BaresipError: the stack rejected the account.
        """
        aor = account.aor() if isinstance(account, Account) else account
        _, payload = await runtime.cmd(lib.BP_CMD_UA_ALLOC, args=aor)
        data = json.loads(payload)
        if "error" in data:
            errno = data.get("errno")
            detail = os.strerror(errno) if errno else data["error"]
            raise BaresipError(f"user agent allocation failed: {detail}")
        return cls(runtime, data["handle"])

    async def register(self, *, timeout: float = _REGISTER_TIMEOUT) -> None:
        """Register with the account's registrar and await the outcome.

        Args:
            timeout: Seconds to wait for the registrar's answer.

        Raises:
            RegistrationError: rejected (``status`` carries the SIP code,
                e.g. 401), transport failure, or no answer within timeout.
            StaleHandleError: the native agent no longer exists.
        """
        outcome, listener = self._registration_outcome("registration failed")
        self._runtime.subscribe(listener)
        try:
            ev, payload = await self._runtime.cmd(lib.BP_CMD_UA_REGISTER, args=str(self._handle))
            if ev == lib.BP_EV_STALE_HANDLE:
                raise StaleHandleError("user agent no longer exists")
            if payload is not None:
                errno = json.loads(payload).get("errno", 0)
                raise RegistrationError(
                    f"REGISTER could not be sent: {os.strerror(errno)}",
                    reason=os.strerror(errno),
                )
            try:
                await asyncio.wait_for(outcome, timeout)
            except TimeoutError:
                raise RegistrationError(f"no registration outcome within {timeout:g} s") from None
        except RegistrationError as exc:
            logger.error("registration failed: %s", exc, extra={"ua": self._handle})
            raise
        finally:
            self._runtime.unsubscribe(listener)
        self._registered = True
        logger.info("registered", extra={"ua": self._handle})

    async def unregister(self, *, timeout: float = _REGISTER_TIMEOUT) -> None:
        """Unregister and await the registrar's confirmation.

        A no-op if this agent never registered.

        Args:
            timeout: Seconds to wait for the confirmation.

        Raises:
            RegistrationError: the registrar rejected the unregister or
                never answered.
            StaleHandleError: the native agent no longer exists.
        """
        if not self._registered:
            return
        outcome, listener = self._registration_outcome("unregistration failed")
        self._runtime.subscribe(listener)
        try:
            ev, _ = await self._runtime.cmd(lib.BP_CMD_UA_UNREGISTER, args=str(self._handle))
            if ev == lib.BP_EV_STALE_HANDLE:
                raise StaleHandleError("user agent no longer exists")
            try:
                await asyncio.wait_for(outcome, timeout)
            except TimeoutError:
                raise RegistrationError(f"no unregistration outcome within {timeout:g} s") from None
        except RegistrationError as exc:
            logger.error("unregistration failed: %s", exc, extra={"ua": self._handle})
            raise
        finally:
            self._runtime.unsubscribe(listener)
        self._registered = False
        logger.info("unregistered", extra={"ua": self._handle})

    def _registration_outcome(self, failure: str):
        """A future resolved by this agent's next REGISTER_OK / REGISTER_FAIL.

        The un-register confirmation arrives as REGISTER_OK too — the
        expires-0 REGISTER goes through the same client — so one shape
        serves both directions.
        """
        future = asyncio.get_running_loop().create_future()

        def listener(event: StackEvent) -> None:
            if event.ua != self._handle or future.done():
                return
            if event.event is Event.REGISTER_OK:
                future.set_result(event)
            elif event.event is Event.REGISTER_FAIL:
                status, reason = _parse_status(event.text or "")
                future.set_exception(
                    RegistrationError(
                        f"{failure}: {event.text or 'no reason given'}",
                        status=status,
                        reason=reason,
                    )
                )

        return future, listener

    def on(self, listener) -> None:
        """Deliver this agent's stack events to ``listener``.

        The listener receives every :class:`StackEvent` whose ``ua`` field
        names this agent — registration life cycle now, calls under it
        later. Same delivery contract as :meth:`Runtime.subscribe`.
        """
        if listener in self._listeners:
            return

        def wrapped(event: StackEvent) -> None:
            if event.ua == self._handle:
                listener(event)

        self._listeners[listener] = wrapped
        self._runtime.subscribe(wrapped)

    def off(self, listener) -> None:
        """Stop delivering events to ``listener``. Unknown listeners are ignored."""
        wrapped = self._listeners.pop(listener, None)
        if wrapped is not None:
            self._runtime.unsubscribe(wrapped)
