"""The durable Teams conversation -> serviceUrl store.

The Bot Framework gives a bot no way to look up where a conversation can be
reached: the ``serviceUrl`` arrives on an inbound activity and the bot must
remember it. So this store is what decides whether a proactive send (a cron
result, a subagent-completion notice, a dashboard mirror leg) has anywhere to go
after a restart, and every test here is about that guarantee or about the store
failing safely rather than taking delivery down with it.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.teams.service_urls import ServiceUrlStore

_SVC = "https://smba.trafficmanager.net/teams"


def _store(tmp_path, name: str = "teams_service_urls.json") -> ServiceUrlStore:
    return ServiceUrlStore(path=tmp_path / name)


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_a_remembered_conversation_survives_a_restart(self, tmp_path) -> None:
        store = _store(tmp_path)
        await store.ensure_loaded()
        assert store.remember("conv-1", _SVC, identity="Me@Example.com") is True
        await store.flush()

        # A fresh instance stands in for the next gateway process.
        reloaded = _store(tmp_path)
        await reloaded.ensure_loaded()

        assert reloaded.get("conv-1") == _SVC
        # Identity lookup is case-insensitive: the allow-list and the activity
        # can disagree on case for the same person.
        assert reloaded.conversation_for("me@example.com") == "conv-1"

    @pytest.mark.asyncio
    async def test_an_unchanged_serviceurl_reports_no_write_needed(self, tmp_path) -> None:
        """The inbound path calls this per message; the common case must be free.

        A conversation's serviceUrl is stable for its lifetime, so re-remembering
        it must not mark the store dirty and trigger a filesystem write on every
        single message.
        """
        store = _store(tmp_path)
        await store.ensure_loaded()
        assert store.remember("conv-1", _SVC, identity="me@example.com") is True
        await store.flush()

        assert store.remember("conv-1", _SVC, identity="me@example.com") is False

    @pytest.mark.asyncio
    async def test_a_live_value_is_not_overwritten_by_a_stale_file(self, tmp_path) -> None:
        """An inbound activity is fresher than anything on disk."""
        path = tmp_path / "teams_service_urls.json"
        path.write_text(
            json.dumps(
                {
                    "conversations": {
                        "conv-1": {"service_url": "https://old.example.com/", "seen_at": 1.0}
                    }
                }
            ),
            encoding="utf-8",
        )
        store = ServiceUrlStore(path=path)
        store.remember("conv-1", _SVC)

        await store.ensure_loaded()

        assert store.get("conv-1") == _SVC

    @pytest.mark.asyncio
    async def test_nothing_is_written_when_nothing_changed(self, tmp_path) -> None:
        store = _store(tmp_path)
        await store.ensure_loaded()
        await store.flush()

        assert not (tmp_path / "teams_service_urls.json").exists()


class TestFailingSafe:
    """A lost routing hint must never stop message delivery."""

    @pytest.mark.asyncio
    async def test_a_missing_file_is_simply_empty(self, tmp_path) -> None:
        store = _store(tmp_path, "absent.json")
        await store.ensure_loaded()

        assert store.get("conv-1") == ""

    @pytest.mark.asyncio
    async def test_corrupt_json_degrades_to_empty_rather_than_raising(self, tmp_path) -> None:
        path = tmp_path / "teams_service_urls.json"
        path.write_text("{not json at all", encoding="utf-8")
        store = ServiceUrlStore(path=path)

        await store.ensure_loaded()

        assert store.get("conv-1") == ""

    @pytest.mark.asyncio
    async def test_a_non_https_serviceurl_does_not_survive_a_reload(self, tmp_path) -> None:
        """The outbound path carries the app credential and refuses plain http.

        Keeping such a row would only defer the refusal to send time, so it is
        dropped at load.
        """
        path = tmp_path / "teams_service_urls.json"
        path.write_text(
            json.dumps(
                {
                    "conversations": {
                        "conv-1": {"service_url": "http://evil.example/", "seen_at": 1.0}
                    },
                    "identities": {"me@example.com": "conv-1"},
                }
            ),
            encoding="utf-8",
        )
        store = ServiceUrlStore(path=path)

        await store.ensure_loaded()

        assert store.get("conv-1") == ""
        # The identity row is dropped with it: advertising a target with no route
        # would make the dashboard offer a destination that cannot be reached.
        assert store.conversation_for("me@example.com") == ""

    @pytest.mark.asyncio
    async def test_an_identity_without_a_surviving_conversation_is_dropped(self, tmp_path) -> None:
        path = tmp_path / "teams_service_urls.json"
        path.write_text(
            json.dumps({"conversations": {}, "identities": {"me@example.com": "gone"}}),
            encoding="utf-8",
        )
        store = ServiceUrlStore(path=path)

        await store.ensure_loaded()

        assert store.conversation_for("me@example.com") == ""

    @pytest.mark.asyncio
    async def test_an_empty_conversation_or_url_is_ignored(self, tmp_path) -> None:
        store = _store(tmp_path)
        await store.ensure_loaded()

        assert store.remember("", _SVC) is False
        assert store.remember("conv-1", "") is False


class TestBounded:
    @pytest.mark.asyncio
    async def test_the_store_cannot_grow_without_limit(self, tmp_path, monkeypatch) -> None:
        """A long-lived gateway must not accumulate conversations forever."""
        monkeypatch.setattr("kiro_crew.teams.service_urls._MAX_ENTRIES", 3)
        store = _store(tmp_path)
        await store.ensure_loaded()

        for index in range(6):
            store.remember(f"conv-{index}", f"{_SVC}/{index}", identity=f"u{index}@x.com")

        # The newest survive; the oldest were evicted with their identity rows.
        assert store.get("conv-5") == f"{_SVC}/5"
        assert store.get("conv-3") == f"{_SVC}/3"
        assert store.get("conv-2") == ""
        assert store.get("conv-0") == ""
        assert store.conversation_for("u0@x.com") == ""

    @pytest.mark.asyncio
    async def test_an_oversize_file_is_capped_at_load(self, tmp_path, monkeypatch) -> None:
        """A file that arrived oversize must not be adopted whole.

        Loading N rows and then evicting down to the cap does that work on the
        event loop, so the cap belongs on the load path too. Keeps the NEWEST
        rows, which are the ones a proactive send is most likely to need.
        """
        monkeypatch.setattr("kiro_crew.teams.service_urls._MAX_ENTRIES", 2)
        path = tmp_path / "teams_service_urls.json"
        path.write_text(
            json.dumps(
                {
                    "conversations": {
                        f"conv-{i}": {"service_url": f"{_SVC}/{i}", "seen_at": float(i)}
                        for i in range(10)
                    },
                    "identities": {f"u{i}@x.com": f"conv-{i}" for i in range(10)},
                }
            ),
            encoding="utf-8",
        )
        store = ServiceUrlStore(path=path)

        await store.ensure_loaded()

        assert store.get("conv-9") == f"{_SVC}/9"
        assert store.get("conv-8") == f"{_SVC}/8"
        assert store.get("conv-7") == ""
        assert store.get("conv-0") == ""
        # An identity whose conversation was dropped goes with it, so no target is
        # advertised without a route.
        assert store.conversation_for("u0@x.com") == ""
        assert store.conversation_for("u9@x.com") == "conv-9"


class TestAFailedWriteStaysRetryable:
    """A transient write failure must not end the retries.

    ``remember`` marks the map only when something CHANGES, and a conversation's
    serviceUrl is stable for its lifetime -- so if one failed flush left the dirty flag
    clear, the common case would never mark it again, nothing would ever flush, and the
    restart this store exists to survive would lose every proactive destination anyway.
    """

    @pytest.mark.asyncio
    async def test_a_failed_flush_is_retried_by_the_next_one(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        store = ServiceUrlStore()
        await store.ensure_loaded()
        assert store.remember("conv-1", "https://smba.trafficmanager.net/amer/")

        attempts: list[int] = []
        real_write = store._write

        def _fail_once(payload: dict) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("disk full")
            real_write(payload)

        monkeypatch.setattr(store, "_write", _fail_once)
        await store.flush()
        assert len(attempts) == 1, "the first write was attempted and failed"

        # Nothing changed in between -- exactly the case that would never re-mark.
        await store.flush()

        assert len(attempts) == 2, "the failed write must still be pending"
        fresh = ServiceUrlStore()
        await fresh.ensure_loaded()
        assert fresh.get("conv-1") == "https://smba.trafficmanager.net/amer/"

    @pytest.mark.asyncio
    async def test_a_successful_flush_does_not_rewrite_forever(self, tmp_path, monkeypatch) -> None:
        """The re-mark is for FAILURE only; a clean flush must settle."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        store = ServiceUrlStore()
        await store.ensure_loaded()
        store.remember("conv-1", "https://smba.trafficmanager.net/amer/")
        writes: list[int] = []
        real_write = store._write
        monkeypatch.setattr(store, "_write", lambda p: (writes.append(1), real_write(p))[1])

        await store.flush()
        await store.flush()

        assert len(writes) == 1
