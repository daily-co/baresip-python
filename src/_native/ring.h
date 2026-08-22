/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef BP_RING_H
#define BP_RING_H

#include <stdint.h>

/* A lock-free single-producer single-consumer byte ring.
 *
 * The concurrency contract: at most one thread writes and at most one
 * thread reads at any moment — the ring orders their memory accesses
 * (acquire/release on the two indices) but arbitrates nothing beyond
 * that. size(), capacity() and stats_get() are safe from any thread.
 *
 * Payload-agnostic by design: it moves bytes and knows nothing of frames
 * or samples. The audio drivers put raw PCM through it; a payload that
 * needs framing carries its own header in-band.
 *
 * Short transfers are a signal, not an error: write() takes what fits and
 * read() returns what is there, each recording the shortfall in the
 * counters. What a short transfer *means* — drop, silence, retry — is
 * policy, and policy lives in the caller.
 */

typedef struct bp_ring bp_ring;

struct bp_ring_stats {
    uint64_t underruns;  /* reads that returned less than asked   */
    uint64_t overruns;   /* writes that could not take everything */
    uint64_t dropped;    /* bytes short writes turned away        */
    uint32_t high_water; /* deepest fill the producer observed    */
};

/* Allocate a ring holding at least `capacity` bytes — rounded up to the
 * next power of two, every byte usable. Returns NULL when capacity is 0,
 * absurd (over 1 GiB), or memory is short. Free with bp_ring_free
 * (NULL-safe); freeing a ring another thread is still using is a caller
 * bug the ring cannot defend against. */
bp_ring *bp_ring_alloc(uint32_t capacity);
void bp_ring_free(bp_ring *ring);

/* Producer side: append up to len bytes, return how many fit. */
uint32_t bp_ring_write(bp_ring *ring, const uint8_t *src, uint32_t len);

/* Consumer side: remove up to len bytes, return how many were there. */
uint32_t bp_ring_read(bp_ring *ring, uint8_t *dst, uint32_t len);

/* Bytes currently readable / total capacity. size() errs in the safe
 * direction on both ends — the consumer never sees more than is readable,
 * the producer never sees more free space than exists — and is a mere
 * snapshot on any third thread. */
uint32_t bp_ring_size(const bp_ring *ring);
uint32_t bp_ring_capacity(const bp_ring *ring);

/* Snapshot the counters. Monotonic, never reset. */
void bp_ring_stats_get(const bp_ring *ring, struct bp_ring_stats *stats);

#endif /* BP_RING_H */
