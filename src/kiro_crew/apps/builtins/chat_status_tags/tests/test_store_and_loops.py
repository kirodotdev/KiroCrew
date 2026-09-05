"""Coverage for the in-process store facade and the loop bodies.

The store is the app's only bridge to the gateway; these tests pin its two
failure modes (no bound state, no serving loop), the read copies, and that
every write is marshalled onto the serving loop with the exact semantics the
dashboard handlers have (invalid/duplicate tag ids dropped, missing slot a
no-op). The loop tests drive one real iteration of each pass with a stubbed
store, so the sweep/resume decision logic runs for real rather than being
stubbed out.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from kiro_crew.apps.builtins.chat_status_tags import hooks, logic
from kiro_crew.apps.builtins.chat_status_tags.store import (
    GatewayUnavailable,
    TagsStore,
    _run_on_loop,
    _state,
)


class _BackgroundLoop:
    """A real event loop on a worker thread, standing in for the serving loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


def _bound_state(**attrs: object) -> SimpleNamespace:
    """A fake DashboardState shaped like what the store reaches for."""
    return SimpleNamespace(**attrs)


def _patch_registry(state: object | None):
    """Route ``store._state()`` at a fake registry carrying *state*."""
    reg = None if state is None else SimpleNamespace(_app={"state": state})
    return patch("kiro_crew.apps.hooks_integration.get_route_registry", return_value=reg)


class TestStateResolution(unittest.TestCase):
    def test_no_registry_raises(self) -> None:
        with _patch_registry(None), self.assertRaises(GatewayUnavailable):
            _state()

    def test_registry_without_state_raises(self) -> None:
        reg = SimpleNamespace(_app={})
        with (
            patch("kiro_crew.apps.hooks_integration.get_route_registry", return_value=reg),
            self.assertRaises(GatewayUnavailable),
        ):
            _state()

    def test_bound_state_is_returned(self) -> None:
        state = _bound_state()
        with _patch_registry(state):
            self.assertIs(_state(), state)


class TestRunOnLoop(unittest.TestCase):
    def test_missing_loop_raises(self) -> None:
        async def _noop() -> None:
            pass

        coro = _noop()
        try:
            with self.assertRaises(GatewayUnavailable):
                _run_on_loop(_bound_state(serving_loop=None), coro)
        finally:
            coro.close()

    def test_closed_loop_raises(self) -> None:
        loop = asyncio.new_event_loop()
        loop.close()

        async def _noop() -> None:
            pass

        coro = _noop()
        try:
            with self.assertRaises(GatewayUnavailable):
                _run_on_loop(_bound_state(serving_loop=loop), coro)
        finally:
            coro.close()

    def test_runs_coro_on_live_loop(self) -> None:
        bg = _BackgroundLoop()
        try:

            async def _answer() -> int:
                return 42

            self.assertEqual(_run_on_loop(_bound_state(serving_loop=bg.loop), _answer()), 42)
        finally:
            bg.close()


class TestReads(unittest.TestCase):
    def test_list_tags_returns_copies(self) -> None:
        tag = {"id": "t1", "name": "done"}
        with _patch_registry(_bound_state(_tags=[tag])):
            out = TagsStore().list_tags()
        self.assertEqual(out, [tag])
        out[0]["name"] = "mutated"
        self.assertEqual(tag["name"], "done")

    def test_list_slots_delegates_to_serializer(self) -> None:
        payload = [{"key": "chat-1", "tags": []}]
        state = _bound_state(serialize_slots=lambda: payload)
        with _patch_registry(state):
            self.assertEqual(TagsStore().list_slots(), payload)

    def test_slot_messages_missing_slot_is_empty(self) -> None:
        with _patch_registry(_bound_state(_slots={})):
            self.assertEqual(TagsStore().slot_messages("nope", 5), [])

    def test_slot_messages_returns_tail_copies(self) -> None:
        msgs = [{"role": "user", "content": str(i)} for i in range(10)]
        slot = SimpleNamespace(messages=msgs)
        with _patch_registry(_bound_state(_slots={"chat-1": slot})):
            out = TagsStore().slot_messages("chat-1", 3)
        self.assertEqual([m["content"] for m in out], ["7", "8", "9"])
        out[0]["content"] = "mutated"
        self.assertEqual(msgs[7]["content"], "7")


