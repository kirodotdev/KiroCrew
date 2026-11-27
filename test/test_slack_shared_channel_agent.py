"""``!channel agent <Bot>|off`` from a confirmed shared user (``slack.shared_access``).

``!channel`` is the owner's. A shared user gets one part of it: putting a channel on
a Crew Bot, or taking it off one, when the channel has no agent or a shared user set
the one it has. The owner's agent, a kiro agent that is no Bot, a DM, a code channel,
a channel with a linked dashboard chat and every other ``!channel`` word refuse them.
The mark that says a shared user set the agent (``shared_agent``) is saved with it.

Config writes land in the per-test ``KIROCREW_HOME`` that ``conftest.py`` pins.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from slack_transport_dispatch_helpers import (
    _MSG_TS,
    FakeSessions,
    RecordingSlackClient,
    ScriptedProvider,
    make_event,
)

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
from kiro_crew.config import live, loader
from kiro_crew.config.loader import KiroCrewConfig, config_path, update_config_locked
from kiro_crew.config.sections import (
    ChannelConfig,
    KiroCrewAgentConfig,
    WorkspaceConfig,
    drop_stale_shared_agents,
)
from kiro_crew.session import SessionManager
from kiro_crew.session_map import SessionMap
from kiro_crew.slack import handler as _handler
from kiro_crew.slack import shared_channel_agent as sca
from kiro_crew.slack import transport_dispatch

_OWNER = "U_OWNER"
_SHARED = "U_SHARED"
_OTHER_SHARED = "U_SHARED2"
_STRANGER = "U_STRANGER"
_CHANNEL = "C1"


def _disk() -> dict:
    """``slack.channels.C1`` as ``config.json`` holds it (``{}`` when absent)."""
    path = config_path()
    if not path.exists():
        return {}
    channels = json.loads(path.read_text(encoding="utf-8")).get("slack", {}).get("channels", {})
    return channels.get(_CHANNEL, {})


def _restart() -> ChannelConfig:
    """The channel as a gateway that has just started reads it."""
    loader._invalidate_config_cache()
    return KiroCrewConfig.load().channel_config(_CHANNEL)


@pytest.fixture()
def crew(monkeypatch, tmp_path):
    """Two Crew Bots, two confirmed shared users, an owner, and an audit recorder."""
    loader._invalidate_config_cache()
    for name in ("web-frontend", "research"):
        (tmp_path / name).mkdir()
    bots = {
        "web-frontend": KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="web-frontend"),
        "research": KiroCrewAgentConfig(
            kiro_agent="kirocrew-research", workspace="research", display_name="Research Bot"
        ),
    }

    def _live() -> KiroCrewConfig:
        # What a Slack turn reads: the Bots, and the channels as the last write left them.
        cfg = KiroCrewConfig.load()
        cfg.agents.update(bots)
        cfg.workspaces.update({n: WorkspaceConfig(dir=str(tmp_path / n)) for n in bots})
        return cfg

    monkeypatch.setattr(live, "snapshot", _live)
    monkeypatch.setattr(_handler, "_orch_cfg", None)
    monkeypatch.setattr(_handler, "_owner_id", _OWNER)
    monkeypatch.setattr(_handler, "is_shared_user", lambda user: user in (_SHARED, _OTHER_SHARED))
    monkeypatch.setattr(_handler, "get_dashboard_state", lambda: None)
    monkeypatch.setattr(sca, "_is_code_channel", lambda channel: False)
    recorder = MagicMock()
    monkeypatch.setattr(_handler, "sel", lambda: recorder)
    return recorder


def _say(text: str, user: str = _SHARED, channel: str = _CHANNEL) -> str:
    return asyncio.run(sca.handle(text, channel, user))


def _audits(recorder) -> list[tuple[str, str, str, str]]:
    return [
        (kw["caller"], kw["outcome"], kw["resources"], kw.get("error", ""))
        for _args, kw in recorder.log_api_access.call_args_list
        if kw["operation"] == "slack.channel_agent"
    ]


def _owner_sets(agent: str) -> None:
    _handler._persist_channel_config(_CHANNEL, agent=agent)


def _raw_edit(change) -> None:
    """Change ``config.json`` the way every locked raw writer does."""

    def _apply(doc: dict) -> dict:
        change(doc)
        return doc

    update_config_locked(config_path(), mutate=_apply)


def _hand_edit(change) -> None:
    """Write the file directly, without the config writer's stale-mark cleanup."""
    path = config_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")
    loader._invalidate_config_cache()


def _after_the_next_write(monkeypatch, then) -> None:
    """Run *then* once, as soon as the next config write has landed."""
    real = _handler.run_config_write
    pending = [then]

    async def _write(fn, /, *args, **kwargs):
        result = await real(fn, *args, **kwargs)
        if pending:
            pending.pop()()
        return result

    monkeypatch.setattr(_handler, "run_config_write", _write)


