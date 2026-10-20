"""Fixtures and doubles that test_session.py and the eight sibling files its docstring names share.

``cfg`` is the short-timeout config the tests request, ``_mock_provider_factory`` (over
``_empty_provider_stream``) builds the mock providers a ``SessionManager`` starts, and ``_raw_sid``
reads a stored sid straight off the session map. ``_TestParentEndCancelsItsChildrenHarness`` holds the
teardown-handler recorder that ``TestParentEndCancelsItsChildren`` (test_session_parent_end_verbs.py)
and ``TestParentEndCancelsItsChildrenTeardownRules`` (test_session_parent_end_teardown.py) call as
``self._recorder``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig


@pytest.fixture(name="cfg")
def cfg_fixture():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2  # short for testing
    return c


async def _empty_provider_stream(_command: str):
    """An empty async iterator for provider methods consumed by ``async for``."""
    if False:  # pragma: no cover - establishes the async-generator protocol
        yield None


def _mock_provider_factory():
    """Return a factory that creates mock LLMProviders."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        # Explicit, not AsyncMock-generated: the post-semaphore re-validate calls
        # this synchronously, and an auto-generated coroutine would read as
        # "alive" only by truthiness while leaking an un-awaited coroutine.
        m.is_process_alive = lambda: True
        # Same reason: the registry calls this synchronously on a race loser.
        m.disown_work_dir = MagicMock()
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_abort_target = lambda: None
        m.stream_command = MagicMock(side_effect=_empty_provider_stream)
        # Explicit, not AsyncMock-generated: the identity sweep reads this through
        # ``spawn_identity_of`` to decide whether a source the holder spawned under
        # dropped. An auto-generated Mock attribute is truthy and reads as a bogus
        # stamp; "" is the realistic unstamped default (pre-stamping spare treatment).
        # ``_runtime`` is cleared too, because ``spawn_identity_of`` falls back to
        # ``_runtime.spawn_identity`` and an auto-Mock runtime would read truthy.
        m.spawn_identity = ""
        m._runtime = None
        return m

    return factory


def _raw_sid(mgr, key: str):
    """The stored sid, read straight off the map entry.

    ``SessionMap.get`` additionally requires the transcript ``<sid>.json`` to
    exist on disk, so it answers None for any synthetic sid — which would make a
    "was it cleared?" assertion pass whether or not the clear ran. These tests
    care about the stored pointer, so they read it.
    """
    from kiro_crew.session_map import canonical_key

    return (mgr._session_map._data.get(canonical_key(key)) or {}).get("sid")


class _TestParentEndCancelsItsChildrenHarness:
    """The ``_recorder`` both parent-end test classes share; never collected.

    ``TestParentEndCancelsItsChildren`` and ``TestParentEndCancelsItsChildrenTeardownRules`` subclass it.
    """

    @staticmethod
    def _recorder(
        children: dict[str, tuple[str, ...]] | None = None,
        *,
        store_children: dict[str, bool] | None = None,
        store_read_raises: bool = False,
        on_store_probe=None,
        fenced_children: dict[str, bool] | None = None,
        pending_work: dict[str, bool] | None = None,
        unfinished_reports: dict[str, bool] | None = None,
    ):
        """A handler shaped like ``SubagentManager``'s teardown halves.

        ``pending_work`` drives both halves of the manager's pending-work signal
        (``has_pending_work_for_async`` and ``has_in_memory_pending_work_for``):
        work the live/queued probe cannot see, such as a finished child whose
        terminal report is not yet registered. ``unfinished_reports`` drives
        ``has_unfinished_report_for``: a registered report still delivering after
        its run's task ended. Default: none.

        Records the keys it was asked to snapshot and the id tuples it was asked
        to cancel, so a test can tell "asked about the right parent" apart from
        "cancelled the right runs".

        The identity sweep now decides retire-vs-defer at the async teardown:
        ``open_teardown_fence`` is called under the lock, then the decision reads
        ``has_live_or_queued_children`` (in memory), ``has_fenced_children`` (the
        fence), and ``has_pending_store_children`` (off-loop store). ``children``
        drives the in-memory probe (default: a live child), ``store_children`` the
        off-loop probe, ``fenced_children`` the fence probe, ``store_read_raises``
        makes the store probe raise (unreadable store), and ``on_store_probe(key)``
        runs inside that await so a test can mutate state during the read.
        """
        snapshotted: list[str] = []
        cancelled: list[tuple[str, ...]] = []
        probed: list[str] = []
        store_probed: list[str] = []
        fence_opened: list[str] = []
        fence_released: list[str] = []
        owned = children if children is not None else {}
        owned_store = store_children if store_children is not None else {}
        owned_fenced = fenced_children if fenced_children is not None else {}
        owed = pending_work if pending_work is not None else {}
        reporting = unfinished_reports if unfinished_reports is not None else {}

        class _Handler:
            def has_in_memory_pending_work_for(self, parent_session_key, *, exclude_id=""):
                return bool(owed.get(parent_session_key, False))

            async def has_pending_work_for_async(self, parent_session_key: str) -> bool:
                return bool(owed.get(parent_session_key, False))

            def has_unfinished_report_for(self, parent_session_key: str) -> bool:
                return bool(reporting.get(parent_session_key, False))

            def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
                snapshotted.append(parent_session_key)
                return owned.get(parent_session_key, ("run-1",))

            def has_live_or_queued_children(self, parent_session_key: str) -> bool:
                probed.append(parent_session_key)
                return bool(owned.get(parent_session_key, ("run-1",)))

            def open_teardown_fence(self, parent_session_key: str):
                fence_opened.append(parent_session_key)
                return set()

            def has_fenced_children(self, parent_session_key: str) -> bool:
                return bool(owned_fenced.get(parent_session_key, False))

            def commit_identity_sweep_retire(self, parent_session_key, expected) -> bool:
                # Commit unless a child was admitted into the fence since it opened.
                return not bool(owned_fenced.get(parent_session_key, False))

            def snapshot_teardown_children_only(self, parent_session_key: str):
                snapshotted.append(parent_session_key)
                return owned.get(parent_session_key, ("run-1",))

            def release_teardown_fence(self, parent_session_key: str, expected=None) -> None:
                fence_released.append(parent_session_key)

            async def has_pending_store_children(self, parent_session_key: str) -> bool:
                store_probed.append(parent_session_key)
                if on_store_probe is not None:
                    on_store_probe(parent_session_key)
                if store_read_raises:
                    raise RuntimeError("store read failed")
                return bool(owned_store.get(parent_session_key, False))

            async def cancel_for_teardown(
                self,
                agent_ids,
                *,
                parent_session_key: str = "",
                verb: str = "",
            ) -> int:
                cancelled.append(tuple(agent_ids))
                return len(tuple(agent_ids))

        _Handler.probed = probed  # type: ignore[attr-defined]
        _Handler.store_probed = store_probed  # type: ignore[attr-defined]
        _Handler.fence_opened = fence_opened  # type: ignore[attr-defined]
        _Handler.fence_released = fence_released  # type: ignore[attr-defined]
        return snapshotted, cancelled, _Handler()
