/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

#include <string.h>

#include <re_atomic.h>
#include <re.h>
#include <rem.h>
#include <baresip.h>

#include "bp_sync.h"
#include "shim.h"
#include "internal.h"

/* aumem: programmatic per-call PCM, three cooperating pieces.
 *
 *   TX   Python --> ring --> pacing thread --> encoder --> RTP
 *   RX   RTP --> decoder --> decode filter --> ring --> Python
 *
 * The transmit half is an audio source ("aumem"): its alloc handler
 * receives the call's `struct audio *` as arg, which walking the public
 * UA/call lists turns back into a call handle. A pacing thread ticks
 * once per ptime on absolute deadlines and feeds the encoder from the
 * ring — silence when Python has not written enough, because a starving
 * producer must never stall the RTP clock.
 *
 * The receive half is an audio FILTER, not the player. The core's full
 * receive path per call is:
 *
 *   RTP --> decoder --> decode filters --> aubuf --> player
 *
 * The core queues every decoded frame into aubuf (its playback buffer)
 * unconditionally and requires a player device to drain it at ptime
 * rate — a call cannot even answer without one. The player cannot be
 * our tap, though: its alloc receives the core's private receiver
 * struct as arg, and no public API maps that back to a call, so PCM
 * pulled there could not be routed to the right call's ring. The
 * filter API does hand decupdh the call's `struct audio *`, which
 * call_handle_for_audio() resolves to a handle — so the filter taps
 * decoded PCM into the ring upstream of aubuf, on the stream's receive
 * thread, and read() works under any configured player. The "aumem"
 * PLAYER is then just a clock: it pulls frames per ptime and discards
 * them — each a duplicate of what the filter already delivered — which
 * is what keeps aubuf drained.
 *
 * Correlation lives in the slot table: call handle -> ring pair + epoch.
 * One lock guards the table AND every Python-side ring access; the
 * stream destructors unpublish under that lock before freeing, so a
 * Python thread can never touch a freed ring. Each ring itself is SPSC:
 * Python on one side, exactly one stack thread on the other. The epoch
 * bumps on every unpublish — a mid-call renegotiation replaces the
 * stream instances, and Python's next operation fails with -ESTALE
 * instead of silently continuing against new streams (data in flight at
 * the swap is lost by design).
 */

#define BP_AUDIO_SLOTS 256
#define BP_AUDIO_RING_SECS 2

/* Reader-side drop-oldest: a reader that has fallen more than half a
 * ring behind is skipped forward to a quarter, so live audio stays
 * live instead of arriving seconds late forever. */
#define BP_AUDIO_CLAMP_TRIGGER_DIV 2
#define BP_AUDIO_CLAMP_KEEP_DIV 4

/* Health sampling: a re-thread timer walks the slots once per second and
 * emits at most one BP_EV_AUDIO_WARNING per direction per tick. Transmit
 * warns only past a few starved (mid-stream short) frames in the window:
 * a lone partial frame is the normal tail of a clip ending on an odd
 * byte count, a repeated one is a writer that cannot keep pace. Receive
 * warns only once the application has read from the call at all — the
 * tap runs under every player, so an application consuming audio through
 * a real device never touches read(), and the resulting full ring is a
 * direction deliberately unused, not a reader failing to keep up. */
#define BP_AUDIO_HEALTH_MS 1000
#define BP_AUDIO_TX_STARVED_MIN 3

struct bp_audio_slot {
    uint32_t call_handle; /* 0 = slot free */
    uint32_t epoch;       /* bumped on every unpublish */
    bp_ring *tx;          /* Python writes, source thread reads */
    bp_ring *rx;          /* decode filter writes, Python reads */
    uint32_t tx_srate, tx_ch, tx_ptime;
    uint32_t rx_srate, rx_ch;

    /* Transmit flush: Python raises the request (atomic, any thread);
     * the source thread — the transmit ring's one consumer — performs
     * the drain on its next tick, keeping the SPSC contract intact. */
    RE_ATOMIC bool tx_flush_req;
    uint64_t tx_flushed; /* bytes drained by flush requests */

