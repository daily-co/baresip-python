#!/usr/bin/env bash
#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#
# cibuildwheel before-all for manylinux (AlmaLinux 8) containers: install
# the native build dependencies, then build the static libre/libbaresip
# stack the extension links against.
#
# libopus is built from source — static, -fPIC — so the codec is compiled
# into the extension itself. OpenSSL comes from the distro dev package as
# shared libraries, which auditwheel grafts into the wheel. Both are ours
# to re-release when upstream ships a security fix (docs/UPGRADING.md).

set -euo pipefail

OPUS_VERSION=1.5.2
OPUS_SHA256=65c1d2f78b9f2fb20082c38cbe47c951ad5839345876e46941612ee87f9a7ce1

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

dnf -y install openssl-devel cmake

# --libdir puts the static library on el8's default linker search path;
# /usr/local/lib is not on it, and the extension links a plain -lopus.
cd "$(mktemp -d)"
curl -fsSLO "https://downloads.xiph.org/releases/opus/opus-${OPUS_VERSION}.tar.gz"
echo "${OPUS_SHA256}  opus-${OPUS_VERSION}.tar.gz" | sha256sum -c -
tar xzf "opus-${OPUS_VERSION}.tar.gz"
cd "opus-${OPUS_VERSION}"
./configure --prefix=/usr --libdir=/usr/lib64 \
    --disable-shared --enable-static --with-pic \
    --disable-doc --disable-extra-programs
make -j"$(nproc)"
make install

# Any modern interpreter works here (the system python3 is too old); the
# manylinux image guarantees this one exists. The build script's module
# completeness guard runs as part of this — a dependency missing in the
# container fails the build instead of shrinking the wheel.
/opt/python/cp312-cp312/bin/python "${REPO_ROOT}/scripts/build_native.py"
