"""The boot side: which generation the restore reads, and how it publishes what it read.

Item 10 of the list in ``test_sidecar_durability_gaps.py``, with the groups that grew from it.
The restore reads the authority pair from the generation the pointer commits, and nowhere else:
a bucket with no pointer is generation 0, a first boot with every authority name absent, and an
orphaned generation whose pointer never landed is not discovered. A committed generation missing
a member refuses the boot, and so does a pointer that cannot be read or parsed, or that
under-lists a name this version knows. What the restore read is published into directories the
agent writes in, so publication links the descriptor it wrote, creates the temporary through the
directory it pinned, and reports success only when the bytes are reachable by name. That
descriptor path is also why the suite collects only on Linux, and the collection gate that says
so is pinned here.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Callable

import pytest
from container.common import keys, statefile
from container.sidecar import restore as restore_mod

from .sidecar_durability_gaps_helpers import (
    _GEN,
    _Bucket,
    _pointer,
    _settings,
)

# --- 10. publication links the inode we wrote, not a name we reopened -------------


def test_publication_links_the_open_descriptor_not_a_reopened_name(tmp_path):
    """Closing the temporary first would publish whatever its name pointed at by then.

    These directories are ones the agent writes in, so a concurrent turn replacing the
    temporary between the close and the link would have its own inode published under the
    target name and receive every later write. Linking the descriptor removes the window.
    """
    target = tmp_path / "published.json"

    assert statefile.link_new(target, b'{"keys": []}', prefix="probe-")
    assert target.read_bytes() == b'{"keys": []}'


def test_publication_refuses_an_existing_target_without_clobbering_it(tmp_path):
    """An existing file is the copy to keep, and the refusal is the filesystem's."""
    target = tmp_path / "published.json"
    target.write_bytes(b"older and better\n")

    assert statefile.link_new(target, b"newer\n", prefix="probe-") is False
    assert target.read_bytes() == b"older and better\n"


def test_publication_leaves_no_temporary_behind(tmp_path):
    """On both paths out: the one that published, and the one that found a file there."""
    target = tmp_path / "published.json"
    statefile.link_new(target, b"first\n", prefix="probe-")
    statefile.link_new(target, b"second\n", prefix="probe-")

    assert [p.name for p in tmp_path.iterdir()] == ["published.json"]


def test_the_collection_gate_declines_off_linux_not_merely_off_posix():
    """Read as source text, because the branch it pins cannot be taken on this host.

    macOS is POSIX and has no ``/proc``, so a gate written against ``os.name`` admits a
    platform where publication's descriptor path does not exist and every test reaching it
    fails on the platform rather than on the code. The condition is the thing being
    pinned, and only its source can say what it tests on a host that never takes it.
    """
    conftest = pathlib.Path(__file__).parent / "conftest.py"
    lines = conftest.read_text(encoding="utf-8").splitlines()
    deps_branch = next(i for i, line in enumerate(lines) if line.startswith("elif _missing_image"))
    gate = next(lines[i] for i in range(deps_branch - 1, -1, -1) if lines[i].startswith("if "))
    assert "sys.platform" in gate and '"linux"' in gate, gate
    assert "os.name" not in gate, gate


def test_a_committed_generation_wins_over_legacy_keys_in_the_same_bucket(tmp_path):
    """A bucket holding BOTH layouts: the pointer decides, and the legacy keys are older.

    This is what a bucket looks like the moment the protocol is adopted -- generation 0 is
    the pair the previous writer left, and it is never deleted or rewritten. Reading it
    after a generation has been committed would boot from the older publication and then
    flush it forward over the newer one.
    """
    settings = _settings(tmp_path)
    present = {
        f"gen/{_GEN}/{name}": b'{"from": "the committed generation"}'
        for name in keys.AUTHORITY_NAMES
    }
    for name in keys.AUTHORITY_NAMES:
        present[f"data/{name}"] = b'{"from": "generation zero"}'
    present[keys.AUTHORITY_POINTER_NAME] = _pointer()
    bucket = _Bucket(present)

    restore_mod.restore_authority(settings, bucket)

    fetched = [key for key in bucket.gets if key.endswith(".json")]
    assert all(f"gen/{_GEN}/" in key for key in fetched if "authority" not in key)
    assert not any(key.endswith(f"data/{name}") for key in fetched for name in keys.AUTHORITY_NAMES)


