"""A chat session that lacks Kiro Crew's own MCP servers says so in its transcript.

On kiro-cli a custom agent gets ``kirocrew-core`` and ``kirocrew-cron`` through
its skill view only. A spawn that falls back to the authored agent (the view
ceiling reached, or projection off) runs without them. The unresolved-ref guard
records that in the gateway log and the slot list's ``mcp_report``, which no chat
surface renders, so the transcript has to say it. These tests pin:

1. ``chat_runner._notice_unresolved_control_plane`` appends one warn ``notice``
   row naming a control-plane server the session's report lists as unresolved,
   read from the guard's own output, and nothing for a third-party ref, a clean
   report, a provider without a report, or a session whose settings file
   withholds Kiro Crew's servers (the seed-declined notice covers that one).
2. A real dashboard turn on a new session whose report lists ``@kirocrew-core``
   shows that row in its transcript before the assistant's reply.
3. For a user-level custom agent that names ``@kirocrew-core`` and defines no
   servers, the runtime's KAS session start (new and load) leaves no such ref in
   the report, because the server Crew adds travels in the session-level array the
   guard judges; on kiro-cli without the skill view the row still shows.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from turn_harness import ScriptedProvider, TurnScript, run_turn

from kiro_crew.acp.mcp_ref_guard import warn_unresolved_server_refs
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.dashboard import chat_runner

#: The spec the reported case runs: an agent a package manager installed, whose
#: tools name Crew's server and a server nothing defines, with no servers of its own.
_AUTHORED_SPEC = {
    "name": "reprobot",
    "mcpServers": {},
    "tools": ["fs_read", "@kirocrew-core", "@kc-repro-absent"],
    "includeMcpJson": False,
}


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Collect ``(role, text, cls)`` for every row appended via ``append_and_surface``."""
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls="", **kw: seen.append((role, text, cls)),
    )
    return seen


def _report_for(spec: dict[str, Any] | None) -> McpSessionReport:
    """A session report holding what the guard found for *spec* on kiro-cli."""
    report = McpSessionReport()
    report.begin_session([])
    if spec is not None:
        report.record_unresolved_refs(
            warn_unresolved_server_refs(
                spec, [], backend=ACP_BACKEND_KIRO, agent="reprobot", gateway_enabled=False
            )
        )
    return report


class _Provider:
    """A provider whose session report holds what the guard found for *spec*."""

    def __init__(self, spec: dict[str, Any] | None, *, withheld: object = None) -> None:
        self._report = _report_for(spec)
        if withheld is not None:
            self.settings_withheld_cause = lambda: withheld

    def mcp_session_report(self) -> McpSessionReport:
        return self._report


def _run(provider: object) -> None:
    asyncio.run(
        chat_runner._notice_unresolved_control_plane(SimpleNamespace(), SimpleNamespace(), provider)
    )


class TestNoticeEmitted:
    def test_a_fallback_session_names_the_missing_control_plane_server(self, notices: list) -> None:
        _run(_Provider(_AUTHORED_SPEC))
        [(role, text, cls)] = notices
        assert role == "notice"
        assert cls == "msg msg-warn"
        # The leading glyph is what NoticeCard reads the warn tone from.
        assert text.startswith("⚠ ")
        assert "kirocrew-core" in text
        assert "@kirocrew-core" in text
        assert "skill view" in text
        assert "kirocrew doctor" in text
        # A third-party ref stays in the log, the report and the doctor.
        assert "kc-repro-absent" not in text
        assert "kirocrew-cron" not in text

    def test_both_control_plane_servers_are_named_in_their_order(self, notices: list) -> None:
        spec = {**_AUTHORED_SPEC, "tools": ["@kirocrew-cron", "@kirocrew-core"]}
        _run(_Provider(spec))
        [(_, text, _)] = notices
        assert "servers kirocrew-core, kirocrew-cron." in text
        assert "defines them, so their tools" in text

    def test_a_settings_cause_that_is_empty_still_shows_the_row(self, notices: list) -> None:
        _run(_Provider(_AUTHORED_SPEC, withheld=""))
        assert len(notices) == 1


