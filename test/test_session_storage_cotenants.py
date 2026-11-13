"""Session storage: when this instance cannot see who else is live in a replay store.

``TestSharedStoreRefusal`` pins the reclaim refusals for a store others may share.
``TestCotenantCache`` pins that the co-tenant lookup reuses a cached answer for reads only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from session_storage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _NOW,
    _cli_half,
    _fresh_scan_cache,
    _index,
    _pin_a_fake_default_home,
    stores_fixture,
)

from kiro_crew import session_storage
from kiro_crew.session_storage import SessionIndex, SessionStorageError


class TestCotenantCache:
    """The co-tenant lookup follows the scan cache's rules: reads may reuse, mutations never.

    The cache is opt-in per call site because the same value gates destructive paths.
    The isolated *stores* fixture skips :func:`reclaim_block_reason`'s uncached read,
    which is why the reuse test counts 1.
    """

    @staticmethod
    def _count_pod_scans(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        """Count full passes over the pod root without changing their answer."""
        calls = {"n": 0}
        real = session_storage._replay_store_cotenants

        def counted() -> list[str]:
            calls["n"] += 1
            return real()

        monkeypatch.setattr(session_storage, "_replay_store_cotenants", counted)
        return calls

    def test_a_read_inside_the_ttl_does_not_rescan_the_pod_root(
        self, stores: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scan-cache hit must not still pay for the co-tenant half."""
        _, kiro_home = stores
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        calls = self._count_pod_scans(monkeypatch)

        index = _index()
        session_storage.list_units(index)
        session_storage.measure(index, now=_NOW)
        session_storage.list_units(index)

        assert calls["n"] == 1, "reads within the TTL must enumerate the pod root once"

    def test_select_reclaimable_rereads_cotenants_on_every_call(
        self, stores: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reclaim selection derives refusals from this value: never from a snapshot."""
        _, kiro_home = stores
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        session_storage.list_units(_index())  # prime both caches
        calls = self._count_pod_scans(monkeypatch)

        session_storage.select_reclaimable(_index(), 30.0, now=_NOW)
        session_storage.select_reclaimable(_index(), 30.0, now=_NOW)

        assert calls["n"] == 2, "a mutation-path selection must re-read co-tenants every call"

    def test_move_to_trash_rereads_cotenants_on_every_call(
        self, stores: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both the scan inside the move and the pre-move authority check must be live.

        The pre-move re-read exists precisely because the view taken during the
        scan is seconds stale by the time anything moves; a primed cache must not
        be allowed to answer it.
        """
        _, kiro_home = stores
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        session_storage.list_units(_index())  # prime both caches
        calls = self._count_pod_scans(monkeypatch)

        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

        assert calls["n"] == 2, "the move's scan and its authority re-read must each hit disk"

    def test_invalidate_scan_cache_drops_the_cotenant_pass_too(
        self, stores: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mutation hooks clear one thing; both caches must go with it."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        calls = self._count_pod_scans(monkeypatch)

        session_storage.list_units(_index())
        session_storage.invalidate_scan_cache()
        session_storage.list_units(_index())

        assert calls["n"] == 2, "invalidation must force a fresh co-tenant pass"

    def test_a_different_pod_root_is_not_answered_from_an_older_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KIROCREW_POD_ROOT`` resolves per call, so the root keys the cache.

        Same reasoning as the store locations in the scan key: keyed without the
        location, the second root would be answered with the first's pass.
        """
        first = tmp_path / "pods-a"
        pod = first / "wt-old"
        pod.mkdir(parents=True)
        # Own replay store: its sids are recorded but it constrains nothing.
        (pod / "kiro").mkdir()
        (pod / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1": {"sid": "podsid01"}}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(first))
        protected, refusals = session_storage.cotenant_sids(cached=True)
        assert protected == frozenset({"podsid01"})
        assert refusals == ()

        second = tmp_path / "pods-b"
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(second))
        assert session_storage.cotenant_sids(cached=True) == (
            frozenset(),
            (),
        ), "an empty pod root must read as empty, not as the first root's pass"

    def test_a_different_crew_home_is_not_answered_from_an_older_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KIROCREW_HOME`` alone keys the cache.

        ``hooks.safe_read_file`` re-anchors its read gate on the raw ``KIROCREW_HOME``,
        so the same symlinked map can be refused under one value and read under another.
        """
        pod_root = tmp_path / "pods"
        (pod_root / "wt-x").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "home-a" / "kiro"))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home-a"))
        calls = self._count_pod_scans(monkeypatch)
        session_storage.cotenant_sids(cached=True)

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home-b"))
        session_storage.cotenant_sids(cached=True)

        assert calls["n"] == 2, "a changed KIROCREW_HOME must not be served the old pass"

    def test_a_different_kiro_home_is_not_answered_from_an_older_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KIRO_HOME`` alone keys the cache — defensive over-keying, ratcheted.

        The gate's anchor set is keyed on both raw values. A spurious miss costs a
        re-scan; a spurious hit would cost a wrong answer.
        """
        pod_root = tmp_path / "pods"
        (pod_root / "wt-x").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home-a"))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "home-a" / "kiro"))
        calls = self._count_pod_scans(monkeypatch)
        session_storage.cotenant_sids(cached=True)

        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "home-b" / "kiro"))
        session_storage.cotenant_sids(cached=True)

        assert calls["n"] == 2, "a changed KIRO_HOME must not be served the old pass"

    def test_reclaim_block_reason_rereads_cotenants_despite_a_primed_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hard reclaim gate must never opt in — and nothing else covered it.

        The *stores* fixture skips this read, so this runs on the default home, where a
        primed cache must not answer it.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        # Pin the default home rather than clearing the memo; see
        # TestSharedStoreRefusal for why re-resolving on a real machine is unsafe.
        _pin_a_fake_default_home(monkeypatch, tmp_path)

        session_storage.cotenant_sids(cached=True)  # prime
        calls = self._count_pod_scans(monkeypatch)
        session_storage.reclaim_block_reason()
        session_storage.reclaim_block_reason()

        assert calls["n"] == 2, "the reclaim gate must re-enumerate the pod root on every call"

    def test_reclaim_block_reason_reuses_cache_when_cached_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Display callers passing cached=True reuse the primed co-tenant pass."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        _pin_a_fake_default_home(monkeypatch, tmp_path)

        session_storage.cotenant_sids(cached=True)  # prime
        calls = self._count_pod_scans(monkeypatch)
        session_storage.reclaim_block_reason(cached=True)
        session_storage.reclaim_block_reason(cached=True)

        assert calls["n"] == 0, "cached=True must reuse the primed co-tenant cache"

    def test_measure_reuses_primed_cotenant_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """measure() opts into cached=True to avoid an extra co-tenant scan."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        _pin_a_fake_default_home(monkeypatch, tmp_path)

        session_storage.cotenant_sids(cached=True)  # prime
        calls = self._count_pod_scans(monkeypatch)
        report = session_storage.measure(
            session_storage.SessionIndex(),
            units=[],
            batches=[],
        )

        assert calls["n"] == 0, "measure() must reuse the primed co-tenant cache"
        assert isinstance(report.reclaim_blocked_reason, str)