# --- 11. the pair is read from the committed generation and nowhere else -----------------------


class _UnreadablePointer(_Bucket):
    """A bucket whose pointer cannot be read, which is not the same as not holding one."""

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        if key.endswith(keys.AUTHORITY_POINTER_NAME):
            raise PermissionError("the pointer is there and this task may not read it")
        return super().get(key, limit=limit)


@pytest.mark.parametrize("missing", list(keys.AUTHORITY_NAMES))
def test_a_committed_generation_that_lost_a_member_refuses_the_boot(tmp_path, missing):
    """The case that must REFUSE: a pair was published whole and one member is gone now.

    Starting from the remaining file lets the backend flush its own empty view of the other
    over a real conversation list. Either half is the same hazard, so both are pinned.
    """
    settings = _settings(tmp_path)
    present = {f"gen/{_GEN}/{name}": b"{}" for name in keys.AUTHORITY_NAMES if name != missing}
    present[keys.AUTHORITY_POINTER_NAME] = _pointer()

    with pytest.raises(restore_mod.RestoreFailed, match="does not hold"):
        restore_mod.restore_authority(settings, _Bucket(present))


def test_a_committed_generation_holding_its_whole_pair_restores_it(tmp_path):
    """The ordinary case: a committed generation holding the pair it was committed with."""
    settings = _settings(tmp_path)
    present = {f"gen/{_GEN}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}
    present[keys.AUTHORITY_POINTER_NAME] = _pointer()

    result = restore_mod.restore_authority(settings, _Bucket(present))

    assert sorted(result.restored + result.kept_local) == sorted(keys.AUTHORITY_NAMES)


def test_a_pointer_that_cannot_be_read_refuses_the_boot(tmp_path):
    """Unreadable is not absent, and only absent is permission to boot on a partial pair.

    Reading a denial as "no pointer" would hand back the very boot the pointer gates, so
    posture matches the one this module already takes for an authority file it cannot read.
    """
    settings = _settings(tmp_path)

    with pytest.raises(restore_mod.RestoreFailed, match="could not be read"):
        restore_mod.restore_authority(
            settings, _UnreadablePointer({keys.AUTHORITY_NAMES[0]: b"{}"})
        )


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"authority": "session_map.json"}'])
def test_a_pointer_that_does_not_parse_refuses_the_boot(tmp_path, raw):
    """A pointer present but unusable cannot say which generation is committed."""
    settings = _settings(tmp_path)
    present = {keys.AUTHORITY_NAMES[0]: b"{}", keys.AUTHORITY_POINTER_NAME: raw}

    with pytest.raises(restore_mod.RestoreFailed):
        restore_mod.restore_authority(settings, _Bucket(present))


