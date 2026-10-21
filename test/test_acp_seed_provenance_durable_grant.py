"""The durable grant behind a ``settings.local.json`` seed.

A sidecar entry is the grant Kiro Crew adopts a file on, so no write floor lets the agent
add one. A claim moves only once the write or the unlink it names has happened, and a
seed and its durable record commit or roll back together, a cancelled teardown included.
Shared fixtures and helpers live in ``acp_seed_provenance_helpers.py``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from acp_seed_provenance_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_seed_provenance_helpers import isolated_records  # noqa: F401
from acp_seed_provenance_helpers import (
    _OWNER,
    _SERVED,
    _client,
    _seed,
    _settings,
    _teardown,
    _the_owning_process_died,
    _unwritable_sidecar,
)

from kiro_crew import model_registry as mr
from kiro_crew import sandbox, security
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import seed_provenance as sp


def _attribute_calls(node: ast.AST, name: str) -> list[ast.Attribute]:
    """Every ``<something>.name`` attribute reference inside *node*.

    Attribute rather than Call so a reference through a decorator, an
    ``ensure_future`` or a ``to_thread`` counts too: the question these tests ask is
    which method a region of source reaches, not how it spells the invocation.
    """
    return [n for n in ast.walk(node) if isinstance(n, ast.Attribute) and n.attr == name]


class TestTheRecordIsOnEveryWriteFloor:
    """An entry in the sidecar IS the grant, so the agent must not be able to add one.

    The forgery chain the floors close, end to end: an agent writes
    ``{"seeds": {"<repo>/.claude/settings.local.json": {size, sha256}}}`` for a
    settings file the USER hand-wrote, the next gateway start loads it, the digest
    matches, and Crew's own trusted writer adopts the file -- replacing the user's
    ``permissions.defaultMode`` with this session's and unlinking the file on reset.
    The digest check is doing exactly what it was designed to do; what must not be
    forgeable is the record it checks against. Read stays allowed: the record holds
    work-dir paths and digests, not a secret.
    """

    DIR = "seed-provenance"
    LEAF = "settings_seeds.json"

    def test_the_sidecar_is_the_leaf_the_floors_name(self):
        # The floors are spelled as names, so they only hold if those are the names
        # the module actually publishes under and falls back to.
        assert sp._sidecar_path().name == self.LEAF
        assert sp._sidecar_path().parent.name == self.DIR == sp.SIDECAR_DIR_NAME

    @pytest.mark.parametrize("inside", ("seed-provenance", "seed-provenance/settings_seeds.json"))
    def test_the_file_edit_gate_refuses_a_write_and_allows_a_read(self, inside):
        for prefix in security.crew_home_prefixes():
            for leaf in (inside, self.LEAF):
                path = f"~/{prefix}/{leaf}"
                assert security.is_sensitive_write_path(path) is True, path
                assert security.is_sensitive_path(path) is False, path

    def test_the_sandbox_seals_it_readonly_even_when_absent(self):
        # The kernel floor under the deny rules, which a runtime-constructed spelling
        # (``$(printf ...)``) walks past. READONLY, not hidden -- the write is the
        # risk. Precreated because ``mount(2)`` cannot seal a path that is not there,
        # and this sidecar does not exist until a claude session has seeded a work
        # dir: on every install that has not, the name is writable. The directory is
        # what holds the record now; the old top-level file stays sealed because a
        # process started before the upgrade still reads it.
        assert self.DIR in sandbox._CREW_READONLY_LEAVES
        assert self.DIR in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
        assert self.DIR in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
        assert self.DIR not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
        assert self.LEAF in sandbox._CREW_READONLY_LEAVES
        assert self.LEAF in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        assert self.LEAF not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES

    def test_an_empty_materialized_ceiling_means_what_an_absent_one_means(
        self, tmp_path, monkeypatch
    ):
        """The precondition for precreating it: ``{}`` must read as "Crew owns nothing".

        A stale pinned read of the stub is the same answer, which is the direction
        that refuses -- the writer takes its leave-it-alone branch, so nothing is
        overwritten and nothing is unlinked.
        """
        sidecar = tmp_path / self.LEAF
        sidecar.write_bytes(sandbox._EMPTY_CEILING_DOCUMENT)
        monkeypatch.setattr(sp, "_sidecar_path", lambda: sidecar)

        sp._load()
        assert sp._RECORDS == {}
        assert sp.recorded(_settings(tmp_path), _OWNER) is None

    def test_an_empty_materialized_directory_means_no_records(self, tmp_path, monkeypatch):
        """The precreated empty directory reads as no records, as an absent one does."""
        monkeypatch.setattr(sp, "peek_data_home", lambda: tmp_path)
        (tmp_path / self.DIR).mkdir()

        assert sp._read_disk_seeds() == {}


class TestOwnershipTracksTheFilesystem:
    """A claim moves only when the write or the unlink actually happened.

    Ownership is a statement about bytes on disk, so every place it is recorded or
    dropped has to be ordered against the syscall that made it true. Both
    directions were wrong: the re-seed truncated the recorded bytes before writing
    the new ones (a failure mid-write left a file no session could ever claim
    again), and reset dropped the claim before knowing the unlink succeeded (a
    failure left Crew's own file behind as an unrecognizable orphan).
    """

    def test_a_failed_reseed_leaves_the_recorded_bytes_and_the_claim(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        first = _client(tmp_path, permission_mode="bypassPermissions")
        first._write_claude_local_settings()
        path = _settings(tmp_path)
        before = path.read_text(encoding="utf-8")
        _the_owning_process_died()

        def _boom(*args, **kwargs):
            raise OSError("ENOSPC")

        monkeypatch.setattr(acp_client, "atomic_write", _boom)
        second = _client(tmp_path, permission_mode="default")
        with pytest.raises(OSError):
            second._write_claude_local_settings()

        # Staging and renaming is what makes this hold: the old bytes are still the
        # bytes the record names, so the path is STILL adoptable. Truncating first
        # destroyed them before the new ones landed, leaving a file whose digest
        # matched nothing -- unclaimable by this session and by every later one.
        assert path.read_text(encoding="utf-8") == before
        assert sp.recorded(path, second._seed_owner) is not None
        assert second._claude_settings_is_still_ours() is True

        # And the instance flag did not move either, so this session's reset cannot
        # delete a file whose current bytes Crew never wrote.
        assert second._claude_settings_authored is False
        _teardown(second)
        assert path.read_text(encoding="utf-8") == before

    def test_the_path_is_still_adoptable_after_a_failed_reseed(self, tmp_path, monkeypatch):
        # The consequence of the above, stated as the user-visible outcome: the
        # ENOSPC session is a no-op, not a permanent loss of the work dir.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        _client(tmp_path, permission_mode="bypassPermissions")._write_claude_local_settings()
        _the_owning_process_died()

        real = acp_client.atomic_write
        disk_is_full = True

        def _boom(*args, **kwargs):
            if disk_is_full:
                raise OSError("ENOSPC")
            return real(*args, **kwargs)

        monkeypatch.setattr(acp_client, "atomic_write", _boom)
        with pytest.raises(OSError):
            _client(tmp_path, permission_mode="default")._write_claude_local_settings()
        _the_owning_process_died()

        disk_is_full = False
        third = _client(tmp_path, permission_mode="default")
        third._write_claude_local_settings()
        assert _seed(tmp_path)["permissions"]["defaultMode"] == "default"
        assert third._claude_settings_authored is True

    def test_a_failed_adoption_hands_the_claim_back_within_the_process(self, tmp_path, monkeypatch):
        """The claim has to be released, not just survive a process restart.

        ``claim`` is the race arbiter, so it is taken BEFORE the write -- it cannot
        wait for one to succeed without letting two clients both decide the same
        orphan is theirs. But a winner that then fails to write still holds the live
        slot, and every later client in this process therefore reads the orphan as a
        LIVE session's file: unadoptable, so the stale ``bypassPermissions`` in it
        can be neither rewritten nor removed for the lifetime of the gateway. Note
        there is no ``_the_owning_process_died()`` below -- that is the point.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        _client(tmp_path, permission_mode="bypassPermissions")._write_claude_local_settings()
        path = _settings(tmp_path)
        _the_owning_process_died()  # the seed is an orphan; the adopter is next

        real = acp_client.atomic_write
        disk_is_full = True

        def _boom(*args, **kwargs):
            if disk_is_full:
                raise OSError("ENOSPC")
            return real(*args, **kwargs)

        monkeypatch.setattr(acp_client, "atomic_write", _boom)
        failed = _client(tmp_path, permission_mode="default")
        with pytest.raises(OSError):
            failed._write_claude_local_settings()

        # Handed back: the record still describes the file (that is what keeps it
        # adoptable at all), and no live holder stands in the next client's way.
        assert sp.recorded(path, "a-sibling-in-this-process") is not None

        disk_is_full = False
        repaired = _client(tmp_path, permission_mode="default")
        repaired._write_claude_local_settings()
        assert _seed(tmp_path)["permissions"]["defaultMode"] == "default"

    def test_a_failed_adoption_does_not_evict_a_live_sibling(self, tmp_path, monkeypatch):
        """Only the winner may release. A loser's failure is not a lever on the slot."""
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        live = _client(tmp_path, permission_mode="bypassPermissions")
        live._write_claude_local_settings()
        path = _settings(tmp_path)

        # A sibling reaches the orphan check, loses the claim, and leaves it alone.
        loser = _client(tmp_path, permission_mode="default")
        loser._write_claude_local_settings()
        assert _seed(tmp_path)["permissions"]["defaultMode"] == "bypassPermissions"
        assert loser._claude_settings_authored is False
        assert sp.recorded(path, live._seed_owner) is not None
        assert sp.recorded(path, loser._seed_owner) is None

    def test_the_create_path_still_refuses_to_clobber_a_racing_sibling(self, tmp_path):
        # A rename REPLACES whatever sits at the name, so it cannot arbitrate a
        # create race at all. The create branch therefore keeps its O_EXCL open --
        # the loser must see the winner's file, not overwrite it.
        opened: list[int] = []
        real_open = os.open

        def _record_flags(path, flags, *rest):
            if str(path).endswith("settings.local.json"):
                opened.append(flags)
            return real_open(path, flags, *rest)

        client = _client(tmp_path, permission_mode="default")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "open", _record_flags)
            client._write_claude_local_settings()

        assert opened, "the create branch must open the path itself"
        assert opened[-1] & os.O_EXCL

    def test_a_failed_unlink_keeps_the_claim(self, tmp_path, monkeypatch):
        """The file is still there, so the claim on it is still true.

        Dropping it made Crew's own file unrecognizable to every later session --
        exactly the orphan this module exists to end, manufactured by the cleanup
        path. Keeping it means the next session re-seeds or removes it.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, permission_mode="bypassPermissions")
        client._write_claude_local_settings()
        path = _settings(tmp_path)
        seeded = path.read_bytes()
        seeded_ino = path.stat().st_ino
        # Teardown inode-pins the delete: it moves the file to a fresh ``*.crew-gc``
        # temp and deletes THAT, so a delete failure is a failure to remove the moved
        # inode. The refusal is pinned to that inode -- the fault modeled here is
        # "this file cannot be deleted", and the restore path must see the same
        # refusal when it tries to consume the moved aside.
        real_by_name = acp_client.pinned_fs.unlink_verified_by_name
        real_verified = acp_client.pinned_fs.unlink_verified

        def _refuse_by_name(parent, name, expect, *, on_error=None):
            if name.endswith(".crew-gc") and expect[1] == seeded_ino:
                if on_error is not None:
                    on_error(OSError("EACCES"))
                return False
            return real_by_name(parent, name, expect, on_error=on_error)

        def _refuse_verified(holder_fd, name, expect, *, on_error=None):
            if name.endswith(".crew-gc") and expect[1] == seeded_ino:
                if on_error is not None:
                    on_error(OSError("EACCES"))
                return False
            return real_verified(holder_fd, name, expect, on_error=on_error)

        monkeypatch.setattr(acp_client.pinned_fs, "unlink_verified_by_name", _refuse_by_name)
        monkeypatch.setattr(acp_client.pinned_fs, "unlink_verified", _refuse_verified)
        _teardown(client)

        # The moved inode could not be deleted, so it is restored under the
        # pathname and re-recorded. The restore is a rename of the moved entry,
        # so nothing is left beside the pathname for a later session to find.
        assert path.exists()
        assert path.read_bytes() == seeded
        assert not list(path.parent.glob("*.crew-gc"))
        _the_owning_process_died()
        assert sp.recorded(path, "a-later-session") is not None

        monkeypatch.setattr(acp_client.pinned_fs, "unlink_verified_by_name", real_by_name)
        monkeypatch.setattr(acp_client.pinned_fs, "unlink_verified", real_verified)
        later = _client(tmp_path, permission_mode="default")
        later._write_claude_local_settings()
        assert _seed(tmp_path)["permissions"]["defaultMode"] == "default"

    def test_a_successful_unlink_revokes_the_grant_for_good(self, tmp_path, monkeypatch):
        """The mirror of the above: the file is gone, so the grant must be too.

        Dropping it from memory alone left the SIDECAR naming a path Crew had just
        deleted, and re-verifying the digest does not neutralize that -- a file can
        hash to the recorded bytes again quite legitimately, most plainly when a user
        committed the generated seed and later restored it. The next process would
        then read that user's file as Crew's own: overwritten with this install's
        permission mode, and unlinked on reset.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, permission_mode="bypassPermissions")
        client._write_claude_local_settings()
        path = _settings(tmp_path)
        seeded = path.read_text(encoding="utf-8")
        _teardown(client)
        assert not path.exists()
        assert not list(path.parent.glob("*.crew-gc"))

        # A fresh process reads only what the sidecar kept.
        monkeypatch.setattr(sp, "_RECORDS", {})
        monkeypatch.setattr(sp, "_LIVE", {})
        sp._load()
        # The user restores the file they had committed -- byte for byte Crew's seed.
        path.write_text(seeded, encoding="utf-8")
        later = _client(tmp_path, permission_mode="default")
        later._write_claude_local_settings()

        assert path.read_text(encoding="utf-8") == seeded
        assert later._claude_settings_authored is False
        _teardown(later)
        assert path.exists()

    def test_the_revoke_lands_before_the_unlink(self, tmp_path, monkeypatch):
        """Ordering, not just presence: the grant dies BEFORE the file it described.

        Both steps can fail independently, so "revoke and unlink" is not enough --
        unlink-then-revoke leaves a window in which the file is gone while the
        sidecar still names it, and a crash inside that window is exactly the state
        :func:`forget` exists to prevent: the next process reloads the entry and
        adopts whatever appears at the path next.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, permission_mode="bypassPermissions")
        client._write_claude_local_settings()
        path = _settings(tmp_path)
        # The delete is inode-pinned: the file is moved to a fresh ``*.crew-gc`` temp
        # first and THAT is what gets unlinked, so the ordering is watched on the moved
        # inode. The temp name is randomized (mkstemp), so match on the suffix.

        order: list[str] = []
        real_forget = sp.forget
        real_unlink = acp_client.pinned_fs.unlink_verified_by_name

        def _watch_forget(*args, **kwargs):
            order.append("revoke")
            return real_forget(*args, **kwargs)

        def _watch_unlink(parent, name, expect, *, on_error=None):
            if name.endswith(".crew-gc"):
                order.append("unlink")
            return real_unlink(parent, name, expect, on_error=on_error)

        monkeypatch.setattr(sp, "forget", _watch_forget)
        monkeypatch.setattr(acp_client.pinned_fs, "unlink_verified_by_name", _watch_unlink)
        _teardown(client)

        assert order == ["revoke", "unlink"]
        assert not path.exists()
        assert not list(path.parent.glob("*.crew-gc"))

    def test_a_failed_revoke_keeps_the_file_and_the_record(self, tmp_path, monkeypatch):
        """A revoke that did not reach the disk must not authorize a deletion.

        The sidecar write can fail on its own (a full disk, a read-only data home).
        Unlinking anyway leaves the durable grant naming a path whose file is gone,
        which is the exact state that makes a user's restored copy adoptable. So the
        file stays, the record stays, and a later session repairs the orphan --
        strictly better than a deletion whose revocation never landed.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, permission_mode="bypassPermissions")
        client._write_claude_local_settings()
        path = _settings(tmp_path)
        seeded = path.read_text(encoding="utf-8")

        real = sp.atomic_write
        data_home_is_read_only = True

        def _boom(*args, **kwargs):
            if data_home_is_read_only:
                raise OSError("EROFS")
            return real(*args, **kwargs)

        monkeypatch.setattr(sp, "atomic_write", _boom)
        _teardown(client)

        assert path.read_text(encoding="utf-8") == seeded
        # In memory the record is back, so this process agrees with what a restart
        # would read off the un-rewritten sidecar.
        _the_owning_process_died()
        assert sp.recorded(path, "a-later-session") is not None

        data_home_is_read_only = False
        later = _client(tmp_path, permission_mode="default")
        later._write_claude_local_settings()
        assert _seed(tmp_path)["permissions"]["defaultMode"] == "default"

    def test_a_replacement_the_user_wrote_is_left_alone_on_teardown(self, tmp_path, monkeypatch):
        """Teardown re-checks the bytes, so an edited file is not this session's.

        The user may replace the seed while the session runs. Crew wrote the ORIGINAL
        bytes, so its ``_claude_settings_authored`` flag is set -- but the file on
        disk is now the user's, and deleting it is data loss.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, permission_mode="bypassPermissions")
        client._write_claude_local_settings()
        path = _settings(tmp_path)
        path.write_text('{"permissions": {"defaultMode": "acceptEdits"}}', encoding="utf-8")

        _teardown(client)

        assert json.loads(path.read_text(encoding="utf-8"))["permissions"] == {
            "defaultMode": "acceptEdits"
        }

    def test_the_teardown_never_touches_the_disk_on_the_event_loop(self, tmp_path, monkeypatch):
        """Both disk steps are off the loop, which is what makes the revoke durable.

        ``forget`` publishes the sidecar and takes :data:`_LOCK` across the write, so
        calling it inline would put a synchronous write -- and a wait on a lock a
        worker thread holds across one -- on the gateway's single event loop. The
        unlink is the same kind of call. Asserting on the running loop from inside
        each one is what pins that: a version that calls either directly fails here.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, permission_mode="bypassPermissions")
        client._write_claude_local_settings()
        path = _settings(tmp_path)

        on_loop: list[str] = []
        real_forget = sp.forget
        real_unlink = Path.unlink

        def _note(step: str) -> None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return  # a worker thread has no loop, which is the point
            on_loop.append(step)

        def _watch_forget(*args, **kwargs):
            _note("forget")
            return real_forget(*args, **kwargs)

        def _watch_unlink(self, *args, **kwargs):
            if self == path:
                _note("unlink")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(sp, "forget", _watch_forget)
        monkeypatch.setattr(Path, "unlink", _watch_unlink)
        _teardown(client)

        assert on_loop == []
        assert not path.exists()


