"""Merge restore resolves each core-file name once.

Checking a core file by name (``is_file()``) and then copying it by name
(``shutil.copy2``), or reading the live ``crons.json`` by name and rewriting it
by name, is two resolutions of one string: a dangling link at the live name passes
``not is_file()`` and is then followed by the copy, a hardlink alias in the
agent-writable staging tree is a regular file every name check accepts, and a
link swapped in between the cron read and rewrite redirects the rewrite.

Every fixture is a real link or hardlink on a real filesystem.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import portability
from kiro_crew import snapshot as snapshot_mod
from kiro_crew import snapshot_merge

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX link fixtures")

SECRET = b"AWS_SECRET_STAND_IN=hunter2\n"


def _dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    snap, mc, outside = tmp_path / "snap", tmp_path / "home", tmp_path / "outside"
    for d in (snap, mc, outside):
        d.mkdir()
    return snap, mc, outside


def _crons(*names: str) -> str:
    return json.dumps({"jobs": [{"name": n, "id": n} for n in names]})


# ── Sites 1-3: memory.db, memory_index.db and crons.json install-if-absent ──


@requires_symlinks
@pytest.mark.parametrize("name,component", [("memory.db", "memory"), ("crons.json", "crons")])
def test_a_dangling_link_at_the_live_name_is_not_followed(tmp_path, name, component):
    snap, mc, outside = _dirs(tmp_path)
    (snap / name).write_bytes(b"bundle bytes" if name.endswith(".db") else _crons("a").encode())
    target = outside / "planted"
    (mc / name).symlink_to(target)

    snapshot_mod._do_merge(snap, mc, [component])

    assert not target.exists(), "the copy followed a dangling link out of the data home"
    assert (mc / name).is_symlink(), "the planted entry is left as it was"


@requires_symlinks
def test_the_index_is_not_installed_through_a_dangling_link(tmp_path):
    snap, mc, outside = _dirs(tmp_path)
    (snap / "memory.db").write_bytes(b"db")
    (snap / "memory_index.db").write_bytes(b"index")
    target = outside / "planted-index"
    (mc / "memory_index.db").symlink_to(target)

    snapshot_mod._do_merge(snap, mc, ["memory"])

    assert (mc / "memory.db").read_bytes() == b"db"
    assert not target.exists(), "the index copy followed a dangling link"


@pytest.mark.parametrize("name,component", [("memory.db", "memory"), ("crons.json", "crons")])
def test_a_hardlink_alias_in_staging_is_not_installed(tmp_path, capsys, name, component):
    snap, mc, outside = _dirs(tmp_path)
    secret = outside / "credential"
    secret.write_bytes(SECRET)
    os.link(secret, snap / name)

    snapshot_mod._do_merge(snap, mc, [component])

    assert not (mc / name).exists(), "a hardlink alias's bytes were installed as a core file"
    assert "not restored" in capsys.readouterr().out


@requires_symlinks
def test_a_symlink_in_staging_is_not_installed(tmp_path):
    snap, mc, outside = _dirs(tmp_path)
    secret = outside / "credential"
    secret.write_bytes(SECRET)
    (snap / "memory.db").symlink_to(secret)

    snapshot_mod._do_merge(snap, mc, ["memory"])

    assert not (mc / "memory.db").exists()


def test_absent_core_files_are_still_installed_with_their_mode(tmp_path, capsys):
    snap, mc, _ = _dirs(tmp_path)
    (snap / "memory.db").write_bytes(b"db")
    (snap / "memory_index.db").write_bytes(b"index")
    (snap / "crons.json").write_text(_crons("a"), encoding="utf-8")
    os.chmod(snap / "crons.json", 0o640)

    snapshot_mod._do_merge(snap, mc, ["memory", "crons"])

    assert (mc / "memory.db").read_bytes() == b"db"
    assert (mc / "memory_index.db").read_bytes() == b"index"
    assert json.loads((mc / "crons.json").read_text())["jobs"][0]["name"] == "a"
    assert (mc / "crons.json").stat().st_mode & 0o777 == 0o640
    out = capsys.readouterr().out
    assert "Memory: copied" in out and "Crons: copied" in out
    assert "✅ memory" in out and "✅ crons" in out


@requires_symlinks
def test_the_dashboard_import_twins_refuse_a_dangling_link_too(tmp_path, monkeypatch):
    """`portability`'s merge branch carries the same copies; one helper serves both."""
    snap, mc, outside = _dirs(tmp_path)
    (snap / "hooks.json").write_text("{}", encoding="utf-8")
    target = outside / "planted"
    (mc / "hooks.json").symlink_to(target)

    assert portability._install_core_file_if_absent(snap / "hooks.json", mc / "hooks.json") is False
    assert not target.exists()


