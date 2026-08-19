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
#define BP_CMD_SET_LOG_LEVEL 3 /* json_args: a BP_LOG_* value, in decimal */
#define BP_CMD_SET_SIP_TRACE 4 /* json_args: "1" to enable, "0" to disable */

/* Event ids delivered to bp_event_h. */
#define BP_EV_PONG 1
#define BP_EV_DONE 2 /* a command that carries no result finished */

/* Severity of a native log line. The stack underneath has two logging
 * systems with different scales; both are mapped onto these four. */
#define BP_LOG_DEBUG 0
#define BP_LOG_INFO 1
#define BP_LOG_WARN 2
#define BP_LOG_ERROR 3

/* Which stream a captured line came from. */
#define BP_LOG_CH_MAIN 0
#define BP_LOG_CH_SIP 1 /* SIP messages, when tracing is enabled */

/* Longest line the stack can produce, NUL included. */
#define BP_LOG_LINE_MAX 8192

struct bp_log_rec {
    uint32_t level;   /* BP_LOG_DEBUG..BP_LOG_ERROR   */
    uint32_t channel; /* BP_LOG_CH_*                  */
    uint32_t dropped; /* lines lost since the last read, see below */
    uint32_t len;     /* bytes in msg, NUL excluded   */
    char msg[BP_LOG_LINE_MAX];
};

const char *bp_version(void);

/* Process-wide init/close of the underlying stack. bp_init must be called
 * once, before the first loop starts; bp_close once, after the last loop
 * has finished. (This pair will grow configuration handling as the binding
 * gains features.) Returns 0 on success, an errno otherwise. */
int bp_init(void);
void bp_close(void);

/* Lifecycle — all three MUST be called on the re thread, in this order:
 *
 *   bp_loop_init();   brings up the SIP stack (see below)
 *   bp_loop_run();    re_main — blocks until a BP_CMD_STOP is processed
 *   bp_loop_done();   tears the stack back down
 *
 * bp_loop_init takes the directory the stack may use for its own files —
 * it must exist, and confines every module to it, so no module can reach
 * the invoking user's home directory — the configuration text to apply,
 * and the initial native log level (a BP_LOG_* value).
 *
 * If bp_loop_init fails it unwinds everything it had brought up, leaving
 * the process as it found it: do NOT call bp_loop_done after a failure.
 *
 * bp_loop_done must only run after bp_loop_run has returned. Concurrent
 * bp_cmd callers need no coordination: an internal mutex pairs the gate
 * check with the push, and pairs closing the gate with freeing the queue,
 * so a push racing teardown gets ESHUTDOWN instead of touching freed
 * memory. On top of that, each lifecycle function refuses misordered
 * calls with a loud stderr message and an errno instead of corrupting
 * state:
 *
 *   bp_loop_init   EALREADY  queue already allocated (double init)
 *   bp_loop_run    EINVAL    never initialized
 *   bp_loop_run    EALREADY  loop already running
 *   bp_loop_done   EINVAL    never initialized — nothing to tear down
 *   bp_loop_done   EBUSY     loop still running — freeing now would be
 *                            the use-after-free described above
 *
 * All three return 0 on success, an errno otherwise.
 */
int bp_loop_init(const char *conf_dir, const char *config_text, int log_level);
int bp_loop_run(void);
int bp_loop_done(void);

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

/* Native log capture.
 *
 * The stack writes to stdout and stderr of its own accord, which a library
 * has no business doing, so both are redirected into a buffer here and
 * handed to whoever calls bp_log_read.
 *
 * The redirect cannot deliver lines the way events are delivered: log calls
 * arrive on ANY thread, including the audio threads, and those must never
 * wait on anything Python does. So writers only ever append to the buffer,
 * and a reader on its own thread collects from it.
 *
 * Writers never block and never wait for space: when the buffer is full the
 * line is dropped and counted, and the count rides out with the next record
 * read, so a reader too slow to keep up learns exactly how much it missed.
 *
 * bp_log_start   begin buffering, discarding anything left from before;
 *                until it is called, lines are dropped without counting.
 * bp_log_stop    stop buffering and wake the reader.
 * bp_log_read    fill rec with the next line, blocking until one is
 *                available. Returns 1 when it filled rec, or 0 once
 *                bp_log_stop has been called AND the buffer is drained —
 *                so a reader that loops until 0 always gets the tail.
 */
void bp_log_start(void);
void bp_log_stop(void);
int bp_log_read(struct bp_log_rec *rec);

#endif /* BP_SHIM_H */
