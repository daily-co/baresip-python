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
#include "internal.h"

/* Identifies us in the SIP User-Agent header. Deliberately version-free:
 * a version there tells a scanner which bugs to try. */
#define BP_SOFTWARE "baresip-python"

/* Loaded at startup, in this order. The build compiles exactly this set
 * into the static libraries (see scripts/build_native.py), so a name here
 * that the build does not know about fails the load loudly. The tail is
 * the platform's hardware speaker/mic driver; loading it opens no device.
 */
static const char *const bp_modules[] = {
    "g711",      "opus",   "srtp",   "dtls_srtp", "ice",      "stun",
    "turn",      "aufile", "ausine", "auconv",    "auresamp", "rtcpsummary",
#if defined(__APPLE__)
    "coreaudio",
#elif defined(__linux__)
    "alsa",
#endif
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

/* -- handle table ----------------------------------------------------------
 *
 * Python refers to stack objects by handle, never by pointer: a pointer
 * held by Python outlives whatever C decides to free, and that is the
 * use-after-free class this table exists to end. A handle packs a slot
 * index with an 8-bit generation; the slot keeps its own reference on the
 * object, and a lookup validates type and generation, so a stale handle
 * fails typed instead of dereferencing. Freeing a slot bumps its
 * generation, which is what invalidates every handle already issued for
 * it. The generation wraps at 256 — a handle held across exactly 256
 * reuses of one slot validates falsely; accepted, and pinned by a test.
 *
 * Everything here runs on the re thread only.
 */

#define BP_HANDLE_SLOTS 1024 /* slot 0 stays empty: handle 0 means "none" */

enum bp_obj_type {
    BP_OBJ_NONE = 0,
    BP_OBJ_UA,
    BP_OBJ_CALL,
    BP_OBJ_TEST,
};

struct bp_slot {
    void *ptr; /* holds a reference; NULL = slot free */
    enum bp_obj_type type;
    uint8_t gen;
};

static struct bp_slot g_slots[BP_HANDLE_SLOTS];

static uint32_t handle_pack(uint32_t idx, uint8_t gen)
{
    return ((uint32_t)gen << 24) | idx;
}

/* The handle for ptr, creating a table entry if it has none. 0 = table
 * full, which is loud: it means more live objects than the table was
 * sized for, and events for the overflow object cannot be correlated. */
static uint32_t handle_create(void *ptr, enum bp_obj_type type)
{
    uint32_t i;

    if (!ptr)
        return 0;

    for (i = 1; i < BP_HANDLE_SLOTS; i++) {
        if (g_slots[i].ptr == ptr)
            return handle_pack(i, g_slots[i].gen);
    }
    for (i = 1; i < BP_HANDLE_SLOTS; i++) {
        if (!g_slots[i].ptr) {
            g_slots[i].ptr = mem_ref(ptr);
            g_slots[i].type = type;
            return handle_pack(i, g_slots[i].gen);
        }
    }
    fprintf(stderr, "baresip shim: handle table full (%d slots)\n", BP_HANDLE_SLOTS - 1);
    return 0;
}

/* Find-only variant for other translation units (aumem correlates its
 * streams to calls with it): never creates, 0 when absent. */
uint32_t bp_call_handle_find(const struct call *call)
{
    uint32_t i;

    if (!call)
        return 0;
    for (i = 1; i < BP_HANDLE_SLOTS; i++) {
        if (g_slots[i].ptr == call && g_slots[i].type == BP_OBJ_CALL)
            return handle_pack(i, g_slots[i].gen);
    }
    return 0;
}

static void *handle_lookup(uint32_t handle, enum bp_obj_type type)
{
    uint32_t idx = handle & 0xFFFFFF;
    uint8_t gen = (uint8_t)(handle >> 24);
    struct bp_slot *slot;

    if (!idx || idx >= BP_HANDLE_SLOTS)
        return NULL;
    slot = &g_slots[idx];
    if (!slot->ptr || slot->type != type || slot->gen != gen)
        return NULL;
    return slot->ptr;
}

static void slot_clear(uint32_t idx)
{
    g_slots[idx].ptr = mem_deref(g_slots[idx].ptr);
    g_slots[idx].type = BP_OBJ_NONE;
    g_slots[idx].gen++; /* wraps at 256 by design */
}

/* Only two callers may free a slot: CLOSED-event processing (here) and
 * the teardown drain — anything else would reopen the question of who
 * invalidates whom. */
static void handle_drop_ptr(void *ptr)
{
    uint32_t i;

    for (i = 1; i < BP_HANDLE_SLOTS; i++) {
        if (g_slots[i].ptr == ptr) {
            slot_clear(i);
            return;
        }
    }
}

static void handle_drain(void)
{
    uint32_t i;

    for (i = 1; i < BP_HANDLE_SLOTS; i++) {
        if (g_slots[i].ptr)
            slot_clear(i);
    }
}

/* -- event payloads ---------------------------------------------------------
 *
 * Stack events carry a JSON object built here. Everything quoted into it
 * comes off the network — not guaranteed UTF-8, possibly hostile, possibly
 * huge — so the writer escapes byte by byte, caps each value, cuts only at
 * a code-point boundary, and marks the payload when it cut. The output is
 * valid JSON, every time; a payload that would overflow the buffer is
 * replaced by a minimal one rather than emitted malformed.
 */

#define BP_JSON_MAX 16384
#define BP_JSON_VALUE_MAX 1024 /* input bytes per quoted value */

struct jw {
    char buf[BP_JSON_MAX];
    size_t len;
    bool overflow;
    bool truncated;
};

static void jw_putc(struct jw *w, char c)
{
    if (w->len + 1 >= sizeof(w->buf)) {
        w->overflow = true;
        return;
    }
    w->buf[w->len++] = c;
}

static void jw_puts(struct jw *w, const char *s)
{
    while (*s)
        jw_putc(w, *s++);
}

/* Length of the valid UTF-8 sequence at p (at most n bytes), or 0 if the
 * bytes there are not one. The second-byte constraints matter: overlong
 * forms and surrogates are not valid UTF-8, and one of them passed
 * through would make the whole payload undecodable on the Python side. */
static size_t utf8_seq(const uint8_t *p, size_t n)
{
    uint8_t b = p[0];
    size_t need, i;

    if (b >= 0xC2 && b <= 0xDF)
        need = 2;
    else if (b >= 0xE0 && b <= 0xEF)
        need = 3;
    else if (b >= 0xF0 && b <= 0xF4)
        need = 4;
    else
        return 0;
    if (n < need)
        return 0;

    if (b == 0xE0 && (p[1] < 0xA0 || p[1] > 0xBF))
        return 0;
    if (b == 0xED && (p[1] < 0x80 || p[1] > 0x9F))
        return 0;
    if (b == 0xF0 && (p[1] < 0x90 || p[1] > 0xBF))
        return 0;
    if (b == 0xF4 && (p[1] < 0x80 || p[1] > 0x8F))
        return 0;

    for (i = 1; i < need; i++) {
        if (p[i] < 0x80 || p[i] > 0xBF)
            return 0;
    }
    return need;
}

/* Append a quoted JSON string from raw bytes. At most cap input bytes are
 * consumed; a cut never lands inside a code point or an escape. Invalid
 * bytes become \u00XX — the byte value is preserved, nothing is dropped,
 * and the result still decodes. */
static void jw_quote(struct jw *w, const char *s, size_t n, size_t cap)
{
    const uint8_t *p = (const uint8_t *)s;
    size_t i = 0;
    char esc[8];

    jw_putc(w, '"');
    while (i < n) {
        uint8_t b = p[i];
        size_t seq;

        if (i >= cap) {
            w->truncated = true;
            break;
        }
        if (b == '"' || b == '\\') {
            jw_putc(w, '\\');
            jw_putc(w, (char)b);
            i++;
        } else if (b < 0x20 || b == 0x7F) {
            re_snprintf(esc, sizeof(esc), "\\u%04x", b);
            jw_puts(w, esc);
            i++;
        } else if (b < 0x80) {
            jw_putc(w, (char)b);
            i++;
        } else if ((seq = utf8_seq(p + i, n - i)) > 0) {
            if (i + seq > cap) { /* would cut mid code point */
                w->truncated = true;
                break;
            }
            while (seq--)
                jw_putc(w, (char)p[i++]);
        } else {
            re_snprintf(esc, sizeof(esc), "\\u%04x", b);
            jw_puts(w, esc);
            i++;
        }
    }
    jw_putc(w, '"');
}

static void jw_key(struct jw *w, const char *key)
{
    if (w->len > 1) /* past the opening brace: needs a separator */
        jw_putc(w, ',');
    jw_puts(w, "\"");
    jw_puts(w, key);
    jw_puts(w, "\":");
}

static void jw_kv_str(struct jw *w, const char *key, const char *val)
{
    jw_key(w, key);
    jw_quote(w, val, strlen(val), BP_JSON_VALUE_MAX);
}

static void jw_kv_pl(struct jw *w, const char *key, const struct pl *val)
{
    jw_key(w, key);
    jw_quote(w, val->p, val->l, BP_JSON_VALUE_MAX);
}

static void jw_kv_u32(struct jw *w, const char *key, uint32_t val)
{
    char num[16];

    jw_key(w, key);
    re_snprintf(num, sizeof(num), "%u", val);
    jw_puts(w, num);
}

/* -- stack event trampoline -------------------------------------------------
 *
 * One handler receives every event the stack emits and forwards it as
 * BP_EV_BASE + the stack's own number, with the JSON payload above. The
 * handle names the most specific object: the call when there is one, else
 * the user agent, else 0.
 */

/* Header allowlist for event payloads, set via BP_CMD_SET_EXPOSE_HEADERS.
 * Owned by the re thread. */
#define BP_EXPOSE_MAX 16
#define BP_EXPOSE_NAME_MAX 64
static char g_expose[BP_EXPOSE_MAX][BP_EXPOSE_NAME_MAX];
static size_t g_expose_n = 0;

static void expose_headers_set(const char *csv)
{
    const char *p = csv;

    g_expose_n = 0;
    while (p && *p) {
        const char *end = strchr(p, ',');
        size_t len = end ? (size_t)(end - p) : strlen(p);

        if (g_expose_n >= BP_EXPOSE_MAX || len >= BP_EXPOSE_NAME_MAX) {
            /* The Python side enforces the same limits; hitting this
             * means the two disagree. */
            fprintf(stderr, "baresip shim: header allowlist entry rejected\n");
        } else if (len) {
            memcpy(g_expose[g_expose_n], p, len);
            g_expose[g_expose_n][len] = '\0';
            g_expose_n++;
        }
        p = end ? end + 1 : NULL;
    }
}

static void emit_stack_event(int ev, struct ua *ua, struct call *call, const struct sip_msg *msg,
                             const char *text)
{
    static struct jw w; /* re thread only; too large for the stack */
    uint32_t ua_handle = handle_create(ua, BP_OBJ_UA);
    uint32_t call_handle = handle_create(call, BP_OBJ_CALL);
    size_t i;

    w.len = 0;
    w.overflow = false;
    w.truncated = false;

    jw_putc(&w, '{');
    jw_kv_str(&w, "event", bp_bevent_str(ev));
    if (text && *text)
        jw_kv_str(&w, "text", text);
    if (ua_handle)
        jw_kv_u32(&w, "ua", ua_handle);
    if (call_handle) {
        const char *peer = call_peeruri(call);
        const char *id = call_id(call);
        const struct list *hdrs = call_get_custom_hdrs(call);
        struct le *le;

        jw_kv_u32(&w, "call", call_handle);
        if (peer)
            jw_kv_str(&w, "peer", peer);
        if (id)
            jw_kv_str(&w, "call_id", id);
        /* Headers captured from the incoming INVITE by the UA's xhdr
         * filter (installed from the expose allowlist at UA_ALLOC). They
         * live on the call, so every event for it carries them. No event
         * carries both a call and a msg, so "headers" stays unique. */
        if (!list_isempty(hdrs)) {
            bool open = false;

            for (le = list_head(hdrs); le; le = le->next) {
                const struct sip_hdr *hdr = le->data;

                if (!open) {
                    jw_key(&w, "headers");
                    jw_putc(&w, '{');
                    open = true;
                } else
                    jw_putc(&w, ',');
                jw_quote(&w, hdr->name.p, hdr->name.l, BP_JSON_VALUE_MAX);
                jw_putc(&w, ':');
                jw_quote(&w, hdr->val.p, hdr->val.l, BP_JSON_VALUE_MAX);
            }
            jw_putc(&w, '}');
        }
    }
    /* Media statistics ride the RTCP report events and the final CLOSED
     * event. Harvested here, while the call still exists — for CLOSED
     * this runs before the handle and audio slot are dropped below. All
     * values are numeric, so the object is composed with one format
     * string; nothing in it needs escaping. */
    if (call && (ev == BEVENT_CALL_CLOSED || ev == BEVENT_CALL_RTCP)) {
        const struct stream *strm = audio_strm(call_audio(call));

        if (strm) {
            const struct rtcp_stats *rc = stream_rtcp_stats(strm);
            struct jbuf_stat jb;
            struct bp_audio_stats as;
            char stats[768];

            if (stream_jbuf_stats(strm, &jb))
                memset(&jb, 0, sizeof(jb));
            if (bp_audio_stats_get(call_handle, &as))
                memset(&as, 0, sizeof(as));

            re_snprintf(
                stats, sizeof(stats),
                "{\"duration\":%u,\"setup\":%u,"
                "\"tx\":{\"packets\":%u,\"bytes\":%u,\"errors\":%u,\"avg_bitrate\":%u},"
                "\"rx\":{\"packets\":%u,\"bytes\":%u,\"errors\":%u,\"avg_bitrate\":%u},"
                "\"rtcp\":{\"tx_lost\":%d,\"rx_lost\":%d,\"tx_jitter_us\":%u,"
                "\"rx_jitter_us\":%u,\"rtt_us\":%u},"
                "\"jbuf\":{\"late\":%u,\"lost\":%u,\"overflow\":%u,\"delay_ms\":%u,"
                "\"jitter_ms\":%u},"
                "\"audio\":{\"tx_silence_frames\":%llu,\"tx_starved_frames\":%llu,"
                "\"rx_dropped\":%llu,\"rx_discarded\":%llu}}",
                call_duration(call), call_setup_duration(call),
                stream_metric_get_tx_n_packets(strm), stream_metric_get_tx_n_bytes(strm),
                stream_metric_get_tx_n_err(strm), (uint32_t)stream_metric_get_tx_avg_bitrate(strm),
                stream_metric_get_rx_n_packets(strm), stream_metric_get_rx_n_bytes(strm),
                stream_metric_get_rx_n_err(strm), (uint32_t)stream_metric_get_rx_avg_bitrate(strm),
                rc ? rc->tx.lost : 0, rc ? rc->rx.lost : 0, rc ? rc->tx.jit : 0,
                rc ? rc->rx.jit : 0, rc ? rc->rtt : 0, jb.n_late, jb.n_lost, jb.n_overflow,
                jb.c_delay, jb.c_jitter, (unsigned long long)as.tx_silence_frames,
                (unsigned long long)as.tx_starved_frames, (unsigned long long)as.rx_dropped,
                (unsigned long long)as.rx_discarded);
            jw_key(&w, "stats");
            jw_puts(&w, stats);
        }
    }
    if (msg) {
        if (!call && pl_isset(&msg->callid))
            jw_kv_pl(&w, "call_id", &msg->callid);
        if (pl_isset(&msg->from.auri))
            jw_kv_pl(&w, "from", &msg->from.auri);
        if (pl_isset(&msg->to.auri))
            jw_kv_pl(&w, "to", &msg->to.auri);
        if (g_expose_n) {
            bool open = false;

            for (i = 0; i < g_expose_n; i++) {
                const struct sip_hdr *hdr = sip_msg_xhdr(msg, g_expose[i]);

                if (!hdr)
                    continue;
                if (!open) {
                    jw_key(&w, "headers");
                    jw_putc(&w, '{');
                    open = true;
                } else
                    jw_putc(&w, ',');
                jw_quote(&w, g_expose[i], strlen(g_expose[i]), BP_JSON_VALUE_MAX);
                jw_putc(&w, ':');
                jw_quote(&w, hdr->val.p, hdr->val.l, BP_JSON_VALUE_MAX);
            }
            if (open)
                jw_putc(&w, '}');
        }
    }
    if (w.truncated) {
        jw_key(&w, "truncated");
        jw_puts(&w, "true");
    }
    jw_putc(&w, '}');

    if (w.overflow) {
        /* Never emit malformed JSON: fall back to the bare minimum. */
        w.len = 0;
        w.overflow = false;
        jw_putc(&w, '{');
        jw_kv_str(&w, "event", bp_bevent_str(ev));
        jw_key(&w, "overflow");
        jw_puts(&w, "true");
        jw_putc(&w, '}');
    }
    w.buf[w.len] = '\0';

    bp_emit(BP_EV_BASE + ev, call_handle ? call_handle : ua_handle, w.buf);
}

/* RE THREAD. */
static void bp_bevent_h(enum bevent_ev ev, struct bevent *event, void *arg)
{
    struct ua *ua = bevent_get_ua(event);
    struct call *call = bevent_get_call(event);
    const struct sip_msg *msg = bevent_get_msg(event);
    const char *text = bevent_get_text(event);

    (void)arg;

    /* The CREATE event's text is the account's full AOR — auth_pass
     * included. A credential must never cross into the payload. */
    if (ev == BEVENT_CREATE)
        text = NULL;

    emit_stack_event(ev, ua, call, msg, text);

    /* The call is over: no later event can reference it, so this is one
     * of the two places allowed to free its slot. The audio slot goes
     * with it — its stream instances die with the call moments later. */
    if (ev == BEVENT_CALL_CLOSED && call) {
        bp_aumem_slot_drop(bp_call_handle_find(call));
        handle_drop_ptr(call);
    }
}

int bp_bevent_max(void)
{
    return BEVENT_MAX;
}

const char *bp_bevent_str(int ev)
{
    return bevent_str((enum bevent_ev)ev);
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

    case BP_CMD_SET_EXPOSE_HEADERS:
        expose_headers_set(msg->json ? msg->json : "");
        bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;

    case BP_CMD_UA_ALLOC: {
        struct ua *ua = NULL;
        char json[64];
        int uerr = ua_alloc(&ua, msg->json ? msg->json : "");

        if (uerr) {
            re_snprintf(json, sizeof(json), "{\"error\":\"alloc\",\"errno\":%d}", uerr);
            bp_emit(BP_EV_DONE, msg->handle, json);
            break;
        }
        /* The CREATE event just fired inside ua_alloc, so the table entry
         * already exists; this returns it. Dropping our creator reference
         * leaves the slot's as the only one. */
        uint32_t h = handle_create(ua, BP_OBJ_UA);
        size_t i;

        mem_deref(ua);
        if (!h) {
            bp_emit(BP_EV_DONE, msg->handle, "{\"error\":\"table_full\"}");
            break;
        }
        /* Capture allowlisted headers from INVITEs to this agent; they
         * surface on its calls' events. */
        for (i = 0; i < g_expose_n; i++) {
            if (ua_add_xhdr_filter(ua, g_expose[i]))
                fprintf(stderr, "baresip shim: xhdr filter %s failed\n", g_expose[i]);
        }
        re_snprintf(json, sizeof(json), "{\"handle\":%u}", h);
        bp_emit(BP_EV_DONE, msg->handle, json);
        break;
    }

    case BP_CMD_UA_REGISTER: {
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, NULL, 10) : 0;
        struct ua *ua = handle_lookup(h, BP_OBJ_UA);

        if (!ua) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }
        int uerr = ua_register(ua);

        if (uerr) {
            char json[64];

            re_snprintf(json, sizeof(json), "{\"error\":\"register\",\"errno\":%d}", uerr);
            bp_emit(BP_EV_DONE, msg->handle, json);
        } else
            bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    case BP_CMD_UA_UNREGISTER: {
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, NULL, 10) : 0;
        struct ua *ua = handle_lookup(h, BP_OBJ_UA);

        if (!ua) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }
        ua_unregister(ua);
        bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    case BP_CMD_CALL_ANSWER: {
        char *end = NULL;
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, &end, 10) : 0;
        bool video = end && atoi(end);
        struct call *call = handle_lookup(h, BP_OBJ_CALL);

        if (!call) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }
        int cerr = call_answer(call, 200, video ? VIDMODE_ON : VIDMODE_OFF);

        if (cerr) {
            char json[64];

            re_snprintf(json, sizeof(json), "{\"error\":\"answer\",\"errno\":%d}", cerr);
            bp_emit(BP_EV_DONE, msg->handle, json);
        } else
            bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    case BP_CMD_CALL_SEND_DIGIT: {
        char *end = NULL;
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, &end, 10) : 0;
        char key = 0;
        struct call *call = handle_lookup(h, BP_OBJ_CALL);

        while (end && *end == ' ')
            end++;
        if (end)
            key = *end;

        if (!call) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }
        int cerr = key ? call_send_digit(call, key == 'R' ? KEYCODE_REL : key) : EINVAL;

        if (cerr) {
            char json[64];

            re_snprintf(json, sizeof(json), "{\"error\":\"dtmf\",\"errno\":%d}", cerr);
            bp_emit(BP_EV_DONE, msg->handle, json);
        } else
            bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    case BP_CMD_UA_CONNECT: {
        char *end = NULL;
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, &end, 10) : 0;
        bool video = end ? strtol(end, &end, 10) : false;
        struct ua *ua = handle_lookup(h, BP_OBJ_UA);
        char json[64];

        if (!ua) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }

        /* First line: the URI. Every further line: one outgoing header. */
        while (end && *end == ' ')
            end++;
        char *uri = end;
        char *line = strchr(uri, '\n');
        struct list hdrs;

        list_init(&hdrs);
        if (line)
            *line++ = '\0';
        while (line && *line) {
            char *next = strchr(line, '\n');
            char *colon;

            if (next)
                *next++ = '\0';
            colon = strstr(line, ": ");
            if (colon) {
                *colon = '\0';
                custom_hdrs_add(&hdrs, line, "%s", colon + 2);
            }
            line = next;
        }

        /* Pre-flight the local-address selection for IP-literal targets.
         * ua_connect fails with a bare EINVAL when interface discovery
         * has no address toward the target (classic case: loopback
         * without net_interface pinned), indistinguishable from an
         * argument error. Mirror the stack's own destination derivation
         * (ua_connect_dir -> ua_call_alloc: sa_set succeeds only for
         * numeric hosts; link-local v6 gets a scope id first; DNS names
         * never take the failing branch) and report just that case as a
         * typed failure. A miss here falls through to the stack. */
        struct sip_addr addr;
        struct pl pl_uri;
        struct sa dst;

        pl_set_str(&pl_uri, uri);
        sa_init(&dst, AF_UNSPEC);
        if (0 == sip_addr_decode(&addr, &pl_uri))
            (void)sa_set(&dst, &addr.uri.host, addr.uri.port);
        if (sa_isset(&dst, SA_ADDR) && !(sa_af(&dst) == AF_INET6 && sa_is_linklocal(&dst)) &&
            !sa_isset(net_laddr_for(baresip_network(), &dst), SA_ADDR)) {
            char njson[96];

            re_snprintf(njson, sizeof(njson), "{\"error\":\"no_laddr\",\"host\":\"%j\"}", &dst);
            bp_emit(BP_EV_DONE, msg->handle, njson);
            list_flush(&hdrs);
            break;
        }

        /* Headers ride on the UA between set and connect; clearing right
         * after keeps them off later calls and re-registrations. All of
         * it inside this one command, so nothing can interleave. */
        if (!list_isempty(&hdrs))
            ua_set_custom_hdrs(ua, &hdrs);

        struct call *call = NULL;
        int cerr = ua_connect(ua, &call, NULL, uri, video ? VIDMODE_ON : VIDMODE_OFF);

        if (!list_isempty(&hdrs)) {
            ua_set_custom_hdrs(ua, NULL);
            list_flush(&hdrs);
        }
        if (cerr) {
            re_snprintf(json, sizeof(json), "{\"error\":\"connect\",\"errno\":%d}", cerr);
            bp_emit(BP_EV_DONE, msg->handle, json);
            break;
        }
        /* The OUTGOING event fired inside ua_connect, so the table entry
         * exists; this returns it. The pointer is borrowed (the UA's list
         * owns the call) — no reference of ours to drop here. */
        uint32_t ch = handle_create(call, BP_OBJ_CALL);

        if (!ch) {
            ua_hangup(ua, call, 0, NULL);
            bp_emit(BP_EV_DONE, msg->handle, "{\"error\":\"table_full\"}");
            break;
        }
        re_snprintf(json, sizeof(json), "{\"handle\":%u}", ch);
        bp_emit(BP_EV_DONE, msg->handle, json);
        break;
    }

    case BP_CMD_CALL_HOLD: {
        char *end = NULL;
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, &end, 10) : 0;
        bool hold = end ? strtol(end, NULL, 10) : true;
        struct call *call = handle_lookup(h, BP_OBJ_CALL);

        if (!call) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }
        int cerr = call_hold(call, hold);

        if (cerr) {
            char json[64];

            re_snprintf(json, sizeof(json), "{\"error\":\"hold\",\"errno\":%d}", cerr);
            bp_emit(BP_EV_DONE, msg->handle, json);
        } else
            bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    case BP_CMD_CALL_TRANSFER: {
        char *end = NULL;
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, &end, 10) : 0;
        struct call *call = handle_lookup(h, BP_OBJ_CALL);

        if (!call) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }
        while (end && *end == ' ')
            end++;
        int cerr = (end && *end) ? call_transfer(call, end) : EINVAL;

        if (cerr) {
            char json[64];

            re_snprintf(json, sizeof(json), "{\"error\":\"transfer\",\"errno\":%d}", cerr);
            bp_emit(BP_EV_DONE, msg->handle, json);
        } else
            bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    case BP_CMD_CALL_REPLACE_TRANSFER: {
        char *end = NULL;
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, &end, 10) : 0;
        uint32_t ch = end ? (uint32_t)strtoul(end, NULL, 10) : 0;
        struct call *call = handle_lookup(h, BP_OBJ_CALL);
        struct call *consult = handle_lookup(ch, BP_OBJ_CALL);

        if (!call || !consult) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }
        /* Known only after the peer answered; both calls are
         * established by the time the binding sends this. */
        if (!call_supported(call, REPLACES)) {
            bp_emit(BP_EV_DONE, msg->handle, "{\"error\":\"replaces_unsupported\"}");
            break;
        }
        int cerr = call_replace_transfer(call, consult);

        if (cerr) {
            char json[64];

            re_snprintf(json, sizeof(json), "{\"error\":\"transfer\",\"errno\":%d}", cerr);
            bp_emit(BP_EV_DONE, msg->handle, json);
        } else
            bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    case BP_CMD_CALL_TRANSFER_ACCEPT: {
        char *end = NULL;
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, &end, 10) : 0;
        struct call *call = handle_lookup(h, BP_OBJ_CALL);
        char json[64];

        if (!call) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }
        while (end && *end == ' ')
            end++;
        if (!end || !*end) {
            bp_emit(BP_EV_DONE, msg->handle, "{\"error\":\"accept\",\"errno\":22}");
            break;
        }
        /* The menu module's recipe: allocate the replacement call with
         * the transferring call as xcall (5th arg) — that linkage is
         * what makes the core report the outcome to the transferor and
         * close the original leg when the new call establishes — then
         * dial the raw Refer-To target. On error the transferor learns
         * via a final 500 sipfrag, as menu does. */
        struct ua *ua = call_get_ua(call);
        struct call *call2 = NULL;
        int cerr = ua_call_alloc(&call2, ua, VIDMODE_OFF, NULL, call, call_localuri(call), true);

        if (!cerr) {
            struct pl pl;

            pl_set_str(&pl, end);
            cerr = call_connect(call2, &pl);
        }
        if (cerr) {
            (void)call_notify_sipfrag(call, 500, "Call Error");
            mem_deref(call2);
            re_snprintf(json, sizeof(json), "{\"error\":\"accept\",\"errno\":%d}", cerr);
            bp_emit(BP_EV_DONE, msg->handle, json);
            break;
        }
        uint32_t ch = handle_create(call2, BP_OBJ_CALL);

        if (!ch) {
            ua_hangup(ua, call2, 0, NULL);
            bp_emit(BP_EV_DONE, msg->handle, "{\"error\":\"table_full\"}");
            break;
        }
        re_snprintf(json, sizeof(json), "{\"handle\":%u}", ch);
        bp_emit(BP_EV_DONE, msg->handle, json);
        break;
    }

    case BP_CMD_CALL_TRANSFER_REJECT: {
        char *end = NULL;
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, &end, 10) : 0;
        long status = end ? strtol(end, NULL, 10) : 0;
        struct call *call = handle_lookup(h, BP_OBJ_CALL);

        if (!call) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }
        if (status < 300 || status > 699)
            status = 603;
        int cerr = call_notify_sipfrag(call, (uint16_t)status, "Decline");

        if (cerr) {
            char json[64];

            re_snprintf(json, sizeof(json), "{\"error\":\"reject\",\"errno\":%d}", cerr);
            bp_emit(BP_EV_DONE, msg->handle, json);
        } else
            bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    case BP_CMD_CALL_REJECT:
    case BP_CMD_CALL_HANGUP: {
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, NULL, 10) : 0;
        struct call *call = handle_lookup(h, BP_OBJ_CALL);

        if (!call) {
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
            break;
        }
        /* ua_hangupf — not bare call_hangup — is the whole-life-cycle
         * hangup: it sends the response/BYE, emits CALL_CLOSED (which
         * clears the handle slot), and releases the UA's reference. */
        if (id == BP_CMD_CALL_REJECT)
            ua_hangup(call_get_ua(call), call, 486, "Busy Here");
        else
            ua_hangup(call_get_ua(call), call, 0, NULL);
        bp_emit(BP_EV_DONE, msg->handle, NULL);
        break;
    }

    case BP_CMD_TEST_EMIT: {
        /* Decode a canned SIP message and run it through the very same
         * payload path a real event takes — header extraction included. */
        struct sip_msg *smsg = NULL;
        struct mbuf *mb = mbuf_alloc(msg->json ? strlen(msg->json) : 1);
        int err = ENOMEM;

        if (mb && msg->json) {
            err = mbuf_write_str(mb, msg->json);
            mb->pos = 0;
            if (!err)
                err = sip_msg_decode(&smsg, mb);
        }
        if (!err)
            emit_stack_event(BEVENT_CUSTOM, NULL, NULL, smsg, NULL);
        bp_emit(BP_EV_DONE, msg->handle, err ? "{\"error\":\"decode\"}" : NULL);
        mem_deref(smsg);
        mem_deref(mb);
        break;
    }

    case BP_CMD_TEST_ESCAPE: {
        /* The JSON encoder, driven directly: whatever bytes came in, the
         * reply payload must parse. */
        static struct jw w;

        w.len = 0;
        w.overflow = false;
        w.truncated = false;
        jw_putc(&w, '{');
        jw_key(&w, "fuzz");
        jw_quote(&w, msg->json ? msg->json : "", msg->json ? strlen(msg->json) : 0,
                 BP_JSON_VALUE_MAX);
        if (w.truncated) {
            jw_key(&w, "truncated");
            jw_puts(&w, "true");
        }
        jw_putc(&w, '}');
        w.buf[w.len] = '\0';
        bp_emit(BP_EV_DONE, msg->handle, w.buf);
        break;
    }

    case BP_CMD_TEST_HANDLE_NEW: {
        void *obj = mem_zalloc(1, NULL);
        uint32_t h = handle_create(obj, BP_OBJ_TEST);
        char json[32];

        mem_deref(obj); /* the slot's reference keeps it alive */
        re_snprintf(json, sizeof(json), "{\"handle\":%u}", h);
        bp_emit(BP_EV_DONE, msg->handle, json);
        break;
    }

    case BP_CMD_TEST_HANDLE_DROP: {
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, NULL, 10) : 0;

        if (handle_lookup(h, BP_OBJ_TEST)) {
            slot_clear(h & 0xFFFFFF);
            bp_emit(BP_EV_DONE, msg->handle, NULL);
        } else
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
        break;
    }

    case BP_CMD_TEST_HANDLE_PROBE: {
        uint32_t h = msg->json ? (uint32_t)strtoul(msg->json, NULL, 10) : 0;

        if (handle_lookup(h, BP_OBJ_TEST))
            bp_emit(BP_EV_DONE, msg->handle, NULL);
        else
            bp_emit(BP_EV_STALE_HANDLE, msg->handle, NULL);
        break;
    }

    /* The leak detector's ground truth: slots are freed C-side when their
     * object closes, so a nonzero count once everything has closed is a
     * real leak, not a consumer that missed an event. */
    case BP_CMD_TEST_HANDLE_COUNT: {
        uint32_t n[4] = {0, 0, 0, 0};
        char json[64];
        uint32_t i;

        for (i = 1; i < BP_HANDLE_SLOTS; i++) {
            if (g_slots[i].ptr)
                n[g_slots[i].type]++;
        }
        re_snprintf(json, sizeof(json), "{\"ua\":%u,\"call\":%u,\"test\":%u}", n[BP_OBJ_UA],
                    n[BP_OBJ_CALL], n[BP_OBJ_TEST]);
        bp_emit(BP_EV_DONE, msg->handle, json);
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
        /* The SIP trace handler stays installed through ua_close: the
         * teardown BYEs are real traffic, and a trace that goes silent
         * exactly at shutdown would hide them. The handler dies with the
         * sip object; until then it only writes the log ring, which the
         * reader is still draining. */
        bevent_unregister(bp_bevent_h);
        /* The teardown drain: the second of the two places allowed to free
         * handle slots. Our references go first, so ua_close can actually
         * free what it tears down. */
        handle_drain();
        ua_close();
        module_app_unload();
        /* After ua_close: every aumem stream instance died with its
         * call, so unregistering now leaves nothing dangling. */
        bp_aumem_unregister();
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

    /* The core's compiled default is call.accept=false, in which mode an
     * incoming INVITE gets NO reply on the wire (not even 100 Trying) and
     * no call object — only a SIPSESS_CONN event, with the application
     * expected to screen the message and call ua_accept() by hand. This
     * binding's inbound API is built entirely on the core-accepted path
     * (CALL_INCOMING carrying a call handle), so accept mode is forced
     * here, after the user config parse, where no config text can
     * override it — with call.accept=false incoming calls would not be
     * screened, they would silently vanish.
     *
     * FIXME: if call screening is ever wanted, expose it additively — a
     * Python hook on the SIPSESS_CONN event plus a per-INVITE accept
     * command — instead of surfacing this flag. */
    conf_config()->call.accept = true;

    stage = BP_STAGE_BARESIP;
    err = baresip_init(conf_config());
    if (err)
        goto out;

    stage = BP_STAGE_UA;
    err = ua_init(BP_SOFTWARE, true, true, true);
    if (err)
        goto out;

    g_expose_n = 0; /* the header allowlist does not survive into a new run */
    err = bevent_register(bp_bevent_h, NULL);
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

    /* Our own audio driver registers directly — it lives in this
     * extension, not in the static module set the build compiled. */
    err = bp_aumem_register();
    if (err)
        goto out;

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
