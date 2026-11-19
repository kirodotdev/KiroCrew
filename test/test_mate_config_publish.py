"""``mate_config.write_mate_config(only_if_absent=True)``: the no-replace publish.

The staged file is moved onto the name with ``os.rename`` on Windows (which refuses an
existing destination there), else with an atomic no-replace rename where the platform
has one, with a hard link where it does not, and with an exclusive create of the name
on a filesystem without hard links; every way, an entry already at the path is never
replaced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_config, platform_compat


@pytest.fixture
def kiro(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "kiro"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("KIRO_HOME", str(home))
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: home)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: home / "agents")
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return home


def _no_rename(*_a: object, **_k: object) -> None:
    raise NotImplementedError("atomic no-replace rename is unavailable")


@pytest.mark.parametrize("rename_available", [True, False])
def test_absent_file_is_written_by_either_publish(
    kiro: Path, monkeypatch: pytest.MonkeyPatch, rename_available: bool
) -> None:
    if not rename_available:
        monkeypatch.setattr(platform_compat, "rename_noreplace", _no_rename)
    path = mate_config.write_mate_config("Ana", only_if_absent=True)
    assert path == mate_config.mate_config_path("Ana")
    assert json.loads(path.read_text(encoding="utf-8")) == mate_config.minimal_mate_spec("Ana")
    leftovers = [p.name for p in path.parent.iterdir() if p.name != path.name]
    assert all(not name.startswith("Ana.json.") for name in leftovers), leftovers


@pytest.mark.parametrize("rename_available", [True, False])
def test_an_entry_that_appears_before_the_publish_is_kept(
    kiro: Path, monkeypatch: pytest.MonkeyPatch, rename_available: bool
) -> None:
    target = mate_config.mate_config_path("Ana")
    real_stage = mate_config._stage_spec

    def stage_then_race(path: Path, text: str) -> str:
        staged = real_stage(path, text)
        target.write_bytes(b'{"owner": "someone else"}')
        return staged

    monkeypatch.setattr(mate_config, "_stage_spec", stage_then_race)
    if not rename_available:
        monkeypatch.setattr(platform_compat, "rename_noreplace", _no_rename)
    assert mate_config.write_mate_config("Ana", only_if_absent=True) is None
    assert target.read_bytes() == b'{"owner": "someone else"}'


def _windows_rename(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Windows' publish on this host: ``os.rename`` with its refusal of an existing name."""
    import os

    calls: list[tuple[str, str]] = []
    real_rename = os.rename

    def rename(src: object, dst: object, *a: object, **k: object) -> None:
        calls.append((str(src), str(dst)))
        if os.path.lexists(str(dst)):
            raise FileExistsError(17, "Cannot create a file when that file already exists")
        real_rename(src, dst, *a, **k)

    def no_link(*_a: object, **_k: object) -> None:
        raise AssertionError("the Windows publish never hard-links")

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "link", no_link)
    monkeypatch.setattr(platform_compat, "rename_noreplace", _no_rename)
    return calls


