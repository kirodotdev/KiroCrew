"""The Slack handler's per-thread override state is bounded, and eviction is recoverable.

``slack.handler`` keeps a hydration guard (``_hydrated_sessions``) and two override
maps (``_thread_agents``, ``_thread_projects``), each gaining one entry per thread the
bot hears from. They are held to ``THREAD_SLOTS`` threads together: an evicted thread
loses all three entries, and its next message hydrates again from the conversation
metadata. An override a command set in this process is pinned and never evicted.
"""

from __future__ import annotations

import json

import pytest

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
    its override, and restore the thread's hydration guard so its next message does
    not hydrate over that override.
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

    def resolve_while_two_threads_hydrate(name, project):
        # Runs in the worker thread ``!ta`` awaits: both threads hydrate on the loop
        # before agent discovery answers.
        for i in (THREAD_SLOTS, THREAD_SLOTS + 1):
            asyncio.run_coroutine_threadsafe(
                handler._hydrate_thread_overrides(_key(i), log), loop
            ).result(timeout=10)
        return "chosen-agent"

    monkeypatch.setattr(handler, "_resolve_agent_name", resolve_while_two_threads_hydrate)
    await handler._bang_thread_agent(
        "!ta chosen-agent", AsyncMock(), AsyncMock(), "C0SYNTH01", "t1", "m1", chosen, "U1", None
    )
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
    ), "the pinned thread has no hydration guard, so its next message hydrates over the override"


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
