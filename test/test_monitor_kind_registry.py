"""Contract for the monitored-kind registry.

The point of the registry is that adding a kind is DATA, so the tests that matter
most are the ones that would fail if a kind's objectives leaked into a shared
vocabulary, or if a capability became something the registry asserts on a kind's
behalf rather than something the kind answers for.
"""

from __future__ import annotations

import pytest

from kiro_crew.monitoring import registry
from kiro_crew.monitoring.registry import (
    GH_PR,
    GITHUB_PULL_REQUEST,
    REVIEW_READY,
    MonitorKind,
    kind_supports_objective,
    kind_supports_shadow,
    monitor_kind,
    publicly_armable_kinds,
    publicly_armable_objectives,
)


class TestTheEntryAllowlistsAreUnchanged:
    """The four boundaries must expose exactly what they hardcoded before.

    This is the behaviour lock for this change: the registry replaces four literal
    allowlists, and replacing them is only safe if what a caller may ask for is
    identical.
    """

    def test_the_publicly_armable_kind_set_is_the_one_the_schema_hardcoded(self) -> None:
        assert publicly_armable_kinds() == frozenset({GITHUB_PULL_REQUEST})

    def test_the_publicly_armable_objective_set_is_the_one_the_schema_hardcoded(self) -> None:
        assert publicly_armable_objectives() == frozenset({REVIEW_READY})

    def test_the_validator_schemas_derive_the_same_values(self) -> None:
        from kiro_crew.validation import MONITOR_WATCH_SCHEMA

        allowed = {field.name: field.allowed for field in MONITOR_WATCH_SCHEMA.fields}
        assert allowed["kind"] == frozenset({GITHUB_PULL_REQUEST})
        assert allowed["objective"] == frozenset({REVIEW_READY})


class TestAKindDeclaresItsOwnObjectives:
    """No shared objective vocabulary. This is the property the change exists for."""

    def test_a_new_kind_declaring_a_new_objective_does_not_widen_another_kind(self) -> None:
        """The test that would fail if objectives lived in one shared enum."""
        before = monitor_kind(GITHUB_PULL_REQUEST)
        assert before is not None

        newcomer = MonitorKind(
            name="calendar",
            objectives=frozenset({"free_slot"}),
            publicly_armable=True,
            supports_shadow=False,
        )

        assert newcomer.objectives == frozenset({"free_slot"})
        assert monitor_kind(GITHUB_PULL_REQUEST) == before
        assert not kind_supports_objective(GITHUB_PULL_REQUEST, "free_slot")

    def test_an_objective_one_kind_declares_is_not_thereby_legal_for_another(self) -> None:
        assert kind_supports_objective(GITHUB_PULL_REQUEST, REVIEW_READY)
        assert not kind_supports_objective(GITHUB_PULL_REQUEST, "free_slot")

    def test_an_unregistered_kind_supports_nothing(self) -> None:
        assert not kind_supports_objective("calendar", REVIEW_READY)
        assert monitor_kind("calendar") is None

    def test_a_non_string_kind_is_refused_rather_than_raising(self) -> None:
        assert monitor_kind(None) is None
        assert monitor_kind(7) is None
        assert not kind_supports_objective(GITHUB_PULL_REQUEST, None)


class TestShadowSupportIsADeclaredCapability:
    """A capability is a fact about what code exists, so the kind answers for it.

    Modelled as an allowlist the registry polices, a kind added later would inherit
    a claim about a path it has never run.
    """

    def test_the_kind_with_a_shadow_implementation_declares_it(self) -> None:
        assert kind_supports_shadow(GITHUB_PULL_REQUEST)

    def test_a_registered_kind_without_that_path_declares_false(self) -> None:
        assert monitor_kind(GH_PR) is not None
        assert not kind_supports_shadow(GH_PR)

    def test_an_unregistered_kind_answers_false_rather_than_inheriting_a_claim(self) -> None:
        assert not kind_supports_shadow("calendar")

    def test_capability_is_independent_of_being_publicly_armable(self) -> None:
        """The two properties answer different questions and must not be conflated.

        One is about what a caller may request; the other about what is implemented.
        """
        entry = MonitorKind(
            name="ticket",
            objectives=frozenset({"triaged"}),
            publicly_armable=False,
            supports_shadow=True,
        )

        assert not entry.publicly_armable
        assert entry.supports_shadow


