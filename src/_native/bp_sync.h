/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

/*
 * Locking wrappers for this extension's C sources.
 *
 * ThreadSanitizer cannot see glibc's C11 <threads.h> synchronization:
 * mtx_lock/mtx_unlock reach pthreads through internal aliases that
 * bypass TSan's interceptors (verified empirically with gcc 14 and
 * clang 19 — a mutex-guarded counter reports as a data race), so every
 * mtx_-guarded access in this extension would be a false positive under
 * the TSan lane. These wrappers add the happens-before edges explicitly
 * under TSan and compile to the bare calls in every other build.
 *
 * Use them for every mutex in this extension; a raw mtx_lock defeats
 * the TSan lane. (thrd_create/thrd_join have the same visibility gap —
 * today no TSan-scoped test reaches them, but expanding the lane onto
 * code that does will need the same treatment.)
 */

#ifndef BP_SYNC_H
#define BP_SYNC_H

#include <re.h>

#if defined(__SANITIZE_THREAD__)
#define BP_TSAN 1
#elif defined(__has_feature)
#if __has_feature(thread_sanitizer)
#define BP_TSAN 1
#endif
#endif

#ifdef BP_TSAN
/* Provided by the TSan runtime; declared here so the header works with
 * toolchains that do not ship <sanitizer/tsan_interface.h>. */
void __tsan_acquire(void *addr);
void __tsan_release(void *addr);
#endif

static inline void bp_mtx_lock(mtx_t *m)
{
    mtx_lock(m);
#ifdef BP_TSAN
    __tsan_acquire(m);
#endif
}

static inline void bp_mtx_unlock(mtx_t *m)
{
#ifdef BP_TSAN
    __tsan_release(m);
#endif
    mtx_unlock(m);
}

static inline void bp_cnd_wait(cnd_t *c, mtx_t *m)
{
#ifdef BP_TSAN
    __tsan_release(m);
#endif
    cnd_wait(c, m);
#ifdef BP_TSAN
    __tsan_acquire(m);
#endif
}

#endif /* BP_SYNC_H */