class TestASharedUserSetsABot:
    def test_a_channel_with_no_agent_takes_the_bot(self, crew):
        reply = _say("!channel agent research")

        assert reply.startswith("Agent for this channel: *research*.")
        assert "now answers new threads here, for everyone" in reply
        assert _disk() == {"agent": "research", "shared_agent": "research"}
        assert _audits(crew) == [(_SHARED, "allowed", "C1:research", "")]

    def test_the_saved_channels_are_loaded_when_put_in_force(self, crew, monkeypatch):
        reload_cfg = MagicMock(wraps=_handler._reload_orch_cfg)
        monkeypatch.setattr(_handler, "_reload_orch_cfg", reload_cfg)

        assert _say("!channel agent research").startswith("Agent for this channel: *research*.")
        assert _disk() == {"agent": "research", "shared_agent": "research"}
        # No snapshot is handed over: the reload reads the file at the moment it applies.
        reload_cfg.assert_called_once_with()

    def test_a_display_name_or_other_case_names_the_bot(self, crew):
        assert "*research*" in _say("!channel agent Research Bot")
        assert _disk()["agent"] == "research"
        assert "*web-frontend*" in _say("!channel agent WEB-FRONTEND")
        assert _disk()["agent"] == "web-frontend"

    def test_another_shared_user_may_replace_it(self, crew):
        _say("!channel agent research")
        reply = _say("!channel agent web-frontend", user=_OTHER_SHARED)

        assert "*web-frontend*" in reply
        assert _disk() == {"agent": "web-frontend", "shared_agent": "web-frontend"}
        assert _audits(crew)[-1] == (_OTHER_SHARED, "allowed", "C1:web-frontend", "")

    def test_off_takes_a_shared_users_bot_off(self, crew):
        _say("!channel agent research")
        reply = _say("!channel agent off", user=_OTHER_SHARED)

        assert reply == sca.CLEARED_TEXT
        assert _disk() == {"agent": ""}
        assert _audits(crew)[-1] == (_OTHER_SHARED, "allowed", "C1:default", "")

    def test_off_with_no_agent_writes_nothing(self, crew):
        assert _say("!channel agent off") == sca.NO_AGENT_TEXT
        assert not config_path().exists() or _disk() == {}
        assert _audits(crew) == [(_SHARED, "allowed", "C1:default", "no agent to clear")]

    def test_only_the_two_agent_keys_are_written(self, crew):
        _handler._persist_channel_config(_CHANNEL, activation="always")
        _say("!channel agent research")

        assert _disk() == {
            "activation": "always",
            "agent": "research",
            "shared_agent": "research",
        }


