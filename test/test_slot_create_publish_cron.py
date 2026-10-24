"""A cron run or channel append that meets a pending ``POST /api/chat/slots``.

A cron bind or a channel opener of a key a create still holds waits for it,
or keeps its rows until the next bind, and the run shows once after publish.
The shared setup is in ``slot_create_publish_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest
import slot_create_publish_helpers
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from slot_create_publish_helpers import (
    WAIT,
    _config,
    _drain,
    _HeldRecord,
    _pending,
    _slots_app,
    _until,
    _use_config,
)

from kiro_crew.dashboard import slot_create_transaction as txn
from kiro_crew.execution_context import read_session_execution
from kiro_crew.history import HistoryLockTimeout

#: The shared fixture, bound here so pytest finds it for this module's tests.
state = slot_create_publish_helpers.state


# -- the other openers behave as on main ----------------------------------------


def _cron_job() -> Any:
    return SimpleNamespace(
        id="job1",
        name="nightly",
        member_id="",
        agent_id="default",
        memory_store="",
        message="",
        chat_folder_id="",
        persistent_session=True,
        hide_in_chat=False,
    )


@pytest.mark.asyncio
async def test_a_cron_bind_during_a_refused_create_keeps_its_slot(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cron bind of the key a create holds survives the create's refusal.

    The bind waits for the create; once the selection write refuses it, the
    bind opens the free key and links it. On base, with the app-owner arm's
    guarded retraction extended to this refusal (the subtraction First
    Principles proposes), the bind lands on the registered newborn first, and
    the retraction then pops the slot the bind linked: no messages, not
    running, still the same object, so the guard lets it go.
    """
    from kiro_crew.dashboard import cron_inject

    _use_config(monkeypatch, _config(worker={}))
    held = _HeldRecord(monkeypatch, refuse=True)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "cron-job1", "agent": "worker"})
        )
        bind: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            bind = asyncio.create_task(cron_inject.ensure_cron_slot(state, _cron_job()))
            await asyncio.sleep(0)
            assert not bind.done()
            held.release.set()
            resp = await asyncio.wait_for(post, WAIT)
            assert resp.status == 503, await resp.text()
            await asyncio.wait_for(bind, WAIT)
        finally:
            held.release.set()
            await _drain(post, *([bind] if bind is not None else []))
    slot = state._slots["cron-job1"]
    assert slot.linked_session_key == "cron:job1"
    assert "cron-job1" not in state._slots_under_construction


