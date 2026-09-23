"""The producer half of the crew log's external-state record (``object/observed``).

A structured monitor's probe already computes a canonical snapshot of the pull
request it watches. These tests pin what happens to that snapshot now: it is
appended into the OWNER session's crew log exactly once per change of the probe's
fingerprint, carrying the producer that made it, and never on an unchanged poll, a
failed read, a declined observation, or a slot with no live session. The emitter's
producer vocabulary is closed and refuses free text.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.crew_log import CrewLog, CrewLogError
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log.entry_types import (
    OBJECT_PRODUCER_PROBE,
    OBJECT_PRODUCERS,
    SESSION_ENTRY_TYPES,
    validate_data,
)
from kiro_crew.crew_log.schema import KIND_SESSION, MAX_ENTRY_BYTES
from kiro_crew.monitoring.controller import MonitorController
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProbeResult
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorDecision,
    MonitorDispatchResult,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorProbeResult,
    MonitorVerdict,
    ProviderErrorKind,
)

SLOT = "chat-1"
SESSION = "acp-owner"
KIND = "github_pull_request"
TARGET = "https://github.com/acme/widgets/pull/7"
ENTRY_TYPE = "object/observed"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, crew log on, and no writer state carried between tests."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(crew_log_emit.CREW_LOG_ENV, "1")
    crew_log_emit.reset_caches()
    yield
    crew_log_emit.drain_for_shutdown(timeout=2.0)
    crew_log_emit.reset_caches()


def _unit(unit_id: str = SESSION) -> None:
    """Create the owner session's crew log and drop the handle so it holds no lease."""
    CrewLog.create(KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=SLOT)


def _observed(unit_id: str = SESSION) -> list:
    assert crew_log_emit.flush(timeout=5.0)
    handle = CrewLog.open(KIND_SESSION, unit_id)
    try:
        return [entry for entry in handle.iter_from(1) if entry.type == ENTRY_TYPE]
    finally:
        del handle


def _canonical(*, head_revision: str = "abc123", passed: list[str] | None = None) -> dict:
    return {
        "kind": KIND,
        "target": TARGET,
        "state": "open",
        "draft": False,
        "head_revision": head_revision,
        "mergeability": "mergeable",
        "review_decision": "approved",
        "blocking_review": "none",
        "unresolved_review_threads": 0,
        "review_threads_complete": True,
        "checks": {
            "failed": [],
            "passed": ["ci"] if passed is None else passed,
            "pending": [],
            "unknown": [],
        },
        "checks_complete": True,
    }


def _result(
    status: MonitorObservationStatus = MonitorObservationStatus.PENDING,
    *,
    fingerprint: str = "fp-1",
    canonical: dict | None = None,
) -> GitHubPullRequestProbeResult:
    error = status is MonitorObservationStatus.PROVIDER_ERROR
    return GitHubPullRequestProbeResult(
        response=None,
        canonical={} if error else (canonical if canonical is not None else _canonical()),
        observation=MonitorObservation(
            "" if error else fingerprint,
            status,
            provider_error=ProviderErrorKind.TRANSIENT if error else None,
            reason_code="provider_transient" if error else "checks_pending",
        ),
    )


@dataclass
class _Provider:
    """Answers each probe with the next scripted result, repeating the last one."""

    results: list[GitHubPullRequestProbeResult]
    probe_count: int = 0

    def probe(self, subjects, *, previous_observations=None, use_owner_credentials=True):
        del previous_observations, use_owner_credentials
        result = self.results[min(self.probe_count, len(self.results) - 1)]
        self.probe_count += 1
        return {subject: result for subject in subjects}


async def _armed(tmp_path, results, *, owner=lambda loop: SESSION, dispatch=None):
    """A real service with one armed monitor, and a controller writing for *owner*."""
    service = AutoNudgeService(base_dir=tmp_path / "nudges")
    loop = await service.add_monitor(
        slot_key=SLOT,
        kind=KIND,
        target=TARGET,
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=6_000),
        wake_instructions="",
        now=100.0,
    )
    controller = MonitorController(
        service,
        dispatch or AsyncMock(return_value=MonitorDispatchResult.DISPATCHED),
        providers={KIND: _Provider(list(results))},
        owner_session_id=owner,
    )
    return service, loop, controller


