"""The Slack handler's per-thread override state is bounded, and eviction is recoverable.

``slack.handler`` keeps a hydration guard (``_hydrated_sessions``) and two override
maps (``_thread_agents``, ``_thread_projects``), each gaining one entry per thread the
bot hears from. They are held to ``THREAD_SLOTS`` threads together: an evicted thread
loses all three entries, and its next message hydrates again from the conversation
metadata. An override a command set in this process is pinned and never evicted.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from kiro_crew.testing.wait import LoopHop, await_until

try:
    from kiro_crew.slack.thread_override_slots import THREAD_SLOTS
except ImportError:  # the unbounded shape: assert against the intended bound
    THREAD_SLOTS = 4096


def _slots():
    try:
        from kiro_crew.slack import thread_override_slots
    except ImportError:
        return None
    return thread_override_slots


def _pin(handler, key: str, kind: str) -> bool:
    """Pin *key* the way ``!ta`` and ``!project`` do, against the handler's containers."""
    return _slots().pin(
        key, kind, handler._hydrated_sessions, handler._thread_agents, handler._thread_projects
    )


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    """Pin the data home and workspace to ``tmp_path`` through the isolation
    floor's own ``MonkeyPatch``, so a test's ``monkeypatch.undo()`` cannot lift
    the pins."""
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        (tmp_path / sub).mkdir()
        _floor_monkeypatch.setenv(var, str(tmp_path / sub))


@pytest.fixture
def handler():
    from kiro_crew.slack import handler as h

    def _reset() -> None:
        h._hydrated_sessions.clear()
        h._thread_agents.clear()
        h._thread_projects.clear()
        slots = _slots()
        if slots is not None:
            slots._order.clear()
            slots._pinned.clear()

    _reset()
    yield h
    _reset()


class _Log:
    """A conversation log whose metadata names an agent and a project for every thread."""

    def __init__(self, meta: dict) -> None:
        self._meta = meta

    def get_metadata(self, session_key: str) -> dict:
        return dict(self._meta)

    def get_metadata_status(self, session_key: str) -> tuple[dict, bool]:
        return self.get_metadata(session_key), True


def _key(i: int) -> str:
    return f"slack:C0SYNTH01:{1_700_000_000 + i}.000100"


@pytest.mark.asyncio
async def test_new_threads_do_not_grow_the_override_state_without_bound(handler, tmp_path):
    log = _Log({"agent": "reviewer", "project": str(tmp_path)})
    extra = 500
    for i in range(THREAD_SLOTS + extra):
        await handler._hydrate_thread_overrides(_key(i), log)
    assert len(handler._hydrated_sessions) <= THREAD_SLOTS, (
        f"{len(handler._hydrated_sessions)} hydrated-session keys for "
        f"{THREAD_SLOTS + extra} threads; the guard set is unbounded"
    )
    assert len(handler._thread_agents) <= THREAD_SLOTS
    assert len(handler._thread_projects) <= THREAD_SLOTS
    # The newest threads are the ones kept.
    assert _key(THREAD_SLOTS + extra - 1) in handler._hydrated_sessions


@pytest.mark.asyncio
async def test_an_evicted_thread_restores_its_overrides_on_its_next_message(handler, tmp_path):
    first = _key(0)
    log = _Log({"agent": "reviewer", "project": str(tmp_path)})
    await handler._hydrate_thread_overrides(first, log)
    assert handler._thread_agents[first] == "reviewer"
    for i in range(1, THREAD_SLOTS + 1):
        await handler._hydrate_thread_overrides(_key(i), log)
    # Evicted as one unit: no stale guard entry can mask a missing override.
    assert first not in handler._hydrated_sessions, "the oldest thread was not evicted"
    assert first not in handler._thread_agents
    assert first not in handler._thread_projects
    await handler._hydrate_thread_overrides(first, log)
    assert handler._thread_agents[first] == "reviewer"
    assert handler._thread_projects[first] == str(tmp_path)


@pytest.mark.asyncio
async def test_a_recently_active_thread_is_kept_over_an_idle_one(handler):
    log = _Log({"agent": "reviewer"})
    active, idle = _key(0), _key(1)
    await handler._hydrate_thread_overrides(active, log)
    await handler._hydrate_thread_overrides(idle, log)
    for i in range(2, THREAD_SLOTS + 1):
        await handler._hydrate_thread_overrides(_key(i), log)
        await handler._hydrate_thread_overrides(active, log)  # a repeat message keeps it fresh
    assert active in handler._thread_agents
    assert idle not in handler._thread_agents


@pytest.mark.asyncio
async def test_one_thread_hydrated_many_times_adds_one_entry(handler):
    log = _Log({"agent": "reviewer"})
    for _ in range(THREAD_SLOTS + 10):
        await handler._hydrate_thread_overrides(_key(0), log)
    assert len(handler._hydrated_sessions) == 1
    assert len(handler._thread_agents) == 1


@pytest.mark.asyncio
async def test_an_override_set_by_a_command_in_this_process_is_never_evicted(handler, tmp_path):
    slots = _slots()
    assert slots is not None, "the bound does not exist"
    chosen = _key(0)
    await handler._hydrate_thread_overrides(chosen, _Log({}))
    # What !ta and !project do after resolving their argument.
    handler._thread_agents[chosen] = "reviewer"
    _pin(handler, chosen, "agent")
    handler._thread_projects[chosen] = str(tmp_path)
    _pin(handler, chosen, "project")
    log = _Log({"agent": "other"})
    for i in range(1, 2 * THREAD_SLOTS):
        await handler._hydrate_thread_overrides(_key(i), log)
    assert handler._thread_agents[chosen] == "reviewer"
    assert handler._thread_projects[chosen] == str(tmp_path)
    assert chosen in handler._hydrated_sessions
    # Clearing one override keeps the pin of the other; clearing both frees the thread.
    handler._thread_agents.pop(chosen)
    slots.unpin(chosen, "agent")
    for i in range(2 * THREAD_SLOTS, 3 * THREAD_SLOTS):
        await handler._hydrate_thread_overrides(_key(i), log)
    assert handler._thread_projects[chosen] == str(tmp_path)
    handler._thread_projects.pop(chosen)
    slots.unpin(chosen, "project")
    for i in range(3 * THREAD_SLOTS, 4 * THREAD_SLOTS + 1):
        await handler._hydrate_thread_overrides(_key(i), log)
    assert chosen not in handler._hydrated_sessions


