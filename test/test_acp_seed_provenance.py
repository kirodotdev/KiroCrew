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

from kiro_crew import model_registry as mr
from kiro_crew import sandbox, security
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import seed_provenance as sp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

_SERVED = [
    "global.anthropic.claude-opus-5[1m]",
    "global.anthropic.claude-opus-4-8[1m]",
]

# The owner token a bare-sidecar test records under. A client uses its own
# ``_seed_owner``; these tests only need one stable identity.
_OWNER = "owner-under-test"


@pytest.fixture(autouse=True)
def isolated_records(monkeypatch):
    """Per-test provenance state.

    ``_RECORDS`` and ``_LIVE`` are process-wide runtime state (like
    ``model_registry._ADVERTISED_MODELS``); the sidecar itself already lands in a
    per-test ``KIROCREW_HOME``.
    """
    monkeypatch.setattr(sp, "_RECORDS", {})
    monkeypatch.setattr(sp, "_LIVE", {})


def _client(tmp_path: Path, **kw) -> AcpClient:
    return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kw)


def _settings(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.local.json"


def _seed(tmp_path: Path) -> dict:
    return json.loads(_settings(tmp_path).read_text(encoding="utf-8"))


def _teardown(client: AcpClient) -> None:
    """Tear a client down the way every real caller does.

    The seed's removal is a DISK operation -- a durable revoke, then an unlink -- so
    it lives in the async ``_discard_claude_settings_seed`` and reaches the
    filesystem through ``asyncio.to_thread``. ``_reset_state`` stays synchronous and
    keeps only the in-memory claim release, so calling it alone leaves the file
    behind on purpose. Every production caller awaits the discard first and then
    resets, and this helper is that pair, so a test that drifts from the real
    ordering fails here rather than passing on a shape nothing uses.
    """
    asyncio.run(client._discard_claude_settings_seed())
    client._reset_state()


def _unwritable_sidecar(*_args, **_kwargs):
    """What a read-only data home (or a full disk) does to the sidecar publish.

    Patched over ``seed_provenance.atomic_write`` specifically, which is a DIFFERENT
    binding from the one ``acp.client`` uses for the settings file itself -- so the
    seed still lands and only its durable grant fails, which is the case under test.
    """
    raise OSError("EROFS: read-only file system")


def _attribute_calls(node: ast.AST, name: str) -> list[ast.Attribute]:
    """Every ``<something>.name`` attribute reference inside *node*.

    Attribute rather than Call so a reference through a decorator, an
    ``ensure_future`` or a ``to_thread`` counts too: the question these tests ask is
    which method a region of source reaches, not how it spells the invocation.
    """
    return [n for n in ast.walk(node) if isinstance(n, ast.Attribute) and n.attr == name]


def _the_owning_process_died() -> None:
    """What a ``kill -9`` leaves behind: the sidecar, but no live claims.

    ``_LIVE`` is process memory, so it dies with the process; ``_RECORDS`` mirrors
    the sidecar on disk, which does not. Every cross-session test has to go
    through here rather than just constructing a second client, because a sibling
    that is still LIVE is deliberately not adoptable.
    """
    sp._LIVE.clear()


class TestProvenanceRecord:
    """The sidecar itself: what it records, what it refuses to claim."""

    def test_record_then_recognize(self, tmp_path):
        path = tmp_path / "settings.local.json"
        sp.record(path, '{"a": 1}\n', _OWNER)
        assert sp.recorded(path, _OWNER) == (len('{"a": 1}\n'), sp.digest('{"a": 1}\n'))

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

    def test_release_cannot_evict_the_winner(self, tmp_path):
        path = tmp_path / "settings.local.json"
        assert sp.claim(path, "winner") is True
        sp.release(path, "loser")
        assert sp.claim(path, "someone-else") is False

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
        assert sp.claim(path, "first") is True
        assert sp.claim(path, "second") is False
        # ...and the winner may re-claim its own slot, so re-seeding is not a
        # self-refusal.
        assert sp.claim(path, "first") is True
        assert sp.recorded(path, "second") is None

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
        # ``release`` is on a loop-side failure path, so it is memory-only too.
        # ``forget`` is NOT: it revokes a grant whose file has just been unlinked,
        # and a grant that outlives its file has to stop existing on disk as well.
        sp.release(path, _OWNER)

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

    def test_the_lookup_and_the_claim_do_not_take_the_lock(self, tmp_path):
        """``_LOCK`` is held across a disk write, so a loop-side READER must not want it.

        ``_reset_state`` consults ownership synchronously ON the event loop; if
        :func:`recorded` or :func:`release` took the lock, a worker thread mid-persist
        would stall the gateway's one loop. Holding it here would deadlock outright
        (a plain, non-reentrant ``threading.Lock``) if either did.

        :func:`forget` deliberately DOES take it -- it publishes -- which is why it is
        exercised separately below rather than here.
        """
        path = tmp_path / "settings.local.json"
        sp.record(path, "payload", _OWNER)

        with sp._LOCK:
            assert sp.recorded(path, _OWNER) is not None
            assert sp.claim(path, _OWNER) is True
            sp.release(path, _OWNER)
        assert sp.recorded(path, "a-later-session") is not None

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
        # Put back, so this process agrees with the sidecar a restart would read --
        # and so the file the caller was told to keep is still adoptable.
        assert sp.recorded(path, "a-later-session") is not None

    def test_forget_refuses_to_revoke_a_live_siblings_claim(self, tmp_path, monkeypatch):
        """A client that could not adopt a seed cannot revoke its grant either."""
        path = tmp_path / "settings.local.json"
        sp.record(path, "payload", "the-live-session")

        def _boom(*args, **kwargs):
            raise AssertionError("a refused revoke must not publish")

        monkeypatch.setattr(sp, "atomic_write", _boom)
        assert sp.forget(path, "a-sibling") is False
        assert sp.recorded(path, "the-live-session") is not None

    def test_forgetting_an_unrecorded_path_writes_nothing(self, tmp_path, monkeypatch):
        """No entry, no publish: a reset for a path Crew never seeded stays free."""

        def _boom(*args, **kwargs):
            raise AssertionError("forget() must not publish when it removed nothing")

        monkeypatch.setattr(sp, "atomic_write", _boom)
        sp.forget(tmp_path / "settings.local.json", _OWNER)

    def test_an_entry_whose_file_is_gone_is_pruned(self, tmp_path):
        """The one prune, and the whole bound: no file, nothing to be owner of.

        A long-lived install rotating through disposable work dirs must not grow the
        sidecar without end. It cannot, because an entry survives only while a file
        is actually at its path -- so the sidecar is bounded by the seeds on disk,
        which is the only bound that means anything here.
        """
        for i in range(10):
            sp.record(tmp_path / f"gone-{i}.json", "x", _OWNER)

        # Only the most recent survives: each ``record`` exempts its own key (see
        # ``_persist``'s ``keep``) and prunes every other path with no file.
        assert list(sp._RECORDS) == [os.fspath(tmp_path / "gone-9.json")]
        # The live map is pruned with it, so it cannot outlive the records.
        assert list(sp._LIVE) == [os.fspath(tmp_path / "gone-9.json")]
        persisted = json.loads(sp._sidecar_path().read_text(encoding="utf-8"))
        assert list(persisted["seeds"]) == [os.fspath(tmp_path / "gone-9.json")]

    def test_an_adoptable_orphan_is_never_pruned_however_many_there_are(self, tmp_path):
        """There is no entry CAP, deliberately, and this is the reason why.

        A cap can only ever evict entries whose file still EXISTS -- the dead ones
        are already gone -- and those are precisely the adoptable orphans this
        module exists to keep. Evicting one makes its path unrecorded, which is
        worse in both directions at once: its own owner can no longer recognize it
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
        carrying it that outlived its session used to be frozen in place and kept
        being read by the adapter; adoption overwrites the mode with THIS session's.
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

    LEAF = "settings_seeds.json"

    def test_the_sidecar_is_the_leaf_the_floors_name(self):
        # The floors are spelled as a filename, so they only hold if that is the name
        # the module actually publishes.
        assert sp._sidecar_path().name == self.LEAF

    def test_the_file_edit_gate_refuses_a_write_and_allows_a_read(self):
        for prefix in security.crew_home_prefixes():
            path = f"~/{prefix}/{self.LEAF}"
            assert security.is_sensitive_write_path(path) is True, path
            assert security.is_sensitive_path(path) is False, path

    def test_the_shell_gate_refuses_every_spelling(self):
        # Paired with the edit gate: protected on one path only is not protected. And
        # bare-token matched, because for a DELETION grant a ``cd`` must not be the
        # whole bypass -- the invariant is the filename, not the way to it.
        for cmd in (
            f"echo forged > ~/.kiro/crew/{self.LEAF}",
            f"cd ~/.kiro/crew && echo forged > {self.LEAF}",
            f"tee ./{self.LEAF}",
            f"python -c \"open('{self.LEAF}','w')\"",
        ):
            assert security.is_sensitive_bash_command(cmd) is not None, cmd

    def test_the_sandbox_seals_it_readonly_even_when_absent(self):
        # The kernel floor under the deny rules, which a runtime-constructed spelling
        # (``$(printf ...)``) walks past. READONLY, not hidden -- the write is the
        # risk. Precreated because ``mount(2)`` cannot seal a path that is not there,
        # and this sidecar does not exist until a claude session has seeded a work
        # dir: on every install that has not, the name is writable.
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
        # Teardown inode-pins the delete: it moves the file to a fresh ``*.crew-gc``
        # temp and deletes THAT, so a delete failure is a failure to remove the moved
        # inode. The temp name is randomized (mkstemp), so match on the suffix.
        real_unlink = Path.unlink

        def _refuse(self, *args, **kwargs):
            if str(self).endswith(".crew-gc"):
                raise OSError("EACCES")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _refuse)
        _teardown(client)

        # The moved inode could not be deleted, so it is restored under the pathname
        # and re-recorded rather than left as a frozen ``.crew-gc`` no session names.
        assert path.exists()
        assert not list(path.parent.glob("*.crew-gc"))
        _the_owning_process_died()
        assert sp.recorded(path, "a-later-session") is not None

        monkeypatch.setattr(Path, "unlink", real_unlink)
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
        real_unlink = Path.unlink

        def _watch_forget(*args, **kwargs):
            order.append("revoke")
            return real_forget(*args, **kwargs)

        def _watch_unlink(self, *args, **kwargs):
            if str(self).endswith(".crew-gc"):
                order.append("unlink")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(sp, "forget", _watch_forget)
        monkeypatch.setattr(Path, "unlink", _watch_unlink)
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
    outlives what it described. These pin the two cases where the pairing used to
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
            time.sleep(0.2)
            return real_settle(*args, **kwargs)

        monkeypatch.setattr(client, "_settle_claude_settings_seed", _slow_settle)

        async def _cancel_mid_teardown() -> None:
            task = asyncio.ensure_future(client._discard_claude_settings_seed())
            await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # ``asyncio.run`` shuts the default executor down on the way out, so by the
        # time it returns the shielded thread has finished -- no polling needed.
        asyncio.run(_cancel_mid_teardown())

        assert not path.exists()
        _the_owning_process_died()
        assert sp.recorded(path, "a-later-session") is None

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
        tree = ast.parse(Path(acp_client.__file__).read_text(encoding="utf-8"))

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

        aside = acp_client.AcpClient._claim_pathname_if_ours(path, expectation)
        assert aside is not None
        assert aside.read_text(encoding="utf-8") == "crew-bytes"
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

        The move-aside destination used to be a FIXED sibling (``<name>.crew-gc``),
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

        source = inspect.getsource(AcpClient._initialize_session)
        assert "if self._seeds_local_settings:" not in source
        assert source.count("await self._reseed_after_capture()") == 2
        rode_along = (
            "if self._uses_advertised_model_selection:\n"
            "                await self._persist_advertised_models_if_changed()\n"
            "                await self._reseed_after_capture()"
        )
        assert rode_along in source

        method = inspect.getsource(AcpClient._reseed_after_capture)
        assert "if not self._seeds_local_settings:\n            return" in method

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
        def _boom():
            raise OSError("read-only filesystem")

        client = _client(tmp_path)
        monkeypatch.setattr(client, "_write_claude_local_settings", _boom)
        await client._reseed_after_capture()  # must not raise

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

        source = inspect.getsource(AcpClient._write_claude_local_settings)
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