    /* Health counters, current-stream lifetime (zeroed on unpublish,
     * like the ring they describe). Written under g_audio_lock. */
    uint64_t tx_silence_frames; /* pacing ticks that found nothing: idle */
    uint64_t tx_starved_frames; /* ticks that ran short mid-frame: late writer */
    uint64_t rx_discarded;      /* bytes the reader-side catch-up skipped */
    bool rx_armed;              /* the app has read: receive warnings earned */

    /* The health sampler's previous snapshots, for per-window deltas. */
    uint64_t prev_tx_starved;
    uint64_t prev_rx_dropped;
    uint64_t prev_rx_discarded;
};

static struct ausrc *g_ausrc;
static struct auplay *g_auplay;
static struct bp_audio_slot g_audio_slots[BP_AUDIO_SLOTS];
static bool g_audio_open;

/* Process-lifetime, like shim.c's g_lock: Python threads may call the
 * accessors at any moment relative to loop lifecycles, and a destroyed
 * mutex under them would be the exact use-after-free this file exists
 * to prevent. */
static mtx_t g_audio_lock;
static once_flag g_audio_lock_once = ONCE_FLAG_INIT;

static void audio_lock_init(void)
{
    mtx_init(&g_audio_lock, mtx_plain);
}

/* -- slot table (call handle -> rings), all under g_audio_lock -------- */

static struct bp_audio_slot *slot_find(uint32_t call_handle)
{
    size_t i;

    if (!call_handle)
        return NULL;
    for (i = 0; i < BP_AUDIO_SLOTS; i++) {
        if (g_audio_slots[i].call_handle == call_handle)
            return &g_audio_slots[i];
    }
    return NULL;
}

static struct bp_audio_slot *slot_find_or_create(uint32_t call_handle)
{
    struct bp_audio_slot *slot = slot_find(call_handle);
    size_t i;

    if (slot)
        return slot;
    for (i = 0; i < BP_AUDIO_SLOTS; i++) {
        if (!g_audio_slots[i].call_handle) {
            memset(&g_audio_slots[i], 0, sizeof(g_audio_slots[i]));
            g_audio_slots[i].call_handle = call_handle;
            return &g_audio_slots[i];
        }
    }
    return NULL;
}

/* Detach `ring` from whichever direction of the call still holds it.
 * Runs in stream destructors, so it tolerates the slot being gone:
 * CALL_CLOSED drops the slot before the call (and its streams) is
 * freed. After this returns no Python thread can reach the ring, and
 * freeing it is safe. */
static void slot_unpublish(uint32_t call_handle, bp_ring *ring)
{
    struct bp_audio_slot *slot;

    if (!ring)
        return;

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    slot = slot_find(call_handle);
    if (slot) {
        if (slot->tx == ring) {
            slot->tx = NULL;
            slot->tx_silence_frames = 0;
            slot->tx_starved_frames = 0;
            slot->tx_flushed = 0;
            slot->prev_tx_starved = 0;
            re_atomic_rlx_set(&slot->tx_flush_req, false);
        }
        if (slot->rx == ring) {
            slot->rx = NULL;
            slot->rx_discarded = 0;
            slot->prev_rx_dropped = 0;
            slot->prev_rx_discarded = 0;
            slot->rx_armed = false;
        }
        slot->epoch++;
    }
    bp_mtx_unlock(&g_audio_lock);
}

/* The alloc/update handlers receive the call's `struct audio *`; the
 * public UA and call lists turn it back into the call, and the call
 * into its handle. Re thread only, where those lists are stable. */
static uint32_t call_handle_for_audio(const struct audio *au)
{
    struct le *le;

    LIST_FOREACH(uag_list(), le)
    {
        struct ua *ua = le->data;
        struct le *lec;

        LIST_FOREACH(ua_calls(ua), lec)
        {
            struct call *call = lec->data;

            if (call_audio(call) == au)
                return bp_call_handle_find(call);
        }
    }
    return 0;
}

/* -- transmit: the "aumem" audio source ------------------------------- */

struct ausrc_st {
    uint32_t call_handle;
    bp_ring *ring;              /* owned; freed only after the thread is joined */
    struct bp_audio_slot *slot; /* cached by src_thread; slots are static */
    struct ausrc_prm prm;
    size_t sampc;
    size_t frame_bytes;
    void *sampv;
    ausrc_read_h *rh;
    void *arg;
    thrd_t thread;
    RE_ATOMIC bool run;
    bool started;
};

