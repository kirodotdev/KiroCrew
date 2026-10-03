"""The crew captain: the one agent caller the creator fence does not bind.

A session running ``kirocrew-captain`` that the person opened themselves may
reach any session in its workspace, including conductors other sessions
created. Every way a captain could be minted or steered by something other than
the person keeps the fence: an agent-created captain, a cron slot, a channel
link or mirror, an app or ephemeral slot, and either switch turned off.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import (
    CAPTAIN_AGENT_FILENAME,
    CAPTAIN_AGENT_NAME,
    CONDUCTOR_AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
)
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import session_control as handlers
from kiro_crew.members import DM_SLOT_KEY_PREFIX

CAPTAIN = "chat-1-captain"


def _slot(
    key: str,
    *,
    agent_name: str = "",
    created_by: str = "",
    memory_store: str = "",
    linked: str = "",
    app: str = "",
    memory_mode: str = "persistent",
) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        agent=agent_name,
        workspace="default",
        memory_mode=memory_mode,
        _app=app,
        linked_session_key=linked,
        _created_by=created_by,
        memory_store=memory_store,
        mode="",
        running=False,
        messages=[],
    )


class _State:
    def __init__(self, *slots: SimpleNamespace):
        self._slots = {s.key: s for s in slots}

    def get_slot(self, key: str):
        return self._slots.get(key)


@pytest.fixture
def switches_on():
    with (
        patch.object(sc, "crew_captain_enabled", return_value=True),
        patch.object(sc, "session_control_enabled", return_value=True),
        patch.object(sc, "_has_channel_mirror", return_value=False),
    ):
        yield


# ── who is a captain ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("switches_on")
class TestCaptainCaller:
    def test_person_opened_captain_is_a_captain(self):
        state = _State(_slot(CAPTAIN, agent_name=CAPTAIN_AGENT_NAME))
        assert sc.captain_caller(state, CAPTAIN) is True

    def test_member_bound_captain_is_a_captain(self):
        # The case that motivated the feature: the person's lead is a crew
        # member, and members are otherwise always fenced.
        slot = _slot(CAPTAIN, agent_name=CAPTAIN_AGENT_NAME, memory_store="member-x-1")
        assert sc.captain_caller(_State(slot), CAPTAIN) is True

    @pytest.mark.parametrize(
        "overrides",
        [
            {"agent_name": "kirocrew-conductor"},
            {"agent_name": "kirocrew-worker"},
            {"created_by": "chat-9-worker"},
            {"linked": "slack:T1:C1"},
            {"app": "some-app"},
            {"memory_mode": "temporary"},
        ],
    )
    def test_every_other_shape_is_not(self, overrides):
        fields = {"agent_name": CAPTAIN_AGENT_NAME, **overrides}
        state = _State(_slot(CAPTAIN, **fields))
        assert sc.captain_caller(state, CAPTAIN) is False

    def test_cron_slot_running_the_captain_is_not(self):
        key = sc.CRON_SLOT_PREFIX + "job"
        state = _State(_slot(key, agent_name=CAPTAIN_AGENT_NAME))
        assert sc.captain_caller(state, key) is False

    def test_unknown_caller_is_not(self):
        assert sc.captain_caller(_State(), CAPTAIN) is False

    def test_member_whose_template_is_the_captain_is_a_captain(self):
        # A member slot stores the member's NAME; the template is in config.
        cfg = SimpleNamespace(
            agents={
                "ops-captain": SimpleNamespace(kiro_agent=CAPTAIN_AGENT_NAME),
                "ops-lead": SimpleNamespace(kiro_agent="kirocrew-lead"),
            }
        )
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            captain = _slot(CAPTAIN, agent_name="ops-captain", memory_store="member-o-1")
            lead = _slot("chat-8-lead", agent_name="ops-lead", memory_store="member-o-2")
            state = _State(captain, lead)
            assert sc.captain_caller(state, CAPTAIN) is True
            assert sc.captain_caller(state, "chat-8-lead") is False

    def test_member_named_like_the_captain_but_bound_elsewhere_is_not(self):
        # The config entry wins over the name: a member NAMED kirocrew-captain
        # that runs the worker template must not get the exemption.
        cfg = SimpleNamespace(
            agents={
                CAPTAIN_AGENT_NAME: SimpleNamespace(kiro_agent="kirocrew-worker", member_id="m-1"),
            }
        )
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            slot = _slot(CAPTAIN, agent_name=CAPTAIN_AGENT_NAME, memory_store="member-m-1")
            assert sc.captain_caller(_State(slot), CAPTAIN) is False

    def test_member_named_like_the_captain_with_no_template_is_not(self):
        cfg = SimpleNamespace(
            agents={CAPTAIN_AGENT_NAME: SimpleNamespace(kiro_agent="", member_id="m-1")}
        )
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            slot = _slot(CAPTAIN, agent_name=CAPTAIN_AGENT_NAME, memory_store="member-m-1")
            assert sc.captain_caller(_State(slot), CAPTAIN) is False

    def test_unreadable_config_is_not_a_captain_by_member_name(self):
        with patch.object(sc.KiroCrewConfig, "load", side_effect=RuntimeError("boom")):
            state = _State(_slot(CAPTAIN, agent_name="ops-captain"))
            assert sc.captain_caller(state, CAPTAIN) is False


class TestCaptainSwitches:
    def _state(self):
        return _State(_slot(CAPTAIN, agent_name=CAPTAIN_AGENT_NAME))

    @pytest.mark.parametrize("captain_on,control_on", [(False, True), (True, False)])
    def test_either_switch_off_withdraws_it(self, captain_on, control_on):
        with (
            patch.object(sc, "crew_captain_enabled", return_value=captain_on),
            patch.object(sc, "session_control_enabled", return_value=control_on),
            patch.object(sc, "_has_channel_mirror", return_value=False),
        ):
            assert sc.captain_caller(self._state(), CAPTAIN) is False

    def test_channel_mirror_withdraws_it(self):
        with (
            patch.object(sc, "crew_captain_enabled", return_value=True),
            patch.object(sc, "session_control_enabled", return_value=True),
            patch.object(sc, "_has_channel_mirror", return_value=True),
        ):
            assert sc.captain_caller(self._state(), CAPTAIN) is False

    def test_switch_fails_closed_on_config_error(self):
        with patch.object(sc.KiroCrewConfig, "load", side_effect=RuntimeError("boom")):
            assert sc.crew_captain_enabled() is False

    def test_switch_fails_closed_on_degraded_agent_section(self):
        cfg = SimpleNamespace(
            degraded_sections=frozenset({"agent"}), agent=SimpleNamespace(crew_captain=True)
        )
        with patch.object(sc.KiroCrewConfig, "load", return_value=cfg):
            assert sc.crew_captain_enabled() is False


# ── the fence ───────────────────────────────────────────────────────────────


def _authorize(state, caller_key, target_key, *, precomputed=None):
    with (
        patch.object(sc, "caller_slot_key", return_value=caller_key),
        patch.object(sc, "member_dispatch_enabled", return_value=True),
        patch.object(sc, "_resolve_slot", return_value=state._slots.get(target_key)),
    ):
        return sc.authorize_target(
            state,
            caller_session_key="dashboard:whatever",
            target=target_key,
            operation="send",
            precomputed_ownership_fenced=precomputed,
        )


def _passes_the_fence(fn) -> None:
    try:
        fn()
    except sc.SessionControlError as exc:
        # Later gates depend on deployment plumbing; the pin is the fence.
        assert exc.code != "not_creator", exc.code


@pytest.mark.usefixtures("switches_on")
class TestCaptainReach:
    def test_member_captain_reaches_a_conductor_another_session_created(self):
        captain = _slot(CAPTAIN, agent_name=CAPTAIN_AGENT_NAME, memory_store="member-x-1")
        conductor = _slot(
            "chat-2-cond", agent_name="kirocrew-conductor", created_by="chat-3-other-lead"
        )
        state = _State(captain, conductor)
        with patch.object(sc, "_store_is_member_owned", return_value=True):
            assert sc._caller_is_ownership_fenced(state, CAPTAIN) is False
            _passes_the_fence(lambda: _authorize(state, CAPTAIN, "chat-2-cond"))

    def test_member_non_captain_is_still_fenced(self):
        lead = _slot(CAPTAIN, agent_name="kirocrew-lead", memory_store="member-x-1")
        conductor = _slot("chat-2-cond", created_by="chat-3-other-lead")
        state = _State(lead, conductor)
        with patch.object(sc, "_store_is_member_owned", return_value=True):
            with pytest.raises(sc.SessionControlError) as exc_info:
                _authorize(state, CAPTAIN, "chat-2-cond")
        assert exc_info.value.code == "not_creator"

    def test_a_captain_made_by_an_agent_is_fenced(self):
        # A worker naming the captain agent in its own session_create must not
        # get an unfenced deputy.
        deputy = _slot("chat-4-deputy", agent_name=CAPTAIN_AGENT_NAME, created_by="chat-5-w")
        foreign = _slot("chat-6-user")
        state = _State(deputy, foreign)
        assert sc._caller_is_ownership_fenced(state, "chat-4-deputy") is True
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, "chat-4-deputy", "chat-6-user")
        assert exc_info.value.code == "not_creator"

    def test_a_captains_own_children_are_fenced(self):
        child = _slot("chat-7-child", agent_name=CAPTAIN_AGENT_NAME, created_by=CAPTAIN)
        state = _State(_slot(CAPTAIN, agent_name=CAPTAIN_AGENT_NAME), child)
        assert sc._caller_is_ownership_fenced(state, "chat-7-child") is True


class TestCarriedCaptainRevalidated:
    """A carried captain admission is re-read at every authorization.

    The HTTP gate decides captain status once per request. A verb that waits on
    a lock (``adopt_target``) re-authorizes later, and the switch may have been
    turned off in between: the carried ``False`` must not outlive it.
    """

    def _state(self, *, member: bool = True):
        store = "member-x-1" if member else ""
        captain = _slot(CAPTAIN, agent_name=CAPTAIN_AGENT_NAME, memory_store=store)
        conductor = _slot("chat-2-cond", created_by="chat-3-other-lead")
        return _State(captain, conductor)

    def _with(self, captain_on: bool):
        return (
            patch.object(sc, "crew_captain_enabled", return_value=captain_on),
            patch.object(sc, "session_control_enabled", return_value=True),
            patch.object(sc, "_has_channel_mirror", return_value=False),
            patch.object(sc, "_store_is_member_owned", return_value=True),
        )

    def test_carried_captain_still_on_passes(self):
        a, b, c, d = self._with(True)
        with a, b, c, d:
            state = self._state()
            _passes_the_fence(lambda: _authorize(state, CAPTAIN, "chat-2-cond", precomputed=False))

    def test_carried_captain_revoked_is_fenced(self):
        a, b, c, d = self._with(False)
        with a, b, c, d:
            state = self._state()
            with pytest.raises(sc.SessionControlError) as exc_info:
                _authorize(state, CAPTAIN, "chat-2-cond", precomputed=False)
        assert exc_info.value.code == "not_creator"

    def test_failed_reread_is_fenced(self):
        a, b, c, d = self._with(True)
        with a, b, c, d, patch.object(sc, "captain_caller", side_effect=RuntimeError("boom")):
            state = self._state()
            with pytest.raises(sc.SessionControlError) as exc_info:
                _authorize(state, CAPTAIN, "chat-2-cond", precomputed=False)
        assert exc_info.value.code == "not_creator"

    def test_plain_tab_captain_with_switch_off_stays_unfenced(self):
        # A person's own non-member tab was never fenced, so losing the
        # exemption changes nothing for it.
        a, b, c, d = self._with(False)
        with a, b, c, patch.object(sc, "_store_is_member_owned", return_value=False):
            state = self._state(member=False)
            _passes_the_fence(lambda: _authorize(state, CAPTAIN, "chat-2-cond", precomputed=False))


# ── the HTTP gate's carried verdict ─────────────────────────────────────────


class TestCarriedFence:
    def test_owner_caller_carries_nothing(self):
        assert handlers._carried_fence({}) is None

    def test_member_caller_carries_fenced(self):
        assert handlers._carried_fence({handlers._MEMBER_ADMITTED: True}) is True

    def test_member_captain_carries_unfenced(self):
        request = {handlers._MEMBER_ADMITTED: True, handlers._CAPTAIN_ADMITTED: True}
        assert handlers._carried_fence(request) is False

    def test_captain_mark_without_member_admission_carries_nothing(self):
        assert handlers._carried_fence({handlers._CAPTAIN_ADMITTED: True}) is None


# ── the spec ────────────────────────────────────────────────────────────────


def _stub_install(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: None)
    monkeypatch.setattr(
        agent,
        "build_agent_config",
        lambda: {
            "name": "kirocrew",
            "prompt": "file://x",
            "mcpServers": {
                "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]}
            },
            "tools": ["fs_write", "@kirocrew-core"],
            "allowedTools": ["@kirocrew-core"],
        },
    )
    monkeypatch.setattr(
        agent, "_kirocrew_mcp_invocation", lambda sub: ("/resolved/kirocrew", [sub])
    )
    monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: True)


def test_captain_spec_is_the_conductor_spec_with_its_own_name_and_prompt(tmp_path, monkeypatch):
    from kiro_crew.agent_materialization import conductor_agents

    _stub_install(tmp_path, monkeypatch)
    conductor_agents._install_conductor_agent()
    conductor_agents._install_captain_agent()
    live = json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))
    captain = json.loads((tmp_path / CAPTAIN_AGENT_FILENAME).read_text(encoding="utf-8"))

    assert captain["name"] == CAPTAIN_AGENT_NAME
    assert captain["prompt"].startswith("# Kiro Crew Captain")
    assert captain["prompt"].endswith(live["prompt"])
    # No grant the conductor lacks: the reach comes from the fence, not the spec.
    for key in set(live) | set(captain):
        if key not in ("name", "description", "prompt"):
            assert captain.get(key) == live.get(key), key
    assert "fs_write" not in captain["tools"]


def test_captain_spec_is_owned_and_installed_at_boot():
    import inspect

    assert CAPTAIN_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES
    assert "conductor_agents._install_captain_agent()" in inspect.getsource(agent)


def test_member_dm_prefix_is_not_a_captain_by_key(switches_on):
    # A member DM slot is a captain only by its agent, never by its key.
    key = DM_SLOT_KEY_PREFIX + "radar"
    assert sc.captain_caller(_State(_slot(key)), key) is False
