/*
 * Copyright (c) 2026, Daily
 *
 * SPDX-License-Identifier: BSD-2-Clause
 */

#include <re.h>
#include <baresip.h>

#include "shim.h"

const char *bp_version(void)
{
	return sys_libre_version_get();
}
