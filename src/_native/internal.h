/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef BP_INTERNAL_H
#define BP_INTERNAL_H

#include <stdint.h>

/* Cross-file plumbing between the native translation units. Nothing here
 * is part of the Python-visible surface — that surface is shim.h. */

struct call;

/* shim.c: the handle already issued for a call, or 0 if it has none.
 * Find-only — never creates an entry. Re thread only. */
uint32_t bp_call_handle_find(const struct call *call);

/* aumem.c: the programmatic audio driver. Registered by bp_loop_init
 * once the core is up; unregistered during teardown, after ua_close has
 * freed every stream instance. slot_drop runs in CALL_CLOSED processing,
 * mirroring the handle table's own discipline. */
int bp_aumem_register(void);
void bp_aumem_unregister(void);
void bp_aumem_slot_drop(uint32_t call_handle);

#endif /* BP_INTERNAL_H */
