/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <re_atomic.h>
#include <re.h>
#include <rem.h>
#include <baresip.h>

#include "bp_sync.h"
#include "shim.h"
#include "internal.h"

/* vidmem: programmatic per-call video frames, three cooperating pieces.
 *
 *   TX   Python --> frame ring --> pacing thread --> VP8 encoder --> RTP
 *   RX   RTP --> decoder --> decode filter --> frame ring --> Python
 *
 * The transmit half is a video source ("vidmem"). Unlike audio, its
 * alloc handler cannot be correlated to a call: the arg is an interior
 * pointer into the opaque `struct video`, and no public API maps it
 * back. Correlation arrives through the DEVICE STRING instead: when a
 * call with video establishes, shim.c calls bp_vidmem_call_established,
 * which re-points the call's source at "vidmem" with the device set to
 * "h<handle>" (public video_set_source), and pins that device name for
 * later renegotiation re-allocs (video_set_devicename). An instance
 * alloc'd with any other device — the brief config-default window
 * before the swap — stays inert and pushes nothing.
 *
 * The pacing thread ticks at the negotiated fps and pushes the newest
 * frame Python has written since the last tick; no new frame means no
 * push — pushing nothing is video's silence, the encoder simply emits
 * no RTP for that interval. The VP8 encode runs synchronously inside
 * the push, so this thread pays the encode cost.
 *
 * The receive half is a decode FILTER, not the display: filter update
 * handlers officially receive the call's `const struct video *`, which
 * the public UA/call lists turn back into a handle, and the core runs
 * decode filters on every frame (a copy) before the display stage. The
 * "vidmem" DISPLAY still exists, as a do-nothing sink, because a
 * registered display is an SDP capability: with none anywhere, the core
 * masks the video direction to sendonly and no one ever receives
 * (call.c, call_set_mdir).
 *
 * Rings are per-direction frame-slot rings: whole frames only, so a
 * slow reader never sees a torn or partial frame. Frames are packed
 * I420 (tight strides: w, w/2, w/2), geometry fixed by the runtime's
 * configured video size — per-call geometry is out of scope, and
 * received frames that do not fit the configured slot are dropped and
 * counted. The slot table, the lock discipline, and the epoch contract
 * (bump on unpublish, -ESTALE to Python afterwards) mirror aumem.c.
 */

#define BP_VIDEO_SLOTS 64
#define BP_VIDEO_RING_SLOTS 4

/* A reader more than this many frames behind is skipped forward to the
 * newest frame (consumer-side, where SPSC allows it): live video stays
 * live. The pacing thread reads newest-only by the same mechanism. */
#define BP_VIDEO_CLAMP_TRIGGER 2

/* -- frame-slot ring: SPSC, whole frames, drop counted ---------------- */

struct bp_vslot_hdr {
    uint32_t size; /* payload bytes (0 = slot unused) */
    uint32_t w, h;
    uint64_t ts; /* microseconds (VIDEO_TIMEBASE units) */
};

struct bp_vring {
    uint32_t nslots;
    uint32_t cap; /* max payload bytes per slot */
    RE_ATOMIC uint64_t wr;
    RE_ATOMIC uint64_t rd;
    RE_ATOMIC uint64_t dropped; /* writer-side: ring full, frame refused */
    uint8_t *buf;               /* nslots * (hdr + cap) */
};

static struct bp_vslot_hdr *vring_slot(struct bp_vring *r, uint64_t seq)
{
    return (struct bp_vslot_hdr *)(void *)(r->buf + (seq % r->nslots) *
                                                        (sizeof(struct bp_vslot_hdr) + r->cap));
}

static struct bp_vring *vring_alloc(uint32_t nslots, uint32_t cap)
{
    struct bp_vring *r = mem_zalloc(sizeof(*r), NULL);

    if (!r)
        return NULL;
    r->nslots = nslots;
    r->cap = cap;
    r->buf = mem_zalloc((size_t)nslots * (sizeof(struct bp_vslot_hdr) + cap), NULL);
    if (!r->buf) {
        mem_deref(r);
        return NULL;
    }
    return r;
}

static void vring_free(struct bp_vring *r)
{
    if (!r)
        return;
    mem_deref(r->buf);
    mem_deref(r);
}

