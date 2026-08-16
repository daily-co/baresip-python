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
#include <re_atomic.h>
#include <baresip.h>

#include "shim.h"

/* Payload carried through the mqueue pipe. Allocated with plain malloc so
 * that bp_cmd stays safe from any thread; freed by cmd_handler on the re
 * thread after dispatch. */
struct bp_cmd_msg {
    uint32_t handle;
    char *json; /* heap copy, may be NULL */
};

static struct mqueue *g_mq = NULL;

/* Gate closed => bp_cmd rejects pushes with ESHUTDOWN. Closed by the
 * BP_CMD_STOP handler (before re_cancel) and by bp_loop_done, so no new
 * command can race the queue teardown. */
static RE_ATOMIC bool g_running = false;

const char *bp_version(void)
{
    return sys_libre_version_get();
}

int bp_init(void)
{
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
        re_atomic_rlx_set(&g_running, false);
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

    err = re_thread_init();
    if (err)
        return err;

    err = mqueue_alloc(&g_mq, cmd_handler, NULL);
    if (err) {
        re_thread_close();
        return err;
    }

    re_atomic_rlx_set(&g_running, true);
    return 0;
}

int bp_loop_run(void)
{
    return re_main(NULL);
}

void bp_loop_done(void)
{
    re_atomic_rlx_set(&g_running, false);
    g_mq = mem_deref(g_mq);
    re_thread_close();
}

int bp_cmd(int cmd, uint32_t handle, const char *json_args)
{
    struct bp_cmd_msg *msg;
    int err;

    if (!re_atomic_rlx(&g_running))
        return ESHUTDOWN;

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

    err = mqueue_push(g_mq, cmd, msg);
    if (err) {
        free(msg->json);
        free(msg);
    }
    return err;
}