static void src_destructor(void *v)
{
    struct ausrc_st *st = v;

    slot_unpublish(st->call_handle, st->ring);
    if (st->started) {
        re_atomic_rlx_set(&st->run, false);
        thrd_join(st->thread, NULL);
    }
    bp_ring_free(st->ring);
    mem_deref(st->sampv);
}

static int src_thread(void *v)
{
    struct ausrc_st *st = v;
    uint64_t t = tmr_jiffies();

    while (re_atomic_rlx(&st->run)) {
        struct auframe af;
        uint32_t got;
        int dt;

        /* A requested flush drains here: this thread is the transmit
         * ring's one consumer, so draining anywhere else would break
         * the SPSC contract. The atomic flag keeps the healthy path
         * lock-free; the slot pointer is looked up once (slots are a
         * static array, so the pointer stays dereferenceable). */
        if (!st->slot) {
            call_once(&g_audio_lock_once, audio_lock_init);
            bp_mtx_lock(&g_audio_lock);
            st->slot = slot_find(st->call_handle);
            bp_mtx_unlock(&g_audio_lock);
        }
        if (st->slot && re_atomic_rlx(&st->slot->tx_flush_req)) {
            call_once(&g_audio_lock_once, audio_lock_init);
            bp_mtx_lock(&g_audio_lock);
            /* Only the currently published ring's thread may act: a
             * mismatch means the flag belongs to a newer publish. */
            if (st->slot->tx == st->ring) {
                uint32_t drained;
                do {
                    drained = bp_ring_read(st->ring, st->sampv, (uint32_t)st->frame_bytes);
                    st->slot->tx_flushed += drained;
                } while (drained);
                re_atomic_rlx_set(&st->slot->tx_flush_req, false);
            }
            bp_mtx_unlock(&g_audio_lock);
        }

        got = bp_ring_read(st->ring, st->sampv, (uint32_t)st->frame_bytes);
        if (got < st->frame_bytes) {
            struct bp_audio_slot *slot;

            memset((uint8_t *)st->sampv + got, 0, st->frame_bytes - got);

            /* Health accounting, on short ticks only — the healthy
             * full-frame path never touches the lock. Empty is idle
             * (silence is what an app with nothing to say wants);
             * partial means audio was flowing and ran dry mid-frame. */
            call_once(&g_audio_lock_once, audio_lock_init);
            bp_mtx_lock(&g_audio_lock);
            slot = slot_find(st->call_handle);
            if (slot) {
                if (got)
                    slot->tx_starved_frames++;
                else
                    slot->tx_silence_frames++;
            }
            bp_mtx_unlock(&g_audio_lock);
        }

        auframe_init(&af, st->prm.fmt, st->sampv, st->sampc, st->prm.srate, st->prm.ch);
        af.timestamp = t * 1000;

        st->rh(&af, st->arg);

        /* Absolute deadline: a late tick shortens the next sleep instead
         * of shifting every one after it — relative sleeps drift. */
        t += st->prm.ptime;
        dt = (int)(t - tmr_jiffies());
        if (dt > 2)
            sys_msleep(dt);
    }
    return 0;
}