/* Producer side. Returns 0 on accept, -ENOSPC when the ring is full
 * (the frame is refused and counted — the consumer-side clamp keeps a
 * live reader current, so full means the reader is entirely absent),
 * -EMSGSIZE when the frame exceeds the slot capacity. `copy` fills the
 * slot payload; it runs only for an accepted frame. */
static int vring_write(struct bp_vring *r, uint32_t size, uint32_t w, uint32_t h, uint64_t ts,
                       void (*copy)(uint8_t *dst, void *arg), void *arg)
{
    uint64_t wr = re_atomic_rlx(&r->wr);
    struct bp_vslot_hdr *slot;

    if (size > r->cap) {
        return -EMSGSIZE;
    }
    if (wr - re_atomic_acq(&r->rd) >= r->nslots) {
        re_atomic_rlx_add(&r->dropped, 1);
        return -ENOSPC;
    }
    slot = vring_slot(r, wr);
    slot->size = size;
    slot->w = w;
    slot->h = h;
    slot->ts = ts;
    copy((uint8_t *)(slot + 1), arg);
    re_atomic_rls_set(&r->wr, wr + 1);
    return 0;
}

/* Consumer read modes: RAW takes the oldest frame verbatim (the test
 * loop); CLAMPED skips to the newest once more than the clamp trigger
 * behind (Python reads — live video stays live); NEWEST always skips
 * to the newest (the pacing thread). */
enum vring_mode { VRING_RAW, VRING_CLAMPED, VRING_NEWEST };

/* Consumer side. Returns payload bytes copied, 0 when nothing new,
 * -EMSGSIZE when the caller's buffer is too small for the frame (the
 * frame is left queued). */
static int32_t vring_read(struct bp_vring *r, uint8_t *dst, uint32_t max, uint32_t *w, uint32_t *h,
                          uint64_t *ts, enum vring_mode mode, uint64_t *skipped)
{
    uint64_t wr = re_atomic_acq(&r->wr);
    uint64_t rd = re_atomic_rlx(&r->rd);
    struct bp_vslot_hdr *slot;

    if (wr == rd)
        return 0;
    if (mode == VRING_NEWEST || (mode == VRING_CLAMPED && wr - rd > BP_VIDEO_CLAMP_TRIGGER)) {
        if (skipped)
            *skipped += (wr - 1) - rd;
        rd = wr - 1;
    }
    slot = vring_slot(r, rd);
    if (slot->size > max)
        return -EMSGSIZE;
    memcpy(dst, slot + 1, slot->size);
    if (w)
        *w = slot->w;
    if (h)
        *h = slot->h;
    if (ts)
        *ts = slot->ts;
    re_atomic_rls_set(&r->rd, rd + 1);
    return (int32_t)slot->size;
}

/* -- slot table (call handle -> ring pair), all under g_video_lock ---- */

struct bp_video_slot {
    uint32_t call_handle;   /* 0 = slot free */
    uint32_t epoch;         /* bumped on every unpublish */
    struct bp_vring *tx;    /* Python writes, pacing thread reads */
    struct bp_vring *rx;    /* decode filter writes, Python reads */
    uint32_t width, height; /* configured geometry, both directions */
    uint32_t fps_x1000;
    uint64_t tx_frames;   /* frames the pacer handed to the encoder */
    uint64_t tx_skipped;  /* stale frames the pacer skipped past */
    uint64_t rx_frames;   /* decoded frames delivered into the ring */
    uint64_t rx_oversize; /* received frames too big for the slot */
};

static struct vidsrc *g_vidsrc;
static struct vidisp *g_vidisp;
static struct bp_video_slot g_video_slots[BP_VIDEO_SLOTS];
static bool g_video_open;

/* Process-lifetime, like aumem's: Python threads may call the accessors
 * at any moment relative to loop lifecycles. */
static mtx_t g_video_lock;
static once_flag g_video_lock_once = ONCE_FLAG_INIT;

static void video_lock_init(void)
{
    mtx_init(&g_video_lock, mtx_plain);
}

static struct bp_video_slot *slot_find(uint32_t call_handle)
{
    size_t i;

