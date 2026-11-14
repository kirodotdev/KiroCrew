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
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from conftest import _HAS_SYMLINKS, requires_symlinks
from kiro_crew import atomic_write, members, pinned_fs, platform_compat
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
    reason=(
        "a swap after the open is refused only where the folder is held by descriptor; "
        "the by-name arm checks and does not hold"
    ),
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


def _read_as_a_junction(monkeypatch: pytest.MonkeyPatch, named: Path) -> None:
    """Make *named* look to ``Path.is_symlink`` as a Windows junction does: not a link.

    A junction is a reparse point that ``is_symlink`` does not report, so only
    ``is_reparse_point`` finds it. On Windows ``make_dir_link`` already makes one and
    nothing is patched; elsewhere the real link at *named* stays and only the
    ``is_symlink`` answer given to ``member_folder`` changes.
    """
    if sys.platform == "win32":
        return
    real_is_symlink = Path.is_symlink

    def is_symlink(self: Path) -> bool:
        # Only the code under test is told; the test's own reads of the disk see the link.
        if self == named and sys._getframe(1).f_code.co_filename.endswith("member_folder.py"):
            return False
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_plain_member_folder_is_written_as_before(site: Site) -> None:
    result = _run(site)
    assert not isinstance(result, Exception), result
    assert (_members() / members.member_slug(SLUG) / site.leaf).is_file()