def test_on_windows_the_publish_is_a_rename(kiro: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _windows_rename(monkeypatch)

    path = mate_config.write_mate_config("Ana", only_if_absent=True)

    assert path == mate_config.mate_config_path("Ana")
    assert [dst for _src, dst in calls] == [str(path)]
    assert json.loads(path.read_text(encoding="utf-8")) == mate_config.minimal_mate_spec("Ana")
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_on_windows_an_entry_that_appears_before_the_publish_is_kept(
    kiro: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = mate_config.mate_config_path("Ana")
    real_stage = mate_config._stage_spec

    def stage_then_race(path: Path, text: str) -> str:
        staged = real_stage(path, text)
        target.write_bytes(b'{"owner": "someone else"}')
        return staged

    monkeypatch.setattr(mate_config, "_stage_spec", stage_then_race)
    calls = _windows_rename(monkeypatch)

    assert mate_config.write_mate_config("Ana", only_if_absent=True) is None
    assert [dst for _src, dst in calls] == [str(target)]
    assert target.read_bytes() == b'{"owner": "someone else"}'
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def _no_hard_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """A filesystem with neither a no-replace rename nor hard links."""
    import errno
    import os

    def no_link(*_a: object, **_k: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(platform_compat, "rename_noreplace", _no_rename)
    monkeypatch.setattr(os, "link", no_link)


def test_without_hard_links_the_publish_is_an_exclusive_create(
    kiro: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_hard_links(monkeypatch)

    path = mate_config.write_mate_config("Ana", only_if_absent=True)

    assert path == mate_config.mate_config_path("Ana")
    assert json.loads(path.read_text(encoding="utf-8")) == mate_config.minimal_mate_spec("Ana")
    assert not path.is_symlink()
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_without_hard_links_an_entry_that_appears_before_the_publish_is_kept(
    kiro: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = mate_config.mate_config_path("Ana")
    real_stage = mate_config._stage_spec

    def stage_then_race(path: Path, text: str) -> str:
        staged = real_stage(path, text)
        target.write_bytes(b'{"owner": "someone else"}')
        return staged

    monkeypatch.setattr(mate_config, "_stage_spec", stage_then_race)
    _no_hard_links(monkeypatch)

    assert mate_config.write_mate_config("Ana", only_if_absent=True) is None
    assert target.read_bytes() == b'{"owner": "someone else"}'
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def test_without_hard_links_a_link_planted_at_the_name_is_never_followed(
    kiro: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = mate_config.mate_config_path("Ana")
    outside = tmp_path / "outside.json"
    real_stage = mate_config._stage_spec

    def stage_then_link(path: Path, text: str) -> str:
        staged = real_stage(path, text)
        target.symlink_to(outside)
        return staged

    monkeypatch.setattr(mate_config, "_stage_spec", stage_then_link)
    _no_hard_links(monkeypatch)

    assert mate_config.write_mate_config("Ana", only_if_absent=True) is None
    assert target.is_symlink() and not outside.exists()


def test_a_link_error_other_than_no_hard_links_is_not_turned_into_a_create(
    kiro: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import os

    def full_disk(*_a: object, **_k: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(platform_compat, "rename_noreplace", _no_rename)
    monkeypatch.setattr(os, "link", full_disk)

    with pytest.raises(mate_config.MateConfigError):
        mate_config.write_mate_config("Ana", only_if_absent=True)
    assert not os.path.lexists(mate_config.mate_config_path("Ana"))


def test_the_exclusive_create_is_the_shared_pinned_one(
    kiro: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import pinned_fs

    calls: list[tuple[str, int, bool]] = []
    real = pinned_fs.write_new_file_pinned

    def spy(dir_fd: int, name: str, data: bytes, *, mode: int, fsync: bool = False) -> None:
        calls.append((name, mode, fsync))
        real(dir_fd, name, data, mode=mode, fsync=fsync)

    monkeypatch.setattr(pinned_fs, "write_new_file_pinned", spy)
    _no_hard_links(monkeypatch)

    path = mate_config.write_mate_config("Ana", only_if_absent=True)

    assert calls == [(path.name, 0o600, True)]
    assert json.loads(path.read_text(encoding="utf-8")) == mate_config.minimal_mate_spec("Ana")


def test_an_exclusive_create_that_fails_mid_write_leaves_no_file(
    kiro: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import os

    real_write = os.write
    target = mate_config.mate_config_path("Ana")

    def full_disk(fd: int, data: bytes) -> int:
        if os.fstat(fd).st_ino == _inode(target):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    _no_hard_links(monkeypatch)
    monkeypatch.setattr(os, "write", full_disk)

    with pytest.raises(mate_config.MateConfigError):
        mate_config.write_mate_config("Ana", only_if_absent=True)
    assert not os.path.lexists(target)


def _inode(path: Path) -> int:
    import os

    try:
        return os.lstat(path).st_ino
    except OSError:
        return -1


def test_the_owner_record_is_read_pinned_and_bounded(
    kiro: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A40: the owner record is read through pinned_fs.read_file_pinned, so a link at
    # its name is never followed and an oversized one never reads as a record.
    assert mate_config._read_owners() == {}
    mate_config.write_mate_config("Ana", only_if_absent=True)
    owners = mate_config._owners_path()
    assert mate_config._read_owners().get("Ana")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"Ana": "/somewhere/else"}), encoding="utf-8")
    owners.unlink()
    try:
        owners.symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    with pytest.raises(mate_config.MateConfigError):
        mate_config._read_owners()
    owners.unlink()
    monkeypatch.setattr(mate_config, "MAX_OWNERS_BYTES", 8)
    owners.write_text(json.dumps({"Ana": "/a/long/enough/home"}), encoding="utf-8")
    with pytest.raises(mate_config.MateConfigError):
        mate_config._read_owners()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"Ana": "/home/\xff/crew"}',
        # An encoded surrogate: json.loads of bytes would take it, strict UTF-8 does not.
        b'{"Ana": "/home/\xed\xa0\x80/crew"}',
        '{"Ana": "/srv/a/crew"}'.encode("utf-16"),
    ],
)
def test_an_owner_record_that_is_not_utf8_is_unreadable(kiro: Path, raw: bytes) -> None:
    # A44: the record is decoded as strict UTF-8 before it is parsed, as it was
    # before the pinned read: an invalid byte inside a string is an unreadable record.
    mate_config.write_mate_config("Ana", only_if_absent=True)
    mate_config._owners_path().write_bytes(raw)
    with pytest.raises(mate_config.MateConfigError):
        mate_config._read_owners()
