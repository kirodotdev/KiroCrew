"""A macOS home folder whose letter case differs from the account record.

On case-insensitive APFS ``HOME=/Users/ApplE`` can name the on-disk folder
``/Users/apple``: ``resolve()`` keeps the caller's spelling while ``F_GETPATH``
reports the on-disk one. The cron script vet, the crons-root containment check
and the V2 memory reader each compared the two spellings and refused the file.
These tests simulate that kernel spelling on any host, keeping real descriptors
and inodes, and pin both halves: the case alias is admitted on macOS only after
the no-follow walk proves it, and a different file is still refused.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import cron_script, cron_script_paths, hooks, mcp_cron_vet, memory_files, pinned_fs
from kiro_crew.memory_files import LocalMemoryFiles
from kiro_crew.platform.interfaces import MemoryRoots

pytestmark = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="the case-alias proof walks a pinned parent"
)


def _kernel_spelling(real_witness):
    def spelled(fd):
        path = real_witness(fd)
        return path.replace("casehome", "CaseHome") if path else None

    return spelled


def _as_platform(monkeypatch, platform: str) -> None:
    monkeypatch.setattr(hooks, "sys", SimpleNamespace(**{**vars(hooks.sys), "platform": platform}))


@pytest.fixture
def script(tmp_path, monkeypatch):
    crons = tmp_path.resolve() / "casehome" / "crons"
    crons.mkdir(parents=True)
    path = crons / "x.py"
    path.write_text("def run(ctx):\n    ctx.notify('hi')\n", encoding="utf-8")
    monkeypatch.setattr(mcp_cron_vet, "fd_real_path", _kernel_spelling(mcp_cron_vet.fd_real_path))
    return path


def test_vet_admits_a_case_only_kernel_spelling_on_macos(script, monkeypatch):
    _as_platform(monkeypatch, "darwin")
    assert mcp_cron_vet._vet_script_file(str(script)) is None


def test_vet_still_refuses_a_case_only_kernel_spelling_off_macos(script, monkeypatch):
    _as_platform(monkeypatch, "linux")
    err = mcp_cron_vet._vet_script_file(str(script))
    assert err is not None and "cannot verify cron script path" in err


def test_vet_refuses_a_case_alias_the_walk_cannot_prove(script, monkeypatch):
    _as_platform(monkeypatch, "darwin")
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    err = mcp_cron_vet._vet_script_file(str(script))
    assert err is not None and "cannot verify cron script path" in err


@pytest.fixture
def case_home(tmp_path, monkeypatch):
    """Data home spelled ``CaseHome`` while the folder on disk is ``casehome``."""
    on_disk = tmp_path.resolve() / "casehome" / "crew"
    (on_disk / "crons").mkdir(parents=True)
    (on_disk / "crons" / "x.py").write_text("def run(ctx):\n    pass\n", encoding="utf-8")
    spelled = tmp_path.resolve() / "CaseHome" / "crew"
    monkeypatch.setattr(cron_script, "config_dir", lambda: spelled)
    return on_disk, spelled


def test_containment_admits_the_on_disk_spelling_of_the_crons_root(case_home, monkeypatch):
    on_disk, spelled = case_home

    def case_insensitive_samefile(a, b):
        fold = lambda p: str(p).replace("CaseHome", "casehome")  # noqa: E731
        return os.path.samefile(fold(a), fold(b))

    monkeypatch.setattr(cron_script_paths, "_samefile", case_insensitive_samefile)
    file_path, func = cron_script_paths.resolve_script_path(f"{on_disk / 'crons' / 'x.py'}:run")
    assert (file_path, func) == (str(spelled / "crons" / "x.py"), "run")


def test_containment_refuses_a_distinct_directory_of_another_case(case_home, monkeypatch):
    on_disk, _ = case_home
    # A case-sensitive volume: the two spellings are two directories.
    monkeypatch.setattr(cron_script_paths, "_samefile", lambda a, b: False)
    with pytest.raises(PermissionError, match="Script must be under"):
        cron_script_paths.resolve_script_path(f"{on_disk / 'crons' / 'x.py'}:run")


def _member_store(tmp_path: Path) -> LocalMemoryFiles:
    workspace = tmp_path.resolve() / "casehome" / "memory_stores" / "member-a"
    memory_dir = workspace / "memory"
    (memory_dir / "history").mkdir(parents=True)
    (memory_dir / "preferences.md").write_text("Keep drafts short.", encoding="utf-8")
    return LocalMemoryFiles(
        MemoryRoots(
            workspace=workspace,
            memory_dir=memory_dir,
            history_dir=memory_dir / "history",
            store_name="member-a",
            memory_version=2,
        )
    )


@pytest.fixture
def member_store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_files, "fd_real_path", _kernel_spelling(memory_files.fd_real_path))
    return _member_store(tmp_path)


def test_member_memory_reads_a_case_only_kernel_spelling_on_macos(member_store, monkeypatch):
    _as_platform(monkeypatch, "darwin")
    anchor = member_store._roots.memory_dir / "preferences.md"
    assert member_store._read_entry_bytes(anchor) == b"Keep drafts short."


def test_member_memory_still_refuses_a_case_only_kernel_spelling_off_macos(
    member_store, monkeypatch
):
    _as_platform(monkeypatch, "linux")
    anchor = member_store._roots.memory_dir / "preferences.md"
    with pytest.raises(OSError, match="outside its expected bound path"):
        member_store._read_entry_bytes(anchor)
