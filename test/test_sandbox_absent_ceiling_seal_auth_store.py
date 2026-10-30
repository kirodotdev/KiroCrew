"""The auth store's staging leaf: reconciliation, one spelling, and the legacy temp sweep.

A staging name that is a second link to the published signing key is dropped once the key's
directory entry is durable, instead of refusing every spawn; the staging directory's name
agrees across the three modules that spell it; and pre-upgrade signing-key temps in the
data-home root are swept on spawn while unrelated temps survive. The scratch data home and
the POSIX marker come from ``sandbox_absent_ceiling_seal_helpers.py``.
"""

from __future__ import annotations

import errno
import inspect
import logging
import os
from pathlib import Path

import pytest
from sandbox_absent_ceiling_seal_helpers import (  # noqa: F401 -- fixtures are requested by name
    _POSIX_ONLY,
    _no_host_ssh_probe,
    crew_home_fixture,
)

from kiro_crew import sandbox


@_POSIX_ONLY
class TestTheAuthStoreStagingReconciliation:
    """A staging name that is a second link to the published key is dropped, not refused.

    Without this the hard-link refusal is a one-way door on a state the gateway's own
    publisher creates deliberately: `token_secret` KEEPS the staging name when the directory
    sync after `os.link` fails, as a recoverable second name, and a kill between the link
    and the cleanup unlink leaves the same shape. The key then has two names, so every
    confined spawn refuses, and the operator's only exit is to find and remove the file by
    hand.
    """

    @pytest.fixture
    def staged_link(self, crew_home):
        """The published key plus a staging name that is a second link to it."""
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(mode=0o700)
        stale = staging / ".token_signing.key.4242.deadbeefcafe0123.tmp"
        os.link(key, stale)
        assert key.stat().st_nlink == 2
        return key, stale

    def test_it_removes_the_stale_staging_link(self, staged_link):
        key, stale = staged_link

        removed = sandbox._reconcile_auth_store_staging_links()

        assert not stale.exists(), "the stale staging link survived"
        assert str(stale) in removed
        assert key.read_bytes() == b"k" * 32, "reconciling must not touch the key's bytes"
        assert key.stat().st_nlink == 1

    def test_the_spawn_path_is_not_deadlocked_by_it(self, staged_link):
        """The whole point: the refusal must be reachable past this state, not blocked by it.

        Asserted as the two calls in the order the spawn path uses them, because that order
        IS the fix -- with the refusal first this raises and no later spawn can ever clear
        it.
        """
        key, stale = staged_link
        assert sandbox._reconcile_auth_store_staging_links() == [str(stale)]
        assert sandbox._refuse_aliased_masked_leaves() == ()
        assert key.stat().st_nlink == 1
        assert key.read_bytes() == b"k" * 32

    def test_it_syncs_the_keys_parent_BEFORE_dropping_the_second_name(
        self, staged_link, monkeypatch
    ):
        """Sharing an inode makes the staged name redundant for READING, not for durability.

        The publisher keeps that name precisely when the sync of the key's own parent
        failed, so at that moment the key's directory entry may not have reached the device
        and the staged name is the inode's one other reference. Dropping it first and
        crashing leaves the inode with no name at all.
        """
        key, stale = staged_link
        seen: list[tuple[str, bool]] = []

        def fake_sync(path, **kwargs):
            seen.append((str(path), stale.exists()))

        monkeypatch.setattr(sandbox, "fsync_dir", fake_sync)

        sandbox._reconcile_auth_store_staging_links()

        assert seen, "the key's own parent was never synced"
        synced, stale_still_there = seen[0]
        assert synced == str(key.parent), f"synced {synced}, not the key's parent"
        assert stale_still_there, (
            "the staged link was unlinked BEFORE the sync that makes the key's own name "
            "durable, so a crash in between would leave the inode with no name"
        )
        assert not stale.exists(), "the link survived a successful sync"

    def test_a_device_refused_sync_refuses_the_spawn_and_KEEPS_the_link(
        self, staged_link, monkeypatch
    ):
        """A failing sync means the key's entry is not durable, so its other name stays.

        Refusing is the honest outcome: the spawn cannot proceed while the leaf carries two
        names, and dropping the second one on a host whose device refuses the write trades
        a refused spawn for a possibly unrecoverable signing key.
        """
        key, stale = staged_link

        def refuse_sync(path, **kwargs):
            raise OSError(errno.EIO, "device refused the directory write")

        monkeypatch.setattr(sandbox, "fsync_dir", refuse_sync)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._reconcile_auth_store_staging_links()

        assert stale.exists(), "the key's only other name was dropped after a failed sync"
        assert key.stat().st_nlink == 2
        assert "durable" in str(caught.value)

    def test_an_unsupported_sync_still_drops_the_link(self, staged_link, monkeypatch):
        """``fsync_dir`` returns quietly where a directory sync cannot be EXPRESSED.

        Windows has no directory descriptor and some network mounts reject one, so a quiet
        return must not be read as a refusal -- that would make every such host refuse
        every spawn.
        """
        _key, stale = staged_link
        monkeypatch.setattr(sandbox, "fsync_dir", lambda path, **kwargs: None)

        removed = sandbox._reconcile_auth_store_staging_links()

        assert not stale.exists()
        assert str(stale) in removed

    def test_no_sync_when_there_is_no_matching_alias(self, crew_home, monkeypatch):
        """The cost bound: an ordinary spawn must not pay a directory sync.

        The key here has a second name, so the link-count test passes, but nothing in the
        staging directory shares its inode -- so there is nothing to drop and nothing to
        make durable first.
        """
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        os.link(key, crew_home / "alias-outside-staging")
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(mode=0o700)
        (staging / ".token_signing.key.1.aa.tmp").write_bytes(b"n" * 32)
        calls: list[str] = []
        monkeypatch.setattr(sandbox, "fsync_dir", lambda path, **kw: calls.append(str(path)))

        sandbox._reconcile_auth_store_staging_links()

        assert calls == [], f"synced {calls} with no alias to drop"

    def test_a_staged_file_for_an_UNPUBLISHED_key_is_left_alone(self, crew_home):
        """The bound, and the inode identity is what draws it.

        A publish still in flight has staged its bytes but not linked them into place, so
        its temp is a DIFFERENT inode. Unlinking that would destroy a concurrent writer's
        work between its staging write and its link.
        """
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        os.link(key, crew_home / "some-other-alias")
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(mode=0o700)
        inflight = staging / ".token_signing.key.9999.abcdef0123456789.tmp"
        inflight.write_bytes(b"n" * 32)

        removed = sandbox._reconcile_auth_store_staging_links()

        assert inflight.exists(), "an in-flight staging write for a different inode was removed"
        assert removed == []

    def test_a_non_regular_staging_entry_is_left_alone(self, crew_home):
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        os.link(key, crew_home / "alias")
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(mode=0o700)
        subdir = staging / "not-a-file"
        subdir.mkdir()

        sandbox._reconcile_auth_store_staging_links()

        assert subdir.is_dir(), "a directory inside the staging dir was removed"

    def test_it_is_quiet_when_the_key_has_one_name(self, crew_home):
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        staging = crew_home / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(mode=0o700)

        assert sandbox._reconcile_auth_store_staging_links() == []

    def test_it_is_quiet_with_no_staging_directory(self, crew_home):
        (crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF).write_bytes(b"k" * 32)

        assert sandbox._reconcile_auth_store_staging_links() == []

    def test_cleanup_runs_before_the_refusal_on_both_launch_paths(self):
        """The ordering contract, pinned in source rather than left to call-site drift.

        A future edit that moves a refusal above the cleanup restores the deadlock, and
        nothing else in the suite would notice: every test here calls the functions directly.

        Both launch paths must reach the CREDENTIAL refusal. A Seatbelt deny is path-shaped,
        so a second hard link is read straight through the profile -- the same exposure the
        namespace path refuses on -- which is why this is not namespace-only work.
        """
        for builder in (
            sandbox.namespace_argv,
            sandbox.sandbox_exec_argv,
        ):
            body = inspect.getsource(builder)
            cleanup = body.index("_reconcile_auth_store_staging_links()")
            # The namespace path reaches the credential refusal THROUGH
            # _refuse_aliased_masked_leaves, which owns the whole alias family; the Seatbelt
            # path calls only the credential half, because the symlink half is a bind-mask
            # concern. Either spelling counts, and neither platform may have none.
            # Matched WITHOUT the closing paren: the Seatbelt path passes
            # `masks_are_path_only=True` now, and pinning `()` made this guard pass only
            # while neither call took an argument -- a guard that a keyword can switch off
            # is not guarding the property it names.
            assert (
                "_refuse_multilinked_credential_leaves(" in body
                or "_refuse_aliased_masked_leaves(" in body
            ), (
                f"{builder.__name__} never judges a second hard link on a credential leaf, "
                "so an alias is readable on that platform"
            )
            for refusal_name in (
                "_refuse_multilinked_credential_leaves(",
                "_refuse_aliased_masked_leaves(",
            ):
                if refusal_name not in body:
                    continue
                assert cleanup < body.index(refusal_name), (
                    f"{builder.__name__} runs {refusal_name} before the reconciliation that "
                    "clears the link it refuses on, which makes the refusal permanent"
                )

    def test_an_unremovable_link_in_an_AGENT_created_home_does_not_refuse(
        self, crew_home, tmp_path, monkeypatch, caplog
    ):
        """The reconciler must carry the same live-home split as the passes around it.

        A sandboxed shell can create an unused home, plant a key plus a staging link to it,
        and make the directory unwritable. Escalating there would refuse every spawn on the
        host over a file the governed process wrote itself.
        """
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        planted = tmp_path / ".kirocrew"
        staging = planted / sandbox._AUTH_STORE_STAGING_LEAF
        staging.mkdir(parents=True)
        key = planted / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        os.link(key, staging / "x")

        def _refuse_unlink(_name, **_kwargs):
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(sandbox.os, "unlink", _refuse_unlink)
        with caplog.at_level(logging.WARNING):
            sandbox._reconcile_auth_store_staging_links()

        assert (staging / "x").exists(), "the link went despite the unlink failing"
        assert any(
            "stale auth-store staging link" in r.getMessage() for r in caplog.records
        ), "the condition was neither refused nor reported, so it is invisible"

    def test_the_legacy_prefix_is_derived_from_the_key_leaf(self):
        """Two spellings of one filename is how a rename half-lands."""
        assert (
            sandbox._AUTH_STORE_LEGACY_TEMP_PREFIX == f".{sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF}."
        )
        assert sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF in sandbox._CREW_HARDLINK_REFUSED_LEAVES

    def test_an_agent_CHOSEN_name_cannot_rewrite_the_remedy(self, crew_home, monkeypatch):
        """The name in the message comes from ``os.listdir`` on an agent-writable root.

        So it is attacker-chosen text going into a message an operator is asked to act on.
        Terminal escapes in it could rewrite the remedy itself, which is why every
        interpolated value is sanitised rather than only the ones that looked risky.
        """
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: crew_home.parent))
        key = crew_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        hostile = crew_home / f"{sandbox._AUTH_STORE_LEGACY_TEMP_PREFIX}1.\x1b[2Krm -rf x\r.tmp"
        hostile.write_bytes(b"staged")

        def _refuse_unlink(_name, **_kwargs):
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(sandbox.os, "unlink", _refuse_unlink)
        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._sweep_legacy_auth_store_temps()

        detail = str(caught.value)
        assert "\x1b" not in detail and "\r" not in detail, (
            "an agent-chosen filename reached the refusal message with its control bytes "
            "intact, so it can rewrite the remedy the operator is told to follow"
        )