class TestRegisteredButNotRequestable:
    """A kind derived internally is registered without being namable by a caller."""

    def test_the_internally_armed_kind_is_registered(self) -> None:
        entry = monitor_kind(GH_PR)

        assert entry is not None
        assert entry.objectives == frozenset({REVIEW_READY})

    def test_but_a_caller_may_not_name_it(self) -> None:
        assert GH_PR not in publicly_armable_kinds()

    def test_and_it_still_supports_its_objective_for_internal_arming(self) -> None:
        """Registered-not-requestable must not mean unusable."""
        assert kind_supports_objective(GH_PR, REVIEW_READY)


class TestEveryRegisteredEntryIsWellFormed:
    """Over ``_KINDS`` rather than in ``__post_init__``.

    Nothing constructs ``MonitorKind`` outside this module's static literal, so a
    runtime guard would only ever validate a literal. These run over whatever the
    table holds, so an entry added later is checked without shipping code to do it.
    """

    def test_each_entry_is_keyed_by_its_own_name(self) -> None:
        """A mismatch would make a lookup answer about a different kind."""
        for key, entry in registry._KINDS.items():
            assert key == entry.name

    def test_each_entry_has_a_non_empty_name(self) -> None:
        for entry in registry._KINDS.values():
            assert entry.name

    def test_each_entry_declares_at_least_one_objective(self) -> None:
        """A kind declaring nothing could be armed for no objective at all."""
        for entry in registry._KINDS.values():
            assert entry.objectives

    def test_no_declared_objective_is_empty(self) -> None:
        for entry in registry._KINDS.values():
            assert all(entry.objectives), entry.name


class TestTheTwoKindVocabulariesAgree:
    """``MonitorState.kind`` is a union of two vocabularies with no import edge.

    ``probes/__init__.py`` owns ``gh-pr`` for the irq subsystem and this module
    spells it again, because coupling the two packages for a constant would put an
    import edge between them ahead of the porting work that unifies them. That makes
    drift the real hazard, so it is pinned HERE: this test reads the spelling the
    inference path actually produces and asserts the registry knows it.
    """

    def test_the_kind_inference_produces_is_registered(self) -> None:
        from kiro_crew.probes import targets

        target = targets.infer("please babysit https://github.com/owner/repo/pull/4137")

        assert target is not None, "inference must still resolve a single PR URL"
        assert monitor_kind(target.kind) is not None, (
            f"probes.targets.infer produced kind {target.kind!r}, which this registry "
            "does not know -- the two spellings have drifted apart"
        )

    def test_that_kind_declares_the_objective_the_inference_path_arms(self) -> None:
        """``infer_monitor`` pairs the inferred kind with ``review_ready``."""
        from kiro_crew.probes import targets

        target = targets.infer("babysit https://github.com/owner/repo/pull/4137")

        assert target is not None
        assert kind_supports_objective(target.kind, REVIEW_READY)

    def test_the_two_modules_spell_it_identically(self) -> None:
        from kiro_crew.probes import GH_PR as PROBES_GH_PR

        assert GH_PR == PROBES_GH_PR