@requires_symlinks
def test_a_staged_link_is_refused_where_open_cannot_refuse_links(tmp_path, monkeypatch):
    """Windows shape, simulated: no O_NOFOLLOW and no dir_fd.

    There a by-name ``os.open`` follows a reparse point and the target passes the
    regular/one-link checks, so the source must be opened through
    ``open_file_no_reparse`` (which settles the final name in the open itself) and
    handed over as a descriptor. The stand-in below refuses a link the way the
    Windows ``CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT)`` path does, with ELOOP.
    """
    import errno

    from kiro_crew import pinned_fs, platform_compat

    snap, mc, outside = _dirs(tmp_path)
    secret = outside / "credential"
    secret.write_bytes(SECRET)
    (snap / "hooks.json").symlink_to(secret)

    def windows_like_open(path, *, nonblocking=False, links_only=False):
        if os.path.islink(path):
            raise OSError(errno.ELOOP, "reparse point at the final component", str(path))
        return os.open(str(path), os.O_RDONLY)

    monkeypatch.delattr(os, "O_NOFOLLOW")
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(platform_compat, "open_file_no_reparse", windows_like_open)

    assert (
        snapshot_merge._install_core_file_if_absent(snap / "hooks.json", mc / "hooks.json") is False
    )
    assert not (mc / "hooks.json").exists(), "a staged link's target was installed"


# ── Site 5: _merge_crons reads and rewrites the live store ──


def _cron_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    snap, mc, outside = _dirs(tmp_path)
    src, dst = snap / "crons.json", mc / "crons.json"
    src.write_text(_crons("imported"), encoding="utf-8")
    dst.write_text(_crons("local"), encoding="utf-8")
    return src, dst, outside


@requires_symlinks
def test_a_live_store_swapped_for_a_link_between_read_and_rewrite_is_not_written_through(
    tmp_path, monkeypatch
):
    """The exact window: the swap lands after both reads, before the rewrite.

    ``_usable_cron_shape`` runs on the destination after it has been read and before
    anything is written, so swapping there is the last instant the old code's by-name
    ``write_text`` would still follow.
    """
    src, dst, outside = _cron_pair(tmp_path)
    victim = outside / "victim.json"
    victim.write_bytes(SECRET)
    real = snapshot_merge._usable_cron_shape

    def swapping(parsed, path):
        if Path(path) == dst:
            dst.unlink()
            dst.symlink_to(victim)
        return real(parsed, path)

    monkeypatch.setattr(snapshot_merge, "_usable_cron_shape", swapping)
    assert snapshot_merge._merge_crons(src, dst) is False
    assert victim.read_bytes() == SECRET, "the rewrite followed a link swapped in after the read"


def test_a_live_store_replaced_by_a_new_file_after_the_read_is_not_overwritten(
    tmp_path, monkeypatch
):
    """A regular file planted at the name is not mistaken for the store that was read.

    The unlink frees the read inode's directory entry; with the read descriptor
    still open the inode cannot be freed, so the replacement can never be handed
    the same inode number and pass the identity re-check.
    """
    src, dst, _ = _cron_pair(tmp_path)
    planted = _crons("someone-else").encode()
    real = snapshot_merge._usable_cron_shape

    def swapping(parsed, path):
        if Path(path) == dst:
            dst.unlink()
            dst.write_bytes(planted)
        return real(parsed, path)

    monkeypatch.setattr(snapshot_merge, "_usable_cron_shape", swapping)
    assert snapshot_merge._merge_crons(src, dst) is False
    assert dst.read_bytes() == planted