class TestTheAuthStoreStagingLeafIsSpelledOnceInEffect:
    """The staging directory's name is written in three modules and must not drift.

    ``sandbox`` masks and precreates it, ``security.paths`` fences it, and
    ``dashboard.token_secret`` stages in it. Each spells the literal rather than
    importing, deliberately -- ``token_secret`` and ``security.paths`` stay off the
    ``sandbox`` import chain, the same trade ``service.live_target`` makes for
    ``live-target-staging``. The cost of that choice is paid here: a rename in one module
    that misses another silently leaves the temp unmasked or unfenced, which is the whole
    exposure, so the equality is asserted instead of assumed.
    """

    def test_the_three_spellings_agree(self):
        from kiro_crew.dashboard import token_secret as ts

        assert sandbox._AUTH_STORE_STAGING_LEAF == ts._AUTH_STORE_STAGING_LEAF, (
            "the sandbox masks one directory name and the publisher stages in another, "
            "so the in-flight signing key is visible in every agent namespace"
        )

    def test_the_fence_names_the_same_directory(self):
        from kiro_crew import security

        assert any(
            leaf == sandbox._AUTH_STORE_STAGING_LEAF for leaf in security.sensitive_home_dirs()
        ) or any(
            leaf.endswith("/" + sandbox._AUTH_STORE_STAGING_LEAF)
            for leaf in security.sensitive_home_dirs()
        ), (
            "the staging directory is masked but not fenced, so the agent file tools can "
            "still read a staged copy of the signing key"
        )

    def test_it_is_both_masked_and_precreated(self):
        assert sandbox._AUTH_STORE_STAGING_LEAF in sandbox._CREW_HIDDEN_LEAVES
        # Precreation is not cosmetic: a sandbox spawned before the first key write would
        # otherwise find the directory absent, the mask loop would skip it, and the
        # directory the gateway creates later would appear INSIDE that running namespace.
        assert sandbox._AUTH_STORE_STAGING_LEAF in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES


