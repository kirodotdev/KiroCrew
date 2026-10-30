"""Cross-session ownership of the ``settings.local.json`` seed, and the model
resolution that depends on it.

Three defects compounded into one user-visible symptom -- a claude session pinned
to the 200K window even when the account is served a 1M one -- and they only make
sense together:

1. The seed's ``availableModels`` fell back to the hand-maintained static model
   registry when the advertised-model cache was cold. The adapter merges
   ``availableModels`` union+dedup, so a registry list that has not caught up
   REPLACES the adapter's correct provider-derived list with one carrying no
   ``[1m]`` id for the model actually picked.
2. The seed ran before ``session/new`` (it must: ``permissions`` has to be on disk
   first) and nothing re-seeded after the capture that warms the cache. So the
   cold-cache seed was the FINAL state of the file, and the startup ``set_model``
   folded against a cache that was still cold.
3. Ownership was proven from per-instance memory only, so the file left behind by
   session 1 read as a stranger's file to session 2 -- which meant the
   leave-it-alone branch, forever. Nothing could repair a work_dir once seeded.

Defect 3 is why the first two could not be fixed on their own: without a
cross-session ownership credential, no later session is ever allowed to write.
The credential is provenance, not permission -- a record of the bytes Crew wrote,
where adoption additionally requires the file to still hash to them.

This module pins the sidecar record, cross-session adoption and the post-capture model
resolution. The shared permission surface lives in
``test_acp_seed_provenance_sibling_sharing.py`` and
``test_acp_seed_provenance_sharer_teardown.py``; the write floors, the claim that tracks
the filesystem and the transactional grant live in
``test_acp_seed_provenance_durable_grant.py``. Fixtures and helpers shared across these
modules live in ``acp_seed_provenance_helpers.py``; this module re-exports the four that
``test_cc_permission_mode_seed.py`` imports from it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest
from acp_seed_provenance_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_seed_provenance_helpers import isolated_records  # noqa: F401
from acp_seed_provenance_helpers import (
    _OWN_START_ID,
    _OWNER,
    _SERVED,
    _client,
    _fp,
    _pin_holder_identities,
    _seed,
    _settings,
    _teardown,
    _the_owning_process_died,
    _write_durable_holder,
)

from kiro_crew import model_registry as mr
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import seed_provenance as sp
from kiro_crew.acp.client import AcpClient


class TestProvenanceRecord:
    """The sidecar itself: what it records, what it refuses to claim."""

    def test_record_then_recognize(self, tmp_path):
        path = tmp_path / "settings.local.json"
        sp.record(path, '{"a": 1}\n', _OWNER)
        assert sp.recorded(path, _OWNER) == (len('{"a": 1}\n'), sp.digest('{"a": 1}\n'))

    def test_record_refuses_a_live_foreign_owner(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.local.json"
        path.write_text("foreign-bytes", encoding="utf-8")
        foreign = _pin_holder_identities(monkeypatch, foreign_live=True)
        _write_durable_holder(
            path,
            "foreign-bytes",
            kind=sp._HOLDER_OWNERS,
            owner="foreign-owner",
            identity=foreign,
        )
        before = sp._read_disk_seeds()[os.fspath(path)]

        assert sp.record(path, "our-bytes", _OWNER) is False

        assert sp._read_disk_seeds()[os.fspath(path)] == before
        assert sp._LIVE.get(os.fspath(path)) is None

    def test_record_reclaims_a_dead_owner_lease(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.local.json"
        path.write_text("foreign-bytes", encoding="utf-8")
        foreign = _pin_holder_identities(monkeypatch, foreign_live=False)
        _write_durable_holder(
            path,
            "foreign-bytes",
            kind=sp._HOLDER_OWNERS,
            owner="dead-owner",
            identity=foreign,
        )

        assert sp.record(path, "our-bytes", _OWNER) is True

        entry = sp._read_disk_seeds()[os.fspath(path)]
        assert (entry["size"], entry["sha256"]) == _fp("our-bytes")
        assert set(entry["holders"][sp._HOLDER_OWNERS]) == {_OWNER}

    def test_record_recreates_under_a_live_foreign_sharer(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.local.json"
        path.write_text("shared-bytes", encoding="utf-8")
        foreign = _pin_holder_identities(monkeypatch, foreign_live=True)
        _write_durable_holder(
            path,
            "shared-bytes",
            kind=sp._HOLDER_SHARERS,
            owner="foreign-reader",
            identity=foreign,
        )

        assert sp.record(path, "shared-bytes", _OWNER) is True

        holders = sp._read_disk_seeds()[os.fspath(path)]["holders"]
        assert set(holders[sp._HOLDER_OWNERS]) == {_OWNER}
        assert set(holders[sp._HOLDER_SHARERS]) == {"foreign-reader"}

    def test_record_promotes_this_process_reader_lease(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.local.json"
        path.write_text("shared-bytes", encoding="utf-8")
        _pin_holder_identities(monkeypatch, foreign_live=False)
        identity = {"pid": os.getpid(), "start_id": _OWN_START_ID}
        _write_durable_holder(
            path,
            "shared-bytes",
            kind=sp._HOLDER_SHARERS,
            owner=_OWNER,
            identity=identity,
        )
        sp._SHARERS[os.fspath(path)] = {_OWNER}

        assert sp.record(path, "shared-bytes", _OWNER) is True

        holders = sp._read_disk_seeds()[os.fspath(path)]["holders"]
        assert set(holders[sp._HOLDER_OWNERS]) == {_OWNER}
        assert _OWNER not in holders[sp._HOLDER_SHARERS]
        assert _OWNER not in sp._SHARERS[os.fspath(path)]

    def test_an_unrecorded_path_is_unowned(self, tmp_path):
        assert sp.recorded(tmp_path / "settings.local.json", _OWNER) is None

    def test_forget_drops_the_claim(self, tmp_path):
        path = tmp_path / "settings.local.json"
        sp.record(path, "x", _OWNER)
        sp.forget(path, _OWNER)
        assert sp.recorded(path, _OWNER) is None

    def test_forget_survives_the_process(self, tmp_path, monkeypatch):
        """A revoked grant has to die with the file, not just leave memory.

        ``forget`` follows a successful unlink, so the file it described is gone.
        Dropping the entry from memory alone leaves the SIDECAR still naming that
        path -- and the digest check does not neutralize it, because a file can
        legitimately hash to the recorded bytes again: a user who committed the
        generated seed and later restored it holds exactly those bytes. The next
        process would then read that user file as Crew's own, overwrite it with this
        install's ``permissions.defaultMode``, and unlink it on reset.
        """
        path = tmp_path / "settings.local.json"
        sp.record(path, "payload", _OWNER)
        sp.forget(path, _OWNER)

        # A fresh process: nothing in memory, everything from the sidecar.
        monkeypatch.setattr(sp, "_RECORDS", {})
        monkeypatch.setattr(sp, "_LIVE", {})
        sp._load()
        assert sp.recorded(path, "a-later-session") is None

        # ...and the file being restored byte-for-byte does not resurrect the grant.
        path.write_text("payload", encoding="utf-8")
        assert sp.recorded(path, "a-later-session") is None

    def test_release_hands_back_the_slot_without_disowning_the_record(self, tmp_path):
        """``release`` is for a claim whose write did not land.

        Only the live slot goes back. Dropping the RECORD too would be the very harm
        being avoided rather than a milder version of it: the orphan on disk would
        become unadoptable by every later session, so a stale
        ``permissions.defaultMode`` in it could never be repaired or removed.
        """
        path = tmp_path / "settings.local.json"
        sp.record(path, "payload", _OWNER)
        assert sp.recorded(path, "a-later-session") is None  # live, so not adoptable
        sp.release(path, _OWNER)
        # Adoptable again, and still described by its recorded bytes.
        assert sp.recorded(path, "a-later-session") == (len("payload"), sp.digest("payload"))

    def test_loop_safe_release_and_unshare_never_lock_or_persist(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.local.json"
        key = sp._key(path)
        sp._LIVE[key] = _OWNER
        sp._SHARERS[key] = {_OWNER, "another-reader"}

        class _RefusingLock:
            def __enter__(self):
                pytest.fail("loop-safe teardown must not take the persistence lock")

            def __exit__(self, *_args):
                return False

        def _refuse_persist(*_args, **_kwargs):
            pytest.fail("loop-safe teardown must not persist")

        monkeypatch.setattr(sp, "_LOCK", _RefusingLock())
        monkeypatch.setattr(sp, "_persist", _refuse_persist)

        sp.release_local(path, _OWNER)
        sp.unshare_local(path, _OWNER)

        assert key not in sp._LIVE
        assert sp._SHARERS[key] == {"another-reader"}

    def test_unshare_never_readds_the_in_memory_lease_on_a_refused_persist(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        key = sp._key(path)
        assert sp.record(path, "payload", "writer") is True
        assert sp.share(path, "payload", _OWNER) is True
        monkeypatch.setattr(sp, "_persist", lambda **_kwargs: False)

        assert sp.unshare(path, _OWNER) is False
        assert _OWNER not in sp._SHARERS[key]
        persisted = sp._read_disk_seeds()[key]["holders"][sp._HOLDER_SHARERS]
        assert _OWNER in persisted

    def test_release_never_readds_the_live_slot_on_a_refused_persist(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        key = sp._key(path)
        assert sp.record(path, "payload", _OWNER) is True
        monkeypatch.setattr(sp, "_persist", lambda **_kwargs: False)

        assert sp.release(path, _OWNER) is False
        assert key not in sp._LIVE
        persisted = sp._read_disk_seeds()[key]["holders"][sp._HOLDER_OWNERS]
        assert _OWNER in persisted

    def test_forget_drops_the_live_slot_but_keeps_the_record_on_a_refused_persist(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        key = sp._key(path)
        assert sp.record(path, "payload", _OWNER) is True
        entry = sp._RECORDS[key]
        monkeypatch.setattr(sp, "_persist", lambda **_kwargs: False)

        assert sp.forget(path, _OWNER) is False
        assert sp._RECORDS[key] is entry
        assert key not in sp._LIVE

    def test_forget_proves_disk_agreement_when_the_local_record_is_absent(self, tmp_path):
        """A local cache miss cannot revoke a record protected by a durable sharer."""
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        assert sp.record(path, "payload", _OWNER) is True
        assert sp.release(path, _OWNER) is True
        assert sp.share(path, "payload", "cross-process-reader") is True
        key = sp._key(path)

        sp._RECORDS.clear()
        sp._LIVE.clear()
        sp._SHARERS.clear()

        assert sp.forget(path, _OWNER) is False
        assert key not in sp._RECORDS
        durable = sp._read_disk_seeds()[key]
        assert "cross-process-reader" in durable["holders"]["sharers"]

    @pytest.mark.asyncio
    async def test_windows_dead_process_withdraws_the_durable_holder_off_loop(
        self, tmp_path, monkeypatch
    ):
        client = _client(tmp_path, permission_mode="default")
        await asyncio.to_thread(client._write_claude_local_settings)
        path = _settings(tmp_path)
        key = sp._key(path)
        assert key in sp._read_disk_seeds()

        class _Process:
            def __init__(self, returncode):
                self.returncode = returncode
                self.stdin = None
                self.stdout = None
                self.stderr = None

        client._process = _Process(1)  # type: ignore[assignment]
        client._pid = None
        client._child_pids = {}
        loop_thread = threading.get_ident()
        forget_threads: list[int] = []
        real_forget = sp.forget

        def _watch_forget(*args, **kwargs):
            forget_threads.append(threading.get_ident())
            return real_forget(*args, **kwargs)

        async def _kill_process(*, force):
            assert force is True

        async def _spawn():
            client._process = _Process(None)  # type: ignore[assignment]

        async def _initialize_session():
            client._session_id = "replacement-session"

        monkeypatch.setattr(sp, "forget", _watch_forget)
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(client, "_kill_process", _kill_process)
        monkeypatch.setattr(client, "_spawn", _spawn)
        monkeypatch.setattr(client, "_initialize_session", _initialize_session)
        monkeypatch.setattr(client, "_snapshot_process_tree", lambda: asyncio.sleep(0))

        await client.ensure_ready()

        assert forget_threads and all(thread != loop_thread for thread in forget_threads)
        assert key not in sp._read_disk_seeds()
        assert key not in sp._LIVE
        assert not path.exists()

    def test_release_cannot_evict_the_winner(self, tmp_path):
        path = tmp_path / "settings.local.json"
        assert sp.claim(path, "winner", expect_digest=None) is True
        sp.release(path, "loser")
        assert sp.claim(path, "someone-else", expect_digest=None) is False

    def test_a_live_owners_record_is_invisible_to_a_sibling(self, tmp_path):
        """A record proves "Crew wrote it", not "any Crew client may take it".

        Adoption exists for an ORPHAN. While its owner is still seeding the path,
        the record describes a LIVE file, and a sibling that read it as its own
        would overwrite that session's permission mode.
        """
        path = tmp_path / "settings.local.json"
        sp.record(path, "x", _OWNER)
        assert sp.recorded(path, "some-other-owner") is None
        assert sp.recorded(path, _OWNER) is not None

    def test_a_sibling_cannot_revoke_a_live_claim(self, tmp_path):
        path = tmp_path / "settings.local.json"
        sp.record(path, "x", _OWNER)
        sp.forget(path, "some-other-owner")
        assert sp.recorded(path, _OWNER) is not None

    def test_the_record_becomes_adoptable_once_its_owner_lets_go(self, tmp_path):
        """The live claim is a lease on THIS process, not a permanent lock.

        Once the owner resets (or a new process loads the sidecar, where nothing
        is live by construction), the record is an orphan again and adoptable.
        """
        path = tmp_path / "settings.local.json"
        sp.record(path, "x", _OWNER)
        sp._LIVE.pop(sp._key(path))  # owner released the path; record survives
        assert sp.recorded(path, "some-other-owner") is not None

    def test_only_one_adopter_can_claim_an_orphan(self, tmp_path):
        """Two clients can READ the same orphan as adoptable; only one may take it.

        Ownership is read at a moment, so a check alone cannot arbitrate between
        siblings that start together -- both would then re-seed with their own
        ``permissions.defaultMode`` and one session would run under the other's.
        The claim is what decides, and it is a single atomic dict operation.
        """
        path = tmp_path / "settings.local.json"
        assert sp.recorded(path, "first") is None  # an orphan nobody holds yet
        assert sp.claim(path, "first", expect_digest=None) is True
        assert sp.claim(path, "second", expect_digest=None) is False
        # ...and the winner may re-claim its own slot, so re-seeding is not a
        # self-refusal.
        assert sp.claim(path, "first", expect_digest=None) is True
        assert sp.recorded(path, "second") is None

    def test_adoption_is_refused_while_a_persisted_reader_is_live(self, tmp_path):
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        assert sp.record(path, "payload", _OWNER) is True
        sp.release(path, _OWNER)
        assert sp.share(path, "payload", "reader") is True

        sp._RECORDS.clear()
        sp._LIVE.clear()
        sp._SHARERS.clear()
        sp._load()

        assert sp.claim(path, "second-process", expect_digest=_fp("payload")) is False

    def test_a_stale_persisted_reader_is_reclaimable(self, tmp_path):
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        assert sp.record(path, "payload", _OWNER) is True
        sp.release(path, _OWNER)
        assert sp.share(path, "payload", "reader") is True

        sidecar = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))
        holder = sidecar["seeds"][os.fspath(path)]["holders"]["sharers"]["reader"]
        holder["start_id"] = "a-different-process-incarnation"
        sp._sidecar_path().write_text(json.dumps(sidecar), encoding="utf-8")
        sp._RECORDS.clear()
        sp._LIVE.clear()
        sp._SHARERS.clear()
        sp._load()

        assert sp.claim(path, "second-process", expect_digest=_fp("payload")) is True

    def test_identity_unprovable_owner_persists_digest_only_but_sibling_declines_sharing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda _pid: None)

        owner = _client(tmp_path, permission_mode="default")
        owner._write_claude_local_settings()
        path = _settings(tmp_path)
        entry = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))["seeds"][os.fspath(path)]

        assert owner._claude_settings_authored is True
        assert entry["holders"] == {"owners": {}, "sharers": {}}

        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()

        assert sibling._claude_settings_shared is False
        assert sibling._permission_surface_governed is False
        assert sibling._seed_owner not in sp._SHARERS.get(os.fspath(path), set())

    def test_share_declines_when_identity_is_unprovable(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        assert sp.record(path, "payload", _OWNER) is True
        assert sp.release(path, _OWNER) is True
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda _pid: None)

        assert sp.share(path, "payload", "reader") is False
        assert "reader" not in sp._SHARERS.get(os.fspath(path), set())

    def test_record_refuses_a_digest_change_under_a_live_foreign_sharer(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "settings.local.json"
        original = '{"permissions": {"defaultMode": "default"}}\n'
        replacement = '{"permissions": {"defaultMode": "acceptEdits"}}\n'
        path.write_text(original, encoding="utf-8")
        foreign = _pin_holder_identities(monkeypatch, foreign_live=True)
        _write_durable_holder(
            path,
            original,
            kind=sp._HOLDER_SHARERS,
            owner="reader",
            identity=foreign,
        )
        path.write_text(replacement, encoding="utf-8")

        assert sp.record(path, replacement, "replacement-owner") is False

        entry = sp._read_disk_seeds()[os.fspath(path)]
        assert (entry["size"], entry["sha256"]) == _fp(original)
        assert set(entry["holders"][sp._HOLDER_SHARERS]) == {"reader"}

    def test_record_allows_a_same_digest_write_under_a_live_sharer(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.local.json"
        payload = '{"permissions": {"defaultMode": "default"}}\n'
        path.write_text(payload, encoding="utf-8")
        foreign = _pin_holder_identities(monkeypatch, foreign_live=True)
        _write_durable_holder(
            path,
            payload,
            kind=sp._HOLDER_SHARERS,
            owner="reader",
            identity=foreign,
        )

        assert sp.record(path, payload, "reader") is True

        entry = sp._read_disk_seeds()[os.fspath(path)]
        assert (entry["size"], entry["sha256"]) == _fp(payload)
        assert set(entry["holders"][sp._HOLDER_OWNERS]) == {"reader"}
        assert entry["holders"][sp._HOLDER_SHARERS] == {}

    def test_record_allows_a_digest_change_when_no_sharer(self, tmp_path):
        path = tmp_path / "settings.local.json"
        path.write_text("original", encoding="utf-8")
        assert sp.record(path, "original", _OWNER) is True
        assert sp.release(path, _OWNER) is True
        path.write_text("replacement", encoding="utf-8")

        assert sp.record(path, "replacement", "replacement-owner") is True
        assert sp.recorded(path, "replacement-owner") == _fp("replacement")

    def test_release_clears_the_persisted_reader(self, tmp_path):
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        assert sp.record(path, "payload", _OWNER) is True
        sp.release(path, _OWNER)
        assert sp.share(path, "payload", "reader") is True
        sp.unshare(path, "reader")

        sp._RECORDS.clear()
        sp._LIVE.clear()
        sp._SHARERS.clear()
        sp._load()

        assert sp.claim(path, "second-process", expect_digest=_fp("payload")) is True

    def test_a_claim_with_a_stale_cached_digest_is_refused(self, tmp_path):
        """A stale in-memory digest cannot authorize adopting a user's file.

        The reviewer's interleaving: this process caches the digest of Crew's
        old seed; ANOTHER process records new bytes, so the durable entry moves
        on; the user then restores the old bytes at the path. The cached digest
        still matches the file, but the record stopped vouching for those bytes
        -- an adoption here would rewrite, and its teardown later delete, the
        user's restoration. The claim revalidates the digest against the
        durable entry under the cross-process lock and refuses.
        """
        path = tmp_path / "settings.local.json"
        path.write_text("old-bytes", encoding="utf-8")
        assert sp.record(path, "old-bytes", _OWNER) is True
        sp.release(path, _OWNER)
        stale = _fp("old-bytes")

        # Another process records new bytes: only the durable sidecar sees it;
        # this process's in-memory cache stays where it was.
        sidecar = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))
        entry = sidecar["seeds"][os.fspath(path)]
        entry["size"], entry["sha256"] = _fp("new-bytes")
        sp._sidecar_path().write_text(json.dumps(sidecar), encoding="utf-8")

        # The user restores the old bytes at the path.
        path.write_text("old-bytes", encoding="utf-8")

        # The stale-digest claim refuses, leaves no live slot behind, and the
        # user's file is untouched.
        assert sp.claim(path, "adopter", expect_digest=stale) is False
        assert sp._LIVE.get(os.fspath(path)) is None
        assert path.read_text(encoding="utf-8") == "old-bytes"

    def test_a_claim_matching_the_durable_entry_still_lands(self, tmp_path):
        """The digest gate refuses staleness, not adoption itself."""
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        assert sp.record(path, "payload", _OWNER) is True
        sp.release(path, _OWNER)
        assert sp.claim(path, "adopter", expect_digest=_fp("payload")) is True

    def test_a_cross_process_revoke_is_not_resurrected_from_local_memory(self, tmp_path):
        """A revoked durable record cannot vouch for a claim or a share.

        Another process forgets the record, so the sidecar entry is gone;
        this process's in-memory cache still holds it. The digest requirement
        is a claim about the DURABLE record -- both the adoption claim and a
        new share must refuse, rather than resurrect provenance from local
        memory that nothing durable stands behind.
        """
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        assert sp.record(path, "payload", _OWNER) is True
        sp.release(path, _OWNER)

        # Another process revokes: the durable entry vanishes; this process's
        # cache keeps the stale copy.
        sidecar = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))
        del sidecar["seeds"][os.fspath(path)]
        sp._sidecar_path().write_text(json.dumps(sidecar), encoding="utf-8")
        assert sp._RECORDS.get(os.fspath(path))

        assert sp.claim(path, "adopter", expect_digest=_fp("payload")) is False
        assert sp.share(path, "payload", "reader") is False

    def test_a_persisted_reader_exempts_a_missing_file_from_prune(self, tmp_path):
        path = tmp_path / "settings.local.json"
        path.write_text("payload", encoding="utf-8")
        assert sp.record(path, "payload", _OWNER) is True
        sp.release(path, _OWNER)
        assert sp.share(path, "payload", "reader") is True

        sp._RECORDS.clear()
        sp._LIVE.clear()
        sp._SHARERS.clear()
        sp._load()
        path.unlink()
        other = tmp_path / "other.json"
        other.write_text("other", encoding="utf-8")
        assert sp.record(other, "other", "other-owner") is True

        persisted = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))["seeds"]
        assert os.fspath(path) in persisted

    def test_a_record_outlives_the_process(self, tmp_path, monkeypatch):
        """The whole point: the claim has to survive a kill.

        A session killed before its reset leaves the seed on disk. Only a record
        that is still there in the NEXT process can tell that file apart from a
        user's own.
        """
        path = tmp_path / "settings.local.json"
        sp.record(path, "payload", _OWNER)
        # Simulate a fresh process: drop the in-memory view and reload from disk.
        monkeypatch.setattr(sp, "_RECORDS", {})
        monkeypatch.setattr(sp, "_LIVE", {})
        sp._load()
        # A reloaded record has no live holder by construction -- whoever wrote it
        # belongs to a process that is gone -- so the NEXT session may adopt it.
        assert sp.recorded(path, "a-later-session") == (len("payload"), sp.digest("payload"))

    @pytest.mark.skipif(
        sys.platform == "win32",
        # Not a weaker assertion on Windows, a different mechanism: NTFS carries
        # ACLs, not POSIX mode bits, so ``st_mode`` there reports a synthesised
        # ``0o666`` from the read-only attribute alone and no ``chmod`` can change
        # it. Asserting ``600`` would pin an artefact of the emulation rather than
        # the confidentiality of the sidecar, which on that platform is inherited
        # from the data home's ACL.
        reason="POSIX mode bits; st_mode on Windows is synthesised from the RO attribute",
    )
    def test_the_sidecar_is_owner_only(self, tmp_path):
        sp.record(tmp_path / "settings.local.json", "x", _OWNER)
        # It names the work dirs this install has seeded.
        assert oct(sp._sidecar_path().stat().st_mode)[-3:] == "600"

    def test_a_corrupt_sidecar_degrades_to_unowned(self, tmp_path, monkeypatch):
        sp._sidecar_path().parent.mkdir(parents=True, exist_ok=True)
        sp._sidecar_path().write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(sp, "_RECORDS", {})
        sp._load()  # must not raise
        assert sp.recorded(tmp_path / "settings.local.json", _OWNER) is None

    def test_a_malformed_entry_is_skipped_not_trusted(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.local.json"
        sp._sidecar_path().parent.mkdir(parents=True, exist_ok=True)
        sp._sidecar_path().write_text(
            # The stray top-level key is deliberate: the reader takes ``seeds`` and
            # ignores everything beside it, so a future format that grows one stays
            # readable here. What it must not do is trust ``{"size": "big"}``.
            json.dumps({"written-by": "something-else", "seeds": {str(path): {"size": "big"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(sp, "_RECORDS", {})
        sp._load()
        assert sp.recorded(path, _OWNER) is None

    def test_the_sidecar_carries_seeds_and_nothing_else(self, tmp_path):
        """No format marker, and that is a decision rather than an omission.

        A ``version``/``schema`` key would have no reader: adoption is decided by the
        digest alone, so nothing branches on it, and an unrecognized value could only
        be ignored -- which is what an ABSENT marker already means. Writing one costs
        a field that must be kept consistent forever and buys a compatibility story
        no code implements. If a second format ever exists, its marker's absence
        identifies the first one.
        """
        sp.record(tmp_path / "settings.local.json", "payload", _OWNER)
        assert list(json.loads(sp._sidecar_path().read_text(encoding="utf-8"))) == ["seeds"]

    def test_a_lookup_never_touches_the_disk(self, tmp_path, monkeypatch):
        """``_reset_state`` consults ownership synchronously ON the event loop.

        A lookup that read the sidecar would put a filesystem round-trip (and, on
        a hostile path, a blocking open) in the gateway's loop.
        """
        path = tmp_path / "settings.local.json"
        sp.record(path, "payload", _OWNER)

        def _boom():
            raise AssertionError("recorded() must not resolve the sidecar path")

        monkeypatch.setattr(sp, "_sidecar_path", _boom)
        assert sp.recorded(path, _OWNER) is not None

    def test_the_publish_happens_under_the_record_lock(self, tmp_path, monkeypatch):
        """Mutate -> prune -> snapshot -> publish is ONE transaction.

        Two seeds run concurrently under ``asyncio.to_thread`` (one client per
        session, both writing the shared default ``work_dir``). Releasing between
        the snapshot and its publish lets an OLDER snapshot land last, and the newer
        seed's provenance is then simply gone -- so the surviving file reads as a
        stranger's next run, which is the whole failure this module exists to
        remove.
        """
        held: list[bool] = []
        real = sp.atomic_write

        def _observe(*args, **kwargs):
            held.append(sp._LOCK.locked())
            return real(*args, **kwargs)

        monkeypatch.setattr(sp, "atomic_write", _observe)
        sp.record(tmp_path / "settings.local.json", "payload", _OWNER)
        assert held == [True]

    def test_concurrent_records_all_survive(self, tmp_path):
        # The observable consequence of the lock: whichever snapshot publishes last
        # is a snapshot of the FULLY mutated map, so no writer's entry is dropped.
        paths = [tmp_path / f"work-{i}" / "settings.local.json" for i in range(8)]
        for path in paths:
            # Each seed exists on disk, as it does on the real seed path: the file is
            # written and then recorded. An entry whose file is gone is pruned, so a
            # version of this test that skipped the write would be measuring the
            # prune rather than the lock.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        ready = threading.Barrier(len(paths))

        def _seed(path: Path) -> None:
            ready.wait()
            sp.record(path, f"payload-{path.parent.name}", _OWNER)

        threads = [threading.Thread(target=_seed, args=(p,)) for p in paths]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        persisted = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))["seeds"]
        assert sorted(persisted) == sorted(os.fspath(p) for p in paths)

    def test_a_concurrent_processs_record_is_not_dropped(self, tmp_path):
        """The gateway and a CLI chat share one sidecar; neither may clobber the other.

        Each process holds its OWN in-memory ``_RECORDS`` and ``atomic_write`` renames
        a fresh inode over the sidecar, so a process that published its process-local
        snapshot would drop every record another process had written -- leaving that
        seed permanently unadoptable, the stale-state failure this module exists to
        remove. Persisting is reload-merge-publish under the cross-process lock, so a
        sibling's on-disk record survives this process publishing its own.

        The sibling is simulated by a record already ON DISK and absent from this
        process's ``_RECORDS`` -- exactly what a second process's write looks like from
        here. A ``_persist`` that wrote only ``_RECORDS`` fails this.
        """
        sibling = tmp_path / "gateway-wd" / "settings.local.json"
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_text("B", encoding="utf-8")
        sp._sidecar_path().parent.mkdir(parents=True, exist_ok=True)
        sp._sidecar_path().write_text(
            json.dumps({"seeds": {os.fspath(sibling): {"size": 1, "sha256": sp.digest("B")}}}),
            encoding="utf-8",
        )

        ours = tmp_path / "cli-wd" / "settings.local.json"
        ours.parent.mkdir(parents=True, exist_ok=True)
        ours.write_text("A", encoding="utf-8")
        assert sp.record(ours, "A", _OWNER) is True

        persisted = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))["seeds"]
        assert os.fspath(sibling) in persisted, "the sibling process's record was dropped"
        assert os.fspath(ours) in persisted
        # And the sibling stays "not ours": reloading it to publish must not make this
        # process treat a live sibling's seed as an adoptable orphan.
        assert sp.recorded(sibling, "a-later-session") is None

    def test_the_digest_lookup_does_not_take_the_record_lock(self, tmp_path):
        """Loop-side fingerprint reads remain memory-only and lock-free."""
        path = tmp_path / "settings.local.json"
        sp.record(path, "payload", _OWNER)

        with sp._LOCK:
            assert sp.recorded(path, _OWNER) is not None

    def test_forget_publishes_under_the_same_lock_record_uses(self, tmp_path, monkeypatch):
        """The revoke is a publish, so it is the same transaction ``record`` is.

        Snapshot-then-publish without the lock lets a concurrent ``record`` land an
        older snapshot last, which would restore the grant this call just revoked.
        """
        path = tmp_path / "settings.local.json"
        sp.record(path, "payload", _OWNER)

        held: list[bool] = []
        real = sp.atomic_write

        def _observe(*args, **kwargs):
            held.append(sp._LOCK.locked())
            return real(*args, **kwargs)

        monkeypatch.setattr(sp, "atomic_write", _observe)
        sp.forget(path, _OWNER)
        assert held == [True]

    def test_forget_reports_true_only_when_the_disk_agrees(self, tmp_path, monkeypatch):
        """The return value is the whole contract: it authorizes a deletion.

        The caller unlinks the file only on ``True``, so a revoke that reports
        success it did not achieve is the one failure mode that matters here -- the
        sidecar would keep naming a path whose file is gone, and the next process
        would adopt whatever appears there.
        """
        path = tmp_path / "settings.local.json"
        sp.record(path, "payload", _OWNER)
        assert sp.forget(path, _OWNER) is True
        assert sp.recorded(path, "a-later-session") is None

        sp.record(path, "payload", _OWNER)

        def _boom(*args, **kwargs):
            raise OSError("EROFS")

        monkeypatch.setattr(sp, "atomic_write", _boom)
        assert sp.forget(path, _OWNER) is False
        # The record stays visible, while the retained durable holder prevents a
        # sibling from claiming bytes the failed revoke still protects.
        assert sp.recorded(path, _OWNER) is not None
        assert sp.recorded(path, "a-later-session") == _fp("payload")
        assert sp.claim(path, "a-later-session", expect_digest=_fp("payload")) is False

    def test_forget_refuses_to_revoke_a_live_siblings_claim(self, tmp_path, monkeypatch):
        """A client that could not adopt a seed cannot revoke its grant either."""
        path = tmp_path / "settings.local.json"
        sp.record(path, "payload", "the-live-session")

        def _boom(*args, **kwargs):
            raise AssertionError("a refused revoke must not publish")

        monkeypatch.setattr(sp, "atomic_write", _boom)
        assert sp.forget(path, "a-sibling") is False
        assert sp.recorded(path, "the-live-session") is not None

    def test_forget_refused_under_sharer_then_release_succeeds(self, tmp_path, monkeypatch):
        """The mechanism an owner falls back to when a live sharer refuses its revoke.

        ``forget`` is the whole-record revoke and fails closed (``require_unheld``)
        while any other live holder protects the path -- a sibling's reader lease
        included. ``release`` drops only THIS owner's holder, with no unheld
        requirement, and leaves the record and the sharer's lease standing. The
        owner's replaced-after-create path relies on exactly that asymmetry; a
        ``require_unheld`` creeping into ``release`` would leak the owner slot again.
        """
        _pin_holder_identities(monkeypatch, foreign_live=False)
        path = tmp_path / "settings.local.json"
        assert sp.record(path, "payload", _OWNER) is True
        assert sp.share(path, "payload", "sib") is True

        assert sp.forget(path, _OWNER) is False
        assert sp.release(path, _OWNER) is True

        assert sp.has_sharers(path) is True
        assert sp.held_by_another(path, "a-later-session") is False
        assert sp.recorded_durable(path) == _fp("payload")

    def test_forgetting_an_unrecorded_path_still_proves_disk_agreement(self, tmp_path):
        """No local entry still requires a durable empty-record agreement."""
        assert sp.forget(tmp_path / "settings.local.json", _OWNER) is True
        assert sp._read_disk_seeds() == {}

    def test_an_entry_whose_file_is_gone_is_pruned(self, tmp_path):
        """The one prune, and the whole bound: no file, nothing to be owner of.

        A long-lived install rotating through disposable work dirs must not grow the
        sidecar without end. It cannot, because an entry survives only while a file
        is actually at its path -- so the sidecar is bounded by the seeds on disk,
        which is the only bound that means anything here.
        """
        for i in range(10):
            path = tmp_path / f"gone-{i}.json"
            sp.record(path, "x", _OWNER)
            sp.release(path, _OWNER)

        # Only the most recent survives: each ``record`` exempts its own key (see
        # ``_persist``'s ``keep``) and prunes every other path with no file or holder.
        assert list(sp._RECORDS) == [os.fspath(tmp_path / "gone-9.json")]
        assert sp._LIVE == {}
        persisted = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))
        assert list(persisted["seeds"]) == [os.fspath(tmp_path / "gone-9.json")]

    def test_an_adoptable_orphan_is_never_pruned_however_many_there_are(self, tmp_path):
        """There is no entry CAP, deliberately, and this is the reason why.

        A cap can only ever evict entries whose file still EXISTS -- the dead ones
        are already gone -- and those are precisely the adoptable orphans this
        module exists to keep. Evicting one makes its path unrecorded, which is
        worse in both directions at once: its own owner cannot recognize it
        on reset, so it leaks, and no later session is permitted to repair it
        either, so whatever it holds (a stale ``availableModels``, a stale
        ``permissions.defaultMode``, up to an inherited ``bypassPermissions``)
        becomes permanent project state. That is the exact failure this module
        removes, so a cap would re-manufacture it for the oldest work dir.

        200 is well past any cap that was ever plausible here, so this fails on any
        version that reintroduces one.
        """
        orphans = []
        for i in range(200):
            seed = tmp_path / f"orphan-{i}.json"
            seed.write_text("x", encoding="utf-8")
            orphans.append(seed)
            sp.record(seed, "x", f"dead-session-{i}")
            sp.release(seed, f"dead-session-{i}")  # the session ended; file remains

        assert len(sp._RECORDS) == 200
        for seed in orphans:
            # Adoptable by the next session, which is the point of keeping it.
            assert sp.recorded(seed, "a-later-session") is not None
        persisted = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))
        assert len(persisted["seeds"]) == 200

    def test_a_live_seed_is_never_pruned_either(self, tmp_path):
        """The same rule seen from the other side: a live claim survives with it."""
        owned = []
        for i in range(50):
            seed = tmp_path / f"live-{i}.json"
            seed.write_text("x", encoding="utf-8")
            owned.append((seed, f"live-owner-{i}"))
            sp.record(seed, "x", f"live-owner-{i}")

        assert len(sp._RECORDS) == 50
        for seed, owner in owned:
            assert sp.recorded(seed, owner) is not None
            # Surviving the prune must not cost the owner scoping.
            assert sp.recorded(seed, "a-sibling") is None

    def test_the_live_owner_is_published_before_the_record(self, tmp_path, monkeypatch):
        """``_LIVE`` has to land first, because the lookup between them is lock-free.

        Asserted from INSIDE the mutation rather than off the source text, so it
        holds however ``record`` is spelled.
        """
        observed: list[str | None] = []

        class _Watched(dict):
            def __setitem__(self, key, value):
                observed.append(sp._LIVE.get(key))
                super().__setitem__(key, value)

        monkeypatch.setattr(sp, "_RECORDS", _Watched())
        seed = tmp_path / "settings.local.json"
        seed.write_text("x", encoding="utf-8")
        sp.record(seed, "x", _OWNER)
        assert observed == [_OWNER]

    def test_a_sibling_never_sees_a_fresh_seed_as_an_orphan(self, tmp_path, monkeypatch):
        """The consequence of that order, stated as the sibling's own answer.

        :func:`recorded` is lock-free by design, so a sibling client reads both dicts
        from another thread between ``record``'s statements. If the record were
        published first, the instant it became visible the just-written seed would
        read as an ORPHAN -- recorded, no live holder, digest matching the file now on
        disk -- and the sibling would claim it, rewrite it under its own
        ``permissions.defaultMode`` and unlink it on its own reset, out from under a
        session still running against it.
        """
        sibling_saw: list[tuple[int, str] | None] = []

        class _Watched(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                sibling_saw.append(sp.recorded(key, "a-sibling"))

        monkeypatch.setattr(sp, "_RECORDS", _Watched())
        seed = tmp_path / "settings.local.json"
        seed.write_text("x", encoding="utf-8")
        sp.record(seed, "x", _OWNER)
        assert sibling_saw == [None]


class TestCrossSessionAdoption:
    """A seed orphaned by a killed session is Crew's to re-seed; nothing else is."""

    def test_an_orphaned_seed_is_reseeded_by_the_next_session(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        # Session 1: cold cache, so no model keys (the adapter's own provider list
        # is better than a guessed one) -- then the process dies without reset.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        first = _client(tmp_path, model="claude-opus-5")
        first._write_claude_local_settings()
        assert "availableModels" not in _seed(tmp_path)
        _the_owning_process_died()

        # Session 2, cache now warm from session 1's capture. Before the durable
        # record existed this session saw a stranger's file and left it alone, so
        # the half-seeded file was the permanent state of that work_dir.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        second = _client(tmp_path, model="claude-opus-5")
        second._model = mr.resolve_wire_model_id(second._model, "claude_code")
        second._write_claude_local_settings()

        data = _seed(tmp_path)
        assert data["availableModels"] == _SERVED
        assert data["model"] == "global.anthropic.claude-opus-5[1m]"
        assert second._claude_settings_authored is True

    def test_a_user_authored_file_is_still_left_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        path = _settings(tmp_path)
        path.parent.mkdir(parents=True)
        original = json.dumps({"permissions": {"allow": ["Bash(ls)"]}}, indent=2)
        path.write_text(original, encoding="utf-8")

        client = _client(tmp_path, permission_mode="default")
        client._write_claude_local_settings()
        assert path.read_text(encoding="utf-8") == original
        assert client._claude_settings_authored is False
        client._reset_state()
        assert path.read_text(encoding="utf-8") == original

    def test_a_crew_seed_the_user_edited_is_left_untouched(self, tmp_path, monkeypatch):
        """Adoption is earned by the digest, not by the record.

        The record says "Crew wrote this path"; it is the hash that says "these are
        still Crew's bytes". Editing the file makes it the user's, which is what
        keeps the credential from becoming a licence to overwrite a path.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        first = _client(tmp_path)
        first._write_claude_local_settings()
        path = _settings(tmp_path)
        edited = json.dumps({"model": "mine", "env": {"X": "1"}}, indent=2)
        path.write_text(edited, encoding="utf-8")
        _the_owning_process_died()

        assert sp.recorded(path, "a-later-session") is not None  # the record is still there
        second = _client(tmp_path, model="claude-opus-5")
        second._write_claude_local_settings()
        assert path.read_text(encoding="utf-8") == edited
        assert second._claude_settings_authored is False

    def test_without_a_record_an_orphan_is_left_untouched(self, tmp_path, monkeypatch):
        # Provenance, not the path: an identical file Crew cannot vouch for gets the
        # same treatment as the user's own. This is the pre-fix behaviour, kept for
        # every case the record does not cover (another install, a copied repo).
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        first = _client(tmp_path)
        first._write_claude_local_settings()
        before = _settings(tmp_path).read_text(encoding="utf-8")
        _the_owning_process_died()

        monkeypatch.setattr(sp, "_RECORDS", {})
        second = _client(tmp_path, model="claude-opus-5")
        second._write_claude_local_settings()
        assert _settings(tmp_path).read_text(encoding="utf-8") == before
        assert second._claude_settings_authored is False

    def test_an_orphaned_bypass_mode_is_overwritten_not_inherited(self, tmp_path, monkeypatch):
        """Re-seeding an orphan is also the only way to clean one up.

        ``bypassPermissions`` takes every tool call out of the host gate. A seed
        carrying it that outlives its session would otherwise stay frozen in place
        and be read by the adapter; adoption overwrites the mode with THIS session's.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        first = _client(tmp_path, permission_mode="bypassPermissions")
        first._write_claude_local_settings()
        assert _seed(tmp_path)["permissions"]["defaultMode"] == "bypassPermissions"
        _the_owning_process_died()

        second = _client(tmp_path, permission_mode="default")
        second._write_claude_local_settings()
        assert _seed(tmp_path)["permissions"]["defaultMode"] == "default"

    def test_a_live_siblings_seed_is_left_alone(self, tmp_path, monkeypatch):
        """Adoption is for an orphan, and a running sibling has not left one.

        Two keyless sessions share the default ``work_dir``, so this is the
        ordinary multi-session case, not a contrived one. Adopting here would write
        THIS session's ``permissions.defaultMode`` into a file the live session is
        running against, and delete that file on this session's reset.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        live = _client(tmp_path, permission_mode="bypassPermissions")
        live._write_claude_local_settings()
        before = _settings(tmp_path).read_text(encoding="utf-8")

        sibling = _client(tmp_path, permission_mode="default")
        sibling._write_claude_local_settings()
        assert _settings(tmp_path).read_text(encoding="utf-8") == before
        assert sibling._claude_settings_authored is False

        # ...and the sibling's own teardown cannot revoke the live session's claim.
        _teardown(sibling)
        assert _settings(tmp_path).read_text(encoding="utf-8") == before
        assert live._claude_settings_is_still_ours() is True

    def test_an_adopted_seed_is_removed_on_reset(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        _client(tmp_path)._write_claude_local_settings()
        _the_owning_process_died()
        second = _client(tmp_path)
        second._write_claude_local_settings()
        _teardown(second)
        # A permission mode must not outlive its session -- including one this
        # session adopted rather than created.
        assert not _settings(tmp_path).exists()
        # And the claim goes with it, so whatever appears at this path next is not
        # adopted on the strength of a stale entry.
        assert sp.recorded(_settings(tmp_path), "a-later-session") is None

    def test_a_symlink_is_refused_before_ownership_is_considered(self, tmp_path, monkeypatch):
        # The path guard runs first: a recorded path that is now a link must not be
        # written THROUGH, whatever the record says.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        first = _client(tmp_path)
        first._write_claude_local_settings()
        path = _settings(tmp_path)
        target = tmp_path / "elsewhere.json"
        path.unlink()
        path.symlink_to(target)
        # Dead owner, so the record IS adoptable -- the symlink guard is what has to
        # refuse, not the live-sibling check.
        _the_owning_process_died()

        second = _client(tmp_path)
        second._write_claude_local_settings()
        assert not target.exists()
        assert second._claude_settings_authored is False

    def test_a_grown_file_is_not_ours(self, tmp_path, monkeypatch):
        # The read is capped one byte past the recorded length, so a file appended
        # to after the fstat is rejected on length instead of matching a prefix.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        first = _client(tmp_path)
        first._write_claude_local_settings()
        with open(_settings(tmp_path), "a", encoding="utf-8") as fh:
            fh.write("trailing")
        assert first._claude_settings_is_still_ours() is False


class TestPostCaptureModelResolution:
    """The ordering half: the fold and the re-seed happen AFTER the capture."""

    @pytest.mark.asyncio
    async def test_startup_model_folds_onto_the_advertised_spelling(self, tmp_path, monkeypatch):
        """A bare id must not reach the wire once the backend has advertised one.

        The spawn-time fold runs before ``session/new``, so on a first-ever session
        it folds against a cold cache and is a no-op -- and the bare id it then
        sends is exactly what resolves to the base window. Folding again here, after
        the capture, is what makes the first session behave like the second.
        """
        sent: list[str] = []

        async def _capture(config_id, value):
            sent.append(value)

        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, model="claude-opus-5")
        client._session_id = "sid"
        # claude-agent-acp takes the model through session/set_config_option.
        monkeypatch.setattr(client, "set_config_option", _capture)

        await client._apply_startup_model()

        assert sent == ["global.anthropic.claude-opus-5[1m]"]
        # And the client remembers the folded id, so the re-seed writes the same
        # spelling the wire carries.
        assert client._model == "global.anthropic.claude-opus-5[1m]"

    @pytest.mark.asyncio
    async def test_an_unadvertised_model_is_left_exactly_as_configured(self, tmp_path, monkeypatch):
        # The fold only ever tightens a bare id onto an advertised one. A model the
        # backend does not serve is not rewritten into one that looks similar.
        sent: list[str] = []

        async def _capture(config_id, value):
            sent.append(value)

        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, model="some-other-vendor-model")
        client._session_id = "sid"
        monkeypatch.setattr(client, "set_config_option", _capture)

        await client._apply_startup_model()
        assert sent == ["some-other-vendor-model"]

    @pytest.mark.asyncio
    async def test_step_six_reseeds_off_the_loop(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = _client(tmp_path, model="claude-opus-5")
        client._model = mr.resolve_wire_model_id(client._model, "claude_code")
        await client._reseed_after_capture()
        assert _seed(tmp_path)["model"] == "global.anthropic.claude-opus-5[1m]"

    def test_the_reseed_rides_an_existing_adapter_only_branch(self):
        """Harness parity (AUTOSDE H13): the KIRO path must not gain the step.

        The rule tests "did the kiro path change at all", not "does it still work",
        so a NEW ``if`` plus a NEW ``await`` in ``_initialize_session`` changes that
        path however the predicate is spelled -- moving the gate to the call site is
        no more exempt than leaving it in the method, because the branch itself IS
        the change. So the re-seed is a second statement inside the
        ``_uses_advertised_model_selection`` branch that already existed in main
        beside the model-cache persist, and ``_initialize_session`` gains no
        conditional of its own. That is the honest home for it besides: the step
        exists BECAUSE the backend advertises its own model list.

        The seeding capability is then tested INSIDE the method -- the two capability
        sets are independent opt-ins, so the caller's gate is not a substitute.
        """
        import inspect
        import textwrap

        source = textwrap.dedent(inspect.getsource(AcpClient._initialize_session))
        assert "if self._seeds_local_settings:" not in source
        assert source.count("await self._reseed_after_capture()") == 2
        rode_along = (
            "if self._uses_advertised_model_selection:\n"
            "            await self._persist_advertised_models_if_changed()\n"
            "            await self._reseed_after_capture()"
        )
        assert rode_along in source

        method = textwrap.dedent(inspect.getsource(AcpClient._reseed_after_capture))
        assert "if not self._seeds_local_settings:\n        return" in method

    @pytest.mark.asyncio
    async def test_a_harness_that_seeds_no_settings_file_writes_nothing(
        self, tmp_path, monkeypatch
    ):
        """The in-method gate is the one that actually has to hold."""
        client = _client(tmp_path)
        monkeypatch.setattr(AcpClient, "_seeds_local_settings", property(lambda self: False))
        calls: list[int] = []
        monkeypatch.setattr(client, "_write_claude_local_settings", lambda: calls.append(1))
        await client._reseed_after_capture()
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_failed_reseed_does_not_kill_the_session(self, tmp_path, monkeypatch):
        # Model fidelity is worth a warning, not a dead session: the adapter still
        # has its own settings sources and tool calls still reach the host gate.
        attempts = []

        def _boom():
            attempts.append(True)
            raise OSError("read-only filesystem")

        client = _client(tmp_path)
        monkeypatch.setattr(client, "_write_claude_local_settings", _boom)
        await client._reseed_after_capture()  # must not raise
        assert attempts == [True]

    def test_session_init_reseeds_right_after_every_model_capture(self):
        """The step is only worth anything if session init still calls it.

        ``_initialize_session`` needs a live child process to drive end to end, so
        this pins the wiring rather than the behaviour: the behaviour is covered
        above. BOTH captures matter -- ``session/load`` on a resume and
        ``session/new`` on a fresh session each warm the cache the re-seed reads.
        """
        import inspect

        source = inspect.getsource(AcpClient._initialize_session)
        assert source.count("_reseed_after_capture()") == 2
        # Each call sits immediately after a capture, so it never reads a cache the
        # session in hand has not warmed yet.
        for capture in (
            "_capture_available_models(load_resp)",
            "_capture_available_models(session_resp)",
        ):
            after = source[source.index(capture) :]
            between = after[: after.index("await self._reseed_after_capture()")]
            # Nothing between them but the pre-existing capability gate and the
            # model-cache persist it already guarded.
            assert between.count("await ") == 1
            assert "if self._uses_advertised_model_selection:" in between

    def test_the_written_model_id_is_folded_by_the_writer_itself(self):
        """No ordering coupling to ``_apply_startup_model``.

        The re-seed now runs beside the model-cache persist, which is BEFORE the
        startup model apply, so the writer cannot lean on that step having folded
        ``self._model`` onto the advertised spelling. It folds the value it writes
        itself -- and the failure it avoids is silent: a bare id names a model that
        is not in the ``availableModels`` list shipped beside it, which is exactly
        the shape that resolves to the base 200K window.
        """
        import inspect

        # The payload is rendered by ``_render_claude_settings_payload`` (split out
        # so the shared-reader check can compare bytes before deciding to write);
        # the fold lives there, on the write path itself either way.
        source = inspect.getsource(AcpClient._render_claude_settings_payload)
        assert 'data["model"] = self._model' not in source
        assert "resolve_wire_model_id" in source

    def test_the_cold_seed_becomes_coherent_after_the_capture(self, tmp_path, monkeypatch):
        """One session, start to finish -- the sequence the fix exists for.

        Cold seed (no model keys) -> ``session/new`` capture warms the cache ->
        re-seed. The file ends up naming a model that IS in the list shipped beside
        it, which is what the pre-fix file never did: it carried
        ``"model": "claude-opus-5"`` next to an allowlist with no Opus 5 entry, so
        the pick resolved to 200K.
        """
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        client = _client(tmp_path, model="claude-opus-5")

        client._write_claude_local_settings()  # spawn: before session/new
        assert "model" not in _seed(tmp_path)

        client._capture_available_models(
            {"models": {"availableModels": [{"modelId": mid} for mid in _SERVED]}}
        )
        client._model = mr.resolve_wire_model_id(client._model, "claude_code")
        client._write_claude_local_settings()  # step 6: after the capture

        data = _seed(tmp_path)
        assert data["model"] == "global.anthropic.claude-opus-5[1m]"
        assert data["model"] in data["availableModels"]

    def test_the_seed_never_ships_a_base_window_sibling(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        # The adapter reads [1m] as a context-window MODIFIER on one base model and
        # dedups availableModels by base name, so shipping both spellings lets it
        # pick the 200K one.
        monkeypatch.setattr(
            mr,
            "_ADVERTISED_MODELS",
            {
                "claude_code": [
                    "global.anthropic.claude-opus-4-8[1m]",
                    "global.anthropic.claude-opus-4-8",
                ]
            },
        )
        client = _client(tmp_path)
        client._write_claude_local_settings()
        assert _seed(tmp_path)["availableModels"] == ["global.anthropic.claude-opus-4-8[1m]"]

    def test_a_non_seeding_backend_writes_nothing(self, tmp_path, monkeypatch):
        # The seam is capability-gated, not claude-literal, and a backend outside
        # the set must not gain a settings file it never reads.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(_SERVED)})
        client = AcpClient(work_dir=tmp_path)  # kiro-cli
        assert client._seeds_local_settings is False
        assert not _settings(tmp_path).exists()
        assert not os.path.exists(_settings(tmp_path))