# --------------------------------------------------------------------------- #
# the probe records a change, once, into the owner's log
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_changed_fingerprint_appends_exactly_one_typed_entry(tmp_path):
    """The first observation of a subject is a change from nothing, so it is recorded."""
    _unit()
    _service, loop, controller = await _armed(tmp_path, [_result(fingerprint="fp-1")])

    verdict = await controller.tick(loop, now=160.0)

    assert verdict.decision is MonitorDecision.RECORD_ONLY
    entries = _observed()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.src == "gateway"
    assert entry.data == {
        "producer": OBJECT_PRODUCER_PROBE,
        "kind": KIND,
        "target": TARGET,
        "fingerprint": "fp-1",
        "facts": _canonical(),
        "observed_at": 160.0,
    }


@pytest.mark.asyncio
async def test_an_unchanged_fingerprint_appends_nothing(tmp_path):
    """Polls are not history: the same fingerprint twice is one entry, not two."""
    _unit()
    _service, loop, controller = await _armed(tmp_path, [_result(fingerprint="fp-1")])

    await controller.tick(loop, now=160.0)
    second = await controller.tick(loop, now=220.0)
    third = await controller.tick(loop, now=280.0)

    assert second.decision is MonitorDecision.NO_CHANGE
    assert third.decision is MonitorDecision.NO_CHANGE
    assert len(_observed()) == 1


@pytest.mark.asyncio
async def test_each_distinct_state_is_one_entry_in_order(tmp_path):
    """Three polls over two distinct states record the two states, oldest first."""
    _unit()
    later = _canonical(head_revision="def456")
    _service, loop, controller = await _armed(
        tmp_path,
        [
            _result(fingerprint="fp-1"),
            _result(fingerprint="fp-1"),
            _result(fingerprint="fp-2", canonical=later),
        ],
    )

    for now in (160.0, 220.0, 280.0):
        await controller.tick(loop, now=now)

    entries = _observed()
    assert [entry.data["fingerprint"] for entry in entries] == ["fp-1", "fp-2"]
    assert entries[1].data["facts"] == later
    assert entries[1].data["observed_at"] == 280.0
    assert entries[0].seq < entries[1].seq


@pytest.mark.asyncio
async def test_the_record_does_not_depend_on_the_wake_decision(tmp_path):
    """An actionable change is recorded once, and the wake is delivered as before."""
    _unit()
    failing = _canonical()
    failing["checks"] = {"failed": ["ci"], "passed": [], "pending": [], "unknown": []}
    dispatch = AsyncMock(return_value=MonitorDispatchResult.DISPATCHED)
    _service, loop, controller = await _armed(
        tmp_path,
        [
            GitHubPullRequestProbeResult(
                response=None,
                canonical=failing,
                observation=MonitorObservation(
                    "fp-red", MonitorObservationStatus.ACTIONABLE, reason_code="checks_failed"
                ),
            )
        ],
        dispatch=dispatch,
    )

    verdict = await controller.tick(loop, now=160.0)

    assert verdict.decision is MonitorDecision.WAKE_ACTIONABLE
    dispatch.assert_awaited_once()
    entries = _observed()
    assert [entry.data["fingerprint"] for entry in entries] == ["fp-red"]
    assert entries[0].data["facts"]["checks"]["failed"] == ["ci"]


# --------------------------------------------------------------------------- #
# what is NOT recorded
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failed_read_records_nothing_and_the_next_real_read_records_once(tmp_path):
    """A provider error is no evidence about the subject, so it leaves no entry."""
    _unit()
    _service, loop, controller = await _armed(
        tmp_path,
        [_result(MonitorObservationStatus.PROVIDER_ERROR), _result(fingerprint="fp-1")],
    )

    first = await controller.tick(loop, now=160.0)
    assert first.decision is MonitorDecision.RETRY_PROVIDER
    assert _observed() == []

    await controller.tick(loop, now=220.0)
    assert [entry.data["fingerprint"] for entry in _observed()] == ["fp-1"]


@pytest.mark.asyncio
async def test_a_slot_with_no_live_session_records_nothing(tmp_path):
    """The resolver's empty answer is a policy no-op: no session is opened to file it."""
    _unit()
    _service, loop, controller = await _armed(
        tmp_path, [_result(fingerprint="fp-1")], owner=lambda loop: ""
    )

    verdict = await controller.tick(loop, now=160.0)

    assert verdict.decision is MonitorDecision.RECORD_ONLY
    assert _observed() == []


@pytest.mark.asyncio
async def test_a_controller_without_a_resolver_records_nothing(tmp_path):
    """A host that hands over no resolver -- the shadow and test hosts -- gets no writes."""
    _unit()
    service = AutoNudgeService(base_dir=tmp_path / "nudges")
    loop = await service.add_monitor(
        slot_key=SLOT,
        kind=KIND,
        target=TARGET,
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=6_000),
        wake_instructions="",
        now=100.0,
    )
    controller = MonitorController(
        service,
        AsyncMock(return_value=MonitorDispatchResult.DISPATCHED),
        providers={KIND: _Provider([_result(fingerprint="fp-1")])},
    )

    await controller.tick(loop, now=160.0)

    assert _observed() == []


