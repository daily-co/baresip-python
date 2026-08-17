#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Tests for the per-call verbosity filter."""

import logging

from baresip.logging_utils import PerCallFilter


def _record(level, **extra):
    record = logging.LogRecord("baresip.test", level, __file__, 1, "msg", None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_info_and_above_always_pass():
    f = PerCallFilter()
    assert f.filter(_record(logging.INFO))
    assert f.filter(_record(logging.ERROR, sip_call_id="abc"))


def test_debug_blocked_unless_traced():
    f = PerCallFilter()
    assert not f.filter(_record(logging.DEBUG, sip_call_id="abc"))
    f.trace("abc")
    assert f.filter(_record(logging.DEBUG, sip_call_id="abc"))
    assert f.filter(_record(logging.DEBUG, call="abc"))
    assert not f.filter(_record(logging.DEBUG, sip_call_id="other"))
    f.untrace("abc")
    assert not f.filter(_record(logging.DEBUG, sip_call_id="abc"))


def test_debug_without_ids_is_blocked():
    f = PerCallFilter()
    f.trace("abc")
    assert not f.filter(_record(logging.DEBUG))
