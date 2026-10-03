"""``kiro_crew.platform_compat`` stays the compatibility layer's import and patch surface.

The cross-process file locks and the owner-only access helpers are defined in
``platform_lock_compat`` and ``platform_owner_compat``. Every name that moved stays
readable under its old path and is FORWARDED, so a patch through the facade lands on the
owner, where the owner's own callers read it. The owners read the platform flags, the
lock modules, the clock, ``ctypes`` and the Win32 struct layouts from the facade when they
run, so a test that rebinds one of those there still reaches them. The checks shared with
the sandbox facade, and the one census of facade patches both run on, live in
``test_sandbox_refactor_facade``.
"""

from __future__ import annotations

import os
import sys
import zlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import test_sandbox_refactor_facade as shared

from kiro_crew import platform_compat, platform_lock_compat, platform_owner_compat

# The patch census parses every test file once; keep it on the sandbox file's worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_sandbox_refactor_facade")

_OWNERS: dict[str, ModuleType] = {
    m.__name__: m for m in (platform_lock_compat, platform_owner_compat)
}

#: Every module-level name the split moved out of platform_compat.py, by its owner, plus
#: ``tempfile``: only the persistence probe reads it now, and a test reaches the stdlib
#: module through ``platform_compat.tempfile``, so the binding moved with its reader.
_MOVED: dict[str, tuple[str, ...]] = {
    "kiro_crew.platform_lock_compat": (
        "_LOCK_POLL_SECS",
        "_LOCK_POLL_MAX_SECS",
        "_LOCK_TIMEOUT_SECS",
        "_WIN_LOCK_POLL_SECS",
        "_WIN_LOCK_TIMEOUT_SECS",
        "_on_event_loop",
        "_lock_timeout_message",
        "_posix_acquire_blocking",
        "_win_acquire_blocking",
        "file_lock",
        "flock_exclusive",
        "open_lock_file",
        "open_create_or_existing",
        "acquire_lock",
        "release_lock",
        "try_acquire_lock",
        "probe_file_persistence",
        "tempfile",
    ),
    "kiro_crew.platform_owner_compat": (
        "_OWNER_RIGHTS_SID",
        "_TOKEN_QUERY",
        "_TOKEN_USER_CLASS",
        "_process_token_sid",
        "_process_token_sid_unguarded",
        "process_owner_sid",
        "_TOKEN_SID_CACHE",
        "current_user_sid",
        "make_owner_only_dir",
        "local_user_id",
        "stat_owned_by_current_user",
        "stat_writable_by_current_user",
        "path_writable_by_current_user",
        "restrict_to_owner",
        "restrict_dir_to_owner",
        "_apply_owner_only_dacl",
    ),
}

_LOCK = "kiro_crew.platform_lock_compat"
_OWNER = "kiro_crew.platform_owner_compat"
_PC = "kiro_crew.platform_compat"

#: Every function in an owner that imports from the facade, and what it reads there.
#: The lock modules are imported at the point where their branch first uses them,
#: because the facade binds only the one this platform has.
_SEAM_IMPORTS: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    (_LOCK, "_posix_acquire_blocking"): (_PC, ("fcntl", "time")),
    (_LOCK, "_win_acquire_blocking"): (_PC, ("time",)),
    (_LOCK, "_win_acquire_blocking._try_once"): (_PC, ("msvcrt",)),
    (_LOCK, "acquire_lock"): (_PC, ("IS_POSIX", "fcntl", "time")),
    (_LOCK, "file_lock"): (_PC, ("IS_POSIX", "fcntl", "msvcrt", "time")),
    (_LOCK, "release_lock"): (_PC, ("IS_POSIX", "fcntl", "msvcrt")),
    (_LOCK, "try_acquire_lock"): (_PC, ("IS_POSIX", "fcntl", "msvcrt")),
    (_OWNER, "_process_token_sid"): (_PC, ("IS_POSIX",)),
    (_OWNER, "_process_token_sid_unguarded"): (
        _PC,
        ("_PROCESS_QUERY_LIMITED_INFORMATION", "_TokenUser", "ctypes"),
    ),
    (_OWNER, "local_user_id"): (_PC, ("IS_POSIX",)),
    (_OWNER, "path_writable_by_current_user"): (_PC, ("IS_POSIX", "_ACCESS_HONOURS_EFFECTIVE_IDS")),
    (_OWNER, "process_owner_sid"): (_PC, ("IS_POSIX",)),
    (_OWNER, "restrict_dir_to_owner"): (_PC, ("IS_POSIX",)),
    (_OWNER, "restrict_to_owner"): (_PC, ("IS_POSIX",)),
    (_OWNER, "stat_owned_by_current_user"): (_PC, ("IS_POSIX",)),
    (_OWNER, "stat_writable_by_current_user"): (_PC, ("IS_POSIX",)),
}

