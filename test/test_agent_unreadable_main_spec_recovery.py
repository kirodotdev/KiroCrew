"""A refusal made after the episode closes says what replaces the refused start.

While an unreadable-main-spec episode is open, a refusal says the spec could not be
read and asks for read access. Once a rebuild closes the episode the spec reads, so
a start still refused was read before the episode ended: a process's pre-episode
view (the process is replaced), skill views prepared for a session (a new session
prepares them again) or a spec read by name (a new start reads it again). The
message and the SEL row both say which (``refuse_unprojected_main_spec_start``).

Also pinned: a healthy host refuses nothing on any path, a refused name other than
the main spec's own says it is resolved by name, and during an episode no read
error, file change or fresh launch admits a bare name.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _make_runtime

from kiro_crew.acp.runtime import AcpRuntimeError
from kiro_crew.acp.types import METHOD_SESSION_NEW

needs_real_chmod = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 000 does not deny the owner's read on this host",
)


def _renamed_main(tmp_path: Path, agent_mod) -> Path:
    """``kirocrew.json`` declares ``custom-main``; the last rebuild wrote it as ``kirocrew``."""
    agents = tmp_path / "kiro_agents"
    agents.mkdir()
    spec = agents / "kirocrew.json"
    spec.write_text(
        json.dumps(
            {
                "name": "custom-main",
                "tools": ["@user-server"],
                "allowedTools": ["@user-server"],
                "mcpServers": {"user-server": {"command": "/opt/user-server", "args": []}},
            }
        ),
        encoding="utf-8",
    )
    (agents / "helper.json").write_text(
        json.dumps({"name": "helper", "tools": []}), encoding="utf-8"
    )
    agent_mod._main_spec_declared_name = "kirocrew"
    agent_mod._main_spec_unreadable = True
    agent_mod._main_spec_episode_revision = 1
    return spec


def _agents_with_helper(tmp_path: Path) -> tuple[Path, Path]:
    agents = tmp_path / "kiro_agents"
    agents.mkdir()
    spec = agents / "kirocrew.json"
    spec.write_text(json.dumps({"name": "kirocrew", "tools": ["a"]}), encoding="utf-8")
    (agents / "helper.json").write_text(
        json.dumps({"name": "helper", "tools": []}), encoding="utf-8"
    )
    return agents, spec


@pytest.fixture
def audited(held_flag, monkeypatch):
    """Each denial as ``(agent, kind)``; the early retry and the runtime's kill stubbed."""
    out: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        held_flag,
        "_audit_main_spec_denial",
        lambda agent, *, superseded=None: out.append((agent, superseded)),
    )
    monkeypatch.setattr(held_flag, "_schedule_main_spec_early_retry", lambda: None)
    from kiro_crew.acp.runtime import AcpRuntime

    monkeypatch.setattr(AcpRuntime, "kill", AsyncMock())
    return out


# ── After recovery: the message and the SEL row name what replaces the start ──


@pytest.mark.parametrize(
    ("kind", "cause", "remedy"),
    [
        ("process", "unreadable_main_spec_superseded_view", "start the agent again"),
        ("session", "unreadable_main_spec_superseded_view", "start a new session"),
        ("read", "unreadable_main_spec_superseded_read", "start the agent again"),
    ],
)
def test_the_sel_row_of_a_refusal_after_recovery_names_its_cause_and_remedy(
    held_flag, monkeypatch, kind, cause, remedy
):
    """With no episode open, each refusal kind writes its own cause and remedy.

    The episode's row ("the last rebuild could not read the main agent spec;
    remedy: restore read access") is what an operator triages from, and after
    recovery the spec reads and was rebuilt.
    """
    monkeypatch.setattr(held_flag, "_schedule_main_spec_early_retry", lambda: None)
    held_flag._main_spec_episode_revision = 2
    log = MagicMock()
    with patch("kiro_crew.agent.sel", return_value=log):
        message = held_flag.refuse_unprojected_main_spec_start("kirocrew", superseded=kind)
    (call,) = log.log_governance_decision.call_args_list
    reason = call.kwargs["reason"]
    assert reason.startswith(f"cause={cause};"), reason
    assert reason.endswith(f"remedy: {remedy}"), reason
    assert "restore read access" not in reason and "could not read" not in reason
    assert "Restore read access" not in message and "could not read" not in message


@pytest.mark.parametrize("kind", ["process", "session", "read"])
def test_a_refusal_while_an_episode_is_open_keeps_the_episode_cause(held_flag, monkeypatch, kind):
    """The kind changes nothing while the episode is open: the spec still cannot be read."""
    monkeypatch.setattr(held_flag, "_schedule_main_spec_early_retry", lambda: None)
    held_flag._main_spec_unreadable = True
    held_flag._main_spec_episode_revision = 1
    log = MagicMock()
    with patch("kiro_crew.agent.sel", return_value=log):
        message = held_flag.refuse_unprojected_main_spec_start("kirocrew", superseded=kind)
    (call,) = log.log_governance_decision.call_args_list
    assert call.kwargs["reason"].startswith("cause=unreadable_main_spec;")
    assert "Restore read access" in message


