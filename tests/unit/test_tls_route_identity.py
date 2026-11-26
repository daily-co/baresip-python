#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""A call's later TLS connections, when its route gives a bare IP address.

A proxy on a call's route commonly names itself by address, and
certificates name hosts. A request inside the call that needs a new
connection (after the first one closed, or to another node of the
service) is then made to an address the certificate does not carry. The
connection is accepted when the server proves the address, or the host
name the call was configured with: the name dialled, or the name a
registered agent's connection was verified under.

Each test watches the far end, or the proxy, to see whether the request
arrived.
"""

import asyncio
import itertools
import logging
import pathlib
import shutil
import subprocess

import pytest

native = pytest.importorskip("baresip._native")

from sip_proxy import SipProxy
from test_proxy_routes import closed

from baresip import Account, Config, Runtime, UserAgent

_PORTS = itertools.count(5460)

# The caller's side of the route, by address, on the listener it dialled
# and on the second one; the answering side's is the UDP entry.
SAME_NODE = ("<sip:127.0.0.1:{udp};lr>", "<sip:127.0.0.1:{tls};transport=tls;lr>")
OTHER_NODE = ("<sip:127.0.0.1:{udp};lr>", "<sip:127.0.0.1:{tls2};transport=tls;lr>")
BY_NAME = "sip:bob@localhost:{tls};transport=tls"
BY_ADDRESS = "sip:bob@127.0.0.1:{tls};transport=tls"

_CA_CONFIG = """\
[ca]
default_ca = this
[this]
database = {folder}/index.txt
new_certs_dir = {folder}
serial = {folder}/serial
default_md = sha256
policy = any
unique_subject = no
copy_extensions = copy
[any]
commonName = supplied
[req]
distinguished_name = dn
[dn]
"""


def _certificate(folder, label: str, names: str, expired: bool = False) -> tuple[str, str]:
    """A self-signed certificate for ``names`` (a subjectAltName value)."""
    cert, key, request = (folder / f"{label}.{ext}" for ext in ("pem", "key", "csr"))
    config = folder / "ca.cnf"
    config.write_text(_CA_CONFIG.format(folder=folder))
    (folder / "index.txt").write_text("")
    (folder / "serial").write_text("01\n")
    subprocess.run(
        ["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key)]
        + ["-out", str(request), "-subj", f"/CN={label}", "-addext", f"subjectAltName={names}"],
        check=True,
        capture_output=True,
    )
    start, end = ("20200101000000Z", "20200102000000Z") if expired else (None, None)
    validity = ["-startdate", start, "-enddate", end] if expired else ["-days", "2"]
    subprocess.run(
        ["openssl", "ca", "-batch", "-selfsign", "-notext", "-config", str(config)]
        + ["-keyfile", str(key), "-in", str(request), "-out", str(cert), *validity],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)


@pytest.fixture(scope="session")
def certificates(tmp_path_factory):
    """Certificates by what they name, and a CA file trusting all but one."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    folder = tmp_path_factory.mktemp("route-identity")
    made = {
        "name": _certificate(folder, "name", "DNS:localhost"),
        "address": _certificate(folder, "address", "IP:127.0.0.1"),
        "other name": _certificate(folder, "other", "DNS:other.example"),
        "address and other name": _certificate(folder, "both", "IP:127.0.0.1,DNS:other.example"),
        "name, expired": _certificate(folder, "expired", "DNS:localhost", expired=True),
        "name, untrusted": _certificate(folder, "untrusted", "DNS:localhost"),
    }
    bundle = folder / "trusted.pem"
    trusted = [cert for label, (cert, _key) in made.items() if label != "name, untrusted"]
    bundle.write_text("".join(pathlib.Path(cert).read_text() for cert in trusted))
    return made, str(bundle)


@pytest.fixture
async def scene(certificates, monkeypatch):
    """Place a call through the proxy: ``await scene(first=..., ...)``."""
    made, bundle = certificates
    monkeypatch.setenv("SIP_CA_FILE", bundle)
    port = next(_PORTS)
    own = f"127.0.0.1:{port}"
    runtime = Runtime()
    await runtime.start(
        Config(
            net_interface="127.0.0.1", extra_config_text=f"sip_listen {own}\ndns_getaddrinfo yes\n"
        )
    )
    proxies: list[SipProxy] = []

    async def connect(
        *,
        first: str,
        second: str | None = None,
        route: tuple[str, ...] = SAME_NODE,
        target: str = BY_NAME,
        caller_transport: str = "tls",
        registered_answerer: bool = False,
        answered: bool = True,
    ):
        proxy = SipProxy(
            ("127.0.0.1", port),
            *made[first],
            record_route=route,
            second_cert=made[second] if second else None,
            registrar=registered_answerer,
        )
        await proxy.start()
        proxies.append(proxy)
        caller = await UserAgent.create(
            runtime,
            Account(
                user="alice", password="", domain=own, reg_interval=0, transport=caller_transport
            ),
        )
        if registered_answerer:
            callee = await UserAgent.create(
                runtime,
                Account(
                    user="bob",
                    password="",
                    domain=f"localhost:{proxy.tls_port}",
                    reg_interval=3600,
                    transport="tls",
                ),
            )
            await callee.register(timeout=5)
        else:
            callee = await UserAgent.create(
                runtime, Account(user="bob", password="", domain=own, reg_interval=0)
            )
        incoming: asyncio.Queue = asyncio.Queue()
        callee.on_incoming(incoming.put_nowait)
        placed = await caller.dial(
            target.format(udp=proxy.udp_port, tls=proxy.tls_port, tls2=proxy.tls2_port)
        )
        received = await asyncio.wait_for(incoming.get(), 5)
        await received.answer()
        await placed.wait_established(timeout=5)
        if answered:
            # Established on the answering side means the caller's ACK arrived.
            await received.wait_established(timeout=5)
        return placed, received, proxy

    yield connect
    await runtime.close()
    for proxy in proxies:
        await proxy.close()


