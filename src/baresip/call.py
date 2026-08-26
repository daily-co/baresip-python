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
import re
import time
from dataclasses import dataclass

from baresip._native import lib
from baresip.audio import AudioWarning, CallAudio
from baresip.errors import (
    BaresipError,
    CallFailed,
    CallTimeout,
    StaleHandleError,
    TransferFailed,
    UnsupportedFeatureError,
    split_status,
)
from baresip.events import Event, StackEvent
from baresip.runtime import Runtime
from baresip.stats import CallStats

logger = logging.getLogger("baresip.call")

_ESTABLISH_TIMEOUT = 30.0
_TRANSFER_TIMEOUT = 60.0
_DTMF_DIGITS = "0123456789ABCD*#"
_DTMF_TONE_MS = 100

# The stack's exact CALL_CLOSED text for a successful transfer (its
# spelling, one r — matched verbatim).
_TRANSFER_SUCCESS = "Call transfered"


@dataclass(frozen=True)
class TransferRequest:
    """A peer's request (a REFER) that we call somewhere else.

    Delivered to ``on_transfer_request`` callbacks and readable as
    ``call.transfer_request`` while pending. The stack has already
    202-accepted the REFER by the time this exists — the decision that
    remains is whether to *execute* it: :meth:`Call.accept_transfer`
    dials the target, :meth:`Call.reject_transfer` refuses.

    Parameters:
        target: The parsed target URI.
        raw: The complete Refer-To value, exactly as received.
        replaces: True when the target embeds a Replaces parameter —
            an attended transfer, pointing at an existing dialog.
        method: The requested method, uppercase. "INVITE" (a call) in
            all but exotic uses.
    """

    target: str
    raw: str
    replaces: bool
    method: str


def _parse_refer_to(raw: str) -> TransferRequest:
    """Split a raw Refer-To value into its decision-relevant parts.

    The value may be angle-bracketed, carry URI headers (?Replaces=...)
    inside the brackets, and address parameters (;method=...) outside
    them. Execution always uses ``raw`` — this parse only informs.
    """
    value = raw.strip()
    if value.startswith("<"):
        addr, _, params = value[1:].partition(">")
    else:
        addr, _, params = value.partition(";")
    target, _, _uri_headers = addr.partition("?")
    match = re.search(r"(?:^|;)\s*method=([^;]+)", params, re.IGNORECASE)
    method = match.group(1).strip().upper() if match else "INVITE"
    return TransferRequest(
        target=target.strip(), raw=raw, replaces="replaces=" in addr.lower(), method=method
    )


