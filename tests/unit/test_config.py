#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Config and Account: the exact text they render, and what they refuse.

The golden strings here are byte-for-byte on purpose: the native parsers
consume this text, so any drift in it is a behavior change and must show up
as a failing test, not as a mystery at registration time.

Pure-Python tests — no built extension required.
"""

import pytest

from baresip import Account, Config
from baresip.config import LOG_LEVEL_NAMES

# -- Account.aor() -------------------------------------------------------------


def test_aor_defaults_golden():
    account = Account(user="alice", domain="example.com", password="s3cret")
    assert account.aor() == (
        '<sip:alice@example.com;transport=udp>;auth_pass="s3cret";regint=600;'
        "answermode=manual;audio_codecs=pcmu,pcma;dtmfmode=rtpevent"
    )


def test_aor_every_field_golden():
    account = Account(
        user="alice",
        domain="example.com:5061",
        password="pass word",  # spaces survive: the parameter is quoted
        registrar="sbc.example.com;transport=tcp",
        reg_interval=300,
        transport="tls",
        audio_codecs=("opus/48000/2", "pcmu"),
        dtmf_mode="info",
        auth_user="alice@corp",  # '@' is fine: parameters end at ';'
    )
    assert account.aor() == (
        '<sip:alice@example.com:5061;transport=tls>;auth_pass="pass word";'
        "auth_user=alice@corp;regint=300;answermode=manual;"
        "audio_codecs=opus/48000/2,pcmu;"
        'dtmfmode=info;outbound="sip:sbc.example.com;transport=tcp"'
    )


def test_aor_empty_password_omits_the_parameter():
    aor = Account(user="alice", domain="example.com", password="").aor()
    assert "auth_pass" not in aor


def test_aor_unset_auth_user_omits_the_parameter():
    # Absent, the stack authenticates as the AOR's user part.
    aor = Account(user="alice", domain="example.com", password="x").aor()
    assert "auth_user" not in aor


def test_aor_empty_codecs_offer_everything_loaded():
    aor = Account(user="alice", domain="example.com", password="x", audio_codecs=()).aor()
    assert "audio_codecs" not in aor


def test_aor_registrar_uri_is_not_double_prefixed():
    aor = Account(user="a", domain="d", password="x", registrar="sips:sbc.example.com").aor()
    assert 'outbound="sips:sbc.example.com"' in aor


def test_repr_redacts_the_password():
    account = Account(user="alice", domain="example.com", password="s3cret")
    assert "s3cret" not in repr(account)
    assert "***" in repr(account)
    # An unset password is shown as such, not as a fake redaction.
    assert "'***'" not in repr(Account(user="alice", domain="example.com", password=""))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user": ""},
        {"user": "a;b"},  # ends the URI early
        {"user": "a b"},
        {"user": "a@b"},
        {"domain": ""},
        {"domain": "ex ample.com"},
        {"domain": "d;transport=tcp"},  # transport is a field, not a suffix
        {"password": 'p"w'},  # no unescape exists: cannot round-trip
        {"password": "p\\w"},
        {"password": "p\nw"},
        {"registrar": ""},
        {"registrar": 'sbc"'},
        {"registrar": "sbc example.com"},
        {"reg_interval": -1},
        {"transport": "sctp"},
        {"audio_codecs": ("",)},
        {"audio_codecs": ("pcmu,pcma",)},  # one name, not a pre-joined list
        {"dtmf_mode": "inband"},
        {"auth_user": ""},  # None means "authenticate as user"
        {"auth_user": "a b"},  # bare parameter: no quoting to hide a space
        {"auth_user": "a;b"},  # ends the parameter early
        {"auth_user": 'a"b'},
    ],
)
def test_account_rejects_what_the_parser_would_misread(kwargs):
    fields = {"user": "alice", "domain": "example.com", "password": "x", **kwargs}
    with pytest.raises(ValueError):
        Account(**fields)


@pytest.mark.parametrize("value", [True, "600", 600.0])
def test_account_rejects_non_int_reg_interval(value):
    with pytest.raises(TypeError):
        Account(user="a", domain="d", password="x", reg_interval=value)


# -- Config.render() -----------------------------------------------------------


# Every render ends with the video block; the drivers are fixed (vidmem
# carries frames to and from Python) and the knobs are Config fields.
DEFAULT_VIDEO_TAIL = (
    "video_source vidmem,default\n"
    "video_display vidmem,default\n"
    'video_size "640x480"\n'
    "video_fps 30\n"
    "video_bitrate 1000000\n"
)


def test_render_defaults_golden():
    # The parser's audio values are strictly "module,device": a bare
    # module must render with a device or the line is silently ignored.
    assert Config().render() == (
        "audio_source aumem,default\naudio_player aumem,default\ncall_max_calls 2\n"
        + DEFAULT_VIDEO_TAIL
    )


def test_render_driver_with_device_golden():
    config = Config(audio_driver="aufile,/tmp/greeting.wav")
    assert config.render() == (
        "audio_source aufile,/tmp/greeting.wav\naudio_player aufile,/tmp/greeting.wav\n"
        "call_max_calls 2\n" + DEFAULT_VIDEO_TAIL
    )


def test_render_per_direction_overrides_golden():
    config = Config(
        audio_source="aufile,/tmp/greeting.wav",
        audio_player="aufile,/tmp/rec.wav",
    )
    assert config.render() == (
        "audio_source aufile,/tmp/greeting.wav\naudio_player aufile,/tmp/rec.wav\n"
        "call_max_calls 2\n" + DEFAULT_VIDEO_TAIL
    )


def test_render_one_override_keeps_the_driver_for_the_other():
    config = Config(audio_player="aufile,/tmp/rec.wav")
    assert config.render() == (
        "audio_source aumem,default\naudio_player aufile,/tmp/rec.wav\ncall_max_calls 2\n"
        + DEFAULT_VIDEO_TAIL
    )


def test_render_bare_override_gets_a_device_too():
    config = Config(audio_source="ausine")
    assert config.render() == (
        "audio_source ausine,default\naudio_player aumem,default\ncall_max_calls 2\n"
        + DEFAULT_VIDEO_TAIL
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"audio_driver": ""},
        {"audio_driver": "aumem\nsip_listen 0.0.0.0:5060"},  # line injection
        {"audio_driver": "aufile,/a path/x.wav"},  # value parsing stops at a space
        {"audio_source": ""},  # None selects the driver; empty is a mistake
        {"audio_source": "aufile,/a path/x.wav"},
        {"audio_player": ""},
        {"audio_player": "aufile\nrtp_tos 184"},  # line injection
        # The stack's fixed buffers silently truncate long values.
        {"audio_source": "a" * 16},
        {"audio_player": "aufile," + "x" * 128},
        {"audio_driver": "aufile," + "x" * 128},
        {"expose_headers": ("X-Custom", "not a header")},
        {"expose_headers": ("",)},
        {"native_log_level": "verbose"},
        {"max_concurrent_calls": 0},
        {"video_size": (0, 480)},
        {"video_size": (640,)},
        {"video_size": (True, True)},
        {"video_fps": 0},
        {"video_fps": -1.0},
        {"video_bitrate": 0},
    ],
)
def test_config_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)


@pytest.mark.parametrize("value", [True, "4", 4.0])
def test_config_rejects_non_int_call_limit(value):
    with pytest.raises(TypeError):
        Config(max_concurrent_calls=value)


def test_render_call_limit_golden():
    config = Config(max_concurrent_calls=3)
    assert "\ncall_max_calls 3\n" in config.render()


def test_render_none_means_unlimited():
    config = Config(max_concurrent_calls=None)
    assert "\ncall_max_calls 0\n" in config.render()


def test_full_config_surface_is_accepted():
    Config(expose_headers=("X-Customer-Id", "P-Asserted-Identity"), max_concurrent_calls=4)


def test_render_video_knobs_golden():
    config = Config(video_size=(320, 240), video_fps=14.5, video_bitrate=512_000)
    assert config.render().endswith(
        "video_source vidmem,default\n"
        "video_display vidmem,default\n"
        'video_size "320x240"\n'
        "video_fps 14.5\n"
        "video_bitrate 512000\n"
    )


def test_config_rejects_non_int_video_bitrate():
    with pytest.raises(TypeError):
        Config(video_bitrate=512.0)


def test_level_names_match_the_runtime():
    pytest.importorskip("baresip._native")
    from baresip.runtime import LOG_LEVELS

    assert set(LOG_LEVELS) == set(LOG_LEVEL_NAMES)
