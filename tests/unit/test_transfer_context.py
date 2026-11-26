#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Call context on a transfer: headers attached to the transfer target.

The transferor attaches headers to the address it asks the far end to
call; the far end sets them on the call it places. The tests at the end
run three agents on loopback: one transfers, one is asked to and
accepts, one receives the new call.
"""

import asyncio
import itertools
import logging

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, Config, Runtime, UserAgent
from baresip.call import (
    TransferRequest,
    _carried_header_lines,
    _parse_refer_to,
    _transfer_target,
)

UUI = "56a390f3d2b7310023a2;encoding=hex"
_PORTS = itertools.count(15650)


@pytest.mark.parametrize(
    ("uri", "headers", "target"),
    [
        ("sip:q@pbx.example", None, "<sip:q@pbx.example>"),
        ("sips:q@pbx.example", None, "<sips:q@pbx.example>"),
        ("SIP:q@pbx.example", None, "<SIP:q@pbx.example>"),
        ("<sip:q@pbx.example>", None, "<sip:q@pbx.example>"),
        ("1005", None, "1005"),
        (
            "1005",
            {"User-to-User": UUI, "X-Case": "7"},
            "1005\nUser-to-User=56a390f3d2b7310023a2%3Bencoding%3Dhex&X-Case=7",
        ),
        ("1005", {}, "1005"),
        ("support@pbx.example", None, "support@pbx.example"),
        ("tel:+15551234567", None, "tel:+15551234567"),
        (
            "sip:q@pbx.example",
            {"User-to-User": UUI},
            "<sip:q@pbx.example?User-to-User=56a390f3d2b7310023a2%3Bencoding%3Dhex>",
        ),
        (
            "sip:q@pbx.example;transport=tls",
            {"X-Case": "12 34", "X-Note": "a&b=c"},
            "<sip:q@pbx.example;transport=tls?X-Case=12%2034&X-Note=a%26b%3Dc>",
        ),
        (
            "<sip:q@pbx.example?X-A=1>;method=INVITE",
            {"X-B": "2"},
            "<sip:q@pbx.example?X-A=1&X-B=2>;method=INVITE",
        ),
        ("sip:q@pbx.example", {"X-Text": "café"}, "<sip:q@pbx.example?X-Text=caf%C3%A9>"),
        ("sip:q@pbx.example", {}, "<sip:q@pbx.example>"),
    ],
)
def test_the_target_is_bracketed_and_carries_the_headers_escaped(uri, headers, target):
    assert _transfer_target(uri, headers) == target


@pytest.mark.parametrize(
    ("uri", "headers"),
    [
        ("sip:q@pbx.example", {"From": "sip:evil@example"}),
        ("sip:q@pbx.example", {"call-id": "x"}),
        ("sip:q@pbx.example", {"Via": "SIP/2.0/UDP 1.2.3.4"}),
        ("sip:q@pbx.example", {"Route": "<sip:1.2.3.4;lr>"}),
        ("sip:q@pbx.example", {"Contact": "<sip:x@y>"}),
        ("sip:q@pbx.example", {"Content-Length": "0"}),
        ("sip:q@pbx.example", {"X-A": "1\r\nX-B: 2"}),
        ("sip:q@pbx.example", {"X A": "1"}),
        ("sip:q@pbx.example", {"": "1"}),
        ("1005", {"Via": "x"}),
        ("support@pbx.example", {"X-A": "1"}),
        ("tel:+15551234567", {"X-A": "1"}),
        ("pbx.example;transport=tls", {"X-A": "1"}),
        ("support?X-B=2", {"X-A": "1"}),
        ("<sip:q@pbx.example", None),
    ],
)
def test_a_target_that_cannot_be_built_is_refused(uri, headers):
    with pytest.raises(ValueError):
        _transfer_target(uri, headers)


@pytest.mark.parametrize(
    ("value", "escaped"),
    [
        ("a b", "a%20b"),
        ("a&b", "a%26b"),
        ("a=b", "a%3Db"),
        ("a;b", "a%3Bb"),
        ("a%b", "a%25b"),
        ("a%20b", "a%2520b"),
        ('"a"', "%22a%22"),
        ("<a>", "%3Ca%3E"),
        ("a,b", "a%2Cb"),
        ("a@b", "a%40b"),
        ("a#b", "a%23b"),
        ("caf\u00e9", "caf%C3%A9"),
        ("a+b/c:d?e$f[g]", "a+b/c:d?e$f[g]"),
        ("a-b_c.d!e~f*g'h(i)", "a-b_c.d!e~f*g'h(i)"),
    ],
)
def test_a_value_is_escaped_for_the_address_and_parses_back(value, escaped):
    target = _transfer_target("sip:q@h", {"X-Case": value})
    assert target == f"<sip:q@h?X-Case={escaped}>"
    assert _parse_refer_to(target).headers == {"X-Case": value}


@pytest.mark.parametrize(
    ("raw", "headers"),
    [
        # A peer that did not escape: inside angle brackets the value is whole.
        ("<sip:q@h?User-to-User=56a3;encoding=hex&X-Case=12 34>", {"User-to-User": "56a3;encoding=hex", "X-Case": "12 34"}),
        # Without them, what follows ";" is a parameter of the Refer-To header.
        ("sip:q@h?User-to-User=56a3;encoding=hex", {"User-to-User": "56a3"}),
        # "+" is a plus sign, not a space.
        ("<sip:q@h?X-Case=a+b>", {"X-Case": "a+b"}),
        # Unescaped once: an escaped percent sign stays one.
        ("<sip:q@h?X-Case=a%2520b>", {"X-Case": "a%20b"}),
        # A percent sign that starts no escape is kept.
        ("<sip:q@h?X-Case=100%&X-B=5%zz>", {"X-Case": "100%", "X-B": "5%zz"}),
        # An escaped name, and escapes in either letter case.
        ("<sip:q@h?X%2DCase=a%3bb%3Bc>", {"X-Case": "a;b;c"}),
        # A value holding "=".
        ("<sip:q@h?X-Case=a=b=c>", {"X-Case": "a=b=c"}),
    ],
)  # fmt: skip
def test_a_received_target_is_unescaped_once(raw, headers):
    assert _parse_refer_to(raw).headers == headers


def test_an_escaped_line_break_from_the_peer_is_not_carried(caplog):
    request = _parse_refer_to("<sip:q@h?X-A=1%0D%0AVia:%20evil&X-Ok=1>")
    assert request.headers == {"X-A": "1\r\nVia: evil", "X-Ok": "1"}
    with caplog.at_level(logging.WARNING, logger="baresip.call"):
        assert _carried_header_lines(request, None) == "\nX-Ok: 1"
    assert "is not carried" in caplog.text


def test_what_is_built_parses_back():
    headers = {"User-to-User": UUI, "X-Note": "a&b=c d", "X-Text": "café"}
    request = _parse_refer_to(_transfer_target("sip:q@pbx.example;transport=tls", headers))
    assert request.target == "sip:q@pbx.example;transport=tls"
    assert request.headers == headers


@pytest.mark.parametrize(
    ("raw", "headers"),
    [
        ("<sip:q@h>", {}),
        ("sip:q@h", {}),
        ("<sip:q@h?User-to-User=56a3%3Bencoding%3Dhex&X-Case=7>", {"User-to-User": "56a3;encoding=hex", "X-Case": "7"}),
        ("sip:q@h?X-Case=7", {"X-Case": "7"}),
        ("<sip:q@h?Replaces=abc%3Bto-tag%3D1%3Bfrom-tag%3D2&X-Case=7>", {"X-Case": "7"}),
        ("<sip:q@h?X-Empty=&X-Flag>;method=INVITE", {"X-Empty": "", "X-Flag": ""}),
    ],
)  # fmt: skip
def test_a_request_exposes_the_headers_attached_to_its_target(raw, headers):
    assert _parse_refer_to(raw).headers == headers


@pytest.mark.parametrize(
    "folded",
    [
        "<sip:q@h?X-Case=7>;\r\n method=INVITE",
        "<sip:q@h?X-Case=7>\r\n\t;method=INVITE",
        "<sip:q@h?X-Case=7>;\n X-Injected: 1",
        "<sip:q@h?X-Case=7>\r\nX-Injected: 1",
        "<sip:q@h?X-Case=7>\r",
    ],
)
def test_a_folded_target_is_unfolded_onto_one_line(folded):
    request = _parse_refer_to(folded)

    assert "\r" not in request.raw and "\n" not in request.raw
    assert request.target == "sip:q@h"
    assert request.headers == {"X-Case": "7"}
    # The accept command reads every line after the target as a header.
    assert _carried_header_lines(request, None) == "\nX-Case: 7"
    assert f"1 {request.raw}{_carried_header_lines(request, None)}".count("\n") == 1


def request_with(headers: dict) -> TransferRequest:
    return TransferRequest(
        target="sip:q@h", raw="<sip:q@h>", replaces=False, method="INVITE", headers=headers
    )


def test_an_accepted_request_carries_its_own_headers_by_default():
    request = request_with({"User-to-User": UUI, "X-Case": "7"})
    assert _carried_header_lines(request, None) == f"\nUser-to-User: {UUI}\nX-Case: 7"


def test_headers_passed_on_accept_are_added_to_the_requests():
    request = request_with({"User-to-User": UUI, "X-Case": "7"})
    assert _carried_header_lines(request, {"X-Mine": "1"}) == (
        f"\nUser-to-User: {UUI}\nX-Case: 7\nX-Mine: 1"
    )
    assert _carried_header_lines(request, {}) == f"\nUser-to-User: {UUI}\nX-Case: 7"


def test_a_header_passed_on_accept_takes_the_place_of_the_requests():
    request = request_with({"User-to-User": UUI, "X-Case": "7"})
    assert _carried_header_lines(request, {"x-case": "8"}) == f"\nUser-to-User: {UUI}\nx-case: 8"


def test_none_on_accept_leaves_a_header_out():
    request = request_with({"User-to-User": UUI, "X-Case": "7"})
    assert _carried_header_lines(request, {"user-to-user": None}) == "\nX-Case: 7"
    assert _carried_header_lines(request, {"User-to-User": None, "X-Case": None}) == ""
    assert _carried_header_lines(request, {"X-Absent": None}) == f"\nUser-to-User: {UUI}\nX-Case: 7"


def test_the_peers_bad_header_does_not_fail_an_accept_that_adds_headers(caplog):
    request = request_with({"From": "sip:evil@example", "X-Ok": "1"})
    with caplog.at_level(logging.WARNING, logger="baresip.call"):
        assert _carried_header_lines(request, {"X-Mine": "1"}) == "\nX-Ok: 1\nX-Mine: 1"
    assert "is not carried" in caplog.text


@pytest.mark.parametrize(
    "name",
    [
        "P-Asserted-Identity",
        "P-Preferred-Identity",
        "Diversion",
        "Privacy",
        "Proxy-Authorization",
        "Authorization",
        "Require",
        "Session-Expires",
        "Reason",
        "Subject",
        "Xtra",
    ],
)
def test_only_context_headers_are_taken_from_a_transfer_target(caplog, name):
    request = request_with({name: "from the far end", "User-to-User": UUI, "x-case": "7"})
    with caplog.at_level(logging.WARNING, logger="baresip.call"):
        assert _carried_header_lines(request, None) == f"\nUser-to-User: {UUI}\nx-case: 7"
    assert repr(name) in caplog.text


def test_the_application_may_add_a_header_a_target_may_not():
    request = request_with({"P-Asserted-Identity": "<sip:evil@example>", "User-to-User": UUI})
    lines = _carried_header_lines(request, {"P-Asserted-Identity": "<sip:bot@example>"})
    assert lines == f"\nUser-to-User: {UUI}\nP-Asserted-Identity: <sip:bot@example>"


def test_the_peers_call_defining_and_malformed_headers_are_dropped(caplog):
    request = request_with(
        {"From": "sip:evil@example", "ROUTE": "<sip:1.2.3.4;lr>", "X-A": "1\r\nVia: x", "X-Ok": "1"}
    )
    with caplog.at_level(logging.WARNING, logger="baresip.call"):
        assert _carried_header_lines(request, None) == "\nX-Ok: 1"
    assert sum("is not carried" in record.message for record in caplog.records) == 3


def test_headers_too_long_for_the_native_layer():
    request = request_with({"X-Long": "v" * 3000})
    assert _carried_header_lines(request, None) == ""
    with pytest.raises(ValueError, match="exceed"):
        _carried_header_lines(request, {"X-Long": "v" * 3000})


@pytest.mark.parametrize("headers", [{"To": "x"}, {"X-A": "1\n2"}])
def test_the_applications_bad_headers_on_accept_are_an_error(headers):
    with pytest.raises(ValueError):
        _carried_header_lines(request_with({}), headers)


@pytest.fixture(params=["manual"])
async def three_way(request):
    """A call from alice to bob, and carol waiting: ``(alice's call, bob's, carol's queue, address)``.

    The parameter is bob's transfer policy.
    """
    own = f"127.0.0.1:{next(_PORTS)}"
    runtime = Runtime()
    await runtime.start(
        Config(
            net_interface="127.0.0.1",
            max_concurrent_calls=None,
            expose_headers=("User-to-User", "X-Case", "X-Mine", "P-Asserted-Identity", "Diversion"),
            extra_config_text=f"sip_listen {own}\n",
        )
    )
    agents = {}
    for user in ("alice", "bob", "carol"):
        agents[user] = await UserAgent.create(
            runtime,
            Account(user=user, password="", domain=own, reg_interval=0),
            transfer_policy=request.param if user == "bob" else "manual",
        )
    to_bob: asyncio.Queue = asyncio.Queue()
    to_carol: asyncio.Queue = asyncio.Queue()
    agents["bob"].on_incoming(to_bob.put_nowait)
    agents["carol"].on_incoming(to_carol.put_nowait)
    placed = await agents["alice"].dial(f"sip:bob@{own}")
    received = await asyncio.wait_for(to_bob.get(), 5)
    await received.answer()
    await placed.wait_established(timeout=5)
    await received.wait_established(timeout=5)
    yield placed, received, to_carol, own
    await runtime.close()


async def asked_to_transfer(placed, received, target: str, headers: dict | None):
    """Have ``placed`` transfer ``received``; returns the request and the pending transfer."""
    requests: list[TransferRequest] = []
    received.on_transfer_request(requests.append)
    transfer = asyncio.create_task(placed.transfer(target, headers=headers, timeout=10))
    for _ in range(100):
        if requests:
            break
        await asyncio.sleep(0.05)
    assert requests, "the transfer request did not arrive"
    return requests[0], transfer


async def test_context_reaches_the_party_the_transfer_calls(three_way):
    placed, received, to_carol, own = three_way
    context = {"User-to-User": UUI, "X-Case": "12 34"}

    request, transfer = await asked_to_transfer(placed, received, f"sip:carol@{own}", context)
    assert request.target == f"sip:carol@{own}"
    assert request.headers == context

    await received.accept_transfer()
    new_call = await asyncio.wait_for(to_carol.get(), 5)
    assert new_call.headers == context
    await new_call.answer()
    await transfer


async def test_the_accepting_side_can_add_to_and_trim_the_context(three_way):
    placed, received, to_carol, own = three_way

    request, transfer = await asked_to_transfer(
        placed, received, f"sip:carol@{own}", {"User-to-User": UUI, "X-Case": "7"}
    )
    assert request.headers == {"User-to-User": UUI, "X-Case": "7"}

    await received.accept_transfer(headers={"X-Mine": "1", "X-Case": None})
    new_call = await asyncio.wait_for(to_carol.get(), 5)
    assert new_call.headers == {"User-to-User": UUI, "X-Mine": "1"}
    await new_call.answer()
    await transfer


# A value with every character the address form treats specially.
AWKWARD = 'a&b=c;d?e f%g+h "i" <j> caf\u00e9 100%25'


async def test_a_value_with_special_characters_arrives_unchanged(three_way):
    placed, received, to_carol, own = three_way

    request, transfer = await asked_to_transfer(
        placed, received, f"sip:carol@{own}", {"X-Case": AWKWARD}
    )
    assert request.headers == {"X-Case": AWKWARD}

    await received.accept_transfer()
    new_call = await asyncio.wait_for(to_carol.get(), 5)
    assert new_call.headers == {"X-Case": AWKWARD}
    await new_call.answer()
    await transfer


async def test_a_transfer_without_context_sets_no_header(three_way):
    placed, received, to_carol, own = three_way

    request, transfer = await asked_to_transfer(placed, received, f"sip:carol@{own}", None)
    assert request.raw == f"<sip:carol@{own}>"
    assert request.headers == {}

    await received.accept_transfer()
    new_call = await asyncio.wait_for(to_carol.get(), 5)
    assert new_call.headers == {}
    await new_call.answer()
    await transfer


@pytest.mark.parametrize("three_way", ["auto"], indirect=True)
async def test_a_transfer_accepted_by_policy_carries_the_context(three_way):
    placed, _received, to_carol, own = three_way
    context = {"User-to-User": UUI}

    transfer = asyncio.create_task(placed.transfer(f"sip:carol@{own}", headers=context, timeout=10))
    new_call = await asyncio.wait_for(to_carol.get(), 5)
    assert new_call.headers == context
    await new_call.answer()
    await transfer


async def test_a_bare_user_part_is_completed_and_carries_the_context(three_way):
    placed, received, to_carol, own = three_way
    context = {"User-to-User": UUI, "X-Case": "12 34"}

    request, transfer = await asked_to_transfer(placed, received, "carol", context)
    assert request.target == f"sip:carol@{own}"
    assert request.headers == context

    await received.accept_transfer()
    new_call = await asyncio.wait_for(to_carol.get(), 5)
    assert new_call.headers == context
    await new_call.answer()
    await transfer


async def test_a_bare_user_part_without_context_is_transferred_as_before(three_way):
    placed, received, to_carol, own = three_way

    request, transfer = await asked_to_transfer(placed, received, "carol", None)
    assert request.target == f"sip:carol@{own}"
    assert request.headers == {}

    await received.accept_transfer()
    await (await asyncio.wait_for(to_carol.get(), 5)).answer()
    await transfer


@pytest.mark.parametrize("three_way", ["manual", "auto"], indirect=True)
async def test_an_identity_attached_to_a_transfer_target_does_not_reach_the_new_call(three_way):
    placed, received, to_carol, own = three_way
    attached = {
        "P-Asserted-Identity": "<sip:ceo@example.com>",
        "Diversion": "<sip:other@example.com>",
        "X-Case": "7",
    }

    transfer = asyncio.create_task(
        placed.transfer(f"sip:carol@{own}", headers=attached, timeout=10)
    )
    if received.transfer_request is None:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if received.transfer_request is not None or not to_carol.empty():
                break
    if to_carol.empty():  # the manual policy: the application accepts
        assert received.transfer_request.headers == attached
        await received.accept_transfer()
    new_call = await asyncio.wait_for(to_carol.get(), 5)

    assert new_call.headers == {"X-Case": "7"}
    await new_call.answer()
    await transfer