static int src_alloc(struct ausrc_st **stp, const struct ausrc *as, struct ausrc_prm *prm,
                     const char *device, ausrc_read_h *rh, ausrc_error_h *errh, void *arg)
{
    struct ausrc_st *st;
    struct bp_audio_slot *slot;
    uint32_t handle;
    int err = 0;
    (void)as;
    (void)device; /* aumem has no devices */
    (void)errh;

    if (!stp || !prm || !rh)
        return EINVAL;
    if (prm->fmt != AUFMT_S16LE) {
        warning("aumem: unsupported source sample format (%s)\n", aufmt_name(prm->fmt));
        return ENOTSUP;
    }
    if (!prm->srate || !prm->ch || !prm->ptime)
        return EINVAL;

    handle = call_handle_for_audio(arg);
    if (!handle) {
        warning("aumem: no call found for this source stream\n");
        return EINVAL;
    }

    st = mem_zalloc(sizeof(*st), src_destructor);
    if (!st)
        return ENOMEM;

    st->call_handle = handle;
    st->prm = *prm;
    st->rh = rh;
    st->arg = arg;
    st->sampc = (size_t)prm->srate * prm->ch * prm->ptime / 1000;
    st->frame_bytes = st->sampc * aufmt_sample_size(prm->fmt);
    st->sampv = mem_zalloc(st->frame_bytes, NULL);
    st->ring =
        bp_ring_alloc(prm->srate * prm->ch * aufmt_sample_size(prm->fmt) * BP_AUDIO_RING_SECS);
    if (!st->sampv || !st->ring) {
        err = ENOMEM;
        goto out;
    }

    re_atomic_rlx_set(&st->run, true);
    err = thread_create_name(&st->thread, "aumem_src", src_thread, st);
    if (err) {
        re_atomic_rlx_set(&st->run, false);
        goto out;
    }
    st->started = true;

    /* Publish last: Python can reach the ring the moment this unlocks. */
    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    slot = slot_find_or_create(handle);
    if (slot) {
        slot->tx = st->ring;
        slot->tx_srate = prm->srate;
        slot->tx_ch = prm->ch;
        slot->tx_ptime = prm->ptime;
    }
    bp_mtx_unlock(&g_audio_lock);
    if (!slot) {
        warning("aumem: audio slot table full (%d)\n", BP_AUDIO_SLOTS);
        err = ENOMEM;
    }

out:
    if (err)
        mem_deref(st);
    else
        *stp = st;
    return err;
}

/* -- receive: the "aumem" decode filter ------------------------------- */

struct aumem_dec_st {
    struct aufilt_dec_st af; /* base class; must be first */
    uint32_t call_handle;
    bp_ring *ring;
    bool bypass;
};

static void dec_destructor(void *v)
{
    struct aumem_dec_st *st = v;

    list_unlink(&st->af.le);
    slot_unpublish(st->call_handle, st->ring);
    bp_ring_free(st->ring);
}

static int dec_update(struct aufilt_dec_st **stp, void **ctx, const struct aufilt *af,
                      struct aufilt_prm *prm, const struct audio *au)
{
    struct aumem_dec_st *st;
    struct bp_audio_slot *slot;
    uint32_t handle;
    (void)ctx;
    (void)af;

    if (!stp || !prm)
        return EINVAL;

    st = mem_zalloc(sizeof(*st), dec_destructor);
    if (!st)
        return ENOMEM;

    /* Attach even when we cannot tap: a filter that declines with an
     * error would be logged against every call, and a NULL state with
     * a zero return crashes the caller. Bypass is the graceful shape. */
    handle = call_handle_for_audio(au);
    if (!handle || prm->fmt != AUFMT_S16LE || !prm->srate || !prm->ch) {
        st->bypass = true;
        goto out;
    }

    st->call_handle = handle;
    st->ring =
        bp_ring_alloc(prm->srate * prm->ch * aufmt_sample_size(prm->fmt) * BP_AUDIO_RING_SECS);
    if (!st->ring) {
        st->bypass = true;
        goto out;
    }

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    slot = slot_find_or_create(handle);
    if (slot) {
        slot->rx = st->ring;
        slot->rx_srate = prm->srate;
        slot->rx_ch = prm->ch;
    }
    bp_mtx_unlock(&g_audio_lock);
    if (!slot) {
        warning("aumem: audio slot table full (%d)\n", BP_AUDIO_SLOTS);
        bp_ring_free(st->ring);
        st->ring = NULL;
        st->bypass = true;
    }

out:
    *stp = (struct aufilt_dec_st *)st;
    return 0;
}

static int dec_frame(struct aufilt_dec_st *stf, struct auframe *af)
{
    struct aumem_dec_st *st = (struct aumem_dec_st *)stf;

    if (!st->bypass && af && af->sampv)
        bp_ring_write(st->ring, af->sampv, (uint32_t)auframe_size(af));
    /* A full ring rejects the frame (counted by the ring); the
     * reader-side catch-up in bp_audio_read keeps a live reader
     * current, so full means the reader is entirely absent. */
    return 0;
}

static struct aufilt g_aufilt = {
    .name = "aumem",
    .enabled = true,
    .decupdh = dec_update,
    .dech = dec_frame,
};

/* -- the "aumem" player: a clock that discards ------------------------ */