@pytest.mark.asyncio
async def test_pinned_threads_count_inside_the_bound(handler, tmp_path):
    slots = _slots()
    assert slots is not None, "the bound does not exist"
    pinned = [_key(i) for i in range(100)]
    for key in pinned:
        await handler._hydrate_thread_overrides(key, _Log({}))
        # What !ta does after resolving its argument.
        handler._thread_agents[key] = "reviewer"
        _pin(handler, key, "agent")
    log = _Log({"agent": "other"})
    for i in range(100, 100 + THREAD_SLOTS):
        await handler._hydrate_thread_overrides(_key(i), log)
    tracked = len(handler._hydrated_sessions)
    assert (
        tracked <= THREAD_SLOTS
    ), f"{tracked} threads kept with 100 pinned: pins are outside the bound"
    assert all(handler._thread_agents[key] == "reviewer" for key in pinned)


@pytest.mark.asyncio
async def test_a_command_past_the_bound_is_refused_out_loud_and_sets_nothing(handler, monkeypatch):
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    # One slot stays unpinned, so hydration never evicts the thread it is hydrating.
    max_pinned = getattr(slots, "MAX_PINNED_THREADS", THREAD_SLOTS - 1)
    assert max_pinned == THREAD_SLOTS - 1
    for i in range(max_pinned):
        assert _pin(handler, _key(i), "agent"), f"pin {i} was refused below the bound"
    monkeypatch.setattr(handler, "_resolve_agent_name", lambda name, project: "reviewer")
    slack = AsyncMock()
    late = _key(max_pinned)
    await handler._bang_thread_agent(
        "!ta reviewer", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", late, "U1", None
    )
    assert late not in handler._thread_agents, "a thread past the bound got an override row"
    replies = [call.args[1] for call in slack.post_message.await_args_list]
    assert any(f"{max_pinned} threads already" in text for text in replies), replies


@pytest.mark.asyncio
async def test_hydration_with_every_pin_taken_keeps_no_untracked_row(handler, tmp_path):
    """Pin as many threads as ``pin`` admits, then hydrate 50 new threads.

    Each new thread's override rows must stay inside the count: hydration must never
    evict the thread it just added and then write that thread's rows anyway.
    """
    slots = _slots()
    assert slots is not None, "the bound does not exist"
    pinned = 0
    for i in range(THREAD_SLOTS):
        key = _key(i)
        await handler._hydrate_thread_overrides(key, _Log({}))
        if not _pin(handler, key, "agent"):
            break
        handler._thread_agents[key] = "pinned"
        pinned += 1
    log = _Log({"agent": "reviewer", "project": str(tmp_path)})
    for i in range(THREAD_SLOTS, THREAD_SLOTS + 50):
        await handler._hydrate_thread_overrides(_key(i), log)
    kept = set(handler._thread_agents) | set(handler._thread_projects)
    tracked = set(slots._order) | {k for k, _ in slots._pinned}
    untracked = kept - tracked
    assert not untracked and len(kept) <= THREAD_SLOTS, (
        f"{len(kept)} threads keep override rows with {THREAD_SLOTS} slots and {pinned} "
        f"pinned ({len(untracked)} untracked by the bound)"
    )
    newest = _key(THREAD_SLOTS + 49)
    assert (
        handler._thread_agents.get(newest) == "reviewer"
    ), "the thread just hydrated lost its agent"


def _value_limit() -> int:
    slots = _slots()
    return getattr(slots, "OVERRIDE_VALUE_MAX_CHARS", 4096) if slots is not None else 4096


def _path_of_length(base: str, n: int) -> str:
    """A path under *base* exactly *n* characters long, in components of at most 200."""
    path = base
    while len(path) < n:
        room = n - len(path)
        path += "/" + "p" * min(199, room - 1) if room > 1 else "p"
    assert len(path) == n
    return path


@pytest.mark.asyncio
async def test_pinning_a_thread_evicted_during_its_command_keeps_the_bound(
    handler, monkeypatch, tmp_path
):
    """``!ta`` awaits agent discovery while two other threads hydrate and evict its own.

    Every slot but two is pinned by an earlier command, so the evicted thread's pin
    must evict an unpinned thread from every shared container before ``!ta`` publishes
    its override, and the hydration ``!ta`` runs just before it pins puts the thread's
    hydration guard back.
    """
    import asyncio
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    for i in range(THREAD_SLOTS - 2):
        key = _key(i)
        handler._hydrated_sessions.add(key)
        handler._thread_agents[key] = "pinned"
        slots._pinned.add((key, "agent"))
    log = _Log({"agent": "reviewer", "project": str(tmp_path)})
    chosen, other = _key(THREAD_SLOTS - 2), _key(THREAD_SLOTS - 1)
    for key in (chosen, other):
        await handler._hydrate_thread_overrides(key, log)
    loop = asyncio.get_running_loop()
    hop1 = LoopHop(loop)

    def resolve_while_two_threads_hydrate(name, project):
        # Runs in the worker thread ``!ta`` awaits: both threads hydrate on the loop
        # before agent discovery answers.
        for i in (THREAD_SLOTS, THREAD_SLOTS + 1):
            hop1.run(handler._hydrate_thread_overrides(_key(i), log), "_hydrate_thread_overrides")
        return "chosen-agent"

    monkeypatch.setattr(handler, "_resolve_agent_name", resolve_while_two_threads_hydrate)
    await handler._bang_thread_agent(
        "!ta chosen-agent", AsyncMock(), AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", None
    )
    hop1.check()
    assert chosen not in slots._order and (chosen, "agent") in slots._pinned
    assert handler._thread_agents.get(chosen) == "chosen-agent"
    containers = {
        "_hydrated_sessions": set(handler._hydrated_sessions),
        "_thread_agents": set(handler._thread_agents),
        "_thread_projects": set(handler._thread_projects),
    }
    over = {name: len(keys) for name, keys in containers.items() if len(keys) > THREAD_SLOTS}
    kept = set().union(*containers.values())
    assert not over and len(kept) <= THREAD_SLOTS, (
        f"{len(kept)} threads keep state with {THREAD_SLOTS} slots after pinning a thread "
        f"evicted during its command; over the bound: {over}"
    )
    assert (
        chosen in handler._hydrated_sessions
    ), "the pinned thread has no hydration guard after the hydration !ta ran before its pin"


