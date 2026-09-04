#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Registration against the FreeSWITCH bench (`make bench-up` first).

Run with: pytest -m bench tests/integration
"""

import logging
import os

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, Config, RegistrationError
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"


def bench_account(password: str = "bench1234") -> Account:
    return Account(user="1001", password=password, domain=DOMAIN)


@pytest.fixture
async def runtime():
    rt = Runtime()
    await rt.start()
    yield rt
    await rt.close()


async def test_register_and_unregister(runtime):
    ua = await UserAgent.create(runtime, bench_account())
    await ua.register()
    await ua.unregister()


async def test_register_carries_instance_id(caplog):
    """The Contact of a REGISTER carries +sip.instance when configured;
    observed through the SIP trace, which logs the actual wire bytes."""
    instance = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    rt = Runtime()
    await rt.start(Config(instance_id=instance, sip_trace=True))
    try:
        with caplog.at_level(logging.DEBUG, logger="baresip.native.sip"):
            ua = await UserAgent.create(rt, bench_account())
            await ua.register()
            await ua.unregister()
        assert f'+sip.instance="<urn:uuid:{instance}>"' in caplog.text
    finally:
        await rt.close()


async def test_wrong_password_is_rejected_with_status(runtime):
    """The 401 challenge is consumed by the digest retry; what the
    application sees is the registrar's final verdict on the bad
    credentials — from FreeSWITCH, a 403."""
    ua = await UserAgent.create(runtime, bench_account(password="wrong"))
    with pytest.raises(RegistrationError) as excinfo:
        await ua.register()
    assert excinfo.value.status == 403
    assert "Forbidden" in excinfo.value.reason


async def test_sip_trace_captures_raw_messages():
    """With sip_trace on, the raw REGISTER traffic must surface as records
    on the baresip.native.sip logger — the observable behind the toggle."""
    records = []
    handler = logging.Handler(level=logging.DEBUG)
    handler.emit = records.append
    sip_logger = logging.getLogger("baresip.native.sip")
    sip_logger.addHandler(handler)
    sip_logger.setLevel(logging.DEBUG)

    runtime = Runtime()
    await runtime.start(Config(sip_trace=True))
    try:
        ua = await UserAgent.create(runtime, bench_account())
        await ua.register()
        await ua.unregister()
    finally:
        await runtime.close()
        sip_logger.removeHandler(handler)

    text = "\n".join(record.getMessage() for record in records)
    assert "REGISTER" in text, "expected the raw REGISTER request in the trace"
    assert "200" in text, "expected the registrar's 200 in the trace"