#: The lock module the facade binds only on the platform that has it.
_PLATFORM_BOUND = frozenset({"msvcrt"} if platform_compat.IS_POSIX else {"fcntl"})


@pytest.mark.parametrize(("owner", "name"), shared._moved_rows(_MOVED))
def test_every_moved_name_is_defined_by_its_owner_and_read_through_the_facade(
    owner: str, name: str
) -> None:
    shared.check_moved_name(platform_compat, _OWNERS, owner, name)


def test_every_moved_name_is_forwarded_and_none_is_bound_here() -> None:
    shared.check_forwarded_and_bound_are_disjoint(platform_compat, _MOVED, frozenset())


def test_the_forwarding_table_names_each_owner_by_its_dotted_name() -> None:
    shared.check_forwarding_table(platform_compat, _OWNERS)


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (platform_lock_compat, "file_lock"),
        (platform_lock_compat, "_LOCK_TIMEOUT_SECS"),
        (platform_owner_compat, "_TOKEN_SID_CACHE"),
        (platform_owner_compat, "restrict_to_owner"),
    ],
)
def test_a_patch_through_the_facade_round_trips_on_the_owner(owner: ModuleType, name: str) -> None:
    shared.check_round_trips(platform_compat, owner, name)


def test_a_write_of_a_facade_name_stays_on_the_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name the facade binds itself -- a platform flag, a module -- is an ordinary
    attribute write, which the owners then read at call time."""
    monkeypatch.setattr(platform_compat, "IS_POSIX", "stub")
    assert vars(platform_compat)["IS_POSIX"] == "stub"
    assert "IS_POSIX" not in vars(platform_lock_compat)
    assert "IS_POSIX" not in vars(platform_owner_compat)


def test_a_loaded_owner_is_read_without_the_import_system() -> None:
    shared.check_loaded_owner_needs_no_import(
        platform_compat, platform_owner_compat, "current_user_sid"
    )


def test_a_purged_owner_is_imported_again_and_read_fresh() -> None:
    shared.check_a_purged_owner_is_imported_again(platform_compat, "_lock_timeout_message")


def test_every_owner_is_loaded_with_the_facade() -> None:
    shared.check_owners_load_with_the_facade(platform_compat)


def test_an_unknown_name_is_an_attribute_error() -> None:
    shared.check_unknown_name(platform_compat)


def test_the_star_import_binds_the_moved_public_names(tmp_path: Path) -> None:
    shared.check_star_import(
        tmp_path,
        platform_compat,
        frozenset({"TYPE_CHECKING", "platform_lock_compat", "platform_owner_compat"}),
    )
    bound = shared.star_import(tmp_path, platform_compat)
    for name in ("file_lock", "try_acquire_lock", "restrict_to_owner", "make_owner_only_dir"):
        assert bound[name] is getattr(platform_compat, name)


def test_the_static_shape_of_the_facade() -> None:
    shared.check_static_shape(platform_compat, _OWNERS)


def test_each_owner_reads_the_facade_only_for_the_listed_seams() -> None:
    assert shared.owner_seams(_OWNERS, platform_compat) == _SEAM_IMPORTS


def test_each_seam_is_a_binding_of_the_facade() -> None:
    shared.check_seams_are_facade_bindings(platform_compat, _MOVED, _SEAM_IMPORTS, _PLATFORM_BOUND)


def test_the_owners_depend_on_nothing_above_them() -> None:
    shared.check_owner_dependency_direction(_OWNERS, platform_compat)


def test_the_owners_log_under_the_compatibility_layer_s_name() -> None:
    shared.check_owner_logger(platform_compat, _OWNERS)


#: Names a test patches through ``kiro_crew.platform_compat`` that the facade AND an owner
#: both bind, each importing its own, so the patch reaches only the facade's code. ``Path``
#: is rebound for the process-identity and trusted-binary helpers, which stay here; no test
#: fakes it for a lock or an owner-only helper. A new entry is a decision that the patch
#: needs no forwarding.
_PC_SHARED_BINDINGS_PATCHED: frozenset[str] = frozenset({"Path"})


def test_a_patched_name_bound_by_the_facade_and_an_owner_is_a_listed_one() -> None:
    shared.check_shared_bindings(platform_compat, _SEAM_IMPORTS, _PC_SHARED_BINDINGS_PATCHED)


# --------------------------------------------------------------------------- #
# A name rebound on the facade reaches the owner that reads it at call time.
# --------------------------------------------------------------------------- #


class _ReachedModule:
    """Stands in for a module: reading any attribute of it raises ``_Reached`` naming the
    facade binding the helper read."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, attr: str) -> Any:
        raise shared._Reached(f"{self._label}.{attr}")