class TestWrites(unittest.TestCase):
    def setUp(self) -> None:
        self.bg = _BackgroundLoop()
        self.addCleanup(self.bg.close)

    def test_create_tag_marshals_onto_loop(self) -> None:
        created = {"id": "t9", "name": "stuck"}

        async def _fake_create(state: object, name: str, color: str, *, status: bool) -> dict:
            return created

        state = _bound_state(serving_loop=self.bg.loop)
        with (
            _patch_registry(state),
            patch("kiro_crew.dashboard.chat_tags.create_tag_definition_off_loop", _fake_create),
        ):
            self.assertEqual(TagsStore().create_tag("stuck", "amber", status=False), created)

    def test_merge_slot_tags_merges_against_live_list(self) -> None:
        # The slot's LIVE tags at write time differ from any snapshot a caller
        # took: a user added "user-tag" meanwhile. The merge must preserve it,
        # replace only the managed subset, and drop want-ids the vocabulary
        # does not know.
        slot = SimpleNamespace(tags=["user-tag", "t-old-health"])
        saved: list[tuple[object, bool]] = []
        pushed: list[bool] = []

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_lock(state: object):
            yield

        async def _fake_save(state: object, s: object, *, force: bool = False) -> None:
            saved.append((s, force))

        state = _bound_state(
            serving_loop=self.bg.loop,
            _slots={"chat-1": slot},
            _tags=[{"id": "user-tag"}, {"id": "t-old-health"}, {"id": "t-new-health"}],
            push_slots_update=lambda: pushed.append(True),
        )
        with (
            _patch_registry(state),
            patch("kiro_crew.dashboard.chat_tags.tags_write_lock", _fake_lock),
            patch("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", _fake_save),
        ):
            changed = TagsStore().merge_slot_tags(
                "chat-1",
                managed_ids={"t-old-health", "t-new-health"},
                want_ids={"t-new-health", "t-unknown"},  # unknown id dropped
            )

        self.assertTrue(changed)
        self.assertEqual(slot.tags, ["user-tag", "t-new-health"])
        self.assertEqual(saved, [(slot, True)])
        self.assertEqual(pushed, [True])

    def test_merge_slot_tags_noops_when_live_state_already_matches(self) -> None:
        slot = SimpleNamespace(tags=["user-tag", "t-health"])
        pushed: list[bool] = []

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_lock(state: object):
            yield

        state = _bound_state(
            serving_loop=self.bg.loop,
            _slots={"chat-1": slot},
            _tags=[{"id": "user-tag"}, {"id": "t-health"}],
            push_slots_update=lambda: pushed.append(True),
        )
        with (
            _patch_registry(state),
            patch("kiro_crew.dashboard.chat_tags.tags_write_lock", _fake_lock),
        ):
            changed = TagsStore().merge_slot_tags(
                "chat-1", managed_ids={"t-health"}, want_ids={"t-health"}
            )
        self.assertFalse(changed)
        self.assertEqual(slot.tags, ["user-tag", "t-health"])
        self.assertEqual(pushed, [])

    def test_merge_slot_tags_missing_slot_is_noop(self) -> None:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_lock(state: object):
            yield

        pushed: list[bool] = []
        state = _bound_state(
            serving_loop=self.bg.loop,
            _slots={},
            _tags=[],
            push_slots_update=lambda: pushed.append(True),
        )
        with (
            _patch_registry(state),
            patch("kiro_crew.dashboard.chat_tags.tags_write_lock", _fake_lock),
        ):
            self.assertFalse(TagsStore().merge_slot_tags("gone", {"t1"}, {"t1"}))
        # The early return fires inside _do(), so no broadcast either.
        self.assertEqual(pushed, [])

    def test_send_message_enqueues_prompt(self) -> None:
        enqueued: list[tuple[str, object, object]] = []
        slot = SimpleNamespace(
            enqueue_or_run_prompt=lambda msg, runner, st: enqueued.append((msg, runner, st))
        )
        pushed: list[bool] = []
        state = _bound_state(
            serving_loop=self.bg.loop,
            _slots={"chat-1": slot},
            push_slots_update=lambda: pushed.append(True),
        )
        with _patch_registry(state):
            TagsStore().send_message("chat-1", "Continue")
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0][0], "Continue")
        self.assertEqual(pushed, [True])

    def test_send_message_missing_slot_is_noop(self) -> None:
        state = _bound_state(serving_loop=self.bg.loop, _slots={})
        with _patch_registry(state):
            TagsStore().send_message("gone", "Continue")  # must not raise


