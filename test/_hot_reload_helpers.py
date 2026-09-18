"""Shared fakes, config builders and the per-channel case table for hot-reload tests.

Nine channels are asked the same questions with a different transport class, a
different roster field name and different identity strings, so the channel is
DATA rather than a copy of the test: :func:`channel_cases` returns one
:class:`ChannelCase` per channel and the tests parametrize over it with
``ids=lambda c: c.name``.

This module is not ``conftest.py`` on purpose. The case table imports nine
transport and nine dispatcher modules, and a conftest is imported by every test
in the repository -- so the table is built lazily behind an ``lru_cache`` and the
cheap helpers (``write_config``, ``change``, ``cfg_with``) cost an importer
nothing.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kiro_crew.config import live
from kiro_crew.config.live import ConfigChange, ConfigWatch
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import _clamp_pct, _normalize_threshold_pair

# ------------------------------------------------------------------
# Config documents and the shape a subscriber receives
# ------------------------------------------------------------------

#: Highest mtime written per path, so a rewrite is always strictly newer.
_LAST_MTIME_NS: dict[Path, int] = {}


def write_config(path: Path, doc: dict) -> None:
    """Write *doc* to *path* with a strictly increasing mtime.

    Two writes inside one clock tick (Windows, and any coarse filesystem) land
    on the SAME mtime, and a same-size edit is then invisible to the watcher's
    fingerprint -- so the mtime is forced forward rather than left to the clock.
    """
    path.write_text(json.dumps(doc), encoding="utf-8")
    st = path.stat()
    mtime = max(st.st_mtime_ns, _LAST_MTIME_NS.get(path, 0)) + 1_000_000_000
    os.utime(path, ns=(st.st_atime_ns, mtime))
    _LAST_MTIME_NS[path] = mtime


def change(new: Any, *changed: str, old: Any = None) -> ConfigChange:
    """The shape a subscriber receives from one reload."""
    return ConfigChange(old=old, new=new, changed=frozenset(changed))


def cfg_with(section_name: str, degraded: frozenset[str] = frozenset(), **section_kw: Any):
    """A real :class:`KiroCrewConfig` carrying one replaced section."""
    cfg = KiroCrewConfig()
    section = replace(getattr(cfg, section_name), **section_kw)
    return replace(cfg, _degraded_sections=degraded, **{section_name: section})


def boot_cfg(section_name: str, **section_kw: Any) -> SimpleNamespace:
    """A boot-snapshot stand-in shaped like the fields the dispatchers read."""
    return SimpleNamespace(
        agent=SimpleNamespace(default_agent="", approval_mode="interactive"),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
        **{section_name: SimpleNamespace(**section_kw)},
    )


def prime_live(section_name: str, **section_kw: Any) -> None:
    """Publish one replaced section as the process watcher's snapshot.

    Thresholds and the ``messaging.*`` rotation fields are read at point of use
    from the snapshot, so a test exercising one has to put the value THERE, not
    only in the dispatcher's boot copy.
    """
    live.watch().prime(cfg_with(section_name, **section_kw))


def prime_raw_section(section_name: str, **raw: Any) -> None:
    """Publish a snapshot whose section carries *raw* values verbatim.

    ``dataclasses.replace`` re-runs the section's own ``__post_init__``, which
    clamps a threshold and orders a pair -- so a config built that way can never
    hold an out-of-range value, and an assertion against it proves the LOADER's
    clamp instead of the point-of-use read. Setting the fields after construction
    is what puts an unclamped value in front of the reader.
    """
    cfg = KiroCrewConfig()
    section = getattr(cfg, section_name)
    for key, value in raw.items():
        setattr(section, key, value)
    live.watch().prime(cfg)


def prime_live_sections(cfg: Any, *names: str) -> None:
    """Publish *cfg*'s *names* sections as the live snapshot.

    Every field the test's stand-in carries is copied onto a real
    :class:`KiroCrewConfig`; the loader's own defaults fill the rest. Call it
    again after mutating the boot copy mid-test -- the snapshot is a copy, not a
    view.
    """
    base = KiroCrewConfig()
    sections: dict[str, Any] = {}
    for name in names:
        section = getattr(cfg, name, None)
        if section is None:
            continue
        overrides = {
            f.name: getattr(section, f.name)
            for f in dataclasses.fields(getattr(base, name))
            if hasattr(section, f.name)
        }
        sections[name] = replace(getattr(base, name), **overrides)
    live.reset_for_tests()
    live.watch().prime(replace(base, **sections))


def sel_outcomes(audited: list[dict], operation: str) -> list[str]:
    """Outcomes for one operation only.

    A monkeypatched module-level ``sel`` also captures ``authorize``'s own
    ``denied`` rows, which are a different event: asserting on the raw list
    would couple a test to how many times it happened to authorize.
    """
    return [a["outcome"] for a in audited if a.get("operation") == operation]


def apply(dispatcher: Any, config_change: ConfigChange) -> None:
    """Drive the dispatcher's OWN registered applier, holding it for the call.

    The watcher resolves the owner weakly, so the dispatcher must stay
    referenced while the applier runs -- which the parameter does.
    """
    applier = dispatcher._config_sub.callback()
    assert applier is not None, "the dispatcher's subscription was collected"
    applier(config_change)


# ------------------------------------------------------------------
# Driving one reload off a real temp config file
# ------------------------------------------------------------------


def point_loader_at(tmp_path: Path, monkeypatch: Any, doc: dict) -> Path:
    """Point the loader at a temp ``config.json`` carrying *doc*."""
    cfg_path = tmp_path / "config.json"
    write_config(cfg_path, doc)
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_path)
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_local_path", lambda: tmp_path / "config.local.json"
    )
    return cfg_path


async def drive_one_reload(dispatcher: Any, cfg_path: Path, doc: dict) -> ConfigChange | None:
    """Baseline, rewrite, then one forced reload -- no sleeping on the poll."""
    watch = ConfigWatch(poll_interval_secs=0.05)
    sub = dispatcher._config_sub
    watch.subscribe(*sub.prefixes, callback=sub.callback(), name=sub.name)
    await watch.refresh_now()
    write_config(cfg_path, doc)
    return await watch.refresh_now()


# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


class FakeSessions:
    """Only the surface the dispatchers touch in these tests."""

    def __init__(self, pct: float = 0.0) -> None:
        self._pct = pct
        self.busy: set[str] = set()

    def is_busy(self, key: Any) -> bool:
        return key in self.busy

    def check_context_usage(self, key: Any, provider: Any) -> float:
        return self._pct

    def max_generation(self, bucket: str) -> int:
        return 0


class FakeProvider:
    def __init__(self) -> None:
        self.compacted = False

    async def compact(self) -> None:
        self.compacted = True

    async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
        return {"type": "completed", "summary": ""}


class FakeClient:
    """A transport client stand-in: nothing is sent in these tests."""


class FakeTeamsClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, conversation_id: str, text: str, **kw: Any) -> str:
        self.sent.append((conversation_id, text))
        return "m1"


class FakeIMessageClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, handle: str, text: str) -> str:
        self.sent.append((handle, text))
        return "m1"

    def set_message_handler(self, fn: Any) -> None:
        pass


class FakeWhatsAppClient:
    def __init__(self) -> None:
        from kiro_crew.whatsapp.jids import OwnIdentity

        self.sent: list[tuple[str, str]] = []
        self.on_message = None
        # ``may_send_to`` asks the linked account whether a JID is its own
        # thread, which is the ``self`` policy's whole answer.
        self.me = OwnIdentity(jid="15559999999@s.whatsapp.net", lid="")

    async def send_text(self, jid: str, text: str) -> str:
        self.sent.append((jid, text))
        return "m1"


class FakeWeComClient:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []

    async def send_proactive(self, chat_id: str, text: str) -> bool:
        self.pushed.append((chat_id, text))
        return True

    def already_delivered(self, msgid: str) -> bool:
        return False

    def forget_msgid(self, msgid: str) -> None:
        pass


class FakeLarkClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []

    async def send_reply(self, message_id: str, text: str) -> bool:
        self.replies.append((message_id, text))
        return True


class FakeWeixinClient:
    pass


# ------------------------------------------------------------------
# The per-channel case table
# ------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ChannelCase:
    """One channel's answers to the questions every channel is asked.

    ``name`` is both the test id and the config SECTION name, which is what lets
    the section-scoped assertions (``touched``, the degraded set, the
    subscription prefixes) be derived rather than listed.
    """

    name: str
    #: Build a transport whose roster is *roster* (raw config values).
    make_transport: Callable[[Sequence[Any]], Any]
    #: ``make_dispatcher(transport=None, *, pct=0.0, soft=80, hard=95)``.
    make_dispatcher: Callable[..., Any]
    #: Every field ``reconfigure`` reads, for a roster of *roster*.
    section_fields: Callable[..., dict[str, Any]]
    boot: tuple[Any, ...]
    added: tuple[Any, ...]
    expect_boot: frozenset[str]
    expect_added: frozenset[str]
    boot_id: str
    added_id: str
    authorize: Callable[[Any, str], bool]
    #: The roster field name, for the wrong-shape test.
    roster_key: str
    audit_fields: dict[str, Any]
    audit_outcomes: tuple[str, ...]
    audit_secret: str
    #: True for a channel with a soft threshold and no hard one to order against.
    soft_only: bool = False
    #: Further ids the reloaded roster must also authorize (opaque-id claims).
    also_authorized: tuple[str, ...] = ()
    toggle_key: str = ""
    toggle_reader: Callable[[Any], bool] | None = None
    #: ``(outcome, resources)`` for a row whose resources string is a claim.
    audit_resources: tuple[str, str] | None = None
    #: ``probe(d) -> (read_this_generation, read_next_generation)``.
    dm_scope_probe: Callable[[Any], tuple[Callable[[], Any], Callable[[], Any]]] | None = None
    #: An inbound message for the ``_maybe_notice`` compaction path.
    inbound: Callable[[], Any] | None = None

    @property
    def transport_module(self) -> str:
        return f"kiro_crew.{self.name}.transport"

    @property
    def audit_operation(self) -> str:
        return f"{self.name}_transport.reconfigure"

    @property
    def subscription_name(self) -> str:
        parts = {"imessage": "IMessage", "whatsapp": "WhatsApp", "wecom": "WeCom"}
        return f"{parts.get(self.name, self.name.title())}Dispatcher"

    def section(self, roster: Sequence[Any], **overrides: Any) -> SimpleNamespace:
        """The reconfigure argument: a section carrying *roster*."""
        return SimpleNamespace(**self.section_fields(roster, **overrides))

    def config(self, roster: Sequence[Any], *, degraded: frozenset[str] = frozenset(), **over: Any):
        """A real config whose section carries *roster*."""
        return cfg_with(self.name, degraded=degraded, **self.section_fields(roster, **over))

    def document(self, roster: Sequence[Any], **overrides: Any) -> dict[str, Any]:
        """The ``config.json`` document carrying *roster*."""
        return {self.name: self.section_fields(roster, **overrides)}

    def threshold_fields(self, soft: int, hard: int) -> dict[str, int]:
        if self.soft_only:
            return {"soft_threshold_pct": soft}
        return {"soft_threshold_pct": soft, "hard_threshold_pct": hard}

    def read_thresholds(self, dispatcher: Any) -> Any:
        if self.soft_only:
            return dispatcher._soft_threshold()
        return dispatcher._thresholds()

    def expected_thresholds(self, soft: int, hard: int) -> Any:
        if self.soft_only:
            return _clamp_pct(soft)
        return _normalize_threshold_pair(soft, hard)


def _inbound(channel_type: str, user_id: str, conversation_id: str) -> Any:
    from kiro_crew.messaging.transport import InboundMessage

    return InboundMessage(
        channel_type=channel_type, user_id=user_id, conversation_id=conversation_id, text="hi"
    )


def _authorizer(name: str, conversation: str = "") -> Callable[[Any, str], bool]:
    """``authorize`` over an inbound whose conversation is *conversation*, or the id."""
    return lambda t, i: t.authorize(_inbound(name, i, conversation or i))


def _transport_factory(cls: Any, client_cls: Any, roster_kw: str, **fixed: Any):
    """``make(roster)``: the transport with *roster* under *roster_kw*."""
    return lambda roster: cls(client_cls(), **{roster_kw: list(roster)}, **fixed)


def _dispatcher_factory(
    cls: Any,
    name: str,
    client_cls: Any,
    *,
    soft_only: bool = False,
    boot_extra: dict[str, Any] | None = None,
    **ctor: Any,
):
    """``make(transport=None, *, pct, soft, hard)``: the dispatcher, wired.

    ``client`` and ``transport`` are assigned rather than passed, exactly as the
    boot factories do it -- the dispatcher is constructed before either exists.
    """

    def make(transport: Any = None, *, pct: float = 0.0, soft: int = 80, hard: int = 95) -> Any:
        thresholds: dict[str, Any] = {"soft_threshold_pct": soft}
        if not soft_only:
            thresholds["hard_threshold_pct"] = hard
        d = cls(
            sessions=FakeSessions(pct=pct),
            ctx_builder=SimpleNamespace(),
            cfg=boot_cfg(name, **thresholds, **(boot_extra or {})),
            **ctor,
        )
        d.client = client_cls()
        d.transport = transport
        return d

    return make


def _fields_factory(roster_key: str, cast: Callable[[Sequence[Any]], Any] = list, **defaults: Any):
    """``fields(roster, **overrides)``: every field ``reconfigure`` reads."""
    return lambda roster, **over: {roster_key: cast(roster), **defaults, **over}


def _generation_probe(read: Callable[[Any], Any], bucket: Any):
    """``probe(d)``: read this generation's key, then bump and read the next one."""

    def probe(d: Any) -> tuple[Callable[[], Any], Callable[[], Any]]:
        def _next() -> Any:
            d._conv.bump_gen(bucket)
            return read(d)

        return lambda: read(d), _next

    return probe