def test_a_rebound_lock_module_reaches_the_posix_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "fcntl", _ReachedModule("fcntl"), raising=False)
    with platform_compat.open_lock_file(tmp_path / "lock") as fd:
        with pytest.raises(shared._Reached, match="fcntl"):
            with platform_compat.file_lock(fd):
                pass


def test_a_rebound_platform_flag_and_lock_module_reach_the_windows_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows branch runs from a POSIX host: the flag and ``msvcrt`` are both read
    from the facade, which is what a stubbed ``msvcrt`` has always relied on."""
    taken: list[tuple[int, int]] = []
    fake = SimpleNamespace(
        LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, _n: taken.append((fd, mode))
    )
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "msvcrt", fake, raising=False)
    with platform_compat.open_lock_file(tmp_path / "lock") as fd:
        with platform_compat.file_lock(fd):
            pass
    assert [mode for _fd, mode in taken] == [2, 0]


def test_a_rebound_clock_reaches_the_windows_spin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_compat, "time", _ReachedModule("time"))
    fake = SimpleNamespace(LK_NBLCK=2, locking=lambda *_a: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(platform_compat, "msvcrt", fake, raising=False)
    with platform_compat.open_lock_file(tmp_path / "lock") as fd:
        with pytest.raises(shared._Reached, match="time.monotonic"):
            platform_compat._win_acquire_blocking(fd, timeout=1.0)


def test_a_rebound_clock_reaches_the_lock_s_wait_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal reports the time the acquire really waited, measured on the clock
    the facade binds. A stand-in ``fcntl`` keeps the POSIX branch runnable on any host
    and takes no real lock."""
    fake = SimpleNamespace(LOCK_EX=2, LOCK_SH=1, LOCK_NB=4, LOCK_UN=8, flock=lambda *_a: None)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "fcntl", fake, raising=False)
    monkeypatch.setattr(platform_compat, "time", _ReachedModule("time"))
    with platform_compat.open_lock_file(tmp_path / "lock") as fd:
        with pytest.raises(shared._Reached, match="time.monotonic"):
            with platform_compat.file_lock(fd):
                pass


def test_a_patched_ceiling_reaches_the_lock_s_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_LOCK_TIMEOUT_SECS`` is the owner's: a patch through the facade is the ceiling
    the refusal message names."""
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "_win_acquire_blocking", lambda _fd, **_kw: False)
    monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", 1234.0)
    with platform_compat.open_lock_file(tmp_path / "lock") as fd:
        with pytest.raises(OSError, match="1234"):
            with platform_compat.file_lock(fd):
                pass


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="test/conftest.py stubs restrict_to_owner on Windows outside the exempt modules",
)
def test_a_rebound_platform_flag_reaches_restrict_to_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows branch of the owner-only lockdown, and the DACL writer it calls in
    its own module, each reached through a patch on the facade."""
    target = tmp_path / "secret"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "_apply_owner_only_dacl", shared._raiser("dacl"))
    with pytest.raises(shared._Reached, match="dacl"):
        platform_compat.restrict_to_owner(target)


def test_a_patched_directory_lockdown_reaches_make_owner_only_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conftest's Windows stub relies on this: ``make_owner_only_dir`` calls
    ``restrict_dir_to_owner`` in its own module, where a patch through the facade lands."""
    reached: list[str] = []
    monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", lambda p: reached.append(str(p)))
    platform_compat.make_owner_only_dir(tmp_path / "owned")
    assert reached == [str(tmp_path / "owned")]


def test_a_rebound_ctypes_reaches_the_token_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "ctypes", _ReachedModule("ctypes"))
    with pytest.raises(shared._Reached, match="ctypes"):
        platform_compat._process_token_sid_unguarded()


def test_a_rebound_platform_flag_reaches_local_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off POSIX the id is derived from the token's SID, read through the facade too."""
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    assert platform_compat.local_user_id() == zlib.crc32(b"S-1-5-21-1-2-3-1001")


@pytest.mark.parametrize("honoured", [False, True])
def test_a_patched_effective_ids_flag_reaches_the_writability_check(
    honoured: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_ACCESS_HONOURS_EFFECTIVE_IDS`` stays on the facade, shared with the trusted-binary
    walk; the owner's writability check reads it there at call time, and reads the
    patched mode-bit check in its own module."""
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    asked: list[object] = []

    def spy(path: object, mode: int, **kwargs: object) -> bool:
        asked.append(kwargs.get("effective_ids"))
        return True

    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "stat_writable_by_current_user", lambda _st: False)
    monkeypatch.setattr(os, "access", spy)
    monkeypatch.setattr(platform_compat, "_ACCESS_HONOURS_EFFECTIVE_IDS", honoured)
    assert platform_compat.path_writable_by_current_user(target) is honoured
    assert asked == ([True] if honoured else [])
