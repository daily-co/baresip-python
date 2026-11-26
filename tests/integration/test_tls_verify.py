#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Server certificate verification, against the FreeSWITCH bench.

The bench presents a self-signed certificate naming ``127.0.0.1`` and
``localhost``. It is trusted only when ``SIP_CA_FILE`` points at it,
and only under those names. Run with:

    pytest -m bench tests/integration
"""

import os
import pathlib
import socket

import pytest

pytest.importorskip("baresip._native")

from baresip import Account, Config, RegistrationError
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

TLS_PORT = os.environ.get("BENCH_TLS_PORT", "15061")
BENCH_CERT = pathlib.Path(__file__).resolve().parents[2] / "bench" / "certs" / "cert.pem"
# A public name for 127.0.0.1 that the bench certificate does not carry.
OTHER_NAME = "localtest.me"


async def register(domain: str, **config_fields):
    runtime = Runtime()
    await runtime.start(Config(net_interface="127.0.0.1", **config_fields))
    try:
        account = Account(user="1003", password="bench1234", domain=domain, transport="tls")
        ua = await UserAgent.create(runtime, account)
        await ua.register()
    finally:
        await runtime.close()


async def test_a_trusted_certificate_registers(monkeypatch):
    monkeypatch.setenv("SIP_CA_FILE", str(BENCH_CERT))

    await register(f"127.0.0.1:{TLS_PORT}")


async def test_an_untrusted_certificate_is_refused(monkeypatch):
    # Whatever the platform trusts, it is not the bench's own certificate.
    for name in ("SIP_CA_FILE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RegistrationError, match="certificate was not accepted") as failure:
        await register(f"127.0.0.1:{TLS_PORT}")

    assert "self-signed" in failure.value.reason
    assert failure.value.status is None


async def test_a_certificate_for_another_name_is_refused(monkeypatch):
    try:
        resolved = socket.gethostbyname(OTHER_NAME)
    except OSError:
        resolved = None
    if resolved != "127.0.0.1":
        pytest.skip(f"{OTHER_NAME} does not resolve to 127.0.0.1 here")
    monkeypatch.setenv("SIP_CA_FILE", str(BENCH_CERT))

    with pytest.raises(RegistrationError, match="certificate was not accepted") as failure:
        await register(f"{OTHER_NAME}:{TLS_PORT}")

    assert "mismatch" in failure.value.reason


async def test_verification_can_be_turned_off_in_raw_configuration(monkeypatch):
    for name in ("SIP_CA_FILE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)

    await register(f"127.0.0.1:{TLS_PORT}", extra_config_text="sip_verify_server no\n")
