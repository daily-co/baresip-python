#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Runtime tests: command futures, concurrency, the one-per-process rule,
SIP-thread death handling, and the failure-loudness contract (each
ERROR-class failure produces exactly one structured record at the
contracted severity, carrying its correlation extras)."""

import asyncio
import errno
import logging

import pytest

from baresip import BaresipError, CommandQueueFull, CommandTimeout, RuntimeDead

native = pytest.importorskip("baresip._native")
ffi = native.ffi
lib = native.lib

from baresip.runtime import Runtime  # imports the extension, hence after the skip

UNKNOWN_CMD = 999  # no case in cmd_handler: sent fine, never completes


async def test_echo_command_resolves_future():
    runtime = Runtime()
    await runtime.start()
    try:
        ev, payload = await runtime.cmd(lib.BP_CMD_PING)
        assert ev == lib.BP_EV_PONG
        assert payload is None
    finally:
        await runtime.close()


async def test_50_concurrent_commands():
    runtime = Runtime()
    await runtime.start()
    try:
        results = await asyncio.gather(*(runtime.cmd(lib.BP_CMD_PING) for _ in range(50)))
        assert len(results) == 50
        assert all(ev == lib.BP_EV_PONG for ev, _ in results)
        assert not runtime._pending, "every future must be consumed"
    finally:
        await runtime.close()


async def test_second_runtime_raises_while_first_is_active():
    runtime = Runtime()
    await runtime.start()
    try:
        with pytest.raises(BaresipError, match="only one"):
            await Runtime().start()
    finally:
        await runtime.close()


async def test_restart_after_clean_close_is_allowed():
    first = Runtime()
    await first.start()
    await first.close()

    second = Runtime()
    await second.start()
    try:
        ev, _ = await second.cmd(lib.BP_CMD_PING)
        assert ev == lib.BP_EV_PONG
    finally:
        await second.close()


async def test_audio_drivers_query_reports_aumem():
    runtime = Runtime()
    await runtime.start()
    try:
        _, payload = await runtime.cmd(lib.BP_CMD_AUDIO_DRIVERS)
        import json

        registered = json.loads(payload)
        assert "aumem" in registered["ausrc"]
        assert "aumem" in registered["auplay"]
    finally:
        await runtime.close()


async def test_typoed_audio_driver_fails_start_loudly():
    from baresip import Config

    runtime = Runtime()
    with pytest.raises(BaresipError, match="aumen.*not a registered audio source"):
        await runtime.start(Config(audio_driver="aumen"))

    # The failed start tore down cleanly: a fresh runtime works.
    second = Runtime()
    await second.start()
    try:
        ev, _ = await second.cmd(lib.BP_CMD_PING)
        assert ev == lib.BP_EV_PONG
    finally:
        await second.close()


async def test_dead_sip_thread_fails_pending_and_poisons_process():
    runtime = Runtime()
    await runtime.start()
    died = asyncio.Event()
    runtime.on_dead = died.set

    pending = asyncio.create_task(runtime.cmd(UNKNOWN_CMD, timeout=30))
    await asyncio.sleep(0.05)  # let the command get queued

    # Kill the loop behind the runtime's back: an unexpected re_main exit.
    assert lib.bp_cmd(lib.BP_CMD_STOP, 0, ffi.NULL) == 0
    await asyncio.wait_for(died.wait(), timeout=5)

    with pytest.raises(RuntimeDead):
        await pending
    with pytest.raises(RuntimeDead):
        await runtime.cmd(lib.BP_CMD_PING)
    with pytest.raises(RuntimeDead):
        await Runtime().start()

    runtime._thread.join(timeout=5)
    assert not runtime._thread.is_alive()


# -- failure-loudness contract -------------------------------------------------


async def test_queue_full_is_one_error_record_with_extras(caplog):
    runtime = Runtime()
    await runtime.start()
    try:
        runtime._push_cmd = lambda *args: errno.EAGAIN
        with (
            caplog.at_level(logging.ERROR, logger="baresip.runtime"),
            pytest.raises(CommandQueueFull),
        ):
            await runtime.cmd(lib.BP_CMD_PING)
        records = [r for r in caplog.records if "queue full" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert records[0].cmd == lib.BP_CMD_PING
        assert records[0].seq > 0
    finally:
        runtime._push_cmd = lib.bp_cmd
        await runtime.close()


async def test_command_timeout_is_one_error_record(caplog):
    runtime = Runtime()
    await runtime.start()
    try:
        with (
            caplog.at_level(logging.ERROR, logger="baresip.runtime"),
            pytest.raises(CommandTimeout),
        ):
            await runtime.cmd(UNKNOWN_CMD, timeout=0.2)
        records = [r for r in caplog.records if "no completion" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert records[0].cmd == UNKNOWN_CMD
    finally:
        await runtime.close()


async def test_unexpected_c_error_is_one_error_record(caplog):
    runtime = Runtime()
    await runtime.start()
    try:
        runtime._push_cmd = lambda *args: errno.EIO
        with (
            caplog.at_level(logging.ERROR, logger="baresip.runtime"),
            pytest.raises(BaresipError, match="failed"),
        ):
            await runtime.cmd(lib.BP_CMD_PING)
        records = [r for r in caplog.records if "command failed" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
    finally:
        runtime._push_cmd = lib.bp_cmd
        await runtime.close()


async def test_drain_requires_a_running_runtime():
    rt = Runtime()
    with pytest.raises(BaresipError, match="cannot drain"):
        await rt.drain()
    assert not rt.draining
