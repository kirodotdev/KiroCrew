"""``mcp_gateway.prewarm`` — the hot-key store's observation/eviction logic and
the prewarm pass itself.

``test_mcp_gateway_prewarm.py`` covers only ``flush``'s platform seam. Everything
else in this module is unobserved, and it is the part that decides WHICH backends
get warmed:

* the warm-pool hit tally (``record_outcome`` / ``hit_stats``), including the
  no-registers-yet rate, since a divide there would crash the stats frame;
* ``record``'s repeat path and its RAM ceiling — the in-memory prune is the only
  thing bounding ``_entries`` between flushes;
* ``load``: absent file, unreadable file, corrupt JSON, non-list records, records
  that are not dicts, a corrupt register, a TTL-expired key, restored totals and
  malformed totals, and the load-time cap;
* ``top_register_payloads`` ordering (hits, then recency) and its ``count <= 0``
  guard;
* ``prewarm_from_payloads``: the empty/limit guards, a malformed payload being
  skipped, ``acquire`` returning ``None`` (which must NOT be counted as warmed),
  ``acquire`` raising, the pin, and the unreserve callback.

No sleeps and no real time dependence: every ``last_seen`` is written explicitly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.mcp_gateway.prewarm import (
    HOT_KEYS_FILENAME,
    HotKeyStore,
    default_hot_keys_path,
    prewarm_from_payloads,
)

pytestmark = pytest.mark.xdist_group("mcp_gateway")


def _register(server: str = "test-mcp", agent: str = "test-agent") -> dict[str, Any]:
    """A payload complete enough for ``PoolKey.from_register``; anything less is
    silently dropped by ``record`` and would make the assertions vacuous."""
    return {
        "type": "register",
        "stub_uuid": "stub-1",
        "server_name": server,
        "agent_name": agent,
        "command_args_hash": "a" * 64,
        "effective_env_hash": "b" * 64,
        "work_dir": "/tmp",
        "binary_version": "deadbeef",
        "os_uid": 1000,
        "sandbox_mode": "standard",
        "autoapprove_set_hash": "c" * 64,
        "approval_mode": "interactive",
        "trust_all_tools": False,
        "config_snapshot_hash": "d" * 64,
    }


def _write(path: Path, records: Any, totals: Any = None) -> None:
    body: dict[str, Any] = {"version": 1, "keys": records}
    if totals is not None:
        body["totals"] = totals
    path.write_text(json.dumps(body), encoding="utf-8")


class TestPathAndDefault:
    def test_path_property_reports_the_file_it_was_built_with(self, tmp_path: Path) -> None:
        store = HotKeyStore(tmp_path / "hot-keys.json")
        assert store.path == tmp_path / "hot-keys.json"

    def test_default_path_is_the_socket_sibling(self, tmp_path: Path) -> None:
        assert default_hot_keys_path(tmp_path / "gateway.sock") == (tmp_path / HOT_KEYS_FILENAME)


class TestHitTally:
    def test_no_registers_yet_reports_a_zero_rate(self, tmp_path: Path) -> None:
        store = HotKeyStore(tmp_path / "hot-keys.json")
        assert store.hit_stats() == {
            "warm_pool_hits": 0,
            "warm_pool_misses": 0,
            "warm_pool_hit_rate_pct": 0,
        }

    def test_hits_and_misses_are_tallied_separately(self, tmp_path: Path) -> None:
        store = HotKeyStore(tmp_path / "hot-keys.json")
        store.record_outcome(hit=True)
        store.record_outcome(hit=True)
        store.record_outcome(hit=False)
        stats = store.hit_stats()
        assert stats["warm_pool_hits"] == 2
        assert stats["warm_pool_misses"] == 1
        assert stats["warm_pool_hit_rate_pct"] == 67

    def test_an_outcome_marks_the_store_dirty_so_it_is_persisted(self, tmp_path: Path) -> None:
        store = HotKeyStore(tmp_path / "hot-keys.json")
        store.record_outcome(hit=False)
        assert store.flush() is True
        payload = json.loads((tmp_path / "hot-keys.json").read_text(encoding="utf-8"))
        assert payload["totals"] == {"hits": 0, "misses": 1}


class TestRecord:
    def test_a_repeat_register_bumps_the_existing_entry(self, tmp_path: Path) -> None:
        store = HotKeyStore(tmp_path / "hot-keys.json")
        store.record(_register())
        store.record(_register())
        store.flush()
        payload = json.loads((tmp_path / "hot-keys.json").read_text(encoding="utf-8"))
        assert len(payload["keys"]) == 1
        assert payload["keys"][0]["hits"] == 2

    def test_a_malformed_payload_is_dropped_not_raised(self, tmp_path: Path) -> None:
        store = HotKeyStore(tmp_path / "hot-keys.json")
        store.record({"type": "register"})  # nothing PoolKey can parse
        assert store.flush() is False, "a dropped payload must not dirty the store"

    def test_crossing_the_high_water_mark_prunes_in_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flush slice bounds only the FILE, so without the in-memory prune
        ``_entries`` grows with every distinct key the daemon ever sees."""
        import kiro_crew.mcp_gateway.prewarm as prewarm_mod

        monkeypatch.setattr(prewarm_mod, "_MAX_TRACKED_KEYS", 3)
        monkeypatch.setattr(prewarm_mod, "_PRUNE_HIGH_WATER", 5)
        store = HotKeyStore(tmp_path / "hot-keys.json")
        for i in range(7):
            store.record(_register(server=f"srv-{i}"))
        store.flush()
        payload = json.loads((tmp_path / "hot-keys.json").read_text(encoding="utf-8"))
        assert len(payload["keys"]) <= 3

    def test_the_prune_drops_ttl_stale_keys_before_cold_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.mcp_gateway.prewarm as prewarm_mod

        monkeypatch.setattr(prewarm_mod, "_MAX_TRACKED_KEYS", 2)
        monkeypatch.setattr(prewarm_mod, "_PRUNE_HIGH_WATER", 3)
        store = HotKeyStore(tmp_path / "hot-keys.json")
        for i in range(3):
            store.record(_register(server=f"srv-{i}"))
        # Age one entry past the TTL, then trip the prune with a fourth key.
        entries = list(store._entries.values())
        entries[0].last_seen = time.time() - (prewarm_mod._MAX_KEY_AGE_SECS + 60)
        stale_register = entries[0].register
        store.record(_register(server="srv-fresh"))
        remaining = [e.register for e in store._entries.values()]
        assert stale_register not in remaining