    if (!call_handle)
        return NULL;
    for (i = 0; i < BP_VIDEO_SLOTS; i++) {
        if (g_video_slots[i].call_handle == call_handle)
            return &g_video_slots[i];
    }
    return NULL;
}

static struct bp_video_slot *slot_find_or_create(uint32_t call_handle)
{
    struct bp_video_slot *slot = slot_find(call_handle);
    size_t i;

    if (slot)
        return slot;
    for (i = 0; i < BP_VIDEO_SLOTS; i++) {
        if (!g_video_slots[i].call_handle) {
            memset(&g_video_slots[i], 0, sizeof(g_video_slots[i]));
            g_video_slots[i].call_handle = call_handle;
            return &g_video_slots[i];
        }
    }
    return NULL;
}

/* Mirror of aumem's slot_unpublish: runs in stream destructors, after
 * which no Python thread can reach the ring and freeing it is safe. */
static void slot_unpublish(uint32_t call_handle, struct bp_vring *ring)
{
    struct bp_video_slot *slot;

    if (!ring)
        return;

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    slot = slot_find(call_handle);
    if (slot) {
        if (slot->tx == ring) {
            slot->tx = NULL;
            slot->tx_frames = 0;
            slot->tx_skipped = 0;
        }
        if (slot->rx == ring) {
            slot->rx = NULL;
            slot->rx_frames = 0;
            slot->rx_oversize = 0;
        }
        slot->epoch++;
    }
    bp_mtx_unlock(&g_video_lock);
}

/* The decode-filter update handler receives the call's `const struct
 * video *`; the public UA and call lists turn it back into the call,
 * and the call into its handle. Re thread only. */
static uint32_t call_handle_for_video(const struct video *vid)
{
    struct le *le;

    LIST_FOREACH(uag_list(), le)
    {
        struct ua *ua = le->data;
        struct le *lec;

        LIST_FOREACH(ua_calls(ua), lec)
        {
            struct call *call = lec->data;

            if (call_video(call) == vid)
                return bp_call_handle_find(call);
        }
    }
    return 0;
}

static uint32_t i420_size(uint32_t w, uint32_t h)
{
    return w * h + 2 * ((w + 1) / 2) * ((h + 1) / 2);
}

/* -- transmit: the "vidmem" video source ------------------------------ */

struct vidsrc_st {
    uint32_t call_handle;  /* 0 = inert (unknown device) */
    struct bp_vring *ring; /* owned; freed only after the thread joins */
    uint32_t w, h;
    double fps;
    uint8_t *framebuf; /* packed I420 scratch the encoder reads */
    vidsrc_frame_h *frameh;
    void *arg;
    thrd_t thread;
    RE_ATOMIC bool run;
    bool started;
};

static void vsrc_destructor(void *v)
{
    struct vidsrc_st *st = v;

    slot_unpublish(st->call_handle, st->ring);
    if (st->started) {
        re_atomic_rlx_set(&st->run, false);
        thrd_join(st->thread, NULL);
    }
    vring_free(st->ring);
    mem_deref(st->framebuf);
}

static int vsrc_thread(void *v)
{
    struct vidsrc_st *st = v;
    uint64_t t = tmr_jiffies();
    const uint32_t tick_ms = (uint32_t)(1000.0 / st->fps);

    while (re_atomic_rlx(&st->run)) {
        uint64_t skipped = 0;
        uint64_t ts = 0;
        uint32_t w = 0, h = 0;
        int32_t got;
        int dt;

        got = vring_read(st->ring, st->framebuf, i420_size(st->w, st->h), &w, &h, &ts, VRING_NEWEST,
                         &skipped);
        if (got > 0) {
            struct vidframe vf;
            struct vidsz sz = {.w = w, .h = h};
            struct bp_video_slot *slot;

            vidframe_init_buf(&vf, VID_FMT_YUV420P, &sz, st->framebuf);
            st->frameh(&vf, ts, st->arg);

            call_once(&g_video_lock_once, video_lock_init);
            bp_mtx_lock(&g_video_lock);
            slot = slot_find(st->call_handle);
            if (slot) {
                slot->tx_frames++;
                slot->tx_skipped += skipped;
            }
            bp_mtx_unlock(&g_video_lock);
        }
        /* Nothing new: push nothing. The encoder emits no RTP for the
         * interval — video's silence. */

        t += tick_ms;
        dt = (int)(t - tmr_jiffies());
        if (dt > 2)
            sys_msleep(dt);
    }
    return 0;
}

