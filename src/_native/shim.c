/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <re.h>
/* Not pulled in by re.h: it carries the debug macros, which we do not use —
 * we are here for the handler that keeps libre's own output off stderr. The
 * two defines are what those unused macros expect to find. */
#define DEBUG_MODULE "baresip-python"
#define DEBUG_LEVEL 0
#include <re_dbg.h>
#include <baresip.h>

#include "shim.h"

/* Identifies us in the SIP User-Agent header. Deliberately version-free:
 * a version there tells a scanner which bugs to try. */
#define BP_SOFTWARE "baresip-python"

/* Loaded at startup, in this order. The build compiles exactly this set
 * into the static libraries (see scripts/build_native.py), so a name here
 * that the build does not know about fails the load loudly. */
static const char *const bp_modules[] = {
    "g711", "opus",   "srtp",   "dtls_srtp", "ice",      "stun",
    "turn", "aufile", "ausine", "auconv",    "auresamp", "rtcpsummary",
};

/* Payload carried through the mqueue pipe. Allocated with plain malloc so
 * that bp_cmd stays safe from any thread; freed by cmd_handler on the re
 * thread after dispatch. */
struct bp_cmd_msg {
    uint32_t handle;
    char *json; /* heap copy, may be NULL */
};

/* All queue state lives behind one mutex. bp_cmd (any thread) holds it
 * across the gate check AND the push; teardown holds it across closing the
 * gate AND freeing the queue. That pairing is what makes the two safe to
 * race: without it, a pusher that passed the gate check could write into a
 * queue freed a microsecond later — the failure mode is memory corruption
 * in exactly the "SIP thread died unexpectedly" scenario this layer must
 * survive. re_main itself never runs under the lock. */
static once_flag g_lock_once = ONCE_FLAG_INIT;
static mtx_t g_lock;

static struct mqueue *g_mq = NULL;
static bool g_running = false; /* gate: bp_cmd accepts pushes  */
static bool g_in_loop = false; /* re_main is executing         */

static void lock_init(void)
{
    mtx_init(&g_lock, mtx_plain);
}

/* -- native log capture --------------------------------------------------
 *
 * Left to themselves the two logging systems underneath write to stdout
 * (baresip) and stderr (libre), which is not acceptable in a library: the
 * host process owns those streams. Both are redirected here instead.
 *
 * These handlers are called from ANY thread — including audio threads,
 * whose deadlines must not depend on anything Python does — so they may
 * never call into Python. They hand the line to bp_log_write(), which
 * buffers it for a reader to collect.
 */

/* Sized to hold thousands of ordinary lines, or a few dozen of the 8 KB
 * ones a full SIP message trace produces. */
#define BP_LOG_RING_BYTES (256 * 1024)

/* Records are stored back to back as a header followed by its bytes, so a
 * short line costs what it weighs — a ring of fixed 8 KB slots would waste
 * two orders of magnitude on the common case, and a ring of small slots
 * would truncate exactly the SIP traces worth capturing. */
struct bp_log_hdr {
    uint32_t level;
    uint32_t channel;
    uint32_t len;
};

static once_flag g_log_once = ONCE_FLAG_INIT;
static mtx_t g_log_lock;
static cnd_t g_log_cnd;

static char g_log_ring[BP_LOG_RING_BYTES];
static size_t g_log_head = 0; /* first byte not yet read */
static size_t g_log_used = 0;
static uint32_t g_log_dropped = 0;
static bool g_log_reading = false; /* someone is collecting records */

static void log_lock_init(void)
{
    mtx_init(&g_log_lock, mtx_plain);
    cnd_init(&g_log_cnd);
}

/* Both halves of the ring walk: a record that reaches the end of the
 * buffer continues at the start. Callers hold g_log_lock and have already
 * checked that the bytes fit (put) or exist (get). */
static void ring_put(const void *data, size_t n)
{
    size_t tail = (g_log_head + g_log_used) % BP_LOG_RING_BYTES;
    size_t first = BP_LOG_RING_BYTES - tail;

    if (first > n)
        first = n;
    memcpy(g_log_ring + tail, data, first);
    memcpy(g_log_ring, (const char *)data + first, n - first);
    g_log_used += n;
}

/* out may be NULL, to discard n bytes. */
static void ring_get(void *out, size_t n)
{
    size_t first = BP_LOG_RING_BYTES - g_log_head;

    if (first > n)
        first = n;
    if (out) {
        memcpy(out, g_log_ring + g_log_head, first);
        memcpy((char *)out + first, g_log_ring, n - first);
    }
    g_log_head = (g_log_head + n) % BP_LOG_RING_BYTES;
    g_log_used -= n;
}