@requires_symlinks
def test_a_link_that_reuses_the_read_inode_number_is_still_refused(tmp_path, monkeypatch):
    """A filesystem may hand a freed inode number to the link planted at the name.

    That made the identity re-check alone pass on CI. The re-check also requires a
    regular file, so the link is refused whatever number it was given; reuse is
    simulated here by reporting the read inode's number for the planted entry.
    """
    from kiro_crew import pinned_fs

    src, dst, outside = _cron_pair(tmp_path)
    victim = outside / "victim.json"
    victim.write_bytes(SECRET)
    read_ident = (dst.stat().st_dev, dst.stat().st_ino)
    real_shape, real_stat_at = snapshot_merge._usable_cron_shape, pinned_fs.stat_at

    def swapping(parsed, path):
        if Path(path) == dst:
            dst.unlink()
            dst.symlink_to(victim)
        return real_shape(parsed, path)

    def reused(dir_fd, name):
        st = real_stat_at(dir_fd, name)
        if st is None:
            return None
        fields = list(st)
        fields[1], fields[2] = read_ident[1], read_ident[0]  # st_ino, st_dev
        return os.stat_result(fields)

    monkeypatch.setattr(snapshot_merge, "_usable_cron_shape", swapping)
    monkeypatch.setattr(pinned_fs, "stat_at", reused)
    assert snapshot_merge._merge_crons(src, dst) is False
    assert dst.is_symlink() and victim.read_bytes() == SECRET


def test_a_hardlinked_live_store_is_refused(tmp_path):
    src, dst, outside = _cron_pair(tmp_path)
    alias = outside / "alias.json"
    os.link(dst, alias)
    before = alias.read_bytes()

    assert snapshot_merge._merge_crons(src, dst) is False
    assert alias.read_bytes() == before, "the merge rewrote a file it was not pointed at"


@requires_symlinks
def test_a_staged_cron_store_that_is_a_link_is_not_imported(tmp_path):
    src, dst, outside = _cron_pair(tmp_path)
    other = outside / "someone-elses-crons.json"
    other.write_text(_crons("foreign"), encoding="utf-8")
    src.unlink()
    src.symlink_to(other)
    before = dst.read_bytes()

    assert snapshot_merge._merge_crons(src, dst) is False
    assert dst.read_bytes() == before


def test_the_rewrite_keeps_the_live_stores_access_control_acl(tmp_path):
    """A named POSIX ACL on the live store survives the replace.

    ``mode=`` carries permission bits only, so a fresh inode installed by the
    rewrite would drop a deny entry the owner set and widen who can read the
    store. The ACL is carried from the read descriptor onto the replacement.
    """
    src, dst, _ = _cron_pair(tmp_path)
    acl = "system.posix_acl_access"
    if not hasattr(os, "setxattr"):
        pytest.skip("no os xattr API on this platform (macOS)")
    try:
        os.setxattr(dst, "user.kirocrew-probe", b"1")
        os.removexattr(dst, "user.kirocrew-probe")
    except OSError:
        pytest.skip("filesystem has no xattrs")
    import shutil
    import subprocess

    if shutil.which("setfacl") is None:
        pytest.skip("setfacl not installed")
    subprocess.run(["setfacl", "-m", "u:nobody:---", str(dst)], check=True)
    before = os.getxattr(dst, acl)
    ino = dst.stat().st_ino

    assert snapshot_merge._merge_crons(src, dst) is True
    assert dst.stat().st_ino != ino, "expected a replaced inode, or the test proves nothing"
    assert os.getxattr(dst, acl) == before, "the rewrite dropped the store's named ACL"


def test_an_unswapped_merge_imports_and_keeps_the_store_regular_with_its_mode(tmp_path):
    src, dst, _ = _cron_pair(tmp_path)
    os.chmod(dst, 0o640)

    assert snapshot_merge._merge_crons(src, dst) is True
    names = [j["name"] for j in json.loads(dst.read_text())["jobs"]]
    assert names == ["local", "imported"]
    assert not dst.is_symlink()
    assert dst.stat().st_mode & 0o777 == 0o640


# ── A refused memory_index.db install is reported, not silent ──


def _sqlite(path: Path, marker: str) -> None:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", (marker,))
        conn.commit()


def test_a_leftover_index_beside_a_freshly_installed_memory_db_is_reported(tmp_path, capsys):
    snap, mc, _ = _dirs(tmp_path)
    _sqlite(snap / "memory.db", "bundle")
    (snap / "memory_index.db").write_bytes(b"bundle index")
    (mc / "memory_index.db").write_bytes(b"old index")

    snapshot_mod._do_merge(snap, mc, ["memory"])

    assert (mc / "memory_index.db").read_bytes() == b"old index", "the index is never overwritten"
    out = capsys.readouterr().out
    assert (
        "memory_index.db: existing index kept, not replaced; the gateway rebuilds it "
        "on its next start"
    ) in out


