/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef BP_SHIM_H
#define BP_SHIM_H

#include <stdint.h>

/* Part of the surface, carried through this funnel header: the SPSC byte
 * ring under the audio path — a pure data structure with no loop or
 * thread ties of its own. */
#include "ring.h"

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
#define BP_CMD_SET_LOG_LEVEL 3      /* json_args: a BP_LOG_* value, in decimal */
#define BP_CMD_SET_SIP_TRACE 4      /* json_args: "1" to enable, "0" to disable */
#define BP_CMD_SET_EXPOSE_HEADERS 5 /* json_args: comma-separated header names */

/* User-agent commands. UA_ALLOC's DONE payload is {"handle":N} on success
 * or {"error":...} on failure. REGISTER/UNREGISTER complete when the
 * request is issued; the outcome arrives later as REGISTER_OK /
 * REGISTER_FAIL stack events carrying the status line as text. */
#define BP_CMD_UA_ALLOC 6      /* json_args: an account AOR */
#define BP_CMD_UA_REGISTER 7   /* json_args: a UA handle, in decimal */
#define BP_CMD_UA_UNREGISTER 8 /* json_args: a UA handle, in decimal */

/* Call commands. Args: a call handle in decimal; ANSWER takes "HANDLE V"
 * where V=1 accepts with video (a hook for later — inert while the build
 * carries no video codecs). Reject and hangup complete when the response
 * or BYE is issued; the CALL_CLOSED stack event follows immediately and
 * is what invalidates the handle. */
#define BP_CMD_CALL_ANSWER 9
#define BP_CMD_CALL_REJECT 10 /* answers 486 Busy Here */
#define BP_CMD_CALL_HANGUP 11

/* Dial out. Args: first line "HANDLE V URI" (V=1 offers video — inert
 * while the build carries no video codecs); each further line is one
 * "Name: value" header for the INVITE. DONE payload: {"handle":N} or
 * {"error":...}. Progress and outcome arrive as stack events. */
#define BP_CMD_UA_CONNECT 12

/* Send one DTMF key. Args: "HANDLE K" where K is a digit [0-9A-D*#] to
 * press, or "R" to release the pressed key. In RTP telephone-event mode
 * the release ends the event on the wire, so a press must be followed by
 * a release; in SIP INFO mode the press sends the whole message and the
 * release is a no-op. Pacing between press and release — the tone
 * duration the far end perceives — is the caller's. */
#define BP_CMD_CALL_SEND_DIGIT 13

/* Hold or resume. Args: "HANDLE H" where H=1 holds, 0 resumes. Completes
 * when call_hold returns; the re-INVITE itself is confirmed only by the
 * CALL_LOCAL_SDP "offer" stack event, because the stack silently skips
 * the re-INVITE (and still returns 0) while another session refresh is
 * in flight. The peer's answer arrives as events. */
#define BP_CMD_CALL_HOLD 14

/* Blind transfer. Args: "HANDLE URI". Completes when the in-dialog REFER
 * is issued; the outcome arrives as events — CALL_TRANSFER_FAILED with
 * "CODE reason" text, or, on success, CALL_CLOSED with the stack's exact
 * text "Call transfered" (the transferred call ends; there is no
 * separate success event). The stack tracks one REFER subscription per
 * call, so callers serialize transfers. */
#define BP_CMD_CALL_TRANSFER 15

/* Attended transfer. Args: "HANDLE CONSULT_HANDLE" — the REFER (with a
 * Replaces header naming the consult call's dialog) goes out on the
 * first call. Refused up front with {"error":"replaces_unsupported"}
 * when the first call's peer never advertised Replaces support.
 * Outcome reporting is identical to BP_CMD_CALL_TRANSFER. */
#define BP_CMD_CALL_REPLACE_TRANSFER 16

