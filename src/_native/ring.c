/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

#include <stdlib.h>
#include <string.h>

#include <re_atomic.h>

#include "ring.h"

/* The indices are free-running 64-bit counters, masked only when touching
 * the buffer: fill is always wr - rd, full versus empty needs no reserved
 * slot, and 64 bits do not wrap in any plausible lifetime.
 *
 * Ownership discipline is the whole design. wr, overruns, dropped and
 * high_water are written by the producer only; rd and underruns by the
 * consumer only. Each side release-stores its own index after touching
 * the buffer and acquire-loads the other's before — the pairing that
 * makes the copied bytes themselves visible across threads. The two
 * sides sit on separate cache lines so neither stalls the other. */
struct bp_ring {
    uint8_t *buf;
    uint32_t cap; /* power of two */
    uint32_t mask;

    /* Producer-owned. */
    _Alignas(64) RE_ATOMIC uint64_t wr;
    RE_ATOMIC uint64_t overruns;
    RE_ATOMIC uint64_t dropped;
    RE_ATOMIC uint32_t high_water;

    /* Consumer-owned. */
    _Alignas(64) RE_ATOMIC uint64_t rd;
    RE_ATOMIC uint64_t underruns;
};

bp_ring *bp_ring_alloc(uint32_t capacity)
{
    if (capacity == 0 || capacity > (1u << 30))
        return NULL;

    uint32_t cap = capacity - 1;
    cap |= cap >> 1;
    cap |= cap >> 2;
    cap |= cap >> 4;
    cap |= cap >> 8;
    cap |= cap >> 16;
    cap++;

    bp_ring *ring = calloc(1, sizeof(*ring) + cap);
    if (!ring)
        return NULL;

    ring->buf = (uint8_t *)(ring + 1);
    ring->cap = cap;
    ring->mask = cap - 1;
    return ring;
}

void bp_ring_free(bp_ring *ring)
{
    free(ring);
}

uint32_t bp_ring_write(bp_ring *ring, const uint8_t *src, uint32_t len)
{
    if (!ring || !src)
        return 0;

    uint64_t rd = re_atomic_acq(&ring->rd);
    uint64_t wr = re_atomic_rlx(&ring->wr);
    uint32_t space = ring->cap - (uint32_t)(wr - rd);
    uint32_t n = len < space ? len : space;

    if (n) {
        uint32_t off = (uint32_t)wr & ring->mask;
        uint32_t first = ring->cap - off;
        if (first > n)
            first = n;
        memcpy(ring->buf + off, src, first);
        memcpy(ring->buf, src + first, n - first);
        re_atomic_rls_set(&ring->wr, wr + n);

        uint32_t fill = (uint32_t)(wr + n - rd);
        if (fill > re_atomic_rlx(&ring->high_water))
            re_atomic_rlx_set(&ring->high_water, fill);
    }

    if (n < len) {
        re_atomic_rlx_add(&ring->overruns, 1);
        re_atomic_rlx_add(&ring->dropped, len - n);
    }
    return n;
}

uint32_t bp_ring_read(bp_ring *ring, uint8_t *dst, uint32_t len)
{
    if (!ring || !dst)
        return 0;

    uint64_t wr = re_atomic_acq(&ring->wr);
    uint64_t rd = re_atomic_rlx(&ring->rd);
    uint32_t avail = (uint32_t)(wr - rd);
    uint32_t n = len < avail ? len : avail;

    if (n) {
        uint32_t off = (uint32_t)rd & ring->mask;
        uint32_t first = ring->cap - off;
        if (first > n)
            first = n;
        memcpy(dst, ring->buf + off, first);
        memcpy(dst + first, ring->buf, n - first);
        re_atomic_rls_set(&ring->rd, rd + n);
    }

    if (n < len)
        re_atomic_rlx_add(&ring->underruns, 1);
    return n;
}

uint32_t bp_ring_size(const bp_ring *ring)
{
    if (!ring)
        return 0;

    /* rd before wr: on the producer thread the stale rd only understates
     * free space, on the consumer thread the stale wr only understates
     * readable bytes — both err safe. A third thread can catch the pair
     * mid-motion, hence the clamp. */
    uint64_t rd = re_atomic_acq(&ring->rd);
    uint64_t wr = re_atomic_acq(&ring->wr);
    uint64_t fill = wr - rd;
    return fill > ring->cap ? ring->cap : (uint32_t)fill;
}

uint32_t bp_ring_capacity(const bp_ring *ring)
{
    return ring ? ring->cap : 0;
}

void bp_ring_stats_get(const bp_ring *ring, struct bp_ring_stats *stats)
{
    if (!ring || !stats)
        return;

    stats->underruns = re_atomic_rlx(&ring->underruns);
    stats->overruns = re_atomic_rlx(&ring->overruns);
    stats->dropped = re_atomic_rlx(&ring->dropped);
    stats->high_water = re_atomic_rlx(&ring->high_water);
}
