# SPDX-License-Identifier: Apache-2.0
"""Member record files are written without following links.

A member's folder is ``members/<slug>`` under the data home. The writers of a file
there (the greeting marker and the welcome and create-request records) must refuse a
link at ``members/<slug>`` or at the file itself, leave what the link points at byte
for byte as it was, and create nothing behind it. The ``members`` folder is resolved
once, as the member dashboard readers do, so a link at ``members`` leads to the
folder it points to, the write is held to that folder, and the mode of that folder is
left as it is. A link at ``members`` that leads to no folder that can be opened (a loop,
a link to nowhere, a file, a folder it may not open, a target that is not text), or to the
data home or a folder above it, is refused and creates nothing. Whatever goes wrong before
the folder is handed out (the data home, a link, a level that cannot be opened or written,
a folder removed meanwhile) ends in that refusal, with a message that names no path and no
descriptor left open. A plain folder keeps working as before.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from conftest import requires_symlinks
from kiro_crew import atomic_write, members, pinned_fs
from kiro_crew.dashboard import mate_welcome
from kiro_crew.testing.links import make_dir_link

SLUG = "ada"


@dataclass(frozen=True)
class Site:
    """One writer: how to call it and the file it touches."""

    name: str
    call: Callable[[], Any]
    leaf: str


def _sites() -> list[Site]:
    slug = members.member_slug(SLUG)
    return [
        Site(
            "greeting_marker",
            lambda: mate_welcome.claim_greeting_marker(slug, "k"),
            mate_welcome.GREETING_MARKER_FILENAME,
        ),
        Site(
            "member_record",
            lambda: members._write_member_record(
                SLUG, None, members.WELCOME_OWED_FILENAME, {"member": SLUG}
            ),
            members.WELCOME_OWED_FILENAME,
        ),
    ]


SITES = _sites()
IDS = [s.name for s in SITES]

needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="a swap after the open only matters where the folder is held by descriptor",
)
needs_posix_modes = pytest.mark.skipif(
    sys.platform == "win32", reason="owner-only modes are a POSIX notion"
)


def _run(site: Site) -> Any:
    """Call the writer; a refusal of any kind is the result, not a failure of the test."""
    try:
        return site.call()
    except Exception as exc:  # noqa: BLE001 - the outcome is read off the disk
        return exc


def _snapshot(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = path.read_bytes()
    return out


def _members() -> Path:
    return members.members_root()


def _empty_folder(tmp_path: Path) -> Path:
    """A folder with no member folder in it, for a link at ``members`` to point to."""
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    return outside


def _peer() -> Path:
    """Another member's real folder, inside the members root."""
    peer = _members() / "bob"
    peer.mkdir(parents=True)
    return peer


@pytest.fixture
def no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform that cannot open relative to a directory descriptor."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_plain_member_folder_is_written_as_before(site: Site) -> None:
    result = _run(site)
    assert not isinstance(result, Exception), result
    assert (_members() / members.member_slug(SLUG) / site.leaf).is_file()


def _assert_written_where_members_points(site: Site, outside: Path) -> None:
    """The write landed in the folder the link at ``members`` points to.

    The folder the write made and the file are owner-only; the folder the link names
    keeps the mode it had.
    """
    assert pinned_fs.is_reparse_point(_members()), "the link at members was replaced"
    folder = outside / members.member_slug(SLUG)
    assert (folder / site.leaf).is_file()
    if sys.platform != "win32":
        assert stat.S_IMODE(folder.stat().st_mode) == stat.S_IRWXU
        assert stat.S_IMODE((folder / site.leaf).stat().st_mode) == 0o600
        assert stat.S_IMODE(outside.stat().st_mode) == 0o755


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_members_leads_to_the_folder_it_points_to(site: Site, tmp_path: Path) -> None:
    outside = _empty_folder(tmp_path)
    make_dir_link(_members(), outside)
    result = _run(site)
    assert not isinstance(result, Exception), result
    _assert_written_where_members_points(site, outside)