@functools.lru_cache(maxsize=1)
def channel_cases() -> tuple[ChannelCase, ...]:
    """Every channel whose allow-list follows ``config.json`` without a restart."""
    from kiro_crew.discord.transport import DiscordTransport
    from kiro_crew.discord.transport_dispatch import DiscordDispatcher
    from kiro_crew.feishu.client import CHAT_P2P, LarkInbound
    from kiro_crew.feishu.transport import FeishuTransport
    from kiro_crew.feishu.transport_dispatch import FeishuDispatcher
    from kiro_crew.imessage.transport import IMessageTransport
    from kiro_crew.imessage.transport_dispatch import IMessageDispatcher
    from kiro_crew.teams.transport import TeamsTransport
    from kiro_crew.teams.transport_dispatch import TeamsDispatcher
    from kiro_crew.telegram.transport import TelegramTransport
    from kiro_crew.telegram.transport_dispatch import TelegramDispatcher
    from kiro_crew.webex.transport import WebexTransport
    from kiro_crew.webex.transport_dispatch import WebexDispatcher
    from kiro_crew.wecom.client import WeComInbound
    from kiro_crew.wecom.transport import WeComTransport
    from kiro_crew.wecom.transport_dispatch import WeComDispatcher
    from kiro_crew.weixin.transport import WeixinTransport
    from kiro_crew.weixin.transport_dispatch import WeixinDispatcher
    from kiro_crew.whatsapp.transport import WhatsAppTransport
    from kiro_crew.whatsapp.transport_dispatch import WhatsAppDispatcher

    async def _no_dispatch(msg: Any) -> None:
        return None

    def whatsapp_transport(roster: Sequence[Any]) -> Any:
        # The dispatch callback is positional here, so there is no roster_kw form.
        return WhatsAppTransport(
            FakeWhatsAppClient(),
            _no_dispatch,
            dm_policy="allowlist",
            allowed_wa_ids=list(roster),
            groups=[],
        )

    def whatsapp_dispatcher(
        transport: Any = None, *, pct: float = 0.0, soft: int = 80, hard: int = 95
    ) -> Any:
        # The only dispatcher whose config is positional rather than ``cfg=``.
        d = WhatsAppDispatcher(
            boot_cfg("whatsapp", soft_threshold_pct=soft, hard_threshold_pct=hard),
            FakeSessions(pct=pct),
            SimpleNamespace(),
            approval_mode="interactive",
        )
        d.client = FakeWhatsAppClient()
        d.transport = transport
        return d

    telegram = ChannelCase(
        name="telegram",
        make_transport=_transport_factory(
            TelegramTransport,
            FakeClient,
            "allowed_user_ids",
            allow_forum=False,
            allowed_forum_chat_ids=[],
        ),
        make_dispatcher=_dispatcher_factory(
            TelegramDispatcher,
            "telegram",
            FakeClient,
            soft_only=True,
            boot_extra={"show_thinking": False},
            allowed_user_ids={"7"},
        ),
        section_fields=_fields_factory(
            "allowed_user_ids", allow_forum=False, allowed_forum_chat_ids=[]
        ),
        boot=(7,),
        added=(9,),
        expect_boot=frozenset({"7"}),
        expect_added=frozenset({"9"}),
        boot_id="7",
        added_id="9",
        authorize=_authorizer("telegram"),
        roster_key="allowed_user_ids",
        audit_fields={
            "allowed_user_ids": [424242],
            "allow_forum": True,
            "allowed_forum_chat_ids": [-100],
        },
        audit_outcomes=("allow_list_changed", "forum_allow_list_changed", "allow_forum_enabled"),
        audit_secret="424242",
        soft_only=True,
        toggle_key="allow_forum",
        toggle_reader=lambda t: t._allow_forum,
        dm_scope_probe=_generation_probe(
            lambda d: d._session_key(("direct", "7")), ("direct", "7")
        ),
    )

    discord = ChannelCase(
        name="discord",
        make_transport=_transport_factory(
            DiscordTransport,
            FakeClient,
            "allowed_user_ids",
            allowed_thread_ids=[],
            allowed_channel_ids=[],
        ),
        make_dispatcher=_dispatcher_factory(
            DiscordDispatcher,
            "discord",
            FakeClient,
            soft_only=True,
            boot_extra={"reactions_enabled": True, "show_thinking": False},
            allowed_user_ids={"11"},
        ),
        section_fields=_fields_factory(
            "allowed_user_ids", allowed_thread_ids=[], allowed_channel_ids=[], auto_thread=True
        ),
        boot=("11",),
        added=("22",),
        expect_boot=frozenset({"11"}),
        expect_added=frozenset({"22"}),
        boot_id="11",
        added_id="22",
        authorize=_authorizer("discord", "c1"),
        roster_key="allowed_user_ids",
        audit_fields={
            "allowed_user_ids": ["222222"],
            "allowed_thread_ids": ["t1"],
            "allowed_channel_ids": ["c1"],
            "auto_thread": True,
        },
        audit_outcomes=(
            "allow_list_changed",
            "channel_allow_list_changed",
            "thread_allow_list_changed",
        ),
        audit_secret="222222",
        soft_only=True,
        toggle_key="auto_thread",
        toggle_reader=lambda t: t._auto_thread,
        dm_scope_probe=_generation_probe(lambda d: d._session_key("11", ""), "user:11"),
    )

    webex = ChannelCase(
        name="webex",
        make_transport=_transport_factory(
            WebexTransport,
            FakeClient,
            "allowed_emails",
            allow_group_rooms=False,
            allowed_room_ids=[],
        ),
        make_dispatcher=_dispatcher_factory(
            WebexDispatcher,
            "webex",
            FakeClient,
            boot_extra={"reply_in_thread": False, "allowed_emails": ["a@example.com"]},
        ),
        section_fields=_fields_factory(
            "allowed_emails", allow_group_rooms=False, allowed_room_ids=[]
        ),
        boot=("a@example.com",),
        added=("b@example.com",),
        expect_boot=frozenset({"a@example.com"}),
        expect_added=frozenset({"b@example.com"}),
        boot_id="a@example.com",
        added_id="b@example.com",
        authorize=_authorizer("webex", "r1"),
        roster_key="allowed_emails",
        audit_fields={
            "allowed_emails": ["secret@example.com"],
            "allow_group_rooms": True,
            "allowed_room_ids": ["oc_room"],
        },
        audit_outcomes=("allow_list_changed", "room_allow_list_changed", "allow_group_enabled"),
        audit_secret="secret@example.com",
        toggle_key="allow_group_rooms",
        toggle_reader=lambda t: t._allow_group_rooms,
    )

    teams = ChannelCase(
        name="teams",
        make_transport=_transport_factory(TeamsTransport, FakeTeamsClient, "allowed_emails"),
        make_dispatcher=_dispatcher_factory(
            TeamsDispatcher, "teams", FakeTeamsClient, allowed_emails={"a@x.com"}
        ),
        section_fields=_fields_factory("allowed_emails"),
        boot=("a@x.com",),
        added=("b@x.com",),
        expect_boot=frozenset({"a@x.com"}),
        expect_added=frozenset({"b@x.com"}),
        boot_id="a@x.com",
        added_id="b@x.com",
        authorize=_authorizer("teams", "conv1"),
        roster_key="allowed_emails",
        audit_fields={"allowed_emails": ["secret@x.com"]},
        audit_outcomes=("allow_list_changed",),
        audit_secret="secret@x.com",
        dm_scope_probe=_generation_probe(lambda d: d._session_key("a@x.com"), "a@x.com"),
    )

    imessage = ChannelCase(
        name="imessage",
        make_transport=_transport_factory(IMessageTransport, FakeIMessageClient, "allowed_handles"),
        make_dispatcher=_dispatcher_factory(IMessageDispatcher, "imessage", FakeIMessageClient),
        section_fields=_fields_factory("allowed_handles"),
        boot=("+15550100000",),
        added=("+1 (555) 010-0001",),
        expect_boot=frozenset({"+15550100000"}),
        expect_added=frozenset({"+15550100001"}),
        boot_id="+15550100000",
        added_id="+15550100001",
        authorize=_authorizer("imessage"),
        roster_key="allowed_handles",
        audit_fields={"allowed_handles": ["+15550109999"]},
        audit_outcomes=("allow_list_changed",),
        audit_secret="5550109999",
    )

    whatsapp = ChannelCase(
        name="whatsapp",
        make_transport=whatsapp_transport,
        make_dispatcher=whatsapp_dispatcher,
        section_fields=_fields_factory("allowed_wa_ids", dm_policy="allowlist", groups=[]),
        boot=("15550100000",),
        added=("15550100001",),
        expect_boot=frozenset({"15550100000@s.whatsapp.net"}),
        expect_added=frozenset({"15550100001@s.whatsapp.net"}),
        boot_id="15550100000@s.whatsapp.net",
        added_id="15550100001@s.whatsapp.net",
        # The JID IS the conversation, so the outbound gate is the roster reader.
        authorize=lambda t, i: t.may_send_to(i),
        roster_key="allowed_wa_ids",
        audit_fields={"dm_policy": "allowlist", "allowed_wa_ids": ["15550109999"], "groups": []},
        audit_outcomes=("allow_list_changed",),
        audit_secret="5550109999",
    )

    wecom = ChannelCase(
        name="wecom",
        make_transport=_transport_factory(
            WeComTransport, FakeWeComClient, "allowed_users", allow_all=False, owner_id=""
        ),
        make_dispatcher=_dispatcher_factory(WeComDispatcher, "wecom", FakeWeComClient, owner_id=""),
        # The config carries dicts; the constructor takes the flattened userids.
        section_fields=_fields_factory(
            "allowed_users",
            cast=lambda roster: [{"userid": str(u)} for u in roster],
            allow_all_users=False,
        ),
        boot=("Wei",),
        added=("Ming",),
        expect_boot=frozenset({"Wei"}),
        expect_added=frozenset({"Ming"}),
        boot_id="Wei",
        added_id="Ming",
        authorize=_authorizer("wecom"),
        roster_key="allowed_users",
        audit_fields={"allowed_users": [{"userid": "Secret"}], "allow_all_users": False},
        audit_outcomes=("allow_list_changed",),
        audit_secret="Secret",
        toggle_key="allow_all_users",
        toggle_reader=lambda t: t._allow_all,
        dm_scope_probe=_generation_probe(lambda d: d._session_key("Wei"), "Wei"),
        inbound=lambda: WeComInbound(
            userid="Wei", text="hi", response_url="", req_id="r1", chatid=""
        ),
    )

    weixin = ChannelCase(
        name="weixin",
        make_transport=_transport_factory(
            WeixinTransport,
            FakeWeixinClient,
            "allowed_user_ids",
            account_id="acct",
            ctx_store=SimpleNamespace(),
            dm_policy="allowlist",
        ),
        make_dispatcher=_dispatcher_factory(
            WeixinDispatcher,
            "weixin",
            FakeWeixinClient,
            account_id="acct",
            ctx_store=SimpleNamespace(),
        ),
        section_fields=_fields_factory("allowed_user_ids", dm_policy="allowlist"),
        boot=("wxid_abc",),
        # An iLink id is OPAQUE: a hex@im.bot bot id must survive un-normalized,
        # or the deny-by-default policy locks that sender out.
        added=("wxid_new", "deadbeef@im.bot"),
        expect_boot=frozenset({"wxid_abc"}),
        expect_added=frozenset({"wxid_new", "deadbeef@im.bot"}),
        boot_id="wxid_abc",
        added_id="wxid_new",
        authorize=_authorizer("weixin"),
        roster_key="allowed_user_ids",
        audit_fields={"allowed_user_ids": ["wxid_secret"], "dm_policy": "disabled"},
        audit_outcomes=("allow_list_changed", "dm_policy_changed"),
        audit_secret="wxid_secret",
        also_authorized=("deadbeef@im.bot",),
        audit_resources=("dm_policy_changed", "from=allowlist to=disabled"),
    )

    feishu = ChannelCase(
        name="feishu",
        make_transport=_transport_factory(
            FeishuTransport,
            FakeLarkClient,
            "allowed_open_ids",
            allow_group=False,
            allowed_group_ids=[],
        ),
        make_dispatcher=_dispatcher_factory(FeishuDispatcher, "feishu", FakeLarkClient),
        section_fields=_fields_factory("allowed_open_ids", allow_group=False, allowed_group_ids=[]),
        boot=("ou_abc",),
        added=("ou_new",),
        expect_boot=frozenset({"ou_abc"}),
        expect_added=frozenset({"ou_new"}),
        boot_id="ou_abc",
        added_id="ou_new",
        authorize=_authorizer("feishu", "msg1"),
        roster_key="allowed_open_ids",
        audit_fields={
            "allowed_open_ids": ["ou_secret"],
            "allow_group": True,
            "allowed_group_ids": ["oc_room"],
        },
        audit_outcomes=("allow_list_changed", "group_allow_list_changed", "allow_group_enabled"),
        audit_secret="ou_secret",
        toggle_key="allow_group",
        toggle_reader=lambda t: t._allow_group,
        dm_scope_probe=_generation_probe(
            lambda d: d._session_key(("direct", "ou_abc")), ("direct", "ou_abc")
        ),
        inbound=lambda: LarkInbound(
            open_id="ou_abc", text="hi", message_id="m1", chat_type=CHAT_P2P, chat_id=""
        ),
    )

    return (telegram, discord, webex, teams, imessage, whatsapp, wecom, weixin, feishu)


