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
#include <baresip.h>

#include "shim.h"

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

int bp_loop_init(void)
{
    int err;

    call_once(&g_lock_once, lock_init);
    mtx_lock(&g_lock);

    if (g_mq) {
        mtx_unlock(&g_lock);
        fprintf(stderr, "baresip shim: bp_loop_init called twice without bp_loop_done\n");
        return EALREADY;
    }

    err = re_thread_init();
    if (err) {
        mtx_unlock(&g_lock);
        return err;
    }

    err = mqueue_alloc(&g_mq, cmd_handler, NULL);
    if (err) {
        mtx_unlock(&g_lock);
        re_thread_close();
        return err;
    }

    g_running = true;
    mtx_unlock(&g_lock);
    return 0;
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
    if (g_in_loop) {
        mtx_unlock(&g_lock);
        fprintf(stderr, "baresip shim: bp_loop_done called while the loop is still "
                        "running; refusing to free a live command queue\n");
        return EBUSY;
    }

    g_running = false;
    g_mq = mem_deref(g_mq);
    mtx_unlock(&g_lock);

    re_thread_close();
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