class TestTheOwnersAgentStaysTheOwners:
    @pytest.mark.parametrize("agent", ["research", "kirocrew-lite"])
    @pytest.mark.parametrize("command", ["!channel agent web-frontend", "!channel agent off"])
    def test_a_shared_user_cannot_replace_or_clear_it(self, crew, agent, command):
        _owner_sets(agent)

        assert _say(command) == sca.OWNER_AGENT_TEXT
        assert _disk() == {"agent": agent}
        assert _audits(crew) == [(_SHARED, "denied", "C1", "owner's agent")]

    def test_the_owner_naming_the_same_bot_takes_it_over(self, crew):
        _say("!channel agent research")
        _owner_sets("research")

        assert _disk() == {"agent": "research"}
        assert _say("!channel agent off") == sca.OWNER_AGENT_TEXT

    def test_an_agent_changed_by_another_writer_is_the_owners(self, crew):
        """A dashboard or hand edit of ``agent`` leaves the old mark behind: it is stale."""
        _say("!channel agent research")
        doc = json.loads(config_path().read_text(encoding="utf-8"))
        doc["slack"]["channels"][_CHANNEL]["agent"] = "web-frontend"
        config_path().write_text(json.dumps(doc), encoding="utf-8")
        loader._invalidate_config_cache()

        assert _restart().shared_agent == ""
        assert _say("!channel agent off") == sca.OWNER_AGENT_TEXT
        assert _disk()["agent"] == "web-frontend"

    def test_the_write_itself_refuses_an_owner_agent_that_landed_after_the_check(self, crew):
        """The rule is asked again inside the locked write, on the file as it is then."""
        _owner_sets("research")

        with pytest.raises(sca.ChannelAgentOwned):
            sca.save(_CHANNEL, "web-frontend")
        with pytest.raises(sca.ChannelAgentOwned):
            sca.save(_CHANNEL, "")
        assert _disk() == {"agent": "research"}

    def test_the_write_reads_the_agent_as_written_not_trimmed(self, crew):
        """The owner's ``"research "`` beside an older mark is not the marked agent."""
        _say("!channel agent research")
        doc = json.loads(config_path().read_text(encoding="utf-8"))
        doc["slack"]["channels"][_CHANNEL]["agent"] = "research "
        config_path().write_text(json.dumps(doc), encoding="utf-8")

        with pytest.raises(sca.ChannelAgentOwned):
            sca.save(_CHANNEL, "")
        assert _disk()["agent"] == "research "

    @pytest.mark.parametrize(
        "slack",
        [["x"], {"channels": "x"}, {"channels": {_CHANNEL: 7}}],
        ids=["slack", "channels", "channel"],
    )
    def test_the_write_leaves_a_wrong_shaped_entry_as_written(self, crew, slack):
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps({"slack": slack}), encoding="utf-8")
        loader._invalidate_config_cache()

        with pytest.raises(ValueError, match="not an object"):
            sca.save(_CHANNEL, "research")
        assert json.loads(config_path().read_text(encoding="utf-8"))["slack"] == slack

    def test_a_handler_that_read_no_agent_is_still_refused_by_the_write(self, crew, monkeypatch):
        _owner_sets("research")
        monkeypatch.setattr(sca, "_pin", lambda handler, channel: ("", False))

        assert _say("!channel agent web-frontend") == sca.OWNER_AGENT_TEXT
        assert _disk() == {"agent": "research"}

    def test_an_agent_that_cannot_be_read_is_the_owners(self, crew, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(live, "snapshot", _boom)
        assert _say("!channel agent research") == sca.OWNER_AGENT_TEXT
        assert _disk() == {}

    def test_an_agent_in_the_local_overlay_is_the_owners(self, crew):
        """``config.local.json`` wins over ``config.json``: an agent there is the owner's."""
        local = config_path().with_name("config.local.json")
        local.write_text(
            json.dumps({"slack": {"channels": {_CHANNEL: {"agent": "research"}}}}),
            encoding="utf-8",
        )
        loader._invalidate_config_cache()

        assert _say("!channel agent web-frontend") == sca.OWNER_AGENT_TEXT
        assert _disk() == {}

    def test_an_empty_agent_in_the_local_overlay_is_the_owners_too(self, crew):
        """The write lands in ``config.json`` and changes nothing in force: it is taken back."""
        local = config_path().with_name("config.local.json")
        local.write_text(
            json.dumps({"slack": {"channels": {_CHANNEL: {"agent": ""}}}}), encoding="utf-8"
        )
        loader._invalidate_config_cache()

        assert _say("!channel agent research") == sca.OWNER_AGENT_TEXT
        assert _disk() == {"agent": ""}
        assert _audits(crew) == [(_SHARED, "denied", "C1:research", "owner's agent")]

    def test_the_owners_write_right_after_is_kept(self, crew, monkeypatch):
        """Taking a refused write back never undoes what the owner wrote in between."""
        _after_the_next_write(monkeypatch, lambda: _owner_sets("kirocrew-lite"))

        assert _say("!channel agent research") == sca.OWNER_AGENT_TEXT
        assert _disk() == {"agent": "kirocrew-lite"}

    def test_the_owner_taking_the_same_bot_right_after_is_not_reported_as_a_success(
        self, crew, monkeypatch
    ):
        _after_the_next_write(monkeypatch, lambda: _owner_sets("research"))

        assert _say("!channel agent research") == sca.OWNER_AGENT_TEXT
        assert _disk() == {"agent": "research"}
        assert _audits(crew) == [(_SHARED, "denied", "C1:research", "owner's agent")]

    def test_the_owners_clear_right_after_is_kept(self, crew, monkeypatch):
        """The channel was on a shared user's Bot: taking back must not bring it back."""
        _say("!channel agent web-frontend")
        _after_the_next_write(monkeypatch, lambda: _owner_sets(""))

        assert _say("!channel agent research") == sca.OWNER_AGENT_TEXT
        assert _disk() == {"agent": ""}

    @pytest.mark.parametrize(
        "on_disk, saved",
        [
            ({"agent": "research", "shared_agent": "research"}, True),
            ({"agent": "research"}, False),
            ({"agent": "kirocrew-lite"}, False),
            ({"agent": ""}, False),
            ({}, False),
        ],
        ids=["the-refused-write", "owner-took-it-over", "owner-set", "owner-cleared", "gone"],
    )
    def test_taking_back_saves_only_over_the_write_it_takes_back(self, crew, on_disk, saved):
        _handler._persist_channel_config(_CHANNEL, activation="always")
        _raw_edit(lambda doc: doc["slack"]["channels"].__setitem__(_CHANNEL, dict(on_disk)))

        assert sca.save(_CHANNEL, "web-frontend", undo_of="research") == (
            "research" if saved else ""
        )
        assert _disk() == (
            {"agent": "web-frontend", "shared_agent": "web-frontend"} if saved else on_disk
        )

    @pytest.mark.parametrize("owners", ["kirocrew-lite", ""], ids=["owner-set", "owner-cleared"])
    def test_a_clear_is_never_taken_back(self, crew, owners):
        """An empty agent cannot be told from the owner's own clear."""
        _owner_sets(owners)
        sca.save(_CHANNEL, "research", undo_of="")
        assert _disk() == {"agent": owners}

    def test_a_refused_clear_is_left_as_saved(self, crew):
        """The owner's overlay names the Bot a shared user set: ``off`` changes nothing in force."""
        _say("!channel agent research")
        local = config_path().with_name("config.local.json")
        local.write_text(
            json.dumps({"slack": {"channels": {_CHANNEL: {"agent": "research"}}}}),
            encoding="utf-8",
        )
        loader._invalidate_config_cache()

        assert _say("!channel agent off") == sca.OWNER_AGENT_TEXT
        assert _disk() == {"agent": ""}
        assert _restart().agent == "research"

    @pytest.mark.parametrize("agent", [["owner"], 7, {"name": "x"}, True], ids=repr)
    def test_an_agent_that_is_not_a_string_is_not_overwritten(self, crew, agent):
        _handler._persist_channel_config(_CHANNEL, activation="always")
        _raw_edit(lambda doc: doc["slack"]["channels"][_CHANNEL].update(agent=agent))

        with pytest.raises(sca.ChannelAgentOwned):
            sca.save(_CHANNEL, "research")
        assert _say("!channel agent research") == sca.OWNER_AGENT_TEXT
        assert _disk()["agent"] == agent

    @pytest.mark.parametrize("back", ["research", "web-frontend"])
    def test_a_raw_edit_of_the_agent_deletes_the_mark_for_good(self, crew, back):
        """``kirocrew config set`` and the dashboard editor write through the locked edit."""
        _say("!channel agent research")
        for agent in ("web-frontend", back):
            _raw_edit(
                lambda doc, agent=agent: doc["slack"]["channels"][_CHANNEL].update(agent=agent)
            )

        assert _disk() == {"agent": back}
        assert _say("!channel agent off") == sca.OWNER_AGENT_TEXT

    def test_a_raw_edit_elsewhere_keeps_the_mark(self, crew):
        _say("!channel agent research")
        _raw_edit(lambda doc: doc["slack"]["channels"][_CHANNEL].update(activation="always"))

        assert _disk() == {
            "activation": "always",
            "agent": "research",
            "shared_agent": "research",
        }

    @pytest.mark.parametrize(
        "doc",
        [{}, {"slack": 7}, {"slack": {"channels": ["C1"]}}, {"slack": {"channels": {"C1": "x"}}}],
        ids=["no-slack", "slack-not-object", "channels-not-object", "channel-not-object"],
    )
    def test_a_wrong_shaped_document_is_left_as_written(self, doc):
        before = json.dumps(doc)
        drop_stale_shared_agents(doc)
        assert json.dumps(doc) == before


class TestOnlyACrewBot:
    @pytest.mark.parametrize("typed", ["kirocrew", "kirocrew-research", "kirocrew-lite", "nope"])
    def test_a_kiro_agent_or_an_unknown_name_is_refused(self, crew, typed, monkeypatch):
        # Every name resolves as a kiro agent for the owner: a shared user still gets none.
        monkeypatch.setattr(_handler, "_resolve_agent_name", lambda name, project=None: name)
        reply = _say(f"!channel agent {typed}")

        assert reply.startswith(f"❌ Unknown Bot `{typed}`. Crew Bots: ")
        assert "`research` (Research Bot)" in reply and "`default`" not in reply
        assert reply.endswith("Only the bot owner can pick another kind of agent.")
        assert _disk() == {}
        assert _audits(crew) == [(_SHARED, "denied", "C1", "not a Crew Bot")]

    def test_a_bot_whose_record_starts_no_session_is_refused(self, crew, monkeypatch):
        monkeypatch.setattr(_handler, "resolve_channel_crew", lambda name: None)
        assert _say("!channel agent research").startswith("❌ Unknown Bot `research`.")
        assert _disk() == {}

    def test_no_name_is_the_usage_line(self, crew):
        assert _say("!channel agent") == sca.USAGE_TEXT
        assert _disk() == {}


class TestWhereItIsRefused:
    @pytest.mark.parametrize("channel", ["D1", "dm", ""])
    def test_a_dm(self, crew, channel):
        assert _say("!channel agent research", channel=channel) == sca.DM_TEXT
        assert not config_path().exists()
        assert _audits(crew) == [(_SHARED, "denied", channel, "dm")]

    def test_a_code_channel(self, crew, monkeypatch):
        monkeypatch.setattr(sca, "_is_code_channel", lambda channel: True)
        for command in ("!channel agent research", "!channel agent off"):
            assert _say(command) == sca.CODE_CHANNEL_TEXT
        assert _disk() == {}
        assert _audits(crew) == [(_SHARED, "denied", "C1", "code channel")] * 2

    def test_a_code_channel_store_that_cannot_answer_counts_as_one(self, monkeypatch):
        from kiro_crew.slack.code_channels import store as store_mod

        def _boom():
            raise RuntimeError("store unreadable")

        monkeypatch.setattr(store_mod, "store", _boom)
        assert sca._is_code_channel(_CHANNEL) is True

    def test_a_bound_code_channel_is_one(self, monkeypatch):
        from kiro_crew.slack.code_channels import store as store_mod

        codes = SimpleNamespace(
            get_binding=lambda channel: object() if channel == _CHANNEL else None,
            is_quarantined=lambda channel: channel == "C3",
            unreadable=False,
        )
        monkeypatch.setattr(store_mod, "store", lambda: codes)
        assert sca._is_code_channel(_CHANNEL) is True
        assert sca._is_code_channel("C3") is True
        assert sca._is_code_channel("C2") is False

    @pytest.mark.parametrize(
        "command",
        ["!channel", "!channel always", "!channel mention", "!channel observe", "!channel off"],
    )
    def test_every_other_channel_word(self, crew, command):
        assert _say(command) == sca.OTHER_WORD_TEXT
        assert not config_path().exists()
        assert _audits(crew) == [(_SHARED, "denied", "C1", "owner-only !channel word")]


def _state(*slots) -> SimpleNamespace:
    """The dashboard state as the handler reads it, with the registry's own rule.

    The thread index holds a chat that mirrors out to a thread, never a Slack
    thread's own tab, and ``get_linked_slot`` answers only a slot still linked.
    """
    by_key = {f"chat-{i}": slot for i, slot in enumerate(slots)}
    index = {slot._slack_thread_ts: key for key, slot in by_key.items() if not slot.own_tab}

    def get_linked_slot(thread_ts: str) -> SimpleNamespace | None:
        slot = by_key.get(index.get(thread_ts, ""))
        if slot is None or not slot._slack_linked or slot._slack_thread_ts != thread_ts:
            return None
        return slot

    return SimpleNamespace(_slots=by_key, _slack_to_slot=index, get_linked_slot=get_linked_slot)


_SLACK_KEY = "slack:1785370133.085469"
_SLACK_STEM = "slack_1785370133.085469"


def _real_state(tmp_path, monkeypatch, link: tuple[str, str]):
    """A real dashboard state whose every new slot finds *link* saved for its session."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.sessions.get_slack_link = MagicMock(return_value=link)
    state.sessions.slack_thread_owners = lambda channel: []
    monkeypatch.setattr(_handler, "get_dashboard_state", lambda: state)
    return state


def _sessions(*links: tuple[str, str, str]) -> SessionMap:
    """A session map holding *links*, none of them with a session ID."""
    smap = SessionMap()
    for key, thread_ts, channel in links:
        smap.set_slack_link(key, thread_ts, channel)
    return smap


_THREADS = itertools.count(1)


def _slot(
    channel: str, *, linked: bool = True, closing: bool = False, own_tab: bool = False
) -> SimpleNamespace:
    """A slot with a Slack link; *own_tab* is the tab of a Slack thread itself."""
    return SimpleNamespace(
        _slack_linked=linked,
        _slack_channel=channel,
        _slack_thread_ts=f"1700000000.{next(_THREADS):06d}",
        is_closing=closing,
        own_tab=own_tab,
    )


class TestALinkedDashboardChatIsNotCutOff:
    def test_a_chat_linked_to_a_thread_here_refuses_a_set(self, crew, monkeypatch):
        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: _state(_slot(_CHANNEL)))

        assert _say("!channel agent research") == sca.LINKED_CHAT_TEXT
        assert _disk() == {}
        assert _audits(crew) == [(_SHARED, "denied", "C1:research", "linked dashboard chat")]

    def test_it_refuses_replacing_a_shared_users_bot_too(self, crew, monkeypatch):
        _say("!channel agent research")
        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: _state(_slot(_CHANNEL)))

        assert _say("!channel agent web-frontend") == sca.LINKED_CHAT_TEXT
        assert _disk()["agent"] == "research"

    def test_off_is_allowed_because_it_cuts_nothing_off(self, crew, monkeypatch):
        _say("!channel agent research")
        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: _state(_slot(_CHANNEL)))

        assert _say("!channel agent off") == sca.CLEARED_TEXT

    @pytest.mark.parametrize(
        "slot",
        [
            _slot("C2"),
            _slot(_CHANNEL, linked=False),
            _slot(_CHANNEL, closing=True),
            _slot(_CHANNEL, own_tab=True),
        ],
        ids=["another-channel", "not-linked", "closing", "a-slack-threads-own-tab"],
    )
    def test_other_chats_do_not_count(self, crew, monkeypatch, slot):
        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: _state(slot))
        assert "*research*" in _say("!channel agent research")

    @pytest.mark.parametrize("before", ["", "web-frontend"], ids=["no-agent", "shared-bot"])
    def test_a_chat_linked_while_the_write_waited_takes_the_write_back(
        self, crew, monkeypatch, before
    ):
        if before:
            _say(f"!channel agent {before}")
        linked = _state(_slot(_CHANNEL, linked=False))
        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: linked)

        def _link() -> None:
            linked._slots["chat-0"]._slack_linked = True

        _after_the_next_write(monkeypatch, _link)

        assert _say("!channel agent research") == sca.LINKED_CHAT_TEXT
        assert _disk() == ({"agent": before, "shared_agent": before} if before else {"agent": ""})
        assert _audits(crew)[-1] == (_SHARED, "denied", "C1:research", "linked dashboard chat")

    def test_taking_back_restores_the_bot_the_locked_write_replaced(self, crew, monkeypatch):
        assert sca.save(_CHANNEL, "web-frontend") == ""
        linked = _state(_slot(_CHANNEL, linked=False))
        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: linked)
        resolve = _handler.resolve_bot_name

        def _resolve(name):
            assert sca.save(_CHANNEL, "research") == "web-frontend"
            return resolve(name)

        monkeypatch.setattr(_handler, "resolve_bot_name", _resolve)

        def _link() -> None:
            linked._slots["chat-0"]._slack_linked = True

        _after_the_next_write(monkeypatch, _link)

        assert _say("!channel agent web-frontend") == sca.LINKED_CHAT_TEXT
        assert _disk() == {"agent": "research", "shared_agent": "research"}

    def test_a_chat_whose_tab_is_closed_still_owns_its_thread(self, crew, monkeypatch):
        """No slot, but the session map still routes the thread to the chat's session."""
        sessions = _sessions(("dashboard:chat-1", "1700000000.000100", _CHANNEL))
        state = SimpleNamespace(_slots={}, sessions=sessions)
        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: state)

        assert _say("!channel agent research") == sca.LINKED_CHAT_TEXT
        assert _disk() == {}

    def test_threads_of_other_owners_and_channels_do_not_count(self, crew, monkeypatch):
        sessions = _sessions(
            ("dashboard:chat-1", "1700000000.000100", "C2"),
            ("slack:1700000000.000200", "1700000000.000200", _CHANNEL),
            ("cron:job-1", "1700000000.000300", _CHANNEL),
            ("dashboard:chat-2", "1700000000.000400", _CHANNEL),
        )
        sessions.clear_slack_link("dashboard:chat-2")
        state = SimpleNamespace(_slots={}, sessions=sessions)
        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: state)

        assert sorted(sessions.slack_thread_owners(_CHANNEL)) == [
            "cron:job-1",
            "slack:1700000000.000200",
        ]
        assert sessions.slack_thread_owners("C2") == ["dashboard:chat-1"]
        assert "*research*" in _say("!channel agent research")

    def test_a_thread_another_session_took_over_does_not_count(self, crew, monkeypatch):
        """The thread index names one owner: a chat that lost its thread owns nothing."""
        sessions = _sessions(
            ("dashboard:chat-1", "1700000000.000100", _CHANNEL),
            ("cron:job-1", "1700000000.000100", _CHANNEL),
        )
        state = SimpleNamespace(_slots={}, sessions=sessions)
        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: state)

        assert sessions.slack_thread_owners(_CHANNEL) == ["cron:job-1"]
        assert "*research*" in _say("!channel agent research")

    def test_the_session_manager_answers_from_its_map(self):
        """The dashboard state holds the manager, so the handler asks it, not the map."""
        manager = SimpleNamespace(
            _session_map=_sessions(("dashboard:chat-1", "1700000000.000100", _CHANNEL))
        )

        assert SessionManager.slack_thread_owners(manager, _CHANNEL) == ["dashboard:chat-1"]
        assert SessionManager.slack_thread_owners(manager, "C2") == []

    def test_a_slack_threads_own_tab_on_a_real_dashboard_is_not_a_linked_chat(
        self, crew, monkeypatch, tmp_path
    ):
        """Every Slack thread has a tab, and that tab carries the thread's own link."""
        state = _real_state(tmp_path, monkeypatch, ("1785370133.085469", _CHANNEL))
        state.sessions.channel_key_for_stem = lambda stem: _SLACK_KEY if stem == _SLACK_STEM else ""
        tab = state.get_or_create_slot(_SLACK_STEM, linked_session_key=_SLACK_KEY)

        assert (tab._slack_linked, tab._slack_channel) == (True, _CHANNEL)
        assert "*research*" in _say("!channel agent research")

    def test_a_chat_mirrored_out_on_a_real_dashboard_is_a_linked_chat(
        self, crew, monkeypatch, tmp_path
    ):
        state = _real_state(tmp_path, monkeypatch, ("1700000000.000900", _CHANNEL))
        chat = state.get_or_create_slot("chat-9-1700000000")

        assert state.get_linked_slot("1700000000.000900") is chat
        assert _say("!channel agent research") == sca.LINKED_CHAT_TEXT
        assert _disk() == {}

    def test_a_chat_list_that_cannot_be_read_refuses(self, crew, monkeypatch):
        class _Broken:
            @property
            def _slack_to_slot(self):
                raise RuntimeError("thread index unreadable")

        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: _Broken())
        assert _say("!channel agent research") == sca.LINKED_CHAT_TEXT