def cases_with_toggle() -> tuple[ChannelCase, ...]:
    """Channels carrying a boolean gate half beside their roster."""
    return tuple(c for c in channel_cases() if c.toggle_key)


def cases_with_dm_scope() -> tuple[ChannelCase, ...]:
    """Channels whose session key is namespaced by ``messaging.dm_scope``."""
    return tuple(c for c in channel_cases() if c.dm_scope_probe is not None)


def cases_with_inbound() -> tuple[ChannelCase, ...]:
    """Channels whose ``_maybe_notice`` can be driven with a fake inbound."""
    return tuple(c for c in channel_cases() if c.inbound is not None)


# ------------------------------------------------------------------
# The two-half group gate
# ------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GatedCase:
    """A channel whose group/forum admission is a conjunction of two fields.

    ``build`` returns the transport plus the gate as an awaitable predicate, so
    a gate asserted through ``receive`` (Feishu) and one read off the resolved
    attributes (Telegram, Webex) are asked the same question.
    """

    name: str
    build: Callable[[], tuple[Any, Callable[[], Any]]]
    section: Callable[[bool, Sequence[str]], SimpleNamespace]
    target_id: str
    other_id: str


@functools.lru_cache(maxsize=1)
def gated_cases() -> tuple[GatedCase, ...]:
    from kiro_crew.feishu.client import CHAT_GROUP, LarkInbound
    from kiro_crew.feishu.transport import FeishuTransport
    from kiro_crew.telegram.transport import TelegramTransport, forum_gate_outcome
    from kiro_crew.webex.transport import WebexTransport

    def telegram_build() -> tuple[Any, Callable[[], Any]]:
        t = TelegramTransport(FakeClient(), allowed_user_ids=[7])

        async def gate() -> bool:
            return (
                forum_gate_outcome(
                    "supergroup",
                    -100,
                    5,
                    allow_forum=t._allow_forum,
                    allowed_forum_chat_ids=t._allowed_forum_chat_ids,
                )
                is None
            )

        return t, gate

    def webex_build() -> tuple[Any, Callable[[], Any]]:
        t = WebexTransport(FakeClient(), allowed_emails=["a@example.com"])

        async def gate() -> bool:
            # The gate as ``room_permitted`` and ``may_send_to`` both spell it.
            return (
                t._allow_group_rooms and "oc_room" in t._allowed_rooms and t.may_send_to("oc_room")
            )

        return t, gate

    def feishu_build() -> tuple[Any, Callable[[], Any]]:
        dispatched: list[Any] = []

        async def _dispatch(inbound: Any) -> None:
            dispatched.append(inbound)

        t = FeishuTransport(FakeLarkClient(), allowed_open_ids=["ou_abc"], dispatch=_dispatch)
        group = LarkInbound(
            open_id="ou_abc",
            text="hi",
            message_id="m1",
            chat_type=CHAT_GROUP,
            chat_id="oc_room",
        )

        async def gate() -> bool:
            before = len(dispatched)
            await t.receive(group)
            return len(dispatched) > before

        return t, gate

    return (
        GatedCase(
            name="telegram",
            build=telegram_build,
            section=lambda on, ids: SimpleNamespace(
                allowed_user_ids=[7], allow_forum=on, allowed_forum_chat_ids=list(ids)
            ),
            target_id=-100,  # type: ignore[arg-type]
            other_id=-999,  # type: ignore[arg-type]
        ),
        GatedCase(
            name="webex",
            build=webex_build,
            section=lambda on, ids: SimpleNamespace(
                allowed_emails=["a@example.com"],
                allow_group_rooms=on,
                allowed_room_ids=list(ids),
            ),
            target_id="oc_room",
            other_id="oc_other",
        ),
        GatedCase(
            name="feishu",
            build=feishu_build,
            section=lambda on, ids: SimpleNamespace(
                allowed_open_ids=["ou_abc"], allow_group=on, allowed_group_ids=list(ids)
            ),
            target_id="oc_room",
            other_id="oc_other",
        ),
    )


