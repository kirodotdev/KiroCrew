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
    monkeypatch.setattr(repository, "_LATCHED_CONFIGURED", "")
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

    def _discover(configured: str | None = None) -> str:
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
        """An unchanged configured path has nothing new to stat.

        The verdict is kept while the operator's own string stays put, so a poll
        re-stats nothing, and the state renders a banner naming the path and the
        remedy instead of asking for a restart. Correcting that string reopens the
        latch: see ``TestACorrectedPathIsNotFrozenBehindTheLatch``.
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


def _configured_tiers(monkeypatch: pytest.MonkeyPatch, *, valid: bool) -> dict:
    """Stub the tiers so tier 2 reads a value the test can rewrite mid-process.

    That is what an operator editing ``config.json`` does, and it is the property the
    latch has to respect: the configured tier is re-read per call, so a value this
    process already judged can become a different value without a restart.

    ``state["whole"]`` is the other half of that read: set it False to stand for a
    ``config.json`` the process could not parse, which reads as the same empty string
    as a config naming no path and must not be mistaken for one.
    """
    state: dict = {"configured": "", "valid": valid, "attempts": 0, "whole": True}
    monkeypatch.setattr(repository, "_configured_main_repo", lambda: state["configured"])
    monkeypatch.setattr(
        repository,
        "_configured_main_repo_checked",
        lambda: (state["configured"], state["whole"]),
    )
    monkeypatch.setattr(repository, "_resolve_primary_checkout", lambda p: p)
    monkeypatch.setattr(repository, "_is_kirocrew_checkout", lambda p: state["valid"])
    monkeypatch.setattr(repository, "_repo_source_hint", lambda: "set dev_fleet.repo_path")

    def _discover(configured: str | None = None) -> str:
        state["attempts"] += 1
        # Production hands its one checked snapshot in; returning it keeps the stub
        # faithful to that contract rather than reading the state a second time.
        return state["configured"] if configured is None else configured

    monkeypatch.setattr(repository, "_discover_main_repo", _discover)
    return state


class TestACorrectedPathIsNotFrozenBehindTheLatch:
    """A found-but-invalid path is truthy, so it latches like any other resolution.

    Keeping that verdict past a config edit reproduces, for the typo case, the freeze
    this chain removes for the not-found case: the operator reads the banner, fixes
    ``dev_fleet.repo_path``, and the same banner stays until a restart.
    """

    async def test_correcting_the_configured_path_re_runs_discovery(
        self, fresh_discovery, monkeypatch
    ) -> None:
        state = _configured_tiers(monkeypatch, valid=False)
        state["configured"] = "/somewhere/kirocrw"
        await repository.ensure_main_repo_discovered()
        assert state["attempts"] == 1
        assert repository._REPO_INVALID_MSG is not None

        state["configured"] = "/somewhere/kirocrew"
        state["valid"] = True
        await repository.ensure_main_repo_discovered()
        assert state["attempts"] == 2, (
            "the operator corrected dev_fleet.repo_path, which tier 2 re-reads per "
            "call, so the latched verdict is about a string this process has left"
        )
        assert repository.MAIN_REPO == "/somewhere/kirocrew"
        assert repository._REPO_INVALID_MSG is None

    async def test_an_unchanged_invalid_path_buys_no_second_attempt(
        self, fresh_discovery, monkeypatch
    ) -> None:
        state = _configured_tiers(monkeypatch, valid=False)
        state["configured"] = "/somewhere/kirocrw"
        await repository.ensure_main_repo_discovered()
        await repository.ensure_main_repo_discovered()
        await repository.ensure_main_repo_discovered()
        assert state["attempts"] == 1, (
            "reopening on a changed string alone is what keeps a poll free; a bare "
            "retry would re-stat a path the operator has not touched"
        )

    async def test_the_compare_is_against_the_configured_string_not_the_resolved_one(
        self, fresh_discovery, monkeypatch
    ) -> None:
        """``MAIN_REPO`` is ``_resolve_primary_checkout`` OF the configured value.

        A configured path pointing into a linked worktree is rewritten to its primary,
        so comparing the config against ``MAIN_REPO`` differs forever and every poll
        would pay a re-resolution for a config nobody edited.
        """
        state = _configured_tiers(monkeypatch, valid=False)
        state["configured"] = "/somewhere/kirocrew/wt/feature"
        monkeypatch.setattr(
            repository, "_resolve_primary_checkout", lambda p: "/somewhere/kirocrew"
        )
        await repository.ensure_main_repo_discovered()
        assert repository.MAIN_REPO == "/somewhere/kirocrew"
        assert repository._LATCHED_CONFIGURED == "/somewhere/kirocrew/wt/feature"
        await repository.ensure_main_repo_discovered()
        assert state["attempts"] == 1

    async def test_an_env_set_path_wins_so_a_config_edit_cannot_reopen_it(
        self, fresh_discovery, monkeypatch
    ) -> None:
        """``KIROCREW_DEVFLEET_REPO`` is read off this process's own environment.

        Env wins over config, so editing ``config.json`` underneath an env-set path
        changes nothing this process resolves: the compare stays equal, no attempt is
        bought, and the banner correctly keeps naming a restart as that route's remedy.
        Drives the real ``_configured_main_repo`` so the precedence is what is tested.
        """
        cfg = {"repo_path": "/somewhere/kirocrw"}
        monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/somewhere/kirocrw")
        monkeypatch.setattr(repository, "_load_dev_fleet_cfg", lambda: cfg)
        monkeypatch.setattr(repository, "_resolve_primary_checkout", lambda p: p)
        monkeypatch.setattr(repository, "_is_kirocrew_checkout", lambda p: False)
        monkeypatch.setattr(repository, "_repo_source_hint", lambda: "set by the env var")
        attempts: list[int] = []

        def _discover(configured: str | None = None) -> str:
            attempts.append(1)
            return repository._configured_main_repo()

        monkeypatch.setattr(repository, "_discover_main_repo", _discover)
        await repository.ensure_main_repo_discovered()
        assert len(attempts) == 1
        assert repository._REPO_INVALID_MSG is not None

        cfg["repo_path"] = "/somewhere/kirocrew"
        await repository.ensure_main_repo_discovered()
        assert len(attempts) == 1, (
            "env wins over config, so a config edit changes nothing this process "
            "resolves and must not buy a re-resolution"
        )

    async def test_the_fleet_route_re_runs_discovery_while_the_path_is_invalid(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrw")
        monkeypatch.setattr(
            repository, "_REPO_INVALID_MSG", "not a Kiro Crew checkout: /somewhere/kirocrw"
        )
        monkeypatch.setattr(worktree_ops, "_refresher_task", None)
        monkeypatch.setattr(worktree_ops, "_background_tasks_disabled", lambda: False)
        ran: list[int] = []

        async def _again() -> None:
            ran.append(1)

        monkeypatch.setattr(repository, "ensure_main_repo_discovered", _again)
        await worktree_ops._ensure_repo_resolved()
        assert ran == [1], (
            "the guard cannot return on truthiness alone: an invalid path is truthy, "
            "and the operator can still correct the path its banner names"
        )
        assert worktree_ops._refresher_task is None, (
            "the refresher returns on its first line for an unusable checkout, so "
            "starting it here would mint a task per poll that dies immediately"
        )


class TestAConfigReadThatFailedIsNotAConfigChange:
    """An unreadable ``config.json`` is not the operator clearing ``repo_path``.

    The reader answers ``{}`` for a file it could not parse, so the configured path
    reads ``""`` -- the same answer a config that names no path gives. Reopening the
    latch on that difference acts on evidence nobody read: discovery re-runs, finds
    no configured path, falls through to the INFERRED tiers and latches a checkout
    the operator never named, while their own setting sits in a file this process
    merely failed to read. Every later git call, including the destructive ones,
    then targets that other checkout.
    """

    async def test_a_partial_config_read_does_not_reopen_the_latch(self, monkeypatch) -> None:
        """The whole point: no whole read, no verdict about what the operator wants."""
        monkeypatch.setattr(repository, "_DISCOVERY_DONE", True)
        monkeypatch.setattr(repository, "_REPO_INVALID_MSG", "not a Kiro Crew checkout")
        monkeypatch.setattr(repository, "MAIN_REPO", "/opt/typo")
        monkeypatch.setattr(repository, "_LATCHED_CONFIGURED", "/opt/typo")
        monkeypatch.setattr(repository, "_configured_main_repo_checked", lambda: ("", False))
        assert repository._invalid_resolution_is_stale() is False

    async def test_a_whole_read_that_cleared_the_path_still_reopens_the_latch(
        self, monkeypatch
    ) -> None:
        """Pins the distinction, and fails under the narrower fix.

        Treating every empty answer as "unchanged" would also freeze this case, which
        is a change the operator really made: they removed ``dev_fleet.repo_path``, the
        file parsed, and the INFERRED tiers should get their turn. Only a read that
        FAILED carries no evidence.
        """
        monkeypatch.setattr(repository, "_DISCOVERY_DONE", True)
        monkeypatch.setattr(repository, "_REPO_INVALID_MSG", "not a Kiro Crew checkout")
        monkeypatch.setattr(repository, "MAIN_REPO", "/opt/typo")
        monkeypatch.setattr(repository, "_LATCHED_CONFIGURED", "/opt/typo")
        monkeypatch.setattr(repository, "_configured_main_repo_checked", lambda: ("", True))
        assert repository._invalid_resolution_is_stale() is True

    async def test_an_unparseable_file_reads_as_a_partial_snapshot(
        self, monkeypatch, tmp_path
    ) -> None:
        """Drives the real reader, so the flag is measured rather than stubbed."""
        from kiro_crew.config import loader as loader_mod

        monkeypatch.setattr(loader_mod, "config_dir", lambda: tmp_path)
        cfg = tmp_path / "config.json"
        cfg.write_text('{"dev_fleet": {"repo_path": "/opt/kc"}}', encoding="utf-8")
        assert repository._load_dev_fleet_cfg_checked() == ({"repo_path": "/opt/kc"}, True)

        # What a reader observes mid-write through a non-atomic external editor.
        cfg.write_text("{not json", encoding="utf-8")
        section, whole = repository._load_dev_fleet_cfg_checked()
        assert section == {}
        assert whole is False

    async def test_a_config_naming_no_path_is_a_whole_read(self, monkeypatch, tmp_path) -> None:
        """A parseable file carrying no ``repo_path`` is an answer, not a missing read."""
        from kiro_crew.config import loader as loader_mod

        monkeypatch.setattr(loader_mod, "config_dir", lambda: tmp_path)
        (tmp_path / "config.json").write_text('{"dev_fleet": {}}', encoding="utf-8")
        monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
        assert repository._configured_main_repo_checked() == ("", True)

    async def test_an_unresolvable_config_dir_is_a_partial_read(self, monkeypatch) -> None:
        """No config directory means no evidence either, so the flag must say so.

        The section being empty is already pinned elsewhere; what matters here is the
        flag, because this is the one path that reaches the caller without having read
        any file at all.
        """
        from kiro_crew.config import loader as loader_mod

        def _boom() -> object:
            raise RuntimeError("no data home")

        monkeypatch.setattr(loader_mod, "config_dir", _boom)
        assert repository._load_dev_fleet_cfg_checked() == ({}, False)


class TestAPartialReadAtDiscoveryLatchesNothing:
    """The second read is the dangerous one, because its latch can be FINAL.

    The staleness test and discovery each need the configured path, and two reads of
    one file can disagree. If the first sees a whole, corrected path and reopens
    while the second returns "" from a torn read, discovery falls to the INFERRED
    tiers and latches a checkout that PASSES the marker test. A valid latch is
    final: nothing re-resolves it, the staleness test only reopens a latched-INVALID
    one, and `Pull + Build` meanwhile mutates a checkout the operator never named.
    Only a restart clears it.

    So the attempt takes ONE checked snapshot and hands it to discovery, and a
    partial read publishes nothing at all.
    """

    async def test_a_partial_read_publishes_no_resolution(
        self, fresh_discovery, monkeypatch
    ) -> None:
        """No latch, no inferred hint left serving, and the next poll may try again.

        ``MAIN_REPO`` is seeded with the provisional import-time value on purpose. The
        fixture zeroes it, so asserting it stays empty would pass whether or not the
        attempt clears anything; a real install reaches this branch holding that hint,
        and ``_repo()`` gates on ``MAIN_REPO`` alone, never on ``_DISCOVERY_DONE``.
        """
        calls: list[int] = []

        def _discover(configured: str | None = None) -> str:
            calls.append(1)
            return "/opt/inferred-kirocrew"

        monkeypatch.setattr(repository, "MAIN_REPO", "/opt/import-time-hint")
        monkeypatch.setattr(repository, "MAIN_REPO_INFERRED", True)
        monkeypatch.setattr(repository, "_configured_main_repo_checked", lambda: ("", False))
        monkeypatch.setattr(repository, "_discover_main_repo", _discover)
        monkeypatch.setattr(repository, "_resolve_primary_checkout", lambda p: p)
        monkeypatch.setattr(repository, "_is_kirocrew_checkout", lambda p: True)
        await repository.ensure_main_repo_discovered()
        assert calls == []
        assert repository.MAIN_REPO == ""
        assert repository.MAIN_REPO_INFERRED is False
        assert repository._DISCOVERY_DONE is False
        with pytest.raises(repository.RepoNotConfigured):
            repository._repo()

    async def test_a_torn_read_on_the_reopen_path_leaves_the_next_poll_reachable(
        self, fresh_discovery, monkeypatch
    ) -> None:
        """Clearing the published pair alone still strands the retry it promises.

        A latched-INVALID resolution whose config has since been corrected reaches the
        read with ``_DISCOVERY_DONE`` already True. Clearing ``MAIN_REPO`` and the
        message without clearing that flag satisfies every assertion the sibling test
        makes and still freezes the page, because the pre-lock gate returns early once
        the pair is empty. Counting the reads is what sees it: the flag's value at the
        end of one attempt cannot be told apart from a fresh start, so the second
        attempt is the evidence.
        """
        reads: list[int] = []

        def _checked() -> tuple[str, bool]:
            reads.append(1)
            return ("", False)

        monkeypatch.setattr(repository, "_DISCOVERY_DONE", True)
        monkeypatch.setattr(repository, "MAIN_REPO", "/opt/latched-invalid")
        monkeypatch.setattr(repository, "_REPO_INVALID_MSG", "not a kirocrew checkout")
        monkeypatch.setattr(repository, "_invalid_resolution_is_stale", lambda: True)
        monkeypatch.setattr(repository, "_configured_main_repo_checked", _checked)
        monkeypatch.setattr(repository, "_discover_main_repo", lambda configured=None: "")
        await repository.ensure_main_repo_discovered()
        assert len(reads) == 1
        assert repository.MAIN_REPO == ""
        assert repository._DISCOVERY_DONE is False
        await repository.ensure_main_repo_discovered()
        assert len(reads) == 2

    async def test_a_whole_read_still_publishes(self, fresh_discovery, monkeypatch) -> None:
        """The control: the same path resolves normally once the read is whole.

        Without this, the test above would pass against a function that never
        resolves anything.
        """
        monkeypatch.setattr(repository, "_configured_main_repo_checked", lambda: ("", True))
        monkeypatch.setattr(
            repository, "_discover_main_repo", lambda configured=None: "/opt/inferred-kirocrew"
        )
        monkeypatch.setattr(repository, "_resolve_primary_checkout", lambda p: p)
        monkeypatch.setattr(repository, "_is_kirocrew_checkout", lambda p: True)
        await repository.ensure_main_repo_discovered()
        assert repository.MAIN_REPO == "/opt/inferred-kirocrew"
        assert repository._DISCOVERY_DONE is True
        assert repository.MAIN_REPO_INFERRED is True

    async def test_discovery_reads_the_handed_snapshot_rather_than_the_file(
        self, fresh_discovery, monkeypatch
    ) -> None:
        """Pins that the attempt passes its snapshot in, so there is no second read.

        The stub returns whatever it is handed. If the attempt stopped passing the
        value, discovery would receive None and fall back to reading the file, and
        the resolved path would not be the snapshot's.
        """
        seen: list[str | None] = []

        def _discover(configured: str | None = None) -> str:
            seen.append(configured)
            return configured or ""

        monkeypatch.setattr(
            repository, "_configured_main_repo_checked", lambda: ("/opt/named-by-operator", True)
        )
        monkeypatch.setattr(repository, "_discover_main_repo", _discover)
        monkeypatch.setattr(repository, "_resolve_primary_checkout", lambda p: p)
        monkeypatch.setattr(repository, "_is_kirocrew_checkout", lambda p: True)
        await repository.ensure_main_repo_discovered()
        assert seen == ["/opt/named-by-operator"]
        assert repository.MAIN_REPO == "/opt/named-by-operator"
        assert repository.MAIN_REPO_INFERRED is False

    async def test_the_default_still_reads_the_file_for_a_caller_with_no_snapshot(
        self, monkeypatch
    ) -> None:
        """`dev_fleet_startup` and the tests call it with no argument, so that path stays."""
        monkeypatch.setattr(repository, "_configured_main_repo", lambda: "/opt/from-the-file")
        assert repository._discover_main_repo() == "/opt/from-the-file"
        assert repository._discover_main_repo("/opt/handed-in") == "/opt/handed-in"