static void bp_log_write(uint32_t level, uint32_t channel, const char *msg, size_t len)
{
    struct bp_log_hdr hdr;

    if (!msg)
        return;
    if (len > BP_LOG_LINE_MAX - 1)
        len = BP_LOG_LINE_MAX - 1;
    /* The stack ends its lines; the receiving logger does that itself. */
    while (len && (msg[len - 1] == '\n' || msg[len - 1] == '\r'))
        len--;
    if (!len)
        return;

    hdr.level = level;
    hdr.channel = channel;
    hdr.len = (uint32_t)len;

    call_once(&g_log_once, log_lock_init);
    mtx_lock(&g_log_lock);

    if (!g_log_reading) {
        mtx_unlock(&g_log_lock);
        return;
    }
    if (sizeof(hdr) + len > BP_LOG_RING_BYTES - g_log_used) {
        g_log_dropped++;
        mtx_unlock(&g_log_lock);
        return;
    }

    ring_put(&hdr, sizeof(hdr));
    ring_put(msg, len);
    cnd_signal(&g_log_cnd);
    mtx_unlock(&g_log_lock);
}

/* baresip's own log levels map one-to-one. */
static void bp_log_h(uint32_t level, const char *msg)
{
    if (msg)
        bp_log_write(level, BP_LOG_CH_MAIN, msg, strlen(msg));
}

/* RE THREAD. Installed only while SIP tracing is on. */
static void bp_sip_trace_h(bool tx, enum sip_transp tp, const struct sa *src, const struct sa *dst,
                           const uint8_t *pkt, size_t len, void *arg)
{
    char buf[BP_LOG_LINE_MAX];
    int n;

    (void)arg;

    n = re_snprintf(buf, sizeof(buf), "%s %s %J -> %J\n%b", tx ? "TX" : "RX", sip_transp_name(tp),
                    src, dst, (const char *)pkt, len);
    if (n < 0) {
        /* Bigger than a line can hold: say so rather than say nothing. */
        n = re_snprintf(buf, sizeof(buf), "%s %s %J -> %J (%zu bytes, too large to trace)",
                        tx ? "TX" : "RX", sip_transp_name(tp), src, dst, len);
        if (n < 0)
            return;
    }

    bp_log_write(BP_LOG_DEBUG, BP_LOG_CH_SIP, buf, (size_t)n);
}

void bp_log_start(void)
{
    call_once(&g_log_once, log_lock_init);
    mtx_lock(&g_log_lock);
    g_log_head = 0;
    g_log_used = 0;
    g_log_dropped = 0;
    g_log_reading = true;
    mtx_unlock(&g_log_lock);
}

void bp_log_stop(void)
{
    call_once(&g_log_once, log_lock_init);
    mtx_lock(&g_log_lock);
    g_log_reading = false;
    cnd_broadcast(&g_log_cnd);
    mtx_unlock(&g_log_lock);
}

int bp_log_read(struct bp_log_rec *rec)
{
    struct bp_log_hdr hdr;

    if (!rec)
        return 0;

    call_once(&g_log_once, log_lock_init);
    mtx_lock(&g_log_lock);

    while (!g_log_used && g_log_reading)
        cnd_wait(&g_log_cnd, &g_log_lock);

    if (!g_log_used) { /* stopped, and nothing left to hand over */
        mtx_unlock(&g_log_lock);
        return 0;
    }

    ring_get(&hdr, sizeof(hdr));
    ring_get(rec->msg, hdr.len);
    rec->msg[hdr.len] = '\0';
    rec->len = hdr.len;
    rec->level = hdr.level;
    rec->channel = hdr.channel;
    rec->dropped = g_log_dropped;
    g_log_dropped = 0;

    mtx_unlock(&g_log_lock);
    return 1;
}

/* libre's scale runs the other way — EMERG is 0 and DEBUG is 7 — and is
 * finer-grained, so several of its levels collapse into one of ours. */
static uint32_t bp_level_from_dbg(int level)
{
    if (level <= DBG_ERR)
        return BP_LOG_ERROR;
    if (level == DBG_WARNING)
        return BP_LOG_WARN;
    if (level <= DBG_INFO)
        return BP_LOG_INFO;
    return BP_LOG_DEBUG;
}

static void bp_dbg_h(int level, const char *p, size_t len, void *arg)
{
    (void)arg;
    if (p)
        bp_log_write(bp_level_from_dbg(level), BP_LOG_CH_MAIN, p, len);
}

