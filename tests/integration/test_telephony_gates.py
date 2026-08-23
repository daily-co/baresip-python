#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Telephony correctness gates against the FreeSWITCH bench.

Four scenarios that separate "compiles and echoes" from "behaves like
telephony software": shutting down while calls are still up, staying
responsive while a Python thread hogs the GIL, re-registering after the
server restarts, and carrying a call over TLS.
Run with: pytest -m bench tests/integration
"""

import array
import asyncio
import collections
import contextlib
import logging
import math
import os
import pathlib
import struct
import subprocess
import threading
import time

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, AudioNotActive, Event
from baresip.call import CallState
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
TLS_DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_TLS_PORT', '15061')}"
CONTAINER = "baresip-bench-freeswitch"
RAW_CONF = "net_interface 127.0.0.1\naudio_source aumem,default\naudio_player aumem,default\n"
BENCH_CERT = pathlib.Path(__file__).resolve().parents[2] / "bench" / "certs" / "cert.pem"

TONE_RMS_FLOOR = 1000  # a full-scale-ish tone survives a G.711 round trip way above this


def sine(rate: int, seconds: float, freq: int = 440, amp: int = 8000) -> bytes:
    n = int(rate * seconds)
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)
    )


def peak_window_rms(pcm: bytes, rate: int, window_ms: int = 100) -> float:
    """The loudest 100 ms anywhere in the capture — alignment-free."""
    samples = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    win = max(1, rate * window_ms // 1000)
    peak = 0.0
    for start in range(0, max(1, len(samples) - win), win):
        chunk = samples[start : start + win]
        if chunk:
            peak = max(peak, math.sqrt(sum(s * s for s in chunk) / len(chunk)))
    return peak


def fs_cli(command: str) -> str:
    return subprocess.run(
        ["docker", "exec", CONTAINER, "fs_cli", "-x", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout


async def make_ua(runtime, *, domain: str = DOMAIN, **account_fields) -> UserAgent:
    account = Account(user="1003", password="bench1234", domain=domain, **account_fields)
    ua = await UserAgent.create(runtime, account)
    await ua.register()
    return ua


async def dial_ready(ua, uri: str | None = None):
    """Dial the echo service and wait until both audio directions are up."""
    call = await ua.dial(uri or f"sip:9196@{DOMAIN}")
    await call.wait_established()
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        try:
            info = call.audio.info()
            if info.tx_ready and info.rx_ready:
                return call, info
        except AudioNotActive:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("audio did not come up within 5 s")


async def wait_closed(call, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while call.state is not CallState.CLOSED and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert call.state is CallState.CLOSED


@contextlib.contextmanager
def capture_sip_trace():
    """Collect every SIP message the trace logs while the block runs.

    The trace arrives as DEBUG records on ``baresip.native.sip``; the
    logger's level is raised for the duration so they reach the handler.
    """
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    trace_logger = logging.getLogger("baresip.native.sip")
    handler = Capture(logging.DEBUG)
    old_level = trace_logger.level
    trace_logger.addHandler(handler)
    trace_logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        trace_logger.removeHandler(handler)
        trace_logger.setLevel(old_level)


def parse_sip_records(records):
    """(direction, start line, Call-ID, CSeq) for each traced SIP message."""
    parsed = []
    for rec in records:
        lines = rec.splitlines()
        if len(lines) < 2 or lines[0][:2] not in ("TX", "RX"):
            continue
        call_id = cseq = ""
        for line in lines[2:]:
            stripped = line.strip()
            if not stripped:
                break  # end of headers
            lowered = stripped.lower()
            if lowered.startswith("call-id:"):
                call_id = stripped.split(":", 1)[1].strip()
            elif lowered.startswith("cseq:"):
                cseq = stripped.split(":", 1)[1].strip()
        parsed.append((lines[0][:2], lines[1].strip(), call_id, cseq))
    return parsed


def count_retransmissions(records) -> int:
    """Messages seen more than once: same direction, start line, dialog
    and transaction. Loopback UDP does not lose packets, so any repeat
    means a peer gave up waiting — the other side was late."""
    seen = collections.Counter(parse_sip_records(records))
    return sum(n - 1 for n in seen.values() if n > 1)


def fs_channel_count() -> int:
    out = fs_cli("show channels")
    for line in out.splitlines():
        if line.endswith("total."):
            return int(line.split()[0])
    raise AssertionError(f"unrecognized `show channels` output:\n{out}")


async def wait_fs_channels(expected: int, timeout: float = 5.0) -> None:
    """Wait until FreeSWITCH reports the expected channel count.

    `show channels` reads the switch's core database, which trails the
    actual call state by a beat — so count changes are polled, never
    sampled once.
    """
    deadline = time.monotonic() + timeout
    while True:
        n = fs_channel_count()
        if n == expected:
            return
        assert time.monotonic() < deadline, (
            f"FreeSWITCH reports {n} channel(s), expected {expected}"
        )
        await asyncio.sleep(0.2)


# -- gate 1: shutdown with live calls ----------------------------------------


async def test_close_with_live_calls_sends_byes_and_returns():
    """Closing the runtime mid-call must tear the calls down properly:
    close() returns within its budget, each call's BYE actually goes out,
    and the far end sees the channels die — not time out."""
    runtime = Runtime()
    await runtime.start(RAW_CONF)
    try:
        await runtime.set_sip_trace(True)
        ua = await make_ua(runtime)
        for _ in range(2):
            call = await ua.dial(f"sip:9196@{DOMAIN}")
            await call.wait_established()
        await wait_fs_channels(2)  # both calls standing before the shutdown

        with capture_sip_trace() as records:
            start = time.monotonic()
            await runtime.close()
            elapsed = time.monotonic() - start
    finally:
        await runtime.close()  # no-op when the measured close got there
    assert elapsed < 5.0, f"close() took {elapsed:.1f} s with live calls"

    byes = {
        call_id
        for direction, start_line, call_id, _ in parse_sip_records(records)
        if direction == "TX" and start_line.startswith("BYE ")
    }
    assert len(byes) == 2, f"expected a BYE per call, traced Call-IDs: {byes}"

    # The far end acted on them: its channels are gone immediately, not
    # half a minute later when the media timeout would have reaped them.
    await wait_fs_channels(0)


# -- gate 2: GIL contention --------------------------------------------------

GIL_SECONDS = 12
RETRANSMISSION_BUDGET = 3


def busy_spin(stop: threading.Event) -> None:
    """Hold the GIL as much as the interpreter allows — a stand-in for
    in-process inference work."""
    x = 1
    while not stop.is_set():
        for _ in range(10000):
            x = (x * 31 + 7) & 0xFFFFFFFF


async def test_gil_contention_keeps_sip_and_audio_healthy():
    """Two echoing calls while a thread hogs the GIL: the SIP side must
    stay timely (no peer gives up and retransmits) and the audio side
    healthy (no warnings, no receive-side loss). The stack's own threads
    never take the GIL on those paths — this gate keeps it that way."""
    runtime = Runtime()
    await runtime.start(RAW_CONF)
    try:
        await runtime.set_sip_trace(True)
        with capture_sip_trace() as records:
            ua = await make_ua(runtime)
            pairs = [await dial_ready(ua) for _ in range(2)]
            warnings: list = []
            for call, _ in pairs:
                call.on_audio_warning(lambda w: warnings.append(w))

            stop = threading.Event()
            spinner = threading.Thread(target=busy_spin, args=(stop,), daemon=True)
            spinner.start()
            try:

                async def echo(call, info):
                    call.audio.write(sine(info.tx_sample_rate, 0.5))
                    deadline = asyncio.get_running_loop().time() + GIL_SECONDS
                    while asyncio.get_running_loop().time() < deadline:
                        pcm = call.audio.read(4096)
                        if pcm:
                            call.audio.write(pcm)
                        await asyncio.sleep(0.01)

                await asyncio.gather(*(echo(call, info) for call, info in pairs))
            finally:
                stop.set()
                spinner.join()

            stats = [call.audio.stats() for call, _ in pairs]
            for call, _ in pairs:
                assert call.state is CallState.ESTABLISHED
                await call.hangup()
            for call, _ in pairs:
                await wait_closed(call)

        assert not warnings, f"audio warned under GIL load: {[w.message for w in warnings]}"
        for s in stats:
            assert s.rx_dropped_bytes == 0, "the reader fell behind under GIL load"
        retransmissions = count_retransmissions(records)
        assert retransmissions <= RETRANSMISSION_BUDGET, (
            f"{retransmissions} SIP retransmissions under GIL load (budget {RETRANSMISSION_BUDGET})"
        )
    finally:
        await runtime.close()


# -- gate 3: re-registration after the server restarts -----------------------

REG_INTERVAL = 15


async def test_reregisters_after_server_restart():
    """Restart FreeSWITCH under a registered agent: the refresh cycle must
    re-establish the registration on its own — no application action —
    and the runtime must come through fully usable."""
    runtime = Runtime()
    await runtime.start(RAW_CONF)
    try:
        ua = await make_ua(runtime, reg_interval=REG_INTERVAL)
        loop = asyncio.get_running_loop()
        register_oks: list[float] = []
        ua.on(lambda e: register_oks.append(loop.time()) if e.event is Event.REGISTER_OK else None)

        await asyncio.to_thread(
            subprocess.run,
            ["docker", "restart", CONTAINER],
            check=True,
            capture_output=True,
            timeout=120,
        )

        def wait_bench_ready():
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                out = fs_cli("sofia status")
                if "bench" in out and "RUNNING" in out:
                    return
                time.sleep(0.5)
            raise AssertionError("FreeSWITCH did not come back after restart")

        await asyncio.to_thread(wait_bench_ready)
        ready_at = loop.time()

        # The restart wiped the server's registration state; the next
        # refresh (with its transaction-level retries riding over any
        # remaining gap) must land within the registration interval.
        deadline = loop.time() + REG_INTERVAL + 15
        while not any(t >= ready_at for t in register_oks):
            assert loop.time() < deadline, (
                f"no re-registration within {REG_INTERVAL} s of the server returning"
            )
            await asyncio.sleep(0.2)

        # Fully recovered, not just re-registered: a call works.
        call, info = await dial_ready(ua)
        call.audio.write(sine(info.tx_sample_rate, 0.3))
        await asyncio.sleep(0.5)
        await call.hangup()
        await wait_closed(call)
    finally:
        await runtime.close()


# -- gate 4: TLS -------------------------------------------------------------


async def test_tls_register_and_echo_call():
    """Register and run an echo call over transport=tls, with the server's
    certificate actually verified: the stack compiles TLS in, and this is
    the gate that keeps it working rather than merely present."""
    assert BENCH_CERT.exists(), (
        f"{BENCH_CERT} missing — `make bench-up` generates the bench TLS certificate"
    )
    # The configuration parser reads one value per line and stops at
    # whitespace, so a path with a space would be silently dropped — and
    # with no CA file configured, server verification silently turns off.
    assert " " not in str(BENCH_CERT), "bench checkout path must not contain spaces"

    runtime = Runtime()
    await runtime.start(RAW_CONF + f"sip_cafile {BENCH_CERT}\n")
    try:
        ua = await make_ua(runtime, domain=TLS_DOMAIN, transport="tls")
        call, info = await dial_ready(ua, f"sip:9196@{TLS_DOMAIN};transport=tls")

        call.audio.write(sine(info.tx_sample_rate, 0.5))
        received = b""
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            received += call.audio.read(4096)
            await asyncio.sleep(0.02)

        assert len(received) >= int(0.4 * info.rx_sample_rate) * 2
        assert peak_window_rms(received, info.rx_sample_rate) > TONE_RMS_FLOOR

        await call.hangup()
        await wait_closed(call)
        await ua.unregister()
    finally:
        await runtime.close()