class _WritableLog(_Log):
    """A :class:`_Log` that records the metadata writes ``!ta`` and ``!project`` make."""

    def __init__(self, meta: dict) -> None:
        super().__init__(meta)
        self.writes: list[tuple[str, dict]] = []

    def update_metadata(self, session_key: str, fields: dict) -> None:
        self.writes.append((session_key, dict(fields)))


def _pin_first(handler, count: int) -> None:
    """Pin threads ``_key(0)`` to ``_key(count - 1)`` as earlier ``!ta`` commands would."""
    slots = _slots()
    for i in range(count):
        key = _key(i)
        handler._hydrated_sessions.add(key)
        handler._thread_agents[key] = "pinned"
        slots._pinned.add((key, "agent"))


@pytest.mark.asyncio
async def test_ta_on_a_thread_evicted_during_its_lookup_keeps_its_restored_project(
    handler, monkeypatch, tmp_path
):
    """The thread's project came from its conversation metadata, so it is evictable.

    ``!ta`` awaits agent discovery while two other threads hydrate and evict the thread,
    project included. The command must read the project back before ``pin`` marks the
    thread hydrated; otherwise ``!project`` reports no project, and the next ``!ta``
    discovers agents without the project's scope.
    """
    import asyncio
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 2)
    project = tmp_path / "proj"
    project.mkdir()
    log = _WritableLog({"agent": "reviewer", "project": str(project)})
    chosen, other = _key(THREAD_SLOTS - 2), _key(THREAD_SLOTS - 1)
    for key in (chosen, other):
        await handler._hydrate_thread_overrides(key, log)
    assert handler._thread_projects.get(chosen) == str(project)
    loop = asyncio.get_running_loop()
    hop2 = LoopHop(loop)

    def resolve_while_two_threads_hydrate(name, project_dir):
        # Runs in the worker thread ``!ta`` awaits: both threads hydrate on the loop
        # and evict ``chosen`` before agent discovery answers.
        for i in (THREAD_SLOTS, THREAD_SLOTS + 1):
            hop2.run(handler._hydrate_thread_overrides(_key(i), log), "_hydrate_thread_overrides")
        return "chosen-agent"

    monkeypatch.setattr(handler, "_resolve_agent_name", resolve_while_two_threads_hydrate)
    await handler._bang_thread_agent(
        "!ta chosen-agent", AsyncMock(), AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
    )
    hop2.check()
    assert handler._thread_agents.get(chosen) == "chosen-agent"
    assert handler._thread_projects.get(chosen) == str(project), (
        "the thread evicted during !ta lost the project its conversation metadata names: "
        f"{handler._thread_projects.get(chosen)!r}"
    )
    slack = AsyncMock()
    await handler._bang_project(
        "!project", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
    )
    reply = slack.post_message.await_args.args[1]
    assert str(project) in reply, f"!project reports {reply!r}"


@pytest.mark.asyncio
async def test_project_on_an_evicted_thread_keeps_its_restored_agent(handler, tmp_path):
    """The other ``pin`` call site: the thread is evicted after its message hydrated and
    before ``!project`` pins it, and keeps the agent its conversation metadata names."""
    import os
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 2)
    log = _WritableLog({"agent": "reviewer", "project": str(tmp_path)})
    chosen = _key(THREAD_SLOTS - 2)
    for i in range(THREAD_SLOTS - 2, THREAD_SLOTS + 2):
        await handler._hydrate_thread_overrides(_key(i), log)
    assert chosen not in handler._hydrated_sessions, "the thread was not evicted"
    target = tmp_path / "proj"
    target.mkdir()
    await handler._bang_project(
        f"!project {target}", AsyncMock(), AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
    )
    assert handler._thread_projects.get(chosen) == os.path.realpath(target)
    assert handler._thread_agents.get(chosen) == "reviewer", (
        "the thread evicted before !project lost the agent its conversation metadata names: "
        f"{handler._thread_agents.get(chosen)!r}"
    )


@pytest.mark.asyncio
async def test_hydration_keeps_no_16_mib_override_from_the_metadata(handler, tmp_path, caplog):
    """The per-string half of the bound: a value read back from the metadata is capped."""
    import logging

    huge = "x" * (16 * 1024 * 1024)
    key = _key(0)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.thread_override_slots"):
        await handler._hydrate_thread_overrides(
            key, _Log({"agent": huge, "project": f"{tmp_path}/{huge}"})
        )
    assert key not in handler._thread_agents, "a 16 MiB agent name was kept"
    assert key not in handler._thread_projects, "a 16 MiB project path was kept"
    assert key in handler._hydrated_sessions, "the thread is still hydrated, on the default"
    assert caplog.text.count("it is not kept") == 2, "each skipped value is logged once"
    assert huge[:64] not in caplog.text, "the log names the length, never the value"


@pytest.mark.asyncio
async def test_an_override_at_the_limit_is_kept_and_one_character_over_is_not(handler, tmp_path):
    limit = _value_limit()
    at_agent, at_project = "a" * limit, _path_of_length(str(tmp_path), limit)
    await handler._hydrate_thread_overrides(
        _key(0), _Log({"agent": at_agent, "project": at_project})
    )
    over_agent, over_project = "a" * (limit + 1), _path_of_length(str(tmp_path), limit + 1)
    await handler._hydrate_thread_overrides(
        _key(1), _Log({"agent": over_agent, "project": over_project})
    )
    assert handler._thread_agents.get(_key(0)) == at_agent
    assert handler._thread_projects.get(_key(0)) == at_project
    assert _key(1) not in handler._thread_agents
    assert _key(1) not in handler._thread_projects


@pytest.mark.asyncio
async def test_a_project_override_that_is_not_text_is_not_kept(handler):
    key = _key(0)
    await handler._hydrate_thread_overrides(
        key, _Log({"agent": "reviewer", "project": ["/srv/project"]})
    )
    assert handler._thread_agents.get(key) == "reviewer", "the text override beside it is kept"
    assert key not in handler._thread_projects


def _project_agent(project, stem: str, declared_name: str) -> None:
    """A project agent spec whose declared ``name`` is *declared_name*."""
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{stem}.json").write_text(json.dumps({"name": declared_name}), encoding="utf-8")