struct auplay_st {
    struct auplay_prm prm; /* negotiated format; ptime sets the pull cadence */
    size_t sampc;          /* samples per ptime frame */
    size_t frame_bytes;    /* bytes per ptime frame */
    void *sampv;           /* scratch frame the core fills; dropped each tick */
    auplay_write_h *wh;    /* the core's pull handler (drains its aubuf) */
    void *arg;             /* opaque state for wh; private to the core */
    thrd_t thread;         /* pacing thread driving wh once per ptime */
    RE_ATOMIC bool run;    /* cleared by the destructor to stop the thread */
    bool started;          /* thread was launched; destructor joins only then */
};

static void play_destructor(void *v)
{
    struct auplay_st *st = v;

    if (st->started) {
        re_atomic_rlx_set(&st->run, false);
        thrd_join(st->thread, NULL);
    }
    mem_deref(st->sampv);
}

static int play_thread(void *v)
{
    struct auplay_st *st = v;
    uint64_t t = tmr_jiffies();

    while (re_atomic_rlx(&st->run)) {
        struct auframe af;
        int dt;

        auframe_init(&af, st->prm.fmt, st->sampv, st->sampc, st->prm.srate, st->prm.ch);
        af.timestamp = t * 1000;

        /* The core's receive path is: decoder -> decode filters ->
         * aubuf (its playback buffer) -> player. Our decode filter
         * already copied this PCM into the call's RX ring one stage
         * upstream, but the core still queues it into aubuf and needs
         * a device to drain that at ptime rate — calls cannot even
         * answer without a player. wh() copies the next frame out of
         * aubuf into sampv; a real driver would hand it to a sound
         * card, we just let the next tick overwrite it. What is
         * dropped here is a duplicate — Python's copy was taken
         * before this buffer. */
        st->wh(&af, st->arg);

        t += st->prm.ptime;
        dt = (int)(t - tmr_jiffies());
        if (dt > 2)
            sys_msleep(dt);
    }
    return 0;
}

static int play_alloc(struct auplay_st **stp, const struct auplay *ap, struct auplay_prm *prm,
                      const char *device, auplay_write_h *wh, void *arg)
{
    struct auplay_st *st;
    int err;
    (void)ap;
    (void)device; /* aumem has no devices */

    if (!stp || !prm || !wh)
        return EINVAL;
    if (!prm->srate || !prm->ch || !prm->ptime)
        return EINVAL;

    st = mem_zalloc(sizeof(*st), play_destructor);
    if (!st)
        return ENOMEM;

    st->prm = *prm;
    st->wh = wh;
    st->arg = arg;
    st->sampc = (size_t)prm->srate * prm->ch * prm->ptime / 1000;
    st->frame_bytes = st->sampc * aufmt_sample_size(prm->fmt);
    st->sampv = mem_zalloc(st->frame_bytes, NULL);
    if (!st->sampv) {
        err = ENOMEM;
        goto out;
    }

    re_atomic_rlx_set(&st->run, true);
    err = thread_create_name(&st->thread, "aumem_play", play_thread, st);
    if (err) {
        re_atomic_rlx_set(&st->run, false);
        goto out;
    }
    st->started = true;

out:
    if (err)
        mem_deref(st);
    else
        *stp = st;
    return err;
}

/* -- the Python-facing accessors (any thread) ------------------------- */

int bp_audio_probe(uint32_t call_handle, struct bp_audio_info *info)
{
    struct bp_audio_slot *slot;

    if (!info)
        return EINVAL;

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    slot = g_audio_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        bp_mtx_unlock(&g_audio_lock);
        return ENOENT;
    }

    memset(info, 0, sizeof(*info));
    info->epoch = slot->epoch;
    info->tx_ready = slot->tx != NULL;
    info->rx_ready = slot->rx != NULL;
    info->tx_srate = slot->tx_srate;
    info->tx_ch = slot->tx_ch;
    info->tx_ptime = slot->tx_ptime;
    info->rx_srate = slot->rx_srate;
    info->rx_ch = slot->rx_ch;
    if (slot->tx) {
        info->tx_fill = bp_ring_size(slot->tx);
        info->tx_capacity = bp_ring_capacity(slot->tx);
    }
    if (slot->rx) {
        info->rx_fill = bp_ring_size(slot->rx);
        info->rx_capacity = bp_ring_capacity(slot->rx);
    }
    bp_mtx_unlock(&g_audio_lock);
    return 0;
}

