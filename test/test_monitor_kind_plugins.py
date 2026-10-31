"""Contract for boot-time registration of contributed monitor kinds.

A downstream edition registers a pull-request-shaped kind once, at boot, and every
boundary that gates a kind must then accept it: the ``monitor_watch`` validator,
target inference and normalization, the persisted-monitor objective check, and the
controller's default provider table. The refusals matter as much as the
acceptances: a contributed kind must never shadow a built-in, claim a built-in
provider's URL, or declare an objective the pull-request path does not implement.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.monitoring import plugins as plugins_module
from kiro_crew.monitoring import registry
from kiro_crew.monitoring.controller import MonitorController
from kiro_crew.monitoring.models import MonitorBudgets, MonitorCreationSurface
from kiro_crew.monitoring.plugins import (
    MonitorKindPlugin,
    register_monitor_kind,
    registered_monitor_kind,
    registered_monitor_kinds,
)
from kiro_crew.monitoring.pull_request import PullRequestFacts, build_pull_request_probe_result
from kiro_crew.monitoring.registry import (
    GITHUB_PULL_REQUEST,
    REVIEW_READY,
    MonitorKind,
    is_pull_request_monitor_kind,
    kind_supports_objective,
    publicly_armable_kinds,
)
from kiro_crew.monitoring.targets import infer_pull_request_kind, normalize_pull_request_target
from kiro_crew.validation import MONITOR_WATCH_SCHEMA
from kiro_crew.validation_parts.field_engine import ValidationError, validate_tool_args

KIND = "example_code_review"
HOST = "https://review.example.test/reviews/"


def _parse(raw: str) -> str:
    if not raw.startswith(HOST) or not raw[len(HOST) :].isdigit():
        raise ValueError("not an example review")
    return raw


def _entry(name: str = KIND, objectives: frozenset[str] = frozenset({REVIEW_READY})) -> MonitorKind:
    return MonitorKind(
        name=name,
        objectives=objectives,
        publicly_armable=True,
        supports_shadow=False,
        subject_noun="code review",
        wake_fields=("checks", "blocking_review", "review_decision", "state"),
    )


class _Provider:
    def __init__(self) -> None:
        self.subjects: list[str] = []
        self.owner_credentials: list[bool] = []

    def probe(self, subjects, *, previous_observations=None, use_owner_credentials=True):
        self.subjects.extend(subjects)
        self.owner_credentials.append(use_owner_credentials)
        return {
            subject: build_pull_request_probe_result(
                PullRequestFacts(
                    kind=KIND,
                    target=subject,
                    state="open",
                    draft=False,
                    head_revision="3",
                    mergeability="mergeable",
                    review_decision="review_required",
                    checks=(),
                    unresolved_review_threads=1,
                    review_threads_complete=True,
                )
            )
            for subject in subjects
        }


@pytest.fixture(autouse=True)
def _isolated_tables() -> Iterator[None]:
    """Snapshot and restore both module-global tables around every test.

    A refusal test that regressed into a successful registration would otherwise
    leave its entry behind for every later test on the same worker.
    """
    plugins_before = dict(plugins_module._PLUGINS)
    kinds_before = dict(registry._PLUGIN_KINDS)
    try:
        yield
    finally:
        plugins_module._PLUGINS.clear()
        plugins_module._PLUGINS.update(plugins_before)
        registry._PLUGIN_KINDS.clear()
        registry._PLUGIN_KINDS.update(kinds_before)


@pytest.fixture
def plugin() -> Iterator[MonitorKindPlugin]:
    provider = _Provider()
    registered = MonitorKindPlugin(
        kind=_entry(), provider_factory=lambda: provider, parse_target=_parse
    )
    register_monitor_kind(registered)
    yield registered


class TestRegistration:
    def test_a_registered_kind_reaches_every_registry_query(self, plugin) -> None:
        assert registry.monitor_kind(KIND) == plugin.kind
        assert KIND in publicly_armable_kinds()
        assert kind_supports_objective(KIND, REVIEW_READY)
        assert is_pull_request_monitor_kind(KIND)
        assert registered_monitor_kind(KIND) is plugin
        assert plugin in registered_monitor_kinds()

    def test_an_unregistered_kind_is_absent_everywhere(self) -> None:
        assert registry.monitor_kind(KIND) is None
        assert KIND not in publicly_armable_kinds()
        assert not is_pull_request_monitor_kind(KIND)
        assert registered_monitor_kind(KIND) is None

    def test_a_built_in_name_cannot_be_replaced(self) -> None:
        before = registry.monitor_kind(GITHUB_PULL_REQUEST)
        with pytest.raises(ValueError, match="built in"):
            register_monitor_kind(
                MonitorKindPlugin(
                    kind=_entry(GITHUB_PULL_REQUEST),
                    provider_factory=_Provider,
                    parse_target=_parse,
                )
            )
        assert registry.monitor_kind(GITHUB_PULL_REQUEST) == before
        assert registered_monitor_kind(GITHUB_PULL_REQUEST) is None

    def test_a_second_registration_of_the_same_name_is_refused(self, plugin) -> None:
        with pytest.raises(ValueError, match="already registered"):
            register_monitor_kind(
                MonitorKindPlugin(kind=_entry(), provider_factory=_Provider, parse_target=_parse)
            )
        assert registered_monitor_kind(KIND) is plugin

    @pytest.mark.parametrize("objectives", [frozenset({"any_change"}), frozenset()])
    def test_an_objective_set_the_pull_request_path_cannot_serve_is_refused(
        self, objectives: frozenset[str]
    ) -> None:
        with pytest.raises(ValueError, match="objectives"):
            register_monitor_kind(
                MonitorKindPlugin(
                    kind=_entry(objectives=objectives),
                    provider_factory=_Provider,
                    parse_target=_parse,
                )
            )
        assert registry.monitor_kind(KIND) is None

    def test_a_kind_claiming_the_shadow_path_is_refused(self) -> None:
        entry = MonitorKind(
            name=KIND,
            objectives=frozenset({REVIEW_READY}),
            publicly_armable=True,
            supports_shadow=True,
            subject_noun="code review",
            wake_fields=("state",),
        )
        with pytest.raises(ValueError, match="shadow"):
            register_monitor_kind(
                MonitorKindPlugin(kind=entry, provider_factory=_Provider, parse_target=_parse)
            )
        assert registry.monitor_kind(KIND) is None

    @pytest.mark.parametrize("name", ["", "Bad-Name", "x", "has space", "9starts_with_digit"])
    def test_a_malformed_name_is_refused(self, name: str) -> None:
        with pytest.raises(ValueError, match="snake_case"):
            register_monitor_kind(
                MonitorKindPlugin(
                    kind=_entry(name), provider_factory=_Provider, parse_target=_parse
                )
            )
        assert registry.monitor_kind(name) is None


class TestTargets:
    def test_inference_reaches_a_contributed_parser(self, plugin) -> None:
        assert infer_pull_request_kind(f"{HOST}42", gitlab_hosts=()) == KIND

    def test_a_built_in_url_is_never_claimed_by_a_contributed_parser(self) -> None:
        greedy = MonitorKindPlugin(
            kind=_entry(), provider_factory=_Provider, parse_target=lambda raw: raw
        )
        register_monitor_kind(greedy)
        assert (
            infer_pull_request_kind("https://github.com/acme/widgets/pull/7", gitlab_hosts=())
            == GITHUB_PULL_REQUEST
        )

    def test_normalization_uses_the_contributed_parser(self, plugin) -> None:
        assert normalize_pull_request_target(KIND, f"{HOST}42", gitlab_hosts=()) == f"{HOST}42"
        with pytest.raises(ValueError):
            normalize_pull_request_target(KIND, "https://github.com/a/b/pull/1", gitlab_hosts=())

    def test_a_parser_that_raises_anything_reads_as_not_its_url(self) -> None:
        def explode(raw: str) -> str:
            raise TypeError("parser bug")

        register_monitor_kind(
            MonitorKindPlugin(kind=_entry(), provider_factory=_Provider, parse_target=explode)
        )
        with pytest.raises(ValueError):
            infer_pull_request_kind(f"{HOST}42", gitlab_hosts=())
        with pytest.raises(ValueError):
            normalize_pull_request_target(KIND, f"{HOST}42", gitlab_hosts=())

    @pytest.mark.parametrize(
        "returned",
        [
            "http://reviews.example.test/r/42",
            "ftp://reviews.example.test/r/42",
            "file:///etc/passwd",
            "https:///r/42",
            "reviews.example.test/r/42",
        ],
    )
    def test_a_parser_returning_a_non_https_target_is_refused(self, returned: str) -> None:
        register_monitor_kind(
            MonitorKindPlugin(
                kind=_entry(), provider_factory=_Provider, parse_target=lambda raw: returned
            )
        )
        with pytest.raises(ValueError):
            normalize_pull_request_target(KIND, f"{HOST}42", gitlab_hosts=())
        with pytest.raises(ValueError):
            infer_pull_request_kind("https://unclaimed.example.test/x", gitlab_hosts=())

    def test_an_unregistered_kind_still_refuses(self) -> None:
        with pytest.raises(ValueError, match="not a supported"):
            normalize_pull_request_target(KIND, f"{HOST}42", gitlab_hosts=())


class TestValidatorReadsTheLiveVocabulary:
    """The schema is built at import, before any edition boots; it must not freeze."""

    def _args(self) -> dict[str, object]:
        return {"kind": KIND, "target": f"{HOST}42", "objective": REVIEW_READY}

    def test_a_kind_registered_after_import_validates(self, plugin) -> None:
        assert validate_tool_args(self._args(), MONITOR_WATCH_SCHEMA)["kind"] == KIND

    def test_the_same_kind_is_refused_once_unregistered(self) -> None:
        with pytest.raises(ValidationError):
            validate_tool_args(self._args(), MONITOR_WATCH_SCHEMA)


class TestController:
    @pytest.mark.asyncio
    async def test_the_default_provider_table_includes_a_contributed_kind(
        self, tmp_path, plugin
    ) -> None:
        service = AutoNudgeService(base_dir=tmp_path)
        loop = await service.add_monitor(
            slot_key="chat-1",
            kind=KIND,
            target=f"{HOST}42",
            objective=REVIEW_READY,
            cadence_secs=60,
            budgets=MonitorBudgets(max_runtime_secs=600),
            wake_instructions="",
            now=100.0,
        )
        controller = MonitorController(service, AsyncMock())

        await controller.tick(loop, now=120.0)

        provider = plugin.provider_factory()
        assert provider.subjects == [f"{HOST}42"]
        assert loop.monitor is not None
        assert loop.monitor.last_observation["kind"] == KIND

    @pytest.mark.parametrize(
        "surface", [MonitorCreationSurface.CHANNEL, MonitorCreationSurface.DASHBOARD]
    )
    @pytest.mark.asyncio
    async def test_a_contributed_kind_gets_no_owner_credentials_without_a_recorded_grant(
        self, tmp_path, plugin, surface
    ) -> None:
        """Channel monitors are fail-closed outright; dashboard ones need the grant."""
        service = AutoNudgeService(base_dir=tmp_path)
        loop = await service.add_monitor(
            slot_key="chat-1",
            kind=KIND,
            target=f"{HOST}42",
            objective=REVIEW_READY,
            cadence_secs=60,
            budgets=MonitorBudgets(max_runtime_secs=600),
            wake_instructions="",
            now=100.0,
            creation_surface=surface,
        )
        assert loop.monitor is not None and loop.monitor.creation_surface is surface
        controller = MonitorController(service, AsyncMock())

        await controller.tick(loop, now=120.0)

        assert plugin.provider_factory().owner_credentials == [False]

    @pytest.mark.asyncio
    async def test_a_factory_that_raises_fails_only_its_own_kind(self, tmp_path) -> None:
        def broken() -> _Provider:
            raise RuntimeError("client setup failed")

        register_monitor_kind(
            MonitorKindPlugin(kind=_entry(), provider_factory=broken, parse_target=_parse)
        )

        controller = MonitorController(AutoNudgeService(base_dir=tmp_path), AsyncMock())

        assert KIND not in controller._providers
        assert GITHUB_PULL_REQUEST in controller._providers