async def dropped(proxy: SipProxy) -> None:
    """Close the proxy's connections and let the agents notice."""
    await proxy.drop_connections()
    await asyncio.sleep(0.3)


async def arrives(proxy: SipProxy, method: str, within: float = 3.0) -> bool:
    """Whether a request of ``method`` reaches the proxy over TLS."""
    for _ in range(int(within / 0.05)):
        if (method, "TLS") in proxy.requests:
            return True
        await asyncio.sleep(0.05)
    return False


async def logged(caplog, text: str, within: float = 5.0) -> bool:
    for _ in range(int(within / 0.05)):
        if any(text in record.getMessage() for record in caplog.records):
            return True
        await asyncio.sleep(0.05)
    return False


async def test_a_hangup_reconnects_to_a_route_by_address(scene):
    placed, received, proxy = await scene(first="name")
    await dropped(proxy)
    before = len(proxy.server_names)

    gone = closed(received)
    await placed.hangup()
    await asyncio.wait_for(gone, 3)

    # The new connection asked for the server by the name the call dialled.
    assert proxy.server_names[before:] == ["localhost"]


async def test_a_hold_and_a_transfer_reconnect_to_a_route_by_address(scene):
    placed, received, proxy = await scene(first="name")

    await dropped(proxy)
    await placed.hold()
    for _ in range(60):
        if received.remote_on_hold:
            break
        await asyncio.sleep(0.05)
    assert received.remote_on_hold

    await dropped(proxy)
    transfer = asyncio.ensure_future(placed.transfer("sip:carol@127.0.0.1:9", timeout=2))
    assert await arrives(proxy, "REFER")
    transfer.cancel()
    await asyncio.gather(transfer, return_exceptions=True)


async def test_another_node_with_the_same_certificate_is_reached(scene):
    placed, received, proxy = await scene(first="name", second="name", route=OTHER_NODE)

    gone = closed(received)
    await placed.hangup()
    await asyncio.wait_for(gone, 3)
    assert proxy.server_names[-1] == "localhost"


async def test_a_route_with_an_maddr_address_is_reached(scene):
    route = (SAME_NODE[0], "<sip:node.invalid:{tls2};maddr=127.0.0.1;transport=tls;lr>")
    placed, received, _proxy = await scene(first="name", second="name", route=route)

    gone = closed(received)
    await placed.hangup()
    await asyncio.wait_for(gone, 3)


async def test_a_call_dialled_by_name_over_udp_follows_a_sips_route_by_address(scene):
    placed, received, proxy = await scene(
        first="name",
        route=("<sip:127.0.0.1:{udp};lr>", "<sips:127.0.0.1:{tls};lr>"),
        target="sip:bob@localhost:{udp}",
        caller_transport="udp",
    )

    gone = closed(received)
    await placed.hangup()
    await asyncio.wait_for(gone, 3)
    assert ("ACK", "TLS") in proxy.requests
    assert ("BYE", "TLS") in proxy.requests


async def test_a_registered_agents_hangup_reconnects_to_a_route_by_address(scene):
    """The call arrives on the connection the agent registered on, by name."""
    placed, received, proxy = await scene(
        first="name",
        route=("<sip:127.0.0.1:{tls};transport=tls;lr>", "<sip:127.0.0.1:{udp};lr>"),
        target="sip:bob@127.0.0.1:{udp}",
        caller_transport="udp",
        registered_answerer=True,
    )
    await dropped(proxy)
    before = len(proxy.server_names)

    gone = closed(placed)
    await received.hangup()
    await asyncio.wait_for(gone, 3)
    assert proxy.server_names[before:] == ["localhost"]


@pytest.mark.parametrize(
    ("second", "reason"),
    [
        ("other name", "IP address mismatch"),
        ("name, expired", "certificate has expired"),
        ("name, untrusted", "certificate verify failed"),
    ],
)
async def test_another_node_that_does_not_prove_the_name_is_refused(scene, caplog, second, reason):
    caplog.set_level(logging.WARNING, logger="baresip.native")
    _placed, _received, proxy = await scene(
        first="name", second=second, route=OTHER_NODE, answered=False
    )

    assert await logged(caplog, reason)
    assert not await arrives(proxy, "ACK", within=0.5)


async def test_a_node_that_proves_its_address_is_still_reached(scene):
    """A certificate for the address, and not the call's name, is enough."""
    placed, received, _proxy = await scene(
        first="name", second="address and other name", route=OTHER_NODE
    )

    gone = closed(received)
    await placed.hangup()
    await asyncio.wait_for(gone, 3)


async def test_a_call_dialled_by_address_reconnects_by_address(scene):
    placed, received, proxy = await scene(first="address", target=BY_ADDRESS)
    await dropped(proxy)

    gone = closed(received)
    await placed.hangup()
    await asyncio.wait_for(gone, 3)


async def test_a_call_dialled_by_address_has_no_name_to_prove(scene, caplog):
    """A node naming only a host is refused: the call never configured that name."""
    caplog.set_level(logging.WARNING, logger="baresip.native")
    _placed, _received, proxy = await scene(
        first="address", second="name", route=OTHER_NODE, target=BY_ADDRESS, answered=False
    )

    assert await logged(caplog, "IP address mismatch")
    assert not await arrives(proxy, "ACK", within=0.5)
