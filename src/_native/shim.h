/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef BP_SHIM_H
#define BP_SHIM_H

/* The C surface exposed to Python. Every function is prefixed bp_ to keep
 * our symbols distinct from libre's and libbaresip's. */

const char *bp_version(void);

#endif /* BP_SHIM_H */
