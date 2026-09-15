"""Skill-read observation at the ACP layer.

The ACP client is the only place that sees every surface's tool calls, so the
observer lives there rather than in each surface's event handler. These tests
pin the properties that make it safe: the cheap gate that keeps unrelated tool
calls off the filesystem, the dedup that stops one read being credited twice
when both the initial ``tool_call`` and its refinement carry arguments, and the
two-phase split that withholds the credit until the tool reports completion.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp.client import _mentions_skill_file
from kiro_crew.skill_usage import (
    get_global_skill_read_observer,
    register_skill_read_observer,
    set_global_skill_read_observer,
)


class TestMentionsSkillFile:
    def test_shell_command_naming_a_skill(self):
        assert _mentions_skill_file(None, "cat /x/skills/a/SKILL.md") is True

    def test_read_tool_path(self):
        assert _mentions_skill_file({"path": "/x/skills/a/SKILL.md"}, None) is True

    def test_list_valued_paths(self):
        assert _mentions_skill_file({"paths": ["/a/SKILL.md"]}, None) is True

    def test_unrelated_call_is_gated_out(self):
        assert _mentions_skill_file({"path": "/etc/hosts"}, None) is False
        assert _mentions_skill_file(None, "ls -la") is False
        assert _mentions_skill_file(None, None) is False

    def test_non_dict_and_odd_values_are_tolerated(self):
        # Tool arguments are model-authored and may hold any shape.
        assert _mentions_skill_file("nope", None) is False  # type: ignore[arg-type]
        assert _mentions_skill_file({"path": 42, "x": [None, 1]}, None) is False

    def test_any_argument_name_counts(self):
        # The gate is deliberately name-agnostic: it decides only whether the
        # offloaded resolver is worth calling, and the resolver applies the
        # read-intent rules. Narrowing it here would let a differently-named
        # argument slip past observation entirely.
        assert _mentions_skill_file({"whatever": "/a/SKILL.md"}, None) is True


class _Observer:
    """Records both phases so a test can tell resolution from crediting."""

    def __init__(self, keys=("alpha",), fail_resolve=False, fail_credit=False):
        self._keys = list(keys)
        self._fail_resolve = fail_resolve
        self._fail_credit = fail_credit
        self.resolved: list[tuple[str, dict | None, str | None]] = []
        self.credited: list[list[str]] = []

    def resolve_tool_read_keys(self, tool_name="", raw_params=None, command=None):
        if self._fail_resolve:
            raise RuntimeError("resolve exploded")
        self.resolved.append((tool_name, raw_params, command))
        return list(self._keys)

    def credit_skill_reads(self, keys):
        if self._fail_credit:
            raise RuntimeError("credit exploded")
        self.credited.append(list(keys))


class _FakeClient:
    """Binds the real observation methods to only the state they touch.

    Exercises production logic without booting a kiro-cli subprocess; if those
    methods later reach for more client state the test fails loudly rather than
    passing against a fiction.
    """

    def __init__(self):
        from kiro_crew.acp.client import AcpClient

        self._skill_read_noted: set[str] = set()
        self._pending_skill_reads: dict[str, list[str]] = {}
        self._note = AcpClient._maybe_note_skill_read.__get__(self)
        self._credit = AcpClient._maybe_credit_skill_read.__get__(self)

    async def call(self, event):
        await self._note(event)

    def result(self, event):
        self._credit(event)


def _call_event(tool_call_id="t1", raw=None, command=None, tool_name="fs_read"):
    return SimpleNamespace(
        tool_call_id=tool_call_id,
        raw_tool_params=raw,
        shell_command=command,
        tool_name=tool_name,
    )


def _result_event(tool_call_id="t1", final=True):
    return SimpleNamespace(tool_call_id=tool_call_id, tool_final=final)


SKILL_ARGS = {"path": "/x/a/SKILL.md"}


class TestRegistrationIsNotRouteDependent:
    """Every runtime that owns a ContextBuilder must register the observer.

    Route-dependent crediting is the bias this feature exists to remove, so a
    runtime that records nothing would ship a smaller version of the same
    defect. The entry-point assertions read each module's SOURCE, because
    booting three runtimes in a unit test would prove less and cost more.
    """

    def setup_method(self):
        self._prior = get_global_skill_read_observer()

    def teardown_method(self):
        set_global_skill_read_observer(self._prior)

    def test_the_first_candidate_with_a_loader_wins(self):
        class _Ctx:
            skills = "loader-sentinel"

        assert register_skill_read_observer(_Ctx()) is True
        assert get_global_skill_read_observer() == "loader-sentinel"

    def test_a_later_candidate_is_used_when_the_first_has_none(self):
        # The API-server path builds its state without a context_builder and
        # reaches the loader through the task runner; registering only the first
        # candidate would leave that whole route crediting nothing.
        class _Runner:
            skills = "runner-loader"

        set_global_skill_read_observer(None)
        assert register_skill_read_observer(None, _Runner()) is True
        assert get_global_skill_read_observer() == "runner-loader"

    def test_no_candidate_reports_a_miss_rather_than_installing(self):
        set_global_skill_read_observer(None)
        assert register_skill_read_observer(None, object()) is False
        assert get_global_skill_read_observer() is None

    def test_both_server_entry_points_register(self):
        from pathlib import Path

        import kiro_crew.dashboard.server as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        for entry in ("async def start_dashboard(", "async def start_api_server("):
            assert entry in src
        assert src.count("register_skill_read_observer(") == 2

    def test_the_api_server_path_passes_the_task_runner_context(self):
        from pathlib import Path

        import kiro_crew.dashboard.server as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert 'getattr(task_runner, "_ctx", None)' in src

    def test_the_cli_entry_point_registers_without_importing_the_dashboard(self):
        from pathlib import Path

        import kiro_crew.cli_server as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "register_skill_read_observer(ctx)" in src
        # The helper is homed in a leaf module, so no runtime needs to import
        # another surface just to register.
        assert "from kiro_crew.dashboard.server import register" not in src


class TestObserveSkillRead:
    def setup_method(self):
        self._prior = get_global_skill_read_observer()

    def teardown_method(self):
        set_global_skill_read_observer(self._prior)

    @pytest.mark.asyncio
    async def test_credit_waits_for_the_tool_to_complete(self):
        # A read that is resolved but never completes must leave no delivery --
        # otherwise a denied or failed read persists a false hit.
        obs = _Observer()
        set_global_skill_read_observer(obs)
        client = _FakeClient()
        await client.call(_call_event(raw=SKILL_ARGS))
        assert obs.resolved and obs.credited == []
        client.result(_result_event())
        assert obs.credited == [["alpha"]]

    @pytest.mark.asyncio
    async def test_unfinished_result_does_not_credit(self):
        obs = _Observer()
        set_global_skill_read_observer(obs)
        client = _FakeClient()
        await client.call(_call_event(raw=SKILL_ARGS))
        client.result(_result_event(final=False))
        assert obs.credited == []

    @pytest.mark.asyncio
    async def test_same_tool_call_resolves_once(self):
        # The arguments arrive on the initial tool_call for some providers and
        # only on the refinement for others; both are observed, so without dedup
        # a single read would be resolved -- and credited -- twice.
        obs = _Observer()
        set_global_skill_read_observer(obs)
        client = _FakeClient()
        ev = _call_event(raw=SKILL_ARGS)
        await client.call(ev)
        await client.call(ev)
        assert len(obs.resolved) == 1
        client.result(_result_event())
        assert obs.credited == [["alpha"]]

    @pytest.mark.asyncio
    async def test_a_second_result_for_the_same_call_does_not_double_credit(self):
        obs = _Observer()
        set_global_skill_read_observer(obs)
        client = _FakeClient()
        await client.call(_call_event(raw=SKILL_ARGS))
        client.result(_result_event())
        client.result(_result_event())
        assert obs.credited == [["alpha"]]

    @pytest.mark.asyncio
    async def test_distinct_tool_calls_are_both_credited(self):
        obs = _Observer()
        set_global_skill_read_observer(obs)
        client = _FakeClient()
        await client.call(_call_event("t1", raw=SKILL_ARGS))
        await client.call(_call_event("t2", raw={"path": "/x/b/SKILL.md"}))
        client.result(_result_event("t1"))
        client.result(_result_event("t2"))
        assert len(obs.credited) == 2

    @pytest.mark.asyncio
    async def test_unrelated_tool_call_never_reaches_the_observer(self):
        obs = _Observer()
        set_global_skill_read_observer(obs)
        await _FakeClient().call(_call_event(raw={"path": "/etc/hosts"}))
        assert obs.resolved == []

    @pytest.mark.asyncio
    async def test_resolver_returning_nothing_credits_nothing(self):
        # A non-read tool naming a skill resolves to no keys; the result must
        # then credit nothing rather than crediting an empty read.
        obs = _Observer(keys=())
        set_global_skill_read_observer(obs)
        client = _FakeClient()
        await client.call(_call_event(raw=SKILL_ARGS, tool_name="fs_write"))
        client.result(_result_event())
        assert obs.credited == []

    @pytest.mark.asyncio
    async def test_no_observer_registered_is_a_noop(self):
        set_global_skill_read_observer(None)
        client = _FakeClient()
        await client.call(_call_event(raw=SKILL_ARGS))
        client.result(_result_event())

    @pytest.mark.asyncio
    async def test_resolver_failure_does_not_propagate(self):
        set_global_skill_read_observer(_Observer(fail_resolve=True))
        await _FakeClient().call(_call_event(raw=SKILL_ARGS))

    @pytest.mark.asyncio
    async def test_credit_failure_does_not_propagate(self):
        # Telemetry must never disturb the tool call it observes.
        obs = _Observer(fail_credit=True)
        set_global_skill_read_observer(obs)
        client = _FakeClient()
        await client.call(_call_event(raw=SKILL_ARGS))
        client.result(_result_event())

    @pytest.mark.asyncio
    async def test_resolution_runs_off_the_event_loop(self):
        # A skills-tree walk on the loop stalls every session in the gateway,
        # so the resolver must not execute on the loop's thread.
        import threading

        loop_thread = threading.get_ident()
        seen: list[int] = []

        class _ThreadProbe(_Observer):
            def resolve_tool_read_keys(self, tool_name="", raw_params=None, command=None):
                seen.append(threading.get_ident())
                return ["alpha"]

        set_global_skill_read_observer(_ThreadProbe())
        await _FakeClient().call(_call_event(raw=SKILL_ARGS))
        assert seen and seen[0] != loop_thread
