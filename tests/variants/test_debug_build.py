#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Assertions for a ``--debug-modules`` native build.

The interactive/debug modules (menu, stdio, cons) may be compiled into a
local debugging build, but the production runtime must never activate them:
the only module-load path is the fixed table in shim.c, which names exactly
the default set. This test witnesses that from the outside. Against a
default build — where the debug modules are not even compiled — it passes
trivially, so it is safe to run anywhere.
"""

import socket

import pytest

from baresip import Runtime

pytest.importorskip("baresip._native")

CONS_PORT = 5555  # the cons module's default command port


async def test_production_runtime_activates_no_debug_modules():
    runtime = Runtime()
    await runtime.start()
    try:
        # cons is the debug module with an outside-observable activation
        # signature: it listens on its command port the moment it loads.
        # All three debug modules share the single load path (the module
        # table in shim.c), so cons staying dark witnesses the whole set.
        with (
            pytest.raises(OSError),
            socket.create_connection(("127.0.0.1", CONS_PORT), timeout=1.0),
        ):
            pass
    finally:
        await runtime.close()
