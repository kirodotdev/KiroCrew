"""WeCom / Weixin / Feishu hot-reload claims that are not shared with any channel.

The questions every channel is asked -- a reloaded roster reaching the live
transport, a degraded document keeping the previous authorization state, the SEL
audit by count, the point-of-use thresholds, the end-to-end file write -- are
parametrized once over the whole channel table in
``test_channels_a_hot_reload.py``. What is here is what those three channels
claim on their own:

* WeCom's ``allow_all_users`` is the widest grant any channel has -- the whole org
  tenant -- so each flip is audited in its own right, in both directions;
* WeCom answers a threshold from the config FILE when no watcher is armed yet,
  which orders the fallback: the file is the truth and the boot copy is reached
  only when the file cannot be read at all;
* Weixin's ids are OPAQUE, so the roster is deduped and blank-stripped without
  the digit coercion that would silently empty it, and an unknown DM policy keeps
  the previous one rather than being adopted;
* Feishu's group gate must leave a P2P turn alone, and its configured-target list
  is a second reader of the same roster.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from _hot_reload_helpers import (
    FakeLarkClient,
    FakeSessions,
    FakeWeComClient,
    FakeWeixinClient,
    apply,
    boot_cfg,
    cfg_with,
    change,
    sel_outcomes,
)

from kiro_crew.config import live
from kiro_crew.feishu.client import CHAT_P2P, LarkInbound
from kiro_crew.feishu.transport import FeishuTransport
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.wecom.transport import WeComTransport
from kiro_crew.wecom.transport_dispatch import WeComDispatcher
from kiro_crew.weixin.transport import WeixinTransport

# ------------------------------------------------------------------
# WeCom: the allow-all flip and the file-over-boot fallback
# ------------------------------------------------------------------


class TestWeComAllowAll:
    def _transport(self, *, allowed=(), allow_all=False) -> WeComTransport:
        return WeComTransport(
            FakeWeComClient(), allowed_users=list(allowed), allow_all=allow_all, owner_id=""
        )

    def _msg(self, userid: str) -> InboundMessage:
        return InboundMessage(
            channel_type="wecom", user_id=userid, conversation_id=userid, text="hi"
        )

    def _audited(self, monkeypatch) -> list[dict]:
        audited: list[dict] = []
        monkeypatch.setattr(
            "kiro_crew.wecom.transport.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: audited.append(kw)),
        )
        return audited

    def test_allow_all_flip_widens_and_is_audited(self, monkeypatch):
        audited = self._audited(monkeypatch)
        t = self._transport()
        assert not t.authorize(self._msg("Stranger"))
        t.reconfigure(SimpleNamespace(allowed_users=[], allow_all_users=True))
        assert t.authorize(self._msg("Stranger"))
        assert sel_outcomes(audited, "wecom_transport.reconfigure") == ["allow_all_enabled"]

    def test_allow_all_flip_off_is_audited_and_narrows(self, monkeypatch):
        audited = self._audited(monkeypatch)
        t = self._transport(allow_all=True)
        t.reconfigure(SimpleNamespace(allowed_users=[], allow_all_users=False))
        assert not t.authorize(self._msg("Stranger"))
        assert sel_outcomes(audited, "wecom_transport.reconfigure") == ["allow_all_disabled"]


class TestWeComThresholds:
    def _dispatcher(self, *, soft: int, hard: int) -> WeComDispatcher:
        d = WeComDispatcher(
            sessions=FakeSessions(pct=90.0),
            ctx_builder=SimpleNamespace(),
            cfg=boot_cfg("wecom", soft_threshold_pct=soft, hard_threshold_pct=hard),
            owner_id="",
        )
        d.client = FakeWeComClient()
        return d

    def test_thresholds_read_the_config_file_when_no_watcher_is_armed(self, tmp_path, monkeypatch):
        """No snapshot yet -> the file, not the boot copy.

        ``load()`` is fingerprint-cached, so this is the cheap and CORRECT
        fallback: the file is the truth, and the boot copy is only reached when
        the file cannot be read at all.
        """
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"wecom": {"soft_threshold_pct": 33, "hard_threshold_pct": 66}})
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_path)
        monkeypatch.setattr(
            "kiro_crew.config.loader.config_local_path", lambda: tmp_path / "local.json"
        )
        assert self._dispatcher(soft=70, hard=90)._thresholds() == (33, 66)


# ------------------------------------------------------------------
# Weixin: opaque ids and a policy that keeps its previous value
# ------------------------------------------------------------------


class TestWeixinAllowListAndPolicy:
    def _transport(self, *, allowed=("wxid_abc",), policy="allowlist") -> WeixinTransport:
        return WeixinTransport(
            FakeWeixinClient(),
            account_id="acct",
            ctx_store=SimpleNamespace(),
            allowed_user_ids=list(allowed),
            dm_policy=policy,
        )

    def _msg(self, user_id: str) -> InboundMessage:
        return InboundMessage(
            channel_type="weixin", user_id=user_id, conversation_id=user_id, text="hi"
        )

    def test_blank_entries_are_dropped_and_duplicates_deduped(self):
        t = self._transport()
        t.reconfigure(
            SimpleNamespace(
                allowed_user_ids=["wxid_a", " ", "wxid_a", "wxid_b"], dm_policy="allowlist"
            )
        )
        assert t._allowed == frozenset({"wxid_a", "wxid_b"})

    def test_an_unknown_policy_keeps_the_previous_one(self):
        t = self._transport()
        t.reconfigure(SimpleNamespace(allowed_user_ids=["wxid_abc"], dm_policy="everyone"))
        assert t._dm_policy == "allowlist"
        assert not t.authorize(self._msg("stranger"))

    def test_outbound_authorization_follows_the_reload(self):
        t = self._transport()
        assert t.may_send_to("wxid_abc")
        t.reconfigure(SimpleNamespace(allowed_user_ids=["wxid_new"], dm_policy="allowlist"))
        assert not t.may_send_to("wxid_abc")
        assert t.may_send_to("wxid_new")

    def test_a_degraded_section_keeps_the_policy_too(self):
        from kiro_crew.weixin.transport_dispatch import WeixinDispatcher

        t = self._transport()
        d = WeixinDispatcher(
            sessions=FakeSessions(),
            ctx_builder=SimpleNamespace(),
            cfg=boot_cfg("weixin", soft_threshold_pct=80, hard_threshold_pct=95),
            account_id="acct",
            ctx_store=SimpleNamespace(),
        )
        d.client = FakeWeixinClient()
        d.transport = t
        with pytest.raises(live.ConfigDeferred):
            apply(
                d,
                change(
                    cfg_with(
                        "weixin",
                        degraded=frozenset({"weixin"}),
                        allowed_user_ids=["wxid_new"],
                        dm_policy="open",
                    ),
                    "weixin",
                ),
            )
        assert t._dm_policy == "allowlist"


# ------------------------------------------------------------------
# Feishu: the group gate leaves P2P alone
# ------------------------------------------------------------------


class TestFeishuAllowLists:
    def _transport(self, *, open_ids=("ou_abc",), dispatch=None) -> FeishuTransport:
        return FeishuTransport(
            FakeLarkClient(),
            allowed_open_ids=list(open_ids),
            allow_group=False,
            allowed_group_ids=[],
            **({"dispatch": dispatch} if dispatch is not None else {}),
        )

    @pytest.mark.asyncio
    async def test_a_p2p_turn_is_unaffected_by_the_group_gate(self):
        dispatched: list[LarkInbound] = []

        async def _dispatch(inbound):
            dispatched.append(inbound)

        t = self._transport(dispatch=_dispatch)
        t.reconfigure(
            SimpleNamespace(allowed_open_ids=["ou_abc"], allow_group=False, allowed_group_ids=[])
        )
        await t.receive(
            LarkInbound(
                open_id="ou_abc", text="hi", message_id="m2", chat_type=CHAT_P2P, chat_id=""
            )
        )
        assert [m.message_id for m in dispatched] == ["m2"]

    def test_a_failing_audit_never_blocks_a_revocation(self, monkeypatch):
        """``sel()`` validates its trust root on first use and can raise. The
        roster is adopted BEFORE the audit runs, so a revocation lands even when
        the audit does not -- and both sets, not just the first: an exception on
        the first audit must not leave the second roster on its old value."""
        from kiro_crew.feishu import transport as feishu_transport

        def _boom():
            raise RuntimeError("SEL trust root too short")

        monkeypatch.setattr(feishu_transport, "sel", _boom)
        t = self._transport(open_ids=("ou_abc", "ou_revoked"))
        with pytest.raises(RuntimeError, match="trust root"):
            t.reconfigure(
                SimpleNamespace(
                    allowed_open_ids=["ou_abc"], allow_group=True, allowed_group_ids=["oc_room"]
                )
            )
        # The audit's error still reaches the watcher (which retries the applier
        # until SEL recovers), but the authorization state is already the
        # reloaded one -- the retry finds nothing left to adopt.
        assert t._allowed == frozenset({"ou_abc"})
        assert t._allowed_group_ids == frozenset({"oc_room"})

    def test_configured_targets_follow_the_reload(self):
        t = self._transport()
        t.reconfigure(
            SimpleNamespace(allowed_open_ids=["ou_new"], allow_group=False, allowed_group_ids=[])
        )
        assert [x.target_id for x in t.configured_targets()] == ["user:ou_new"]