int32_t bp_audio_write(uint32_t call_handle, uint32_t epoch, const uint8_t *src, uint32_t len)
{
    struct bp_audio_slot *slot;
    int32_t n = 0;

    if (!src || !len)
        return 0;
    if (len > INT32_MAX)
        len = INT32_MAX;

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    slot = g_audio_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        bp_mtx_unlock(&g_audio_lock);
        return -ENOENT;
    }
    if (slot->epoch != epoch) {
        bp_mtx_unlock(&g_audio_lock);
        return -ESTALE;
    }
    if (slot->tx)
        n = (int32_t)bp_ring_write(slot->tx, src, len);
    bp_mtx_unlock(&g_audio_lock);
    return n;
}

int bp_audio_flush_tx(uint32_t call_handle, uint32_t epoch)
{
    struct bp_audio_slot *slot;

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    slot = g_audio_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        bp_mtx_unlock(&g_audio_lock);
        return -ENOENT;
    }
    if (slot->epoch != epoch) {
        bp_mtx_unlock(&g_audio_lock);
        return -ESTALE;
    }
    if (slot->tx)
        re_atomic_rlx_set(&slot->tx_flush_req, true);
    bp_mtx_unlock(&g_audio_lock);
    return 0;
}

int32_t bp_audio_read(uint32_t call_handle, uint32_t epoch, uint8_t *dst, uint32_t len)
{
    struct bp_audio_slot *slot;
    int32_t n = 0;

    if (!dst || !len)
        return 0;
    if (len > INT32_MAX)
        len = INT32_MAX;

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    slot = g_audio_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        bp_mtx_unlock(&g_audio_lock);
        return -ENOENT;
    }
    if (slot->epoch != epoch) {
        bp_mtx_unlock(&g_audio_lock);
        return -ESTALE;
    }
    if (slot->rx) {
        uint32_t cap = bp_ring_capacity(slot->rx);
        uint32_t fill = bp_ring_size(slot->rx);

        slot->rx_armed = true;

        /* Drop-oldest, done on the consumer side where SPSC allows it:
         * discard into dst, which the real read below overwrites. The
         * ring cannot tell these reads from real ones, so the discard
         * is counted here — it is the drop the stats exist for. */
        if (fill > cap / BP_AUDIO_CLAMP_TRIGGER_DIV) {
            uint32_t excess = fill - cap / BP_AUDIO_CLAMP_KEEP_DIV;

            while (excess) {
                uint32_t got = bp_ring_read(slot->rx, dst, excess < len ? excess : len);

                if (!got)
                    break;
                excess -= got;
                slot->rx_discarded += got;
            }
        }
        n = (int32_t)bp_ring_read(slot->rx, dst, len);
    }
    bp_mtx_unlock(&g_audio_lock);
    return n;
}

int bp_audio_stats_get(uint32_t call_handle, struct bp_audio_stats *out)
{
    struct bp_audio_slot *slot;
    struct bp_ring_stats rs;

    if (!out)
        return EINVAL;

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    slot = g_audio_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        bp_mtx_unlock(&g_audio_lock);
        return ENOENT;
    }

    memset(out, 0, sizeof(*out));
    out->epoch = slot->epoch;
    out->tx_silence_frames = slot->tx_silence_frames;
    out->tx_starved_frames = slot->tx_starved_frames;
    out->tx_flushed = slot->tx_flushed;
    out->rx_discarded = slot->rx_discarded;
    if (slot->tx) {
        bp_ring_stats_get(slot->tx, &rs);
        out->tx_rejected = rs.dropped;
        out->tx_high_water = rs.high_water;
        out->tx_fill = bp_ring_size(slot->tx);
    }
    if (slot->rx) {
        bp_ring_stats_get(slot->rx, &rs);
        out->rx_dropped = rs.dropped;
        out->rx_high_water = rs.high_water;
        out->rx_fill = bp_ring_size(slot->rx);
    }
    bp_mtx_unlock(&g_audio_lock);
    return 0;
}

/* -- health sampling (re thread) -------------------------------------- */

static struct tmr g_health_tmr;

struct health_warn {
    uint32_t call_handle;
    bool tx;
    uint64_t amount; /* starved frames (tx) or lost bytes (rx) */
};

