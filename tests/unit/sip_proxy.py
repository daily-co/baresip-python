#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""A minimal SIP proxy for tests: one call, between two agents on loopback.

FreeSWITCH, the bench's switch, ends each call leg itself and never puts
itself on a call's route, so a test that needs a proxy between two
agents runs this one. It forwards requests and responses, adds its Via,
and inserts the Record-Route the test asks for; it can also rewrite the
answering side's Contact. It is not a general proxy: it knows one call
at a time, does no retransmission or timers of its own, and trusts its
peers. What it exists to prove is how the stack under test follows the
route it is given.

The caller reaches the proxy over TLS or UDP. The proxy reaches the
answering agent over UDP at ``agent_addr``, or, as a registrar, over the
TLS connection the agent registered on. Later requests are sent to
whichever side did not send them, told apart by the caller's From tag,
over the path that side last sent from.

A second TLS listener with its own certificate stands in for another
node of the same service.
"""

import asyncio
import contextlib
import ssl

CRLF = "\r\n"


class Message:
    """One SIP message: a start line, its headers in order, and a body."""

    def __init__(self, raw: bytes):
        head, _, self.body = raw.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", "replace").split(CRLF)
        self.start = lines[0]
        self.headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            name, _, value = line.partition(":")
            self.headers.append((name.strip(), value.strip()))

    @property
    def is_request(self) -> bool:
        return not self.start.startswith("SIP/2.0")

    @property
    def method(self) -> str:
        if self.is_request:
            return self.start.split(" ", 1)[0]
        return self.get("CSeq").split(" ", 1)[-1]

    @property
    def status(self) -> int:
        return 0 if self.is_request else int(self.start.split(" ")[1])

    def get(self, name: str) -> str:
        return next((v for n, v in self.headers if n.lower() == name.lower()), "")

    def all(self, name: str) -> list[str]:
        return [v for n, v in self.headers if n.lower() == name.lower()]

    def remove_first(self, name: str) -> None:
        for i, (n, _) in enumerate(self.headers):
            if n.lower() == name.lower():
                del self.headers[i]
                return

    def replace(self, name: str, value: str) -> None:
        self.headers = [(n, value if n.lower() == name.lower() else v) for n, v in self.headers]

    def tag(self, name: str) -> str:
        _, found, rest = self.get(name).partition(";tag=")
        return rest.split(";")[0] if found else ""

    def encode(self) -> bytes:
        head = CRLF.join([self.start, *(f"{n}: {v}" for n, v in self.headers)])
        return head.encode() + b"\r\n\r\n" + self.body


class SipProxy:
    """A loopback SIP proxy that inserts a chosen Record-Route.

    Args:
        agent_addr: Where the answering agent listens, as (host, port), UDP.
        certfile, keyfile: The proxy's TLS certificate and key.
        record_route: Record-Route header values to insert, each a
            format string given ``udp`` and ``tls``, the proxy's ports;
            empty for none.
        contact: When set, replaces the URI in the Contact of the
            answering side's 2xx; a format string as above plus ``user``.
        tls_port: The port to accept TLS on; 0 for any free one.
        second_cert: The (certfile, keyfile) of a second TLS listener,
            ``tls2`` in the format strings; None for no such listener.
        registrar: Answer REGISTER with 200 and reach the answering
            agent over the connection it registered on.
    """

    def __init__(
        self,
        agent_addr: tuple[str, int],
        certfile: str,
        keyfile: str,
        record_route: tuple[str, ...] = (),
        contact: str | None = None,
        tls_port: int = 0,
        second_cert: tuple[str, str] | None = None,
        registrar: bool = False,
    ):
        self._agent_addr = agent_addr
        self._tls = self._context(certfile, keyfile)
        self._tls2 = self._context(*second_cert) if second_cert else None
        self._registrar = registrar
        self._agent_path = agent_addr  # how to reach the answering agent
        self._record_route = record_route
        self._contact = contact
        self._caller_tag = ""
        self._caller_path = None  # how to reach the caller: a writer or a UDP address
        self._return_paths: dict[str, object] = {}  # our Via branch -> where its request came from
        self._writers: list[asyncio.StreamWriter] = []
        #: Every request that passed through, as (method, transport it arrived on).
        self.requests: list[tuple[str, str]] = []
        #: The server name each TLS connection asked for, in order; None for none.
        self.server_names: list[str | None] = []
        self.udp_port = 0
        self.tls_port = tls_port
        self.tls2_port = 0

    def _context(self, certfile: str, keyfile: str) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        # The callback holds the list and not the proxy: a context that reached
        # itself through its own callback would never be freed.
        names = self.server_names
        context.sni_callback = lambda _socket, name, _context: names.append(name)
        return context

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._udp, _ = await loop.create_datagram_endpoint(
            lambda: _Udp(self), local_addr=("127.0.0.1", 0)
        )
        self.udp_port = self._udp.get_extra_info("sockname")[1]
        self._server = await asyncio.start_server(
            self._on_tls, "127.0.0.1", self.tls_port, ssl=self._tls
        )
        self.tls_port = self._server.sockets[0].getsockname()[1]
        self._server2 = None
        if self._tls2:
            self._server2 = await asyncio.start_server(self._on_tls, "127.0.0.1", 0, ssl=self._tls2)
            self.tls2_port = self._server2.sockets[0].getsockname()[1]

    async def close(self) -> None:
        self._udp.close()
        self._server.close()
        if self._server2:
            self._server2.close()
        for writer in self._writers:
            writer.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()

    async def drop_connections(self) -> None:
        """Close every TLS connection, as a restart or a network fault would."""
        writers, self._writers = self._writers, []
        for writer in writers:
            writer.transport.abort()
        await asyncio.sleep(0)

    def _format(self, template: str, **extra) -> str:
        return template.format(udp=self.udp_port, tls=self.tls_port, tls2=self.tls2_port, **extra)

    async def _on_tls(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = Message(head).get("Content-Length") or Message(head).get("l")
                body = await reader.readexactly(int(length or 0))
                self.handle(head + body, writer, "TLS")
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass

    def _send(self, message: Message, path) -> None:
        if isinstance(path, asyncio.StreamWriter):
            path.write(message.encode())
        else:
            self._udp.sendto(message.encode(), path)

    def handle(self, raw: bytes, source, transport: str) -> None:
        if not raw.strip():
            return  # a keep-alive
        message = Message(raw)
        if message.is_request:
            self._forward_request(message, source, transport)
        else:
            self._forward_response(message)

    def _register(self, message: Message, source) -> None:
        self._agent_path = source
        message.start = "SIP/2.0 200 OK"
        kept = ("via", "from", "to", "call-id", "cseq", "contact")
        message.headers = [(n, v) for n, v in message.headers if n.lower() in kept]
        message.replace("To", message.get("To") + ";tag=registrar")
        message.headers += [("Expires", "3600"), ("Content-Length", "0")]
        message.body = b""
        self._send(message, source)

    def _forward_request(self, message: Message, source, transport: str) -> None:
        self.requests.append((message.method, transport))
        if self._registrar and message.method == "REGISTER":
            self._register(message, source)
            return
        initial = not message.tag("To")
        if initial and message.method == "INVITE":
            self._caller_tag = message.tag("From")
            self._caller_path = source
        to_agent = message.tag("From") == self._caller_tag
        # Each side is reached over the path it last sent from.
        if to_agent:
            self._caller_path = source
        elif self._registrar:
            self._agent_path = source
        path = self._agent_path if to_agent else self._caller_path

        # Routes naming this proxy have done their work.
        ports = (self.udp_port, self.tls_port, self.tls2_port)
        ours = tuple(f":{port}" for port in ports if port)
        while message.get("Route") and any(port in message.get("Route") for port in ours):
            message.remove_first("Route")
        if to_agent:
            user = message.start.split(" ")[1].split(":", 1)[1].split("@")[0]
            host, port = self._agent_addr
            message.start = f"{message.method} sip:{user}@{host}:{port} SIP/2.0"

        # The branch follows the request's own, so that a CANCEL and the
        # ACK of a refusal land in the transaction of the INVITE they
        # belong to.
        branch = "z9hG4bK-proxy-" + message.get("Via").partition("branch=")[2].split(";")[0]
        out = "TLS" if isinstance(path, asyncio.StreamWriter) else "UDP"
        port = self.tls_port if out == "TLS" else self.udp_port
        message.headers.insert(0, ("Via", f"SIP/2.0/{out} 127.0.0.1:{port};branch={branch}"))
        self._return_paths[branch] = source
        if initial and message.method == "INVITE":
            for template in reversed(self._record_route):
                message.headers.insert(1, ("Record-Route", self._format(template)))
        self._send(message, path)

    def _forward_response(self, message: Message) -> None:
        branch = message.get("Via").partition("branch=")[2].split(";")[0]
        message.remove_first("Via")
        path = self._return_paths.get(branch)
        if path is None:
            return
        if self._contact and message.method == "INVITE" and 200 <= message.status < 300:
            user = message.get("Contact").split("sip:", 1)[-1].split("@")[0]
            message.replace("Contact", f"<{self._format(self._contact, user=user)}>")
        self._send(message, path)


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, proxy: SipProxy):
        self._proxy = proxy

    def datagram_received(self, data: bytes, addr) -> None:
        self._proxy.handle(data, addr, "UDP")
