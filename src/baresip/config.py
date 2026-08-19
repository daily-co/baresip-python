#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Typed configuration for the stack and its accounts.

Both classes render themselves into the text formats the native stack
consumes — :meth:`Config.render` produces configuration text and
:meth:`Account.aor` the address-of-record line — so applications never
hand-write either format. Values are validated on construction: anything
the underlying parsers would misread is rejected with ValueError where the
mistake was made, instead of surfacing as a protocol failure later.
"""

import re
from dataclasses import dataclass
from typing import Literal

#: Native log levels, lowest to highest, by the names used in configuration.
LOG_LEVEL_NAMES = ("debug", "info", "warning", "error")

_TRANSPORTS = ("udp", "tcp", "tls")
_DTMF_MODES = ("rtpevent", "info", "auto")

# RFC 7230 token characters: what a header name may consist of.
_HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")


def _reject_chars(field: str, value: str, deny: str = "", *, allow_space: bool = False) -> None:
    """Raise ValueError if value contains a control character, a space
    (unless allowed), or any character in deny."""
    floor = 0x20 if allow_space else 0x21
    for ch in value:
        if ord(ch) < floor or ord(ch) == 0x7F or ch in deny:
            raise ValueError(f"{field} contains {ch!r}, which the stack's parser would misread")


@dataclass(repr=False)
class Account:
    """One SIP identity: who to register as, where, and how.

    :meth:`aor` renders it into the single address-of-record line the
    stack's account parser consumes. The password appears only there —
    ``repr()`` redacts it, so accounts are safe to log.

    Parameters:
        user: The user part of ``sip:user@domain``.
        domain: Registration domain. May carry a port ("example.com:5061").
        password: Authentication password. May be empty, for deployments
            that authenticate by address.
        registrar: Outbound proxy to register through, when that is not the
            domain itself: a host, host:port, or full ``sip:`` URI, which
            may carry URI parameters ("sbc.example.com;transport=tcp").
        reg_interval: Seconds between registration refreshes. 0 disables
            registration entirely (direct-call use).
        transport: SIP transport toward the domain.
        audio_codecs: Codec preference order, by stack codec name. An empty
            tuple offers every loaded codec.
        dtmf_mode: How DTMF is sent: RTP telephone-events ("rtpevent"),
            SIP INFO ("info"), or per-call automatic selection ("auto").
    """

    user: str
    domain: str
    password: str
    registrar: str | None = None
    reg_interval: int = 600
    transport: Literal["udp", "tcp", "tls"] = "udp"
    audio_codecs: tuple[str, ...] = ("pcmu", "pcma")
    dtmf_mode: Literal["rtpevent", "info", "auto"] = "rtpevent"

    def __post_init__(self):
        if not self.user:
            raise ValueError("user must not be empty")
        _reject_chars("user", self.user, '@:;<>"?')
        if not self.domain:
            raise ValueError("domain must not be empty")
        _reject_chars("domain", self.domain, '@;<>"?')
        # The password travels in a quoted parameter, so spaces and
        # semicolons are fine — but the quoting has no unescape, so a quote
        # or backslash cannot round-trip.
        _reject_chars("password", self.password, '"\\', allow_space=True)
        if self.registrar is not None:
            if not self.registrar:
                raise ValueError("registrar must not be empty; use None for no outbound proxy")
            _reject_chars("registrar", self.registrar, '"\\<>')
        if isinstance(self.reg_interval, bool) or not isinstance(self.reg_interval, int):
            raise TypeError(f"reg_interval must be an int, got {self.reg_interval!r}")
        if self.reg_interval < 0:
            raise ValueError(f"reg_interval must be >= 0, got {self.reg_interval}")
        if self.transport not in _TRANSPORTS:
            raise ValueError(f"transport must be one of {_TRANSPORTS}, got {self.transport!r}")
        for codec in self.audio_codecs:
            if not codec:
                raise ValueError("audio_codecs must not contain an empty name")
            _reject_chars("audio_codecs", codec, ',;"')
        if self.dtmf_mode not in _DTMF_MODES:
            raise ValueError(f"dtmf_mode must be one of {_DTMF_MODES}, got {self.dtmf_mode!r}")

    def aor(self) -> str:
        """The address-of-record line the stack's account parser consumes.

        Contains the real password; never log it — log the account itself,
        whose ``repr()`` is redacted. The answer mode is pinned to manual:
        calls are answered by explicit API call, never by stack policy.
        """
        params = []
        if self.password:
            params.append(f'auth_pass="{self.password}"')
        params.append(f"regint={self.reg_interval}")
        params.append("answermode=manual")
        if self.audio_codecs:
            params.append("audio_codecs=" + ",".join(self.audio_codecs))
        params.append(f"dtmfmode={self.dtmf_mode}")
        if self.registrar is not None:
            uri = self.registrar
            if not uri.startswith(("sip:", "sips:")):
                uri = f"sip:{uri}"
            params.append(f'outbound="{uri}"')
        return f"<sip:{self.user}@{self.domain};transport={self.transport}>;" + ";".join(params)

    def __repr__(self) -> str:
        password = "***" if self.password else ""
        return (
            f"Account(user={self.user!r}, domain={self.domain!r}, "
            f"password={password!r}, registrar={self.registrar!r}, "
            f"reg_interval={self.reg_interval!r}, transport={self.transport!r}, "
            f"audio_codecs={self.audio_codecs!r}, dtmf_mode={self.dtmf_mode!r})"
        )


@dataclass
class Config:
    """Stack-wide settings, applied when the runtime starts.

    The fields are declarative: the runtime reads them and wires each one
    to the mechanism that implements it — configuration text, the initial
    native log level, the SIP trace switch.

    Parameters:
        audio_driver: Audio driver as "module" or "module,device"; used for
            both capture and playback.
        expose_headers: Reserved — allowlist of SIP header names to carry
            in event payloads. Accepted and validated; events do not carry
            headers yet.
        native_log_level: Lowest severity captured from the native stack,
            active from its first line: "debug", "info", "warning" or
            "error".
        sip_trace: Log every SIP message sent and received, under
            ``baresip.native.sip`` at DEBUG level.
        max_concurrent_calls: Reserved — accepted and validated, not yet
            enforced.
    """

    audio_driver: str = "aumem"
    expose_headers: tuple[str, ...] = ()
    native_log_level: str = "warning"
    sip_trace: bool = False
    max_concurrent_calls: int | None = None

    def __post_init__(self):
        if not self.audio_driver:
            raise ValueError("audio_driver must not be empty")
        # The configuration parser reads one value per line and stops at
        # whitespace, so neither can be smuggled into a value.
        _reject_chars("audio_driver", self.audio_driver, '"')
        for header in self.expose_headers:
            if not _HEADER_NAME.fullmatch(header):
                raise ValueError(f"expose_headers entry {header!r} is not a valid header name")
        if self.native_log_level not in LOG_LEVEL_NAMES:
            raise ValueError(
                f"native_log_level must be one of {LOG_LEVEL_NAMES}, got {self.native_log_level!r}"
            )
        if self.max_concurrent_calls is not None:
            if isinstance(self.max_concurrent_calls, bool) or not isinstance(
                self.max_concurrent_calls, int
            ):
                raise TypeError(
                    f"max_concurrent_calls must be an int, got {self.max_concurrent_calls!r}"
                )
            if self.max_concurrent_calls < 1:
                raise ValueError(
                    f"max_concurrent_calls must be >= 1, got {self.max_concurrent_calls}"
                )

    def render(self) -> str:
        """The configuration text handed to the stack's parser."""
        return f"audio_source {self.audio_driver}\naudio_player {self.audio_driver}\n"