@pytest.mark.asyncio
async def test_a_cron_bind_of_the_same_key_waits_and_opens_the_creates_slot(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run-start cron bind on the SAME key as a pending create ends as on main.

    On main the create registered first, so the bind got the create's slot,
    with its folder, inherited tags and member pin, and linked it to the job's
    transcript. Here the bind waits for the create to publish and then does
    exactly that.

    Mutation pin: drop the wait in ``ensure_cron_slot`` and the bind raises
    "still being built".

    The bind's history read is held until the create has answered, so its
    rebind cannot race the create's own off-loop birth save and filing save
    (both pinned to the transcript the create authorized, and refused when a
    rebind lands mid-write): the order on main, where the create finished first.
    """
    from kiro_crew.dashboard import cron_inject

    _use_config(monkeypatch, _config(worker={}))
    state._tags.append({"id": "t1", "name": "T"})
    state._tags_authoritative = True
    state._folders.append({"id": "f1", "name": "F", "tags": ["t1"]})
    held = _HeldRecord(monkeypatch, refuse=False)
    create_answered = asyncio.Event()
    real_prefetch = cron_inject.prefetch_cron_history

    async def prefetch_after_the_create(*args: Any, **kwargs: Any) -> Any:
        # Reached only once the bind's wait saw the create publish.
        assert "cron-job1" in state._slots, "the bind did not wait for the create"
        await asyncio.wait_for(create_answered.wait(), WAIT)
        return await real_prefetch(*args, **kwargs)

    monkeypatch.setattr(cron_inject, "prefetch_cron_history", prefetch_after_the_create)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots",
                json={"name": "cron-job1", "agent": "worker", "folder_id": "f1"},
            )
        )
        bind: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            bind = asyncio.create_task(cron_inject.ensure_cron_slot(state, _cron_job()))
            await asyncio.sleep(0)
            assert not bind.done()
            held.release.set()
            resp = await asyncio.wait_for(post, WAIT)
            assert resp.status == 200
            assert (await resp.json())["folder_id"] == "f1"
            create_answered.set()
            await asyncio.wait_for(bind, WAIT)
        finally:
            held.release.set()
            create_answered.set()
            await _drain(post, *([bind] if bind is not None else []))
    slot = state._slots["cron-job1"]
    assert slot.linked_session_key == "cron:job1"
    assert slot.folder_id == "f1" and slot.tags == ["t1"]
    assert read_session_execution("dashboard:cron-job1") is not None


@pytest.mark.asyncio
async def test_a_cron_result_past_the_wait_is_kept_in_its_transcript(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result whose tab cannot be bound yet is written to the job's transcript.

    The next bind hydrates the tab from it, so no cron result is dropped.

    Mutation pin: let ``_bind_cron_slot``'s ValueError propagate and the
    injection raises with nothing written.
    """
    from kiro_crew.dashboard import cron_inject

    create = _pending(state, "cron-job1")
    try:
        cron_inject.inject_cron_result_to_dashboard(
            state, _cron_job(), "the nightly answer", history=[]
        )

        def written() -> bool:
            rows = state.conversation_log.read_messages("cron:job1")
            return any("the nightly answer" in str(row.get("content")) for row in rows)

        await _until(written)
    finally:
        await create.settle()
    slot = cron_inject._bind_cron_slot(
        state, _cron_job(), state.conversation_log.read_messages("cron:job1")
    )
    assert slot is not None
    assert any("the nightly answer" in str(m.get("content")) for m in slot.messages)


def _flaky_transcript(state: Any, monkeypatch: pytest.MonkeyPatch, *, timeouts: int) -> list[str]:
    """The ``cron:job1`` appends raise ``HistoryLockTimeout`` *timeouts* times, then write."""
    real = state.conversation_log.append_if_absent
    calls: list[str] = []

    def append_if_absent(key: str, *args: Any, **kwargs: Any) -> Any:
        if key == "cron:job1":
            calls.append(key)
            if len(calls) <= timeouts:
                raise HistoryLockTimeout(f"lock on {key} timed out")
        return real(key, *args, **kwargs)

    monkeypatch.setattr(state.conversation_log, "append_if_absent", append_if_absent)
    return calls


async def _inject_past_the_bound(
    state: Any, monkeypatch: pytest.MonkeyPatch, job: Any, result: str = "the nightly answer"
) -> None:
    """Inject *job*'s run while a dashboard create of its key outlasts the bounded wait."""
    from kiro_crew.dashboard import cron_inject, cron_pending

    monkeypatch.setattr(txn, "SLOT_CREATE_NAME_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(cron_pending, "KEEP_RUN_RETRY_SECS", 0)
    create = _pending(state, "cron-job1")
    try:
        await asyncio.wait_for(txn.wait_for_pending_create(state, "cron-job1"), WAIT)
        cron_inject.inject_cron_result_to_dashboard(state, job, result, history=[])
        await _settle_pending_store()
    finally:
        await create.settle()


async def _settle_pending_store() -> None:
    """Wait for the cron pending store's writers and releases to finish."""
    from kiro_crew.dashboard import cron_pending

    await asyncio.wait_for(asyncio.gather(*cron_pending.writers()), WAIT)


def _pending_entries(state: Any) -> list[Any]:
    return sorted(state.conversation_log.cron_pending_dir("job1").glob("*.json"))


def _simulate_restart(state: Any) -> None:
    """Drop what the process remembers of held runs; only the disk keeps them."""
    from kiro_crew.dashboard import cron_pending

    cron_pending._INDEX.pop(state, None)


def _run_rows_in(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    contents = [str(row.get("content")) for row in rows]
    prompts = [c for c in contents if c.startswith("# Cron Run:") and "summarise the night" in c]
    results = [c for c in contents if c.startswith("# Cron Job Result:")]
    return prompts, results


@pytest.mark.asyncio
async def test_a_kept_run_whose_append_times_out_once_is_retried_into_its_transcript(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock timeout on the no-tab fallback's append is retried, not dropped.

    The create of ``cron-job1`` is still pending past the bounded wait, so the
    run has no tab, and its first transcript append raises
    ``HistoryLockTimeout``. The retry writes the prompt and the result, and
    the next bind hydrates both into the tab, once each.

    Mutation pin: give ``cron_pending._hold_and_write`` a single try and the
    transcript never gets the run.
    """
    from kiro_crew.dashboard import cron_inject

    job = _cron_job()
    job.message = "summarise the night"
    calls = _flaky_transcript(state, monkeypatch, timeouts=1)
    await _inject_past_the_bound(state, monkeypatch, job)
    # The first try has run, whichever head fires it.
    await _until(lambda: bool(calls))
    assert len(calls) > 1
    prompts, results = _run_rows_in(state.conversation_log.read_messages("cron:job1"))
    assert len(prompts) == 1 and len(results) == 1
    await asyncio.wait_for(cron_inject.ensure_cron_slot(state, job), WAIT)
    prompts, results = _run_rows_in(state._slots["cron-job1"].messages)
    assert len(prompts) == 1 and len(results) == 1
    assert "the nightly answer" in results[0]


@pytest.mark.asyncio
async def test_a_kept_run_whose_append_keeps_failing_is_held_for_the_next_bind(
    state: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Past the retry bound the run is logged at ERROR by its id and still shown.

    Every append times out, so the transcript never gets the run. The rows
    stay held, and the next bind puts the prompt and the result in the tab,
    whose own saves then write them.

    Mutation pin: stop holding the rows in ``cron_pending.admit`` and the tab
    opens without the run.
    """
    from kiro_crew.dashboard import cron_inject, cron_pending

    job = _cron_job()
    job.message = "summarise the night"
    job.last_result_ts = "1760450000.25"
    calls = _flaky_transcript(state, monkeypatch, timeouts=10**6)
    with caplog.at_level(logging.ERROR, logger=cron_inject.logger.name):
        await _inject_past_the_bound(state, monkeypatch, job)
    assert len(calls) == cron_pending.KEEP_RUN_ATTEMPTS
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("job1@1760450000.25" in message for message in errors)
    assert _run_rows_in(state.conversation_log.read_messages("cron:job1")) == ([], [])
    await asyncio.wait_for(cron_inject.ensure_cron_slot(state, job), WAIT)
    prompts, results = _run_rows_in(state._slots["cron-job1"].messages)
    assert len(prompts) == 1 and len(results) == 1
    assert _contents_order(state._slots["cron-job1"].messages, prompts[0], results[0])
    # Merged once: a second bind shows nothing new.
    await asyncio.wait_for(cron_inject.ensure_cron_slot(state, job), WAIT)
    assert _run_rows_in(state._slots["cron-job1"].messages) == (prompts, results)


@pytest.mark.asyncio
async def test_a_held_run_survives_a_restart_and_is_merged_on_the_next_bind(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run held while its tab was being built is on disk, not only in memory.

    Every append times out, and the process then forgets the run (a restart).
    The next bind still shows the prompt and the result, and once the tab's
    save confirms, the entry is gone and the transcript holds the run.

    Mutation pin: skip the entry's disk write in ``cron_pending`` and the tab
    opens without the run.
    """
    from kiro_crew.dashboard import cron_inject

    job = _cron_job()
    job.message = "summarise the night"
    job.last_result_ts = "1760450000.25"
    _flaky_transcript(state, monkeypatch, timeouts=10**6)
    await _inject_past_the_bound(state, monkeypatch, job)
    assert len(_pending_entries(state)) == 1
    _simulate_restart(state)
    await asyncio.wait_for(cron_inject.ensure_cron_slot(state, job), WAIT)
    prompts, results = _run_rows_in(state._slots["cron-job1"].messages)
    assert len(prompts) == 1 and len(results) == 1
    await _settle_pending_store()
    assert _pending_entries(state) == []
    prompts, results = _run_rows_in(state.conversation_log.read_messages("cron:job1"))
    assert len(prompts) == 1 and len(results) == 1


@pytest.mark.asyncio
async def test_a_held_run_is_deleted_only_after_the_tabs_save_confirms(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merged run whose tab save does not confirm stays on disk for the next bind.

    Mutation pin: delete the entry before awaiting the save in
    ``cron_pending._release`` and the refused save leaves nothing to merge.
    """
    from kiro_crew.dashboard import chat_persistence, cron_inject

    job = _cron_job()
    job.message = "summarise the night"
    job.last_result_ts = "1760450000.25"
    _flaky_transcript(state, monkeypatch, timeouts=10**6)
    await _inject_past_the_bound(state, monkeypatch, job)
    real_save = chat_persistence.save_slot_off_loop

    async def refused_save(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(chat_persistence, "save_slot_off_loop", refused_save)
    await asyncio.wait_for(cron_inject.ensure_cron_slot(state, job), WAIT)
    await _settle_pending_store()
    assert len(_pending_entries(state)) == 1
    monkeypatch.setattr(chat_persistence, "save_slot_off_loop", real_save)
    _simulate_restart(state)
    state._slots.pop("cron-job1", None)
    await asyncio.wait_for(cron_inject.ensure_cron_slot(state, job), WAIT)
    prompts, results = _run_rows_in(state._slots["cron-job1"].messages)
    assert len(prompts) == 1 and len(results) == 1
    await _settle_pending_store()
    assert _pending_entries(state) == []


@pytest.mark.asyncio
async def test_a_job_holding_its_cap_of_runs_refuses_the_next_with_an_error(
    state: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Past ``PENDING_RUNS_PER_JOB`` unwritten runs a new run is refused, not evicted.

    An ERROR names the job and the refused run, the held runs stay, and the
    job's stored result is left as it is.

    Mutation pin: drop the cap check in ``cron_pending._make_room`` and a third
    entry is written.
    """
    from kiro_crew.dashboard import cron_pending

    monkeypatch.setattr(cron_pending, "PENDING_RUNS_PER_JOB", 2)
    job = _cron_job()
    job.last_result = "the stored answer"
    _flaky_transcript(state, monkeypatch, timeouts=10**6)
    with caplog.at_level(logging.ERROR, logger=cron_pending.logger.name):
        for n in range(3):
            job.last_result_ts = f"176045000{n}.5"
            await _inject_past_the_bound(state, monkeypatch, job, f"answer {n}")
    assert len(_pending_entries(state)) == 2
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("job1@1760450002.5" in m and "not held" in m for m in errors)
    assert cron_pending._INDEX[state]["job1"].refused == 1
    assert any("1 run(s) refused" in m for m in errors)
    assert job.last_result == "the stored answer"


@pytest.mark.asyncio
async def test_a_run_over_the_byte_cap_is_refused_with_an_error(
    state: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A run whose entry is over ``PENDING_RUN_MAX_BYTES`` is refused, not held or cut.

    Mutation pin: drop the byte check in ``cron_pending.admit`` and the entry
    is written.
    """
    from kiro_crew.dashboard import cron_pending

    monkeypatch.setattr(cron_pending, "PENDING_RUN_MAX_BYTES", 512)
    job = _cron_job()
    job.last_result = "the stored answer"
    job.last_result_ts = "1760450000.25"
    _flaky_transcript(state, monkeypatch, timeouts=10**6)
    with caplog.at_level(logging.ERROR, logger=cron_pending.logger.name):
        await _inject_past_the_bound(state, monkeypatch, job, "x" * 2000)
    assert _pending_entries(state) == []
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("job1@1760450000.25" in m and "byte cap" in m for m in errors)
    assert cron_pending._INDEX[state]["job1"].refused == 1
    assert job.last_result == "the stored answer"


def _contents_order(messages: list[dict[str, Any]], first: str, second: str) -> bool:
    contents = [str(m.get("content")) for m in messages]
    return contents.index(first) < contents.index(second)


@pytest.mark.asyncio
async def test_a_cron_run_injected_during_a_same_key_create_shows_once_after_publish(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract for the synchronous cron injector during a pending create.

    A run injected while a create of the same ``cron-{id}`` key is still
    building it cannot bind the tab, so it is kept in the job's transcript. Once
    the create publishes, the next bind shows the run's prompt and result rows
    in the tab, each exactly once, however often the tab is bound again. This
    is why the synchronous ``_bind_cron_slot`` may refuse mid-create.

    Mutation pin: drop ``_keep_run_in_transcript`` from the injector's
    ValueError branch and the tab never shows the run.
    """
    from kiro_crew.dashboard import cron_inject

    _use_config(monkeypatch, _config(worker={}))
    job = _cron_job()
    job.message = "summarise the night"
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "cron-job1", "agent": "worker"})
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            cron_inject.inject_cron_result_to_dashboard(
                state, job, "the nightly answer", history=[]
            )

            def written() -> bool:
                rows = state.conversation_log.read_messages("cron:job1")
                return any("the nightly answer" in str(row.get("content")) for row in rows)

            await _until(written)
            held.release.set()
            assert (await asyncio.wait_for(post, WAIT)).status == 200
        finally:
            held.release.set()
            await _drain(post)
    for _ in range(2):
        await asyncio.wait_for(cron_inject.ensure_cron_slot(state, job), WAIT)
    slot = state._slots["cron-job1"]
    contents = [str(m.get("content")) for m in slot.messages]
    prompts = [c for c in contents if c.startswith("# Cron Run:") and "summarise the night" in c]
    results = [c for c in contents if c.startswith("# Cron Job Result:")]
    assert len(prompts) == 1 and len(results) == 1
    assert "the nightly answer" in results[0]
    assert contents.index(prompts[0]) < contents.index(results[0])


@pytest.mark.asyncio
async def test_a_channel_opener_of_a_pending_key_is_surfaced_on_its_next_pass(
    state: Any,
) -> None:
    """``surface_channel_session`` is synchronous: it skips a key under construction
    (its existing ValueError handling) and surfaces it on the reconciler's next pass.
    """
    from kiro_crew.dashboard import channel_slots

    def surface() -> Any:
        return channel_slots.surface_channel_session(
            state,
            {"key": "slack_1700000000.000100"},
            {},
            [],
            session_key="slack:1700000000.000100",
        )

    create = _pending(state, "slack_1700000000.000100")
    assert surface() is None
    await create.settle()
    slot = surface()
    assert slot is not None and state._slots.get(slot.key) is slot


# -- the gateway's cron delivery waits before it injects --------------------------


class _InjectReached(Exception):
    """Raised by the patched injector: the delivery got as far as the tab."""


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["duplicate", "silent", "creator"])
async def test_each_cron_delivery_path_waits_for_the_tabs_create_before_injecting(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every gateway path that injects a cron result into ``cron-<id>`` first waits
    for a pending create of that key, then merges the job's held runs.

    The duplicate result, the silent job and the first-delivery creator each
    reach ``inject_cron_result_to_dashboard`` only after
    ``wait_for_pending_create(state, "cron-<id>")`` and ``cron_pending.load``.

    Mutation pins, one per site in ``gateway_runtime/cron_delivery.py``: drop
    the wait and load before that site's inject and its case fails.
    """
    import time
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.cron import CronJob, CronSchedule
    from kiro_crew.slack import gateway

    events: list[tuple[str, Any]] = []

    async def wait(state_: Any, key: str) -> None:
        events.append(("wait", key))

    async def load(state_: Any, job_id: str) -> None:
        events.append(("load", job_id))

    def inject(*args: Any, **kwargs: Any) -> None:
        events.append(("inject", None))
        raise _InjectReached

    monkeypatch.setattr(gateway, "wait_for_pending_create", wait)
    monkeypatch.setattr(gateway, "cron_pending", SimpleNamespace(load=load))
    monkeypatch.setattr(gateway, "inject_cron_result_to_dashboard", inject)
    monkeypatch.setattr(gateway, "prefetch_cron_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(gateway, "sel", MagicMock())

    gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    gw.dashboard_state = MagicMock()
    gw.dashboard_state.has_slot.return_value = True
    gw.dashboard_state.conversation_log.read_messages.return_value = []
    job = CronJob(
        id="job1",
        name="nightly",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
        persistent_session=True,
        hide_in_chat=False,
        silent=path == "silent",
    )
    rh = "fresh"
    if path == "duplicate":
        job.last_posted_hash = rh
        job.last_posted_at = time.time()

    with pytest.raises(_InjectReached):
        await gw._deliver_cron_agent_result(job, "cron:job1", MagicMock(), "answer", rh, None, None)
    assert events == [("wait", "cron-job1"), ("load", "job1"), ("inject", None)]