static int vsrc_alloc(struct vidsrc_st **stp, const struct vidsrc *vs, struct vidsrc_prm *prm,
                      const struct vidsz *size, const char *fmt, const char *dev,
                      vidsrc_frame_h *frameh, vidsrc_packet_h *packeth, vidsrc_error_h *errorh,
                      void *arg)
{
    struct vidsrc_st *st;
    struct bp_video_slot *slot = NULL;
    uint32_t handle = 0;
    int err = 0;
    (void)vs;
    (void)fmt;
    (void)packeth;
    (void)errorh;

    if (!stp || !prm || !size || !frameh)
        return EINVAL;

    st = mem_zalloc(sizeof(*st), vsrc_destructor);
    if (!st)
        return ENOMEM;

    /* The device string carries the call handle ("h<n>"), placed there
     * by bp_vidmem_call_established. Anything else — the config-default
     * window before the swap — makes an inert instance: no ring, no
     * thread, nothing pushed. */
    if (dev && dev[0] == 'h')
        handle = (uint32_t)strtoul(dev + 1, NULL, 10);
    if (!handle || !size->w || !size->h || !(prm->fps > 0.0))
        goto out; /* inert */

    st->call_handle = handle;
    st->w = size->w;
    st->h = size->h;
    st->fps = prm->fps;
    st->frameh = frameh;
    st->arg = arg;
    st->framebuf = mem_zalloc(i420_size(st->w, st->h), NULL);
    st->ring = vring_alloc(BP_VIDEO_RING_SLOTS, i420_size(st->w, st->h));
    if (!st->framebuf || !st->ring) {
        err = ENOMEM;
        goto out;
    }

    re_atomic_rlx_set(&st->run, true);
    err = thread_create_name(&st->thread, "vidmem_src", vsrc_thread, st);
    if (err) {
        re_atomic_rlx_set(&st->run, false);
        goto out;
    }
    st->started = true;

    /* Publish last: Python can reach the ring the moment this unlocks. */
    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    slot = slot_find_or_create(handle);
    if (slot) {
        slot->tx = st->ring;
        slot->width = st->w;
        slot->height = st->h;
        slot->fps_x1000 = (uint32_t)(st->fps * 1000.0);
    }
    bp_mtx_unlock(&g_video_lock);
    if (!slot) {
        warning("vidmem: video slot table full (%d)\n", BP_VIDEO_SLOTS);
        err = ENOMEM;
    }

out:
    if (err)
        mem_deref(st);
    else
        *stp = st;
    return err;
}

/* -- the "vidmem" display: a sink that unlocks receiving -------------- */

struct vidisp_st {
    int unused;
};

static int vdisp_alloc(struct vidisp_st **stp, const struct vidisp *vd, struct vidisp_prm *prm,
                       const char *dev, vidisp_resize_h *resizeh, void *arg)
{
    struct vidisp_st *st;
    (void)vd;
    (void)prm;
    (void)dev;
    (void)resizeh;
    (void)arg;

    if (!stp)
        return EINVAL;
    st = mem_zalloc(sizeof(*st), NULL);
    if (!st)
        return ENOMEM;
    *stp = st;
    return 0;
}

static int vdisp_display(struct vidisp_st *st, const char *title, const struct vidframe *frame,
                         uint64_t timestamp)
{
    (void)st;
    (void)title;
    (void)frame;
    (void)timestamp;
    /* Every frame here is a duplicate: the decode filter already copied
     * it into the call's RX ring one stage upstream. */
    return 0;
}

/* -- receive: the "vidmem" decode filter ------------------------------ */

struct vidmem_dec_st {
    struct vidfilt_dec_st vf; /* base class; must be first */
    const struct video *vid;  /* for lazy handle resolution */
    uint32_t call_handle;     /* 0 until the first decoded frame */
    struct bp_vring *ring;
    bool bypass;
};

static void vdec_destructor(void *v)
{
    struct vidmem_dec_st *st = v;

    list_unlink(&st->vf.le);
    slot_unpublish(st->call_handle, st->ring);
    vring_free(st->ring);
}

