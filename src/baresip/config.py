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
import uuid
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
            registration entirely: the account dials directly and
            ``register()`` refuses instead of pretending.
        transport: SIP transport toward the domain.
        audio_codecs: Codec preference order, by stack codec name. An empty
            tuple offers every loaded codec. A bare name implies 8 kHz —
            codecs registered at other rates need the full
            ``name/srate/channels`` spec (``"g722/16000/1"``,
            ``"opus/48000/2"``); a spec that matches nothing is skipped
            with a native warning.
        dtmf_mode: How DTMF is sent: RTP telephone-events ("rtpevent"),
            SIP INFO ("info"), or per-call automatic selection ("auto").
        auth_user: Digest-authentication username, for services whose
            credential store keys it differently from ``user``
            (credential-list trunks, for example). None authenticates as
            ``user``. Travels as a bare parameter, so it cannot contain
            spaces, quotes, backslashes, semicolons, or angle brackets.
    """

    user: str
    domain: str
    password: str
    registrar: str | None = None
    reg_interval: int = 600
    transport: Literal["udp", "tcp", "tls"] = "udp"
    audio_codecs: tuple[str, ...] = ("pcmu", "pcma")
    dtmf_mode: Literal["rtpevent", "info", "auto"] = "rtpevent"
    auth_user: str | None = None

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
        if self.auth_user is not None:
            if not self.auth_user:
                raise ValueError("auth_user must not be empty; use None to authenticate as user")
            _reject_chars("auth_user", self.auth_user, ';<>"\\')

    def aor(self) -> str:
        """The address-of-record line the stack's account parser consumes.

        Contains the real password; never log it — log the account itself,
        whose ``repr()`` is redacted. The answer mode is pinned to manual:
        calls are answered by explicit API call, never by stack policy.
        """
        params = []
        if self.password:
            params.append(f'auth_pass="{self.password}"')
        if self.auth_user is not None:
            params.append(f"auth_user={self.auth_user}")
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
            f"audio_codecs={self.audio_codecs!r}, dtmf_mode={self.dtmf_mode!r}, "
            f"auth_user={self.auth_user!r})"
        )


@dataclass
class Config:
    """Stack-wide settings, applied when the runtime starts.

    The fields are declarative: the runtime reads them and wires each one
    to the mechanism that implements it — configuration text, the initial
    native log level, the SIP trace switch.

    Parameters:
        audio_driver: Audio driver as "module" or "module,device"; used
            for both capture and playback unless a direction is overridden
            below. What "device" means is the module's own affair: a sound
            card for hardware drivers, a WAV path for ``aufile``, a tone
            frequency for ``ausine``; ``aumem`` (the default) ignores it.
        audio_source: Capture-side override, same format. What the call
            transmits comes from this driver — e.g.
            ``"aufile,/path/greeting.wav"`` plays a file at the caller
            (the file's end is reported as an ``END_OF_FILE`` call event;
            the call stays up). None uses ``audio_driver``.
        audio_player: Playback-side override, same format. What the call
            receives goes to this driver — e.g. ``"aufile,/path/rec.wav"``
            records the caller. None uses ``audio_driver``. Received
            audio remains readable via ``call.audio`` under any player.
        expose_headers: Allowlist of SIP header names (an X-/P- custom
            header, typically) whose values event payloads carry when the
            triggering message has them. At most 16 names, each under 64
            characters.
        native_log_level: Lowest severity captured from the native stack,
            active from its first line: "debug", "info", "warning" or
            "error".
        sip_trace: Log every SIP message sent and received, under
            ``baresip.native.sip`` at DEBUG level.
        max_concurrent_calls: Maximum simultaneous calls; a further
            inbound INVITE is answered 486, and every live call (either
            direction) counts toward the limit. Defaults to 2 — one
            conversation plus a consultation leg, the warm-transfer
            shape. None means unlimited. The stack's own compiled
            default is 4 — it applies only when the runtime is started
            from raw configuration text that leaves ``call_max_calls``
            unset.
        instance_id: A UUID (canonical lowercase form) identifying this
            endpoint independent of its network address. Carried on
            registration Contacts as ``+sip.instance="<urn:uuid:...>"``
            (RFC 5626/3840): a registrar that supports it replaces a
            restarted instance's old binding instead of stacking a stale
            one, and the ``gruu`` extension is advertised. The
            application owns the value — supply the same one across
            restarts for a stable identity; the library never invents
            it. None (the default) sends no instance parameter.
        rtp_timeout: Seconds without received RTP after which a call is
            declared dead and closed (close reason ``"rtp stream
            error"``). RTP normally flows continuously — even silence is
            packetized — so this catches peers that vanish without a BYE:
            a crashed device, a network partition, an expired NAT
            binding. Direction-aware: a stream that is not receiving by
            negotiation (held, send-only) is not checked. 0 (the
            default) disables detection.
        video_source: Camera-side override as "module" or
            "module,device": ``"avcapture"`` on macOS or ``"v4l2"`` on
            Linux captures a real camera (device: a camera index/name
            for avcapture, a ``/dev/videoN`` path for v4l2). None uses
            ``vidmem``, the programmatic driver behind
            ``call.video.write_frame()`` — under a camera source,
            ``write_frame()`` has no effect while ``read_frame()``
            still taps received video.
        video_size: Video geometry as ``(width, height)``, for both
            directions: transmitted frames must be exactly this size,
            and received frames beyond it are dropped (and counted).
            Applies only to calls made or answered with ``video=True``.
        video_fps: Transmit frame pacing in frames per second.
        video_bitrate: VP8 encoder target, in bits per second.
    """

    audio_driver: str = "aumem"
    audio_source: str | None = None
    audio_player: str | None = None
    expose_headers: tuple[str, ...] = ()
    native_log_level: str = "warning"
    sip_trace: bool = False
    max_concurrent_calls: int | None = 2
    instance_id: str | None = None
    rtp_timeout: int = 0
    video_source: str | None = None
    video_size: tuple[int, int] = (640, 480)
    video_fps: float = 30.0
    video_bitrate: int = 1_000_000

    def __post_init__(self):
        if not self.audio_driver:
            raise ValueError("audio_driver must not be empty")
        for field in ("audio_driver", "audio_source", "audio_player", "video_source"):
            value = getattr(self, field)
            if value is None:
                continue
            if not value:
                raise ValueError(f"{field} must not be empty; use None for the default")
            # The configuration parser reads one value per line and stops
            # at whitespace, so neither can be smuggled into a value.
            _reject_chars(field, value, '"')
            # The stack stores these in fixed buffers (16-byte module,
            # 128-byte device) and silently truncates overflow — a long
            # WAV path would quietly become a different, nonexistent one.
            module, _, device = value.partition(",")
            if len(module.encode()) > 15:
                raise ValueError(f"{field} module name exceeds the stack's 15-byte limit")
            if len(device.encode()) > 127:
                raise ValueError(f"{field} device exceeds the stack's 127-byte limit")
        # The limits mirror the native allowlist's fixed capacity.
        if len(self.expose_headers) > 16:
            raise ValueError("expose_headers allows at most 16 names")
        for header in self.expose_headers:
            if not _HEADER_NAME.fullmatch(header) or len(header) > 63:
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
        if self.instance_id is not None:
            if not isinstance(self.instance_id, str):
                raise TypeError(f"instance_id must be a str, got {self.instance_id!r}")
            try:
                canonical = str(uuid.UUID(self.instance_id))
            except ValueError:
                canonical = None
            if canonical != self.instance_id:
                raise ValueError(
                    f"instance_id must be a canonical lowercase UUID "
                    f"(e.g. {uuid.uuid4()}), got {self.instance_id!r}"
                )
        if isinstance(self.rtp_timeout, bool) or not isinstance(self.rtp_timeout, int):
            raise TypeError(f"rtp_timeout must be an int, got {self.rtp_timeout!r}")
        if self.rtp_timeout < 0:
            raise ValueError(f"rtp_timeout must be >= 0, got {self.rtp_timeout}")
        if (
            len(self.video_size) != 2
            or not all(isinstance(v, int) and v > 0 for v in self.video_size)
            or any(isinstance(v, bool) for v in self.video_size)
        ):
            raise ValueError(f"video_size must be two positive ints, got {self.video_size!r}")
        if not self.video_fps > 0:
            raise ValueError(f"video_fps must be positive, got {self.video_fps!r}")
        if isinstance(self.video_bitrate, bool) or not isinstance(self.video_bitrate, int):
            raise TypeError(f"video_bitrate must be an int, got {self.video_bitrate!r}")
        if self.video_bitrate < 1:
            raise ValueError(f"video_bitrate must be >= 1, got {self.video_bitrate}")

    def render(self) -> str:
        """The configuration text handed to the stack's parser."""

        # The parser's audio values are strictly "module,device" — a bare
        # module name fails its regex and the line is silently ignored,
        # leaving whatever driver happens to be registered first. Supply
        # the conventional device name for module-only drivers ("default"
        # is what device-picking modules treat as their default anyway;
        # deviceless ones like aumem ignore it).
        def norm(driver: str) -> str:
            return driver if "," in driver else f"{driver},default"

        source = norm(self.audio_source or self.audio_driver)
        player = norm(self.audio_player or self.audio_driver)
        # Always written: the stack's compiled default is 4, so leaving
        # the key unset would silently cap concurrency.
        limit = self.max_concurrent_calls or 0  # 0 = unlimited
        w, h = self.video_size
        # Only when enabled: the stack's compiled default is already
        # 0 (detection off), so an absent key changes nothing.
        timeout = f"rtp_timeout {self.rtp_timeout}\n" if self.rtp_timeout else ""
        return (
            f"audio_source {source}\n"
            f"audio_player {player}\n"
            f"call_max_calls {limit}\n"
            f"{timeout}"
            # Video is inert until a call is made or answered with
            # video=True; the vidmem driver carries frames to and from
            # Python (call.video) on such calls.
            f"video_source {norm(self.video_source or 'vidmem')}\n"
            "video_display vidmem,default\n"
            f'video_size "{w}x{h}"\n'
            f"video_fps {self.video_fps:g}\n"
            f"video_bitrate {self.video_bitrate}\n"
        )
