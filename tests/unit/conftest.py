#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def reset_runtime_class():
    """Tests exercise death and poisoning; reset the process-level guards
    so each test starts from a clean slate."""
    yield
    try:
        from baresip.runtime import Runtime
    except ImportError:
        return  # no built extension; nothing to reset
    Runtime._active = None
    Runtime._process_poisoned = False