static int vdec_update(struct vidfilt_dec_st **stp, void **ctx, const struct vidfilt *vf,
                       struct vidfilt_prm *prm, const struct video *vid)
{
    struct vidmem_dec_st *st;
    struct config_video *cfg = &conf_config()->video;
    (void)ctx;
    (void)prm; /* may be NULL; geometry rides each frame */

    if (!stp)
        return EINVAL;
    if (*stp)
        return 0;

    st = mem_zalloc(sizeof(*st), vdec_destructor);
    if (!st)
        return ENOMEM;
    st->vf.vf = vf;

    /* Attach even when we cannot tap (aufilt lesson: declining crashes
     * the caller). Bypass is the graceful shape. The call HANDLE is not
     * resolved here: this update runs during call allocation, before
     * the handle table has an entry for the call — the first decoded
     * frame resolves it instead (vdec_frame, same re thread). */
    if (!cfg->width || !cfg->height) {
        st->bypass = true;
        goto out;
    }

    st->vid = vid;
    st->ring = vring_alloc(BP_VIDEO_RING_SLOTS, i420_size(cfg->width, cfg->height));
    if (!st->ring)
        st->bypass = true;

out:
    *stp = &st->vf;
    return 0;
}

/* First-frame publication: resolve the call handle (it exists by the
 * time media flows) and attach the ring to the slot table. */
static bool vdec_publish(struct vidmem_dec_st *st)
{
    struct config_video *cfg = &conf_config()->video;
    struct bp_video_slot *slot;
    uint32_t handle;

    handle = call_handle_for_video(st->vid);
    if (!handle)
        return false;

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    slot = slot_find_or_create(handle);
    if (slot) {
        slot->rx = st->ring;
        if (!slot->width) {
            slot->width = cfg->width;
            slot->height = cfg->height;
        }
    }
    bp_mtx_unlock(&g_video_lock);
    if (!slot) {
        warning("vidmem: video slot table full (%d)\n", BP_VIDEO_SLOTS);
        st->bypass = true;
        return false;
    }
    st->call_handle = handle;
    return true;
}

struct pack_ctx {
    const struct vidframe *frame;
};

/* Pack a possibly stride-padded vidframe into tight I420. */
static void pack_i420(uint8_t *dst, void *arg)
{
    const struct vidframe *f = ((struct pack_ctx *)arg)->frame;
    unsigned p;

    for (p = 0; p < 3; p++) {
        uint32_t w = p ? (f->size.w + 1) / 2 : f->size.w;
        uint32_t h = p ? (f->size.h + 1) / 2 : f->size.h;
        const uint8_t *src = f->data[p];
        uint32_t row;

        for (row = 0; row < h; row++) {
            memcpy(dst, src, w);
            dst += w;
            src += f->linesize[p];
        }
    }
}

static int vdec_frame(struct vidfilt_dec_st *stf, struct vidframe *frame, uint64_t *timestamp)
{
    struct vidmem_dec_st *st = (struct vidmem_dec_st *)stf;
    struct pack_ctx ctx = {.frame = frame};
    struct bp_video_slot *slot;
    int rc;

    if (st->bypass || !frame || frame->fmt != VID_FMT_YUV420P)
        return 0;
    if (!st->call_handle && !vdec_publish(st))
        return 0;

    rc = vring_write(st->ring, i420_size(frame->size.w, frame->size.h), frame->size.w,
                     frame->size.h, timestamp ? *timestamp : 0, pack_i420, &ctx);

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    slot = slot_find(st->call_handle);
    if (slot) {
        if (rc == 0)
            slot->rx_frames++;
        else if (rc == -EMSGSIZE)
            slot->rx_oversize++;
        /* -ENOSPC is counted by the ring: a reader that never existed. */
    }
    bp_mtx_unlock(&g_video_lock);
    return 0;
}

static struct vidfilt g_vidfilt = {
    .name = "vidmem",
    .decupdh = vdec_update,
    .dech = vdec_frame,
};

/* -- Python-facing accessors (any thread) ----------------------------- */