@pytest.mark.asyncio
async def test_ta_refuses_a_project_agent_name_over_the_limit_and_sets_nothing(handler, tmp_path):
    """``!ta`` keeps the name a project spec declares, which nothing else limits."""
    from unittest.mock import AsyncMock

    slots = _slots()
    limit = _value_limit()
    project = tmp_path / "proj"
    key = _key(0)
    handler._thread_projects[key] = str(project)

    async def ta(slack):
        await handler._bang_thread_agent(
            "!ta x", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", key, "U1", None
        )
        return [call.args[1] for call in slack.post_message.await_args_list]

    _project_agent(project, "x", "n" * (limit + 1))
    replies = await ta(AsyncMock())
    assert key not in handler._thread_agents, "an over-limit agent name was kept"
    assert (key, "agent") not in slots._pinned, "a refused thread was pinned"
    assert any(f"over the {limit}-character limit" in text for text in replies), replies
    assert all("n" * 64 not in text for text in replies), "the reply quotes the name"

    _project_agent(project, "x", "n" * limit)
    await ta(AsyncMock())
    assert handler._thread_agents.get(key) == "n" * limit, "a name at the limit is set"


@pytest.mark.asyncio
async def test_project_refuses_a_path_over_the_limit_and_sets_nothing(
    handler, tmp_path, monkeypatch
):
    """A Linux host cannot open a directory path over 4,096 characters, so this lowers the
    limit to one that a real directory here passes; the check the setter runs is the same."""
    import os
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.slack import handler as h

    slots = _slots()
    short = tmp_path / "p"
    longer = tmp_path / "p-with-a-longer-name"
    short.mkdir()
    longer.mkdir()
    monkeypatch.setattr(slots, "OVERRIDE_VALUE_MAX_CHARS", len(os.path.realpath(short)))
    audit = MagicMock()
    monkeypatch.setattr(h, "sel", lambda: audit)
    key = _key(0)

    async def project(path):
        slack = AsyncMock()
        await handler._bang_project(
            f"!project {path}", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", key, "U1", None
        )
        return [call.args[1] for call in slack.post_message.await_args_list]

    replies = await project(longer)
    assert key not in handler._thread_projects, "an over-limit project path was kept"
    assert (key, "project") not in slots._pinned, "a refused thread was pinned"
    assert any("characters, over the" in text for text in replies), replies
    assert all(str(longer) not in text for text in replies), "the reply quotes the path"
    outcome = audit.log_tool_invocation.call_args.kwargs
    assert outcome["outcome"] == "project_denied_too_long"
    assert "project" not in outcome["metadata"], "the audit record carries the path"

    await project(short)
    assert handler._thread_projects.get(key) == os.path.realpath(
        short
    ), "a path at the limit is set"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "setup", "outcome"),
    [
        ("!ta", "name_over_limit", "agent_denied_too_long"),
        ("!ta", "every_pin_taken", "agent_denied_capacity"),
        ("!project", "every_pin_taken", "project_denied_capacity"),
    ],
    ids=["ta-name-over-limit", "ta-every-pin-taken", "project-every-pin-taken"],
)
async def test_each_refusal_writes_an_audit_record_without_the_over_limit_value(
    handler, monkeypatch, tmp_path, command, setup, outcome
):
    """Like every other ``!project`` refusal, the refusals this bound adds are audited."""
    from unittest.mock import AsyncMock, MagicMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    limit = _value_limit()
    audit = MagicMock()
    monkeypatch.setattr(handler, "sel", lambda: audit)
    name = "n" * (limit + 1) if setup == "name_over_limit" else "reviewer"
    monkeypatch.setattr(handler, "_resolve_agent_name", lambda agent, project: name)
    if setup == "every_pin_taken":
        _pin_first(handler, slots.MAX_PINNED_THREADS)
    key = _key(THREAD_SLOTS)
    project = tmp_path / "proj"
    project.mkdir()
    slack = AsyncMock()
    run = handler._bang_thread_agent if command == "!ta" else handler._bang_project
    argument = "x" if command == "!ta" else str(project)
    await run(f"{command} {argument}", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", key, "U1", None)
    replies = [call.args[1] for call in slack.post_message.await_args_list]
    assert any(text.startswith("❌ Not set") for text in replies), replies
    records = [call.kwargs for call in audit.log_tool_invocation.call_args_list]
    assert records, f"{command} refused ({setup}) with no audit record"
    (record,) = records
    assert (record["tool_name"], record["tool_kind"]) == (command, "command")
    assert (record["session_key"], record["outcome"]) == (key, outcome)
    if setup == "name_over_limit":
        assert record["metadata"]["length"] == limit + 1
        assert "n" * 64 not in repr(record), "the audit record carries the over-limit name"


class _PersistingLog(_Log):
    """A :class:`_Log` whose metadata writes land, as the real conversation log's do."""

    def update_metadata(self, session_key: str, fields: dict) -> None:
        self._meta.update(fields)


def _template_meta(template: str, project: str) -> dict:
    """The metadata of a cron-created thread: a recorded execution template, and its agent."""
    from kiro_crew import execution_context

    record = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        template,
        selection_name=template,
    ).to_record()
    return {"agent": template, "project": project, "execution_context": record}


@pytest.mark.asyncio
async def test_ta_off_on_a_thread_with_a_recorded_template_stays_reset_after_eviction(
    handler, tmp_path
):
    """Hydration prefers a recorded execution template over the ``agent`` field that
    ``!ta off`` clears. Evicting the thread must not bring the template's agent back: the
    reset holds for the life of the process, as it does on main, where nothing evicts."""
    from unittest.mock import AsyncMock

    from kiro_crew.messaging.session_resume import session_agent_from_metadata

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    log = _PersistingLog(_template_meta("reviewer", str(tmp_path)))
    chosen = _key(0)
    await handler._hydrate_thread_overrides(chosen, log)
    assert handler._thread_agents.get(chosen) == "reviewer"
    slack = AsyncMock()
    await handler._bang_thread_agent(
        "!ta off", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
    )
    assert log.get_metadata(chosen)["agent"] == ""
    assert session_agent_from_metadata(log.get_metadata(chosen)) == "reviewer"
    assert slack.post_message.await_args_list[0].args[1] == "🔄 Thread agent reset."
    for i in range(1, THREAD_SLOTS + 1):
        await handler._hydrate_thread_overrides(_key(i), None)
    await handler._hydrate_thread_overrides(chosen, log)
    assert handler._thread_agents.get(chosen) is None, (
        "eviction undid !ta off: the thread's next message hydrated the recorded template "
        f"back as its agent ({handler._thread_agents.get(chosen)!r})"
    )
    assert handler._thread_projects.get(chosen) == str(tmp_path)