def _assert_written_where_members_points(site: Site, outside: Path) -> None:
    """The write landed in the folder the link (or junction) at ``members`` points to.

    The folder the write made and the file are owner-only; the folder the link names
    keeps the mode it had.
    """
    named = _members()
    linked = named.is_symlink() or platform_compat.is_link_or_junction(named)
    assert linked, "the link at members was replaced"
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


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_junction_at_members_leads_to_the_folder_it_names(
    site: Site, pinned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A junction is not a symlink to ``Path.is_symlink``; it is a link all the same."""
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    outside = _empty_folder(tmp_path)
    make_dir_link(_members(), outside)
    _read_as_a_junction(monkeypatch, _members())
    result = _run(site)
    assert not isinstance(result, Exception), result
    _assert_written_where_members_points(site, outside)


JUNCTION_KINDS = [
    "dangling",
    "itself",
    "link_to_a_file",
    "data_home_itself",
    "data_home_absolute",
    "parent_of_the_data_home",
    "grandparent_of_the_data_home",
]


@requires_symlinks
@pytest.mark.parametrize("kind", JUNCTION_KINDS)
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_junction_at_members_that_leads_to_no_existing_folder_is_refused_and_creates_nothing(
    site: Site, pinned: bool, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusals of a link at ``members`` hold when the link is read as a junction."""
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    watched = _unresolvable(kind, tmp_path, monkeypatch)
    _read_as_a_junction(monkeypatch, _members())
    before = _snapshot(watched)
    fds = _open_fds()
    result = _run(site)
    assert isinstance(result, members.MemberSlugError), result
    assert str(tmp_path) not in str(result)
    assert _open_fds() == fds
    assert _snapshot(watched) == before
    slug = members.member_slug(SLUG)
    for above in (watched, watched.parent, watched.parent.parent):
        assert not (above / slug).exists()


def test_the_folder_a_link_at_members_names_is_not_tightened_without_descriptor_pinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_pin: None
) -> None:
    """Where folders are tightened by name, the slug folder is and the linked-to folder is not."""
    outside = _empty_folder(tmp_path)
    make_dir_link(_members(), outside)
    tightened: list[Path] = []
    monkeypatch.setattr(
        platform_compat, "restrict_dir_to_owner", lambda path: tightened.append(Path(path))
    )
    assert not isinstance(_run(SITES[1]), Exception)
    assert [p.resolve() for p in tightened] == [outside.resolve() / members.member_slug(SLUG)]


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_at_members_whose_target_is_not_text_is_refused(
    site: Site, pinned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target that cannot be written as text is refused on every platform.

    Some platforms cannot make a name that is not text, so the resolved path is
    replaced by one that has a lone surrogate in it.
    """
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    outside, walks = _link_at_members_naming_a_target_that_is_not_text(tmp_path, monkeypatch)
    before = _watched(tmp_path)
    fds = _open_fds()
    result = _run(site)
    assert isinstance(result, members.MemberSlugError), result
    message = str(result)
    assert str(tmp_path) not in message
    for part in (outside.name, "folder-", "\udcff", "udcff"):
        assert part not in message, f"the message names a part of the target: {part!r}"
    assert len(walks) == 1, "the folder the link names was opened although its name is not text"
    assert _open_fds() == fds
    assert _watched(tmp_path) == before, "something was created"
    assert not (outside / members.member_slug(SLUG)).exists()


def test_the_callers_meet_a_link_at_members_whose_target_is_not_text_and_go_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The welcome record reports the refusal and returns ``False``; the create request goes on."""
    outside, _walks = _link_at_members_naming_a_target_that_is_not_text(tmp_path, monkeypatch)
    before = _watched(tmp_path)
    assert members.mark_welcome_owed(SLUG) is False
    assert "Could not record the first welcome" in caplog.text
    members.mark_create_request(SLUG, "req-1")
    assert _watched(tmp_path) == before, "something was created"
    assert not (outside / members.member_slug(SLUG)).exists()


def _watched(tmp_path: Path) -> tuple[list[str], list[str]]:
    """What is below the test's folder and below the data home, by name."""
    return _names_below(tmp_path), _names_below(_members().parent)


def _names_below(root: Path) -> list[str]:
    """The names under *root*, without reading through a link: safe for a dangling junction."""
    found: list[str] = []
    for folder, names, files in os.walk(root, followlinks=False):
        for name in (*names, *files):
            found.append(os.path.relpath(os.path.join(folder, name), root))
    return sorted(found)


def _link_at_members_naming_a_target_that_is_not_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[Any]]:
    """A link made by ``make_dir_link`` whose resolved target has a lone surrogate in its name.

    Some platforms cannot make a name that is not text, so ``Path.resolve`` is made to
    return one. Returns the folder the link really points to and the walks the helper made.
    """
    from kiro_crew import member_folder

    outside = _empty_folder(tmp_path)
    make_dir_link(_members(), outside)
    named = _members()
    real_resolve = Path.resolve
    real_walk = member_folder.open_pinned_descendant_dir
    walks: list[Any] = []

    def resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self == named:
            return outside / "folder-\udcff"
        return real_resolve(self, *args, **kwargs)

    def walk(*args: Any, **kwargs: Any) -> Any:
        walks.append(args)
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(member_folder, "open_pinned_descendant_dir", walk)
    return outside, walks


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_members_folder_that_is_a_reparse_point_but_not_a_link_is_refused_and_the_refusal_is_true(
    site: Site, monkeypatch: pytest.MonkeyPatch, no_pin: None
) -> None:
    """A folder that carries a reparse tag but is not a link (a cloud placeholder) holds its own files.

    The walk refuses every reparse point. The refusal must not call this folder a link,
    must not try to follow it, and must create nothing.
    """
    named = _members()
    named.mkdir(parents=True)
    real_lstat = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_lstat(path, *args, **kwargs)
        if Path(os.fsdecode(path)) != named:
            return result
        # A directory with a reparse tag that is not a name surrogate (a cloud-files
        # placeholder), the shape ``is_reparse_point`` reads off ``lstat`` on Windows.
        return types.SimpleNamespace(st_mode=result.st_mode, st_reparse_tag=0x9000001A)

    monkeypatch.setattr(os, "lstat", lstat)
    real_resolve = Path.resolve
    resolved: list[Path] = []

    def resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self == named:
            resolved.append(self)
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    result = _run(site)
    assert type(result) is members.MemberSlugError, result
    assert "link" not in str(result), "a folder that is not a link was called one"
    assert str(named) not in str(result)
    assert not resolved, "a folder that is not a link was resolved as if it were one"
    assert not named.is_symlink() and named.is_dir()
    assert list(named.iterdir()) == [], "something was created"