def test_a_pointer_that_under_lists_a_known_name_refuses_the_boot(tmp_path):
    """A pointer omitting a name this version knows is unusable, not a partial generation.

    Read as a generation that simply contains one file, the omitted name would present as
    legitimately absent -- the same answer a first boot gets -- and the backend would flush
    its own empty view over whatever the bucket holds. The writer commits the pair whole,
    so a pointer under-listing it is an object no cycle of this writer produced.
    """
    settings = _settings(tmp_path)
    pointer = json.dumps({"generation": _GEN, "authority": [keys.AUTHORITY_NAMES[0]]})
    present = {f"gen/{_GEN}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}
    present[keys.AUTHORITY_POINTER_NAME] = pointer.encode()

    with pytest.raises(restore_mod.RestoreFailed):
        restore_mod.restore_authority(settings, _Bucket(present))


def test_a_pointer_naming_a_name_this_version_does_not_know_still_restores(tmp_path):
    """The tolerance the check above must not cost: a rollback has to stay bootable.

    A newer writer that commits a third authority file names it in the pointer. This
    version cannot restore what it has no name for, but the pair it does know is whole and
    committed, so refusing would make rolling back to this version unbootable on a bucket
    a newer one wrote -- a worse failure than ignoring a file this version never reads.
    """
    settings = _settings(tmp_path)
    listed = [*keys.AUTHORITY_NAMES, "a_later_authority_file.json"]
    present = {f"gen/{_GEN}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}
    present[keys.AUTHORITY_POINTER_NAME] = json.dumps(
        {"generation": _GEN, "authority": listed}
    ).encode()

    result = restore_mod.restore_authority(settings, _Bucket(present))

    assert result.absent == []


def test_both_absent_is_still_a_first_boot(tmp_path):
    """The allowed case has to stay allowed, or no crew could ever start."""
    settings = _settings(tmp_path)

    result = restore_mod.restore_authority(settings, _Bucket({}))

    assert sorted(result.absent) == sorted(keys.AUTHORITY_NAMES)
    assert result.restored == []


# --- 13. the temporary is created in the directory we pinned ----------------------


def test_publication_creates_its_temporary_through_the_pinned_directory():
    """Creating it by path lets a rename between the open and the create detach it.

    The bytes would land in the replacement directory while the link published into the
    old, detached one, so the backend would start from an empty history and the backup
    would then overwrite the bucket with it.
    """
    source = pathlib.Path(statefile.__file__).read_text(encoding="utf-8")

    assert "dir_fd=parent_fd" in source
    assert "tempfile.mkstemp" not in source


def test_publication_still_lands_the_bytes_through_the_pinned_directory(tmp_path):
    """Non-vacuity for the check above: the mechanism has to still work."""
    target = tmp_path / "published.json"

    assert statefile.link_new(target, b'{"keys": ["cust-1"]}', prefix="probe-")
    assert target.read_bytes() == b'{"keys": ["cust-1"]}'
    assert [p.name for p in tmp_path.iterdir()] == ["published.json"]


# --- 15. publication is reported only when the bytes are reachable by name -----------


def test_publication_refuses_a_parent_renamed_under_it(tmp_path):
    """Pinning the parent makes every step agree; it does not make them reachable.

    A rename after the descriptor is opened detaches that directory from all of them at
    once, so the temporary is created there, the link publishes there, and both agree on a
    directory that the path does not resolve to. Reported as published, the caller's next
    act is to start the conversation with no history and let the following backup upload
    that emptiness over the bucket's copy.
    """
    parent = tmp_path / "sessions"
    parent.mkdir()
    target = parent / "session_map.json"
    real_link = os.link

    def rename_the_parent_then_link(src, dst, **kw):
        # Exactly the window: the descriptor still addresses this inode, and the path stops
        # naming it. A fresh directory takes its place, as a concurrent turn would leave.
        parent.rename(tmp_path / "detached")
        (tmp_path / "sessions").mkdir()
        return real_link(src, dst, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "link", rename_the_parent_then_link)
        with pytest.raises(OSError, match="no longer resolves"):
            statefile.link_new(target, b'{"keys": ["cust-1"]}', prefix="probe-")

    assert not target.exists(), "the path must not name a published file"
    assert not (
        tmp_path / "detached" / "session_map.json"
    ).exists(), "the copy left in the detached directory must be removed, not abandoned"


def test_publication_refuses_when_the_replacement_directory_holds_that_name(tmp_path):
    """Existence at the path is not the test; being OUR inode is.

    The directory that takes the old path can already hold a file of the same name -- a
    concurrent turn that renamed ours aside and wrote its own. The name then resolves, so a
    check for existence is satisfied while the bytes just written are in the detached
    directory and the caller is told they were published. Only comparing the inode the link
    actually created separates the two.
    """
    parent = tmp_path / "sessions"
    parent.mkdir()
    target = parent / "session_map.json"
    real_link = os.link

    def swap_in_a_directory_that_already_has_the_name(src, dst, **kw):
        parent.rename(tmp_path / "detached")
        replacement = tmp_path / "sessions"
        replacement.mkdir()
        (replacement / "session_map.json").write_bytes(b"someone else's index\n")
        return real_link(src, dst, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "link", swap_in_a_directory_that_already_has_the_name)
        with pytest.raises(OSError, match="no longer resolves"):
            statefile.link_new(target, b'{"keys": ["cust-1"]}', prefix="probe-")

    # The stranger's file is untouched: the unlink goes through the pinned descriptor, so it
    # can only reach the copy in the directory ours actually landed in.
    assert target.read_bytes() == b"someone else's index\n"
    assert not (tmp_path / "detached" / "session_map.json").exists()


def test_publication_does_not_unlink_a_foreign_file_that_replaced_our_name(tmp_path):
    """The cleanup must remove OUR inode, never whatever is at the name.

    The parent is NOT detached here: it stays the live directory the pinned descriptor
    holds. What changes is the name -- a concurrent writer renames its own newer file over
    ours in that same directory after our link lands. The fresh resolution then sees a
    foreign inode, so publication is correctly refused; but the cleanup unlinks through the
    pinned descriptor, which still points at the live directory, so unlinking the name
    blindly would delete the stranger's file. It must stat the name first and remove it only
    when it is still our own inode -- on a foreign inode, unlink nothing.
    """
    parent = tmp_path / "sessions"
    parent.mkdir()
    target = parent / "session_map.json"
    real_link = os.link

    def rename_a_stranger_over_our_name(src, dst, **kw):
        # Our link lands first, then a concurrent turn replaces the name in the SAME live
        # directory with its own file (rename is atomic and clobbers ours).
        rv = real_link(src, dst, **kw)
        stranger = parent / "stranger.json"
        stranger.write_bytes(b"a concurrent writer's newer index\n")
        os.replace(stranger, target)
        return rv

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "link", rename_a_stranger_over_our_name)
        with pytest.raises(OSError, match="no longer resolves"):
            statefile.link_new(target, b'{"keys": ["cust-1"]}', prefix="probe-")

    # The stranger's file is untouched: the name resolved to a foreign inode, so the cleanup
    # unlinked nothing. Deleting it would destroy a file this writer never wrote.
    assert target.read_bytes() == b"a concurrent writer's newer index\n"


def test_publication_still_succeeds_when_the_parent_stays_put(tmp_path):
    """The over-strict direction: the reachability check must not refuse the ordinary path."""
    parent = tmp_path / "sessions"
    parent.mkdir()
    target = parent / "session_map.json"

    assert statefile.link_new(target, b'{"keys": ["cust-1"]}', prefix="probe-") is True
    assert target.read_bytes() == b'{"keys": ["cust-1"]}'
    assert [p.name for p in parent.iterdir()] == ["session_map.json"], "no temporary left"


def test_an_existing_target_is_still_reported_as_already_there(tmp_path):
    """``False`` is not an error, and the new check must not turn it into one."""
    parent = tmp_path / "sessions"
    parent.mkdir()
    target = parent / "session_map.json"
    target.write_bytes(b"older\n")

    assert statefile.link_new(target, b"newer\n", prefix="probe-") is False
    assert target.read_bytes() == b"older\n"


# --- 18. a bucket with no pointer is generation 0; there is no slot survey ----------------
#
# Each cycle publishes its pair under a WRITER-UNIQUE generation id and the durability
# process holds only ``get`` and ``put`` -- never a ``list`` (a ``list`` turns "bring back
# my own files" into "enumerate the bucket"). So a bucket with no committed pointer cannot
# be surveyed for an orphaned generation: it is read as GENERATION 0, the legacy ``data/``
# keys the previous protocol left, and a bucket holding neither those nor a pointer is a
# first boot. A generation whose pair uploaded but whose pointer PUT never landed is
# unreferenced and unlisted; the next cycle mints a fresh id and commits a valid pointer,
# so a boot in that window reads generation 0 rather than adopting an orphan it cannot find.


def test_an_orphaned_generation_with_no_pointer_is_not_discovered(tmp_path):
    """A pair under a unique id whose pointer never landed reads as a first boot, not adopted.

    There is no ``list`` to find an unreferenced generation id, so a bucket holding only
    such objects and no pointer is generation 0 -- here, a first boot, because no legacy
    keys are present either. The next cycle commits a fresh pointer; nothing here silently
    boots from an orphan it cannot name.
    """
    settings = _settings(tmp_path)
    for name in keys.AUTHORITY_NAMES:
        (settings.config_dir / name).unlink()
    orphan = "00000000000000000009-fedcba9876543210"
    present = {f"gen/{orphan}/{name}": b"{}" for name in keys.AUTHORITY_NAMES}

    result = restore_mod.restore_authority(settings, _Bucket(present))

    assert sorted(result.absent) == sorted(keys.AUTHORITY_NAMES)
    assert result.restored == []


def test_an_empty_bucket_with_no_pointer_still_boots(tmp_path):
    """The over-strict direction. Nothing anywhere is the genuine first boot.

    Refusing here would strand every task that has served no turn, which is the case the
    module's contract names as explicitly not a fault.
    """
    settings = _settings(tmp_path)

    result = restore_mod.restore_authority(settings, _Bucket({}))

    assert sorted(result.absent) == sorted(keys.AUTHORITY_NAMES)
    assert result.restored == []