class TestItSurvivesARestart:
    def test_the_agent_and_who_set_it_are_read_back(self, crew):
        _say("!channel agent research")

        fresh = _restart()
        assert (fresh.agent, fresh.shared_agent) == ("research", "research")
        assert _say("!channel agent off", user=_OTHER_SHARED) == sca.CLEARED_TEXT

    def test_the_owners_agent_is_still_the_owners(self, crew):
        _owner_sets("research")

        fresh = _restart()
        assert (fresh.agent, fresh.shared_agent) == ("research", "")
        assert _say("!channel agent off") == sca.OWNER_AGENT_TEXT

    def test_a_config_save_keeps_the_mark(self, crew):
        """Any save of the whole config writes each channel from its fields."""
        _say("!channel agent research")
        loader._invalidate_config_cache()
        saved = KiroCrewConfig.load().to_dict()["slack"]["channels"][_CHANNEL]

        assert saved["agent"] == saved["shared_agent"] == "research"

    @pytest.mark.parametrize(
        "raw, kept",
        [
            ({"agent": "research", "shared_agent": "research"}, "research"),
            ({"agent": "web-frontend", "shared_agent": "research"}, ""),
            ({"agent": "", "shared_agent": "research"}, ""),
            ({"shared_agent": "research"}, ""),
            ({"agent": "research"}, ""),
            ({"agent": "research", "shared_agent": 7}, ""),
        ],
    )
    def test_a_mark_counts_only_beside_the_agent_it_names(self, raw, kept):
        assert ChannelConfig.from_dict(raw).shared_agent == kept


