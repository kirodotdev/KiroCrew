"""Owner-only file helpers open without following links.

A link at the final name raises ``OSError`` (``ELOOP``) and its target is left
untouched, whether the target exists or the link dangles. Where ``os.O_NOFOLLOW``
does not exist (Windows) the flag is 0 and the helpers open exactly as before, so
the module is skipped there; one test runs the helpers with the flag removed.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import owner_only_files as oof
from kiro_crew.atomic_write import append_line
from kiro_crew.config.paths import config_dir

pytestmark = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="os.O_NOFOLLOW is not available here, so the helpers open as before",
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(params=["existing-target", "dangling-link"])
def link(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Path, Path]:
    """A link in the data home, and the file it names outside it."""
    target = tmp_path / "target.txt"
    if request.param == "existing-target":
        target.write_bytes(b"original\n")
    leaf = config_dir() / "linked.txt"
    leaf.symlink_to(target)
    return leaf, target


def _assert_untouched(target: Path, before: str | None) -> None:
    if before is None:
        assert not target.exists()
    else:
        assert _digest(target) == before


def _refused(link: tuple[Path, Path], attempt: Callable[[Path], object]) -> None:
    leaf, target = link
    before = _digest(target) if target.exists() else None
    with pytest.raises(OSError) as raised:
        attempt(leaf)
    assert raised.value.errno == errno.ELOOP
    assert leaf.is_symlink()
    _assert_untouched(target, before)


@requires_symlinks
@pytest.mark.parametrize("mode", ["a", "w", "a+"])
def test_owner_only_opener_refuses_a_link_at_the_leaf(link: tuple[Path, Path], mode: str) -> None:
    def attempt(leaf: Path) -> None:
        with open(leaf, mode, encoding="utf-8", opener=oof.owner_only_opener) as handle:
            handle.write("appended\n")

    _refused(link, attempt)


@requires_symlinks
def test_owner_only_opener_for_refuses_a_link_at_the_leaf(link: tuple[Path, Path]) -> None:
    def attempt(leaf: Path) -> None:
        opener = oof.owner_only_opener_for(leaf)
        assert opener is not None
        with open(leaf, "a", encoding="utf-8", opener=opener) as handle:
            handle.write("appended\n")

    _refused(link, attempt)


@requires_symlinks
def test_append_line_refuses_a_link_at_the_leaf(link: tuple[Path, Path]) -> None:
    _refused(link, lambda leaf: append_line(leaf, "appended"))
    _refused(link, lambda leaf: append_line(leaf, "appended", opener=oof.owner_only_opener))


@requires_symlinks
def test_write_text_owner_only_refuses_a_link_at_the_leaf(link: tuple[Path, Path]) -> None:
    _refused(link, lambda leaf: oof.write_text_owner_only(leaf, "replaced\n"))


def test_the_helpers_open_files_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("old\n", encoding="utf-8")
    with monkeypatch.context() as mp:
        mp.delattr(os, "O_NOFOLLOW")
        mp.setattr(oof, "_O_NOFOLLOW", 0)
        for name, make in {
            "opener": lambda p: open(p, "a", opener=oof.owner_only_opener).close(),
            "opener_for": lambda p: open(p, "a", opener=oof.owner_only_opener_for(p)).close(),
            "write_text": lambda p: oof.write_text_owner_only(p, "new\n"),
            "append_line": lambda p: append_line(p, "new", opener=oof.owner_only_opener),
        }.items():
            created = config_dir() / f"{name}.txt"
            make(created)
            assert stat.S_IMODE(created.stat().st_mode) == 0o600, name
            make(existing)
        append_line(tmp_path / "plain.txt", "new")
        assert (tmp_path / "plain.txt").read_text(encoding="utf-8") == "new\n"