@pytest.mark.asyncio
async def test_a_resolver_failure_is_logged_and_never_reaches_the_tick(tmp_path, caplog):
    """A record that cannot be made costs nothing the wake path owes."""
    _unit()

    def _broken(loop):
        raise RuntimeError("registry unavailable")

    _service, loop, controller = await _armed(
        tmp_path, [_result(fingerprint="fp-1")], owner=_broken
    )

    with caplog.at_level(logging.ERROR, logger="kiro_crew.monitoring.controller"):
        verdict = await controller.tick(loop, now=160.0)

    assert verdict.decision is MonitorDecision.RECORD_ONLY
    assert _observed() == []
    assert any("could not record its observation" in rec.getMessage() for rec in caplog.records)


class _DecliningService:
    """A service that publishes an observation only when told to.

    Stands in for the two ways the real service declines one: a probe taken under
    a superseded configuration generation, and a wake already in flight. Neither
    touches ``last_fingerprint``, so the controller must read nothing as changed.
    """

    def __init__(self, loop, *, publish: bool) -> None:
        self.loop = loop
        self.publish = publish

    async def stop_monitor_if_budget_exhausted(self, monitor_id, *, now):
        return False

    async def apply_monitor_probe(self, monitor_id, result, *, now, config_generation):
        if self.publish:
            self.loop.monitor.last_observation = dict(result.canonical)
            self.loop.monitor.last_fingerprint = result.observation.fingerprint
            self.loop.monitor.last_observed_at = now
        return MonitorVerdict(decision=MonitorDecision.NO_CHANGE)

    async def record_monitor_dispatch_failure(self, monitor_id, fingerprint, *, now=None):
        raise AssertionError("no wake is dispatched in this test")

    async def record_monitor_dispatch_busy(self, monitor_id, fingerprint, *, now):
        raise AssertionError("no wake is dispatched in this test")

    async def record_monitor_dispatched(self, monitor_id, fingerprint, *, now):
        raise AssertionError("no wake is dispatched in this test")

    async def record_monitor_completion_evidence_unavailable(self, monitor_id, fingerprint, *, now):
        raise AssertionError("no wake is dispatched in this test")

    async def monitor_dispatch_is_authorized(self, monitor_id, fingerprint):
        return False


@pytest.mark.asyncio
async def test_an_observation_the_service_declined_is_not_recorded(tmp_path):
    """Changed is judged against the PUBLISHED state, so a declined probe writes nothing.

    The next tick observes the subject again and records it then -- once -- which is
    the property a raw comparison against the observation would lose.
    """
    _unit()
    real = AutoNudgeService(base_dir=tmp_path / "nudges")
    loop = await real.add_monitor(
        slot_key=SLOT,
        kind=KIND,
        target=TARGET,
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=6_000),
        wake_instructions="",
        now=100.0,
    )
    service = _DecliningService(loop, publish=False)
    controller = MonitorController(
        service,
        AsyncMock(return_value=MonitorDispatchResult.DISPATCHED),
        providers={KIND: _Provider([_result(fingerprint="fp-1")])},
        owner_session_id=lambda loop: SESSION,
    )

    await controller.tick(loop, now=160.0)
    assert _observed() == []

    service.publish = True
    await controller.tick(loop, now=220.0)
    assert [entry.data["fingerprint"] for entry in _observed()] == ["fp-1"]


# --------------------------------------------------------------------------- #
# the emitter and the registry
# --------------------------------------------------------------------------- #


def test_the_producer_vocabulary_is_closed_and_starts_with_the_probe():
    assert OBJECT_PRODUCERS == ("probe",)
    spec = SESSION_ENTRY_TYPES[ENTRY_TYPE]
    producer = next(field for field in spec.fields if field.name == "producer")
    assert producer.enum_closed
    assert producer.enum == OBJECT_PRODUCERS
    assert producer.required


def test_a_free_text_producer_is_refused_at_the_emitter_and_nothing_is_appended():
    """Refused, not coerced: a coerced producer would attribute the record to a mechanism
    that did not make it."""
    _unit()

    with pytest.raises(ValueError, match="producer must be one of"):
        crew_log_emit.on_object_observed(
            SESSION,
            producer="the agent said so",
            kind=KIND,
            target=TARGET,
            fingerprint="fp-1",
            facts=_canonical(),
            observed_at=160.0,
        )

    assert _observed() == []