@pytest.mark.asyncio
async def test_project_off_whose_write_failed_stays_cleared_after_eviction(handler, tmp_path):
    """``!project off`` cannot persist the clear (the write raises), so hydration would read
    the old project back. Evicting the thread must not restore it."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    log = _Log({"project": str(tmp_path)})  # no update_metadata: every write fails
    chosen = _key(0)
    await handler._hydrate_thread_overrides(chosen, log)
    assert handler._thread_projects.get(chosen) == str(tmp_path)
    await handler._bang_project(
        "!project off", AsyncMock(), AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
    )
    for i in range(1, THREAD_SLOTS + 1):
        await handler._hydrate_thread_overrides(_key(i), None)
    await handler._hydrate_thread_overrides(chosen, log)
    assert handler._thread_projects.get(chosen) is None, (
        "eviction undid !project off: the thread's next message hydrated the old project "
        f"back ({handler._thread_projects.get(chosen)!r})"
    )


@pytest.mark.asyncio
async def test_ta_off_for_a_thread_evicted_before_it_runs_tracks_no_extra_thread(handler):
    """``!ta off`` releases the pin of a thread evicted while the command awaited, one it
    never pinned. The command hydrates the thread again first, which makes room for it
    inside the bound; releasing the pin it never held must not track it a second time:
    with every pin but one taken, that would keep one thread more than ``THREAD_SLOTS``."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 1)
    chosen, other = _key(THREAD_SLOTS), _key(THREAD_SLOTS + 1)
    for key in (chosen, other):
        await handler._hydrate_thread_overrides(key, None)
    assert chosen not in slots._order, "the thread was not evicted"
    await handler._bang_thread_agent(
        "!ta off", AsyncMock(), AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", None
    )
    tracked = {key for key, _kind in slots._pinned} | set(slots._order)
    assert len(tracked) <= THREAD_SLOTS, (
        f"{len(tracked)} threads are tracked with {THREAD_SLOTS} slots after !ta off on a "
        "thread evicted before it ran"
    )


class _GatedLog(_PersistingLog):
    """A :class:`_PersistingLog` whose metadata writes wait for ``gate``, as a slow disk
    write does. Reads never wait. ``writing`` is set once a write has started."""

    def __init__(self, meta: dict) -> None:
        super().__init__(meta)
        self.gate = threading.Event()
        self.writing = threading.Event()

    def update_metadata(self, session_key: str, fields: dict) -> None:
        self.writing.set()
        assert self.gate.wait(10), "the write gate never opened"
        super().update_metadata(session_key, fields)


class _LateReadLog(_GatedLog):
    """A :class:`_GatedLog` whose read, when it starts while a write is in flight, takes the
    metadata as it was before the write and answers only once ``read_gate`` opens."""

    def __init__(self, meta: dict) -> None:
        super().__init__(meta)
        self.read_gate = threading.Event()
        self.read_parked = threading.Event()
        self.write_done = threading.Event()

    def get_metadata(self, session_key: str) -> dict:
        if self.writing.is_set() and not self.write_done.is_set():
            before_the_write = super().get_metadata(session_key)
            self.read_parked.set()
            assert self.read_gate.wait(10), "the read gate never opened"
            return before_the_write
        return super().get_metadata(session_key)

    def update_metadata(self, session_key: str, fields: dict) -> None:
        super().update_metadata(session_key, fields)
        self.write_done.set()


async def _set_in_a_worker(flag: threading.Event) -> None:
    """Wait, off the loop and within a bound, for *flag*, which a worker thread sets."""
    assert await asyncio.to_thread(flag.wait, 10), "a worker thread never got there"


async def _drain(*tasks: asyncio.Future) -> None:
    """Let *tasks* end, so a failed assertion leaves none of them waiting on a gate."""
    await await_until(asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True)))


@pytest.mark.asyncio
async def test_ta_off_stays_reset_when_its_thread_hydrates_again_during_the_write(handler):
    """With every pin but one taken, another thread's message evicts this thread while ``!ta
    off`` awaits its metadata write, and this thread's next message hydrates it again from a
    read that ran before the write landed. ``!ta off`` then reads no agent back and releases
    the pin; the agent that hydration published must not outlive the reset it replies."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 1)
    chosen, other = _key(THREAD_SLOTS), _key(THREAD_SLOTS + 1)
    # The `agent` field names an agent (a `!ta` from before a restart) and no execution
    # template is recorded, so `!ta off` releases the pin instead of keeping it.
    log = _GatedLog({"agent": "reviewer"})
    await handler._hydrate_thread_overrides(chosen, log)
    assert handler._thread_agents.get(chosen) == "reviewer"
    slack = AsyncMock()
    command = asyncio.ensure_future(
        handler._bang_thread_agent(
            "!ta off", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
        )
    )
    try:
        await _set_in_a_worker(log.writing)  # `!ta off` awaits its metadata write
        await handler._hydrate_thread_overrides(other, None)  # another thread's message
        assert chosen not in handler._hydrated_sessions, "the thread was not evicted"
        await handler._hydrate_thread_overrides(chosen, log)  # this thread's next message
        log.gate.set()
        await await_until(command)
    finally:
        log.gate.set()
        await _drain(command)
    reply = slack.post_message.await_args_list[0].args[1]
    assert handler._thread_agents.get(chosen) is None, (
        f"!ta off replied {reply!r} and wrote agent={log.get_metadata(chosen)['agent']!r}, "
        f"yet the thread still runs {handler._thread_agents.get(chosen)!r}"
    )


@pytest.mark.asyncio
async def test_ta_off_stays_reset_when_a_read_from_before_its_write_publishes_after_it(handler):
    """As above, except that the thread's next message read the metadata before the write
    landed and publishes only after ``!ta off`` has replied. The reset must still hold."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 1)
    chosen, other = _key(THREAD_SLOTS), _key(THREAD_SLOTS + 1)
    log = _LateReadLog({"agent": "reviewer"})
    await handler._hydrate_thread_overrides(chosen, log)
    slack = AsyncMock()
    command = asyncio.ensure_future(
        handler._bang_thread_agent(
            "!ta off", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
        )
    )
    late = None
    try:
        await _set_in_a_worker(log.writing)
        await handler._hydrate_thread_overrides(other, None)  # another thread's message
        assert chosen not in handler._hydrated_sessions, "the thread was not evicted"
        late = asyncio.ensure_future(handler._hydrate_thread_overrides(chosen, log))
        await _set_in_a_worker(log.read_parked)  # it read the metadata before the write
        log.gate.set()
        await await_until(command)  # `!ta off` reads no agent back and replies
        log.read_gate.set()
        await await_until(late)
    finally:
        log.gate.set()
        log.read_gate.set()
        await _drain(command, *([late] if late is not None else []))
    reply = slack.post_message.await_args_list[0].args[1]
    assert handler._thread_agents.get(chosen) is None, (
        f"!ta off replied {reply!r} and wrote agent={log.get_metadata(chosen)['agent']!r}, "
        f"yet the thread still runs {handler._thread_agents.get(chosen)!r}"
    )