def test_the_by_name_walk_treats_an_empty_component_as_the_root_itself(
    tmp_path: Path, no_pin: None
) -> None:
    """A root whose name is empty (a drive root) is walked as the root, then the slug below it.

    ``Path / ""`` is the same path, so the walk neither fails nor leaves the root.
    """
    root = tmp_path / "volume"
    root.mkdir()
    with pinned_fs.open_pinned_descendant_dir(root, ("",), what="member folder") as held:
        assert held is None
    with pinned_fs.open_pinned_descendant_dir(
        root, ("", "below"), what="member folder", create=True
    ) as held:
        assert held is None
    assert [p.name for p in root.iterdir()] == ["below"]


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
@pytest.mark.parametrize("site", SITES, ids=IDS)
@pytest.mark.parametrize("destination", ["the_data_home", "the_parent_of_the_data_home"])
def test_a_link_made_by_make_dir_link_at_members_that_names_the_data_home_or_its_parent_is_refused(
    site: Site,
    pinned: bool,
    destination: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A junction on Windows, a symlink elsewhere; the folders are compared as ``resolve`` spells them.

    Only a Windows run shows how ``resolve`` spells a junction's target (a long-path
    prefix, 8.3 names, the case of the drive letter) next to the data home.
    """
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    named = _members()
    data_home = named.parent
    data_home.mkdir(parents=True, exist_ok=True)
    target = data_home if destination == "the_data_home" else data_home.parent
    make_dir_link(named, target)
    slug = members.member_slug(SLUG)
    names_before = (
        sorted(p.name for p in data_home.iterdir()),
        sorted(p.name for p in data_home.parent.iterdir()),
    )
    result = _run(site)
    assert type(result) is members.MemberSlugError, result
    assert str(data_home) not in str(result)
    assert (
        sorted(p.name for p in data_home.iterdir()),
        sorted(p.name for p in data_home.parent.iterdir()),
    ) == names_before, "something was created"
    assert not (data_home / slug).exists()
    assert not (data_home.parent / slug).exists()


_WINDOWS_DEVICE_NAMES = ["con", "nul", "aux", "prn", "com1", "lpt1"]


@pytest.mark.skipif(
    sys.platform != "win32", reason="a device name is a reserved file name on Windows only"
)
@pytest.mark.parametrize("name", _WINDOWS_DEVICE_NAMES)
@pytest.mark.parametrize("writer", ["greeting_marker", "member_record"])
def test_a_member_whose_slug_is_a_windows_device_name_is_refused_and_nothing_is_created_there(
    name: str, writer: str
) -> None:
    """Windows treats these names as devices in every folder, so no folder may be made for one."""
    root = _members()
    root.mkdir(parents=True, exist_ok=True)
    slug = members.member_slug(name)
    assert slug == name

    def write() -> Any:
        if writer == "greeting_marker":
            return mate_welcome.claim_greeting_marker(slug, "k")
        return members._write_member_record(
            name, None, members.WELCOME_OWED_FILENAME, {"member": name}
        )

    try:
        write()
        outcome: Any = None
    except Exception as exc:  # noqa: BLE001 - the outcome is read off the disk
        outcome = exc
    assert isinstance(outcome, members.MemberSlugError), outcome
    assert [p.name for p in root.iterdir()] == [], "something was created under members"


@pytest.mark.skipif(
    sys.platform != "win32", reason="another spelling of one folder needs a case-insensitive path"
)
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_junction_at_members_naming_the_data_home_in_another_case_is_refused_and_creates_nothing(
    site: Site,
) -> None:
    """The data home written with its letters' case swapped is still the data home."""
    named = _members()
    data_home = named.parent
    data_home.mkdir(parents=True, exist_ok=True)
    make_dir_link(named, Path(str(data_home).swapcase()))
    names_before = sorted(p.name for p in data_home.iterdir())
    result = _run(site)
    assert type(result) is members.MemberSlugError, result
    assert str(data_home) not in str(result)
    assert sorted(p.name for p in data_home.iterdir()) == names_before, "something was created"
    assert not (data_home / members.member_slug(SLUG)).exists()


def test_a_directory_at_the_greeting_marker_ends_in_false_or_the_refusal_and_never_in_a_write() -> (
    None
):
    """Where ``os.open`` reports an existing directory as another error than ``FileExistsError``,
    that error would show here; it must not turn into a write."""
    folder = _members() / members.member_slug(SLUG)
    folder.mkdir(parents=True)
    marker = folder / SITES[0].leaf
    marker.mkdir()
    result = _run(SITES[0])
    assert result is False or isinstance(result, members.MemberSlugError), result
    assert marker.is_dir() and not marker.is_symlink()
    assert list(marker.iterdir()) == [], "something was written into the folder at the marker"
    assert [p.name for p in folder.iterdir()] == [SITES[0].leaf]