int bp_video_probe(uint32_t call_handle, struct bp_video_info *info)
{
    struct bp_video_slot *slot;

    if (!info)
        return EINVAL;

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    slot = g_video_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        bp_mtx_unlock(&g_video_lock);
        return ENOENT;
    }

    memset(info, 0, sizeof(*info));
    info->epoch = slot->epoch;
    info->tx_ready = slot->tx != NULL;
    info->rx_ready = slot->rx != NULL;
    info->width = slot->width;
    info->height = slot->height;
    info->fps_x1000 = slot->fps_x1000;
    info->tx_frames = slot->tx_frames;
    info->tx_skipped = slot->tx_skipped;
    info->rx_frames = slot->rx_frames;
    info->rx_oversize = slot->rx_oversize;
    if (slot->rx)
        info->rx_dropped = re_atomic_rlx(&slot->rx->dropped);
    bp_mtx_unlock(&g_video_lock);
    return 0;
}

struct write_ctx {
    const uint8_t *src;
    uint32_t len;
};

static void copy_write(uint8_t *dst, void *arg)
{
    struct write_ctx *c = arg;

    memcpy(dst, c->src, c->len);
}

int32_t bp_video_write(uint32_t call_handle, uint32_t epoch, const uint8_t *i420, uint32_t len,
                       uint64_t timestamp_us)
{
    struct bp_video_slot *slot;
    struct write_ctx ctx;
    int32_t rc = 0;

    if (!i420 || !len)
        return -EINVAL;

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    slot = g_video_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        bp_mtx_unlock(&g_video_lock);
        return -ENOENT;
    }
    if (slot->epoch != epoch) {
        bp_mtx_unlock(&g_video_lock);
        return -ESTALE;
    }
    if (!slot->tx) {
        /* The direction is not up (renegotiation gap, or never
         * started): the frame is refused, not an error — mirroring
         * audio's "accepts none, without error" contract. */
        bp_mtx_unlock(&g_video_lock);
        return -ENOSPC;
    }
    if (len != i420_size(slot->width, slot->height)) {
        bp_mtx_unlock(&g_video_lock);
        return -EINVAL;
    }
    ctx.src = i420;
    ctx.len = len;
    rc = vring_write(slot->tx, len, slot->width, slot->height, timestamp_us, copy_write, &ctx);
    bp_mtx_unlock(&g_video_lock);
    return rc; /* 0, or -ENOSPC (pacer absent/behind; frame refused) */
}

int32_t bp_video_read(uint32_t call_handle, uint32_t epoch, uint8_t *dst, uint32_t max_len,
                      uint32_t *width, uint32_t *height, uint64_t *timestamp_us)
{
    struct bp_video_slot *slot;
    int32_t n = 0;

    if (!dst || !max_len)
        return -EINVAL;

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    slot = g_video_open ? slot_find(call_handle) : NULL;
    if (!slot) {
        bp_mtx_unlock(&g_video_lock);
        return -ENOENT;
    }
    if (slot->epoch != epoch) {
        bp_mtx_unlock(&g_video_lock);
        return -ESTALE;
    }
    if (slot->rx)
        n = vring_read(slot->rx, dst, max_len, width, height, timestamp_us, VRING_CLAMPED, NULL);
    bp_mtx_unlock(&g_video_lock);
    return n;
}

/* -- correlation swap, called from shim.c on the re thread ------------ */

void bp_vidmem_call_established(struct call *call)
{
    struct video *v;
    struct sdp_media *m;
    char dev[16];
    uint32_t handle;

    if (str_cmp(conf_config()->video.src_mod, "vidmem"))
        return;
    v = call_video(call);
    if (!v)
        return;
    m = stream_sdpmedia(video_strm(v));
    if (!m || !(sdp_media_dir(m) & SDP_SENDONLY))
        return;
    handle = bp_call_handle_find(call);
    if (!handle)
        return;

    re_snprintf(dev, sizeof(dev), "h%u", handle);
    /* Pin the device name first so any later config-driven re-alloc
     * (hold/resume renegotiation restarts the source) keeps the handle;
     * then swap the live instance. */
    video_set_devicename(v, dev, "default");
    if (video_set_source(v, "vidmem", dev))
        warning("vidmem: video_set_source failed for call h%u\n", handle);
}