@pytest.mark.asyncio
async def test_project_off_stays_cleared_when_its_thread_hydrates_again_during_the_write(
    handler, tmp_path
):
    """The ``!project off`` form of the first race above: the thread's next message hydrates
    the old project from a read that ran before the clear was written."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 1)
    chosen, other = _key(THREAD_SLOTS), _key(THREAD_SLOTS + 1)
    log = _GatedLog({"project": str(tmp_path)})
    await handler._hydrate_thread_overrides(chosen, log)
    assert handler._thread_projects.get(chosen) == str(tmp_path)
    slack = AsyncMock()
    command = asyncio.ensure_future(
        handler._bang_project(
            "!project off", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
        )
    )
    try:
        await _set_in_a_worker(log.writing)
        await handler._hydrate_thread_overrides(other, None)
        assert chosen not in handler._hydrated_sessions, "the thread was not evicted"
        await handler._hydrate_thread_overrides(chosen, log)
        log.gate.set()
        await await_until(command)
    finally:
        log.gate.set()
        await _drain(command)
    reply = slack.post_message.await_args_list[0].args[1]
    assert handler._thread_projects.get(chosen) is None, (
        f"!project off replied {reply!r} and wrote project="
        f"{log.get_metadata(chosen)['project']!r}, yet the thread still has "
        f"{handler._thread_projects.get(chosen)!r}"
    )


@pytest.mark.asyncio
async def test_ta_off_stays_reset_when_a_stale_read_publishes_after_a_later_eviction(handler):
    """The thread's next message read the metadata before ``!ta off``'s write landed, and
    that read is still in flight after the command has replied. Another thread's message
    then evicts the thread again, so the read finds it outside the hydration guard. It must
    not publish the agent it read: a ``!ta`` write landed after the read began."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 1)
    chosen, other, third = (_key(THREAD_SLOTS + i) for i in range(3))
    log = _LateReadLog({"agent": "reviewer"})
    await handler._hydrate_thread_overrides(chosen, log)
    slack = AsyncMock()
    command = asyncio.ensure_future(
        handler._bang_thread_agent(
            "!ta off", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
        )
    )
    late = None
    try:
        await _set_in_a_worker(log.writing)
        await handler._hydrate_thread_overrides(other, None)  # another thread's message
        assert chosen not in handler._hydrated_sessions, "the thread was not evicted"
        late = asyncio.ensure_future(handler._hydrate_thread_overrides(chosen, log))
        await _set_in_a_worker(log.read_parked)  # it read the metadata before the write
        log.gate.set()
        await await_until(command)  # `!ta off` replies
        await handler._hydrate_thread_overrides(third, None)  # a third thread's message
        assert chosen not in handler._hydrated_sessions, "not evicted after the command"
        log.read_gate.set()
        await await_until(late)
    finally:
        log.gate.set()
        log.read_gate.set()
        await _drain(command, *([late] if late is not None else []))
    reply = slack.post_message.await_args_list[0].args[1]
    assert handler._thread_agents.get(chosen) is None, (
        f"!ta off replied {reply!r}; a read from before its write, published after a later "
        f"eviction, left the thread running {handler._thread_agents.get(chosen)!r}"
    )


