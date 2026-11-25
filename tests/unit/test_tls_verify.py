#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""TLS servers are verified against CA certificates found at start.

The discovery order, what the runtime adds to the stack's configuration,
and what happens when nothing is found: TLS is refused, everything else
runs. Verification itself, against real certificates, is covered by the
bench tests.
"""

import os
import ssl
import sys
import types

import pytest

pytest.importorskip("baresip._native")

import baresip.config as config_module
import baresip.runtime as runtime_module
from baresip import Account, Config, NoCaCertificatesError, Runtime, UserAgent
from baresip.config import discover_ca, sip_ca_file


@pytest.fixture
def no_ca_anywhere(monkeypatch):
    """A host with no CA certificates in any place discovery looks."""
    for name in ("SIP_CA_FILE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    paths = ssl.DefaultVerifyPaths(None, None, "SSL_CERT_FILE", None, "SSL_CERT_DIR", None)
    monkeypatch.setattr(config_module.ssl, "get_default_verify_paths", lambda: paths)
    monkeypatch.setitem(sys.modules, "certifi", None)


def platform_paths(monkeypatch, cafile=None, capath=None):
    paths = ssl.DefaultVerifyPaths(cafile, capath, "SSL_CERT_FILE", None, "SSL_CERT_DIR", None)
    monkeypatch.setattr(config_module.ssl, "get_default_verify_paths", lambda: paths)


def bundle(tmp_path, name="ca.pem"):
    path = tmp_path / name
    path.write_text("-----BEGIN CERTIFICATE-----\n")
    return str(path)


def test_sip_ca_file_must_name_a_file(no_ca_anywhere, monkeypatch, tmp_path):
    assert sip_ca_file() is None

    monkeypatch.setenv("SIP_CA_FILE", bundle(tmp_path))
    assert sip_ca_file() == str(tmp_path / "ca.pem")

    for wrong in (tmp_path / "absent.pem", tmp_path):
        monkeypatch.setenv("SIP_CA_FILE", str(wrong))
        with pytest.raises(ValueError, match="SIP_CA_FILE"):
            sip_ca_file()


def test_ssl_cert_file_wins_over_the_platform(no_ca_anywhere, monkeypatch, tmp_path):
    env, platform = bundle(tmp_path, "env.pem"), bundle(tmp_path, "platform.pem")
    monkeypatch.setenv("SSL_CERT_FILE", env)
    platform_paths(monkeypatch, cafile=platform)

    assert discover_ca() == ("sip_cafile", env)


def test_ssl_cert_dir_is_used_when_it_holds_certificates(no_ca_anywhere, monkeypatch, tmp_path):
    bundle(tmp_path)
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))

    assert discover_ca() == ("sip_capath", str(tmp_path))


def test_missing_and_empty_candidates_are_skipped(no_ca_anywhere, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "absent.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(empty))
    platform = bundle(tmp_path)
    platform_paths(monkeypatch, cafile=platform)

    assert discover_ca() == ("sip_cafile", platform)


def test_platform_directory_follows_platform_file(no_ca_anywhere, monkeypatch, tmp_path):
    bundle(tmp_path)
    platform_paths(monkeypatch, cafile=str(tmp_path / "absent.pem"), capath=str(tmp_path))

    assert discover_ca() == ("sip_capath", str(tmp_path))


def test_certifi_is_the_last_resort(no_ca_anywhere, monkeypatch, tmp_path):
    path = bundle(tmp_path)
    monkeypatch.setitem(sys.modules, "certifi", types.SimpleNamespace(where=lambda: path))

    assert discover_ca() == ("sip_cafile", path)


def test_nothing_found(no_ca_anywhere):
    assert discover_ca() is None


def test_runtime_adds_the_discovered_bundle(no_ca_anywhere, monkeypatch, tmp_path):
    path = bundle(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", path)
    runtime = Runtime()

    text = runtime._with_tls_trust("net_interface 127.0.0.1")

    assert text == f"net_interface 127.0.0.1\nsip_cafile {path}\n"
    assert runtime.tls_available


def test_sip_ca_file_is_trusted_alongside_the_platform_file(no_ca_anywhere, monkeypatch, tmp_path):
    platform, private = tmp_path / "platform.pem", tmp_path / "private.pem"
    platform.write_text("PUBLIC CAS\n")
    private.write_text("PRIVATE CA")
    platform_paths(monkeypatch, cafile=str(platform))
    monkeypatch.setenv("SIP_CA_FILE", str(private))
    runtime = Runtime()

    text = runtime._with_tls_trust("")

    joined = runtime._ca_bundle
    assert text == f"\nsip_cafile {joined}\n"
    with open(joined) as f:
        assert f.read() == "PUBLIC CAS\nPRIVATE CA\n"
    runtime._remove_conf_dir()
    assert not os.path.exists(joined)


def test_sip_ca_file_is_trusted_alongside_the_platform_directory(
    no_ca_anywhere, monkeypatch, tmp_path
):
    certs = tmp_path / "certs"
    certs.mkdir()
    bundle(certs)
    private = bundle(tmp_path, "private.pem")
    platform_paths(monkeypatch, capath=str(certs))
    monkeypatch.setenv("SIP_CA_FILE", private)

    text = Runtime()._with_tls_trust("x\n")

    assert text == f"x\nsip_cafile {private}\nsip_capath {certs}\n"


PEM = "-----BEGIN CERTIFICATE-----\n{}\n-----END CERTIFICATE-----\n"


def stack_value(text: str, directive: str) -> str:
    """What the stack reads for ``directive``: its parser stops at whitespace."""
    line = next(ln for ln in text.splitlines() if ln.startswith(directive + " "))
    return line.split(None, 2)[1]


def test_a_ca_file_whose_path_has_a_space_still_reaches_the_stack(
    no_ca_anywhere, monkeypatch, tmp_path
):
    spaced = tmp_path / "My Certs"
    spaced.mkdir()
    private = spaced / "ca.pem"
    private.write_text(PEM.format("PRIVATE"))
    monkeypatch.setenv("SIP_CA_FILE", str(private))
    runtime = Runtime()

    text = runtime._with_tls_trust("")

    read_by_stack = stack_value(text, "sip_cafile")
    assert " " not in read_by_stack
    with open(read_by_stack) as f:
        assert f.read() == PEM.format("PRIVATE")
    runtime._remove_conf_dir()
    assert not os.path.exists(read_by_stack)


def test_a_platform_file_whose_path_has_a_space_still_reaches_the_stack(
    no_ca_anywhere, monkeypatch, tmp_path
):
    spaced = tmp_path / "Program Files"
    spaced.mkdir()
    platform = spaced / "cert.pem"
    platform.write_text(PEM.format("PUBLIC"))
    platform_paths(monkeypatch, cafile=str(platform))
    runtime = Runtime()

    text = runtime._with_tls_trust("")

    with open(stack_value(text, "sip_cafile")) as f:
        assert f.read() == PEM.format("PUBLIC")
    runtime._remove_conf_dir()


def test_a_ca_directory_whose_path_has_a_space_is_gathered_into_a_file(
    no_ca_anywhere, monkeypatch, tmp_path
):
    spaced = tmp_path / "ssl certs"
    spaced.mkdir()
    (spaced / "one.pem").write_text(PEM.format("ONE"))
    (spaced / "0a1b2c3d.0").write_text(PEM.format("ONE"))  # the same certificate, by its hash
    (spaced / "two.crt").write_text(PEM.format("TWO"))
    (spaced / "README").write_text("not a certificate\n")
    monkeypatch.setenv("SSL_CERT_DIR", str(spaced))
    runtime = Runtime()

    text = runtime._with_tls_trust("")

    assert "sip_capath" not in text
    with open(stack_value(text, "sip_cafile")) as f:
        assert f.read() == PEM.format("ONE") + PEM.format("TWO")
    runtime._remove_conf_dir()


def test_paths_without_spaces_are_handed_over_as_they_are(no_ca_anywhere, monkeypatch, tmp_path):
    path = bundle(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", path)
    runtime = Runtime()

    runtime._with_tls_trust("")

    assert runtime._ca_bundle is None


def test_sip_ca_file_alone_is_enough_for_tls(no_ca_anywhere, monkeypatch, tmp_path):
    private = bundle(tmp_path)
    monkeypatch.setenv("SIP_CA_FILE", private)
    runtime = Runtime()

    assert runtime._with_tls_trust("x\n") == f"x\nsip_cafile {private}\n"
    assert runtime.tls_available


@pytest.mark.parametrize(
    "line", ["sip_cafile /etc/my-ca.pem", "sip_capath /etc/my-cas", "  sip_verify_server no"]
)
def test_configuration_that_sets_tls_trust_is_left_as_written(no_ca_anywhere, line):
    runtime = Runtime()
    text = f"net_interface 127.0.0.1\n{line}\n"

    assert runtime._with_tls_trust(text) == text
    assert runtime.tls_available


def test_without_ca_certificates_tls_leaves_the_transports(no_ca_anywhere):
    runtime = Runtime()

    text = runtime._with_tls_trust("net_interface 127.0.0.1\n")

    assert text == "net_interface 127.0.0.1\nsip_transports UDP,TCP\n"
    assert not runtime.tls_available


@pytest.mark.parametrize(
    ("given", "left"),
    [
        ("sip_transports UDP,TCP,TLS\n", "sip_transports UDP,TCP\n"),
        ("sip_transports tls\n", "sip_transports UDP\n"),
        ("net_interface x\n  sip_transports\tTLS , udp,WSS,ws\n", "net_interface x\n  sip_transports\tudp,ws\n"),
        ("sip_transports UDP\nsip_transports TLS\n", "sip_transports UDP\nsip_transports UDP\n"),
    ],
)  # fmt: skip
def test_without_ca_certificates_a_transports_line_of_the_callers_loses_tls(
    no_ca_anywhere, given, left
):
    runtime = Runtime()

    assert runtime._with_tls_trust(given) == left
    assert not runtime.tls_available


async def test_without_ca_certificates_tls_is_refused_and_udp_works(no_ca_anywhere):
    runtime = Runtime()
    await runtime.start(Config(net_interface="127.0.0.1"))
    try:
        with pytest.raises(NoCaCertificatesError, match="SIP_CA_FILE"):
            await UserAgent.create(
                runtime, Account(user="bot", password="", domain="sip.invalid", transport="tls")
            )

        ua = await UserAgent.create(
            runtime, Account(user="bot", password="", domain="sip.invalid", reg_interval=0)
        )
        for target in ("sip:x@sip.invalid;transport=tls", "sips:x@sip.invalid"):
            with pytest.raises(NoCaCertificatesError):
                await ua.dial(target)
        call = await ua.dial("sip:x@sip.invalid")
        assert call.peer == "sip:x@sip.invalid"
    finally:
        await runtime.close()


def test_a_recent_certificate_rejection_is_remembered_then_forgotten(monkeypatch):
    runtime = Runtime()
    now = [100.0]
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: now[0])
    runtime._tls_verify_error = (now[0], "hostname mismatch")

    assert runtime.tls_verify_error() == "hostname mismatch"
    now[0] += 60
    assert runtime.tls_verify_error() is None