/* -- test support (shim.c BP_CMD_TEST_VIDEO_SLOT) --------------------- */

int bp_vidmem_test_slot(uint32_t call_handle, uint32_t w, uint32_t h, uint32_t fps)
{
    struct bp_video_slot *slot;
    int err = 0;

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    if (!w) {
        /* drop form */
        slot = slot_find(call_handle);
        if (slot) {
            vring_free(slot->tx);
            vring_free(slot->rx);
            memset(slot, 0, sizeof(*slot));
        }
        bp_mtx_unlock(&g_video_lock);
        return 0;
    }
    slot = slot_find_or_create(call_handle);
    if (!slot) {
        bp_mtx_unlock(&g_video_lock);
        return ENOMEM;
    }
    slot->width = w;
    slot->height = h;
    slot->fps_x1000 = fps * 1000;
    if (!slot->tx)
        slot->tx = vring_alloc(BP_VIDEO_RING_SLOTS, i420_size(w, h));
    if (!slot->rx)
        slot->rx = vring_alloc(BP_VIDEO_RING_SLOTS, i420_size(w, h));
    if (!slot->tx || !slot->rx)
        err = ENOMEM;
    bp_mtx_unlock(&g_video_lock);
    return err;
}

/* Test-only: move every queued TX frame into the RX ring, as if the
 * far end echoed it — lets unit tests drive the full write->read path
 * without a call. Returns frames moved. */
int bp_vidmem_test_loop(uint32_t call_handle)
{
    struct bp_video_slot *slot;
    int moved = 0;

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    slot = slot_find(call_handle);
    while (slot && slot->tx && slot->rx) {
        uint8_t *buf;
        uint32_t w = 0, h = 0;
        uint64_t ts = 0;
        int32_t got;
        struct write_ctx ctx;

        buf = mem_zalloc(slot->tx->cap, NULL);
        if (!buf)
            break;
        got = vring_read(slot->tx, buf, slot->tx->cap, &w, &h, &ts, VRING_RAW, NULL);
        if (got <= 0) {
            mem_deref(buf);
            break;
        }
        ctx.src = buf;
        ctx.len = (uint32_t)got;
        if (vring_write(slot->rx, (uint32_t)got, w, h, ts, copy_write, &ctx) == 0)
            moved++;
        mem_deref(buf);
    }
    bp_mtx_unlock(&g_video_lock);
    return moved;
}

/* -- lifecycle, called from shim.c ------------------------------------ */

int bp_vidmem_register(void)
{
    int err;

    err = vidsrc_register(&g_vidsrc, baresip_vidsrcl(), "vidmem", vsrc_alloc, NULL);
    err |= vidisp_register(&g_vidisp, baresip_vidispl(), "vidmem", vdisp_alloc, NULL, vdisp_display,
                           NULL);
    if (err) {
        g_vidsrc = mem_deref(g_vidsrc);
        g_vidisp = mem_deref(g_vidisp);
        return err;
    }
    vidfilt_register(baresip_vidfiltl(), &g_vidfilt);

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    g_video_open = true;
    bp_mtx_unlock(&g_video_lock);
    return 0;
}

void bp_vidmem_unregister(void)
{
    size_t i;

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    g_video_open = false;
    for (i = 0; i < BP_VIDEO_SLOTS; i++) {
        /* Test slots own their rings; stream-owned rings were already
         * unpublished (and the slots cleared) by the destructors that
         * ran during ua_close. */
        vring_free(g_video_slots[i].tx);
        vring_free(g_video_slots[i].rx);
        memset(&g_video_slots[i], 0, sizeof(g_video_slots[i]));
    }
    bp_mtx_unlock(&g_video_lock);

    vidfilt_unregister(&g_vidfilt);
    g_vidsrc = mem_deref(g_vidsrc);
    g_vidisp = mem_deref(g_vidisp);
}

void bp_vidmem_slot_drop(uint32_t call_handle)
{
    struct bp_video_slot *slot;

    if (!call_handle)
        return;

    call_once(&g_video_lock_once, video_lock_init);
    bp_mtx_lock(&g_video_lock);
    slot = slot_find(call_handle);
    if (slot)
        memset(slot, 0, sizeof(*slot));
    bp_mtx_unlock(&g_video_lock);
}
