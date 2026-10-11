"""A saved governance profile re-derives the on-disk auto-approvals.

``allowedTools`` and each server's ``autoApprove`` are materialised: the writers ask
``may_skip_gate_now`` when they write, and that consults the policy ceiling AND every
configured governance profile. kiro-cli then reads the FILE, and an auto-approved tool
never reaches Kiro Crew's own PreToolUse gate. So a profile saved after the spec was
written, one that comes to govern an auto-approved tool, has to re-derive the spec the
same way a ceiling install does, or every new session keeps running that tool without
a prompt until something unrelated rebuilds the config.

A profile is saved by editing a file under the profiles directory. These tests use a
real profile store over a real directory; only the rebuild itself is replaced, by a
recorder that asks the same question the real writers ask at write time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance as gov
from kiro_crew.platform import governance_profiles as gp

_REF = "@user-server"


class _Ungoverned:
    """A host with no policy ceiling: only the profile layer can govern a ref."""

    governance = None


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", d)
    monkeypatch.setattr(ctx_mod, "current_context", lambda: _Ungoverned())
    gp.reset_store()
    yield d
    gp.reset_store()


@pytest.fixture
def rebuilds(monkeypatch, profiles_dir) -> list[bool]:
    """Replace the rebuild with a recorder of what a real writer would decide.

    Each entry is ``may_skip_gate_now(_REF)`` evaluated INSIDE the rebuild, which is
    the answer the real ``allowedTools`` / ``autoApprove`` writers persist.
    """
    seen: list[bool] = []

    def fake_reporting(**kw):
        seen.append(gov.may_skip_gate_now(_REF))
        return Path("/agents/kirocrew.json"), True

    monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", fake_reporting)
    monkeypatch.setattr(agent_mod, "_conductor_spec_held", False, raising=False)
    monkeypatch.setattr(agent_mod, "_projected_ceiling_generation", None, raising=False)
    monkeypatch.setattr(agent_mod, "_pending_projection_warned_generation", None, raising=False)
    monkeypatch.setattr(agent_mod, "_profile_watch_generation", None, raising=False)
    monkeypatch.setattr(agent_mod, "_profile_watch_retry_at", 0.0, raising=False)
    monkeypatch.setattr(agent_mod, "_rebuild_answer_generation", None, raising=False)
    monkeypatch.setattr(agent_mod, "_rebuild_incomplete", False, raising=False)
    return seen


def _boot() -> None:
    """Model the boot rebuild: it loads the profile store, then the baseline is seeded."""
    gp.poll_profiles_fresh()
    assert gov.may_skip_gate_now(_REF) is True, "precondition: nothing governs the ref yet"
    agent_mod.prime_ceiling_projection()


def _save_profile_governing_the_ref(profiles_dir: Path) -> None:
    (profiles_dir / "narrow.json").write_text(
        json.dumps({"name": "narrow", "mcp": {"mode": "allow", "allow": ["@keep"]}}),
        encoding="utf-8",
    )


class TestTheDistributionHookSeesAProfileSave:
    def test_a_profile_save_re_derives_on_the_next_poll(self, profiles_dir, rebuilds):
        """A profile save with an unchanged ceiling re-derives the spec, and the
        re-derived spec withholds the auto-approval the profile now governs."""
        _boot()
        agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [], "an unchanged ceiling and profile set rebuilds nothing"

        _save_profile_governing_the_ref(profiles_dir)
        agent_mod.reproject_for_ceiling_change()

        assert rebuilds == [False], (
            "the saved profile must re-derive the spec, and the re-derived spec must "
            "withhold the auto-approval the profile now governs"
        )
        agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [False], "once projected, an unchanged answer rebuilds nothing"


class TestTheGatewayProfileWatch:
    """A host with no distribution source runs no poll, so the gateway's own watch is
    the only thing that observes a profile save there."""

    def test_a_save_is_projected_without_a_distribution_poll(self, profiles_dir, rebuilds):
        _boot()
        agent_mod.reproject_for_profile_change()
        assert rebuilds == []

        _save_profile_governing_the_ref(profiles_dir)
        agent_mod.reproject_for_profile_change()
        assert rebuilds == [False]

        agent_mod.reproject_for_profile_change()
        assert rebuilds == [False]

    def test_a_save_after_the_hook_projected_it_is_not_rebuilt_twice(self, profiles_dir, rebuilds):
        """On a host that has both, whichever runs first projects the save."""
        _boot()
        _save_profile_governing_the_ref(profiles_dir)
        agent_mod.reproject_for_ceiling_change()
        agent_mod.reproject_for_profile_change()
        assert rebuilds == [False]

    def test_a_declined_rebuild_is_retried_on_the_backoff_not_every_tick(
        self, profiles_dir, rebuilds, monkeypatch
    ):
        """A declined rebuild logs a refusal and records an audit event per attempt, so a
        short watch interval must not turn one pending projection into a stream of them.
        It is still retried, because on a host with no distribution poll and nothing held
        the watch is the only retry a decline has."""
        attempts: list[int] = []
        declined = {"value": True}
        clock = {"now": 1000.0}

        def maybe_declined(**kw):
            attempts.append(1)
            return Path("/agents/kirocrew.json"), not declined["value"]

        _boot()
        monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", maybe_declined)
        monkeypatch.setattr(agent_mod, "_watch_clock", lambda: clock["now"])
        _save_profile_governing_the_ref(profiles_dir)
        for _ in range(4):
            agent_mod.reproject_for_profile_change()
        assert attempts == [1]

        declined["value"] = False
        clock["now"] += agent_mod._PROFILE_WATCH_RETRY_S
        agent_mod.reproject_for_profile_change()
        assert attempts == [1, 1], "the decline cleared, and the backoff retried it"
        clock["now"] += agent_mod._PROFILE_WATCH_RETRY_S
        agent_mod.reproject_for_profile_change()
        assert attempts == [1, 1], "once projected, nothing is retried"

    def test_a_raising_rebuild_is_retried_on_a_short_backoff(
        self, profiles_dir, rebuilds, monkeypatch
    ):
        """A raise is often transient, so it is retried well before the hourly backoff,
        but not every tick: a persistent one would rewrite the spec kiro-cli watches and
        log a failure every few seconds for the life of the process."""
        attempts: list[int] = []
        clock = {"now": 1000.0}

        def flaky(**kw):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("could not write the agent config")
            return Path("/agents/kirocrew.json"), True

        _boot()
        monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", flaky)
        monkeypatch.setattr(agent_mod, "_watch_clock", lambda: clock["now"])
        _save_profile_governing_the_ref(profiles_dir)
        with pytest.raises(OSError):
            agent_mod.reproject_for_profile_change()
        agent_mod.reproject_for_profile_change()
        assert len(attempts) == 1, "a raise is not retried on the very next tick"
        clock["now"] += agent_mod._PROFILE_WATCH_RAISE_RETRY_S
        agent_mod.reproject_for_profile_change()
        assert len(attempts) == 2
        assert agent_mod._PROFILE_WATCH_RAISE_RETRY_S < agent_mod._PROFILE_WATCH_RETRY_S

    def test_the_gateway_runs_the_watch_off_the_loop_and_stops_it_at_shutdown(self):
        import inspect

        import kiro_crew.slack.gateway as gw

        src = Path(gw.__file__).read_text(encoding="utf-8")
        assert "await asyncio.to_thread(reproject_for_profile_change)" in src
        # Started ahead of the block that seeds the baseline and starts the refresher,
        # so a failure there does not take the watch with it.
        assert src.index("self._profile_watch_task = asyncio.create_task(") < src.index(
            "await asyncio.to_thread(prime_ceiling_projection)"
        )
        shutdown = inspect.getsource(gw.GatewayOrchestrator._shutdown)
        assert "self._profile_watch_task" in shutdown
        assert "profile_watch.cancel()" in shutdown


class TestARaisedRebuildCannotBeErasedByALaterCompletion:
    def test_a_raise_clears_the_memo_itself(self, profiles_dir, rebuilds, monkeypatch):
        """A rebuild that raises may have written grants from an older answer. A later
        rebuild completing must not be able to reset that, so the raise clears the
        recorded projection directly instead of only setting a flag."""
        _boot()
        assert agent_mod._projected_ceiling_generation is not None
        monkeypatch.setattr(agent_mod, "_rebuild_incomplete", False, raising=False)

        @agent_mod._tracks_rebuild_outcome
        def wrote_then_raised():
            raise PermissionError("lite spec write refused")

        with pytest.raises(PermissionError):
            wrote_then_raised()
        assert agent_mod._projected_ceiling_generation is None
        # A later rebuild's completion resets the flag, but the memo stays cleared, so
        # the next tick still re-projects over the failed rebuild's write.
        agent_mod._rebuild_incomplete = False
        agent_mod.reproject_for_profile_change()
        assert rebuilds == [True]


class TestAnUnlockedRebuildThatFinishesLastIsNotTrusted:
    """A dashboard save or config change rebuilds without the projection lock. One that
    derived its grants under an older answer can finish after a projection under the
    newer answer was recorded, so on completion it clears the memo when the answer moved
    since it started, and the next tick re-projects."""

    def test_a_stale_rebuild_clears_the_memo(self, profiles_dir, rebuilds):
        _boot()
        started_under = gp.governance_answer_generation()
        _save_profile_governing_the_ref(profiles_dir)
        agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [False]
        assert agent_mod._projected_ceiling_generation is not None

        agent_mod._invalidate_projection_if_answer_moved(started_under)
        assert agent_mod._projected_ceiling_generation is None
        agent_mod.reproject_for_profile_change()
        assert rebuilds == [False, False], "the next tick re-projected over the stale write"

    def test_a_stale_rebuild_that_raised_is_re_projected(self, profiles_dir, rebuilds):
        """A rebuild can write its grants and then raise further down (an installer
        failure re-raised at its end), so it never reaches the completion check. Its
        incomplete mark is what keeps the watch from trusting the memo it overtook."""
        _boot()
        _save_profile_governing_the_ref(profiles_dir)
        agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [False]
        # The unlocked rebuild that wrote older grants and then raised.
        agent_mod._rebuild_incomplete = True
        agent_mod.reproject_for_profile_change()
        assert rebuilds == [False, False]
        # The fake rebuild never completes a real one, so the mark stands; a real
        # completed rebuild clears it, after which the watch is quiet again.
        agent_mod._rebuild_incomplete = False
        agent_mod.reproject_for_profile_change()
        assert rebuilds == [False, False]

    def test_a_rebuild_under_the_current_answer_leaves_the_memo(self, profiles_dir, rebuilds):
        _boot()
        projected = agent_mod._projected_ceiling_generation
        agent_mod._invalidate_projection_if_answer_moved(gp.governance_answer_generation())
        assert agent_mod._projected_ceiling_generation == projected


class TestOnlyARaisedRebuildIsIncomplete:
    """The watch distrusts the memo after a rebuild that RAISED, and only then: one still
    running is covered by its own completion check, so treating it as raised would start
    a second, overlapping rebuild on every tick that lands during a dashboard save."""

    def test_a_rebuild_that_raises_marks_itself_and_one_that_runs_does_not(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "_rebuild_incomplete", False, raising=False)
        seen_while_running: list[bool] = []

        @agent_mod._tracks_rebuild_outcome
        def running_then_ok():
            seen_while_running.append(agent_mod._rebuild_incomplete)
            return "ok"

        @agent_mod._tracks_rebuild_outcome
        def raising():
            raise OSError("sharing violation")

        assert running_then_ok() == "ok"
        assert seen_while_running == [False]
        assert agent_mod._rebuild_incomplete is False
        with pytest.raises(OSError):
            raising()
        assert agent_mod._rebuild_incomplete is True

    def test_the_rebuild_is_tracked_and_keeps_its_contract(self):
        import inspect

        assert agent_mod.install_agent is agent_mod.rebuild_agent_config
        assert agent_mod.rebuild_agent_config.__wrapped__.__name__ == "rebuild_agent_config"
        assert "clean" in inspect.signature(agent_mod.rebuild_agent_config).parameters

    def test_a_watch_tick_during_a_running_rebuild_starts_none(self, profiles_dir, rebuilds):
        _boot()
        agent_mod.reproject_for_profile_change()
        assert rebuilds == [], "a rebuild in flight is not a reason to re-project"


class TestConcurrentProjectionsAreSerialized:
    def test_a_rebuild_derived_under_an_older_answer_cannot_finish_last(
        self, profiles_dir, rebuilds, monkeypatch
    ):
        """The hook runs on the refresher thread and from the profile watch. Unserialized,
        a rebuild that read the old answer could write after one that read the new answer
        and still leave the memo saying the new answer is projected."""
        import threading

        _boot()
        inside = threading.Event()
        release = threading.Event()
        active = {"n": 0, "max": 0}
        lock = threading.Lock()

        def slow(**kw):
            with lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            inside.set()
            release.wait(timeout=5)
            with lock:
                active["n"] -= 1
            return Path("/agents/kirocrew.json"), True

        monkeypatch.setattr(agent_mod, "rebuild_agent_config_reporting", slow)
        _save_profile_governing_the_ref(profiles_dir)
        first = threading.Thread(target=agent_mod.reproject_for_ceiling_change)
        first.start()
        assert inside.wait(timeout=5)
        second = threading.Thread(target=agent_mod.reproject_for_profile_change)
        second.start()
        second.join(timeout=0.3)
        assert second.is_alive(), "the watch ran while a projection was in flight"
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert active["max"] == 1
        assert agent_mod._projected_ceiling_generation == gp.governance_answer_generation()


class TestTheBaselineSeed:
    @pytest.fixture(autouse=True)
    def _fresh_memo(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(agent_mod, "_conductor_spec_held", False, raising=False)
        _floor_monkeypatch.setattr(agent_mod, "_projected_ceiling_generation", None, raising=False)
        _floor_monkeypatch.setattr(agent_mod, "_rebuild_answer_generation", None, raising=False)
        _floor_monkeypatch.setattr(agent_mod, "_rebuild_incomplete", False, raising=False)

    def test_a_loaded_store_is_seeded_without_a_restat(self, profiles_dir, monkeypatch):
        """Boot loaded the store, so the seed reads its published generation as it is.
        A re-stat here would fold an edit made since the boot rebuild into the baseline
        and mark it projected when it never was."""
        gp.poll_profiles_fresh()

        def walked():
            raise AssertionError("prime_ceiling_projection re-stat'd a loaded profile store")

        monkeypatch.setattr(gp, "poll_profiles_fresh", walked)
        monkeypatch.setattr(gp, "_dir_fingerprint", lambda d: walked())
        agent_mod.prime_ceiling_projection()
        assert agent_mod._projected_ceiling_generation == gp.governance_answer_generation()

    def test_an_edit_between_boot_and_the_seed_still_reads_as_a_move(self, profiles_dir, rebuilds):
        gp.poll_profiles_fresh()
        _save_profile_governing_the_ref(profiles_dir)
        agent_mod.prime_ceiling_projection()
        agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [False]

    def test_the_seed_is_the_answer_the_boot_rebuild_read(self, profiles_dir, rebuilds):
        """The boot rebuild and the seed are far apart, and any profile reader between
        them (a tool-approval check, a dashboard read) re-stats and publishes an edit.
        The seed must be the answer the rebuild derived under, not the one published
        since, or that edit is absorbed into the baseline and never projected."""
        gp.poll_profiles_fresh()
        agent_mod._rebuild_answer_generation = agent_mod._answer_generation_after_profile_poll()
        _save_profile_governing_the_ref(profiles_dir)
        gp.poll_profiles_fresh()  # another reader publishes the edit before the seed
        agent_mod.prime_ceiling_projection()
        agent_mod.reproject_for_ceiling_change()
        assert rebuilds == [False]

    def test_a_rebuild_records_the_answer_before_deriving_grants(self, profiles_dir, monkeypatch):
        import inspect

        src = inspect.getsource(agent_mod.rebuild_agent_config)
        record = src.index("answer_generation = _answer_generation_after_profile_poll()")
        assert record < src.index("_load_existing_config(path")
        assert src.index("declined = _decline_shared_agent_home()") < record
        # Published, and checked against the live answer, only once the rebuild completed:
        # after the sibling pass, which re-raises a dashboard-author install error.
        publish = src.index("_rebuild_answer_generation = answer_generation")
        assert src.index("_install_sibling_specs(refresh_forks") < publish
        sibling_src = inspect.getsource(agent_mod._install_sibling_specs)
        assert sibling_src.index("raise dashboard_author_install_error") < sibling_src.index(
            "return conductor_held"
        )
        assert publish < src.index("_invalidate_projection_if_answer_moved(answer_generation)")

    def test_a_seed_over_a_rebuild_that_raised_projects_nothing(self, profiles_dir, rebuilds):
        """A rebuild that raised may have rewritten some specs and not others, so the
        seed must not mark any answer projected over it: the first tick rebuilds."""
        gp.poll_profiles_fresh()
        agent_mod._rebuild_answer_generation = gp.governance_answer_generation()
        agent_mod._rebuild_incomplete = True
        agent_mod.prime_ceiling_projection()
        assert agent_mod._projected_ceiling_generation is None
        agent_mod.reproject_for_profile_change()
        assert rebuilds == [True]

    def test_a_never_loaded_store_is_not_a_move(self, profiles_dir, rebuilds):
        """No grant consulted the profiles, so the store's first load is not a change
        to anything derived from them, and the first poll rebuilds nothing."""
        assert gp.profile_store_loaded() is False
        agent_mod.prime_ceiling_projection()
        agent_mod.reproject_for_ceiling_change()
        agent_mod.reproject_for_profile_change()
        assert rebuilds == []

    def test_the_gateway_seeds_off_the_loop(self):
        import kiro_crew.slack.gateway as gw

        src = Path(gw.__file__).read_text(encoding="utf-8")
        assert "await asyncio.to_thread(prime_ceiling_projection)" in src
