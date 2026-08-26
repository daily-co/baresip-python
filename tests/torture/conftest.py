#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Torture fixtures; the harness itself lives in torture_helpers.py."""

import os
import random

import pytest


@pytest.fixture
def rng():
    """The run's seeded RNG; the seed is printed for reproduction."""
    seed = os.environ.get("TORTURE_SEED")
    seed = int(seed) if seed else random.SystemRandom().randrange(2**32)
    print(f"\nTORTURE_SEED={seed}", flush=True)
    return random.Random(seed)
