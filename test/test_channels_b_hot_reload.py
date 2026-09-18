"""Teams / iMessage / WhatsApp hot-reload claims that are not shared with any channel.

The questions every channel is asked -- a reloaded roster reaching the live
transport, a degraded document keeping the previous authorization state, the SEL
audit by count, the point-of-use thresholds, the end-to-end file write -- are
parametrized once over the whole channel table in
``test_channels_a_hot_reload.py``. What is here is what those three channels
claim on their own:

* Teams' allow-list has THREE holders -- the transport's frozen roster, the
  dispatcher's copy and the session-resume owner. Two agreeing and the third
  stale is exactly the state that lets a removed identity keep listing dashboard
  sessions, so every apply is checked for agreement across all three;
* iMessage adopts an EMPTY roster, because an empty roster denies everyone here
  and that is how an operator shuts inbound off without stopping the channel;
* WhatsApp's DM policy runs the opposite way from every other channel's
  wrong-shape rule: an unknown policy STRING is adopted, because ``_dm_policy``
  denies whatever it cannot name and keeping the previous policy would leave the
  WIDER one in force. Its group rule set is a third holder, rebuilt only when the
  coerced rules actually differ so the unprompted-reply cooldown is not churned.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _hot_reload_helpers import (
    FakeIMessageClient,
    FakeSessions,
    FakeTeamsClient,
    FakeWhatsAppClient,
    apply,
    boot_cfg,
    cfg_with,
    change,
    drive_one_reload,
    point_loader_at,
    sel_outcomes,
)

from kiro_crew.config import live
from kiro_crew.imessage.transport import IMessageTransport
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.teams.transport import TeamsTransport
from kiro_crew.teams.transport_dispatch import TeamsDispatcher
from kiro_crew.whatsapp.transport import WhatsAppTransport
from kiro_crew.whatsapp.transport_dispatch import WhatsAppDispatcher

# ------------------------------------------------------------------
# Teams: three holders of one roster
# ------------------------------------------------------------------


class TestTeamsAllowList:
    def _transport(self, *, allowed=("a@x.com",)) -> TeamsTransport:
        return TeamsTransport(FakeTeamsClient(), allowed_emails=list(allowed))

    def _msg(self, email: str) -> InboundMessage:
        return InboundMessage(
            channel_type="teams", user_id=email, conversation_id="conv1", text="hi"
        )

    def test_a_reloaded_identity_matches_case_insensitively(self):
        t = self._transport(allowed=())
        t.reconfigure(SimpleNamespace(allowed_emails=["MiXeD@X.com"]))
        assert t.authorize(self._msg("mixed@x.com"))

    def test_an_unchanged_roster_is_not_audited(self, monkeypatch):
        audited: list[dict] = []
        monkeypatch.setattr(
            "kiro_crew.teams.transport.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: audited.append(kw)),
        )
        t = self._transport()
        t.reconfigure(SimpleNamespace(allowed_emails=["A@X.com"]))
        assert sel_outcomes(audited, "teams_transport.reconfigure") == []


class TestTeamsDispatcherApplier:
    def _dispatcher(self, transport=None, *, allowed=("a@x.com",)) -> TeamsDispatcher:
        d = TeamsDispatcher(
            sessions=FakeSessions(),
            ctx_builder=SimpleNamespace(),
            cfg=boot_cfg("teams", soft_threshold_pct=80, hard_threshold_pct=95),
            allowed_emails=set(allowed),
        )
        d.client = FakeTeamsClient()
        d.transport = transport
        return d

    def test_all_three_allow_list_copies_agree_after_one_apply(self):
        transport = TeamsTransport(FakeTeamsClient(), allowed_emails=["a@x.com"])
        d = self._dispatcher(transport)
        assert d._session_resume.owner_id == "a@x.com"

        apply(d, change(cfg_with("teams", allowed_emails=["b@x.com"]), "teams"))

        assert transport._allowed == frozenset({"b@x.com"})
        assert d._allowed_emails == frozenset({"b@x.com"})
        assert d._session_resume.owner_id == "b@x.com"
        assert d._session_resume.is_owner("B@X.com")
        assert not d._session_resume.is_owner("a@x.com")

    def test_a_second_identity_removes_the_session_resume_owner(self):
        d = self._dispatcher(TeamsTransport(FakeTeamsClient(), allowed_emails=["a@x.com"]))
        apply(d, change(cfg_with("teams", allowed_emails=["a@x.com", "b@x.com"]), "teams"))
        assert d._session_resume.owner_id == ""
        assert not d._session_resume.is_owner("a@x.com")

    def test_a_degraded_section_keeps_every_copy(self):
        transport = TeamsTransport(FakeTeamsClient(), allowed_emails=["a@x.com"])
        d = self._dispatcher(transport)
        with pytest.raises(live.ConfigDeferred):
            apply(
                d,
                change(
                    cfg_with("teams", degraded=frozenset({"teams"}), allowed_emails=["b@x.com"]),
                    "teams",
                ),
            )
        assert transport._allowed == frozenset({"a@x.com"})
        assert d._allowed_emails == frozenset({"a@x.com"})
        assert d._session_resume.owner_id == "a@x.com"

    @pytest.mark.asyncio
    async def test_a_config_file_write_reaches_the_session_resume_owner(
        self, tmp_path, monkeypatch
    ):
        """The third holder end to end: it is the one no inbound message touches."""
        doc = {"teams": {"allowed_emails": ["a@x.com"]}}
        cfg_path = point_loader_at(tmp_path, monkeypatch, doc)
        d = self._dispatcher(TeamsTransport(FakeTeamsClient(), allowed_emails=["a@x.com"]))
        await drive_one_reload(d, cfg_path, {"teams": {"allowed_emails": ["b@x.com"]}})
        assert d._session_resume.owner_id == "b@x.com"


# ------------------------------------------------------------------
# iMessage: an empty roster is adopted
# ------------------------------------------------------------------


class TestIMessageAllowList:
    def test_an_empty_roster_is_adopted_and_denies_everyone(self):
        t = IMessageTransport(FakeIMessageClient(), allowed_handles=["+15550100000"])
        t.reconfigure(SimpleNamespace(allowed_handles=[]))
        assert not t.authorize(
            InboundMessage(
                channel_type="imessage",
                user_id="+15550100000",
                conversation_id="+15550100000",
                text="hi",
            )
        )


# ------------------------------------------------------------------
# WhatsApp: the DM policy and the group rule set
# ------------------------------------------------------------------


def _no_dispatch_factory():
    async def _dispatch(msg):
        return None

    return _dispatch


class TestWhatsAppAuthorizationReload:
    def _transport(self, *, policy="allowlist", wa_ids=("15550100000",), groups=None):
        return WhatsAppTransport(
            FakeWhatsAppClient(),
            _no_dispatch_factory(),
            dm_policy=policy,
            allowed_wa_ids=list(wa_ids),
            groups=list(groups or []),
        )

    def _section(self, **over) -> SimpleNamespace:
        base = {"dm_policy": "allowlist", "allowed_wa_ids": ["15550100000"], "groups": []}
        return SimpleNamespace(**{**base, **over})

    def test_a_policy_flip_to_disabled_narrows_immediately(self):
        t = self._transport()
        t.reconfigure(self._section(dm_policy="disabled"))
        assert not t.may_send_to("15550100000@s.whatsapp.net")

    def test_an_unknown_policy_string_is_adopted_and_denies_everyone(self):
        """Adopted, because ``_dm_policy`` fails closed on what it cannot name.

        Keeping the previous (wider) policy would be the unsafe direction here.
        """
        t = self._transport()
        t.reconfigure(self._section(dm_policy="whatever"))
        assert not t.may_send_to("15550100000@s.whatsapp.net")

    def test_a_non_string_policy_keeps_the_previous_one(self):
        t = self._transport()
        t.reconfigure(self._section(dm_policy=None))
        assert t.may_send_to("15550100000@s.whatsapp.net")

    def test_a_reloaded_group_becomes_configured(self):
        t = self._transport(groups=[])
        assert not t.group_gate.configured("123@g.us")
        t.reconfigure(self._section(groups=[{"jid": "123@g.us", "mode": "mention"}]))
        assert t.group_gate.configured("123@g.us")

    def test_a_removed_group_stops_being_configured(self):
        t = self._transport(groups=[{"jid": "123@g.us", "mode": "mention"}])
        t.reconfigure(self._section())
        assert not t.group_gate.configured("123@g.us")

    def test_an_unknown_group_mode_falls_back_to_mention(self):
        t = self._transport(groups=[])
        t.reconfigure(
            self._section(allowed_wa_ids=[], groups=[{"jid": "123@g.us", "mode": "shout"}])
        )
        assert t._group_rules[0]["mode"] == "mention"

    def test_an_unchanged_group_set_keeps_the_same_gate(self):
        """The gate holds the unprompted-reply cooldown clock, so it is not churned."""
        rules = [{"jid": "123@g.us", "mode": "mention"}]
        t = self._transport(groups=rules)
        gate = t.group_gate
        t.reconfigure(self._section(groups=rules))
        assert t.group_gate is gate


class TestWhatsAppDispatcherApplier:
    def _dispatcher(self, transport=None) -> WhatsAppDispatcher:
        d = WhatsAppDispatcher(
            boot_cfg("whatsapp", soft_threshold_pct=80, hard_threshold_pct=95),
            FakeSessions(),
            SimpleNamespace(),
            approval_mode="interactive",
        )
        d.client = FakeWhatsAppClient()
        d.transport = transport
        return d

    def _transport(self, **kw):
        return WhatsAppTransport(FakeWhatsAppClient(), _no_dispatch_factory(), **kw)

    def test_a_reloaded_policy_reaches_the_transport(self):
        transport = self._transport(dm_policy="allowlist", allowed_wa_ids=["15550100000"])
        apply(
            self._dispatcher(transport),
            change(cfg_with("whatsapp", dm_policy="disabled"), "whatsapp"),
        )
        assert transport._dm_policy == "disabled"

    def test_a_degraded_section_keeps_the_policy_and_the_group_rules(self):
        transport = self._transport(
            dm_policy="allowlist",
            allowed_wa_ids=["15550100000"],
            groups=[{"jid": "123@g.us", "mode": "mention"}],
        )
        with pytest.raises(live.ConfigDeferred):
            apply(
                self._dispatcher(transport),
                change(
                    cfg_with(
                        "whatsapp",
                        degraded=frozenset({"whatsapp"}),
                        dm_policy="open",
                        allowed_wa_ids=[],
                        groups=[],
                    ),
                    "whatsapp",
                ),
            )
        assert transport._dm_policy == "allowlist"
        assert transport.group_gate.configured("123@g.us")

    @pytest.mark.asyncio
    async def test_a_config_file_write_reaches_the_group_rules(self, tmp_path, monkeypatch):
        """The group rule set is the holder no allow-list assertion reaches."""
        doc = {"whatsapp": {"dm_policy": "allowlist", "allowed_wa_ids": []}}
        cfg_path = point_loader_at(tmp_path, monkeypatch, doc)
        transport = self._transport(dm_policy="allowlist", allowed_wa_ids=[])
        await drive_one_reload(
            self._dispatcher(transport),
            cfg_path,
            {
                "whatsapp": {
                    "dm_policy": "allowlist",
                    "allowed_wa_ids": ["15550100001"],
                    "groups": [{"jid": "123@g.us", "mode": "mention"}],
                }
            },
        )
        assert transport.may_send_to("15550100001@s.whatsapp.net")
        assert transport.group_gate.configured("123@g.us")