class TestTopRegisterPayloads:
    def test_zero_or_negative_count_returns_nothing(self, tmp_path: Path) -> None:
        store = HotKeyStore(tmp_path / "hot-keys.json")
        store.record(_register())
        assert store.top_register_payloads(0) == []
        assert store.top_register_payloads(-1) == []

    def test_hottest_first_then_most_recent(self, tmp_path: Path) -> None:
        store = HotKeyStore(tmp_path / "hot-keys.json")
        store.record(_register(server="cold"))
        store.record(_register(server="hot"))
        store.record(_register(server="hot"))
        top = store.top_register_payloads(2)
        assert [r["server_name"] for r in top] == ["hot", "cold"]

    def test_the_count_is_a_ceiling(self, tmp_path: Path) -> None:
        store = HotKeyStore(tmp_path / "hot-keys.json")
        store.record(_register(server="a"))
        store.record(_register(server="b"))
        assert len(store.top_register_payloads(1)) == 1


class TestLoad:
    def test_a_missing_file_yields_an_empty_store(self, tmp_path: Path) -> None:
        store = HotKeyStore(tmp_path / "absent.json")
        store.load()
        assert store.top_register_payloads(5) == []

    def test_an_unreadable_path_is_logged_not_raised(self, tmp_path: Path) -> None:
        """A directory in the file's place raises ``IsADirectoryError`` (an OSError
        that is not ``FileNotFoundError``) — the branch that must degrade."""
        target = tmp_path / "hot-keys.json"
        target.mkdir()
        store = HotKeyStore(target)
        store.load()
        assert store.top_register_payloads(5) == []

    def test_corrupt_json_yields_an_empty_store(self, tmp_path: Path) -> None:
        target = tmp_path / "hot-keys.json"
        target.write_text("{not json", encoding="utf-8")
        store = HotKeyStore(target)
        store.load()
        assert store.top_register_payloads(5) == []

    def test_a_dict_without_a_keys_member_is_treated_as_corrupt(self, tmp_path: Path) -> None:
        target = tmp_path / "hot-keys.json"
        target.write_text(json.dumps({"version": 1}), encoding="utf-8")
        store = HotKeyStore(target)
        store.load()
        assert store.top_register_payloads(5) == []

    def test_a_bare_list_file_is_accepted(self, tmp_path: Path) -> None:
        target = tmp_path / "hot-keys.json"
        target.write_text(
            json.dumps([{"register": _register(), "hits": 4, "last_seen": time.time()}]),
            encoding="utf-8",
        )
        store = HotKeyStore(target)
        store.load()
        assert len(store.top_register_payloads(5)) == 1

    def test_records_that_are_not_a_list_are_ignored(self, tmp_path: Path) -> None:
        target = tmp_path / "hot-keys.json"
        _write(target, {"not": "a list"})
        store = HotKeyStore(target)
        store.load()
        assert store.top_register_payloads(5) == []

    @pytest.mark.parametrize(
        "record",
        [
            "a string, not a dict",
            {"register": "not a dict"},
            {"register": {"type": "register"}},  # PoolKey cannot parse it
            {"register": _register(), "hits": "not a number", "last_seen": 0.0},
        ],
    )
    def test_a_corrupt_record_is_skipped_not_raised(self, tmp_path: Path, record: Any) -> None:
        target = tmp_path / "hot-keys.json"
        _write(target, [record])
        store = HotKeyStore(target)
        store.load()
        assert store.top_register_payloads(5) == []

    def test_a_ttl_expired_key_is_not_loaded(self, tmp_path: Path) -> None:
        import kiro_crew.mcp_gateway.prewarm as prewarm_mod

        target = tmp_path / "hot-keys.json"
        stale = time.time() - (prewarm_mod._MAX_KEY_AGE_SECS + 3600)
        _write(target, [{"register": _register(), "hits": 9, "last_seen": stale}])
        store = HotKeyStore(target)
        store.load()
        assert store.top_register_payloads(5) == []

    def test_totals_are_restored(self, tmp_path: Path) -> None:
        target = tmp_path / "hot-keys.json"
        _write(
            target,
            [{"register": _register(), "hits": 2, "last_seen": time.time()}],
            totals={"hits": 7, "misses": 3},
        )
        store = HotKeyStore(target)
        store.load()
        assert store.hit_stats()["warm_pool_hits"] == 7
        assert store.hit_stats()["warm_pool_misses"] == 3
        assert store.hit_stats()["warm_pool_hit_rate_pct"] == 70

    def test_malformed_totals_reset_to_zero_rather_than_raising(self, tmp_path: Path) -> None:
        target = tmp_path / "hot-keys.json"
        _write(target, [], totals={"hits": "seven", "misses": None})
        store = HotKeyStore(target)
        store.load()
        assert store.hit_stats()["warm_pool_hits"] == 0
        assert store.hit_stats()["warm_pool_misses"] == 0

    def test_negative_persisted_totals_are_clamped(self, tmp_path: Path) -> None:
        target = tmp_path / "hot-keys.json"
        _write(target, [], totals={"hits": -5, "misses": -1})
        store = HotKeyStore(target)
        store.load()
        assert store.hit_stats()["warm_pool_hits"] == 0

    def test_a_bloated_file_is_capped_at_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.mcp_gateway.prewarm as prewarm_mod

        monkeypatch.setattr(prewarm_mod, "_MAX_TRACKED_KEYS", 2)
        now = time.time()
        target = tmp_path / "hot-keys.json"
        _write(
            target,
            [
                {"register": _register(server=f"s{i}"), "hits": i + 1, "last_seen": now}
                for i in range(6)
            ],
        )
        store = HotKeyStore(target)
        store.load()
        assert len(store.top_register_payloads(99)) == 2