/* Act on a received transfer request (the CALL_TRANSFER event). The
 * stack has already 202-accepted the REFER by the time the event is
 * reported, so these speak through the implicit subscription's NOTIFY
 * sipfrags. ACCEPT args: "HANDLE RAW_REFER_TO" (rest of line) — dials
 * the target with the xcall linkage so the core ties the original
 * leg's fate to the new call; DONE payload {"handle":N} names the new
 * call. REJECT args: "HANDLE STATUS" — sends the final failing sipfrag
 * (out-of-range statuses fall back to 603) and the call continues. */
#define BP_CMD_CALL_TRANSFER_ACCEPT 17
#define BP_CMD_CALL_TRANSFER_REJECT 18

/* Test-only commands: fixed inputs in, observable events out, so the paths
 * under them — the JSON encoder, the handle table, header extraction — are
 * testable without network traffic. Harmless if sent in production. */
#define BP_CMD_TEST_EMIT 100         /* json_args: a raw SIP message */
#define BP_CMD_TEST_ESCAPE 101       /* json_args: bytes for the JSON encoder */
#define BP_CMD_TEST_HANDLE_NEW 102   /* allocates a dummy table entry */
#define BP_CMD_TEST_HANDLE_DROP 103  /* json_args: a handle, in decimal */
#define BP_CMD_TEST_HANDLE_PROBE 104 /* json_args: a handle, in decimal */
#define BP_CMD_TEST_HANDLE_COUNT 105 /* live slots by type, as JSON */

/* Event ids delivered to bp_event_h.
 *
 * Ids below BP_EV_BASE complete a command: their handle is the command's
 * handle, and they resolve whoever is waiting on it. Ids at or above
 * BP_EV_BASE are stack events: BP_EV_BASE plus the stack's own event
 * number, their handle names the object they concern (0 for none), and
 * their json payload carries the details. */
#define BP_EV_PONG 1
#define BP_EV_DONE 2         /* a command that carries no result finished */
#define BP_EV_STALE_HANDLE 3 /* the command named an object that no longer exists */
#define BP_EV_BASE 1000

/* Shim-origin stack events. They travel exactly like the stack's own
 * events (id = BP_EV_BASE + number, handle = the object concerned, JSON
 * payload) but the numbers are ours, starting at 900 — far above
 * anything the stack's enum can plausibly grow to, so the two spaces
 * never collide.
 *
 * BP_EV_AUDIO_WARNING: a call's audio is being damaged by the
 * application's pacing — transmit fed too slowly mid-stream, or receive
 * not being read. Sampled once per second per call and rate-limited to
 * one event per direction per sample, so a starved 20 ms cadence cannot
 * flood the event channel. A direction the application never uses is a
 * choice, not a fault: writing nothing transmits silence without
 * warning, and receive warnings are emitted only once the application
 * has read from the call at all (the drops still count in the stats).
 * Payload: {"call":N,"text":message}, where the message begins with
 * "transmit:" or "receive:". */
#define BP_EV_AUDIO_WARNING 900

/* The stack's event numbering as actually compiled, for the cross-check
 * against the Python side: the numbers are bare enum positions upstream
 * has historically inserted into, so a mismatch after a version bump must
 * fail a test — not silently relabel every event. */
int bp_bevent_max(void);
const char *bp_bevent_str(int ev);

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
 * always invoked on the re thread. json may be NULL.
 *
 * Stack events (BP_EV_BASE and up) carry a JSON payload built natively:
 * the event name, the object handles concerned, and — when the event has a
 * SIP message — peer URI, From, To, Call-ID, and any header the configured
 * allowlist names. Every value in it originates on the network, so the
 * encoder escapes byte by byte (invalid UTF-8 included), caps each value,
 * and marks the payload "truncated" when it had to cut. The payload is
 * always valid JSON.
 *
 * Handles: Python refers to stack objects by 32-bit handle, never by
 * pointer. A handle packs a table slot with a generation counter; the slot
 * keeps a reference on the object, and lookups validate type and
 * generation, so a handle kept past its object's end fails typed
 * (BP_EV_STALE_HANDLE) instead of dereferencing freed memory. The
 * generation is 8 bits: a handle held across exactly 256 reuses of one
 * slot would validate falsely, which we accept and test for rather than
 * hide. */
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