class TestNoNotice:
    def test_only_a_third_party_ref_unresolved(self, notices: list) -> None:
        spec = {**_AUTHORED_SPEC, "tools": ["fs_read", "@kc-repro-absent"]}
        _run(_Provider(spec))
        assert notices == []

    def test_a_spec_that_defines_the_server(self, notices: list) -> None:
        spec = {**_AUTHORED_SPEC, "mcpServers": {"kirocrew-core": {"command": "kirocrew-mcp"}}}
        _run(_Provider(spec))
        assert notices == []

    def test_a_clean_report(self, notices: list) -> None:
        _run(_Provider(None))
        assert notices == []

    def test_a_provider_without_a_report(self, notices: list) -> None:
        _run(SimpleNamespace(mcp_session_report=lambda: None))
        _run(None)
        assert notices == []

    @pytest.mark.parametrize("cause", ["session", "project"])
    def test_a_settings_file_that_withholds_crews_servers(self, notices: list, cause: str) -> None:
        """The seed-declined notice already says Crew's tools are off, and why."""
        _run(_Provider(_AUTHORED_SPEC, withheld=cause))
        assert notices == []


class _FallbackSession(ScriptedProvider):
    """A new session started on the authored agent, without its skill view."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._report = _report_for(_AUTHORED_SPEC)

    def mcp_session_report(self) -> McpSessionReport:
        return self._report


@pytest.mark.asyncio
async def test_a_new_session_shows_it_before_the_first_reply() -> None:
    """The notice is a transcript row of the session's first turn, so a session
    started on a fallback agent says so before its first reply."""
    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="pong"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ],
            provider=_FallbackSession,
        )
    )
    for rows in (record.history_rows, record.window):
        roles = [row.get("role") for row in rows]
        assert "assistant" in roles, roles
        [notice] = [
            index
            for index, row in enumerate(rows)
            if row.get("role") == "notice" and "kirocrew-core" in str(row.get("content", ""))
        ]
        assert str(rows[notice]["content"]).startswith("⚠ ")
        assert notice < roles.index("assistant")


# --- The guard's own verdict for a user-level custom agent --------------------------
#
# The rows above hand the notice a report built from the spec as written. These read
# the report the runtime's guard leaves for a user-level custom agent whose ``tools``
# names ``@kirocrew-core`` and whose ``mcpServers`` does not define it. On KAS the
# projection adds Crew's default servers to such a spec (``harness/kas.py`` ->
# ``with_default_crew_servers``) and the runtime carries them in the session-level
# array (``kas_agents.hoist_servers_for_session``) that its guard judges, so that
# session is not told kirocrew-core is missing. On kiro-cli without the skill view the
# authored agent runs as written, so the notice still shows.

_CORE_ENTRY = {"command": "test-core", "args": ["mcp"]}
_CRON_ENTRY = {"command": "test-core", "args": ["mcp-cron"]}

_CUSTOM_SPEC = {"name": "mine", "prompt": "p", "tools": ["fs_read", "@kirocrew-core"]}


def _managed_entry(name: str, *, include_opt_in: bool = False) -> dict[str, Any] | None:
    del include_opt_in
    return {"kirocrew-core": dict(_CORE_ENTRY), "kirocrew-cron": dict(_CRON_ENTRY)}.get(name)


@pytest.fixture
def custom_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """The checkout a session of the user-level custom agent ``mine`` runs in."""
    import json

    import kiro_crew.agent as agent_mod
    import kiro_crew.config.paths as paths_mod
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", _managed_entry)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _agent: None)
    monkeypatch.setattr("kiro_crew.apps.manager.owning_app_for_agent", lambda agent: None)
    user = paths_mod.kiro_agents_dir()
    user.mkdir(parents=True, exist_ok=True)
    (user / "mine.json").write_text(json.dumps(_CUSTOM_SPEC), encoding="utf-8")
    checkout = tmp_path / "checkout"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    return checkout


def _guarded_report(backend: str, checkout: Any) -> McpSessionReport:
    """The session report the runtime's guard leaves for ``mine`` on *backend*.

    The pooled hop and the guard alone, with the pooled array as the wire: the
    array a kiro-cli session gets when no skill view adds its control-plane element.
    """
    from kiro_crew.acp.runtime_session_setup import (
        _guard_unresolved_mcp_refs,
        _pooled_session_servers_and_ref_spec,
    )

    pooled, ref_spec, mcp_json = _pooled_session_servers_and_ref_spec(
        None, "mine", backend, checkout
    )
    assert ref_spec is not None, "the guard read the authored spec"
    report = McpSessionReport()
    report.begin_session(pooled)
    runtime = SimpleNamespace(
        acp_backend=backend,
        _mcp_gateway_overlay=None,
        _child_env_names=frozenset(),
        _expect_mcp_reports=False,
    )
    handle = SimpleNamespace(mcp_session_report=lambda: report)
    _guard_unresolved_mcp_refs(runtime, handle, ref_spec, "mine", pooled, mcp_json)
    return report


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_a_kas_custom_agent_session_is_not_told_kirocrew_core_is_missing(
    custom_agent: Any, notices: list, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    """The runtime's own KAS session start: projection, hoist, report and guard.

    Only the request is faked, so the guard judges the array the session was sent.
    """
    from unittest.mock import AsyncMock

    from acp_runtime_helpers import _make_runtime

    import kiro_crew.acp.runtime as runtime_mod
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._can_load_session = True
    rt._work_dir = custom_agent
    sent: list[tuple[str, dict[str, Any]]] = []

    async def fake_send(method: str, params: dict[str, Any], timeout: Any = None) -> dict:
        sent.append((method, params))
        if method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD):
            return {"sessionId": "sid-kas", "modes": {"currentModeId": "mine"}}
        return {}

    judged: list[tuple[Any, list[Any]]] = []
    judge = runtime_mod.warn_unresolved_server_refs

    def recording_judge(spec: Any, wire: Any, **kwargs: Any) -> list[str]:
        judged.append((spec, [entry.get("name") for entry in wire]))
        return judge(spec, wire, **kwargs)

    monkeypatch.setattr(rt, "_send_and_await", fake_send)
    # Through the facade, which writes every holder of the name (``_facade_mirror``).
    monkeypatch.setattr(runtime_mod, "warn_unresolved_server_refs", recording_judge)
    # Readiness is not this test's concern; the fake engine reports no server.
    monkeypatch.setattr(AcpSessionHandle, "wait_mcp_ready", AsyncMock(return_value=None))
    cwd, key = str(custom_agent), "dashboard:chat-1"
    if resume:
        handle = await rt.load_session("", "sid-kas", cwd=cwd, agent="mine", session_key=key)
        method = METHOD_SESSION_LOAD
    else:
        handle = await rt.create_session(cwd=cwd, agent="mine", session_key=key)
        method = METHOD_SESSION_NEW

    params = next(p for m, p in sent if m == method)
    # KAS mounted it: Crew's default server travels in the session-level array.
    assert "kirocrew-core" in [entry.get("name") for entry in params["mcpServers"]]
    # The guard ran on the spec as written, which names the server and defines none.
    assert judged, "the unresolved-ref guard never judged this session"
    for spec, wire_names in judged:
        assert "@kirocrew-core" in spec["tools"] and not spec.get("mcpServers")
        assert "kirocrew-core" in wire_names
    assert handle.mcp_session_report().payload()["unresolved_refs"] == []

    await chat_runner._notice_unresolved_control_plane(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(mcp_session_report=handle.mcp_session_report),
    )

    assert notices == [], notices


@pytest.mark.asyncio
async def test_a_kiro_cli_session_without_its_skill_view_still_shows_the_notice(
    custom_agent: Any, notices: list
) -> None:
    report = _guarded_report(ACP_BACKEND_KIRO, custom_agent)

    await chat_runner._notice_unresolved_control_plane(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(mcp_session_report=lambda: report)
    )

    [(role, text, _cls)] = notices
    assert role == "notice"
    assert "@kirocrew-core" in text
