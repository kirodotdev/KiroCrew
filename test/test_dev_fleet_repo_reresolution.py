"""Re-resolving the main checkout inside one process.

``ensure_main_repo_discovered`` latches only once a checkout RESOLVED, and
``worktree_ops._ensure_repo_resolved`` re-runs it from ``/api/fleet`` while nothing
is. So a gateway that starts before the operator points Dev Fleet at a checkout
serves a fleet as soon as one resolves, with no restart.

Only the ``dev_fleet.repo_path`` half of the remedy can self-heal:
``_load_dev_fleet_cfg`` re-reads ``config.json`` on every call, whereas
``KIROCREW_DEVFLEET_REPO`` is read off this process's own environment, which no
outside shell can change. The setup card's copy says so.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.apps.builtins.dev_fleet import repository, runtime, worktree_ops

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fresh_discovery(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """An un-run discovery chain, with the git-touching warms counted not executed.

    Every global the chain writes is reset, so a test starts from the state a
    freshly-imported process is in rather than from whatever an earlier test in
    the same interpreter left behind.
    """
    monkeypatch.setattr(repository, "_DISCOVERY_DONE", False)
    monkeypatch.setattr(repository, "_DISCOVERY_LOCK", None)
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    monkeypatch.setattr(repository, "MAIN_REPO_INFERRED", False)
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(runtime, "_GIT_TRUSTED_HELPERS", None)

    counts = {"helpers": 0, "fallback": 0, "upstream": 0}

    async def _helpers() -> None:
        counts["helpers"] += 1
        # Mirrors the real loader, which always assigns a dict — that is what
        # makes `None` usable as the not-yet-loaded sentinel.
        runtime._GIT_TRUSTED_HELPERS = {}

    async def _fallback() -> None:
        counts["fallback"] += 1

    async def _upstream() -> str:
        counts["upstream"] += 1
        return "origin"

    monkeypatch.setattr(repository, "_load_trusted_credential_helpers", _helpers)
    monkeypatch.setattr(repository, "_load_fallback_repos", _fallback)
    monkeypatch.setattr(repository, "_upstream_remote", _upstream)
    return counts


def _discovers(monkeypatch: pytest.MonkeyPatch, *results: str) -> list[int]:
    """Stub the discovery tiers to yield ``results`` in order, counting attempts.

    The last result repeats once exhausted, so a test asserting "it stops trying"
    fails loudly (an extra attempt is counted) instead of raising StopIteration
    and passing for the wrong reason.
    """
    attempts: list[int] = []
    pending = list(results)

    def _discover() -> str:
        attempts.append(1)
        return pending.pop(0) if len(pending) > 1 else pending[0]

    monkeypatch.setattr(repository, "_configured_main_repo", lambda: "")
    monkeypatch.setattr(repository, "_discover_main_repo", _discover)
    monkeypatch.setattr(repository, "_resolve_primary_checkout", lambda p: p)
    monkeypatch.setattr(repository, "_is_kirocrew_checkout", lambda p: True)
    monkeypatch.setattr(repository, "_repo_source_hint", lambda: "set dev_fleet.repo_path")
    return attempts


class TestTheLatchWaitsForAnAnswerWorthKeeping:
    async def test_an_attempt_that_finds_nothing_does_not_latch(
        self, fresh_discovery, monkeypatch
    ) -> None:
        attempts = _discovers(monkeypatch, "")
        await repository.ensure_main_repo_discovered()
        await repository.ensure_main_repo_discovered()
        await repository.ensure_main_repo_discovered()
        assert len(attempts) == 3, (
            "a process with no checkout has no answer worth keeping: the operator "
            "can write dev_fleet.repo_path at any moment, so every attempt must look"
        )
        assert repository._DISCOVERY_DONE is False

    async def test_the_first_attempt_that_resolves_latches(
        self, fresh_discovery, monkeypatch
    ) -> None:
        attempts = _discovers(monkeypatch, "", "/somewhere/kirocrew")
        await repository.ensure_main_repo_discovered()
        assert repository.MAIN_REPO == ""
        await repository.ensure_main_repo_discovered()
        assert repository.MAIN_REPO == "/somewhere/kirocrew"
        assert repository._DISCOVERY_DONE is True
        await repository.ensure_main_repo_discovered()
        assert len(attempts) == 2, "a resolved checkout is discovered once per process"

    async def test_a_resolved_path_that_fails_the_marker_test_still_latches(
        self, fresh_discovery, monkeypatch
    ) -> None:
        """Tiers 1-2 are taken verbatim, so re-running returns the same path forever.

        Retrying it would re-stat a path whose verdict cannot change, and the
        state renders a banner naming the path and the remedy instead of asking
        for a restart.
        """
        attempts = _discovers(monkeypatch, "/somewhere/not-a-checkout")
        monkeypatch.setattr(repository, "_is_kirocrew_checkout", lambda p: False)
        await repository.ensure_main_repo_discovered()
        await repository.ensure_main_repo_discovered()
        assert len(attempts) == 1
        assert repository._REPO_INVALID_MSG is not None
        assert "/somewhere/not-a-checkout" in repository._REPO_INVALID_MSG


class TestNoVerdictOutlivesTheAttemptThatProducedIt:
    async def test_an_attempt_finding_nothing_clears_a_stale_invalid_path_message(
        self, fresh_discovery, monkeypatch
    ) -> None:
        """The shape to avoid: ``MAIN_REPO`` from this attempt beside an earlier
        attempt's validation verdict. ``_repo()`` would then raise against a path
        this process does not hold, or hand out one whose markers went unchecked
        — and ``worktree remove``, ``update-ref -d`` and ``pip install -e`` run
        inside whatever that is.
        """
        monkeypatch.setattr(repository, "_REPO_INVALID_MSG", "not a Kiro Crew checkout: /gone")
        _discovers(monkeypatch, "")
        await repository.ensure_main_repo_discovered()
        assert repository._REPO_INVALID_MSG is None
        assert repository.MAIN_REPO == ""

    async def test_the_credential_helper_warm_is_paid_once_not_once_per_attempt(
        self, fresh_discovery, monkeypatch
    ) -> None:
        """Repo-INDEPENDENT (``--system``/``--global`` only), so it is a
        once-per-process warm; charging its two subprocesses to every poll of an
        unconfigured dashboard would be a new cost this change invented.
        """
        _discovers(monkeypatch, "")
        await repository.ensure_main_repo_discovered()
        await repository.ensure_main_repo_discovered()
        await repository.ensure_main_repo_discovered()
        assert fresh_discovery["helpers"] == 1


class TestTheRouteOnlyPaysWhileUnresolved:
    async def test_a_resolved_install_runs_no_discovery(self, monkeypatch) -> None:
        monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")
        ran = []

        async def _never() -> None:
            ran.append(1)

        monkeypatch.setattr(repository, "ensure_main_repo_discovered", _never)
        await worktree_ops._ensure_repo_resolved()
        assert ran == [], "the guard returns before any await, so a poll costs nothing"

    async def test_a_late_resolution_starts_the_status_refresher(self, monkeypatch) -> None:
        """``_status_refresher`` RETURNS when nothing is resolved rather than idling,
        so a late resolution that left it stopped would serve a fleet whose rows
        never refresh again: the setup card disappears, the page looks alive, and
        nothing fetches.
        """
        monkeypatch.setattr(repository, "MAIN_REPO", "")
        monkeypatch.setattr(worktree_ops, "_refresher_task", None)
        monkeypatch.setattr(worktree_ops, "_background_tasks_disabled", lambda: False)

        async def _resolve() -> None:
            monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")

        started: list[int] = []

        async def _refresher() -> None:
            started.append(1)

        monkeypatch.setattr(repository, "ensure_main_repo_discovered", _resolve)
        monkeypatch.setattr(worktree_ops, "_status_refresher", _refresher)
        await worktree_ops._ensure_repo_resolved()
        assert worktree_ops._refresher_task is not None
        await asyncio.sleep(0)
        assert started == [1]

    async def test_a_still_unresolved_attempt_starts_no_refresher(self, monkeypatch) -> None:
        monkeypatch.setattr(repository, "MAIN_REPO", "")
        monkeypatch.setattr(worktree_ops, "_refresher_task", None)
        monkeypatch.setattr(worktree_ops, "_background_tasks_disabled", lambda: False)

        async def _resolve_nothing() -> None:
            return None

        started: list[int] = []

        async def _refresher() -> None:
            started.append(1)

        monkeypatch.setattr(repository, "ensure_main_repo_discovered", _resolve_nothing)
        monkeypatch.setattr(worktree_ops, "_status_refresher", _refresher)
        await worktree_ops._ensure_repo_resolved()
        assert worktree_ops._refresher_task is None
        await asyncio.sleep(0)
        assert started == []

    async def test_a_disabled_background_mode_resolves_without_starting_a_task(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(repository, "MAIN_REPO", "")
        monkeypatch.setattr(worktree_ops, "_refresher_task", None)
        monkeypatch.setattr(worktree_ops, "_background_tasks_disabled", lambda: True)

        async def _resolve() -> None:
            monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")

        monkeypatch.setattr(repository, "ensure_main_repo_discovered", _resolve)
        await worktree_ops._ensure_repo_resolved()
        assert worktree_ops._refresher_task is None