@requires_symlinks
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_junction_at_members_leads_to_the_folder_it_points_to(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows junction is a reparse point that ``Path.is_symlink`` does not report."""
    outside = _empty_folder(tmp_path)
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, named)
    real_is_symlink = Path.is_symlink
    real_is_junction = Path.is_junction
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: False if self == named else real_is_symlink(self)
    )
    monkeypatch.setattr(
        Path, "is_junction", lambda self: True if self == named else real_is_junction(self)
    )
    result = _run(site)
    assert not isinstance(result, Exception), result
    assert (outside / members.member_slug(SLUG) / site.leaf).is_file()


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_the_member_folder_is_refused(site: Site) -> None:
    peer = _peer()
    before = _snapshot(peer)
    make_dir_link(_members() / SLUG, peer)
    assert isinstance(_run(site), members.MemberSlugError)
    assert _snapshot(peer) == before, f"{site.name} changed what the link points at"


@requires_symlinks
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_the_file_is_refused(site: Site, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    os.symlink(target, folder / site.leaf)
    result = _run(site)
    assert target.read_bytes() == b"untouched"
    assert (folder / site.leaf).is_symlink(), "the link itself was replaced"
    assert isinstance(result, members.MemberSlugError), result
    assert sorted(p.name for p in folder.iterdir()) == [site.leaf], "something was created"


@requires_symlinks
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_the_file_is_refused_without_descriptor_pinning(
    site: Site, tmp_path: Path, no_pin: None
) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    os.symlink(target, folder / site.leaf)
    _run(site)
    assert target.read_bytes() == b"untouched"
    assert (folder / site.leaf).is_symlink(), "the link itself was replaced"


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_members_leads_to_the_folder_it_points_to_without_descriptor_pinning(
    site: Site, tmp_path: Path, no_pin: None
) -> None:
    outside = _empty_folder(tmp_path)
    make_dir_link(_members(), outside)
    result = _run(site)
    assert not isinstance(result, Exception), result
    _assert_written_where_members_points(site, outside)


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_the_member_folder_is_refused_without_descriptor_pinning(
    site: Site, no_pin: None
) -> None:
    peer = _peer()
    before = _snapshot(peer)
    make_dir_link(_members() / SLUG, peer)
    assert isinstance(_run(site), members.MemberSlugError)
    assert _snapshot(peer) == before, f"{site.name} changed what the link points at"


@needs_posix_modes
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_folders_made_by_a_writer_are_owner_only(site: Site) -> None:
    old = os.umask(0o022)
    try:
        assert not isinstance(_run(site), Exception)
    finally:
        os.umask(old)
    folder = _members() / members.member_slug(SLUG)
    for path in (_members(), folder):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
    assert stat.S_IMODE((folder / site.leaf).stat().st_mode) == 0o600


@needs_posix_modes
@pytest.mark.parametrize(("site", "expected"), [(SITES[0], 0o755), (SITES[1], 0o700)], ids=IDS)
def test_an_existing_folder_keeps_or_gains_the_mode_its_writer_gives_it(
    site: Site, expected: int
) -> None:
    """A claim leaves an existing folder as it is; a record writer tightens it, as before."""
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    for path in (_members(), folder):
        path.chmod(0o755)
    assert not isinstance(_run(site), Exception)
    for path in (_members(), folder):
        assert stat.S_IMODE(path.stat().st_mode) == expected, path


@needs_posix_modes
@pytest.mark.parametrize(
    "site",
    [s for s in SITES if s.name != "greeting_marker"],
    ids=[s.name for s in SITES if s.name != "greeting_marker"],
)
def test_a_record_written_without_descriptor_pinning_is_owner_only(
    site: Site, no_pin: None
) -> None:
    assert not isinstance(_run(site), Exception)
    leaf = _members() / members.member_slug(SLUG) / site.leaf
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o600


def test_the_welcome_record_behind_a_link_at_members_is_written_and_read_there(
    tmp_path: Path,
) -> None:
    outside = _empty_folder(tmp_path)
    make_dir_link(_members(), outside)
    assert members.mark_welcome_owed(SLUG) is True
    slug = members.member_slug(SLUG)
    assert (outside / slug / members.WELCOME_OWED_FILENAME).is_file()
    assert members.welcome_owed(slug, SLUG) is True
    if sys.platform != "win32":
        assert stat.S_IMODE(outside.stat().st_mode) == 0o755


@needs_pinned_walk
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_folder_swapped_for_a_link_after_it_is_open_is_not_written_through(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    peer = _peer()
    before = _snapshot(peer)

    def swap() -> None:
        folder.rename(_members() / "moved-away")
        make_dir_link(folder, peer)

    fired: list[bool] = []
    real = {name: getattr(os, name) for name in ("open", "replace", "rename", "unlink")}

    def wrap(name: str) -> Callable[..., Any]:
        def inner(*args: Any, **kwargs: Any) -> Any:
            names = [str(a) for a in args[:2] if isinstance(a, (str, os.PathLike))]
            if not fired and any(site.leaf in n for n in names):
                fired.append(True)
                swap()
            return real[name](*args, **kwargs)

        return inner

    with monkeypatch.context() as mp:
        wrapped = {name: wrap(name) for name in real}
        # The wrappers must still count as descriptor-relative, or the code under test
        # sees a platform that cannot pin and takes the by-name route.
        mp.setattr(
            os,
            "supports_dir_fd",
            {
                wrapped.get(fn.__name__, fn) if fn in real.values() else fn
                for fn in os.supports_dir_fd
            },
        )
        for name, fn in wrapped.items():
            mp.setattr(os, name, fn)
        _run(site)
    assert _snapshot(peer) == before, f"{site.name} changed what the link points at"


@needs_posix_modes
@needs_pinned_walk
def test_a_folder_swapped_for_a_link_after_it_is_open_is_not_tightened_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    peer = _peer()
    peer.chmod(0o755)
    fired: list[bool] = []

    def swap_first(real: Callable[..., Any]) -> Callable[..., Any]:
        def inner(*args: Any, **kwargs: Any) -> Any:
            if not fired:
                fired.append(True)
                folder.rename(_members() / "moved-away")
                make_dir_link(folder, peer)
            return real(*args, **kwargs)

        return inner

    with monkeypatch.context() as mp:
        for name in ("chmod", "fchmod"):
            mp.setattr(os, name, swap_first(getattr(os, name)))
        _run(SITES[1])
    assert fired, "the writer never tightened a folder"
    assert stat.S_IMODE(peer.stat().st_mode) == 0o755


@requires_symlinks
@needs_pinned_walk
@needs_posix_modes
def test_the_members_folder_stays_held_after_the_walk() -> None:
    from kiro_crew import member_folder

    root = _members()
    victim = root.parent / "victim"
    victim.mkdir()
    victim.chmod(0o755)
    with member_folder.open_member_folder(SLUG) as folder:
        assert folder.parent_fd is not None
        # The members folder is swapped for a link after the walk has passed it.
        root.rename(root.parent / "moved-away")
        make_dir_link(root, victim)
        os.fchmod(folder.parent_fd, stat.S_IRWXU)
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755
    assert stat.S_IMODE((root.parent / "moved-away").stat().st_mode) == 0o700


@requires_symlinks
@needs_pinned_walk
@needs_posix_modes
def test_the_record_writer_tightens_members_through_the_held_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import member_folder

    root = _members()
    victim = root.parent / "victim"
    victim.mkdir()
    victim.chmod(0o755)
    real_open = member_folder.open_member_folder

    @contextmanager
    def open_then_swap(slug: str) -> Iterator[Any]:
        with real_open(slug) as folder:
            # The members folder is swapped for a link after the walk has passed it.
            root.rename(root.parent / "moved-away")
            make_dir_link(root, victim)
            yield folder

    monkeypatch.setattr(member_folder, "open_member_folder", open_then_swap)
    assert members.mark_welcome_owed(SLUG)
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755
    moved = root.parent / "moved-away"
    assert stat.S_IMODE(moved.stat().st_mode) == 0o700
    assert (moved / members.member_slug(SLUG) / members.WELCOME_OWED_FILENAME).is_file()


@requires_symlinks
@needs_pinned_walk
@needs_posix_modes
def test_members_swapped_for_a_link_between_the_two_levels_is_not_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _members()
    victim = root.parent / "victim"
    victim.mkdir()
    victim.chmod(0o755)
    slug = members.member_slug(SLUG)
    real_mkdir = os.mkdir
    fired: list[bool] = []

    def swap_when_the_slug_level_is_made(path: Any, *args: Any, **kwargs: Any) -> Any:
        if not fired and Path(str(path)).name == slug:
            fired.append(True)
            root.rename(root.parent / "moved-away")
            make_dir_link(root, victim)
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", swap_when_the_slug_level_is_made)
    assert members.mark_welcome_owed(SLUG) is True
    assert fired, "the slug level was never made"
    assert list(victim.iterdir()) == []
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755
    moved = root.parent / "moved-away"
    assert (moved / slug / members.WELCOME_OWED_FILENAME).is_file()


@requires_symlinks
def test_the_folder_a_link_at_members_names_swapped_for_a_link_after_it_is_resolved_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)  # the by-name arm resolves the link
    from kiro_crew import member_folder

    outside = _empty_folder(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    before = _snapshot(victim)
    make_dir_link(_members(), outside)
    real_walk = member_folder.open_pinned_descendant_dir
    calls: list[Path] = []

    def swap_the_resolved_folder_then_walk(root: Any, parts: Any, **kwargs: Any) -> Any:
        calls.append(Path(root))
        if len(calls) == 2:
            outside.rename(tmp_path / "moved-away")
            make_dir_link(outside, victim)
        return real_walk(root, parts, **kwargs)

    monkeypatch.setattr(
        member_folder, "open_pinned_descendant_dir", swap_the_resolved_folder_then_walk
    )
    assert members.mark_welcome_owed(SLUG) is False
    assert len(calls) == 2, "the folder the link names was never opened"
    assert _snapshot(victim) == before


@requires_symlinks
def test_a_link_at_members_pointed_elsewhere_after_it_is_resolved_still_writes_to_the_resolved_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)  # the by-name arm resolves the link
    resolved = _empty_folder(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    make_dir_link(_members(), resolved)
    named = _members()
    real_resolve = type(named).resolve
    fired: list[bool] = []

    def point_the_link_elsewhere_once_resolved(self: Path, *args: Any, **kwargs: Any) -> Path:
        result = real_resolve(self, *args, **kwargs)
        if self == named and not fired:
            fired.append(True)
            named.unlink()
            make_dir_link(named, elsewhere)
        return result

    monkeypatch.setattr(type(named), "resolve", point_the_link_elsewhere_once_resolved)
    assert members.mark_welcome_owed(SLUG) is True
    assert fired, "the link at members was never resolved"
    slug = members.member_slug(SLUG)
    assert (resolved / slug / members.WELCOME_OWED_FILENAME).is_file()
    assert list(elsewhere.iterdir()) == []
    if sys.platform != "win32":
        assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755


@requires_symlinks
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
def test_a_dangling_link_at_members_is_refused_and_creates_nothing(
    pinned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    nowhere = tmp_path / "nowhere"
    os.symlink(nowhere, _members())
    assert members.mark_welcome_owed(SLUG) is False
    assert not nowhere.exists()
    assert _members().is_symlink()


def _file(tmp_path: Path) -> Path:
    path = tmp_path / "plain.txt"
    path.write_bytes(b"a file")
    return path


def _fifo(tmp_path: Path) -> Path:
    path = tmp_path / "fifo"
    os.mkfifo(path)
    return path


def _socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind("sock")
    return tmp_path / "sock"


def _link_at_members(target: str | Path) -> None:
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, named)


def _unsearchable(tmp_path: Path) -> Path:
    locked = tmp_path / "locked"
    (locked / "target").mkdir(parents=True)
    locked.chmod(0)
    return locked / "target"


def _unreadable(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0)
    return target


def _a_chain_of_links(tmp_path: Path) -> str:
    """More than 40 links in a row that end in a real folder: no loop, and the system
    still refuses to follow them."""
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    (named.parent / "real").mkdir()
    count = 45
    for index in range(1, count):
        os.symlink(f"hop{index + 1}", named.parent / f"hop{index}")
    os.symlink("real", named.parent / f"hop{count}")
    return "hop1"


def _skip_without_bytes_names() -> None:
    probe = os.fsencode(_members().parent) + b"/probe-\xff"
    try:
        os.mkdir(probe)
    except OSError:
        pytest.skip("this file system refuses names that are not text")
    os.rmdir(probe)


def _data_home_elsewhere(monkeypatch: pytest.MonkeyPatch, members_path: Path) -> None:
    """The data home is reached by *members_path*; nothing else changes."""
    monkeypatch.setattr(members, "members_root", lambda: members_path)


def _unresolvable(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make ``members`` something that does not lead to an existing folder.

    Returns the folder whose content a refused call must leave as it was.
    """
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    parent = named.parent
    watched = parent
    if kind == "data_home_through_a_loop":
        os.symlink("loop", tmp_path / "loop")
        _data_home_elsewhere(monkeypatch, tmp_path / "loop" / "data" / "members")
        return tmp_path
    if kind == "data_home_parent_cannot_be_searched":
        (tmp_path / "noperm").mkdir()
        (tmp_path / "noperm").chmod(0)
        _data_home_elsewhere(monkeypatch, tmp_path / "noperm" / "data" / "members")
        return tmp_path
    if kind == "data_home_relative_and_cwd_removed":
        gone = tmp_path / "gone"
        gone.mkdir()
        monkeypatch.chdir(gone)
        gone.rmdir()
        _data_home_elsewhere(monkeypatch, Path("relative-data") / "members")
        return tmp_path
    if kind == "itself":
        os.symlink(named.name, named)
    elif kind == "two_links":
        os.symlink("hop-a", named)
        os.symlink(named.name, parent / "hop-a")
    elif kind == "three_links":
        os.symlink("hop-a", named)
        os.symlink("hop-b", parent / "hop-a")
        os.symlink(named.name, parent / "hop-b")
    elif kind == "loop_that_misses_members":
        os.symlink("hop-a", named)
        os.symlink("hop-b", parent / "hop-a")
        os.symlink("hop-a", parent / "hop-b")
    elif kind == "dangling":
        os.symlink(tmp_path / "nowhere", named)
    elif kind == "two_missing_levels":
        os.symlink(tmp_path / "missing" / "again", named)
    elif kind == "regular_file":
        named.write_bytes(b"a file")
    elif kind == "link_to_a_file":
        os.symlink(_file(tmp_path), named)
    elif kind == "file_in_the_middle":
        os.symlink(_file(tmp_path) / "below", named)
    elif kind == "link_to_a_fifo":
        os.symlink(_fifo(tmp_path), named)
    elif kind == "link_to_a_socket":
        os.symlink(_socket(tmp_path, monkeypatch), named)
    elif kind == "link_to_dev_null":
        os.symlink("/dev/null", named)
    elif kind == "name_too_long":
        os.symlink("x" * 300, named)
    elif kind == "data_home_itself":
        os.symlink(".", named)
    elif kind == "data_home_absolute":
        os.symlink(parent, named)
    elif kind == "parent_of_the_data_home":
        os.symlink("..", named)
    elif kind == "parent_absolute":
        os.symlink(parent.parent, named)
    elif kind == "grandparent_of_the_data_home":
        os.symlink("../..", named)
    elif kind == "invalid_text_dangling":
        _skip_without_bytes_names()
        os.symlink(os.fsencode(tmp_path) + b"/nowhere-\xff", os.fsencode(named))
    elif kind == "invalid_text_folder":
        _skip_without_bytes_names()
        folder = os.fsencode(tmp_path) + b"/folder-\xff"
        os.mkdir(folder)
        os.symlink(folder, os.fsencode(named))
    elif kind == "parent_cannot_be_searched":
        os.symlink(_unsearchable(tmp_path), named)
    elif kind == "folder_cannot_be_opened":
        os.symlink(_unreadable(tmp_path), named)
    elif kind == "plain_members_not_writable":
        named.mkdir()
        named.chmod(0o555)
    elif kind == "link_to_a_folder_not_writable":
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o555)
        os.symlink(readonly, named)
    elif kind == "link_to_a_root_owned_folder":
        os.symlink("/usr/share", named)
    elif kind == "link_to_proc":
        os.symlink("/proc", named)
    elif kind == "link_to_dev":
        os.symlink("/dev", named)
    elif kind == "data_home_not_writable_and_members_missing":
        parent.chmod(0o555)
    elif kind in (
        "mkdir_fails_read_only_file_system",
        "mkdir_fails_no_space",
        "mkdir_fails_over_quota",
    ):
        code = {
            "mkdir_fails_read_only_file_system": errno.EROFS,
            "mkdir_fails_no_space": errno.ENOSPC,
            "mkdir_fails_over_quota": errno.EDQUOT,
        }[kind]

        def mkdir_fails(path: Any, *args: Any, **kwargs: Any) -> None:
            raise OSError(code, os.strerror(code), str(path))

        monkeypatch.setattr(os, "mkdir", mkdir_fails)
    elif kind == "cannot_tell_whether_members_is_a_link":
        named.write_bytes(b"a file")
        real_is_symlink = Path.is_symlink

        def is_symlink(self: Path) -> bool:
            if self == named and sys._getframe(1).f_code.co_filename.endswith("member_folder.py"):
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(self))
            return real_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", is_symlink)
    elif kind == "named_folder_removed_before_the_slug_mkdir":
        slug = members.member_slug(SLUG)
        if hasattr(os, "O_NOFOLLOW"):
            # With descriptors the slug level is made relative to the held folder.
            real_mkdir = os.mkdir

            def remove_the_held_folder_then_mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
                if path == slug and kwargs.get("dir_fd") is not None:
                    named.rmdir()
                real_mkdir(path, *args, **kwargs)

            monkeypatch.setattr(os, "mkdir", remove_the_held_folder_then_mkdir)
        else:
            # Without them the slug level is made by name, after the level above was seen.
            real_path_mkdir = Path.mkdir

            def remove_the_folder_then_make_the_slug(self: Path, *args: Any, **kwargs: Any) -> None:
                if self.name == slug and self.parent.name == named.name:
                    named.rmdir()
                real_path_mkdir(self, *args, **kwargs)

            monkeypatch.setattr(Path, "mkdir", remove_the_folder_then_make_the_slug)
    else:
        raise AssertionError(kind)
    return watched


