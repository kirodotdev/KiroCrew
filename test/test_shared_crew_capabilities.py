"""Capability aggregation and the ``/api/shared-crew/capabilities`` route."""

from __future__ import annotations

from typing import Iterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.shared_crew import capabilities as caps_mod
from kiro_crew.shared_crew.capabilities import (
    CAPABILITY_FIELDS,
    REASON_NO_SOURCE,
    REASON_SOURCE_FAILED,
    CapabilityContribution,
    aggregate_capabilities,
    register_capability_source,
    registered_capability_sources,
    unregister_capability_source,
)
from kiro_crew.shared_crew.contracts import (
    MEMBERSHIP_LINK_CONSOLE,
    MEMBERSHIP_LINK_TAILSCALE,
    Capabilities,
    CapabilityState,
    EnforcementLevel,
)
from kiro_crew.shared_crew.routes_capabilities import CAPABILITIES_PATH, register

A = CapabilityState.AVAILABLE
D = CapabilityState.DEGRADED


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    caps_mod._reset_capability_sources_for_tests()
    yield
    caps_mod._reset_capability_sources_for_tests()


def _const(contribution: CapabilityContribution):
    return lambda: contribution


class TestAggregation:
    def test_nothing_registered_reports_contract_default(self) -> None:
        report = aggregate_capabilities()
        assert report.capabilities == Capabilities()
        assert report.reasons == {f: REASON_NO_SOURCE for f in CAPABILITY_FIELDS}

    def test_source_answers_its_declared_fields(self) -> None:
        register_capability_source(
            "identity",
            {"attribution", "authoritative_directory"},
            _const(CapabilityContribution(states={"attribution": A})),
        )
        report = aggregate_capabilities()
        assert report.capabilities.attribution is A
        assert report.capabilities.authoritative_directory is CapabilityState.UNAVAILABLE
        assert "attribution" not in report.reasons
        assert report.reasons["presence"] == REASON_NO_SOURCE

    def test_undeclared_field_is_ignored(self) -> None:
        register_capability_source(
            "presence",
            {"presence"},
            _const(CapabilityContribution(states={"presence": A, "queueing": A})),
        )
        caps = aggregate_capabilities().capabilities
        assert caps.presence is A and caps.queueing is CapabilityState.UNAVAILABLE

    def test_weaker_answer_wins(self) -> None:
        register_capability_source(
            "b", {"presence"}, _const(CapabilityContribution({"presence": A}))
        )
        register_capability_source(
            "d", {"presence"}, _const(CapabilityContribution({"presence": D}))
        )
        assert aggregate_capabilities().capabilities.presence is D

    def test_failing_source_claims_nothing(self) -> None:
        def boom() -> CapabilityContribution:
            raise RuntimeError("boom")

        register_capability_source("q", {"queueing", "containment", "containment_level"}, boom)
        report = aggregate_capabilities()
        assert report.capabilities.queueing is CapabilityState.UNAVAILABLE
        assert report.capabilities.containment_level is EnforcementLevel.NONE
        assert report.reasons["queueing"] == REASON_SOURCE_FAILED

    def test_non_contribution_answer_is_a_failure(self) -> None:
        register_capability_source("q", {"queueing"}, lambda: {"queueing": "available"})  # type: ignore[arg-type,return-value]
        assert aggregate_capabilities().reasons["queueing"] == REASON_SOURCE_FAILED

    def test_level_not_claimed_without_running_containment(self) -> None:
        register_capability_source(
            "c",
            {"containment", "containment_level"},
            _const(
                CapabilityContribution(
                    states={"containment": CapabilityState.DISABLED},
                    containment_level=EnforcementLevel.HARD,
                )
            ),
        )
        assert aggregate_capabilities().capabilities.containment_level is EnforcementLevel.NONE

    def test_weakest_level_wins(self) -> None:
        for name, level in (
            ("hard", EnforcementLevel.HARD),
            ("soft", EnforcementLevel.BEST_EFFORT),
        ):
            register_capability_source(
                name,
                {"containment", "containment_level"},
                _const(CapabilityContribution({"containment": A}, containment_level=level)),
            )
        assert aggregate_capabilities().capabilities.containment_level is (
            EnforcementLevel.BEST_EFFORT
        )

    def test_conflicting_membership_links_leave_none(self) -> None:
        register_capability_source(
            "t",
            {"membership_link"},
            _const(CapabilityContribution(membership_link=MEMBERSHIP_LINK_TAILSCALE)),
        )
        assert aggregate_capabilities().capabilities.membership_link == MEMBERSHIP_LINK_TAILSCALE
        register_capability_source(
            "c",
            {"membership_link"},
            _const(CapabilityContribution(membership_link=MEMBERSHIP_LINK_CONSOLE)),
        )
        assert aggregate_capabilities().capabilities.membership_link == ""

    def test_url_link_never_passes(self) -> None:
        register_capability_source(
            "t",
            {"membership_link"},
            _const(CapabilityContribution(membership_link="https://evil.example")),
        )
        assert aggregate_capabilities().capabilities.membership_link == ""

    def test_registration_errors(self) -> None:
        source = _const(CapabilityContribution())
        with pytest.raises(ValueError):
            register_capability_source("x", {"not_a_field"}, source)
        with pytest.raises(ValueError):
            register_capability_source("x", set(), source)
        with pytest.raises(ValueError):
            register_capability_source("", {"presence"}, source)
        register_capability_source("x", {"presence"}, source)
        with pytest.raises(ValueError):
            register_capability_source("x", {"presence"}, source)
        assert registered_capability_sources() == {"x": frozenset({"presence"})}
        unregister_capability_source("x")
        assert registered_capability_sources() == {}

    def test_report_round_trips_through_the_contract(self) -> None:
        register_capability_source(
            "i", {"attribution"}, _const(CapabilityContribution({"attribution": A}))
        )
        body = aggregate_capabilities().to_dict()
        assert body["attribution"] == A.value


class TestRoute:
    @pytest.mark.asyncio
    async def test_default_build_reports_everything_unavailable(self) -> None:
        app = web.Application()
        register(app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(CAPABILITIES_PATH)
            assert resp.status == 200
            assert resp.headers["Cache-Control"] == "no-store"
            body = await resp.json()
        for f in ("attribution", "presence", "queueing", "containment", "remote_actions"):
            assert body[f] == "unavailable"
        assert body["containment_level"] == "none"
        assert body["membership_link"] == ""
        assert body["protocols"] == [1]
        assert set(body["reasons"]) == set(CAPABILITY_FIELDS)

    @pytest.mark.asyncio
    async def test_route_carries_no_references_or_secrets(self) -> None:
        app = web.Application()
        register(app)
        async with TestClient(TestServer(app)) as client:
            text = await (await client.get(CAPABILITIES_PATH)).text()
        for needle in ("http", "ref", "key", "token", "secret", "tenant"):
            assert needle not in text.lower(), needle
