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
import re

from baresip._native import lib
from baresip.call import Call, CallState, _parse_refer_to
from baresip.config import Account
from baresip.errors import (
    BaresipError,
    DrainingError,
    NoLocalAddressError,
    RegistrationError,
    StaleHandleError,
    split_status,
)
from baresip.events import Event, StackEvent
from baresip.runtime import Runtime

logger = logging.getLogger("baresip.ua")

_REGISTER_TIMEOUT = 10.0

# regint=0 in an AOR disables registration in the stack.
_REGINT_ZERO = re.compile(r";regint=0(?:;|$)")

_TRANSFER_POLICIES = ("manual", "auto", "reject")


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
        self._registration_disabled = False
        self._transfer_policy = "manual"
        self._transfer_call_callbacks: list = []
        self._listeners: dict = {}
        self._incoming_callbacks: list = []
        self._incoming_listener = None

    def __repr__(self) -> str:
        return f"<UserAgent handle={self._handle:#x} registered={self._registered}>"

    @property
    def handle(self) -> int:
        """The native handle naming this agent (matches ``StackEvent.ua``)."""
        return self._handle

    @classmethod
    async def create(
        cls,
        runtime: Runtime,
        account: Account | str,
        *,
        transfer_policy: str = "manual",
    ) -> "UserAgent":
        """Allocate a native user agent for an account.

        Args:
            runtime: A running runtime.
            account: The account to embody, or a raw AOR string.
            transfer_policy: What happens when a peer asks a call of
                this agent to transfer (a REFER): "manual" (default)
                reports it and leaves the decision to the application —
                the toll-fraud-safe stance, since a REFER can steer a
                bot into dialing arbitrary numbers; "auto" executes
                every INVITE-method request immediately (the new calls
                arrive via :meth:`on_transfer_call`); "reject" refuses
                every request with a 603.

        Returns:
            The bound user agent. Not yet registered.

        Raises:
            ValueError: an unknown ``transfer_policy``.
            BaresipError: the stack rejected the account.
        """
        if transfer_policy not in _TRANSFER_POLICIES:
            raise ValueError(
                f"transfer_policy must be one of {_TRANSFER_POLICIES}, got {transfer_policy!r}"
            )
        aor = account.aor() if isinstance(account, Account) else account
        _, payload = await runtime.cmd(lib.BP_CMD_UA_ALLOC, args=aor)
        data = json.loads(payload)
        if "error" in data:
            errno = data.get("errno")
            detail = os.strerror(errno) if errno else data["error"]
            raise BaresipError(f"user agent allocation failed: {detail}")
        ua = cls(runtime, data["handle"])
        # The stack silently does nothing on ua_register() for such an
        # account, so register() refuses up front instead of timing out.
        # The Account field is authoritative; raw AORs are checked by text.
        if isinstance(account, Account):
            ua._registration_disabled = account.reg_interval == 0
        else:
            ua._registration_disabled = _REGINT_ZERO.search(aor) is not None
        ua._transfer_policy = transfer_policy
        if transfer_policy != "manual":
            ua._install_transfer_policy()
        return ua

    async def register(self, *, timeout: float = _REGISTER_TIMEOUT) -> None:
        """Register with the account's registrar and await the outcome.

        Args:
            timeout: Seconds to wait for the registrar's answer.

        Raises:
            RegistrationError: rejected (``status`` carries the SIP code,
                e.g. 401), transport failure, no answer within timeout —
                or immediately, if the account disables registration.
            StaleHandleError: the native agent no longer exists.
        """
        if self._registration_disabled:
            raise RegistrationError("account has registration disabled (reg_interval=0)")
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
            RegistrationError: the registrar rejected the unregister,
                never answered, or the account disables registration.
            StaleHandleError: the native agent no longer exists.
        """
        if self._registration_disabled:
            raise RegistrationError("account has registration disabled (reg_interval=0)")
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

    async def dial(self, uri: str, headers: dict | None = None, *, video: bool = False) -> Call:
        """Start an outbound call.

        Returns as soon as the INVITE is on its way — await
        :meth:`Call.wait_established <baresip.call.Call.wait_established>`
        on the returned call for the outcome; ringing and progress arrive
        as events on it.

        A 401/407 challenge on the INVITE is answered automatically with
        the account's credentials, same as registration — no application
        involvement.

        Args:
            uri: The SIP URI to call (e.g. ``"sip:9196@example.com"``).
            headers: Extra headers for the INVITE, name to value.
            video: Offer video. Inert until video support ships.

        Returns:
            The call, in :attr:`~baresip.call.CallState.OUTGOING` state.

        Raises:
            ValueError: a URI or header that cannot travel in a request.
            StaleHandleError: the native agent no longer exists.
            NoLocalAddressError: no local interface can reach the target
                (loopback targets need ``net_interface`` pinned).
            DrainingError: the runtime is draining.
            BaresipError: the stack refused to dial.
        """
        if not uri or any(c in uri for c in "\r\n"):
            raise ValueError("uri must be non-empty and single-line")
        if self._runtime.draining:
            raise DrainingError("runtime is draining; new calls are refused")
        args = f"{self._handle} {int(video)} {uri}"
        for name, value in (headers or {}).items():
            if not name or any(c in name for c in "\r\n: "):
                raise ValueError(f"invalid header name {name!r}")
            if any(c in str(value) for c in "\r\n"):
                raise ValueError(f"header {name}: value must be single-line")
            args += f"\n{name}: {value}"
        ev, payload = await self._runtime.cmd(lib.BP_CMD_UA_CONNECT, args=args)
        if ev == lib.BP_EV_STALE_HANDLE:
            raise StaleHandleError("user agent no longer exists")
        data = json.loads(payload)
        if data.get("error") == "no_laddr":
            host = data.get("host") or uri
            raise NoLocalAddressError(
                f"no local address toward {host}: no INVITE was sent. Loopback "
                'targets need "net_interface 127.0.0.1" in the runtime '
                "configuration (see examples/06_softphone.py)."
            )
        if "error" in data:
            errno = data.get("errno")
            detail = os.strerror(errno) if errno else data["error"]
            raise BaresipError(f"dial failed: {detail}")
        return Call(
            self._runtime,
            handle=data["handle"],
            ua_handle=self._handle,
            state=CallState.OUTGOING,
            peer=uri,
        )

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
                status, reason = split_status(event.text or "")
                future.set_exception(
                    RegistrationError(
                        f"{failure}: {event.text or 'no reason given'}",
                        status=status,
                        reason=reason,
                    )
                )

        return future, listener

    def on_transfer_call(self, callback) -> None:
        """Invoke ``callback(call)`` for calls the "auto" transfer policy dials.

        When the policy executes a peer's transfer request, the
        replacement call is outbound and brand new — this is how the
        application gets hold of it. (With the "manual" policy the new
        call is simply :meth:`Call.accept_transfer`'s return value.)
        """
        if callback not in self._transfer_call_callbacks:
            self._transfer_call_callbacks.append(callback)

    def off_transfer_call(self, callback) -> None:
        """Stop delivering policy-dialed calls to ``callback``. Unknown
        callbacks are ignored."""
        if callback in self._transfer_call_callbacks:
            self._transfer_call_callbacks.remove(callback)

    def _install_transfer_policy(self) -> None:
        def listener(event: StackEvent) -> None:
            if event.event is not Event.CALL_TRANSFER or event.ua != self._handle:
                return
            asyncio.get_running_loop().create_task(self._apply_transfer_policy(event))

        self._runtime.subscribe(listener)

    async def _apply_transfer_policy(self, event: StackEvent) -> None:
        try:
            if self._transfer_policy == "reject" or not event.text:
                await self._runtime.cmd(lib.BP_CMD_CALL_TRANSFER_REJECT, args=f"{event.call} 603")
                logger.info("transfer request refused by policy", extra={"call": event.call})
                return
            request = _parse_refer_to(event.text)
            if request.method != "INVITE":
                # The policy only auto-dials calls; anything more exotic
                # is refused rather than guessed at.
                await self._runtime.cmd(lib.BP_CMD_CALL_TRANSFER_REJECT, args=f"{event.call} 501")
                return
            ev, payload = await self._runtime.cmd(
                lib.BP_CMD_CALL_TRANSFER_ACCEPT, args=f"{event.call} {request.raw}"
            )
            data = json.loads(payload) if payload else {}
            if ev == lib.BP_EV_STALE_HANDLE or "error" in data:
                logger.warning(
                    "transfer policy could not execute the request: %s",
                    data.get("error", "call gone"),
                    extra={"call": event.call},
                )
                return
            new_call = Call(
                self._runtime,
                handle=data["handle"],
                ua_handle=self._handle,
                state=CallState.OUTGOING,
                peer=request.target,
            )
            logger.info("transfer request executed by policy", extra={"call": event.call})
            for callback in list(self._transfer_call_callbacks):
                try:
                    callback(new_call)
                except Exception:
                    logger.exception("on_transfer_call callback raised; continuing")
        except BaresipError as exc:
            logger.warning("transfer policy action failed: %s", exc, extra={"ua": self._handle})

    def on_incoming(self, callback) -> None:
        """Invoke ``callback(call)`` for each new inbound call to this agent.

        The :class:`~baresip.call.Call` arrives in
        :attr:`~baresip.call.CallState.INCOMING` state carrying the
        caller's URI, Call-ID, and any allowlisted headers from the
        INVITE; the callback decides to ``answer()`` or ``reject()``.
        Exceptions in one callback do not starve the others.
        """
        if callback in self._incoming_callbacks:
            return
        self._incoming_callbacks.append(callback)
        if self._incoming_listener is None:

            def listener(event: StackEvent) -> None:
                if event.event is not Event.CALL_INCOMING or event.ua != self._handle:
                    return
                call = Call._from_incoming(self._runtime, event)
                for cb in list(self._incoming_callbacks):
                    try:
                        cb(call)
                    except Exception:
                        logger.exception("on_incoming callback raised; continuing")

            self._incoming_listener = listener
            self._runtime.subscribe(listener)

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