UNRESOLVABLE = [
    "itself",
    "two_links",
    "three_links",
    "loop_that_misses_members",
    "dangling",
    "two_missing_levels",
    "regular_file",
    "link_to_a_file",
    "file_in_the_middle",
    "link_to_a_fifo",
    "link_to_a_socket",
    "link_to_dev_null",
    "name_too_long",
    "data_home_itself",
    "data_home_absolute",
    "parent_of_the_data_home",
    "parent_absolute",
    "grandparent_of_the_data_home",
    "invalid_text_dangling",
    "invalid_text_folder",
    "parent_cannot_be_searched",
    "folder_cannot_be_opened",
    "plain_members_not_writable",
    "link_to_a_folder_not_writable",
    "link_to_a_root_owned_folder",
    "link_to_proc",
    "link_to_dev",
    "data_home_not_writable_and_members_missing",
    "mkdir_fails_read_only_file_system",
    "mkdir_fails_no_space",
    "mkdir_fails_over_quota",
    "cannot_tell_whether_members_is_a_link",
    "named_folder_removed_before_the_slug_mkdir",
    "data_home_through_a_loop",
    "data_home_parent_cannot_be_searched",
    "data_home_relative_and_cwd_removed",
]


@contextmanager
def _descriptors_left_open(monkeypatch: pytest.MonkeyPatch) -> Iterator[set[int]]:
    """The descriptors ``os.open`` hands this thread inside the block that ``os.close`` has not closed.

    The worker's other threads open and close descriptors of their own, so
    only this thread's ``os.open`` calls are counted. A descriptor handed to
    ``os.fdopen`` is closed by its file object, not ``os.close``; every call
    checked here refuses or raises before any writer reaches that step.
    A descriptor made another way (``os.dup``, ``os.pipe``) is not seen; the
    member-folder walk makes every descriptor it holds with ``os.open``.
    """
    outstanding: set[int] = set()
    caller = threading.get_ident()
    real_open, real_close = os.open, os.close

    def spy_open(*args: Any, **kwargs: Any) -> int:
        fd = real_open(*args, **kwargs)
        if threading.get_ident() == caller:
            outstanding.add(fd)
        return fd

    def spy_close(fd: int) -> None:
        try:
            real_close(fd)
        finally:
            outstanding.discard(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", spy_open)
        patch.setattr(os, "close", spy_close)
        # The code asks ``os.open in os.supports_dir_fd`` before it walks by
        # descriptor; the spies stand in for the real functions there too.
        spies = {real_open: spy_open, real_close: spy_close}
        for name in ("supports_dir_fd", "supports_fd", "supports_follow_symlinks"):
            patch.setattr(os, name, {spies.get(f, f) for f in getattr(os, name)})
        yield outstanding


NEEDS_A_USER_WHO_CAN_BE_REFUSED = (
    "parent_cannot_be_searched",
    "folder_cannot_be_opened",
    "plain_members_not_writable",
    "link_to_a_folder_not_writable",
    "link_to_a_root_owned_folder",
    "link_to_proc",
    "link_to_dev",
    "data_home_parent_cannot_be_searched",
    "data_home_not_writable_and_members_missing",
)


WINDOWS_CANNOT_MAKE = (
    "invalid_text_dangling",
    "invalid_text_folder",
    "data_home_relative_and_cwd_removed",
)


def _skip_what_this_platform_cannot_make(kind: str, pinned: bool = True) -> None:
    if sys.platform == "win32" and kind in WINDOWS_CANNOT_MAKE:
        pytest.skip("Windows has no names that are not text, and cannot remove the working folder")
    for system_folder, name in (
        ("/usr/share", "link_to_a_root_owned_folder"),
        ("/proc", "link_to_proc"),
        ("/dev", "link_to_dev"),
    ):
        if kind == name and (not Path(system_folder).is_dir() or os.access(system_folder, os.W_OK)):
            pytest.skip(f"{system_folder} is missing here or can be written by this user")
    if kind == "link_to_a_fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("no named pipes here")
    if kind == "link_to_a_socket" and not hasattr(socket, "AF_UNIX"):
        pytest.skip("no unix sockets here")
    if kind == "link_to_dev_null" and not Path("/dev/null").exists():
        pytest.skip("no /dev/null here")
    if kind in NEEDS_A_USER_WHO_CAN_BE_REFUSED and (
        not hasattr(os, "geteuid") or os.geteuid() == 0
    ):
        pytest.skip("root searches every folder")


@pytest.fixture
def restore_modes(tmp_path: Path) -> Iterator[None]:
    plain_members = members.members_root()
    yield
    for path in (
        tmp_path / "locked",
        tmp_path / "target",
        tmp_path / "readonly",
        tmp_path / "noperm",
        plain_members,
        plain_members.parent,
    ):
        with suppress(OSError):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@requires_symlinks
@pytest.mark.parametrize("kind", UNRESOLVABLE)
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_members_that_leads_to_no_existing_folder_is_refused_and_creates_nothing(
    site: Site,
    pinned: bool,
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_modes: None,
) -> None:
    _skip_what_this_platform_cannot_make(kind, pinned)
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    watched = _unresolvable(kind, tmp_path, monkeypatch)
    before = _snapshot(watched)
    modes = {
        p: stat.S_IMODE(p.lstat().st_mode)
        for p in (tmp_path / "locked", tmp_path / "target", tmp_path / "readonly")
        if p.exists()
    }
    with _descriptors_left_open(monkeypatch) as left_open:
        result = _run(site)
    assert isinstance(result, members.MemberSlugError), result
    assert left_open == set()
    assert _snapshot(watched) == before
    assert not (tmp_path / "missing").exists()
    assert not (tmp_path / "nowhere").exists()
    slug = members.member_slug(SLUG)
    for above in (watched, watched.parent, watched.parent.parent):
        assert not (above / slug).exists()
    assert modes == {p: stat.S_IMODE(p.lstat().st_mode) for p in modes}


@requires_symlinks
@pytest.mark.parametrize("kind", UNRESOLVABLE)
def test_the_callers_report_a_members_that_leads_nowhere_and_go_on(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    restore_modes: None,
) -> None:
    _skip_what_this_platform_cannot_make(kind)
    watched = _unresolvable(kind, tmp_path, monkeypatch)
    before = _snapshot(watched)
    assert members.mark_welcome_owed(SLUG) is False
    assert "Could not record the first welcome" in caplog.text
    members.mark_create_request(SLUG, "req-1")
    assert _snapshot(watched) == before


@requires_symlinks
def test_a_loop_reported_as_an_oserror_is_refused_like_one_reported_as_a_runtimeerror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``Path.resolve`` reports a loop as ``OSError`` on some Pythons and ``RuntimeError`` on others."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)  # the by-name arm resolves the link
    target = tmp_path / "elsewhere"
    target.mkdir()
    _link_at_members(target)
    real_resolve = Path.resolve
    named = _members()

    def resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self == named:
            raise OSError(errno.ELOOP, "Too many levels of symbolic links", str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    for site in SITES:
        assert isinstance(_run(site), members.MemberSlugError)
    assert not (target / members.member_slug(SLUG)).exists()


@requires_symlinks
def test_a_link_removed_after_it_was_seen_and_before_it_is_resolved_is_refused_or_a_plain_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)  # the by-name arm resolves the link
    target = _members().parent / "other-folder"
    target.mkdir(parents=True)
    _link_at_members("other-folder")
    real_resolve = Path.resolve
    named = _members()

    def resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self == named and named.is_symlink():
            named.unlink()
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    result = _run(SITES[1])
    assert isinstance(result, members.MemberSlugError), result
    assert not named.exists() and not named.is_symlink()
    assert list(target.iterdir()) == []


@requires_symlinks
def test_a_link_replaced_by_a_folder_after_it_was_seen_is_written_as_a_plain_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)  # the by-name arm resolves the link
    target = _members().parent / "other-folder"
    target.mkdir(parents=True)
    _link_at_members("other-folder")
    real_resolve = Path.resolve
    named = _members()

    def resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self == named and named.is_symlink():
            named.unlink()
            named.mkdir()
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    result = _run(SITES[1])
    assert not isinstance(result, Exception) or isinstance(result, members.MemberSlugError), result
    assert list(target.iterdir()) == []


