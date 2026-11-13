"""Tests for persistent_session flag on CronJob.

Bug: stateful cron sessions accumulate context indefinitely for
polling-style jobs, causing LLM turns to drift from seconds to tens of minutes
over days. Adding ``persistent_session=False`` gives each run a fresh session
key and skips the ``last_result`` prefix.

This file covers:
- Dataclass field round-trip (save → load)
- Default is True (backward-compat for all existing jobs)
- Session key + prompt builder helper:
    - persistent_session=True  → key = f"cron:{job.id}"           (stable)
                              → prompt contains last_result prefix
    - persistent_session=False → key = f"cron:{job.id}:<run_id>"  (fresh)
                              → prompt does NOT contain last_result prefix
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.cron import CronJob, CronService


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


class TestCronJobPersistentSessionField:
    """Dataclass field + serialization round-trip."""

    def test_default_is_true(self):
        """New CronJob defaults to persistent_session=True (backward-compat)."""
        job = CronJob(id="j1", name="x", message="y")
        assert job.persistent_session is True

    def test_field_roundtrips_through_save_load(self, tmp_path):
        """Save a job with persistent_session=False → reload → still False."""
        svc = CronService()
        job = svc.add_job(name="poll", message="check", every_secs=120)
        job.persistent_session = False
        svc._save()

        # Force reload from disk
        svc2 = CronService()
        loaded = [j for j in svc2.list_jobs() if j.id == job.id][0]
        assert loaded.persistent_session is False

    def test_legacy_job_without_field_defaults_to_true(self, tmp_path):
        """A crons.json written before this feature must load as persistent=True."""
        path = tmp_path / "crons.json"
        legacy = {
            "version": 2,
            "jobs": [
                {
                    "id": "legacy1",
                    "name": "old",
                    "message": "hello",
                    "schedule": {"kind": "every", "every_secs": 300},
                    "created_ts": 1_700_000_000.0,
                    # Note: no "persistent_session" key
                }
            ],
        }
        path.write_text(json.dumps(legacy), encoding="utf-8")

        svc = CronService()
        loaded = [j for j in svc.list_jobs() if j.id == "legacy1"][0]
        assert loaded.persistent_session is True


class TestBuildCronSessionContext:
    """Helper that computes session_key + prompt for a given run.

    This helper is what ``_cron_capture`` in slack/gateway_runtime/cron_fire.py will call.
    Extracting it lets us unit-test the decision without spinning up the gateway.
    """

    def test_persistent_uses_stable_key(self):
        """persistent_session=True → key == f'cron:{job.id}' (unchanged)."""
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(id="abc123", name="x", message="hi", persistent_session=True)
        key, _prompt = build_cron_session_context(job)
        assert key == "cron:abc123"

    def test_persistent_job_carries_last_result_beside_the_prompt(self):
        """persistent_session=True → the previous result is carried, not in the request.

        The prompt is the owner's message alone; the previous run's result travels
        as ``cron_prior_result_context``, framed as data, for the context region.
        """
        from kiro_crew.cron import build_cron_session_context
        from kiro_crew.cron_service.identity import cron_prior_result_context

        job = CronJob(
            id="abc123",
            name="x",
            message="do work",
            persistent_session=True,
            last_result="PREVIOUS OUTPUT HERE",
        )
        _key, prompt = build_cron_session_context(job)
        assert prompt == "do work"
        assert "PREVIOUS OUTPUT HERE" in cron_prior_result_context(job)

    def test_stateless_uses_fresh_key_per_call(self):
        """persistent_session=False → two calls return two different keys."""
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(id="abc123", name="x", message="hi", persistent_session=False)
        key1, _ = build_cron_session_context(job)
        key2, _ = build_cron_session_context(job)
        assert key1 != key2

    def test_stateless_key_prefix_is_job_id(self):
        """Ephemeral key must still start with 'cron:{job.id}:' for reaper matching."""
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(id="abc123", name="x", message="hi", persistent_session=False)
        key, _ = build_cron_session_context(job)
        assert key.startswith("cron:abc123:")
        # Suffix is non-empty
        assert len(key) > len("cron:abc123:")

    def test_stateless_prompt_skips_last_result(self):
        """persistent_session=False → prompt does NOT contain last_result prefix.

        This is the second half of the fix: even if a stateless job
        accidentally had last_result from a previous version's persisted state,
        the new code must never inject it.
        """
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(
            id="abc123",
            name="x",
            message="do work",
            persistent_session=False,
            last_result="STALE OUTPUT MUST NOT APPEAR",
        )
        from kiro_crew.cron_service.identity import cron_prior_result_context

        _key, prompt = build_cron_session_context(job)
        assert "STALE OUTPUT MUST NOT APPEAR" not in prompt
        assert "do work" in prompt
        assert cron_prior_result_context(job) == ""

    def test_stateless_prompt_equals_bare_message(self):
        """When persistent_session=False and no acks, prompt is exactly job.message."""
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(
            id="abc123",
            name="x",
            message="do work",
            persistent_session=False,
            last_result=None,
        )
        _key, prompt = build_cron_session_context(job)
        assert prompt == "do work"


def _stateless_llm_job(**overrides):
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

    fields = dict(
        id="abcd1234",
        name="poll",
        message="check",
        persistent_session=False,
        execution_context=ExecutionContext(
            None, MemoryStoreRef(DEFAULT_MEMORY_STORE), "template", "kirocrew"
        ).to_record(),
    )
    fields.update(overrides)
    return CronJob(**fields)


def _run_files(job_id):
    from kiro_crew.history import ConversationLog

    return sorted(p.name for p in ConversationLog()._dir.glob(f"cron_{job_id}_*.jsonl"))


async def _fire(gw, job, events, *, extra=()):
    """Fire *job* through the callback ``_init_cron`` builds, recording order.

    *events* receives ``("active", key)`` when the run registers its key and
    ``("tag", key)`` when the ephemeral tag is written.
    """
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.history import ConversationLog

    real_update = ConversationLog.update_metadata

    def recording_update(log, key, updates, *args, **kwargs):
        if updates.get("cron_run") == "ephemeral":
            events.append(("tag", key))
        return real_update(log, key, updates, *args, **kwargs)

    captured = {}

    def capture_cron(on_job=None, **_kw):
        captured["cb"] = on_job
        svc = MagicMock()
        svc.start = AsyncMock()
        svc.remove_job_async = AsyncMock(return_value=True)
        svc.register_active_session_key.side_effect = lambda _id, key: events.append(
            ("active", key)
        )
        return svc

    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    with ExitStack() as stack:
        cron_cls = stack.enter_context(patch("kiro_crew.slack.gateway.CronService"))
        cron_cls.create = AsyncMock(side_effect=capture_cron)
        stack.enter_context(
            patch(
                "kiro_crew.slack.gateway.run_in_embed_pool",
                AsyncMock(return_value=("full prompt", None)),
            )
        )
        stack.enter_context(
            patch("kiro_crew.slack.gateway.stream_and_collect", AsyncMock(return_value="done"))
        )
        stack.enter_context(patch("kiro_crew.slack.gateway.sel"))
        stack.enter_context(
            patch("kiro_crew.cron_service.agent_presence.cron_agent_missing", return_value=False)
        )
        stack.enter_context(
            patch("kiro_crew.mate_worker.cron_worker_execution", lambda *args: None)
        )
        stack.enter_context(patch.object(ConversationLog, "update_metadata", recording_update))
        for ctx in extra:
            stack.enter_context(ctx)
        await gw._init_cron()
        return await captured["cb"](job)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("sequence", [[], ["deadbeef"], ["deadbeef", "cafebabe"]])
async def test_fire_capture_writes_no_transcript(monkeypatch, persistent, sequence):
    """The capture runs before the gate and admission, so it never tags a key."""
    from cron_gateway_integration_helpers import _make_gw_for_llm

    from kiro_crew.history import ConversationLog

    job = _stateless_llm_job(persistent_session=persistent, agent_sequence=sequence)
    gw = _make_gw_for_llm()
    monkeypatch.setattr("kiro_crew.mate_worker.cron_worker_execution", lambda *args: None)
    await gw._cron_capture(job)
    assert _run_files(job.id) == []
    assert not ConversationLog().has_log(f"cron:{job.id}")


@pytest.mark.asyncio
async def test_a_stateless_run_is_tagged_only_after_its_key_is_active():
    """The prune skips active keys, so the tag must land after the registration."""
    from cron_gateway_integration_helpers import _make_gw_for_llm

    from kiro_crew.history import ConversationLog

    job = _stateless_llm_job()
    events: list[tuple[str, str]] = []
    await _fire(_make_gw_for_llm(), job, events)
    tags = [key for kind, key in events if kind == "tag"]
    assert len(tags) == 1
    key = tags[0]
    assert key.startswith(f"cron:{job.id}:")
    assert events.index(("active", key)) < events.index(("tag", key))
    assert ConversationLog().get_metadata(key)["cron_run"] == "ephemeral"


@pytest.mark.asyncio
async def test_a_persistent_run_is_never_tagged():
    from cron_gateway_integration_helpers import _make_gw_for_llm

    from kiro_crew.history import ConversationLog

    job = _stateless_llm_job(persistent_session=True)
    events: list[tuple[str, str]] = []
    await _fire(_make_gw_for_llm(), job, events)
    assert ("active", f"cron:{job.id}") in events
    assert not [e for e in events if e[0] == "tag"]
    assert "cron_run" not in ConversationLog().get_metadata(f"cron:{job.id}")


@pytest.mark.asyncio
async def test_a_stateless_fire_refused_by_the_fire_time_gate_writes_no_transcript():
    from unittest.mock import AsyncMock, patch

    from cron_gateway_integration_helpers import _make_gw_for_llm

    job = _stateless_llm_job()
    gw = _make_gw_for_llm()
    gw._alert_cron_failure = AsyncMock()
    events: list[tuple[str, str]] = []
    gate = patch(
        "kiro_crew.slack.gateway._await_cron_fire_time_gate",
        AsyncMock(return_value=("cron capability is denied", False)),
    )
    assert await _fire(gw, job, events, extra=[gate]) is None
    assert job.fire_time_denied is True
    assert events == []
    assert _run_files(job.id) == []


@pytest.mark.asyncio
async def test_a_stateless_fire_refused_at_admission_writes_no_transcript():
    from unittest.mock import patch

    from cron_gateway_integration_helpers import _make_gw_for_llm

    async def refuse(_job, dispatch):
        # A refused fire never starts its dispatch; close the run coroutine the
        # holding wrapper carries as well as the wrapper itself.
        dispatch.cr_frame.f_locals["dispatch"].close()
        dispatch.close()
        return None

    job = _stateless_llm_job()
    events: list[tuple[str, str]] = []
    admission = patch("kiro_crew.shared_crew.admission_entry.admitted_cron_dispatch", refuse)
    assert await _fire(_make_gw_for_llm(), job, events, extra=[admission]) is None
    assert events == []
    assert _run_files(job.id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [{"command": "echo hi"}, {"script": "/tmp/x.py:run"}])
async def test_fire_capture_creates_no_transcript_for_a_code_job(monkeypatch, code):
    from cron_gateway_integration_helpers import _make_gw_for_llm

    from kiro_crew.history import ConversationLog

    job = _stateless_llm_job(**code)
    gw = _make_gw_for_llm()
    monkeypatch.setattr("kiro_crew.mate_worker.cron_worker_execution", lambda *args: None)
    cap = await gw._cron_capture(job)
    assert not ConversationLog().has_log(cap.session_key)