def test_the_registry_refuses_a_producer_outside_the_vocabulary_on_append():
    """The append path guards what a caller writing around the emitter would skip."""
    data = {
        "producer": "agent",
        "kind": KIND,
        "target": TARGET,
        "fingerprint": "fp-1",
        "facts": _canonical(),
        "observed_at": 160.0,
    }
    with pytest.raises(CrewLogError) as excinfo:
        validate_data(KIND_SESSION, ENTRY_TYPE, data)
    assert excinfo.value.code == lg.CODE_BAD_DATA_FIELD
    assert excinfo.value.field == "data.producer"


def test_the_emitter_records_the_snapshot_verbatim_with_its_producer():
    _unit()
    facts = _canonical(head_revision="0ff1ce")

    crew_log_emit.on_object_observed(
        SESSION,
        producer=OBJECT_PRODUCER_PROBE,
        kind=KIND,
        target=TARGET,
        fingerprint="fp-verbatim",
        facts=facts,
        observed_at=161.5,
    )

    entries = _observed()
    assert len(entries) == 1
    assert entries[0].data["facts"] == facts
    assert entries[0].data["producer"] == "probe"
    assert entries[0].data["observed_at"] == 161.5
    assert "facts_omitted" not in entries[0].data


def test_an_oversize_snapshot_is_recorded_short_by_a_named_member():
    """A review host reporting hundreds of long checks cannot lose the whole record.

    The largest member goes first and is named, so a reader sees a record that is
    short by ``checks`` rather than no record at all -- and cannot mistake the
    absence for "unchanged".
    """
    _unit()
    facts = _canonical(passed=[f"lane-{index:04d}-" + "x" * 190 for index in range(400)])
    assert len(json.dumps(facts).encode("utf-8")) > MAX_ENTRY_BYTES

    crew_log_emit.on_object_observed(
        SESSION,
        producer=OBJECT_PRODUCER_PROBE,
        kind=KIND,
        target=TARGET,
        fingerprint="fp-wide",
        facts=facts,
        observed_at=170.0,
    )

    entries = _observed()
    assert len(entries) == 1
    data = entries[0].data
    assert data["facts_omitted"] == ["checks"]
    assert "checks" not in data["facts"]
    kept = dict(facts)
    del kept["checks"]
    assert data["facts"] == kept
    assert data["fingerprint"] == "fp-wide"


def test_a_session_with_no_crew_log_is_a_policy_no_op():
    """No unit is created to file an observation: the emitter never opens one."""
    crew_log_emit.on_object_observed(
        "acp-never-opened",
        producer=OBJECT_PRODUCER_PROBE,
        kind=KIND,
        target=TARGET,
        fingerprint="fp-1",
        facts=_canonical(),
        observed_at=160.0,
    )
    assert crew_log_emit.flush(timeout=5.0)
    assert not CrewLog.exists(KIND_SESSION, "acp-never-opened")
    assert crew_log_emit.dropped_writes() == 0


def test_a_disabled_emitter_does_no_fit_work(monkeypatch):
    """Off is FREE, not merely silent.

    The fit loop serializes the whole snapshot and reaches the storage package,
    once per fingerprint change on the event loop. With the crew log off that work
    must not happen; the producer refusal still does, because it is a programming
    error and not a feature.
    """
    _unit()
    monkeypatch.delenv(crew_log_emit.CREW_LOG_ENV, raising=False)
    worked: list[str] = []
    monkeypatch.setattr(
        crew_log_emit,
        "_entry_line_fits",
        lambda *a, **k: worked.append("fit") or True,
    )
    crew_log_emit.on_object_observed(
        SESSION,
        producer=OBJECT_PRODUCER_PROBE,
        kind=KIND,
        target=TARGET,
        fingerprint="fp-off",
        facts=_canonical(),
        observed_at=170.0,
    )
    assert worked == [], f"a disabled emitter still did fit work: {worked}"
    assert crew_log_emit.flush(timeout=5.0)
    assert _observed(SESSION) == []
    with pytest.raises(ValueError):
        crew_log_emit.on_object_observed(
            SESSION,
            producer="human",
            kind=KIND,
            target=TARGET,
            fingerprint="fp-off",
            facts=_canonical(),
            observed_at=171.0,
        )


def test_the_probe_result_type_is_the_engine_s_own():
    """The recorder reads the generic result shape, so a second kind needs no new hook."""
    assert issubclass(GitHubPullRequestProbeResult, MonitorProbeResult)