# ------------------------------------------------------------------
# Roster normalizers
# ------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def normalizer_cases() -> tuple[tuple[str, Callable[[Any], Any], Any, Any], ...]:
    """``(id, fn, raw, expected)`` for the three roster normalizers.

    One contract, three pure functions: usable entries are normalized and kept,
    unusable ENTRIES are dropped, and an unusable WHOLE VALUE answers ``None``
    so the caller keeps its previous roster instead of emptying it.
    """
    from kiro_crew.imessage.transport import allowed_handles_from_config
    from kiro_crew.teams.transport import allowed_emails_from_config
    from kiro_crew.wecom.transport import allowed_userids_from_config

    return (
        (
            "teams-normalizes",
            allowed_emails_from_config,
            ["A@x.com", "", 7, "b@X.com"],
            ["a@x.com", "b@x.com"],
        ),
        ("teams-non-list", allowed_emails_from_config, "a@x.com", None),
        ("teams-none", allowed_emails_from_config, None, None),
        (
            "imessage-normalizes",
            allowed_handles_from_config,
            ["+1 (555) 010-0000", "", 7],
            ["+15550100000"],
        ),
        ("imessage-non-list", allowed_handles_from_config, "+15550100000", None),
        ("imessage-none", allowed_handles_from_config, None, None),
        (
            "wecom-flattens",
            allowed_userids_from_config,
            [{"userid": "Wei"}, {"name": "x"}, "junk"],
            ["Wei"],
        ),
        ("wecom-non-list", allowed_userids_from_config, "Wei", None),
        ("wecom-none", allowed_userids_from_config, None, None),
    )