class _FakeStoreClient:
    """Stub TagsStore for the loop-pass tests: pass bodies run for real."""

    def __init__(self, slots: list[dict], messages: dict[str, list[dict]]) -> None:
        vocab = list(logic.STATUS_ORDER) + list(logic.HEALTH_TAGS)
        self.tags = [{"id": f"id-{n}", "name": n} for n in vocab]
        self.slots = slots
        self.messages = messages
        self.tag_writes: list[tuple[str, list[str]]] = []
        self.sent: list[tuple[str, str]] = []

    def list_tags(self) -> list[dict]:
        return [dict(t) for t in self.tags]

    def create_tag(self, name: str, color: str, *, status: bool) -> dict:
        tag = {"id": f"id-{name}", "name": name}
        self.tags.append(tag)
        return tag

    def list_slots(self) -> list[dict]:
        return [dict(s) for s in self.slots]

    def slot_messages(self, key: str, limit: int) -> list[dict]:
        msgs = self.messages.get(key)
        if msgs is None:
            raise RuntimeError("detail unavailable")
        return msgs[-limit:]

    def merge_slot_tags(self, key: str, managed_ids: set[str], want_ids: set[str]) -> bool:
        self.tag_writes.append((key, sorted(want_ids)))
        return True

    def send_message(self, key: str, message: str) -> None:
        self.sent.append((key, message))