/* Once per second: per-window deltas per slot, at most one warning per
 * direction. Warnings are collected under the lock but emitted after it
 * is released — bp_event_h enters Python, which must never run under
 * g_audio_lock. */
static void health_tick(void *arg)
{
    struct health_warn warns[64];
    unsigned n = 0, i;
    (void)arg;

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    for (i = 0; i < BP_AUDIO_SLOTS; i++) {
        struct bp_audio_slot *slot = &g_audio_slots[i];
        struct bp_ring_stats rs;
        uint64_t d;

        if (!slot->call_handle)
            continue;

        if (slot->tx) {
            d = slot->tx_starved_frames - slot->prev_tx_starved;
            slot->prev_tx_starved = slot->tx_starved_frames;
            if (d >= BP_AUDIO_TX_STARVED_MIN && n < RE_ARRAY_SIZE(warns)) {
                warns[n].call_handle = slot->call_handle;
                warns[n].tx = true;
                warns[n].amount = d;
                n++;
            }
        }
        if (slot->rx) {
            bp_ring_stats_get(slot->rx, &rs);
            d = (rs.dropped - slot->prev_rx_dropped) +
                (slot->rx_discarded - slot->prev_rx_discarded);
            slot->prev_rx_dropped = rs.dropped;
            slot->prev_rx_discarded = slot->rx_discarded;
            if (d && slot->rx_armed && n < RE_ARRAY_SIZE(warns)) {
                warns[n].call_handle = slot->call_handle;
                warns[n].tx = false;
                warns[n].amount = d;
                n++;
            }
        }
    }
    bp_mtx_unlock(&g_audio_lock);

    for (i = 0; i < n; i++) {
        char json[256];

        /* Static ASCII text plus numbers: JSON-safe without escaping. */
        if (warns[i].tx)
            re_snprintf(json, sizeof(json),
                        "{\"call\":%u,\"text\":\"transmit: application is not feeding "
                        "audio fast enough (%llu frame(s) ran short in the last second)\"}",
                        warns[i].call_handle, (unsigned long long)warns[i].amount);
        else
            re_snprintf(json, sizeof(json),
                        "{\"call\":%u,\"text\":\"receive: application is not reading "
                        "audio fast enough (%llu byte(s) of audio lost in the last second)\"}",
                        warns[i].call_handle, (unsigned long long)warns[i].amount);
        bp_event_h(BP_EV_BASE + BP_EV_AUDIO_WARNING, warns[i].call_handle, json);
    }

    tmr_start(&g_health_tmr, BP_AUDIO_HEALTH_MS, health_tick, NULL);
}

/* -- lifecycle, called from shim.c ------------------------------------ */

int bp_aumem_register(void)
{
    int err;

    err = ausrc_register(&g_ausrc, baresip_ausrcl(), "aumem", src_alloc);
    err |= auplay_register(&g_auplay, baresip_auplayl(), "aumem", play_alloc);
    if (err) {
        g_ausrc = mem_deref(g_ausrc);
        g_auplay = mem_deref(g_auplay);
        return err;
    }
    aufilt_register(baresip_aufiltl(), &g_aufilt);

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    g_audio_open = true;
    bp_mtx_unlock(&g_audio_lock);

    tmr_start(&g_health_tmr, BP_AUDIO_HEALTH_MS, health_tick, NULL);
    return 0;
}

void bp_aumem_unregister(void)
{
    size_t i;

    tmr_cancel(&g_health_tmr);

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    g_audio_open = false;
    for (i = 0; i < BP_AUDIO_SLOTS; i++)
        memset(&g_audio_slots[i], 0, sizeof(g_audio_slots[i]));
    bp_mtx_unlock(&g_audio_lock);

    aufilt_unregister(&g_aufilt);
    g_ausrc = mem_deref(g_ausrc);
    g_auplay = mem_deref(g_auplay);
}

void bp_aumem_slot_drop(uint32_t call_handle)
{
    struct bp_audio_slot *slot;

    if (!call_handle)
        return;

    call_once(&g_audio_lock_once, audio_lock_init);
    bp_mtx_lock(&g_audio_lock);
    slot = slot_find(call_handle);
    if (slot)
        memset(slot, 0, sizeof(*slot));
    bp_mtx_unlock(&g_audio_lock);
}