# ── Through the command router: who reaches the handler, and who does not ──


class _Sessions(FakeSessions):
    def __init__(self):
        super().__init__(
            ScriptedProvider(
                [
                    make_event(EVENT_TEXT_CHUNK, text="hi"),
                    make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
                ]
            )
        )


def _route(text: str, user: str) -> list[str]:
    """Send *text* as a channel message; every reply posted."""
    slack = RecordingSlackClient()

    async def _scenario():
        await transport_dispatch.handle_message_transport(
            slack=slack,
            sessions=_Sessions(),
            channel=_CHANNEL,
            text=text,
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id=user,
            context_builder=None,
            conversation_log=None,
        )

    asyncio.run(_scenario())
    return [kw["text"] for (method, kw) in slack.transcript if method == "post_message"]


class TestTheCommandRouter:
    def test_a_shared_user_reaches_the_handler_with_no_deprecation_notice(self, crew):
        posts = _route("!channel agent research", _SHARED)

        assert len(posts) == 1 and posts[0].startswith("Agent for this channel: *research*.")
        assert _disk() == {"agent": "research", "shared_agent": "research"}

    @pytest.mark.parametrize("command", ["!channel", "!channel always", "!channel off"])
    def test_a_shared_users_other_word_is_refused_as_before(self, crew, command):
        assert _route(command, _SHARED) == ["⛔ Owner-only command."]
        assert not config_path().exists()
        crew.log_api_access.assert_any_call(
            caller=_SHARED,
            operation="slack.owner_command",
            outcome="denied",
            source="slack",
            resources="!channel",
            error="unauthorized sender",
        )

    def test_the_agent_word_is_read_in_any_case(self, crew):
        assert _route("!channel Agent research", _SHARED)[0].startswith("Agent for this channel")

    def test_a_stranger_is_refused_as_before(self, crew):
        assert _route("!channel agent research", _STRANGER) == ["⛔ Owner-only command."]
        assert not config_path().exists()
        crew.log_api_access.assert_any_call(
            caller=_STRANGER,
            operation="slack.owner_command",
            outcome="denied",
            source="slack",
            resources="!channel",
            error="unauthorized sender",
        )
        assert _audits(crew) == []

    def test_the_owner_sets_any_agent_and_the_mark_goes(self, crew, monkeypatch):
        monkeypatch.setattr(_handler, "_resolve_agent_name", lambda name, project=None: name)
        _say("!channel agent research")

        posts = _route("!channel agent kirocrew-lite", _OWNER)
        assert posts[-1] == "Agent for this channel: *kirocrew-lite*"
        assert _disk() == {"agent": "kirocrew-lite"}
        assert _audits(crew)[-1] == (_OWNER, "allowed", "C1:kirocrew-lite", "")

        posts = _route("!channel agent off", _OWNER)
        assert posts[-1] == "Agent for this channel: default"
        assert _disk() == {"agent": ""}

    def test_the_owner_replaces_a_shared_users_bot_with_a_chat_linked(self, crew, monkeypatch):
        _say("!channel agent research")
        monkeypatch.setattr(_handler, "get_dashboard_state", lambda: _state(_slot(_CHANNEL)))

        assert _route("!channel agent web-frontend", _OWNER)[-1] == (
            "Agent for this channel: *web-frontend*"
        )
        assert _disk() == {"agent": "web-frontend"}


