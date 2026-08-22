#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""The SPSC byte ring under the audio path.

Pure data-structure tests: no Runtime, no re thread. The ring is driven
directly through the extension, including from two real threads at once —
cffi releases the GIL around C calls, so the producer and consumer
genuinely overlap inside the ring's memcpys and the acquire/release
pairing is exercised for real, not just in theory.
"""

import hashlib
import random
import threading
import time

import pytest

native = pytest.importorskip("baresip._native")
ffi = native.ffi
lib = native.lib


@pytest.fixture
def ring():
    rings = []

    def make(capacity):
        r = lib.bp_ring_alloc(capacity)
        assert r != ffi.NULL
        rings.append(r)
        return r

    yield make
    for r in rings:
        lib.bp_ring_free(r)


def stats(r):
    st = ffi.new("struct bp_ring_stats *")
    lib.bp_ring_stats_get(r, st)
    return st


def read_bytes(r, n):
    buf = ffi.new("uint8_t[]", n)
    got = lib.bp_ring_read(r, buf, n)
    return bytes(ffi.buffer(buf, got))


def test_capacity_rounds_up_to_a_power_of_two(ring):
    assert lib.bp_ring_capacity(ring(1000)) == 1024
    assert lib.bp_ring_capacity(ring(16)) == 16
    assert lib.bp_ring_capacity(ring(1)) == 1


def test_absurd_capacities_are_refused():
    assert lib.bp_ring_alloc(0) == ffi.NULL
    assert lib.bp_ring_alloc((1 << 30) + 1) == ffi.NULL
    lib.bp_ring_free(ffi.NULL)  # NULL-safe, like free()


def test_data_survives_wraparound(ring):
    # A 16-byte ring fed 7 bytes at a time: the offsets cross the
    # power-of-two boundary every few iterations, splitting the copies
    # at every possible position.
    r = ring(16)
    feed = random.Random(7).randbytes(7 * 100)
    out = bytearray()
    for i in range(100):
        assert lib.bp_ring_write(r, feed[i * 7 : (i + 1) * 7], 7) == 7
        out += read_bytes(r, 7)
    assert bytes(out) == feed
    assert lib.bp_ring_size(r) == 0


def test_counter_accounting(ring):
    r = ring(64)

    # A write bigger than the ring: 64 taken, the rest turned away.
    data = bytes(range(80))
    assert lib.bp_ring_write(r, data, 80) == 64
    st = stats(r)
    assert st.overruns == 1
    assert st.dropped == 16
    assert st.high_water == 64
    assert lib.bp_ring_size(r) == 64

    # A read bigger than the content: short read, one underrun.
    assert read_bytes(r, 80) == data[:64]
    assert stats(r).underruns == 1

    # Reading from empty is a short read too.
    assert read_bytes(r, 10) == b""
    st = stats(r)
    assert st.underruns == 2
    # The producer-side counters were untouched by all that reading.
    assert st.overruns == 1
    assert st.dropped == 16


def test_high_water_records_the_deepest_fill(ring):
    r = ring(64)
    lib.bp_ring_write(r, b"x" * 10, 10)
    read_bytes(r, 10)
    lib.bp_ring_write(r, b"y" * 20, 20)
    assert stats(r).high_water == 20
    read_bytes(r, 20)
    lib.bp_ring_write(r, b"z" * 5, 5)
    assert stats(r).high_water == 20  # a shallower fill never lowers it


def test_threaded_10mb_checksum_through_a_16kb_ring(ring):
    # Both sides size each transfer from bp_ring_size() first — which errs
    # in the safe direction on the ring's own two threads — so every byte
    # must arrive exactly once and every counter must stay at zero.
    r = ring(16 * 1024)
    cap = lib.bp_ring_capacity(r)
    total = 10 * 1024 * 1024
    data = random.Random(42).randbytes(total)
    chunk = 8192
    digest = hashlib.sha256()
    failures = []

    def produce():
        view = memoryview(data)
        off = 0
        while off < total:
            n = min(chunk, total - off, cap - lib.bp_ring_size(r))
            if n == 0:
                time.sleep(0)  # ring full — let the consumer run
                continue
            wrote = lib.bp_ring_write(r, ffi.from_buffer("uint8_t[]", view[off : off + n]), n)
            if wrote != n:
                failures.append(f"short write: {wrote} != {n}")
                return
            off += wrote

    def consume():
        got = 0
        buf = ffi.new("uint8_t[]", chunk)
        while got < total:
            avail = lib.bp_ring_size(r)
            if avail == 0:
                time.sleep(0)  # ring empty — let the producer run
                continue
            n = lib.bp_ring_read(r, buf, min(chunk, avail))
            digest.update(ffi.buffer(buf, n))
            got += n

    producer = threading.Thread(target=produce, daemon=True)
    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    producer.start()
    producer.join(timeout=60)
    consumer.join(timeout=60)
    assert not producer.is_alive() and not consumer.is_alive(), "transfer stalled"
    assert not failures

    assert digest.hexdigest() == hashlib.sha256(data).hexdigest()
    st = stats(r)
    assert st.overruns == 0
    assert st.dropped == 0
    assert st.underruns == 0
    assert 0 < st.high_water <= cap