@dataclass(frozen=True)
class DigitEvent:
    """One DTMF digit the far end sent, delivered to ``on_dtmf`` callbacks.

    Parameters:
        digit: The key: ``0``-``9``, ``A``-``D``, ``*`` or ``#``.
        duration_ms: How long the key was held, measured between the
            digit's start and end reports.
    """

    digit: str
    duration_ms: int


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
        self._on_hold = False
        self._remote_on_hold = False
        self._transfer_pending = False
        self._transfer_request: TransferRequest | None = None
        self._transfer_req_listeners: list = []
        self._close_reason: str | None = None
        self._audio: CallAudio | None = None
        self._listeners: list = []
        # on_audio_warning registers a wrapper, not the callback itself;
        # this maps callback -> wrapper so off_audio_warning can find it.
        self._warning_adapters: dict = {}
        self._dtmf_listeners: list = []
        self._dtmf_pressed: tuple[str, float] | None = None
        self._rtcp_adapters: dict = {}
        self._final_stats: CallStats | None = None
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

    @property
    def is_on_hold(self) -> bool:
        """True while we hold the call (the last :meth:`hold` /
        :meth:`resume` that reached the wire)."""
        return self._on_hold

    @property
    def remote_on_hold(self) -> bool:
        """True while the far end holds us.

        Advanced by the ``CALL_HOLD`` and ``CALL_RESUME`` stack events.
        The stack detects these from the peer's SDP, with two limits:
        detection is audio-only, and only on an established call.
        """
        return self._remote_on_hold

    @property
    def final_stats(self) -> CallStats | None:
        """The call's closing statistics report; None until it closes.

        Populated from the ``CALL_CLOSED`` event, and also emitted as one
        structured log record under ``baresip.call`` — every field an
        ``extra`` attribute — so fleets get per-call quality without
        writing any code. Live snapshots during the call: :meth:`on_rtcp`.
        """
        return self._final_stats

    def _log_final_stats(self) -> None:
        stats = self._final_stats
        logger.info(
            "call finished: %ds, MOS estimate %.2f",
            stats.duration_s,
            stats.mos_estimate,
            extra={
                "call": self._handle,
                "sip_call_id": self.call_id,
                "peer": self.peer,
                "mos_estimate": stats.mos_estimate,
                **{f: getattr(stats, f) for f in stats.__dataclass_fields__},
            },
        )

    @property
    def audio(self) -> CallAudio:
        """This call's PCM streams; see :mod:`baresip.audio`.

        Always available as an object — its operations raise
        :class:`~baresip.errors.AudioNotActive` until the call's media
        is actually up.
        """
        if self._audio is None:
            self._audio = CallAudio(self._handle)
        return self._audio

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
        elif event.event is Event.CALL_HOLD:
            self._remote_on_hold = True
        elif event.event is Event.CALL_RESUME:
            self._remote_on_hold = False
        elif event.event is Event.CALL_TRANSFER and event.text:
            self._transfer_request = _parse_refer_to(event.text)
            for callback in list(self._transfer_req_listeners):
                try:
                    callback(self._transfer_request)
                except Exception:
                    logger.exception("transfer-request listener raised; continuing")
        elif event.event is Event.CALL_TRANSFER_FAILED:
            # The core gave up on the pending request (the implicit
            # subscription timed out, or an execution failed) — there
            # is nothing left to accept.
            self._transfer_request = None
        elif event.event is Event.CALL_CLOSED:
            self._state = CallState.CLOSED
            self._close_reason = event.text
            if event.stats:
                self._final_stats = CallStats._from_payload(event.stats)
                self._log_final_stats()
            self._runtime.unsubscribe(self._on_stack_event)
        elif event.event is Event.CALL_DTMF_START and event.text:
            # The start report carries the digit; the end report does
            # not — pair them here so listeners get one typed event.
            self._dtmf_pressed = (event.text, time.monotonic())
        elif event.event is Event.CALL_DTMF_END:
            pressed, self._dtmf_pressed = self._dtmf_pressed, None
            if pressed is not None:
                digit, t0 = pressed
                digit_event = DigitEvent(digit, int((time.monotonic() - t0) * 1000))
                for callback in list(self._dtmf_listeners):
                    try:
                        callback(digit_event)
                    except Exception:
                        logger.exception("dtmf listener raised; continuing")
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

    async def hold(self) -> None:
        """Put the call on hold: a re-INVITE tells the far end, and media
        pauses until :meth:`resume`.

        Returns once the re-INVITE is on the wire; the peer's answer
        arrives as events. Already-held calls return immediately. The far
        end holding *us* is :attr:`remote_on_hold`, not this.

        Raises:
            StaleHandleError: the call is already gone.
            BaresipError: the call is not established, another session
                refresh is in flight (retry shortly), or the stack
                refused.
        """
        await self._set_hold(True)

    async def resume(self) -> None:
        """Take the call off hold (the counterpart of :meth:`hold`).

        A call that is not held returns immediately.

        Raises:
            StaleHandleError: the call is already gone.
            BaresipError: the call is not established, another session
                refresh is in flight (retry shortly), or the stack
                refused.
        """
        await self._set_hold(False)

    async def _set_hold(self, hold: bool) -> None:
        verb = "hold" if hold else "resume"
        if self._on_hold == hold:
            return
        if self._state is not CallState.ESTABLISHED:
            raise BaresipError(f"{verb} requires an established call")

        # The stack flips its hold state and returns 0 even when it could
        # not send the re-INVITE (another INVITE/ACK still in flight) —
        # and then a retry would be a no-op against the already-flipped
        # flag. The re-INVITE's one observable is the CALL_LOCAL_SDP
        # "offer" event, emitted before the command completes; when it is
        # missing, flip the state back and report, so a later retry
        # starts clean.
        saw_offer = False

        def listener(event: StackEvent) -> None:
            nonlocal saw_offer
            if (
                event.call == self._handle
                and event.event is Event.CALL_LOCAL_SDP
                and event.text == "offer"
            ):
                saw_offer = True

        self._runtime.subscribe(listener)
        try:
            ev, payload = await self._runtime.cmd(
                lib.BP_CMD_CALL_HOLD, args=f"{self._handle} {int(hold)}"
            )
        finally:
            self._runtime.unsubscribe(listener)
        if ev == lib.BP_EV_STALE_HANDLE:
            raise StaleHandleError("call no longer exists")
        if payload is not None:
            errno = json.loads(payload).get("errno", 0)
            raise BaresipError(f"{verb} failed: {os.strerror(errno)}")
        if not saw_offer:
            await self._runtime.cmd(lib.BP_CMD_CALL_HOLD, args=f"{self._handle} {int(not hold)}")
            raise BaresipError(
                f"{verb} not sent: a session refresh is already in flight; retry shortly"
            )
        self._on_hold = hold

    async def transfer(self, uri: str, *, timeout: float = _TRANSFER_TIMEOUT) -> None:
        """Blind-transfer the call (REFER) and await the outcome.

        Asks the far end to call ``uri`` instead of talking to us. On
        success **this call ends**: the stack closes it once the far end
        reports its new call answered, and this method returns with the
        call CLOSED. On failure the call survives, still established,
        and the reported outcome raises.

        The call is not put on hold first — hold before transferring
        when the far end should not keep hearing media. One transfer at
        a time: the stack tracks a single REFER subscription per call.

        Args:
            uri: The SIP URI the far end should call. A bare user part
                is completed against the account's domain by the stack.
            timeout: Seconds to wait for the far end's reported outcome
                (a peer may accept the REFER and never report).

        Raises:
            ValueError: a URI that cannot travel in a request.
            TransferFailed: the far end refused or reported a failing
                outcome, the call closed without one, or no outcome
                arrived within ``timeout``.
            StaleHandleError: the call is already gone.
            BaresipError: the call is not established, a transfer is
                already in progress, or the stack refused to send.
        """
        if not uri or any(c in uri for c in " \r\n"):
            raise ValueError("uri must be non-empty, single-line, and without spaces")
        if self._state is not CallState.ESTABLISHED:
            raise BaresipError("transfer requires an established call")
        if self._transfer_pending:
            raise BaresipError("a transfer is already in progress on this call")
        await self._execute_transfer(
            f"{self._handle} {uri}", cmd=lib.BP_CMD_CALL_TRANSFER, timeout=timeout
        )

    async def attended_transfer(
        self, consult_call: "Call", *, timeout: float = _TRANSFER_TIMEOUT
    ) -> None:
        """Attended transfer: hand this call's peer over to ``consult_call``'s.

        The classic consult-then-connect: talk to the original party
        (this call), separately call the consultation target
        (``consult_call``), then splice the two together. Both legs are
        put on hold first (the convention peers expect; already-held
        legs stay as they are), then the REFER with a Replaces header
        goes out on this call. On success **both of our legs end**:
        this call closes with the transfer outcome, and the far end
        replaces the consultation dialog — that call's close arrives as
        its own events. On failure both calls survive, on hold.

        Args:
            consult_call: The established consultation call whose peer
                takes over this call's peer.
            timeout: Seconds to wait for the reported outcome.

        Raises:
            ValueError: ``consult_call`` is this very call.
            TransferFailed: the peer never advertised Replaces support,
                refused, reported a failing outcome, or none arrived in
                time.
            StaleHandleError: either call is already gone.
            BaresipError: a call is not established, a transfer is
                already in progress, or the stack refused to send.
        """
        if consult_call is self:
            raise ValueError("cannot transfer a call to itself")
        if (
            self._state is not CallState.ESTABLISHED
            or consult_call.state is not CallState.ESTABLISHED
        ):
            raise BaresipError("attended transfer requires both calls established")
        if self._transfer_pending:
            raise BaresipError("a transfer is already in progress on this call")
        await self.hold()
        await consult_call.hold()
        await self._execute_transfer(
            f"{self._handle} {consult_call.handle}",
            cmd=lib.BP_CMD_CALL_REPLACE_TRANSFER,
            timeout=timeout,
        )

    @property
    def transfer_request(self) -> TransferRequest | None:
        """The pending request that we transfer this call; None if none.

        Set when the peer's REFER arrives (the ``CALL_TRANSFER`` event),
        cleared by :meth:`accept_transfer` / :meth:`reject_transfer` or
        when the stack gives up on it (``CALL_TRANSFER_FAILED`` — the
        implicit subscription times out after about a minute if nothing
        acts).
        """
        return self._transfer_request

    def on_transfer_request(self, callback) -> None:
        """Deliver this call's transfer requests to ``callback``.

        Args:
            callback: Called with a :class:`TransferRequest`. Same
                delivery contract as :meth:`on`.
        """
        if callback not in self._transfer_req_listeners:
            self._transfer_req_listeners.append(callback)

    def off_transfer_request(self, callback) -> None:
        """Stop delivering transfer requests to ``callback``. Unknown
        callbacks are ignored."""
        if callback in self._transfer_req_listeners:
            self._transfer_req_listeners.remove(callback)

    async def accept_transfer(self) -> "Call":
        """Execute the pending transfer request: dial its target.

        Returns the new outbound call immediately (await its
        :meth:`wait_established` for the outcome). The stack ties this
        call's fate to the new one: when the new call establishes, the
        transferor is notified of success and **this call closes**; if
        the new call fails, the transferor is notified of the failure
        and this call continues.

        Raises:
            BaresipError: no request is pending, or dialing failed (the
                transferor is then told with a 500 sipfrag).
            StaleHandleError: the call is already gone.
        """
        request = self._transfer_request
        if request is None:
            raise BaresipError("no transfer request is pending on this call")
        ev, payload = await self._runtime.cmd(
            lib.BP_CMD_CALL_TRANSFER_ACCEPT, args=f"{self._handle} {request.raw}"
        )
        if ev == lib.BP_EV_STALE_HANDLE:
            raise StaleHandleError("call no longer exists")
        data = json.loads(payload)
        if "error" in data:
            errno = data.get("errno")
            detail = os.strerror(errno) if errno else data["error"]
            raise BaresipError(f"transfer accept failed: {detail}")
        self._transfer_request = None
        return Call(
            self._runtime,
            handle=data["handle"],
            ua_handle=self._ua_handle,
            state=CallState.OUTGOING,
            peer=request.target,
        )

    async def reject_transfer(self, status: int = 603) -> None:
        """Refuse the pending transfer request; the call continues.

        The REFER itself was 202-accepted by the stack before the
        request was even reported, so the refusal travels as the
        implicit subscription's final failing NOTIFY.

        Args:
            status: The SIP status the transferor is told (3xx-6xx).

        Raises:
            ValueError: a status outside 300-699.
            BaresipError: no request is pending, or the stack refused.
            StaleHandleError: the call is already gone.
        """
        if not 300 <= status <= 699:
            raise ValueError(f"status must be a 3xx-6xx SIP code, got {status}")
        if self._transfer_request is None:
            raise BaresipError("no transfer request is pending on this call")
        ev, payload = await self._runtime.cmd(
            lib.BP_CMD_CALL_TRANSFER_REJECT, args=f"{self._handle} {status}"
        )
        if ev == lib.BP_EV_STALE_HANDLE:
            raise StaleHandleError("call no longer exists")
        if payload is not None:
            errno = json.loads(payload).get("errno", 0)
            raise BaresipError(f"transfer reject failed: {os.strerror(errno)}")
        self._transfer_request = None

    async def _execute_transfer(self, args: str, *, cmd: int, timeout: float) -> None:
        # Shared by blind and attended transfer: send the REFER command,
        # then await the terminal outcome the stack reports as events.
        outcome = asyncio.get_running_loop().create_future()

        def listener(event: StackEvent) -> None:
            if event.call != self._handle or outcome.done():
                return
            if event.event is Event.CALL_TRANSFER_FAILED:
                status, reason = split_status(event.text or "")
                outcome.set_exception(
                    TransferFailed(
                        f"transfer failed: {event.text or 'no reason given'}",
                        status=status,
                        reason=reason,
                    )
                )
            elif event.event is Event.CALL_CLOSED:
                if event.text == _TRANSFER_SUCCESS:
                    outcome.set_result(None)
                else:
                    outcome.set_exception(
                        TransferFailed(
                            "call closed before the transfer completed: "
                            f"{event.text or 'no reason given'}"
                        )
                    )

        self._transfer_pending = True
        self._runtime.subscribe(listener)
        try:
            ev, payload = await self._runtime.cmd(cmd, args=args)
            if ev == lib.BP_EV_STALE_HANDLE:
                raise StaleHandleError("call no longer exists")
            if payload is not None:
                data = json.loads(payload)
                if data.get("error") == "replaces_unsupported":
                    raise TransferFailed("peer does not support the Replaces header")
                errno = data.get("errno", 0)
                raise BaresipError(f"transfer failed to send: {os.strerror(errno)}")
            try:
                await asyncio.wait_for(outcome, timeout)
            except TimeoutError:
                raise TransferFailed(f"no transfer outcome within {timeout:g} s") from None
        finally:
            self._runtime.unsubscribe(listener)
            self._transfer_pending = False

    async def send_dtmf(self, digits: str, interdigit_ms: int = 120) -> None:
        """Send DTMF digits to the far end.

        Each digit is pressed for about 100 ms and released, with
        ``interdigit_ms`` of silence before the next. How the digits
        travel — RTP telephone-events or SIP INFO — follows the
        account's :attr:`~baresip.config.Account.dtmf_mode`; this method
        returns once the last digit has been issued to the stack.

        Interop note: digits sent in the first moments after a call is
        answered can be swallowed by switches still wiring the call
        (SIP-INFO digits especially, since they traverse the switch's
        signaling path). IVR navigation is more reliable after a brief
        settle, or after the far end has started prompting.

        Args:
            digits: One or more of ``0-9 A-D * #`` (lowercase accepted).
            interdigit_ms: Milliseconds of spacing between digits.

        Raises:
            ValueError: a character is not a DTMF digit, or the spacing
                is negative.
            StaleHandleError: the call is already gone.
            BaresipError: the stack refused a digit.
        """
        digits = digits.upper()
        for digit in digits:
            if digit not in _DTMF_DIGITS:
                raise ValueError(f"{digit!r} is not a DTMF digit")
        if interdigit_ms < 0:
            raise ValueError(f"interdigit_ms must be >= 0, got {interdigit_ms}")
        for i, digit in enumerate(digits):
            await self._send_digit(digit)
            await asyncio.sleep(_DTMF_TONE_MS / 1000)
            await self._send_digit("R")  # ends the event on the wire
            if i < len(digits) - 1:
                await asyncio.sleep(interdigit_ms / 1000)

    async def _send_digit(self, key: str) -> None:
        ev, payload = await self._runtime.cmd(
            lib.BP_CMD_CALL_SEND_DIGIT, args=f"{self._handle} {key}"
        )
        if ev == lib.BP_EV_STALE_HANDLE:
            raise StaleHandleError("call no longer exists")
        if payload is not None:
            errno = json.loads(payload).get("errno", 0)
            raise BaresipError(f"sending DTMF failed: {os.strerror(errno)}")

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

    def on_audio_warning(self, callback) -> None:
        """Deliver this call's audio health warnings to ``callback``.

        The stack samples the call's audio once per second and reports
        damage done by the application's pacing, at most one warning per
        direction per second: transmit audio ran dry mid-stream (the
        application is not feeding fast enough — being silent on purpose
        never warns), or received audio was lost after the application
        had started reading (never reading at all is equally deliberate,
        and warns no more than never writing does). The counters behind
        the warnings are available any time from ``call.audio.stats()``.

        Args:
            callback: Called with an :class:`~baresip.audio.AudioWarning`.
                Same delivery contract as :meth:`on`.
        """

        def adapter(event: StackEvent) -> None:
            if event.event is Event.AUDIO_WARNING and event.text:
                direction = "tx" if event.text.startswith("transmit") else "rx"
                callback(AudioWarning(direction=direction, message=event.text))

        self._warning_adapters[callback] = adapter
        self.on(adapter)

    def off_audio_warning(self, callback) -> None:
        """Stop delivering audio warnings to ``callback``. Unknown callbacks
        are ignored."""
        adapter = self._warning_adapters.pop(callback, None)
        if adapter is not None:
            self.off(adapter)

    def on_rtcp(self, callback) -> None:
        """Deliver live media-quality snapshots for this call to ``callback``.

        One fires per RTCP report the far end sends — typically every few
        seconds — so a fleet can watch per-call quality without polling.
        The same fields arrive one last time via :attr:`final_stats`.

        Args:
            callback: Called with a :class:`~baresip.stats.CallStats`.
                Same delivery contract as :meth:`on`.
        """

        def adapter(event: StackEvent) -> None:
            if event.event is Event.CALL_RTCP and event.stats:
                callback(CallStats._from_payload(event.stats))

        self._rtcp_adapters[callback] = adapter
        self.on(adapter)

    def off_rtcp(self, callback) -> None:
        """Stop delivering RTCP snapshots to ``callback``. Unknown callbacks
        are ignored."""
        adapter = self._rtcp_adapters.pop(callback, None)
        if adapter is not None:
            self.off(adapter)

    def on_dtmf(self, callback) -> None:
        """Deliver the far end's DTMF digits to ``callback``.

        A digit is reported once, when its key is released, as a
        :class:`DigitEvent`. RTP telephone-events and SIP INFO both
        arrive here; in-band tones do not (decoding them needs the
        opt-in ``in_band_dtmf`` build).

        Args:
            callback: Called with a :class:`DigitEvent`. Same delivery
                contract as :meth:`on`.
        """
        if callback not in self._dtmf_listeners:
            self._dtmf_listeners.append(callback)

    def off_dtmf(self, callback) -> None:
        """Stop delivering digits to ``callback``. Unknown callbacks are
        ignored."""
        if callback in self._dtmf_listeners:
            self._dtmf_listeners.remove(callback)