class TestStaleMarksRetireOnReload:
    def test_a_hand_selected_agent_stays_the_owners_when_selected_again(self, crew):
        _say("!channel agent research")
        _hand_edit(lambda doc: doc["slack"]["channels"][_CHANNEL].update(agent="web-frontend"))

        asyncio.run(sca.retire_stale_marks_on_reload())

        assert _disk() == {"agent": "web-frontend"}
        _hand_edit(lambda doc: doc["slack"]["channels"][_CHANNEL].update(agent="research"))
        assert _restart().shared_agent == ""
        assert _say("!channel agent off") == sca.OWNER_AGENT_TEXT
        assert _say("!channel agent web-frontend") == sca.OWNER_AGENT_TEXT
        assert _disk() == {"agent": "research"}

    @pytest.mark.parametrize("shared", [False, True])
    def test_a_file_without_stale_marks_is_not_rewritten(self, crew, shared):
        if shared:
            _say("!channel agent research")
        else:
            _owner_sets("research")
            KiroCrewConfig.load()
        path = config_path()
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns

        assert sca.retire_stale_marks() is False
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == mtime

    def test_retirement_keeps_other_keys_and_channels_and_does_not_repeat(self, crew):
        _say("!channel agent research")

        def edit(doc):
            doc["slack"]["channels"][_CHANNEL].update(agent="web-frontend", activation="always")
            doc["slack"]["channels"]["C2"] = {"agent": "research", "shared_agent": "research"}
            doc["agent"] = {"model": "keep-this-model"}

        _hand_edit(edit)
        before = json.loads(config_path().read_text(encoding="utf-8"))
        assert sca.retire_stale_marks() is True
        after = json.loads(config_path().read_text(encoding="utf-8"))
        del before["slack"]["channels"][_CHANNEL]["shared_agent"]
        before.pop("meta", None)
        after.pop("meta", None)
        assert after == before
        saved = config_path().read_bytes()
        assert sca.retire_stale_marks() is False
        assert config_path().read_bytes() == saved

    def test_an_unreadable_document_is_not_replaced(self, crew):
        path = config_path()
        path.write_text("{broken json", encoding="utf-8")
        before = path.read_bytes()

        assert sca.retire_stale_marks() is False
        assert path.read_bytes() == before

    def test_a_sensitive_path_is_not_written(self, crew, monkeypatch):
        _say("!channel agent research")
        _hand_edit(lambda doc: doc["slack"]["channels"][_CHANNEL].update(agent="web-frontend"))
        before = config_path().read_bytes()
        monkeypatch.setattr(_handler, "is_sensitive_path", lambda path: True)

        assert sca.retire_stale_marks() is False
        assert config_path().read_bytes() == before

    @pytest.mark.parametrize("stale_endorsed", [False, True])
    def test_a_pending_hand_edit_is_not_published(self, crew, monkeypatch, stale_endorsed):
        from kiro_crew.config import loosening_provenance as lp

        _say("!channel agent research")
        if stale_endorsed:
            _hand_edit(lambda doc: doc["slack"]["channels"][_CHANNEL].update(agent="web-frontend"))
        monkeypatch.setattr(lp, "enforcement_active", lambda: True)
        path = config_path()
        endorsed = path.read_bytes()
        lp.record(lp.LEAF_CONFIG, endorsed, strict=True)
        _hand_edit(lambda doc: doc["slack"]["channels"][_CHANNEL].update(agent="owner-choice"))
        pending = path.read_bytes()

        assert sca.retire_stale_marks() is False
        assert path.read_bytes() == pending
        assert lp.leaf_pending(lp.LEAF_CONFIG)

    @pytest.mark.parametrize("error", [RuntimeError("lock failed"), asyncio.CancelledError()])
    def test_reload_errors_are_best_effort_but_cancellation_propagates(
        self, crew, monkeypatch, error
    ):
        async def fail(fn):
            raise error

        monkeypatch.setattr(_handler, "run_config_write", fail)
        if isinstance(error, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(sca.retire_stale_marks_on_reload())
        else:
            asyncio.run(sca.retire_stale_marks_on_reload())
