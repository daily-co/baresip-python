#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Wheel-build entry point.

All project metadata lives in pyproject.toml; this file exists only because
``cffi_modules`` is a setup() keyword (registered by cffi through the
setuptools plugin mechanism) with no pyproject equivalent.

The extension is declared only when BARESIP_BUILD_EXT=1. Wheel builds set it
(see [tool.cibuildwheel] in pyproject.toml) after their before-all step has
produced the static libraries. Development installs leave it unset — a fresh
clone must `uv sync` before `make native` has ever run — and compile the
extension in place with `make ext` instead.
"""

import os

from setuptools import setup

kwargs = {}
if os.environ.get("BARESIP_BUILD_EXT") == "1":
    kwargs["cffi_modules"] = ["src/_native/build_ffi.py:ffi"]

setup(**kwargs)