def test_the_sel_row_of_a_view_refused_after_recovery_does_not_ask_to_restore_read_access(
    held_flag, monkeypatch
):
    """A view refused after recovery says in its SEL row what its message says."""
    monkeypatch.setattr(held_flag, "_schedule_main_spec_early_retry", lambda: None)
    held_flag._main_spec_unreadable = False
    held_flag._main_spec_episode_revision = 2
    log = MagicMock()
    with patch("kiro_crew.agent.sel", return_value=log):
        with pytest.raises(held_flag.ForkGovernanceUnresolved) as refused:
            held_flag.require_main_spec_launch_bound(
                "kirocrew",
                before=(1, False),
                view_source="/agents/kirocrew.json",
            )
    assert "being replaced" in str(refused.value)
    (call,) = log.log_governance_decision.call_args_list
    reason = call.kwargs["reason"]
    assert "restore read access" not in reason, reason
    assert "could not read" not in reason, reason


@pytest.mark.asyncio
async def test_a_kas_open_refused_after_recovery_does_not_ask_to_restore_read_access(
    held_flag, audited, tmp_path
):
    """The KAS payload re-check after an episode opened and closed: a new start reads again.

    ``_require_payload_built_on_current_episode`` refuses a payload built before
    an episode that began in between, also once a rebuild has closed it. The spec
    reads now, so the message says so and the remedy is a new start.
    """
    rt, _, _proc = _make_runtime()
    rt._agent = "helper"
    rt._work_dir = str(tmp_path)
    sent = AsyncMock(return_value={"sessionId": "s-1"})
    rt._send_and_await = sent
    built_on = held_flag.main_spec_episode()  # (0, False): built healthy
    held_flag._main_spec_episode_revision += 1  # an episode opened and a rebuild closed it
    with pytest.raises(AcpRuntimeError, match="may not start") as refused:
        await rt._send_session_open(
            METHOD_SESSION_NEW,
            {"cwd": str(tmp_path)},
            timeout=5.0,
            built_on=built_on,
            built_for="helper",
        )
    sent.assert_not_awaited()
    assert audited == [("helper", "read")]
    message = str(refused.value)
    assert "Restore read access" not in message, message
    assert "could not read" not in message, message
    assert "Start the agent again" in message


@pytest.mark.asyncio
async def test_a_switch_refused_after_recovery_does_not_promise_a_replacement(
    held_flag, audited, tmp_path
):
    """A mode switch recycles nothing, so its refusal after recovery names a new session.

    ``check_mode_frame`` takes no runtime and ``AcpSessionHandle.set_mode``
    re-prepares no projection: the same switch is refused until a session start
    prepares the projection again (the start bracket does).
    """
    from kiro_crew.acp.session_handle import check_mode_frame
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    projection = NativeSkillProjection(
        aliases={"kirocrew": "kirocrew-view-0000"},
        sources={"kirocrew": (tmp_path / "kirocrew.json").as_posix()},
        episodes={"kirocrew": held_flag.main_spec_episode()},  # built healthy
    )
    held_flag._main_spec_episode_revision += 1  # an episode opened and a rebuild closed it
    with pytest.raises(AcpRuntimeError, match="may not start") as refused:
        await check_mode_frame("kirocrew", str(tmp_path), projection, "kirocrew-view-0000")
    assert audited == [("kirocrew", "session")]
    message = str(refused.value)
    assert "being replaced" not in message, message
    assert "Start a new session" in message


def test_a_projection_refusal_after_recovery_tells_the_user_to_start_a_new_session(
    held_flag, audited
):
    """The projection's own refusal of a view it prepared during the episode, after recovery.

    The view of the main spec was refused at preparation, during the episode. A
    switch or spawn that asks for it once a rebuild has closed the episode is
    still refused by that projection, so the message names the next session start
    that prepares the views again, not the read access the episode needed.
    """
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    projection = NativeSkillProjection(
        aliases={},
        errors={"kirocrew": "read during an unreadable-main-spec episode"},
        main_spec_refused={"kirocrew"},
    )
    held_flag._main_spec_unreadable = True
    held_flag._main_spec_episode_revision = 1
    with pytest.raises(ValueError, match="Restore read access"):
        projection.agent("kirocrew")
    held_flag._main_spec_unreadable = False  # a rebuild closed the episode
    with pytest.raises(ValueError) as refused:
        projection.agent("kirocrew")
    message = str(refused.value)
    assert "Start a new session" in message and "could not read" not in message
    assert audited == [("kirocrew", None), ("kirocrew", "session")]