@requires_symlinks
def test_an_occupied_live_name_is_named_without_pointing_at_a_report_that_is_not_there(
    tmp_path, capsys
):
    snap, mc, outside = _dirs(tmp_path)
    (snap / "crons.json").write_text(_crons("a"), encoding="utf-8")
    (mc / "crons.json").symlink_to(outside / "planted")

    snapshot_mod._do_merge(snap, mc, ["crons"])

    out = capsys.readouterr().out
    assert "crons.json: not restored; the existing entry at that name is not a regular file" in out
    assert "see above" not in out


def test_the_dashboard_import_reports_a_leftover_index_as_refused(tmp_path):
    import zipfile
    from unittest.mock import patch

    src = tmp_path / "src"
    src.mkdir()
    _sqlite(src / "memory.db", "bundle")
    _sqlite(src / "memory_index.db", "bundle index")
    z = tmp_path / "import.zip"
    with zipfile.ZipFile(str(z), "w") as zf:
        zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
        zf.write(str(src / "memory.db"), "snap/memory.db")
        zf.write(str(src / "memory_index.db"), "snap/memory_index.db")
    target = tmp_path / "target_mc"
    target.mkdir()
    (target / "memory_index.db").write_bytes(b"old index")

    with patch.object(portability, "config_dir", return_value=target):
        with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
            summary = portability.apply_import_zip(z, mode="merge")

    assert (target / "memory_index.db").read_bytes() == b"old index"
    assert "memory (copied)" in summary["items"], summary
    kept = "memory search index (kept the existing one; rebuilt when the gateway restarts)"
    assert kept in summary["items"], summary
    # The Portability tab's NOT_APPLIED_ITEM keys on "(kept", so it is not counted as imported.
    assert "(kept " in kept
    assert "see above" not in " ".join(summary["items"]), summary
    assert "memory_index" in summary.get("refused_merges", []), summary


# ── Where the ACL cannot be carried (macOS), the merge keeps the inode ──


def _simulate_macos(monkeypatch) -> None:
    """macOS shape on Linux: pinning works, the xattr ACL carry does not.

    A simulation: no real Mac runs this, and a native macOS ACL is not created.
    """
    from kiro_crew import atomic_write

    monkeypatch.setattr(atomic_write, "ACCESS_CONTROL_XATTRS_SUPPORTED", False)


def test_where_the_acl_cannot_be_carried_the_store_is_rewritten_in_place(tmp_path, monkeypatch):
    """An atomic replace there would publish a fresh inode without the owner's ACL."""
    _simulate_macos(monkeypatch)
    src, dst, _ = _cron_pair(tmp_path)
    os.chmod(dst, 0o640)
    ino = dst.stat().st_ino

    assert snapshot_merge._merge_crons(src, dst) is True

    assert dst.stat().st_ino == ino, "the store was replaced, so a native ACL on it is dropped"
    assert {j["name"] for j in json.loads(dst.read_text())["jobs"]} == {"local", "imported"}
    assert dst.stat().st_mode & 0o777 == 0o640


@requires_symlinks
def test_the_in_place_rewrite_still_refuses_a_store_swapped_after_the_read(tmp_path, monkeypatch):
    _simulate_macos(monkeypatch)
    src, dst, outside = _cron_pair(tmp_path)
    victim = outside / "victim.json"
    victim.write_bytes(SECRET)
    real = snapshot_merge._usable_cron_shape

    def swapping(parsed, path):
        if Path(path) == dst:
            dst.unlink()
            dst.symlink_to(victim)
        return real(parsed, path)

    monkeypatch.setattr(snapshot_merge, "_usable_cron_shape", swapping)
    assert snapshot_merge._merge_crons(src, dst) is False
    assert victim.read_bytes() == SECRET


def test_where_the_acl_can_be_carried_the_store_is_still_replaced_atomically(tmp_path):
    from kiro_crew import atomic_write

    if not atomic_write.ACCESS_CONTROL_XATTRS_SUPPORTED:
        pytest.skip("this platform takes the in-place path")
    src, dst, _ = _cron_pair(tmp_path)
    ino = dst.stat().st_ino

    assert snapshot_merge._merge_crons(src, dst) is True
    assert dst.stat().st_ino != ino, "the atomic replace was dropped where it is safe to keep"
