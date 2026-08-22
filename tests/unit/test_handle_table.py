#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""The handle table: Python's only way to name a stack object.

A handle packs a slot index with an 8-bit generation. These tests drive the
table through its test commands and pin the whole contract: a live handle
resolves, a dropped one fails typed, a reused slot invalidates the old
handle — and the one honest limitation, the generation wrapping at 256, is
pinned as a fact rather than left as a surprise.
"""

import json

import pytest

native = pytest.importorskip("baresip._native")
lib = native.lib

from baresip.runtime import Runtime  # imports the extension, hence after the skip

SLOT_MASK = 0xFFFFFF


@pytest.fixture
async def runtime():
    rt = Runtime()
    await rt.start()
    yield rt
    await rt.close()


async def new_handle(rt) -> int:
    ev, payload = await rt.cmd(lib.BP_CMD_TEST_HANDLE_NEW)
    assert ev == lib.BP_EV_DONE
    handle = json.loads(payload)["handle"]
    assert handle != 0, "table full — nothing else allocates in this test"
    return handle


async def drop(rt, handle: int) -> int:
    ev, _ = await rt.cmd(lib.BP_CMD_TEST_HANDLE_DROP, args=str(handle))
    return ev


async def probe(rt, handle: int) -> int:
    ev, _ = await rt.cmd(lib.BP_CMD_TEST_HANDLE_PROBE, args=str(handle))
    return ev


async def test_live_handle_resolves(runtime):
    handle = await new_handle(runtime)
    assert await probe(runtime, handle) == lib.BP_EV_DONE


async def test_dropped_handle_fails_typed(runtime):
    handle = await new_handle(runtime)
    assert await drop(runtime, handle) == lib.BP_EV_DONE
    assert await probe(runtime, handle) == lib.BP_EV_STALE_HANDLE
    # Dropping twice is the same misuse as probing: typed, not corrupting.
    assert await drop(runtime, handle) == lib.BP_EV_STALE_HANDLE


async def test_garbage_handles_fail_typed(runtime):
    for handle in (0, 1, 0xFFFFFFFF, (1 << 24) | 0xFFFFFF):
        assert await probe(runtime, handle) == lib.BP_EV_STALE_HANDLE


async def test_slot_reuse_invalidates_the_old_handle(runtime):
    first = await new_handle(runtime)
    await drop(runtime, first)
    second = await new_handle(runtime)

    assert (first & SLOT_MASK) == (second & SLOT_MASK), "expected the slot to be reused"
    assert first != second, "the generation must differ"
    assert await probe(runtime, first) == lib.BP_EV_STALE_HANDLE
    assert await probe(runtime, second) == lib.BP_EV_DONE


async def test_generation_wraparound(runtime):
    """The 8-bit generation, both sides of it: hundreds of reuses still
    fail typed — and at exactly 256 reuses of one slot, an ancient handle
    validates falsely. That limitation is accepted by design; this pins it
    so a change to the packing shows up as a failing test."""
    first = await new_handle(runtime)
    slot = first & SLOT_MASK
    await drop(runtime, first)

    # 299 more recycles of the same slot: 300 total generations advanced.
    for _ in range(299):
        handle = await new_handle(runtime)
        assert (handle & SLOT_MASK) == slot, "expected the same slot every time"
        await drop(runtime, handle)
    assert await probe(runtime, first) == lib.BP_EV_STALE_HANDLE

    # An empty slot never validates, whatever its generation — so complete
    # the second lap to exactly 512 = 2·256 generations and leave a LIVE
    # object at the wrapped generation. The ancient handle now names it.
    for _ in range(212):
        handle = await new_handle(runtime)
        await drop(runtime, handle)
    revenant = await new_handle(runtime)
    assert revenant == first, "the collision itself: same slot, same generation"
    assert await probe(runtime, first) == lib.BP_EV_DONE