# ── While the episode is open: why another agent's name is refused ────────────


def test_an_episode_refusal_of_another_agent_says_it_is_resolved_by_name(held_flag, audited):
    """Users of unrelated agents hit the refusal, so it says why they are refused.

    A bare name other than the main spec's own is refused because it is resolved
    by name and the unreadable spec may declare it; the main agent's own refusal
    needs no such clause.
    """
    held_flag._main_spec_unreadable = True
    held_flag._main_spec_episode_revision = 1
    held_flag._main_spec_declared_name = "kirocrew"
    with pytest.raises(held_flag.ForkGovernanceUnresolved) as other:
        held_flag.require_main_spec_launch_bound("helper")
    message = str(other.value)
    assert "'helper' is resolved by name when it starts, and that spec may declare it" in message
    assert "kirocrew.json" in message and "Restore read access" in message
    with pytest.raises(held_flag.ForkGovernanceUnresolved) as own:
        held_flag.require_main_spec_launch_bound("kirocrew")
    assert "resolved by name" not in str(own.value)
    assert audited == [("helper", None), ("kirocrew", None)]


def test_a_view_refused_during_an_episode_is_not_told_it_is_resolved_by_name(held_flag, audited):
    """A view is pinned to the file it was built from, so the by-name clause is not its reason.

    A project ``helper.json`` linked to ``kirocrew.json`` gives ``helper`` a view of
    the main spec. The view and the projection's own refusal of it say the spec
    could not be read, without the by-name sentence a bare name gets.
    """
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    held_flag._main_spec_unreadable = True
    held_flag._main_spec_episode_revision = 1
    with pytest.raises(held_flag.ForkGovernanceUnresolved) as view:
        held_flag.require_main_spec_launch_bound(
            "helper",
            before=(0, False),
            view_source="/project/.kiro/agents/helper.json",
        )
    assert "resolved by name" not in str(view.value)
    assert "Restore read access" in str(view.value)
    projection = NativeSkillProjection(
        aliases={},
        errors={"helper": "read during an unreadable-main-spec episode"},
        main_spec_refused={"helper"},
    )
    with pytest.raises(ValueError) as projected:
        projection.agent("helper")
    assert "resolved by name" not in str(projected.value)
    assert audited == [("helper", None), ("helper", None)]


# ── Pins that hold: the healthy host and the path after recovery ──────────────


def _bound_calls(agent_mod, tmp_path: Path, *, before):
    """Every argument shape a production caller hands the launch bound, one per path."""
    bound = agent_mod.require_main_spec_launch_bound
    other = (tmp_path / "helper.json").as_posix()
    main = (tmp_path / "kirocrew.json").as_posix()
    return {
        # _spawn_admitted / client_start._spawn / an unbound open / a bare-name switch,
        # and an app launch copy, which is checked the same way (no episode reading)
        "bare": lambda: bound("helper", str(tmp_path)),
        "launch-copy": lambda: bound("reviewer", str(tmp_path)),
        # a view launch's open / the direct client's frame / a view switch
        "view-other": lambda: bound("helper", str(tmp_path), before=before, view_source=other),
        "view-main": lambda: bound("kirocrew", str(tmp_path), before=before, view_source=main),
        # KasHarness.session_extras and the open's payload re-check
        "kas": lambda: bound("helper", str(tmp_path), before=before),
    }


def test_a_healthy_host_refuses_nothing_on_any_path(tmp_path, held_flag, audited):
    """No episode ever: every argument shape a caller passes is admitted, with no denial."""
    calls = _bound_calls(held_flag, tmp_path, before=held_flag.main_spec_episode())
    for name, call in calls.items():
        call()  # raises on a refusal
        assert audited == [], name


def test_after_recovery_only_a_pre_episode_view_is_refused(tmp_path, held_flag, audited):
    """An episode opened and closed after the readings: only the views refuse, both of them.

    A bare name and a launch copy carry no reading, and a running process keeps
    its view for life, whatever file it was read from, so a view built before
    the episode is refused, and its message names the process.
    """
    built = held_flag.main_spec_episode()
    held_flag._main_spec_episode_revision += 1  # a skip, then a rebuild closed it
    calls = _bound_calls(held_flag, tmp_path, before=built)
    for name in ("bare", "launch-copy"):
        calls[name]()
    assert audited == []
    for name, agent in (("view-main", "kirocrew"), ("view-other", "helper")):
        with pytest.raises(held_flag.ForkGovernanceUnresolved) as refused:
            calls[name]()
        assert "being replaced" in str(refused.value)
    assert audited == [("kirocrew", "process"), ("helper", "process")]


