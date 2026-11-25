# SPDX-License-Identifier: Apache-2.0
"""A link at ``members`` into another folder of the data home is refused.

The slug folders are made inside the folder ``members`` names. When that is the trust
folder, the SEL folder or any other folder the data home keeps, they would sit beside the
stores kept there. A folder outside the data home (the normal relocation) stays allowed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import members, pinned_fs
from kiro_crew.member_folder import open_member_folder

SLUG = "ada"

needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="descriptor open needs a pinned walk"
)


def _open() -> Exception | None:
    try:
        with open_member_folder(members.member_slug(SLUG)):
            pass
    except members.MemberSlugError as exc:
        return exc
    return None


def _link_members_to(target: Path) -> Path:
    named = members.members_root()
    named.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, named)
    return named


@requires_symlinks
@needs_pinned_walk
@pytest.mark.parametrize("inside", ["trust", "sel", "a/b/c"])
def test_a_link_to_a_folder_inside_the_data_home_is_refused(inside: str) -> None:
    base = members.members_root().parent
    target = base / inside
    target.mkdir(parents=True)
    _link_members_to(target)
    result = _open()
    assert isinstance(result, members.MemberSlugError), result
    assert not (target / members.member_slug(SLUG)).exists()


@requires_symlinks
@needs_pinned_walk
def test_a_link_to_a_link_that_ends_inside_the_data_home_is_refused(tmp_path: Path) -> None:
    base = members.members_root().parent
    target = base / "trust"
    target.mkdir(parents=True)
    hop = tmp_path / "hop"
    os.symlink(target, hop)
    _link_members_to(hop)
    result = _open()
    assert isinstance(result, members.MemberSlugError), result
    assert not (target / members.member_slug(SLUG)).exists()


@requires_symlinks
@needs_pinned_walk
def test_a_link_to_a_folder_outside_the_data_home_is_allowed(tmp_path: Path) -> None:
    outside = tmp_path / "outside-members"
    outside.mkdir()
    _link_members_to(outside)
    result = _open()
    assert result is None, repr(result.__cause__)
    assert (outside / members.member_slug(SLUG)).is_dir()