@pytest.mark.asyncio
async def test_project_off_stays_cleared_when_a_stale_read_publishes_after_a_later_eviction(
    handler, tmp_path
):
    """The ``!project off`` form of the race above."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 1)
    chosen, other, third = (_key(THREAD_SLOTS + i) for i in range(3))
    log = _LateReadLog({"project": str(tmp_path)})
    await handler._hydrate_thread_overrides(chosen, log)
    assert handler._thread_projects.get(chosen) == str(tmp_path)
    slack = AsyncMock()
    command = asyncio.ensure_future(
        handler._bang_project(
            "!project off", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
        )
    )
    late = None
    try:
        await _set_in_a_worker(log.writing)
        await handler._hydrate_thread_overrides(other, None)
        assert chosen not in handler._hydrated_sessions, "the thread was not evicted"
        late = asyncio.ensure_future(handler._hydrate_thread_overrides(chosen, log))
        await _set_in_a_worker(log.read_parked)
        log.gate.set()
        await await_until(command)
        await handler._hydrate_thread_overrides(third, None)
        assert chosen not in handler._hydrated_sessions, "not evicted after the command"
        log.read_gate.set()
        await await_until(late)
    finally:
        log.gate.set()
        log.read_gate.set()
        await _drain(command, *([late] if late is not None else []))
    reply = slack.post_message.await_args_list[0].args[1]
    assert handler._thread_projects.get(chosen) is None, (
        f"!project off replied {reply!r}; a read from before its write, published after a "
        f"later eviction, left the thread with {handler._thread_projects.get(chosen)!r}"
    )


class _PerThreadLog:
    """Metadata kept per thread, as the real conversation log keeps it. Writes land."""

    def __init__(self, meta: dict[str, dict]) -> None:
        self.meta = {key: dict(fields) for key, fields in meta.items()}

    def get_metadata(self, session_key: str) -> dict:
        return dict(self.meta.get(session_key, {}))

    def get_metadata_status(self, session_key: str) -> tuple[dict, bool]:
        return self.get_metadata(session_key), True

    def update_metadata(self, session_key: str, fields: dict) -> None:
        self.meta.setdefault(session_key, {}).update(fields)


@pytest.mark.asyncio
async def test_hydration_publishes_nothing_while_every_read_overlaps_a_command_write(handler):
    """``!ta off`` in another thread runs to the end, its metadata write included, during
    each of a thread's hydration reads, so each read may predate a write. Hydration then
    publishes nothing and does not mark the thread hydrated, and the thread's next message,
    with no write landing during its read, hydrates it."""
    from unittest.mock import AsyncMock

    chosen = _key(0)
    others = [_key(i) for i in range(1, 10)]
    loop = asyncio.get_running_loop()
    hop3 = LoopHop(loop)
    reads: list[str] = []

    class _CommandDuringEachRead(_PerThreadLog):
        overlap = True

        def get_metadata(self, session_key: str) -> dict:
            if session_key == chosen and self.overlap:
                reads.append(session_key)
                # Runs on the loop, to its reply, while this read is in flight.
                hop3.run(
                    handler._bang_thread_agent(
                        "!ta off",
                        AsyncMock(),
                        AsyncMock(),
                        "C0SYNTH01",
                        "t1",
                        "m1",
                        others.pop(0),
                        "U1",
                        self,
                    ),
                    "_bang_thread_agent",
                )
            return super().get_metadata(session_key)

    log = _CommandDuringEachRead({chosen: {"agent": "reviewer"}})
    await handler._hydrate_thread_overrides(chosen, log)
    assert handler._thread_agents.get(chosen) is None, (
        f"hydration published {handler._thread_agents.get(chosen)!r} from a read that a "
        f"command's metadata write overlapped (reads: {len(reads)})"
    )
    assert chosen not in handler._hydrated_sessions, "the thread was marked hydrated"
    slots = _slots()
    assert len(reads) == slots.HYDRATION_READ_ATTEMPTS, f"{len(reads)} reads"
    assert chosen not in slots._order, "the thread is tracked though it was not hydrated"
    log.overlap = False
    await handler._hydrate_thread_overrides(chosen, log)  # the thread's next message
    assert chosen in handler._hydrated_sessions
    assert handler._thread_agents.get(chosen) == "reviewer"


@pytest.mark.asyncio
async def test_ta_on_an_evicted_thread_whose_hydration_gives_up_reads_its_project_next(
    handler, monkeypatch, tmp_path
):
    """The thread was evicted; its conversation metadata names a project and records an
    execution template. ``!ta`` hydrates it again just before it pins, and a ``!ta off`` in
    another thread writes metadata during each of that hydration's reads, so it gives up.
    The pin must not mark the thread hydrated, so its next message reads the project back,
    and that hydration must not publish the template's agent over the agent ``!ta`` set."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 2)
    project = tmp_path / "proj"
    project.mkdir()
    chosen = _key(THREAD_SLOTS - 2)
    others = [_key(THREAD_SLOTS + i) for i in range(10)]
    loop = asyncio.get_running_loop()
    hop4 = LoopHop(loop)

    class _WriteDuringEachRead(_PerThreadLog):
        overlap = False

        def get_metadata(self, session_key: str) -> dict:
            if session_key == chosen and self.overlap:
                # Runs on the loop, to its reply, while this read is in flight.
                hop4.run(
                    handler._bang_thread_agent(
                        "!ta off",
                        AsyncMock(),
                        AsyncMock(),
                        "C0SYNTH01",
                        "t1",
                        "m1",
                        others.pop(0),
                        "U1",
                        self,
                    ),
                    "_bang_thread_agent",
                )
            return super().get_metadata(session_key)

    log = _WriteDuringEachRead({chosen: _template_meta("reviewer", str(project))})
    await handler._hydrate_thread_overrides(chosen, log)  # the thread's first message
    for key in (_key(THREAD_SLOTS - 1), _key(THREAD_SLOTS + 20)):
        await handler._hydrate_thread_overrides(key, None)  # two other threads' messages
    assert chosen not in handler._hydrated_sessions, "the thread was not evicted"
    monkeypatch.setattr(handler, "_resolve_agent_name", lambda name, project_dir: "chosen-agent")
    log.overlap = True
    await handler._bang_thread_agent(
        "!ta chosen-agent", AsyncMock(), AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
    )
    log.overlap = False
    assert len(others) == 10 - slots.HYDRATION_READ_ATTEMPTS, "the hydration did not give up"
    assert (chosen, "agent") in slots._pinned
    await handler._hydrate_thread_overrides(chosen, log)  # the thread's next message
    assert handler._thread_projects.get(chosen) == str(project), (
        "the pin marked the thread hydrated after its hydration gave up, so its next message "
        f"did not read its project back: {handler._thread_projects.get(chosen)!r}"
    )
    assert handler._thread_agents.get(chosen) == "chosen-agent", (
        "the thread's next message hydrated the recorded template over the agent !ta set: "
        f"{handler._thread_agents.get(chosen)!r}"
    )


class _StoreThatCanFail(_PersistingLog):
    """A :class:`_PersistingLog` whose store can be down. Then a write raises, and a read
    answers as the real conversation log does for a line it could not read: empty metadata
    from ``get_metadata``, ``({}, False)`` from ``get_metadata_status``."""

    down = False

    def get_metadata(self, session_key: str) -> dict:
        return {} if self.down else super().get_metadata(session_key)

    def get_metadata_status(self, session_key: str) -> tuple[dict, bool]:
        return ({}, False) if self.down else (super().get_metadata(session_key), True)

    def update_metadata(self, session_key: str, fields: dict) -> None:
        if self.down:
            raise OSError("the metadata store is unavailable")
        super().update_metadata(session_key, fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "project"])
async def test_a_reset_whose_metadata_cannot_be_read_back_keeps_its_pin(handler, tmp_path, kind):
    """The store fails both the reset's write and its read-back, then recovers with the old
    value still recorded. The reset cannot show that hydration would restore nothing, so it
    keeps its pin, and eviction after the store recovers must not bring the value back."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    value = "reviewer" if kind == "agent" else str(tmp_path)
    command = "!ta off" if kind == "agent" else "!project off"
    run = handler._bang_thread_agent if kind == "agent" else handler._bang_project
    values = handler._thread_agents if kind == "agent" else handler._thread_projects
    log = _StoreThatCanFail({kind: value})
    chosen = _key(0)
    await handler._hydrate_thread_overrides(chosen, log)
    assert values.get(chosen) == value
    log.down = True
    slack = AsyncMock()
    await run(command, slack, AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log)
    log.down = False
    reply = slack.post_message.await_args_list[0].args[1]
    for i in range(1, THREAD_SLOTS + 1):
        await handler._hydrate_thread_overrides(_key(i), None)
    await handler._hydrate_thread_overrides(chosen, log)  # the thread's next message
    assert values.get(chosen) is None, (
        f"{command} replied {reply!r} while the store was down; after it recovered, eviction "
        f"brought {values.get(chosen)!r} back"
    )
    assert (chosen, kind) in slots._pinned, "the reset released a pin it could not verify"


async def _evict_from_two_free_slots(handler, chosen: str) -> None:
    """Two other threads' messages: with every slot but two pinned, *chosen* is evicted."""
    for key in (_key(THREAD_SLOTS - 1), _key(THREAD_SLOTS + 20)):
        await handler._hydrate_thread_overrides(key, None)
    assert chosen not in handler._hydrated_sessions, "the thread was not evicted"