class TestSharedStoreRefusal:
    """An isolated data home over a shared replay store cannot see who is live."""

    def test_reclaim_is_refused_when_only_the_data_home_is_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crew_home = tmp_path / "crew"
        (crew_home / "sessions").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        monkeypatch.delenv("KIRO_HOME", raising=False)

        assert session_storage.reclaim_block_reason() != ""
        with pytest.raises(SessionStorageError, match="sits outside it"):
            session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

    def test_isolating_both_homes_is_allowed(self, stores: tuple[Path, Path]) -> None:
        """The fixture sets both, which is the safe configuration."""
        assert session_storage.reclaim_block_reason() == ""

    def test_isolating_neither_home_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        # The co-tenant check reads the pod root, which is real host state — point it
        # at an empty dir so this asserts the guard and not the developer's machine.
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        # Pin the default home rather than clearing the memo: a re-resolve on a real
        # machine migrates the operator's data home and stays memoized for the worker.
        _pin_a_fake_default_home(monkeypatch, tmp_path)

        assert session_storage.reclaim_block_reason() == ""

    def test_a_pre_migration_legacy_home_is_not_isolation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An install that has not migrated yet must still be able to reclaim.

        The legacy home is a default, not an isolated instance.
        """
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        _pin_a_fake_default_home(monkeypatch, tmp_path, legacy=True)

        assert session_storage.reclaim_block_reason() == ""

    def test_a_custom_store_shared_by_two_isolated_instances_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two pods pointed at one custom KIRO_HOME see neither map."""
        crew_home = tmp_path / "pod-a"
        crew_home.mkdir()
        shared_store = tmp_path / "shared-kiro"
        shared_store.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        monkeypatch.setenv("KIRO_HOME", str(shared_store))

        # Not the DEFAULT store, so a default-location test would pass it — and
        # that is the arrangement where sharing is least visible.
        assert session_storage.reclaim_block_reason() != ""

    def test_a_store_inside_the_isolated_data_home_may_reclaim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dedicated private store is genuinely isolated."""
        crew_home = tmp_path / "pod-b"
        crew_home.mkdir()
        own_store = crew_home / "kiro"
        own_store.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        monkeypatch.setenv("KIRO_HOME", str(own_store))

        assert session_storage.reclaim_block_reason() == ""

    def test_an_unreadable_batch_is_not_emptied(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scan that gives up early must not read as "nothing unaccounted for"."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        def failing_walk(top, onerror=None, **kwargs):
            if onerror is not None:
                onerror(OSError(5, "simulated read failure"))
            return iter(())

        # Both mechanisms: ``_unlisted_files`` picks fwalk-vs-walk at call time
        # by availability (fwalk does not exist on Windows).
        monkeypatch.setattr(session_storage.os, "walk", failing_walk)
        monkeypatch.setattr(session_storage.os, "fwalk", failing_walk, raising=False)

        assert session_storage.empty_trash([batch.batch_id]) == 0
        # The batch survives, so nothing was destroyed on an unverifiable scan.
        assert (session_storage.trash_root() / batch.batch_id).is_dir()

    def test_a_staged_symlink_is_not_restored(self, stores: tuple[Path, Path]) -> None:
        """is_file() follows links, so a link would be put back as session data.

        The link points INSIDE the batch on purpose: `_staged_path` resolves the
        manifest's `rel` and already refuses one that escapes, so only a link
        resolving within the batch reaches this check.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        batch_dir = session_storage.trash_root() / batch.batch_id
        staged = next(
            p
            for p in batch_dir.rglob("*")
            if p.is_file() and p.name != session_storage.MANIFEST_NAME
        )
        staged.unlink()
        try:
            staged.symlink_to(batch_dir / session_storage.MANIFEST_NAME)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        assert session_storage.restore(batch.batch_id) == 0
        # The origin is still absent rather than occupied by a dangling link.
        assert not (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").exists()

    def test_a_pod_with_a_readable_map_protects_its_sessions_without_blocking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mirror case: a pod reads THIS store while keeping its own map.

        Its map names the sessions it can resume, so those stay protected and the
        rest stays reclaimable.
        """
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-feature"
        pod.mkdir(parents=True)
        # Current pods export KIRO_HOME into the pod home, so this one reads its own
        # replay store and cannot be harmed by a reclaim here.
        (pod / "kiro" / "sessions" / "cli").mkdir(parents=True)
        (pod / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1": {"sid": "podsid01"}}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        _pin_a_fake_default_home(monkeypatch, tmp_path)

        assert session_storage.reclaim_block_reason() == ""
        protected, refusals = session_storage.cotenant_sids()
        assert protected == frozenset({"podsid01"})
        assert refusals == ()

    def test_a_shared_store_instance_with_mappings_refuses_the_whole_reclaim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-session protection is not enough for a genuine co-tenant.

        An instance without its own replay store can resume a session mid-move, so no
        ownership snapshot covers it and the reclaim refuses outright.
        """
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-legacy-shared"
        pod.mkdir(parents=True)
        # No `kiro/` directory: nothing shows this instance to be self-contained.
        (pod / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1": {"sid": "sharedsid1"}}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        _pin_a_fake_default_home(monkeypatch, tmp_path)

        reason = session_storage.reclaim_block_reason()
        assert "wt-legacy-shared" in reason
        assert "shares this replay store" in reason
        protected, refusals = session_storage.cotenant_sids()
        assert protected == frozenset({"sharedsid1"}), "still named, so still protected"
        assert [name for name, _why in refusals] == ["wt-legacy-shared"]

    def test_a_pod_that_left_only_a_directory_does_not_block_reclaiming(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A torn-down pod's leftover directory owns no sessions.

        Machines that run pods accumulate these; blocking on them would disable
        reclaiming for good.
        """
        pod_root = tmp_path / "pods"
        (pod_root / "wt-long-gone").mkdir(parents=True)
        # What an evicted pod leaves behind: its audit log, and no session map.
        (pod_root / "wt-long-gone" / "security_events.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        _pin_a_fake_default_home(monkeypatch, tmp_path)

        assert session_storage.reclaim_block_reason() == ""
        assert session_storage.cotenant_sids() == (frozenset(), ())

    def test_a_legacy_string_map_entry_still_protects_its_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain-string entry is the LEGACY format, and must not fail open.

        ``SessionMap._load`` still migrates ``{"key": "<sid>"}`` on read, so such a map
        is live data; the oldest co-tenants are the ones most likely to use it.
        """
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-legacy"
        pod.mkdir(parents=True)
        # Self-contained, so this isolates the entry-format question from the
        # shared-store refusal.
        (pod / "kiro" / "sessions" / "cli").mkdir(parents=True)
        (pod / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1": "legacysid01"}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        _pin_a_fake_default_home(monkeypatch, tmp_path)

        protected, refusals = session_storage.cotenant_sids()
        assert protected == frozenset({"legacysid01"})
        assert refusals == ()

    def test_a_cotenant_claiming_a_session_during_the_scan_is_refused(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Co-tenant ownership is re-read in the FINAL authority check.

        A co-tenant adopts an old replay log during the slow scan. The freshness floor
        cannot cover it: the adopted session is 40 days old.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "adopted001", log_bytes=64, age_days=40)

        calls = {"n": 0}

        def claimed_late(
            *, cached: bool = False
        ) -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
            calls["n"] += 1
            # Empty while the scan runs; owned by the time the move is authorized.
            return (frozenset() if calls["n"] == 1 else frozenset({"adopted001"})), ()

        monkeypatch.setattr(session_storage, "cotenant_sids", claimed_late)

        with pytest.raises(SessionStorageError, match="still in use"):
            session_storage.move_to_trash(["adopted001"], reason="manual", index=_index(), now=_NOW)
        assert (kiro_home / "sessions" / "cli" / "adopted001.jsonl").is_file()

    def test_a_cotenant_map_that_becomes_unreadable_mid_move_refuses(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ownership that stops being establishable cannot be worked around."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)

        calls = {"n": 0}

        def unreadable_late(
            *, cached: bool = False
        ) -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
            calls["n"] += 1
            return frozenset(), (() if calls["n"] == 1 else (("wt-corrupt", "unreadable map"),))

        monkeypatch.setattr(session_storage, "cotenant_sids", unreadable_late)

        with pytest.raises(SessionStorageError, match="make reclaiming unsafe"):
            session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    def test_a_symlinked_cotenant_map_is_refused_not_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pod root is writable, so its map must go through the file gate.

        ``hooks.safe_read_file`` re-checks the resolved target, so a link into a
        protected path is refused and the co-tenant reads as ownership-unknown.
        """
        secret = tmp_path / "aws-credentials"
        secret.write_text("[default]\naws_secret_" "access_key = shhh\n", encoding="utf-8")
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-symlinked"
        pod.mkdir(parents=True)
        (pod / "session_map.json").symlink_to(secret)
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))

        def refuse_everything(resolved: str) -> bool:
            return Path(resolved) == secret

        monkeypatch.setattr(session_storage.hooks, "is_sensitive_path", refuse_everything)

        protected, refusals = session_storage.cotenant_sids()

        assert protected == frozenset(), "nothing may be harvested from the target"
        assert [name for name, _why in refusals] == ["wt-symlinked"]
        # The reason tells refused-before-reading from a bare read that followed the
        # link and then failed to parse.
        assert refusals[0][1] == "its session map could not be read"
        assert "parsed" not in refusals[0][1]

    def test_a_map_that_is_not_valid_utf8_is_refused_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Undecodable bytes are an unreadable map, not an exception to the caller.

        ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``, so it could
        escape the refusal path as a 500.
        """
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-binary"
        pod.mkdir(parents=True)
        (pod / "session_map.json").write_bytes(b"\xff\xfe{\x00")
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))

        protected, refusals = session_storage.cotenant_sids()

        assert protected == frozenset()
        assert [name for name, _why in refusals] == ["wt-binary"]
        # Undecodable is a failure to READ, not to parse: the bytes never became
        # text, so no parse was ever attempted.
        assert refusals[0][1] == "its session map could not be read"

    def test_a_pod_whose_map_cannot_be_read_still_blocks_and_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown ownership is the one case that must still refuse.

        A map that is present but unparseable says a co-tenant claims SOMETHING
        without saying what, which is exactly when reclaiming cannot be made safe.
        """
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-corrupt"
        pod.mkdir(parents=True)
        (pod / "session_map.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        _pin_a_fake_default_home(monkeypatch, tmp_path)

        reason = session_storage.reclaim_block_reason()
        assert "make reclaiming unsafe" in reason
        assert "wt-corrupt" in reason
        assert "could not be parsed" in reason

    def test_a_cotenant_session_is_refused_like_a_mapped_one(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The protection has to bite at the move, not only in the report.

        Patched at :func:`cotenant_sids` because the *stores* fixture isolates both
        homes, where pod discovery is correctly skipped.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "podsid01", log_bytes=64, age_days=40)
        monkeypatch.setattr(
            session_storage,
            "cotenant_sids",
            lambda *, cached=False: (frozenset({"podsid01"}), ()),
        )

        with pytest.raises(SessionStorageError, match="still in use"):
            session_storage.move_to_trash(["podsid01"], reason="manual", index=_index(), now=_NOW)
        assert (kiro_home / "sessions" / "cli" / "podsid01.jsonl").is_file()

    def test_no_pods_leaves_the_default_instance_able_to_reclaim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A normal single install must not be refused by the co-tenant check."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "no-pods-here"))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        _pin_a_fake_default_home(monkeypatch, tmp_path)

        assert session_storage.reclaim_block_reason() == ""

    def test_a_rollback_does_not_overwrite_a_recreated_origin(self, tmp_path: Path) -> None:
        """A rollback runs after a failure, so the origin may be back and newer."""
        landed = tmp_path / "landed.jsonl"
        landed.write_bytes(b"staged copy")
        origin = tmp_path / "origin.jsonl"
        origin.write_bytes(b"newer generation")

        session_storage._rollback([(landed, origin)])

        assert origin.read_bytes() == b"newer generation"
        assert landed.is_file(), "the staged copy must stay recoverable"

    def test_a_trash_root_linked_inside_the_home_reaches_nothing(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The anchor alone permits this: the link points INSIDE the data home.

        Pointing the root at the live archive tree satisfies both the data-home
        anchor and per-batch containment, so `empty` would delete real session data.
        """
        crew_home, _ = stores
        archive = crew_home / "sessions" / "archive"
        victim = archive / "20260101T000000-victim01"
        victim.mkdir(parents=True)
        (victim / "dashboard_chat-1__20260101-000000.jsonl").write_bytes(b"history")

        root = session_storage.trash_root()
        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            root.symlink_to(archive, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        assert session_storage.list_trash() == []
        with pytest.raises(SessionStorageError, match="trash root is a link"):
            session_storage.empty_trash(["20260101T000000-victim01"])
        assert session_storage.empty_trash() == 0
        assert (victim / "dashboard_chat-1__20260101-000000.jsonl").is_file()

    def test_a_relocated_trash_root_reaches_nothing(self, stores: tuple[Path, Path]) -> None:
        """A linked ANCESTOR escapes the home, which the link test cannot see.

        `is_link_or_junction(root)` inspects only the final component, so a link one
        level up leaves the root a real directory while relocating it. Resolving and
        anchoring to the data home is what catches that.
        """
        crew_home, _ = stores
        outside = crew_home.parent / "not-ours"
        victim = outside / "sessions" / "20260101T000000-victim01"
        victim.mkdir(parents=True)
        (victim / "keep.txt").write_bytes(b"precious")
        # A self-consistent manifest, so the unmanifested-file guard does not refuse
        # the delete before the containment anchor under test does.
        header = {
            "schema": session_storage.MANIFEST_SCHEMA,
            "batch_id": "20260101T000000-victim01",
            "created_at": _NOW,
            "reason": "manual",
        }
        entry = {
            "uid": "aaaa1111",
            "sid": "aaaa1111",
            "files": [{"rel": "keep.txt", "origin": "/nowhere.jsonl", "bytes": 8}],
        }
        (victim / session_storage.MANIFEST_NAME).write_text(
            json.dumps(header) + "\n" + json.dumps(entry) + "\n"
        )

        root = session_storage.trash_root()
        # Link the PARENT, so the root is a real directory that escapes only once resolved.
        try:
            root.parent.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        # Nothing outside the data home is offered as a batch...
        assert session_storage.list_trash() == []
        # ...and naming one explicitly is refused rather than deleted.
        with pytest.raises(SessionStorageError, match="does not live under the data"):
            session_storage.empty_trash(["20260101T000000-victim01"])
        assert session_storage.empty_trash() == 0
        assert (victim / "keep.txt").is_file()

    def test_the_batch_link_test_covers_junctions(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """is_symlink() is False for an NTFS junction, so the guard must not use it.

        Linux cannot create a junction, so this pins that the guard goes through the
        junction-aware resolver: when it reports a link, the batch is refused.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        # Only the BATCH reads as a link. Reporting one for the root too would trip
        # the separate root check and prove nothing about this guard.
        monkeypatch.setattr(
            session_storage.platform_compat,
            "is_link_or_junction",
            lambda p: Path(p).name == batch.batch_id,
        )

        with pytest.raises(SessionStorageError, match="not a valid batch id"):
            session_storage.empty_trash([batch.batch_id])
        assert session_storage.list_trash() == []
        # Nothing was destroyed by the refusal.
        assert (session_storage.trash_root() / batch.batch_id).is_dir()

    def test_a_batch_link_pointing_at_another_batch_is_refused(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Containment alone permits this: the link resolves INSIDE the root.

        Without the link check, emptying the alias would destroy the batch it
        points at — a real batch the user never named.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        root = session_storage.trash_root()
        alias = root / "20260101T000000-aliased1"
        try:
            alias.symlink_to(root / batch.batch_id, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with pytest.raises(SessionStorageError, match="not a valid batch id"):
            session_storage.empty_trash(["20260101T000000-aliased1"])

        # The real batch survives and is still restorable.
        assert [b.batch_id for b in session_storage.list_trash()] == [batch.batch_id]

    def test_a_symlinked_batch_is_not_restorable(self, stores: tuple[Path, Path]) -> None:
        """A link's NAME passes the id check; following it would escape the trash."""
        outside = stores[0].parent / "outside"
        outside.mkdir(exist_ok=True)
        root = session_storage.trash_root()
        root.mkdir(parents=True, exist_ok=True)
        try:
            (root / "20260101T000000-deadbeef").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with pytest.raises(SessionStorageError, match="not a valid batch id"):
            session_storage.restore("20260101T000000-deadbeef")

        # The link's target is untouched — refusing is not a partial operation.
        assert outside.is_dir()

    def test_a_rejected_kiro_home_does_not_look_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unsafe override is silently rejected, so presence proves nothing."""
        crew_home = tmp_path / "crew3"
        (crew_home / "sessions").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        # A filesystem root is refused on every platform; /etc would resolve to an
        # accepted C:\etc on Windows.
        monkeypatch.setenv("KIRO_HOME", "/")

        assert session_storage.reclaim_block_reason() != ""

    def test_the_refresh_authority_check_runs_after_the_scan(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The scan is the slow part; a check before it is stale by move time."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        order: list[str] = []
        real_scan = session_storage._scan_units

        def watched_scan(index: SessionIndex, **kwargs):
            order.append("scan")
            return real_scan(index, **kwargs)

        def refresh() -> SessionIndex:
            order.append("refresh")
            return _index({"aaaa1111": "dashboard_chat-1"}, active={"aaaa1111"})

        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(session_storage, "_scan_units", watched_scan)
            with pytest.raises(SessionStorageError, match="still in use"):
                session_storage.move_to_trash(
                    ["aaaa1111"],
                    reason="manual",
                    index=_index(),
                    now=_NOW,
                    refresh=refresh,
                )

        # The refresh must be the LAST thing before the move, i.e. after the scan.
        assert order == ["scan", "refresh"]
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    def test_a_freshly_mapped_session_is_protected_at_move_time(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The scan can take minutes; a session mapped meanwhile must not move."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        stale = _index()  # nothing mapped when the selection was computed

        def refresh() -> SessionIndex:
            # By the time the lock is held, the session has been resumed.
            return _index({"aaaa1111": "dashboard_chat-1"}, active={"aaaa1111"})

        with pytest.raises(SessionStorageError, match="still in use"):
            session_storage.move_to_trash(
                ["aaaa1111"], reason="manual", index=stale, now=_NOW, refresh=refresh
            )

        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    def test_a_failed_re_read_refuses_rather_than_proceeding(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Unable to confirm who is live is not permission to guess."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)

        def broken() -> SessionIndex:
            raise RuntimeError("session map unreadable")

        with pytest.raises(SessionStorageError, match="could not confirm"):
            session_storage.move_to_trash(
                ["aaaa1111"], reason="manual", index=_index(), now=_NOW, refresh=broken
            )

        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    def test_the_report_names_the_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client must be able to explain instead of offering a doomed button."""
        crew_home = tmp_path / "crew2"
        (crew_home / "sessions").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        monkeypatch.delenv("KIRO_HOME", raising=False)

        report = session_storage.measure(_index(), now=_NOW)

        assert "sits outside it" in report.reclaim_blocked_reason
