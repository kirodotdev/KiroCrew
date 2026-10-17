"""The ``--out`` writer's by-name branch and the ``--out`` UNC screen, driven on POSIX.

``destination._write_bytes_nofollow`` has two branches: a descriptor-relative ``O_NOFOLLOW``
open where the platform has ``dir_fd``, and a by-name write judged by shape first where it
does not. The builder refuses at its entry point on a platform without ``dir_fd``, so the
by-name branch is reached only by a caller below that guard -- which is exactly why its
refusals have to be pinned on their own: they are what a direct caller on such a platform
gets. Each case here takes the branch by answering ``_dir_fd_supported`` False through the
facade, which is the one predicate the branch consults.

The screen's own branch -- ``kiro_crew.hooks`` not importable while ``os.name`` says Windows
-- is driven the same way, with the Windows answer patched into each owner.
"""

from __future__ import annotations

import builtins
import os
import pathlib

import pytest

from .test_producer import load_build, patch_builder_global

_posix_only = pytest.mark.skipif(
    os.name != "posix", reason="plants a symlink, which needs POSIX link semantics here"
)


@pytest.fixture
def by_name(monkeypatch: pytest.MonkeyPatch):
    """A builder copy whose ``--out`` writer takes the branch without ``dir_fd``.

    Most of these outcomes are the same on both branches, so the fixture proves which one
    ran: the descriptor branch's first step, pinning the parent, fails the test outright, and
    the writer has to have asked the platform question at least once.
    """
    mod = load_build()
    asked: list[bool] = []

    def _no_dir_fd() -> bool:
        asked.append(True)
        return False

    def _descriptor_branch(*_args, **_kwargs):
        raise AssertionError("the descriptor branch ran; the by-name branch was not taken")

    monkeypatch.setattr(mod, "_dir_fd_supported", _no_dir_fd)
    monkeypatch.setattr(mod, "_open_dir_nofollow_pinned", _descriptor_branch)
    yield mod
    assert asked, "the writer never asked whether dir_fd is supported, so no branch was chosen"


def test_the_by_name_branch_writes_the_bytes_verbatim(by_name, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "report.json"
    assert by_name._write_bytes_nofollow(target, b"line one\r\nline two\n") is True
    assert target.read_bytes() == b"line one\r\nline two\n"


def test_the_by_name_branch_truncates_a_regular_file_in_place(
    by_name, tmp_path: pathlib.Path
) -> None:
    target = tmp_path / "report.json"
    target.write_bytes(b"a much longer previous report\n")
    assert by_name._write_nofollow(target, "new\n") is True
    assert target.read_bytes() == b"new\n"


@_posix_only
def test_the_by_name_branch_refuses_a_planted_link(by_name, tmp_path: pathlib.Path) -> None:
    victim = tmp_path / "precious.txt"
    victim.write_bytes(b"do not truncate me\n")
    link = tmp_path / "bundle.staging.owned"
    link.symlink_to(victim)
    with pytest.raises(by_name.ExportRefused, match="is a symlink"):
        by_name._write_bytes_nofollow(link, b"marker\n")
    assert victim.read_bytes() == b"do not truncate me\n"


def test_the_by_name_branch_refuses_a_directory(by_name, tmp_path: pathlib.Path) -> None:
    occupied = tmp_path / "bundle.smc-bundle.json"
    occupied.mkdir()
    with pytest.raises(by_name.ExportRefused, match="is a directory"):
        by_name._write_bytes_nofollow(occupied, b"{}\n")
    assert occupied.is_dir()


def test_an_exclusive_by_name_write_reports_an_existing_file_as_not_written(
    by_name, tmp_path: pathlib.Path
) -> None:
    plan = tmp_path / "curation-plan.json"
    plan.write_bytes(b'{"signed": true}\n')
    written = by_name._write_bytes_nofollow(plan, b"{}\n", exclusive=True, exists_ok=True)
    assert written is False
    assert plan.read_bytes() == b'{"signed": true}\n'


def test_an_exclusive_by_name_write_refuses_an_existing_file_it_may_not_keep(
    by_name, tmp_path: pathlib.Path
) -> None:
    marker = tmp_path / "bundle.staging.owned"
    marker.write_bytes(b"someone else's file\n")
    with pytest.raises(by_name.ExportRefused, match="already exists"):
        by_name._write_bytes_nofollow(marker, b"marker\n", exclusive=True)
    assert marker.read_bytes() == b"someone else's file\n"


def test_an_exclusive_by_name_write_creates_an_absent_file(by_name, tmp_path: pathlib.Path) -> None:
    marker = tmp_path / "bundle.staging.owned"
    assert by_name._write_bytes_nofollow(marker, b"marker\n", exclusive=True) is True
    assert marker.read_bytes() == b"marker\n"


def test_the_by_name_branch_refuses_a_missing_parent(by_name, tmp_path: pathlib.Path) -> None:
    orphan = tmp_path / "not-there" / "report.json"
    with pytest.raises(by_name.ExportRefused, match="is not there"):
        by_name._write_bytes_nofollow(orphan, b"{}\n")
    assert not orphan.parent.exists()


class _OsThatSaysWindows:
    """``os`` as the builder's owners see it, reporting nt; everything else is genuine."""

    name = "nt"

    def __getattr__(self, attr):
        return getattr(os, attr)


def test_the_out_screen_refuses_when_hooks_is_unimportable_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """No way to ask whether ``--out`` names a share, so it is refused rather than touched."""
    mod = load_build()
    patch_builder_global(monkeypatch, mod, "os", _OsThatSaysWindows())
    real_import = builtins.__import__

    def _no_hooks(name, *args, **kwargs):
        if name == "kiro_crew.hooks":
            raise ImportError("no module named 'kiro_crew.hooks' (simulated standalone venv)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_hooks)
    with pytest.raises(mod.ExportRefused, match="kiro_crew.hooks is not importable"):
        mod._refuse_unc_out(tmp_path / "bundle")


def test_the_out_screen_passes_a_local_path_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    mod = load_build()
    patch_builder_global(monkeypatch, mod, "os", _OsThatSaysWindows())
    assert mod._refuse_unc_out(tmp_path / "bundle") is None