@pytest.mark.asyncio
async def test_ta_on_an_evicted_thread_while_its_metadata_cannot_be_read_reads_its_project_next(
    handler, monkeypatch, tmp_path
):
    """The thread was evicted, and its conversation metadata names a project. ``!ta`` runs
    while the store cannot read that line, so the hydration just before its pin reads
    nothing. That read must not mark the thread hydrated: once the store recovers, the
    thread's next message reads its project back, and keeps the agent ``!ta`` set."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 2)
    project = tmp_path / "proj"
    project.mkdir()
    chosen = _key(THREAD_SLOTS - 2)
    log = _StoreThatCanFail({"project": str(project)})
    await handler._hydrate_thread_overrides(chosen, log)  # the thread's first message
    assert handler._thread_projects.get(chosen) == str(project)
    await _evict_from_two_free_slots(handler, chosen)
    monkeypatch.setattr(handler, "_resolve_agent_name", lambda name, project_dir: "chosen-agent")
    log.down = True
    await handler._bang_thread_agent(
        "!ta chosen-agent", AsyncMock(), AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
    )
    log.down = False
    assert (chosen, "agent") in slots._pinned
    marked = chosen in handler._hydrated_sessions
    await handler._hydrate_thread_overrides(chosen, log)  # the thread's next message
    assert handler._thread_projects.get(chosen) == str(project), (
        f"a hydration that could not read the metadata marked the thread hydrated "
        f"({marked}) before its pin, so its next message did not read its project back: "
        f"{handler._thread_projects.get(chosen)!r}"
    )
    assert handler._thread_agents.get(chosen) == "chosen-agent"


@pytest.mark.asyncio
async def test_ta_off_while_its_metadata_cannot_be_read_keeps_its_project_after_recovery(
    handler, tmp_path
):
    """``!ta off`` on an evicted thread while the store fails its write and cannot read
    the line: the reset keeps its pin, and the hydration before that pin read nothing, so
    it must not mark the thread hydrated. Once the store recovers, the thread's next
    message reads its project back, and the agent stays reset."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, THREAD_SLOTS - 2)
    project = tmp_path / "proj"
    project.mkdir()
    chosen = _key(THREAD_SLOTS - 2)
    log = _StoreThatCanFail({"agent": "reviewer", "project": str(project)})
    await handler._hydrate_thread_overrides(chosen, log)
    assert handler._thread_projects.get(chosen) == str(project)
    await _evict_from_two_free_slots(handler, chosen)
    log.down = True
    slack = AsyncMock()
    await handler._bang_thread_agent(
        "!ta off", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
    )
    log.down = False
    reply = slack.post_message.await_args_list[0].args[1]
    assert (chosen, "agent") in slots._pinned, "the reset released a pin it could not verify"
    marked = chosen in handler._hydrated_sessions
    for i in range(THREAD_SLOTS + 30, THREAD_SLOTS + 40):
        await handler._hydrate_thread_overrides(_key(i), None)  # other threads' messages
    await handler._hydrate_thread_overrides(chosen, log)  # the thread's next message
    agent = handler._thread_agents.get(chosen)
    assert agent is None, f"the reset agent came back: {agent!r}"
    assert handler._thread_projects.get(chosen) == str(project), (
        f"!ta off replied {reply!r} while the store was down, and its hydration marked the "
        f"thread hydrated ({marked}) without reading it, so after the store recovered the "
        f"next message did not read its project back: {handler._thread_projects.get(chosen)!r}"
    )


class _UnreadableButWritable(_StoreThatCanFail):
    """A :class:`_StoreThatCanFail` whose writes land even while its reads cannot."""

    def update_metadata(self, session_key: str, fields: dict) -> None:
        _PersistingLog.update_metadata(self, session_key, fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("write_lands", [True, False])
async def test_a_reset_that_cannot_be_held_or_read_back_promises_nothing_about_the_value(
    handler, write_lands
):
    """Every pin is taken, so a reset whose metadata cannot be read back cannot be held.
    What comes back then depends on what the store kept. When the clear's write landed,
    the old agent never comes back, even after an eviction. When the write failed and the
    thread was evicted before the reset, it comes back with the thread's next message once
    the store recovers, with no eviction. The reply may say only that it can come back."""
    from unittest.mock import AsyncMock

    slots = _slots()
    assert slots is not None, "the bound does not exist"
    _pin_first(handler, slots.MAX_PINNED_THREADS)
    chosen = _key(slots.MAX_PINNED_THREADS)
    other = _key(THREAD_SLOTS + 20)
    log = (_UnreadableButWritable if write_lands else _StoreThatCanFail)({"agent": "reviewer"})
    await handler._hydrate_thread_overrides(chosen, log)
    assert handler._thread_agents.get(chosen) == "reviewer"
    if not write_lands:
        await handler._hydrate_thread_overrides(other, None)  # another thread evicts it
        assert chosen not in handler._hydrated_sessions, "the thread was not evicted"
    log.down = True
    slack = AsyncMock()
    await handler._bang_thread_agent(
        "!ta off", slack, AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", log
    )
    log.down = False
    reply = slack.post_message.await_args_list[0].args[1]
    assert reply == slots.unheld_reset_text("agent"), reply
    assert (chosen, "agent") not in slots._pinned
    if write_lands:
        await handler._hydrate_thread_overrides(other, None)  # another thread evicts it
        assert chosen not in handler._hydrated_sessions, "the thread was not evicted"
    await handler._hydrate_thread_overrides(chosen, log)  # the thread's next message
    came_back = handler._thread_agents.get(chosen)
    expected = None if write_lands else "reviewer"
    assert came_back == expected, f"after the store recovered, the next message had {came_back!r}"
    outcome = "never came back" if write_lands else "came back with no eviction"
    tied = "if it is evicted" in reply or "can come back" not in reply
    why = f"the reply ties the agent's return to an eviction, but here it {outcome}: {reply!r}"
    assert not tied, why
