#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Patches carried on top of the pinned submodules, and how the build applies them.

The build stages a copy of the library with the patches applied and
leaves the submodule checkout alone. These tests run the staging on a
small made-up source tree, then on the real pinned library with the
patches the repository carries.
"""

import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_native import PATCHES_DIR, discover_patches, patch_stamp, stage_patched

PATCH = """\
--- a/src/hello.c
+++ b/src/hello.c
@@ -1,4 +1,4 @@
 int answer(void)
 {
-\treturn 41;
+\treturn 42;
 }
"""


@pytest.fixture
def tree(tmp_path):
    """A source tree with one file, a build directory, and one patch for it."""
    source = tmp_path / "lib"
    (source / "src").mkdir(parents=True)
    (source / "src" / "hello.c").write_text("int answer(void)\n{\n\treturn 41;\n}\n")
    (source / "build").mkdir()
    (source / "build" / "stale.o").write_text("object code")
    patches = tmp_path / "patches" / "lib"
    patches.mkdir(parents=True)
    (patches / "0001-answer.patch").write_text(PATCH)
    return source, patches.parent, tmp_path / "prefix" / "src" / "lib"


def test_patches_are_found_in_name_order(tree):
    _source, patches_dir, _dest = tree
    (patches_dir / "lib" / "0000-first.patch").write_text(PATCH)
    (patches_dir / "lib" / "notes.txt").write_text("not a patch")

    names = [p.name for p in discover_patches("lib", patches_dir)]

    assert names == ["0000-first.patch", "0001-answer.patch"]
    assert discover_patches("other", patches_dir) == []


def test_staging_applies_the_patch_and_leaves_the_source_alone(tree):
    source, patches_dir, dest = tree

    staged = stage_patched(source, discover_patches("lib", patches_dir), dest)

    assert "return 42;" in (staged / "src" / "hello.c").read_text()
    assert "return 41;" in (source / "src" / "hello.c").read_text()
    assert not (staged / "build").exists()  # an in-tree build is not carried over


def test_a_current_staged_copy_is_reused(tree):
    source, patches_dir, dest = tree
    patches = discover_patches("lib", patches_dir)
    stage_patched(source, patches, dest)
    (dest / "built.o").write_text("an incremental build's output")

    stage_patched(source, patches, dest)

    assert (dest / "built.o").exists()


def test_a_changed_patch_or_source_restages(tree):
    source, patches_dir, dest = tree
    patches = discover_patches("lib", patches_dir)
    before = patch_stamp(source, patches)
    stage_patched(source, patches, dest)
    (dest / "built.o").write_text("stale")

    later = time.time() + 5
    os.utime(source / "src" / "hello.c", (later, later))  # what a submodule bump does
    assert patch_stamp(source, patches) != before
    stage_patched(source, patches, dest)

    assert not (dest / "built.o").exists()


def test_a_patch_that_does_not_apply_fails_the_build(tree):
    source, patches_dir, dest = tree
    (source / "src" / "hello.c").write_text("int answer(void)\n{\n\treturn 7;\n}\n")

    with pytest.raises(SystemExit, match=r"0001-answer\.patch does not apply"):
        stage_patched(source, discover_patches("lib", patches_dir), dest)


@pytest.mark.parametrize("library", ["re", "baresip"])
def test_every_carried_patch_applies_to_the_pinned_submodule(library, tmp_path):
    patches = discover_patches(library)
    source = REPO_ROOT / "third_party" / library
    if not patches:
        pytest.skip(f"no patches are carried for {library}")
    if not (source / "CMakeLists.txt").exists():
        pytest.skip(f"the {library} submodule is not checked out")

    stage_patched(source, patches, tmp_path / library)


def test_only_known_libraries_carry_patches():
    carried = (
        {p.name for p in PATCHES_DIR.iterdir() if p.is_dir()} if PATCHES_DIR.exists() else set()
    )

    assert carried <= {"re", "baresip"}