/* Programmatic audio (the "aumem" driver).
 *
 * When the configuration selects audio_source "aumem", each call's
 * transmit audio comes from a ring buffer Python fills; independent of
 * the source, decoded receive audio is tapped into a second ring Python
 * drains. These three functions are the Python side of those rings.
 * Callable from ANY thread and deliberately NOT commands: audio moves
 * at frame rate and must not round-trip the re thread's queue.
 *
 * PCM is signed 16-bit interleaved, native byte order (every supported
 * platform is little-endian). Rates and channel counts are whatever the
 * call negotiated — probe for them, per direction.
 *
 * bp_audio_probe fills `info` and returns 0, or ENOENT when the call
 * has no audio (not started yet, already closed, or the runtime is
 * down). A direction with its `*_ready` flag clear is not up (yet);
 * reading returns no data and writing accepts none, without error.
 *
 * bp_audio_write / bp_audio_read return the byte count moved (possibly
 * 0 — the rings never block), or a negative errno:
 *
 *   -ENOENT  no audio for that call (see above)
 *   -ESTALE  `epoch` is stale: a renegotiation replaced the streams.
 *            Data in flight across the swap is lost by design; probe
 *            again for the new epoch and continue.
 *
 * A reader that has fallen more than half a ring behind is skipped
 * forward (oldest audio dropped) so live audio stays live. */
struct bp_audio_info {
    uint32_t epoch;
    uint32_t tx_ready; /* Python may feed the call     */
    uint32_t rx_ready; /* decoded audio is being tapped */
    uint32_t tx_srate, tx_ch, tx_ptime;
    uint32_t rx_srate, rx_ch; /* the receive tap is frame-size agnostic */
    uint32_t tx_fill, tx_capacity;
    uint32_t rx_fill, rx_capacity;
};

int bp_audio_probe(uint32_t call_handle, struct bp_audio_info *info);
int32_t bp_audio_write(uint32_t call_handle, uint32_t epoch, const uint8_t *src, uint32_t len);
int32_t bp_audio_read(uint32_t call_handle, uint32_t epoch, uint8_t *dst, uint32_t len);

/* Audio health counters, per call, cumulative for the current streams
 * (they reset when a renegotiation replaces a direction — same lifetime
 * the epoch tracks).
 *
 * Transmit is fed by Python and drained by a pacing thread one frame
 * per ptime. An empty ring at a tick sends a frame of silence and counts
 * in tx_silence_frames — that is idle, not an error: an application
 * that has nothing to say writes nothing. A tick that found *some* bytes
 * but not a full frame counts in tx_starved_frames: audio was flowing
 * and ran dry mid-stream, the signature of a writer that cannot keep
 * pace. The once-per-second health sampler warns on starved frames only.
 *
 * Receive loses data two ways, both warned on: rx_dropped bytes were
 * rejected at the tap because the ring was full (nothing is reading),
 * and rx_discarded bytes were skipped by the reader-side catch-up
 * (something is reading, but too far behind). tx_rejected counts bytes
 * bp_audio_write could not take — the caller already sees that in the
 * return value; it is repeated here so one snapshot tells the whole
 * story. */
struct bp_audio_stats {
    uint32_t epoch;
    uint32_t tx_fill, tx_high_water;
    uint32_t rx_fill, rx_high_water;
    uint64_t tx_silence_frames;
    uint64_t tx_starved_frames;
    uint64_t tx_rejected;
    uint64_t rx_dropped;
    uint64_t rx_discarded;
};

/* Fill `out` and return 0, or ENOENT as bp_audio_probe. Any thread. */
int bp_audio_stats_get(uint32_t call_handle, struct bp_audio_stats *out);

#endif /* BP_SHIM_H */