static enum log_level bp_level_to_log(int level)
{
    switch (level) {
    case BP_LOG_DEBUG:
        return LEVEL_DEBUG;
    case BP_LOG_INFO:
        return LEVEL_INFO;
    case BP_LOG_WARN:
        return LEVEL_WARN;
    default:
        return LEVEL_ERROR;
    }
}

static int bp_level_to_dbg(int level)
{
    switch (level) {
    case BP_LOG_DEBUG:
        return DBG_DEBUG;
    case BP_LOG_INFO:
        return DBG_INFO;
    case BP_LOG_WARN:
        return DBG_WARNING;
    default:
        return DBG_ERR;
    }
}

static struct log g_log_handler = {.h = bp_log_h};

const char *bp_version(void)
{
    return sys_libre_version_get();
}

int bp_init(void)
{
    call_once(&g_lock_once, lock_init);
    return libre_init();
}

void bp_close(void)
{
    libre_close();
}

static void bp_emit(int ev, uint32_t handle, const char *json)
{
    bp_event_h(ev, handle, json);
}

/* RE THREAD. The single dispatch funnel: every command the binding ever
 * grows is one new case here. */
static void cmd_handler(int id, void *data, void *arg)
{
    struct bp_cmd_msg *msg = data;
    (void)arg;

    switch (id) {

    case BP_CMD_PING:
        bp_emit(BP_EV_PONG, msg->handle, NULL);
        break;

    case BP_CMD_STOP:
        mtx_lock(&g_lock);
        g_running = false;
        mtx_unlock(&g_lock);
        re_cancel();
        break;

    case BP_CMD_SET_LOG_LEVEL: {
        int level = msg->json ? atoi(msg->json) : BP_LOG_WARN;

        log_level_set(bp_level_to_log(level));
        dbg_init(bp_level_to_dbg(level), DBG_NONE);
        bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    case BP_CMD_SET_SIP_TRACE: {
        struct sip *sip = uag_sip();
        bool on = msg->json && atoi(msg->json);

        if (sip)
            sip_set_trace_handler(sip, on ? bp_sip_trace_h : NULL);
        bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    default:
        /* Never silent: an unknown id means a Python/C mismatch. */
        fprintf(stderr, "baresip shim: unknown command id %d\n", id);
        break;
    }

    if (msg) {
        free(msg->json);
        free(msg);
    }
}

static once_flag g_config_once = ONCE_FLAG_INIT;
static struct config g_config_defaults;

static void config_defaults_save(void)
{
    g_config_defaults = *conf_config();
}

/* How far bp_loop_init got. Bringing the stack up is a sequence of steps
 * that each need undoing, and a failure halfway must leave nothing behind
 * — a half-initialized stack would, among other things, keep our log
 * handler linked into a list it can never be unlinked from.
 *
 * A stage is marked before the step it names, not after: a step that fails
 * partway still leaves things standing (a failing ua_init has already
 * allocated the SIP stack, for one), and those still have to come down. */
enum bp_stage {
    BP_STAGE_NONE = 0,
    BP_STAGE_LOG,
    BP_STAGE_RE_THREAD,
    BP_STAGE_CONF,
    BP_STAGE_BARESIP,
    BP_STAGE_UA,
};

/* Undo everything up to `stage`. The order is baresip's own shutdown
 * order, which is not simply the reverse of the startup order, so both
 * the failure path and bp_loop_done go through here — one sequence to get
 * right, and no way for the two to drift apart. */
static void loop_unwind(enum bp_stage stage)
{
    if (stage >= BP_STAGE_UA) {
        struct sip *sip = uag_sip();

        if (sip)
            sip_set_trace_handler(sip, NULL);
        ua_close();
        module_app_unload();
    }
    if (stage >= BP_STAGE_CONF)
        conf_close();
    if (stage >= BP_STAGE_BARESIP) {
        baresip_close();
        /* Modules must go after everything that could still be using
         * them, and before the thread they were loaded on is closed. */
        mod_close();
        re_thread_async_close();
    }
    if (stage >= BP_STAGE_RE_THREAD)
        re_thread_close();
    if (stage >= BP_STAGE_LOG) {
        log_unregister_handler(&g_log_handler);
        dbg_handler_set(NULL, NULL);
    }
}

int bp_loop_init(const char *conf_dir, const char *config_text, int log_level)
{
    enum bp_stage stage = BP_STAGE_NONE;
    int err;
    size_t i;

    if (!conf_dir || !config_text)
        return EINVAL;

    call_once(&g_lock_once, lock_init);
    mtx_lock(&g_lock);

    if (g_mq) {
        mtx_unlock(&g_lock);
        fprintf(stderr, "baresip shim: bp_loop_init called twice without bp_loop_done\n");
        return EALREADY;
    }

    /* First of all, before any call below can write a line of its own. */
    log_enable_stdout(false);
    log_enable_color(false);
    log_enable_timestamps(false); /* the receiving logger timestamps */
    log_level_set(bp_level_to_log(log_level));
    log_register_handler(&g_log_handler);
    dbg_init(bp_level_to_dbg(log_level), DBG_NONE);
    dbg_handler_set(bp_dbg_h, NULL);
    stage = BP_STAGE_LOG;

    err = re_thread_init();
    if (err)
        goto out;
    stage = BP_STAGE_RE_THREAD;

    /* Confine the stack to its own directory before it reads anything. */
    stage = BP_STAGE_CONF;
    err = conf_path_set(conf_dir);
    if (err)
        goto out;

    /* Configuration lives in one struct for the whole process, and parsing
     * only overwrites the keys the text mentions — so without this, a
     * setting from an earlier run survives into a later one that never
     * asked for it. The defaults are captured before the first parse, when
     * they are still untouched. */
    call_once(&g_config_once, config_defaults_save);
    *conf_config() = g_config_defaults;

    err = conf_configure_buf((const uint8_t *)config_text, strlen(config_text));
    if (err)
        goto out;

    stage = BP_STAGE_BARESIP;
    err = baresip_init(conf_config());
    if (err)
        goto out;

    stage = BP_STAGE_UA;
    err = ua_init(BP_SOFTWARE, true, true, true);
    if (err)
        goto out;

    /* After ua_init: modules may register user agents of their own. */
    for (i = 0; i < RE_ARRAY_SIZE(bp_modules); i++) {
        err = module_preload(bp_modules[i]);
        if (err) {
            fprintf(stderr, "baresip shim: module '%s' failed to load (%d)\n", bp_modules[i], err);
            goto out;
        }
    }

    err = mqueue_alloc(&g_mq, cmd_handler, NULL);
    if (err)
        goto out;

    g_running = true;
    mtx_unlock(&g_lock);
    return 0;

out:
    loop_unwind(stage);
    mtx_unlock(&g_lock);
    return err;
}

int bp_loop_run(void)
{
    int err;

    call_once(&g_lock_once, lock_init);
    mtx_lock(&g_lock);
    if (!g_mq) {
        mtx_unlock(&g_lock);
        fprintf(stderr, "baresip shim: bp_loop_run called without bp_loop_init\n");
        return EINVAL;
    }
    if (g_in_loop) {
        mtx_unlock(&g_lock);
        fprintf(stderr, "baresip shim: bp_loop_run called while the loop is already running\n");
        return EALREADY;
    }
    g_in_loop = true;
    mtx_unlock(&g_lock);

    err = re_main(NULL);

    mtx_lock(&g_lock);
    g_in_loop = false;
    mtx_unlock(&g_lock);
    return err;
}

int bp_loop_done(void)
{
    call_once(&g_lock_once, lock_init);
    mtx_lock(&g_lock);
    if (!g_mq) {
        mtx_unlock(&g_lock);
        fprintf(stderr, "baresip shim: bp_loop_done called with nothing to tear down\n");
        return EINVAL;
    }
    if (g_in_loop) {
        mtx_unlock(&g_lock);
        fprintf(stderr, "baresip shim: bp_loop_done called while the loop is still "
                        "running; refusing to free a live command queue\n");
        return EBUSY;
    }

    g_running = false;
    g_mq = mem_deref(g_mq);
    loop_unwind(BP_STAGE_UA);
    mtx_unlock(&g_lock);
    return 0;
}

int bp_cmd(int cmd, uint32_t handle, const char *json_args)
{
    struct bp_cmd_msg *msg;
    int err;

    msg = calloc(1, sizeof(*msg));
    if (!msg)
        return ENOMEM;

    msg->handle = handle;
    if (json_args) {
        msg->json = strdup(json_args);
        if (!msg->json) {
            free(msg);
            return ENOMEM;
        }
    }

    call_once(&g_lock_once, lock_init);
    mtx_lock(&g_lock);
    if (!g_running || !g_mq) {
        mtx_unlock(&g_lock);
        free(msg->json);
        free(msg);
        return ESHUTDOWN;
    }
    err = mqueue_push(g_mq, cmd, msg);
    mtx_unlock(&g_lock);

    if (err) {
        free(msg->json);
        free(msg);
    }
    return err;
}