@pytest.mark.parametrize(
    "slug",
    ["CON", "Ada", "ada.", "ada ", "con:stream", "ada~1", "-ada", "ada-", "", "a" * 81],
    ids=lambda s: repr(s) if len(s) < 20 else "81-long",
)
def test_a_slug_that_is_not_lower_case_letters_digits_and_hyphens_is_refused_before_any_folder_is_touched(
    slug: str,
) -> None:
    """Two spellings of one folder (case, a trailing dot or space, a stream, a short name)
    cannot both be slugs, so no slug can name another member's folder on a platform that folds them.
    """
    from kiro_crew import member_folder

    with pytest.raises(members.MemberSlugError):
        with member_folder.open_member_folder(slug):
            pytest.fail("the helper opened a folder for a slug that is not well formed")
    with pytest.raises(members.MemberSlugError):
        mate_welcome.claim_greeting_marker(slug, "k")
    assert not _members().exists(), "a folder was made before the slug was refused"


@pytest.mark.parametrize("name", ["CON", "Ada.", "ada ", "con:stream", "ADA~1"])
def test_a_member_name_is_folded_to_one_well_formed_lower_case_slug(name: str) -> None:
    slug = members.slug_for_name(name)
    assert members.validate_slug(slug) == slug
    assert slug == slug.lower()


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


@needs_pinned_walk
def test_the_folder_a_link_at_members_names_swapped_for_a_link_after_it_is_resolved_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


@needs_pinned_walk
def test_a_link_at_members_pointed_elsewhere_after_it_is_resolved_still_writes_to_the_resolved_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    except (OSError, UnicodeError):
        # Windows decodes a name given as bytes and raises ``UnicodeDecodeError``.
        pytest.skip("this platform or file system refuses names that are not text")
    os.rmdir(probe)


def _getcwd_fails_where_a_relative_path_is_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.getcwd`` raises ``FileNotFoundError`` while ``member_folder`` is resolving a path.

    Only a call that has ``member_folder.py`` on its stack (the helper's own ``resolve``
    of a relative data home, through ``realpath`` and ``abspath``) sees the failure, as
    it does when the working folder has been removed. Every other caller (the test
    runner, the fixtures, logging, ``tempfile``, the monkeypatch undo) gets the real
    working folder.
    """
    real_getcwd = os.getcwd

    def getcwd() -> str:
        frame: types.FrameType | None = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_filename.endswith("member_folder.py"):
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))
            frame = frame.f_back
        return real_getcwd()

    monkeypatch.setattr(os, "getcwd", getcwd)


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
    if kind in ("data_home_relative_and_cwd_removed", "data_home_relative_and_getcwd_fails"):
        if kind == "data_home_relative_and_cwd_removed" and sys.platform != "win32":
            # The real state: the folder the process is in is gone.
            gone = tmp_path / "gone"
            gone.mkdir()
            monkeypatch.chdir(gone)
            gone.rmdir()
        else:
            # Windows does not remove the working folder, so there the same state is
            # made by what the system reports for it: ``getcwd`` fails for the code that
            # resolves a relative path, and for nothing else in the run.
            _getcwd_fails_where_a_relative_path_is_resolved(monkeypatch)
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
    "data_home_relative_and_getcwd_fails",
]


def _open_fds() -> set[str] | None:
    """The descriptors this process holds, or ``None`` where they cannot be listed."""
    try:
        return set(os.listdir("/proc/self/fd"))
    except OSError:
        return None


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


# The kinds whose set-up in ``_unresolvable`` never calls ``os.symlink``: a plain file, a
# plain folder, a folder that cannot be searched, a failing ``mkdir`` and a failing
# ``getcwd`` or ``is_symlink``. They run where a symlink cannot be made. Every other kind
# makes a symlink and is skipped there, so a kind added to ``UNRESOLVABLE`` is skipped
# where a symlink cannot be made until it is named here.
KINDS_MADE_WITHOUT_A_SYMLINK = (
    "regular_file",
    "plain_members_not_writable",
    "data_home_not_writable_and_members_missing",
    "mkdir_fails_read_only_file_system",
    "mkdir_fails_no_space",
    "mkdir_fails_over_quota",
    "cannot_tell_whether_members_is_a_link",
    "named_folder_removed_before_the_slug_mkdir",
    "data_home_parent_cannot_be_searched",
    "data_home_relative_and_cwd_removed",
    "data_home_relative_and_getcwd_fails",
)


def _skip_what_this_platform_cannot_make(kind: str, pinned: bool = True) -> None:
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
    if kind not in KINDS_MADE_WITHOUT_A_SYMLINK and not _HAS_SYMLINKS:
        pytest.skip(requires_symlinks.kwargs["reason"])


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
    fds = _open_fds()
    result = _run(site)
    assert isinstance(result, members.MemberSlugError), result
    assert _open_fds() == fds
    assert _snapshot(watched) == before
    assert not (tmp_path / "missing").exists()
    assert not (tmp_path / "nowhere").exists()
    slug = members.member_slug(SLUG)
    for above in (watched, watched.parent, watched.parent.parent):
        assert not (above / slug).exists()
    assert modes == {p: stat.S_IMODE(p.lstat().st_mode) for p in modes}


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
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
def test_a_loop_reported_as_an_oserror_is_refused_like_one_reported_as_a_runtimeerror(
    pinned: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``Path.resolve`` reports a loop as ``OSError`` on some Pythons and ``RuntimeError`` on others."""
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
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
    fds = _open_fds()
    with pytest.raises(members.MemberSlugError) as refused:
        with member_folder.open_member_folder(members.member_slug(SLUG)):
            pytest.fail("the helper opened a folder behind a link that leads nowhere")
    message = str(refused.value)
    assert str(tmp_path) not in message
    assert "hop-" not in message and "folder-" not in message and "nowhere" not in message
    assert _open_fds() == fds


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
    fds = _open_fds()
    with pytest.raises(type(raised)) as at_the_helper:
        with member_folder.open_member_folder(members.member_slug(SLUG)):
            pytest.fail("the helper opened a folder it could not make")
    assert at_the_helper.value is raised
    with pytest.raises(type(raised)) as at_a_writer:
        SITES[0].call()
    assert at_a_writer.value is raised
    assert _open_fds() == fds


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
    fds = _open_fds()
    with pytest.raises(type(raised)) as caught:
        with member_folder.open_member_folder(members.member_slug(SLUG)):
            raise raised
    assert caught.value is raised
    assert _open_fds() == fds


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
        "forty_five_links",
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
    elif how == "forty_five_links":
        target = data_home / "real"
        spelling = _a_chain_of_links(tmp_path)
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


