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

struct bp_audio_slot {
    uint32_t call_handle; /* 0 = slot free */
    uint32_t epoch;       /* bumped on every unpublish */
    bp_ring *tx;          /* Python writes, source thread reads */
    bp_ring *rx;          /* decode filter writes, Python reads */
    uint32_t tx_srate, tx_ch, tx_ptime;
    uint32_t rx_srate, rx_ch;
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
    mtx_lock(&g_audio_lock);
    slot = slot_find(call_handle);
    if (slot) {
        if (slot->tx == ring)
            slot->tx = NULL;
        if (slot->rx == ring)
            slot->rx = NULL;
        slot->epoch++;
    }
    mtx_unlock(&g_audio_lock);
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
    bp_ring *ring; /* owned; freed only after the thread is joined */
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

        got = bp_ring_read(st->ring, st->sampv, (uint32_t)st->frame_bytes);
        if (got < st->frame_bytes)
            memset((uint8_t *)st->sampv + got, 0, st->frame_bytes - got);

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
    mtx_lock(&g_audio_lock);
    slot = slot_find_or_create(handle);
    if (slot) {
        slot->tx = st->ring;
        slot->tx_srate = prm->srate;
        slot->tx_ch = prm->ch;
        slot->tx_ptime = prm->ptime;
    }
    mtx_unlock(&g_audio_lock);
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
    mtx_lock(&g_audio_lock);
    slot = slot_find_or_create(handle);
    if (slot) {
        slot->rx = st->ring;
        slot->rx_srate = prm->srate;
        slot->rx_ch = prm->ch;
    }
    mtx_unlock(&g_audio_lock);
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
    mtx_lock(&g_audio_lock);
    slot = g_audio_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        mtx_unlock(&g_audio_lock);
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
    mtx_unlock(&g_audio_lock);
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
    mtx_lock(&g_audio_lock);
    slot = g_audio_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        mtx_unlock(&g_audio_lock);
        return -ENOENT;
    }
    if (slot->epoch != epoch) {
        mtx_unlock(&g_audio_lock);
        return -ESTALE;
    }
    if (slot->tx)
        n = (int32_t)bp_ring_write(slot->tx, src, len);
    mtx_unlock(&g_audio_lock);
    return n;
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
    mtx_lock(&g_audio_lock);
    slot = g_audio_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        mtx_unlock(&g_audio_lock);
        return -ENOENT;
    }
    if (slot->epoch != epoch) {
        mtx_unlock(&g_audio_lock);
        return -ESTALE;
    }
    if (slot->rx) {
        uint32_t cap = bp_ring_capacity(slot->rx);
        uint32_t fill = bp_ring_size(slot->rx);

        /* Drop-oldest, done on the consumer side where SPSC allows it:
         * discard into dst, which the real read below overwrites. */
        if (fill > cap / BP_AUDIO_CLAMP_TRIGGER_DIV) {
            uint32_t excess = fill - cap / BP_AUDIO_CLAMP_KEEP_DIV;

            while (excess) {
                uint32_t got = bp_ring_read(slot->rx, dst, excess < len ? excess : len);

                if (!got)
                    break;
                excess -= got;
            }
        }
        n = (int32_t)bp_ring_read(slot->rx, dst, len);
    }
    mtx_unlock(&g_audio_lock);
    return n;
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
    mtx_lock(&g_audio_lock);
    g_audio_open = true;
    mtx_unlock(&g_audio_lock);
    return 0;
}

void bp_aumem_unregister(void)
{
    size_t i;

    call_once(&g_audio_lock_once, audio_lock_init);
    mtx_lock(&g_audio_lock);
    g_audio_open = false;
    for (i = 0; i < BP_AUDIO_SLOTS; i++)
        memset(&g_audio_slots[i], 0, sizeof(g_audio_slots[i]));
    mtx_unlock(&g_audio_lock);

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
    mtx_lock(&g_audio_lock);
    slot = slot_find(call_handle);
    if (slot)
        memset(slot, 0, sizeof(*slot));
    mtx_unlock(&g_audio_lock);
}