def test_every_start_is_refused_while_an_episode_is_open(tmp_path, held_flag, audited):
    """While an episode is open no argument shape a caller passes is admitted.

    Mutation pin: admitting a view of another file, a launch copy or a bare name
    during the episode writes one denial fewer here.
    """
    built = held_flag.main_spec_episode()
    held_flag._main_spec_unreadable = True
    held_flag._main_spec_episode_revision += 1
    calls = _bound_calls(held_flag, tmp_path, before=built)
    for name, call in calls.items():
        with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
            call()
    assert [agent for agent, _kind in audited] == [
        "helper",
        "reviewer",
        "helper",
        "kirocrew",
        "helper",
    ]


# ── During an episode nothing admits a bare name ──────────────────────────────


@needs_real_chmod
@pytest.mark.asyncio
async def test_a_warm_unbound_runtime_of_a_renamed_main_is_refused_with_no_probe(
    tmp_path, held_flag, audited
):
    """A warm unbound runtime of a renamed main agent is refused while the spec is unreadable."""
    spec = _renamed_main(tmp_path, held_flag)
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent):
        spec.chmod(0)
        try:
            with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
                held_flag.require_main_spec_projected("custom-main")
            rt, _, _proc = _make_runtime()
            rt._agent = "custom-main"
            rt._launch_unbound = True
            rt._work_dir = str(tmp_path)
            sent = AsyncMock(return_value={"sessionId": "s-1"})
            rt._send_and_await = sent
            with pytest.raises(AcpRuntimeError, match="may not start"):
                await rt._send_session_open(
                    METHOD_SESSION_NEW, {"cwd": str(tmp_path), "mcpServers": []}, timeout=5.0
                )
            sent.assert_not_awaited()
        finally:
            spec.chmod(0o600)
    assert audited == [("custom-main", None), ("custom-main", None)]


@pytest.mark.parametrize("err", [errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EINTR])
def test_no_read_error_on_the_main_spec_admits_a_bare_name(tmp_path, held_flag, audited, err):
    """No read error on the main spec admits a bare name: nothing probes its readability."""
    agents, spec = _agents_with_helper(tmp_path)
    held_flag._main_spec_unreadable = True
    held_flag._main_spec_episode_revision = 1
    real_open = os.open

    def _open(path, flags, *a, **k):
        if Path(path) == spec:
            raise OSError(err, os.strerror(err), str(path))
        return real_open(path, flags, *a, **k)

    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents), patch("os.open", _open):
        with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
            held_flag.require_main_spec_launch_bound("helper")
    assert audited == [("helper", None)]


@needs_real_chmod
@pytest.mark.parametrize(
    "change", ["replaced-same-bytes", "chmod-round-trip", "edited-mtime-restored"]
)
def test_a_bare_name_is_refused_before_and_after_any_change_to_the_kept_file(
    tmp_path, held_flag, audited, change
):
    """A bare name stays refused when the kept file is replaced, re-permissioned or edited."""
    agents, spec = _agents_with_helper(tmp_path)
    body = spec.read_text(encoding="utf-8")
    held_flag._main_spec_unreadable = True
    held_flag._main_spec_episode_revision = 1
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents):
        spec.chmod(0)
        try:
            with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
                held_flag.require_main_spec_launch_bound("helper", str(tmp_path))
            if change == "replaced-same-bytes":
                tmp = agents / "kirocrew.json.tmp"
                tmp.write_text(body, encoding="utf-8")
                tmp.chmod(0)
                os.replace(tmp, spec)
            elif change == "chmod-round-trip":
                spec.chmod(0o600)
                spec.chmod(0)
            else:
                info = os.stat(spec)
                spec.chmod(0o600)
                spec.write_text(body.replace('"a"', '"b"'), encoding="utf-8")
                os.utime(spec, ns=(info.st_atime_ns, info.st_mtime_ns))
                spec.chmod(0)
            with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
                held_flag.require_main_spec_launch_bound("helper", str(tmp_path))
        finally:
            spec.chmod(0o600)
    assert audited == [("helper", None), ("helper", None)]


@needs_real_chmod
def test_a_fresh_unbound_launch_is_refused_too_so_no_recycle_is_owed(tmp_path, held_flag, audited):
    """A fresh unbound launch is refused like the warm one, so no recycle is owed.

    ``_spawn_admitted`` and ``client_start._spawn`` call the bound with no reading,
    so relaunching the same name during the episode is refused too, and keeping
    the warm process changes nothing.
    """
    agents, spec = _agents_with_helper(tmp_path)
    held_flag._main_spec_unreadable = True
    held_flag._main_spec_episode_revision = 1
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents):
        spec.chmod(0)
        try:
            with pytest.raises(held_flag.ForkGovernanceUnresolved, match="may not start"):
                held_flag.require_main_spec_launch_bound("helper", str(tmp_path))
        finally:
            spec.chmod(0o600)
    assert audited == [("helper", None)]