def _a_link_at_members_to_a_folder_that_is_then_removed(tmp_path: Path) -> Path:
    """``make_dir_link`` (a junction on Windows, which needs no privilege) to a folder, then the folder is removed.

    The link is left pointing at nothing. Returns the folder that was removed.
    """
    vanished = tmp_path / "vanished"
    vanished.mkdir()
    make_dir_link(_members(), vanished)
    vanished.rmdir()
    return vanished


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_a_link_to_a_folder_that_was_removed_is_refused_and_creates_nothing(
    site: Site, pinned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dangling link made without the symlink privilege: refused, nothing made behind it."""
    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    vanished = _a_link_at_members_to_a_folder_that_is_then_removed(tmp_path)
    before = _watched(tmp_path)
    fds = _open_fds()
    result = _run(site)
    assert isinstance(result, members.MemberSlugError), result
    message = str(result)
    assert str(tmp_path) not in message and vanished.name not in message
    assert _open_fds() == fds
    assert _watched(tmp_path) == before, "something was created"
    assert not vanished.exists(), "the folder the link named was made again"


def test_the_callers_report_a_link_to_a_folder_that_was_removed_and_go_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vanished = _a_link_at_members_to_a_folder_that_is_then_removed(tmp_path)
    before = _watched(tmp_path)
    assert members.mark_welcome_owed(SLUG) is False
    assert "Could not record the first welcome" in caplog.text
    members.mark_create_request(SLUG, "req-1")
    assert _watched(tmp_path) == before, "something was created"
    assert not vanished.exists()


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "no_pin"])
def test_the_helper_refuses_a_link_to_a_folder_that_was_removed_and_names_no_path(
    pinned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import member_folder

    if not pinned:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    vanished = _a_link_at_members_to_a_folder_that_is_then_removed(tmp_path)
    fds = _open_fds()
    with pytest.raises(members.MemberSlugError) as refused:
        with member_folder.open_member_folder(members.member_slug(SLUG)):
            pytest.fail("the helper opened a folder behind a link that leads nowhere")
    message = str(refused.value)
    assert str(tmp_path) not in message and vanished.name not in message
    assert _open_fds() == fds