@_POSIX_ONLY
class TestTheLegacyAuthStoreTempSweep:
    """Pre-upgrade signing-key temps in the data-home root are removed on spawn.

    The bound is the interesting half. The md-notebook sweep may take every ``*.tmp`` in
    its own directory because nothing else writes there; the data home root is shared, and
    ``atomic_write`` stages ``tmp<random>.tmp`` in it for unrelated stores. So the control
    test below -- an unrelated temp SURVIVES -- is what proves this sweep cannot unlink
    another component's in-flight write between its ``mkstemp`` and its rename.

    POSIX-only for a reason that is a property of the code under test, not of the test.
    The sweep lists and unlinks through a PINNED DIRECTORY DESCRIPTOR -- ``os.listdir(fd)``
    and ``os.unlink(..., dir_fd=fd)`` from :func:`_open_dir_anchored` -- which is how it
    refuses to delete through a component swapped under it. Windows offers none of that
    family, so the descriptor open fails, the sweep skips every root and removes nothing.
    That is correct rather than broken: its only callers are the Linux namespace launcher
    and the macOS Seatbelt builder, and Windows has no sandbox launcher to call it. Running
    these assertions there would measure the absence of a code path instead of its
    behaviour, and the bound control would pass while discriminating nothing.
    """

    @pytest.fixture
    def isolated_home(self, crew_home, monkeypatch, tmp_path):
        """Keep the sweep's ``Path.home()`` arm inside the scratch tree.

        The sweep deliberately visits both crew-home spellings under ``$HOME`` as well as
        the live data home, so without this the test would reach the developer's real home.
        """
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        return crew_home

    def _orphan(self, home: Path) -> Path:
        return home / f"{sandbox._AUTH_STORE_LEGACY_TEMP_PREFIX}4242.deadbeefcafe0123.tmp"

    def test_it_removes_a_signing_key_staging_orphan(self, isolated_home):
        key = isolated_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"live" * 8)
        orphan = self._orphan(isolated_home)
        orphan.write_bytes(b"k" * 32)

        removed = sandbox._sweep_legacy_auth_store_temps()

        assert not orphan.exists(), (
            "a pre-upgrade staged copy of the signing key survived the sweep, so it stays "
            "readable in every agent namespace"
        )
        assert str(orphan) in removed
        assert key.exists(), "the live key was removed alongside the orphan"

    def test_an_orphan_is_KEPT_when_the_published_key_is_absent(self, isolated_home, caplog):
        """With no key at its own name, the orphan may be the inode's last name.

        It is also indistinguishable from a file created inside a sandbox, because an unused
        home is masked by nothing while it is absent. So the pass neither removes it -- that
        could destroy a key an operator can still recover by renaming -- nor refuses, which
        would let one ``touch`` stop every launch. It reports, and the message names both
        recoveries so the operator picks.
        """
        orphan = self._orphan(isolated_home)
        orphan.write_bytes(b"k" * 32)

        with caplog.at_level(logging.WARNING):
            removed = sandbox._sweep_legacy_auth_store_temps()

        assert orphan.exists(), (
            "the sweep removed the orphan while the key's own name was absent; that can "
            "destroy the only remaining name for the signing key's inode"
        )
        assert str(orphan) not in removed
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert orphan.name in messages, "the kept orphan was not reported at all"
        assert sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF in messages, (
            "the report must name the signing-key path, or the operator cannot tell which "
            "of the two recoveries it is offering"
        )

    def test_an_orphan_sharing_the_keys_inode_is_synced_before_removal(
        self, isolated_home, monkeypatch
    ):
        """A second name for the key inode gets the reconciler's fsync-first protocol.

        The pre-upgrade publisher kept exactly this name when its own directory sync failed,
        so the key's own entry may not have reached the device and this is the inode's one
        other reference.
        """
        key = isolated_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        orphan = self._orphan(isolated_home)
        os.link(key, orphan)

        synced: list[str] = []
        unlinked: list[str] = []
        real_unlink = os.unlink

        def _record_sync(path):
            synced.append(str(path))

        def _record_unlink(name, *args, **kwargs):
            unlinked.append(str(name))
            return real_unlink(name, *args, **kwargs)

        monkeypatch.setattr(sandbox, "fsync_dir", _record_sync)
        monkeypatch.setattr(os, "unlink", _record_unlink)

        sandbox._sweep_legacy_auth_store_temps()

        assert synced, "the key's own parent was never synced before its second name went"
        assert unlinked, "nothing was unlinked, so the ordering claim is untested"
        assert key.exists(), "the sweep removed the key's own name"

    def test_a_device_refused_sync_KEEPS_the_keys_second_name(self, isolated_home, monkeypatch):
        """Where the device refuses the sync, removing the second name can lose the inode."""
        key = isolated_home / sandbox._AUTH_STORE_PUBLISHED_KEY_LEAF
        key.write_bytes(b"k" * 32)
        orphan = self._orphan(isolated_home)
        os.link(key, orphan)

        def _refusing_sync(_path):
            raise OSError(errno.EIO, "device refused the directory sync")

        monkeypatch.setattr(sandbox, "fsync_dir", _refusing_sync)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._sweep_legacy_auth_store_temps()

        assert orphan.exists(), (
            "the second name was dropped even though the key's own entry could not be made "
            "durable; a crash then leaves the inode with no name at all"
        )
        assert "sync" in str(caught.value)

    def test_it_leaves_an_unrelated_atomic_write_temp_alone(self, isolated_home):
        # atomic_write's own shape for ANY other store in this shared directory.
        bystander = isolated_home / "tmpa1b2c3d4.tmp"
        bystander.write_bytes(b"another store's in-flight write")

        removed = sandbox._sweep_legacy_auth_store_temps()

        assert bystander.exists(), (
            "the sweep unlinked a temp belonging to another store; in the shared data "
            "home that aborts an unrelated in-flight write for no reason"
        )
        assert removed == []

    def test_it_leaves_the_published_key_alone(self, isolated_home):
        key = isolated_home / "token_signing.key"
        key.write_bytes(b"k" * 32)

        sandbox._sweep_legacy_auth_store_temps()

        assert key.exists(), "the sweep removed the live signing key"

    @_POSIX_ONLY
    def test_a_symlink_matching_the_shape_is_not_followed(self, isolated_home, tmp_path):
        target = tmp_path / "outside-the-home"
        target.write_bytes(b"not ours")
        link = self._orphan(isolated_home)
        link.symlink_to(target)

        removed = sandbox._sweep_legacy_auth_store_temps()

        assert target.exists(), "the sweep followed a link and deleted outside the home"
        assert removed == []
        assert link.is_symlink(), "the link itself was removed, which lstat should prevent"

    def test_the_prefix_matches_the_name_the_publisher_really_stages(self, tmp_path, monkeypatch):
        """Pin the sweep's pattern against the publisher's actual output, not a literal.

        A rename on either side otherwise leaves the sweep matching a shape nothing
        produces, which reads exactly like a clean home.
        """
        from kiro_crew.dashboard import token_secret as ts

        captured: list[str] = []
        real_link = os.link

        def _spy_link(src_path, dst_path, **kwargs):
            captured.append(os.path.basename(str(src_path)))
            return real_link(src_path, dst_path, **kwargs)

        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        monkeypatch.setattr(os, "link", _spy_link)
        ts._load_or_create_secret()

        assert captured, "the publish never linked, so no staged name was observed"
        staged_name = captured[0]
        assert staged_name.startswith(sandbox._AUTH_STORE_LEGACY_TEMP_PREFIX), (
            f"the publisher stages {staged_name!r}, which the sweep's prefix "
            f"{sandbox._AUTH_STORE_LEGACY_TEMP_PREFIX!r} does not match, so a pre-upgrade "
            "orphan of that shape would never be found"
        )
        assert staged_name.endswith(".tmp")

    def test_both_launch_paths_sweep(self):
        """Both launchers must call it: a Seatbelt profile names paths, never a temp shape.

        Asserted against the real sources, so a launcher added or reordered later cannot
        quietly drop the sweep on one platform.
        """
        for fn in (sandbox.namespace_argv, sandbox.sandbox_exec_argv):
            src = inspect.getsource(fn)
            assert "_sweep_legacy_auth_store_temps()" in src, (
                f"{fn.__name__} does not sweep legacy auth-store temps, so an orphan "
                "holding the signing key stays readable on that platform"
            )
