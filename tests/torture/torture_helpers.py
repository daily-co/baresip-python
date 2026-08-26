#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""The torture harness: seeded adversarial repetition.

Races live in transitions, not steady state — these suites hunt the
rare-interleaving bug class that ordinary tests never see, by repeating
transitions thousands of times with a seeded RNG steering the timing.

The rules that keep torture useful rather than flaky-CI theater:

- Every run logs its PRNG seed, and ``TORTURE_SEED=<n>`` reproduces it
  exactly. A torture failure is **by definition a real bug**: no
  auto-retries, and quarantine only with a filed issue carrying the
  seed.
- Resource use is asserted bounded at fixed intervals *during* the run
  (RSS, file descriptors, handle-table slots), so a leak fails the
  iteration window that caused it, not some later victim.
- ``TORTURE_SCALE`` scales the iteration counts: the nightly lane runs
  at 1.0, the per-push lite lane at a tenth.
"""

import asyncio
import json
import os
import subprocess

import pytest

native = pytest.importorskip("baresip._native")
lib = native.lib


def scaled(n: int) -> int:
    """n iterations scaled by TORTURE_SCALE (never below 1)."""
    return max(1, int(n * float(os.environ.get("TORTURE_SCALE", "1"))))


def rss_kb() -> int:
    """Resident set size, portably (same probe as the soak test)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except FileNotFoundError:
        pass
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=True
    )
    return int(out.stdout.strip())


def fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except FileNotFoundError:
        return len(os.listdir("/dev/fd"))


async def handle_counts(runtime) -> dict:
    """Live handle-table slots by type, from the C side."""
    _, payload = await runtime.cmd(lib.BP_CMD_TEST_HANDLE_COUNT)
    return json.loads(payload)


class Bounds:
    """Periodic resource assertions against a baseline.

    RSS may grow a bounded fraction over the whole run (allocator slack
    is real); file descriptors get a small absolute allowance.
    """

    def __init__(self, *, rss_growth: float = 1.10, fd_slack: int = 16):
        self.rss0 = rss_kb()
        self.fd0 = fd_count()
        self.rss_growth = rss_growth
        self.fd_slack = fd_slack
        # Under ASan (the sanitized lane exports ASAN_OPTIONS), RSS is
        # not a leak signal: the quarantine retains freed memory by
        # design, so growth there is expected and unbounded-looking.
        # That lane's leak detection is LSan's job; the RSS curve is
        # asserted only in plain builds.
        self.rss_asserted = "ASAN_OPTIONS" not in os.environ

    def check(self, where: str, *, allowance_kb: int = 0) -> None:
        """allowance_kb: absolute extra budget on top of the relative
        limit — for creep that is known, measured, and tracked in a
        filed issue (the torture rules' quarantine shape)."""
        if self.rss_asserted:
            rss = rss_kb()
            limit = self.rss0 * self.rss_growth + allowance_kb
            assert rss <= limit, (
                f"{where}: RSS grew {self.rss0} -> {rss} KiB "
                f"(limit {self.rss_growth:.0%} + {allowance_kb} KiB)"
            )
        fds = fd_count()
        assert fds <= self.fd0 + self.fd_slack, (
            f"{where}: fd count grew {self.fd0} -> {fds} (slack {self.fd_slack})"
        )


async def expect_call_slots_empty(runtime, where: str, *, ua: int) -> None:
    counts = await handle_counts(runtime)
    assert counts.get("call", 0) == 0, f"{where}: leaked call slots: {counts}"
    assert counts.get("ua", 0) == ua, f"{where}: unexpected ua slots: {counts}"


async def drain_loop() -> None:
    """Give queued event callbacks a chance to run."""
    for _ in range(3):
        await asyncio.sleep(0)