_NETWORK_ERROR_CARD = {"role": "error", "content": "❌ ACP error: connection lost"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestHealthPass(unittest.TestCase):
    def test_error_slot_gains_health_tag_and_healthy_slot_clears(self) -> None:
        net_id = "id-network"
        slots = [
            # Idle slot with a terminal network-error card and no tags yet
            # -> gains the `network` health tag.
            {"key": "sick", "running": False, "tags": [], "last_ts": _now_iso()},
            # Idle healthy slot still carrying a managed tag -> cleared.
            {"key": "healed", "running": False, "tags": [net_id], "last_ts": _now_iso()},
        ]
        messages = {
            "sick": [_NETWORK_ERROR_CARD],
            "healed": [{"role": "assistant", "content": "all good"}],
        }
        client = _FakeStoreClient(slots, messages)
        changes = hooks._health_pass(cast(TagsStore, client), stuck_min=30)

        self.assertEqual(len(changes), 2)
        writes = dict(client.tag_writes)
        self.assertIn(net_id, writes["sick"])
        self.assertNotIn(net_id, writes["healed"])

    def test_detail_read_failure_skips_slot(self) -> None:
        slots = [{"key": "opaque", "running": False, "tags": ["id-error"], "last_ts": _now_iso()}]
        client = _FakeStoreClient(slots, messages={})  # slot_messages raises
        self.assertEqual(hooks._health_pass(cast(TagsStore, client), stuck_min=30), [])
        self.assertEqual(client.tag_writes, [])


class TestResumeHelpers(unittest.TestCase):
    def test_probe_hosts_parses_and_defaults(self) -> None:
        ctx = SimpleNamespace(config={"probe_hosts": ["example.com:443", "badentry"]})
        self.assertEqual(hooks._probe_hosts(ctx), (("example.com", 443),))
        self.assertEqual(hooks._probe_hosts(SimpleNamespace(config={})), hooks._DEFAULT_PROBES)

    def test_failure_anchor_is_stable_across_resume_cycles(self) -> None:
        """The anchor must not move when the only new messages are our own
        injected resume turns and fresh error cards — otherwise every failed
        resume would re-key the episode and unbound the attempt cap (the exact
        defect this pins)."""
        base = [
            {"role": "user", "content": "please build the thing", "ts": "t1"},
            {"role": "assistant", "content": "working on it", "ts": "t2"},
            {"role": "error", "content": "❌ ACP error: connection lost", "ts": "t3"},
        ]
        anchor0 = logic.failure_anchor(base, hooks._RESUME_TEXT)
        self.assertEqual(anchor0, "t2")
        # One failed resume cycle later: injected Continue + a new error card.
        after_resume = base + [
            {"role": "user", "content": hooks._RESUME_TEXT, "ts": "t4"},
            {"role": "error", "content": "❌ ACP error: connection lost", "ts": "t5"},
        ]
        self.assertEqual(logic.failure_anchor(after_resume, hooks._RESUME_TEXT), "t2")
        # Attempts therefore accumulate on ONE episode across cycles.
        ep = logic.next_episode(None, anchor0)
        ep.attempts += 1
        ep = logic.next_episode(ep, logic.failure_anchor(after_resume, hooks._RESUME_TEXT))
        self.assertEqual(ep.attempts, 1)
        # A real recovery (genuine assistant turn) moves the anchor -> fresh episode.
        recovered = after_resume + [{"role": "assistant", "content": "back!", "ts": "t6"}]
        self.assertEqual(logic.failure_anchor(recovered, hooks._RESUME_TEXT), "t6")
        ep = logic.next_episode(ep, logic.failure_anchor(recovered, hooks._RESUME_TEXT))
        self.assertEqual(ep.attempts, 0)

    def test_seed_vocabulary_survives_differently_cased_existing_tags(self) -> None:
        """A pre-existing user tag "Network" makes create_tag return that tag
        verbatim; a verbatim-keyed map would then KeyError on "network" and
        silently kill the sweep. Pin the canonicalized lookup."""
        client = _FakeStoreClient([], {})
        client.tags = [{"id": "id-user-net", "name": "Network"}]

        real_create = client.create_tag

        def _case_insensitive_create(name: str, color: str, *, status: bool) -> dict:
            for t in client.tags:
                if t["name"].lower() == name.lower():
                    return t  # existing tag returned verbatim, original casing
            return real_create(name, color, status=status)

        client.create_tag = _case_insensitive_create  # type: ignore[method-assign]
        have = hooks._seed_vocabulary(cast(TagsStore, client))
        for name in list(logic.STATUS_ORDER) + list(logic.HEALTH_TAGS):
            self.assertIn(name, have)
        self.assertEqual(have["network"], "id-user-net")

    def test_network_up_true_and_false(self) -> None:
        class _Conn:
            def __enter__(self) -> "_Conn":
                return self

            def __exit__(self, *a: object) -> None:
                return None

        with patch.object(socket, "create_connection", return_value=_Conn()):
            self.assertTrue(hooks._network_up((("h", 1), ("h2", 2))))
        with patch.object(socket, "create_connection", side_effect=OSError):
            self.assertFalse(hooks._network_up((("h", 1),)))

    def test_episode_state_roundtrip_and_corrupt_file(self) -> None:
        with TemporaryDirectory() as tmp:
            ctx = SimpleNamespace(data_dir=Path(tmp))
            self.assertEqual(hooks._load_episodes(ctx), {})  # no file yet
            eps = {"chat-1": logic.Episode(last_ts="2026-01-01T00:00:00Z", attempts=2)}
            hooks._save_episodes(ctx, eps)
            self.assertEqual(hooks._load_episodes(ctx), eps)
            (ctx.data_dir / hooks._STATE_FILE).write_text("{not json", encoding="utf-8")
            self.assertEqual(hooks._load_episodes(ctx), {})

    def test_find_resume_candidates_filters(self) -> None:
        ts = _now_iso()
        slots = [
            {"key": "busy", "running": True, "last_ts": ts},
            {"key": "queued", "running": False, "queue_depth": 1, "last_ts": ts},
            {"key": "authfail", "running": False, "last_ts": ts},
            {"key": "netdead", "running": False, "last_ts": ts},
            {"key": "opaque", "running": False, "last_ts": ts},
        ]
        messages = {
            "busy": [_NETWORK_ERROR_CARD],
            "queued": [_NETWORK_ERROR_CARD],
            "authfail": [{"role": "error", "content": "🔑 auth expired — please sign in"}],
            "netdead": [_NETWORK_ERROR_CARD],
            # "opaque" absent -> detail read raises -> skipped
        }
        client = _FakeStoreClient(slots, messages)
        episodes: dict[str, logic.Episode] = {}
        cands = hooks._find_resume_candidates(cast(TagsStore, client), episodes)
        self.assertEqual([k for k, _ in cands], ["netdead"])
        self.assertIn("netdead", episodes)


class TestResumeLoopIteration(unittest.IsolatedAsyncioTestCase):
    async def test_health_loop_survives_malformed_stuck_min(self) -> None:
        """`stuck_min: "30m"` must degrade to the default, not raise before the
        loop's exception boundary and kill health tagging for the session.
        (An async test on purpose: ``asyncio.run`` reads as a spawn primitive
        to the repo's spawn audit, so the loop task is driven inline.)"""
        ctx = SimpleNamespace(config={"stuck_min": "30m"})
        with patch.object(hooks, "TagsStore"):
            task = asyncio.get_event_loop().create_task(hooks._health_loop(ctx))
            await asyncio.sleep(0)  # let the parse run — must not raise ValueError
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_one_iteration(self, ctx: SimpleNamespace, client: _FakeStoreClient) -> None:
        """Drive exactly one loop body: the second sleep cancels the loop."""
        real_sleep = asyncio.sleep
        calls = {"n": 0}

        async def _counting_sleep(secs: float) -> None:
            calls["n"] += 1
            if calls["n"] >= 2 and secs == hooks._RESUME_INTERVAL_SECS:
                raise asyncio.CancelledError
            await real_sleep(0)

        with (
            patch.object(hooks, "TagsStore", return_value=client),
            patch.object(hooks.asyncio, "sleep", _counting_sleep),
            patch.object(hooks, "_network_up", return_value=True),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await hooks._resume_loop(ctx)

    async def test_resume_fires_after_stable_network(self) -> None:
        ts = _now_iso()
        client = _FakeStoreClient(
            [{"key": "netdead", "running": False, "last_ts": ts}],
            {"netdead": [_NETWORK_ERROR_CARD]},
        )
        with TemporaryDirectory() as tmp:
            ctx = SimpleNamespace(config={}, data_dir=Path(tmp))
            await self._run_one_iteration(ctx, client)
            self.assertEqual(client.sent, [("netdead", hooks._RESUME_TEXT)])
            saved = json.loads((ctx.data_dir / hooks._STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(saved["netdead"]["attempts"], 1)

    async def test_disabled_flag_does_no_work(self) -> None:
        client = _FakeStoreClient(
            [{"key": "netdead", "running": False, "last_ts": _now_iso()}],
            {"netdead": [_NETWORK_ERROR_CARD]},
        )
        with TemporaryDirectory() as tmp:
            ctx = SimpleNamespace(config={}, data_dir=Path(tmp))
            with patch.object(
                hooks.settings, "get_flags", return_value={"auto_resume_enabled": False}
            ):
                await self._run_one_iteration(ctx, client)
            self.assertEqual(client.sent, [])


if __name__ == "__main__":
    unittest.main()
