/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef BP_SHIM_H
#define BP_SHIM_H

#include <stdint.h>

/* The C surface exposed to Python. Every function is prefixed bp_ to keep
 * our symbols distinct from libre's and libbaresip's.
 *
 * Threading model: libre's event loop (re_main) runs on one dedicated
 * thread — "the re thread" — and every libre/libbaresip object is touched
 * only from that thread. The sole entry points from other threads are
 * bp_cmd(), which marshals a command onto the re thread through a pipe-based
 * mqueue, and the version query. Events flow the other way through the
 * cffi-provided bp_event_h callback, invoked on the re thread.
 */

/* Command ids accepted by bp_cmd. */
#define BP_CMD_PING 1
#define BP_CMD_STOP 2

/* Event ids delivered to bp_event_h. */
#define BP_EV_PONG 1

const char *bp_version(void);

/* Process-wide init/close of the underlying stack. bp_init must be called
 * once, before the first loop starts; bp_close once, after the last loop
 * has finished. (This pair will grow configuration handling as the binding
 * gains features.) Returns 0 on success, an errno otherwise. */
int bp_init(void);
void bp_close(void);

/* Lifecycle — all three MUST be called on the re thread, in this order:
 *
 *   bp_loop_init();   re_thread_init + command-queue allocation
 *   bp_loop_run();    re_main — blocks until a BP_CMD_STOP is processed
 *   bp_loop_done();   frees the command queue, re_thread_close
 *
 * bp_loop_done must only run after bp_loop_run has returned and every
 * bp_cmd caller has quiesced: a push into a freed queue is a use-after-
 * free. The Python runtime enforces that ordering.
 *
 * Both int-returning functions return 0 on success, an errno otherwise.
 */
int bp_loop_init(void);
int bp_loop_run(void);
void bp_loop_done(void);

/* Queue a command for the re thread. Callable from ANY thread.
 *
 * json_args may be NULL; when given it is copied before returning, so the
 * caller's buffer has no lifetime constraints.
 *
 * Returns 0 on success or an errno; the command is NOT queued on failure:
 *   EAGAIN     the pipe is full — the caller must handle the loss
 *   ESHUTDOWN  the loop is not running (never started, stopping, stopped)
 */
int bp_cmd(int cmd, uint32_t handle, const char *json_args);

/* Event funnel out to Python; implemented by cffi (extern "Python+C"),
 * always invoked on the re thread. json may be NULL. */
void bp_event_h(int ev, uint32_t handle, const char *json);

#endif /* BP_SHIM_H */
