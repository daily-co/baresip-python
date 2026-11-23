#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Media encryption against the FreeSWITCH bench.

Calls in both directions with FreeSWITCH offering no SRTP, requiring it,
or offering both, against an account with each ``media_encryption``
setting. Each test checks both ends: what the call reports, and what
FreeSWITCH says it negotiated on its own leg. Run with:

    pytest -m bench tests/integration
"""

import asyncio
import contextlib
import os
import subprocess
import uuid

import pytest

pytest.importorskip("baresip._native")

from baresip import Account, AudioRestarted, CallState, Config
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
CONTAINER = "baresip-bench-freeswitch"
# FreeSWITCH's rtp_secure_media for the leg it originates; None leaves
# the leg unencrypted, "optional" offers an SRTP section and a plain one.
PLAIN, REQUIRED, BOTH = None, "mandatory", "optional"
NO_CRYPTO = "_undef_"


async def fs_cli(command: str) -> str:
    result = await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", CONTAINER, "fs_cli", "-x", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


async def fs_crypto(channel: str) -> str:
    """The SRTP suite FreeSWITCH negotiated on a leg, or NO_CRYPTO."""
    return await fs_cli(f"uuid_getvar {channel} rtp_has_crypto")


def originate(channel: str, secure: str | None, *, background: bool = True) -> str:
    variables = f"origination_uuid={channel}"
    if secure:
        variables += f",rtp_secure_media={secure}"
    return f"{'bgapi ' if background else ''}originate {{{variables}}}user/1003 &echo()"


@pytest.fixture
async def runtime():
    rt = Runtime()
    await rt.start(Config(net_interface="127.0.0.1", max_concurrent_calls=None))
    yield rt
    await fs_cli("hupall NORMAL_CLEARING")
    await rt.close()


async def registered(runtime, media_encryption: str, user: str = "1003") -> UserAgent:
    account = Account(
        user=user, password="bench1234", domain=DOMAIN, media_encryption=media_encryption
    )
    ua = await UserAgent.create(runtime, account)
    await ua.register()
    return ua


async def audio_arrives(call) -> bool:
    """True when decoded audio keeps arriving on the call."""
    with contextlib.suppress(AudioRestarted):  # a hold renegotiates the stream
        call.audio.read(1 << 20)
    call.audio.read(1 << 20)  # drain what has queued so far
    await asyncio.sleep(0.5)
    return len(call.audio.read(1 << 20)) > 0


@pytest.mark.parametrize(
    ("setting", "offered", "encrypted"),
    [
        ("none", PLAIN, False),
        ("none", REQUIRED, True),
        ("none", BOTH, True),
        ("srtp", REQUIRED, True),
        ("srtp", BOTH, True),
    ],
)
async def test_a_received_call_follows_the_setting_and_the_offer(
    runtime, setting, offered, encrypted
):
    ua = await registered(runtime, setting)
    incoming: asyncio.Queue = asyncio.Queue()
    ua.on_incoming(incoming.put_nowait)
    channel = str(uuid.uuid4())

    await fs_cli(originate(channel, offered))
    call = await asyncio.wait_for(incoming.get(), 10)
    await call.answer()
    await call.wait_established()

    assert await audio_arrives(call)
    assert call.media_encrypted is encrypted
    assert (await fs_crypto(channel) != NO_CRYPTO) is encrypted


async def test_requiring_srtp_refuses_an_unencrypted_call(runtime):
    ua = await registered(runtime, "srtp")
    incoming: asyncio.Queue = asyncio.Queue()
    ua.on_incoming(incoming.put_nowait)

    result = await fs_cli(originate(str(uuid.uuid4()), PLAIN, background=False))

    # 488 Not Acceptable Here, as FreeSWITCH names it.
    assert "INCOMPATIBLE_DESTINATION" in result
    assert incoming.empty()


@pytest.mark.parametrize(("setting", "encrypted"), [("none", False), ("srtp", True)])
async def test_a_placed_call_is_encrypted_only_when_the_setting_says_so(
    runtime, setting, encrypted
):
    ua = await registered(runtime, setting)

    call = await ua.dial(f"sip:9196@{DOMAIN}")
    await call.wait_established()

    assert await audio_arrives(call)
    assert call.media_encrypted is encrypted
    rows = (await fs_cli("show channels")).splitlines()[1:]
    channel = next(row.split(",")[0] for row in rows if row.count(",") > 5)
    assert (await fs_crypto(channel) != NO_CRYPTO) is encrypted


async def test_an_encrypted_call_survives_hold_and_resume(runtime):
    ua = await registered(runtime, "srtp")
    call = await ua.dial(f"sip:9196@{DOMAIN}")
    await call.wait_established()
    assert await audio_arrives(call)

    await call.hold()
    await asyncio.sleep(0.5)
    await call.resume()
    await asyncio.sleep(0.5)

    assert call.media_encrypted
    assert await audio_arrives(call)


async def test_an_encrypted_call_can_be_transferred(runtime):
    # The far end takes whatever FreeSWITCH offers its leg, which is not
    # SRTP: the switch does not carry one leg's encryption to the other.
    ua_a = await registered(runtime, "srtp", user="1003")
    ua_b = await registered(runtime, "none", user="1004")
    incoming: asyncio.Queue = asyncio.Queue()
    ua_b.on_incoming(incoming.put_nowait)
    a_call = await ua_a.dial(f"sip:1004@{DOMAIN}")
    b_call = await asyncio.wait_for(incoming.get(), 10)
    await b_call.answer()
    await a_call.wait_established()
    await b_call.wait_established()
    assert a_call.media_encrypted
    assert await audio_arrives(a_call)

    await a_call.transfer(f"sip:9196@{DOMAIN}")

    assert a_call.state is CallState.CLOSED
    # FreeSWITCH moves the far leg to its echo service; its own teardown
    # race sometimes releases the leg instead (see test_transfer.py).
    deadline = asyncio.get_running_loop().time() + 5
    channels = ""
    while asyncio.get_running_loop().time() < deadline:
        channels = await fs_cli("show channels")
        if ",echo," in channels or b_call.state is CallState.CLOSED:
            break
        await asyncio.sleep(0.2)
    if ",echo," in channels:
        assert b_call.state is CallState.ESTABLISHED
        assert await audio_arrives(b_call)
    else:
        assert b_call.state is CallState.CLOSED