class _FakeBackend:
    def __init__(self) -> None:
        self.pinned = False


class TestPrewarmFromPayloads:
    @pytest.mark.asyncio
    async def test_no_payloads_warms_nothing(self) -> None:
        async def _acquire(_key: PoolKey) -> Any:
            raise AssertionError("acquire must not be called")

        assert await prewarm_from_payloads([], _acquire, limit=3) == 0

    @pytest.mark.asyncio
    async def test_a_non_positive_limit_warms_nothing(self) -> None:
        async def _acquire(_key: PoolKey) -> Any:
            raise AssertionError("acquire must not be called")

        assert await prewarm_from_payloads([_register()], _acquire, limit=0) == 0

    @pytest.mark.asyncio
    async def test_a_warmed_backend_is_pinned_and_unreserved(self) -> None:
        backend = _FakeBackend()
        seen: list[str] = []
        unreserved: list[str] = []

        async def _acquire(key: PoolKey) -> Any:
            seen.append(key.server_name)
            return backend

        warmed = await prewarm_from_payloads(
            [_register(server="warm-me")],
            _acquire,
            limit=5,
            unreserve=lambda key: unreserved.append(key.server_name),
        )
        assert warmed == 1
        assert seen == ["warm-me"]
        assert unreserved == ["warm-me"]
        assert backend.pinned is True

    @pytest.mark.asyncio
    async def test_the_limit_caps_how_many_are_warmed(self) -> None:
        calls: list[str] = []

        async def _acquire(key: PoolKey) -> Any:
            calls.append(key.server_name)
            return _FakeBackend()

        warmed = await prewarm_from_payloads(
            [_register(server="a"), _register(server="b"), _register(server="c")],
            _acquire,
            limit=2,
        )
        assert warmed == 2
        assert calls == ["a", "b"]

    @pytest.mark.asyncio
    async def test_a_malformed_payload_is_skipped(self) -> None:
        async def _acquire(_key: PoolKey) -> Any:
            return _FakeBackend()

        warmed = await prewarm_from_payloads(
            [{"type": "register"}, _register(server="ok")], _acquire, limit=5
        )
        assert warmed == 1

    @pytest.mark.asyncio
    async def test_acquire_returning_none_is_not_counted_as_warmed(self) -> None:
        """Counting it would overstate the live warm set in the reported total."""

        async def _acquire(_key: PoolKey) -> Any:
            return None

        assert await prewarm_from_payloads([_register()], _acquire, limit=5) == 0

    @pytest.mark.asyncio
    async def test_one_spawn_failure_does_not_abort_the_pass(self) -> None:
        async def _acquire(key: PoolKey) -> Any:
            if key.server_name == "boom":
                raise RuntimeError("spawn failed")
            return _FakeBackend()

        warmed = await prewarm_from_payloads(
            [_register(server="boom"), _register(server="fine")], _acquire, limit=5
        )
        assert warmed == 1

    @pytest.mark.asyncio
    async def test_a_backend_that_cannot_be_pinned_is_still_warmed(self) -> None:
        """A test double / resolver may return an object with no ``pinned``
        attribute; that must not abort the pass."""

        class _NoPin:
            __slots__ = ()

        async def _acquire(_key: PoolKey) -> Any:
            return _NoPin()

        assert await prewarm_from_payloads([_register()], _acquire, limit=5) == 1