class TestTheGrantIsATransaction:
    """Neither half of a provenance mutation may land without the other.

    A grant is only real once it is on disk, so both directions have to commit or
    roll back together: a seed with no durable record is a permission mode nothing
    can clean up, and a revoked record with the file still there is a grant that
    outlives what it described. These pin the two cases where the pairing can
    come apart -- a sidecar publish that fails, and a teardown that is cancelled.
    """

    def test_a_seed_whose_grant_is_not_durable_is_withdrawn(self, tmp_path, monkeypatch):
        """No record, no seed. The write is undone rather than left unowned.

        Ownership IS the record, so a settings file written while the sidecar cannot
        be published is the one state nothing on the host can repair: this session
        would still remove it, but a kill before teardown leaves a
        ``permissions.defaultMode`` the user never approved behind a file no later
        session is permitted to re-seed or remove. Withdrawing it costs this session
        the allowlist and the deny rules -- exactly what a cold advertised-model
        cache already costs -- which is the strictly smaller harm.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        monkeypatch.setattr(sp, "atomic_write", _unwritable_sidecar)
        client = _client(tmp_path, permission_mode="bypassPermissions")
        client._write_claude_local_settings()

        assert not _settings(tmp_path).exists()
        # No instance claim either, so teardown has nothing to act on and a
        # replacement client in this process starts from a clean path.
        assert client._claude_settings_authored is False
        _the_owning_process_died()
        assert sp.recorded(_settings(tmp_path), "a-later-session") is None

    def test_a_failed_record_rolls_the_memory_back_to_the_sidecar(self, tmp_path):
        """A refused publish leaves this process reading what a restart would read.

        The rollback restores the DISPLACED entry rather than dropping the key: the
        sidecar on disk still names the previous digest, and on a re-seed the bytes
        that digest describes may still be the ones on disk, because ``atomic_write``
        publishes by rename and so leaves the old file intact when it fails.
        """
        path = tmp_path / "settings.local.json"
        assert sp.record(path, "first", _OWNER) is True

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sp, "atomic_write", _unwritable_sidecar)
            assert sp.record(path, "second", _OWNER) is False

        assert sp.recorded(path, _OWNER) == (len("first"), sp.digest("first"))
        # And the live slot is the one the failed call found, not the one it took:
        # an adopter arrives here already holding it, so a rollback that popped it
        # would hand a live path to a sibling.
        assert sp._LIVE.get(str(path)) == _OWNER

    def test_a_cancelled_teardown_still_settles_the_seed(self, tmp_path, monkeypatch):
        """Cancellation cannot land between the revoke and the unlink.

        Teardown runs on paths that are themselves being cancelled -- a turn cancel, a
        session close, a shutdown. As a sequence of awaited steps this had a
        suspension point between the ownership check, the revoke and the unlink, and a
        cancellation on any of them cleared the flags with the file still on disk and
        its grant already gone: unrecoverable. As one shielded thread the transaction
        has either not started or run to completion, which is what this asserts.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, permission_mode="bypassPermissions")
        client._write_claude_local_settings()
        path = _settings(tmp_path)
        assert path.exists()

        entered = threading.Event()
        real_settle = client._settle_claude_settings_seed

        def _slow_settle(*args, **kwargs):
            entered.set()
            # Long enough that the cancel below lands while this is mid-transaction,
            # which is the window the awaited-sequence version could not survive.
            # flake-ok: 200 ms stub delay keeps the settle mid-transaction; moved verbatim
            # nosemgrep: kirocrew.test-sleep-as-barrier -- slow-work stub, the test waits on entered
            time.sleep(0.2)
            return real_settle(*args, **kwargs)

        monkeypatch.setattr(client, "_settle_claude_settings_seed", _slow_settle)

        async def _cancel_mid_teardown() -> None:
            task = asyncio.ensure_future(client._discard_claude_settings_seed())
            await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                # flake-ok: awaits the task just cancelled; the cancellation ends it; moved verbatim
                await task

        # ``asyncio.run`` shuts the default executor down on the way out, so by the
        # time it returns the shielded thread has finished -- no polling needed.
        asyncio.run(_cancel_mid_teardown())

        assert not path.exists()
        _the_owning_process_died()
        assert sp.recorded(path, "a-later-session") is None

    def test_sharer_discard_under_cancellation_leaves_no_phantom(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert sibling._claude_settings_shared is True

        persist_entered = threading.Event()
        finish_persist = threading.Event()

        def _parked_refusal(**_kwargs):
            persist_entered.set()
            assert finish_persist.wait(5)
            return False

        monkeypatch.setattr(sp, "_persist", _parked_refusal)

        async def _cancel_while_persisting() -> None:
            task = asyncio.ensure_future(sibling._discard_claude_settings_seed())
            assert await asyncio.to_thread(persist_entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                # flake-ok: awaits the task just cancelled; the cancellation ends it; moved verbatim
                await task
            sibling._reset_state()
            finish_persist.set()

        asyncio.run(_cancel_while_persisting())

        assert not sp._SHARERS.get(os.fspath(path))

    def test_every_discard_call_site_resets_in_a_finally(self):
        """The in-memory reset must survive a cancelled discard, at every call site.

        The mirror image of the test above, and it is a source-shape assertion for the
        same reason the loop-bound-locks and to_thread gates are: the defect is a
        MISSING ``finally``, so no runtime path exercises it. Before the discard was
        async, ``_reset_state`` was a plain synchronous statement that always ran;
        awaiting something in front of it means a cancellation on that await skips the
        PID untracking and the pipe closes entirely. Any call site that reaches both
        must therefore pair them.
        """
        tree = ast.Module(
            body=[
                stmt
                for name in (acp_client.__name__, *acp_client._CLIENT_OWNERS)
                for stmt in ast.parse(
                    Path(sys.modules[name].__file__).read_text(encoding="utf-8")
                ).body
            ],
            type_ignores=[],
        )

        paired: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not any(_attribute_calls(stmt, "_reset_state") for stmt in node.finalbody):
                continue
            for stmt in node.body:
                for call in _attribute_calls(stmt, "_discard_claude_settings_seed"):
                    paired.add(id(call))

        unpaired: list[str] = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _attribute_calls(fn, "_reset_state"):
                continue  # nothing to pair with in this function
            unpaired += [
                f"{fn.name}:{call.lineno}"
                for call in _attribute_calls(fn, "_discard_claude_settings_seed")
                if id(call) not in paired
            ]

        assert unpaired == [], (
            "await _discard_claude_settings_seed() must sit in the `try` of a "
            "try/finally whose `finally` calls _reset_state(), or a cancelled "
            f"teardown skips the reset entirely: {unpaired}"
        )

    def test_claim_pathname_moves_ours_aside_and_leaves_a_stranger(self, tmp_path):
        """The inode-pin primitive: ours is captured, a stranger is left untouched.

        ``_claim_pathname_if_ours`` is what closes the TOCTOU between an ownership
        check and the delete/overwrite that acts on it -- it moves the pathname's
        current content aside in one atomic step and hands it back ONLY when the moved
        inode is still Crew's. A stranger's file is restored exactly as found, never
        deleted or clobbered.
        """
        path = tmp_path / "settings.local.json"
        path.write_text("crew-bytes", encoding="utf-8")
        expectation = (len(b"crew-bytes"), sp.digest("crew-bytes"))

        claimed = acp_client.AcpClient._claim_pathname_if_ours(path, expectation)
        assert claimed is not None
        aside, moved_ident = claimed
        assert aside.read_text(encoding="utf-8") == "crew-bytes"
        moved = os.lstat(aside)
        assert moved_ident == (moved.st_dev, moved.st_ino)
        assert not path.exists()  # the pathname is now free

        # A file that is NOT Crew's is left exactly in place, not moved or removed.
        path.write_text("USER-OWNED", encoding="utf-8")
        assert acp_client.AcpClient._claim_pathname_if_ours(path, expectation) is None
        assert path.read_text(encoding="utf-8") == "USER-OWNED"

    def test_a_replacement_that_races_the_teardown_delete_survives(self, tmp_path, monkeypatch):
        """A user save landing after the ownership check but before the delete is kept.

        The delete is inode-pinned: teardown moves Crew's file to ``<name>.crew-gc`` in
        one atomic step and deletes THAT, so a replacement written at the pathname
        afterwards is a different inode this never touches. The race is made
        deterministic by writing the user's file at the freed pathname the instant
        Crew's file is moved aside -- the exact window the inode-pin closes. A teardown
        that unlinked the pathname would delete the user's file here.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, permission_mode="bypassPermissions")
        client._write_claude_local_settings()
        path = _settings(tmp_path)

        real_replace = os.replace

        def _race(src, dst, *args, **kwargs):
            real_replace(src, dst, *args, **kwargs)
            # Only the move-aside frees the pathname; the sidecar's own renames must
            # not trip this. The user saves their settings the instant it is free.
            if str(dst).endswith(".crew-gc"):
                Path(path).write_text('{"user": true}\n', encoding="utf-8")

        monkeypatch.setattr(os, "replace", _race)
        _teardown(client)

        # Crew removed only its own moved inode; the racing replacement is intact.
        assert path.exists()
        assert path.read_text(encoding="utf-8") == '{"user": true}\n'

    def test_a_project_file_at_the_move_aside_name_is_not_clobbered(self, tmp_path, monkeypatch):
        """A file already at the fixed ``.crew-gc`` name must survive the capture.

        The move-aside destination must not be a FIXED sibling (``<name>.crew-gc``),
        which is itself a pathname a project can own -- and ``os.replace`` onto it
        clobbers it atomically, relocating the very data loss the inode-pin exists to
        prevent. The capture now lands on a fresh ``mkstemp`` name that provably did
        not pre-exist, so a project's own ``<name>.crew-gc`` is left untouched. Against
        a fixed-name capture this file would be destroyed by the teardown.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, permission_mode="bypassPermissions")
        client._write_claude_local_settings()
        path = _settings(tmp_path)
        squatter = path.with_name(path.name + ".crew-gc")
        squatter.write_text("PROJECT-OWNED", encoding="utf-8")

        _teardown(client)

        # Crew deleted only its own freshly-named temp; the project's file is intact.
        assert squatter.exists()
        assert squatter.read_text(encoding="utf-8") == "PROJECT-OWNED"
        assert not path.exists()
