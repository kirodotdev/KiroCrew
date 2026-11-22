# SPDX-License-Identifier: Apache-2.0
"""Upgrade notices write, create and unlink through the held member folder, not by name.

``members.member_dir`` only builds a path. A write that goes on to use that path looks
the folder up by name a second time, so a link put at ``members/<slug>`` after the folder
is found sends the write behind it. Each test puts that link right after
``open_member_folder`` has pinned the folder, before the write, unlink or claim, checks the
link was put there, and shows the effect landed in the held folder and the other folder
is left as it was.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import mate_upgrade_notice as notice
from kiro_crew import members as members_mod
from kiro_crew import pinned_fs
from kiro_crew.member_folder import MemberFolder
from kiro_crew.member_folder import open_member_folder as real_open_member_folder

SLUG = members_mod.member_slug("ada")

pytestmark = [
    requires_symlinks,
    pytest.mark.skipif(
        not pinned_fs.supports_pinned_walk(), reason="descriptor open needs a pinned walk"
    ),
]


@pytest.fixture
def arm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[], tuple[Path, list[bool]]]:
    """Return a function that links ``members/<slug>`` to a folder elsewhere, once, and
    returns that folder and a list that holds ``True`` after the link was put there.

    The link goes in right after ``open_member_folder`` has pinned the member's folder,
    before the caller's write, unlink or claim. A caller that looks the folder up by name
    instead (``member_dir``) gets the link right after that call answers. The member's
    real folder is renamed aside, so a held descriptor still names it and a lookup by
    name lands in the other folder. A test arms it after it has put the member's own
    files in place.
    """

    def install() -> tuple[Path, list[bool]]:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        real_member_dir = members_mod.member_dir
        done: list[bool] = []

        def swap() -> None:
            if not done:
                done.append(True)
                folder = members_mod.members_root() / SLUG
                folder.mkdir(parents=True, exist_ok=True)
                folder.rename(folder.with_name(folder.name + ".moved"))
                os.symlink(elsewhere, folder)

        def member_dir(slug: str) -> Path:
            answer = real_member_dir(slug)
            swap()
            return answer

        @contextmanager
        def open_member_folder(slug: str) -> Iterator[MemberFolder]:
            with real_open_member_folder(slug) as held:
                swap()
                yield held

        monkeypatch.setattr(members_mod, "member_dir", member_dir)
        monkeypatch.setattr(notice, "open_member_folder", open_member_folder, raising=False)
        return elsewhere, done

    return install


def _real_files() -> list[str]:
    """What is in the member's real folder, wherever the swap moved it."""
    folder = members_mod.members_root() / SLUG
    moved = folder.with_name(folder.name + ".moved")
    return sorted(p.name for p in (moved if moved.exists() else folder).iterdir())


def test_the_owed_notice_is_written_to_the_held_folder(arm) -> None:
    elsewhere, done = arm()
    notice.mark_notice_owed("ada", "a change")
    assert done == [True]
    assert list(elsewhere.iterdir()) == []
    assert _real_files() == [notice.NOTICE_OWED_FILENAME]


def test_the_kept_notice_is_written_to_the_held_folder(arm) -> None:
    elsewhere, done = arm()
    notice.mark_kept_owed("ada", "why")
    assert done == [True]
    assert list(elsewhere.iterdir()) == []
    assert _real_files() == [notice.KEPT_OWED_FILENAME]


def test_the_policy_notice_is_written_to_the_held_folder(arm) -> None:
    elsewhere, done = arm()
    notice.mark_policy_owed("ada", "restored")
    assert done == [True]
    assert list(elsewhere.iterdir()) == []
    assert _real_files() == [notice.POLICY_OWED_FILENAME]


def test_clearing_the_kept_notice_unlinks_in_the_held_folder(arm) -> None:
    notice.mark_kept_owed("ada", "why")
    elsewhere, done = arm()
    (elsewhere / notice.KEPT_OWED_FILENAME).write_text("not the member's\n", encoding="utf-8")
    notice.clear_kept_owed("ada")
    assert done == [True]
    assert [p.name for p in elsewhere.iterdir()] == [notice.KEPT_OWED_FILENAME]
    assert _real_files() == []


@pytest.mark.parametrize(
    ("mark", "claim", "marker"),
    [
        (
            lambda: notice.mark_notice_owed("ada"),
            notice.claim_notice_prompt,
            notice.NOTICE_CLAIMED_FILENAME,
        ),
        (
            lambda: notice.mark_policy_owed("ada", "restored"),
            notice.claim_policy_notice,
            notice.POLICY_CLAIMED_FILENAME,
        ),
    ],
    ids=["moved", "policy"],
)
def test_the_claim_marker_is_created_in_the_held_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mark, claim, marker: str
) -> None:
    """The link lands as the marker is created: the record was read from the real folder."""
    mark()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real_open = os.open
    done: list[bool] = []

    def spy_open(path, flags, *args, **kwargs):
        if not done and Path(os.fsdecode(path)).name == marker:
            done.append(True)
            folder = members_mod.members_root() / SLUG
            folder.rename(folder.with_name(folder.name + ".moved"))
            os.symlink(elsewhere, folder)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)
    # ``supports_pinned_walk`` asks whether ``os.open`` takes ``dir_fd``; the spy does too.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {spy_open})
    claim(SLUG, "ada")
    assert done == [True]
    assert list(elsewhere.iterdir()) == []
    assert marker in _real_files()