@requires_symlinks
def test_a_link_swapped_for_a_loop_after_it_is_resolved_still_writes_to_the_resolved_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)  # the by-name arm resolves the link
    target = tmp_path / "elsewhere"
    target.mkdir()
    _link_at_members(target)
    real_resolve = Path.resolve
    named = _members()

    def resolve_then_swap(self: Path, *args: Any, **kwargs: Any) -> Path:
        out = real_resolve(self, *args, **kwargs)
        if self == named and named.is_symlink() and os.readlink(named) != named.name:
            named.unlink()
            os.symlink(named.name, named)
        return out

    monkeypatch.setattr(Path, "resolve", resolve_then_swap)
    assert not isinstance(_run(SITES[1]), Exception)
    assert (target / members.member_slug(SLUG) / SITES[1].leaf).is_file()


@requires_symlinks
@pytest.mark.parametrize("kind", UNRESOLVABLE)
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
def test_the_helper_raises_nothing_but_the_refusal_and_its_message_names_no_path(
    kind: str,
    pinned: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_modes: None,
) -> None:
    _skip_what_this_platform_cannot_make(kind, pinned)
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    from kiro_crew import member_folder

    _unresolvable(kind, tmp_path, monkeypatch)
    with _descriptors_left_open(monkeypatch) as left_open:
        with pytest.raises(members.MemberSlugError) as refused:
            with member_folder.open_member_folder(members.member_slug(SLUG)):
                pytest.fail("the helper opened a folder behind a link that leads nowhere")
    message = str(refused.value)
    assert str(tmp_path) not in message
    assert "hop-" not in message and "folder-" not in message and "nowhere" not in message
    assert left_open == set()


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
@pytest.mark.parametrize("where", ["members_root", "mkdir"])
def test_the_refusal_keeps_the_original_exception_as_its_cause(
    where: str, pinned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slip in the code that the refusal covers still shows in the traceback."""
    from kiro_crew import member_folder

    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    _members().parent.mkdir(parents=True, exist_ok=True)
    original: Exception
    if where == "members_root":
        original = TypeError("a slip in the code")

        def members_root() -> Path:
            raise original

        monkeypatch.setattr(members, "members_root", members_root)
    else:
        original = OSError(errno.EROFS, "a read-only file system")

        def mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
            raise original

        monkeypatch.setattr(os, "mkdir", mkdir)
    with pytest.raises(members.MemberSlugError) as refused:
        with member_folder.open_member_folder(members.member_slug(SLUG)):
            pytest.fail("the helper opened a folder it could not make")
    assert refused.value.__cause__ is original


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
@pytest.mark.parametrize(
    "raised", [KeyboardInterrupt(), SystemExit(3)], ids=["KeyboardInterrupt", "SystemExit"]
)
def test_an_interrupt_before_the_folder_is_handed_out_is_not_turned_into_the_refusal(
    raised: BaseException, pinned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import member_folder

    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    _members().parent.mkdir(parents=True, exist_ok=True)

    def members_root() -> Path:
        raise raised

    monkeypatch.setattr(members, "members_root", members_root)
    with _descriptors_left_open(monkeypatch) as left_open:
        with pytest.raises(type(raised)) as at_the_helper:
            with member_folder.open_member_folder(members.member_slug(SLUG)):
                pytest.fail("the helper opened a folder it could not make")
        assert at_the_helper.value is raised
        with pytest.raises(type(raised)) as at_a_writer:
            SITES[0].call()
        assert at_a_writer.value is raised
    assert left_open == set()


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
@pytest.mark.parametrize(
    "raised",
    [ValueError("body"), OSError(errno.EIO, "body"), RuntimeError("body")],
    ids=["ValueError", "OSError", "RuntimeError"],
)
def test_what_the_callers_body_raises_inside_the_with_reaches_the_caller_unchanged(
    raised: Exception, pinned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import member_folder

    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with _descriptors_left_open(monkeypatch) as left_open:
        with pytest.raises(type(raised)) as caught:
            with member_folder.open_member_folder(members.member_slug(SLUG)):
                raise raised
    assert caught.value is raised
    assert left_open == set()


@requires_symlinks
@needs_pinned_walk
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_the_slug_folder_swapped_for_a_loop_before_it_is_opened_is_refused(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = os.mkdir
    slug = members.member_slug(SLUG)

    def mkdir_then_swap(path: Any, *args: Any, **kwargs: Any) -> None:
        real_mkdir(path, *args, **kwargs)
        dir_fd = kwargs.get("dir_fd")
        if path == slug and dir_fd is not None:
            os.rmdir(path, dir_fd=dir_fd)
            os.symlink(path, path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", mkdir_then_swap)
    result = _run(site)
    assert isinstance(result, members.MemberSlugError), result
    assert (_members() / slug).is_symlink()


@requires_symlinks
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
@pytest.mark.parametrize(
    "how",
    [
        "relative",
        "trailing_slash",
        "inside_the_data_home",
        "sticky_and_world_writable",
    ],
)
def test_a_link_to_a_folder_that_exists_is_followed_once(
    how: str, pinned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    data_home = _members().parent
    data_home.mkdir(parents=True, exist_ok=True)
    mode = 0o755
    if how == "inside_the_data_home":
        target = data_home / "other-folder"
        spelling = "other-folder"
    else:
        target = tmp_path / "elsewhere"
        spelling = os.path.relpath(target, data_home) + ("/" if how == "trailing_slash" else "")
        if how == "sticky_and_world_writable":
            mode = 0o1777
    target.mkdir(exist_ok=True)
    target.chmod(mode)
    _link_at_members(spelling)
    for site in SITES:
        assert not isinstance(_run(site), Exception)
    folder = target / members.member_slug(SLUG)
    assert (folder / SITES[1].leaf).is_file()
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == mode
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        assert stat.S_IMODE((folder / SITES[1].leaf).stat().st_mode) == 0o600


@requires_symlinks
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
def test_a_chain_of_more_than_forty_links_is_refused_where_the_folder_is_held_by_descriptor(
    pinned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Was ``forty_five_links`` in the followed-once test: the kernel stops a descriptor open at
    40 links, so the pinned arm refuses; the by-name arm (``Path.resolve``) still follows."""
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    target = _members().parent / "real"
    _link_at_members(_a_chain_of_links(tmp_path))
    for site in SITES:
        outcome = _run(site)
        if pinned and pinned_fs.supports_pinned_walk():
            assert isinstance(outcome, members.MemberSlugError), outcome
        else:
            assert not isinstance(outcome, Exception), outcome
    held = pinned and pinned_fs.supports_pinned_walk()
    assert (target / members.member_slug(SLUG)).exists() is (not held)


@requires_symlinks
def test_a_link_at_the_greeting_marker_is_refused_not_read_as_already_claimed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    os.symlink(target, folder / SITES[0].leaf)
    with pytest.raises(members.MemberSlugError):
        SITES[0].call()
    assert target.read_bytes() == b"untouched"


@requires_symlinks
def test_a_link_at_the_record_file_is_refused_and_the_callers_report_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    os.symlink(target, folder / members.WELCOME_OWED_FILENAME)
    with pytest.raises(members.MemberSlugError):
        SITES[1].call()
    assert members.mark_welcome_owed(SLUG) is False
    assert "Could not record the first welcome" in caplog.text
    assert target.read_bytes() == b"untouched"


@needs_posix_modes
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
def test_a_record_that_exists_with_wider_modes_is_rewritten_owner_only(
    pinned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        # Without the data home's owner-only default, the record's own request decides.
        monkeypatch.setattr(atomic_write, "is_owner_only_target", lambda path: False)
    elif not pinned_fs.supports_pinned_walk():
        pytest.skip("descriptor pinning is not available here")
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    leaf = folder / SITES[1].leaf
    leaf.write_text("{}\n")
    leaf.chmod(0o644)
    assert not isinstance(_run(SITES[1]), Exception)
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o600


@needs_posix_modes
def test_an_existing_folder_is_tightened_by_name_without_descriptor_pinning(no_pin: None) -> None:
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    assert not isinstance(_run(SITES[1]), Exception)
    assert stat.S_IMODE(folder.stat().st_mode) == stat.S_IRWXU


@requires_symlinks
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
def test_a_link_appearing_at_the_record_file_after_the_check_is_not_written_through(
    pinned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.member_folder import MemberFolder

    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    planted: list[bool] = []
    real_refuse_link = MemberFolder.refuse_link

    def plant_a_link_after_the_check(self: MemberFolder, name: str) -> None:
        real_refuse_link(self, name)
        if not planted:
            planted.append(True)
            os.symlink(target, folder / name)

    monkeypatch.setattr(MemberFolder, "refuse_link", plant_a_link_after_the_check)
    assert not isinstance(_run(SITES[1]), Exception)
    assert planted, "the writer never checked the file"
    assert target.read_bytes() == b"untouched", "the record was written through the link"
    record = folder / SITES[1].leaf
    assert record.is_file() and not record.is_symlink(), "the link was not replaced"
    assert json.loads(record.read_text(encoding="utf-8")) == {"member": SLUG}


@requires_symlinks
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
def test_a_link_appearing_at_the_greeting_marker_just_before_it_is_claimed_is_refused(
    pinned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"untouched")
    marker = folder / mate_welcome.GREETING_MARKER_FILENAME
    planted: list[bool] = []
    real_open = os.open

    def plant_a_link_then_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if not planted and os.path.basename(os.fspath(path)) == marker.name:
            planted.append(True)
            os.symlink(target, marker)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", plant_a_link_then_open)
    result = _run(SITES[0])
    assert planted, "the marker was never opened"
    assert isinstance(result, members.MemberSlugError), result
    assert target.read_bytes() == b"untouched", "the marker was written through the link"
    assert marker.is_symlink()


@needs_pinned_walk
def test_a_folder_swapped_for_a_link_before_the_write_creates_anything_is_not_written_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.member_folder import MemberFolder

    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    peer = _peer()
    before = _snapshot(peer)
    moved = _members() / "moved-away"
    fired: list[bool] = []
    real_refuse_link = MemberFolder.refuse_link

    def swap_after_the_link_check(self: MemberFolder, name: str) -> None:
        real_refuse_link(self, name)
        if not fired:
            fired.append(True)
            folder.rename(moved)
            make_dir_link(folder, peer)

    monkeypatch.setattr(MemberFolder, "refuse_link", swap_after_the_link_check)
    assert not isinstance(_run(SITES[1]), Exception)
    assert fired, "the writer never checked the file"
    assert (moved / SITES[1].leaf).is_file(), "the record did not land in the folder that was open"
    assert _snapshot(peer) == before, "the record was written through the swapped-in link"