@pytest.mark.asyncio
async def test_shadow_refuses_a_kind_that_does_not_declare_the_capability() -> None:
    """The refusal names the missing capability, not an allowlist.

    ``gh-pr`` is a REGISTERED kind, so this is not "unknown kind" -- it is a kind
    whose persistence-only path is not implemented, which is the distinction the
    declared capability exists to carry.
    """
    from kiro_crew.monitoring.models import MonitorState
    from kiro_crew.monitoring.shadow import run_shadow_probe

    probes = 0

    class Provider:
        def probe(self, subjects: object, **kwargs: object) -> dict[str, object]:
            nonlocal probes
            probes += 1
            raise AssertionError("the capability check must precede the provider")

    async def persist(updated: MonitorState) -> None:
        raise AssertionError("nothing may be persisted for an unsupported kind")

    state = MonitorState(
        kind=GH_PR,
        target="https://github.com/owner/repo/pull/123",
        objective=REVIEW_READY,
        created_ts=1_000.0,
    )

    with pytest.raises(ValueError, match="shadow mode is not implemented"):
        await run_shadow_probe(state, Provider(), persist, now=1_100.0)
    assert probes == 0


@pytest.mark.asyncio
async def test_shadow_refuses_an_objective_the_kind_does_not_declare() -> None:
    from kiro_crew.monitoring.models import MonitorState
    from kiro_crew.monitoring.shadow import run_shadow_probe

    class Provider:
        def probe(self, subjects: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("the objective check must precede the provider")

    async def persist(updated: MonitorState) -> None:
        raise AssertionError("nothing may be persisted")

    state = MonitorState(
        kind=GITHUB_PULL_REQUEST,
        target="https://github.com/owner/repo/pull/123",
        objective="free_slot",
        created_ts=1_000.0,
    )

    with pytest.raises(ValueError, match="does not declare objective"):
        await run_shadow_probe(state, Provider(), persist, now=1_100.0)


@pytest.mark.asyncio
async def test_arming_refuses_an_unregistered_kind_before_it_is_persisted(tmp_path) -> None:
    """The hole the registry closes: MonitorState itself accepts any non-empty kind.

    A monitor stored under a kind nothing registered would be probed by whatever
    provider the controller happens to hold, so the refusal belongs at the arming
    boundary rather than at the point of discovery.
    """
    from kiro_crew.autonudge import AutoNudgeService
    from kiro_crew.monitoring.models import MonitorBudgets, MonitorState

    # The record itself is still permissive, which is why the boundary must check.
    MonitorState(
        kind="calendar",
        target="https://github.com/owner/repo/pull/1",
        objective=REVIEW_READY,
        created_ts=1_000.0,
    )

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        with pytest.raises(ValueError, match="no monitored kind 'calendar'"):
            await service.add_monitor(
                slot_key="chat-1",
                kind="calendar",
                target="https://github.com/owner/repo/pull/1",
                objective=REVIEW_READY,
                cadence_secs=60,
                budgets=MonitorBudgets(),
                now=100.0,
            )
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_updating_an_objective_the_kind_does_not_declare_is_refused(tmp_path) -> None:
    """The update path is the other boundary that knows both halves.

    The objective allowlist upstream is a UNION across every publicly armable kind,
    so it cannot express the pairing: it admits an objective some other kind
    declares. This test drives update_monitor directly for that reason -- the point
    is that the pairing holds without the upstream filter's help, which is what will
    still be true once a second kind widens the union.
    """
    from kiro_crew.autonudge import AutoNudgeService
    from kiro_crew.monitoring.models import MonitorBudgets

    service = AutoNudgeService(base_dir=tmp_path)
    try:
        loop = await service.add_monitor(
            slot_key="chat-1",
            kind=GITHUB_PULL_REQUEST,
            target="https://github.com/owner/repo/pull/1",
            objective=REVIEW_READY,
            cadence_secs=60,
            budgets=MonitorBudgets(),
            now=100.0,
        )
        assert loop is not None

        with pytest.raises(ValueError, match="does not support objective 'free_slot'"):
            await service.update_monitor(loop.id, objective="free_slot")

        # The refusal must not have half-applied.
        after = service._loops[loop.id].monitor
        assert after is not None
        assert after.objective == REVIEW_READY

        # And the objective the kind DOES declare still applies.
        again = await service.update_monitor(loop.id, objective=REVIEW_READY)
        assert again is not None
    finally:
        service.stop()
