"""Tests for the Slack guest allowlist: admission, routing and capability limits.

An allow-listed guest is a non-owner who may reach ONE thing — the inbound message
gate in a tracked channel. These tests pin that, and pin the boundary around it:
every owner control still refuses them, the turn resolves a memory store that is
not the owner's or does not run, and its tools are deny-by-default.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.agent_files import GUEST_AGENT_NAME
from kiro_crew.config.loader import (
    ACTIVATION_ALWAYS,
    ACTIVATION_MENTION,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    ChannelConfig,
    KiroCrewConfig,
    MessagingConfig,
)
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE
from kiro_crew.messaging.link import canonical_key
from kiro_crew.slack.events import (
    SeenCache,
    _denial_ephemeral,
    _resolve_approval_mode,
    _route_message,
)
from kiro_crew.slack.handler import (
    APPROVAL_AUTO,
    APPROVAL_INTERACTIVE,
    guest_member_configured,
    guest_session_key,
    is_allowed_user,
    is_guest_session_key,
    is_guest_user,
    maybe_handle_keyword_command,
    resolve_guest_agent,
    set_allowed_users,
    set_owner_id,
    set_tracking_channels,
)
from kiro_crew.slack.tool_gate import (
    GUEST_SAFE_TOOLS,
    build_guest_hooks,
    is_guest_safe_tool,
)

OWNER = "U0OWNER01"
GUEST = "U0GUEST01"
STRANGER = "U0STRANGE"
TRACKED = "C0TRACKED"
UNTRACKED = "C0UNTRACK"
GUEST_MEMBER = "guest-member"


@pytest.fixture(autouse=True)
def _identities():
    """Owner set, guest allow-listed, one tracked channel."""
    set_owner_id(OWNER)
    set_allowed_users({OWNER, GUEST})
    set_tracking_channels({TRACKED})
    yield
    set_owner_id("")
    set_allowed_users(set())
    set_tracking_channels(set())


def _cfg(activation: str = ACTIVATION_MENTION, *, guest_agent: str = GUEST_MEMBER, **kw):
    """Config with TRACKED at *activation* and a guest member configured."""
    cfg = KiroCrewConfig(
        slack_channels={
            TRACKED: ChannelConfig(activation=activation),
            UNTRACKED: ChannelConfig(activation=activation),
        },
        messaging=MessagingConfig(use_transport=False),
        **kw,
    )
    cfg.slack.guest_agent = guest_agent
    if guest_agent:
        # Both halves item 2's predicate demands: a store that is not the owner's,
        # and a binding to the generated guest spec. A member missing either is
        # refused admission, so a fixture setting only one would test the refusal.
        cfg.agents[guest_agent] = MagicMock(
            memory_store="guest-store", kiro_agent=GUEST_AGENT_NAME, member_id=""
        )
    return cfg


def _make_orch(cfg=None) -> MagicMock:
    orch = MagicMock()
    orch._cfg = cfg if cfg is not None else _cfg()
    orch.channel_history = MagicMock()
    orch.slack = MagicMock()
    orch.slack.post_ephemeral = AsyncMock()
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_busy = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.clear_queue = MagicMock()
    orch.sessions.has_session = MagicMock(return_value=False)
    orch.sessions.get_session_for_thread = MagicMock(return_value=None)
    orch.ctx_builder = None
    orch.cron_svc = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch.task_runner = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    orch._approval_mode = None
    return orch


async def _route(orch, *, channel=TRACKED, user=GUEST, is_mention=True, ts="10.0", thread=None):
    """Drive one inbound message and return the handle_message mock."""
    seen = SeenCache()
    event = {"user": user, "channel": channel, "text": "hi", "ts": ts, "team": "TTEST"}
    if thread:
        event["thread_ts"] = thread
    with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
        await _route_message(orch, event, seen, is_mention=is_mention)
        await asyncio.sleep(0)
        await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)
        return hm


# ───────────────────────── the two predicates are disjoint ─────────────────────


class TestPredicatesAreDisjoint:
    def test_guest_is_not_an_allowed_user(self):
        assert is_guest_user(GUEST) is True
        assert is_allowed_user(GUEST) is False

    def test_owner_is_not_a_guest(self):
        assert is_allowed_user(OWNER) is True
        assert is_guest_user(OWNER) is False

    def test_stranger_is_neither(self):
        assert is_guest_user(STRANGER) is False
        assert is_allowed_user(STRANGER) is False


# ───────────────────────────── inbound admission ───────────────────────────────


class TestGuestAdmission:
    @pytest.mark.asyncio
    async def test_guest_mention_in_tracked_channel_is_answered(self):
        orch = _make_orch()
        hm = await _route(orch)
        hm.assert_called_once()
        assert hm.call_args.kwargs["guest_user"] == GUEST

    @pytest.mark.asyncio
    @pytest.mark.parametrize("activation", [ACTIVATION_MENTION, ACTIVATION_ALWAYS])
    async def test_guest_is_dispatched_with_its_guest_flag(self, activation):
        """Admission passes the guest id on; WHICH AGENT runs is a seam test.

        ``channel_agent`` is a ``config.agents`` key that the layer below re-derives,
        and the provider never sees it, so an assertion on it here says nothing about
        the agent that runs. The agent that crosses into the session layer is pinned
        in ``TestGuestAgentSeam``; what this pins is that the guest id travels.
        """
        orch = _make_orch(_cfg(activation))
        hm = await _route(orch)
        hm.assert_called_once()
        assert hm.call_args.kwargs["guest_user"] == GUEST

    @pytest.mark.asyncio
    async def test_guest_denied_in_untracked_channel(self):
        orch = _make_orch()
        hm = await _route(orch, channel=UNTRACKED)
        hm.assert_not_called()
        assert "not one the owner tracks" in orch.slack.post_ephemeral.call_args[0][2]

    @pytest.mark.asyncio
    async def test_guest_denied_in_review_activation(self):
        orch = _make_orch(_cfg(ACTIVATION_REVIEW))
        hm = await _route(orch)
        hm.assert_not_called()
        assert "does not answer guests" in orch.slack.post_ephemeral.call_args[0][2]

    @pytest.mark.asyncio
    async def test_guest_denied_in_off_activation(self):
        orch = _make_orch(_cfg(ACTIVATION_OFF))
        hm = await _route(orch)
        hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_guest_denied_in_a_dm(self):
        orch = _make_orch(_cfg(ACTIVATION_ALWAYS))
        hm = await _route(orch, channel="D0GUESTDM")
        hm.assert_not_called()
        assert "not in DMs" in orch.slack.post_ephemeral.call_args[0][2]

    @pytest.mark.asyncio
    async def test_guest_denied_in_a_thread_that_is_not_their_own(self):
        """An owner's thread holds an owner session, and one thread has one owner."""
        orch = _make_orch()
        orch.sessions.get_session_for_thread = MagicMock(return_value="slack:9.0")
        hm = await _route(orch, is_mention=False, ts="11.0", thread="9.0")
        hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_guest_followed_up_in_their_own_thread(self):
        orch = _make_orch()
        orch.sessions.get_session_for_thread = MagicMock(
            return_value=guest_session_key(GUEST, "9.0")
        )
        hm = await _route(orch, is_mention=False, ts="11.0", thread="9.0")
        hm.assert_called_once()

    @pytest.mark.asyncio
    async def test_stranger_still_refused_with_allowlist_advice(self):
        orch = _make_orch()
        hm = await _route(orch, user=STRANGER)
        hm.assert_not_called()
        assert "add you to the allowlist" in orch.slack.post_ephemeral.call_args[0][2]


# ─────────────────────── the memory-store refusal (no leak) ────────────────────


class TestGuestMemberRefusal:
    def test_unconfigured_guest_member_is_refused(self):
        cfg = _cfg(guest_agent="")
        assert resolve_guest_agent(cfg, TRACKED) == ""
        assert guest_member_configured(cfg, "") is False

    def test_a_name_absent_from_agents_is_refused(self):
        """The exact shape that would bind a guest to the owner's DEFAULT store."""
        cfg = _cfg(guest_agent="not-a-member")
        cfg.agents.pop("not-a-member", None)
        assert resolve_guest_agent(cfg, TRACKED) == "not-a-member"
        assert guest_member_configured(cfg, "not-a-member") is False
        assert guest_member_configured(cfg, DEFAULT_MEMORY_STORE) is False

    @pytest.mark.asyncio
    async def test_guest_turn_does_not_run_without_a_member(self):
        orch = _make_orch(_cfg(guest_agent=""))
        hm = await _route(orch)
        hm.assert_not_called()
        assert "guest agent for this channel is not" in orch.slack.post_ephemeral.call_args[0][2]

    def test_per_channel_guest_agent_overrides_the_global(self):
        cfg = _cfg()
        cfg.slack_channels[TRACKED].guest_agent = "channel-member"
        assert resolve_guest_agent(cfg, TRACKED) == "channel-member"
        # A channel with no override falls back to the global.
        assert resolve_guest_agent(cfg, UNTRACKED) == GUEST_MEMBER

    def test_both_paths_converge_on_the_same_refusal(self):
        """An unresolvable name refuses whether it came from the channel or global."""
        cfg = _cfg(guest_agent="")
        cfg.slack_channels[TRACKED].guest_agent = "ghost-member"
        assert resolve_guest_agent(cfg, TRACKED) == "ghost-member"
        assert guest_member_configured(cfg, "ghost-member") is False
        assert resolve_guest_agent(cfg, UNTRACKED) == ""
        assert guest_member_configured(cfg, "") is False


# ─────────────────────────── guest session isolation ──────────────────────────


class TestGuestSessionKey:
    def test_guest_key_is_not_the_owner_thread_key(self):
        from kiro_crew.messaging.link import canonical_key

        assert guest_session_key(GUEST, "5.0") != canonical_key("5.0")

    def test_two_guests_do_not_share_a_key(self):
        assert guest_session_key(GUEST, "5.0") != guest_session_key(STRANGER, "5.0")

    def test_guest_key_is_not_mistaken_for_a_bare_timestamp(self):
        from kiro_crew.messaging.link import legacy_key

        assert legacy_key(guest_session_key(GUEST, "5.0")) is None


# ─────────────────── YOLO: two independent block points ───────────────────────


class TestYoloBlockPointOne:
    """``_resolve_approval_mode`` alone, with the hooks layer out of the picture."""

    def test_owner_turn_under_yolo_is_auto(self):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_yolo_mode", return_value=True):
            assert _resolve_approval_mode(orch) == APPROVAL_AUTO

    def test_guest_turn_under_yolo_is_interactive(self):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_yolo_mode", return_value=True):
            assert _resolve_approval_mode(orch, guest=True) == APPROVAL_INTERACTIVE

    def test_guest_turn_is_interactive_even_with_configured_auto(self):
        orch = _make_orch()
        orch._approval_mode = APPROVAL_AUTO
        with patch("kiro_crew.slack.events.is_yolo_mode", return_value=False):
            assert _resolve_approval_mode(orch, guest=True) == APPROVAL_INTERACTIVE


class TestYoloBlockPointTwo:
    """The hooks layer alone: the owner's auto-approve list cannot grant."""

    def test_guest_hooks_drop_auto_approve_but_keep_deny(self):
        from kiro_crew.hooks import HookManager, HooksConfig

        owner_hooks = HookManager(HooksConfig(auto_approve_tools=["*"], auto_deny_tools=["curl*"]))
        guest_hooks = build_guest_hooks(owner_hooks)
        assert owner_hooks._config.auto_approve_tools == ["*"]
        assert guest_hooks._config.auto_approve_tools == []
        assert guest_hooks._config.auto_deny_tools == ["curl*"]

    def test_owner_wildcard_auto_approves_and_guest_does_not(self):
        """Same tool, same owner config, opposite verdicts from the hook gate."""
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, HooksConfig

        owner_hooks = HookManager(HooksConfig(auto_approve_tools=["*"]))
        guest_hooks = build_guest_hooks(owner_hooks)
        assert owner_hooks.on_tool_call("shell").action == TOOL_AUTO_APPROVE
        assert guest_hooks.on_tool_call("shell").action != TOOL_AUTO_APPROVE


# ─────────────────────── the guest tool allowlist ─────────────────────────────


class TestGuestSafeTools:
    @pytest.mark.parametrize("tool", sorted(GUEST_SAFE_TOOLS))
    def test_listed_tools_are_allowed(self, tool):
        assert is_guest_safe_tool(tool, "") is True

    @pytest.mark.parametrize(
        "tool",
        [
            "web_fetch",
            "shell",
            "use_aws",
            "Read",
            "Write",
            "Grep",
            "Glob",
            "spawn_run",
            "send_message",
            "cron_add",
            "memory_recall",
            "local_knowledge_search",
            "artifact_get",
            "learn_add",
        ],
    )
    def test_owner_reach_tools_are_refused(self, tool):
        assert is_guest_safe_tool(tool, "") is False

    def test_the_set_holds_exactly_one_entry(self):
        """A guest chooses words, never a destination."""
        assert GUEST_SAFE_TOOLS == frozenset({"web_search"})

    @pytest.mark.parametrize(
        "name",
        [
            "web",
            "web_",
            "web_sea",
            "web_search_admin",
            "web_search_raw",
            "unsafe_web_search",
            "my_web_search",
            "WEB_SEARCH",
            "Web_Search",
        ],
    )
    def test_only_the_exact_name_is_admitted(self, name):
        """A neighbour of the allowed name is a DIFFERENT tool and is refused.

        The match is exact, so neither a prefix of the allowed name nor a name
        containing it is admitted. A substring or case-insensitive comparison here
        would let a server expose ``web_search_raw`` and inherit the grant.
        """
        assert is_guest_safe_tool(name, "") is False
        # CONTROL: the exact name still passes, so these are refusals of the
        # neighbours rather than a matcher that admits nothing.
        assert is_guest_safe_tool("web_search", "") is True

    def test_web_fetch_is_refused_because_the_guest_picks_the_host(self):
        """``web_fetch`` takes a URL, so admitting it is admitting an arbitrary GET.

        Paired with the control below so an all-refused result cannot be mistaken
        for a matcher that matches nothing.
        """
        assert is_guest_safe_tool("web_fetch", "") is False
        # CONTROL: the gate does answer True for something, so the refusals above
        # are refusals and not a dead matcher.
        assert is_guest_safe_tool("web_search", "") is True

    @pytest.mark.parametrize(
        "name",
        [
            "web_fetch",
            "WebFetch",
            "fetch",
            "http_get",
            "curl",
        ],
    )
    def test_no_spelling_of_a_fetch_tool_is_admitted(self, name):
        """Every name a URL-taking tool could arrive under is refused."""
        assert is_guest_safe_tool(name, "") is False

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "http://127.0.0.1:5476/api/state",
            "http://localhost:8080/admin",
            "http://[::1]:5476/",
            "http://metadata.google.internal/computeMetadata/v1/",
            "file:///etc/passwd",
        ],
    )
    def test_a_guest_supplied_internal_url_reaches_no_tool(self, url):
        """The destination never matters, because no tool accepting one is approved.

        The gate keys on the tool identity, so a link-local or loopback address is
        unreachable for a guest whether or not the fetch builtin guards it. That is
        why the exclusion lives in the allowlist rather than in a URL filter.
        """
        for name in (f"web_fetch({url})", "web_fetch", f"web_fetch {url}"):
            assert is_guest_safe_tool(name, "") is False
        # CONTROL: a search for the same string is still permitted, so the
        # refusals above are about the tool and not about the text.
        assert is_guest_safe_tool("web_search", "") is True

    def test_read_shaped_invented_names_are_refused(self):
        for name in ("get_all_credentials", "list_env_secrets", "read_owner_memory"):
            assert is_guest_safe_tool(name, "") is False

    @pytest.mark.parametrize(
        "title",
        [
            "Running: web_search",
            "mcp__srv__web_search",
            "@srv/web_search",
            "Running: @srv/web_search",
            "Searching the web for web_search",
        ],
    )
    def test_a_wire_TITLE_spelling_is_refused(self, title):
        """The gate reads a verified identity, so a title-shaped string is not one.

        ``event.title`` is LLM-authored prose, and these are the forms a title
        arrives in. None of them is what the harness stamps as ``tool_name``, so
        each is refused -- a gate that unwrapped them would be reading the model's
        own words, and a crafted title would route onto the one safe name.
        """
        assert is_guest_safe_tool(title, "") is False
        # CONTROL: the verified spelling of the same tool passes, so this is a
        # refusal of the title FORM and not of the tool.
        assert is_guest_safe_tool("web_search", "") is True

    def test_an_mcp_served_tool_is_refused_even_under_the_allowed_name(self):
        """The guest spec mounts no MCP servers, so any server naming one is not it.

        Without this, a second server exposing its own ``web_search`` would inherit
        the grant -- the bare-name hazard ``normalize_tool_title`` documents.
        """
        assert is_guest_safe_tool("web_search", "srv") is False
        assert is_guest_safe_tool("web_search", "evil-server") is False
        # CONTROL: the same name with no server is the builtin, and passes.
        assert is_guest_safe_tool("web_search", "") is True

    def test_an_absent_identity_is_refused(self):
        """An unverified call is not a safe one, and there is no title fallback."""
        assert is_guest_safe_tool("", "") is False
        assert is_guest_safe_tool("   ", "") is False
        # Absent identity plus a server name is still absent.
        assert is_guest_safe_tool("", "srv") is False

    def test_the_gate_reads_the_verified_identity_not_the_title(self):
        """The call site must pass ``tool_name``/``mcp_server_name``, never ``title``.

        A behavioural test cannot see which FIELD the handler read, so this reads
        the call site itself. The needle is built from parts so this assertion's own
        line cannot match and report itself as the offender.
        """
        import inspect

        from kiro_crew.slack import handler as _handler

        src = inspect.getsource(_handler)
        good = "is_guest_safe_tool(" + "event.tool_name, " + "event.mcp_server_name)"
        bad = "is_guest_safe_tool(" + "event." + "title)"
        assert src.count(good) == 1, "the guest gate must key on the verified identity"
        assert bad not in src, "the guest gate must not key on the model-authored title"

    def test_guest_set_does_not_inherit_the_heartbeat_filesystem_reads(self):
        """Heartbeat is the owner's own session; a guest is a different person."""
        from kiro_crew.slack.gateway import HEARTBEAT_SAFE_TOOLS

        assert {"Read", "Grep", "Glob"} <= HEARTBEAT_SAFE_TOOLS
        assert not ({"Read", "Grep", "Glob"} & GUEST_SAFE_TOOLS)

    def test_the_guest_agent_spec_does_not_advertise_a_refused_tool(self):
        """The prompt must not offer what the gate rejects, or a turn is wasted."""
        from kiro_crew.agent import _GUEST_SYSTEM_PROMPT

        assert "web search" in _GUEST_SYSTEM_PROMPT
        assert "web fetch" not in _GUEST_SYSTEM_PROMPT


# ─────────────────── every owner control still refuses a guest ────────────────


class TestOwnerControlsRefuseGuests:
    """One case per class of control named in the contract."""

    @pytest.mark.asyncio
    async def test_bang_command_refuses_a_guest(self):
        """``!`` commands gate on the owner predicate, which a guest fails."""
        from kiro_crew.slack import handler as handler_mod

        assert handler_mod.is_allowed_user(GUEST) is False
        assert handler_mod.is_owner(GUEST) is False

    def test_interaction_buttons_gate_on_the_owner_predicate(self):
        """Every interactions.py gate reads is_allowed_user, never is_guest_user."""
        import inspect

        from kiro_crew.slack import interactions

        src = inspect.getsource(interactions)
        assert "is_guest_user" not in src
        assert src.count("is_allowed_user(user_id)") >= 8

    def test_home_tab_gates_on_the_owner_predicate(self):
        import inspect

        from kiro_crew.slack import events

        # The home-tab branch lives inside the socket-mode handler.
        src = inspect.getsource(events.init_socket_mode)
        assert "app_home_opened" in src
        assert "is_allowed_user(user)" in src
        assert "is_guest_user" not in src

    def test_presigned_dashboard_link_gates_on_the_owner_predicate(self):
        import inspect

        from kiro_crew.dashboard.handlers import messaging

        src = inspect.getsource(messaging)
        assert "is_guest_user" not in src

    def test_the_guest_predicate_reaches_only_the_message_gate(self):
        """``is_guest_user`` is consulted where messages arrive, and nowhere else."""
        import inspect

        from kiro_crew.slack import events, interactions

        assert "is_guest_user" in inspect.getsource(events._route_message)
        assert "is_guest_user" not in inspect.getsource(interactions)


# ─────────────────────── the owner keeps what they had ────────────────────────


class TestOwnerUnchanged:
    @pytest.mark.asyncio
    async def test_owner_mention_still_answered_with_no_guest_marker(self):
        orch = _make_orch()
        hm = await _route(orch, user=OWNER)
        hm.assert_called_once()
        assert hm.call_args.kwargs["guest_user"] == ""

    @pytest.mark.asyncio
    async def test_owner_in_an_untracked_channel_is_still_answered(self):
        """Tracking gates GUESTS. The owner's reach is unchanged by this feature."""
        orch = _make_orch()
        hm = await _route(orch, channel=UNTRACKED, user=OWNER)
        hm.assert_called_once()

    @pytest.mark.asyncio
    async def test_owner_dm_still_answered(self):
        orch = _make_orch(_cfg(ACTIVATION_ALWAYS))
        hm = await _route(orch, channel="D0OWNERDM", user=OWNER)
        hm.assert_called_once()

    @pytest.mark.asyncio
    async def test_owner_in_review_activation_still_dispatches(self):
        orch = _make_orch(_cfg(ACTIVATION_REVIEW))
        hm = await _route(orch, user=OWNER)
        hm.assert_called_once()

    def test_owner_predicate_is_unchanged_by_the_allowlist(self):
        """A longer allowlist does not widen who the owner predicate answers for."""
        set_allowed_users({OWNER, GUEST, STRANGER})
        assert is_allowed_user(OWNER) is True
        assert is_allowed_user(GUEST) is False
        assert is_allowed_user(STRANGER) is False


# ─────────────────────── denial text names the real cause ─────────────────────


class TestDenialText:
    def test_a_stranger_is_told_about_the_allowlist(self):
        assert "add you to the allowlist" in _denial_ephemeral(False, "")

    @pytest.mark.parametrize(
        "reason,fragment",
        [
            ("guest_dm_not_supported", "not in DMs"),
            ("guest_channel_not_tracked", "not one the owner tracks"),
            ("guest_thread_not_own", "Post a new message in the channel"),
            ("guest_member_not_configured", "guest agent for this channel is not"),
            ("guest_denied_in_activation_review", "does not answer guests"),
        ],
    )
    def test_each_guest_reason_names_itself(self, reason, fragment):
        text = _denial_ephemeral(True, reason)
        assert fragment in text
        # A guest is never sent to ask for the allowlist they are already on.
        assert "add you to the allowlist" not in text


# ───────────────────────── per-store lessons ──────────────────────────────────


class TestGuestLessons:
    def test_a_named_store_has_its_own_lessons_directory(self):
        """A guest member's store is NAMED, so its lessons live outside the workspace.

        ``LessonStore`` reads ``lessons.jsonl`` under the base directory it is
        given, so distinct base directories are distinct lesson files.
        """
        from kiro_crew.memory import workspace_dir
        from kiro_crew.memory_stores import _named_store_dir

        guest_dir = _named_store_dir("guest-store")
        assert guest_dir != workspace_dir()
        assert workspace_dir() not in guest_dir.parents
        assert guest_dir.name == "guest-store"

    def test_get_lessons_for_branches_on_the_named_store(self):
        """The resolver reaches a named store's own directory, not the workspace."""
        import inspect

        from kiro_crew.context import ContextBuilder

        src = inspect.getsource(ContextBuilder.get_lessons_for)
        assert "ensure_memory_store_dir(store_name)" in src
        assert "workspace_dir_for(workspace or _DEFAULT_KEY)" in src
        # The named branch is taken whenever a store name is present.
        assert "if store_name:" in src

    def test_guest_store_is_not_the_default_store(self):
        """A config-level fact only. What the TURN resolves is ``TestGuestStoreSeam``.

        This and the assertion below say the configuration is shaped right. They do
        not say the guest turn resolves that store, and a guest turn silently
        resolving the owner's default is exactly what they missed.
        """
        cfg = _cfg()
        assert cfg.agents[GUEST_MEMBER].memory_store != DEFAULT_MEMORY_STORE

    def test_default_store_name_would_be_the_owner_store(self):
        """Why the refusal exists: the fallback store name IS the owner's."""
        assert DEFAULT_MEMORY_STORE == "default"
        cfg = _cfg(guest_agent="")
        assert guest_member_configured(cfg, DEFAULT_MEMORY_STORE) is False


# ──────────────────── guests never take the transport path ────────────────────


class TestGuestStaysNative:
    @pytest.mark.asyncio
    async def test_transport_refuses_a_guest_turn(self):
        from kiro_crew.slack.transport_dispatch import handle_message_transport

        sessions = AsyncMock()
        slack = MagicMock()
        await handle_message_transport(
            slack, sessions, TRACKED, "hi", None, "1.0", GUEST, guest_user=GUEST
        )
        # Refused before any session work.
        sessions.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_guest_dispatches_native_even_with_transport_enabled(self):
        cfg = _cfg(ACTIVATION_MENTION)
        cfg.messaging.use_transport = True
        orch = _make_orch(cfg)
        with patch(
            "kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock
        ) as transport:
            hm = await _route(orch)
        hm.assert_called_once()
        transport.assert_not_called()


# ───────── downstream: every owner-keyed mechanism the guest path reaches ──────


class TestGuestMemberMustBeIsolatedAndRestricted:
    """Membership alone proves neither half of the posture (contract item 2)."""

    def test_a_member_omitting_memory_store_is_refused(self):
        """The field DEFAULTS to ``default``, which IS the owner's own store."""
        cfg = _cfg()
        cfg.agents[GUEST_MEMBER] = MagicMock(
            memory_store=DEFAULT_MEMORY_STORE, kiro_agent=GUEST_AGENT_NAME
        )
        assert guest_member_configured(cfg, GUEST_MEMBER) is False
        # CONTROL: the same member with a named store passes, so this refusal is
        # about the store and not about the lookup failing outright.
        cfg.agents[GUEST_MEMBER] = MagicMock(
            memory_store="guest-store", kiro_agent=GUEST_AGENT_NAME
        )
        assert guest_member_configured(cfg, GUEST_MEMBER) is True

    def test_a_member_with_a_blank_memory_store_is_refused(self):
        cfg = _cfg()
        cfg.agents[GUEST_MEMBER] = MagicMock(memory_store="", kiro_agent=GUEST_AGENT_NAME)
        assert guest_member_configured(cfg, GUEST_MEMBER) is False

    def test_a_member_bound_to_another_spec_is_refused(self):
        """Only the guest spec mounts no MCP servers, so only it is restricted.

        A permission-time gate cannot see a spec's ``allowedTools`` pre-approval --
        the harness raises no permission request for a pre-approved tool -- so this
        predicate is the only place a wrongly-bound spec can be stopped.
        """
        cfg = _cfg()
        for spec in ("kirocrew", "kirocrew-worker", "kirocrew-heartbeat", ""):
            cfg.agents[GUEST_MEMBER] = MagicMock(memory_store="guest-store", kiro_agent=spec)
            assert guest_member_configured(cfg, GUEST_MEMBER) is False, spec
        cfg.agents[GUEST_MEMBER] = MagicMock(
            memory_store="guest-store", kiro_agent=GUEST_AGENT_NAME
        )
        assert guest_member_configured(cfg, GUEST_MEMBER) is True

    @pytest.mark.asyncio
    async def test_a_misconfigured_member_denies_admission_loudly(self):
        """An owner misconfiguration refuses the turn; it never downgrades it."""
        cfg = _cfg()
        cfg.agents[GUEST_MEMBER] = MagicMock(
            memory_store=DEFAULT_MEMORY_STORE, kiro_agent=GUEST_AGENT_NAME
        )
        orch = _make_orch(cfg)
        hm = await _route(orch)
        hm.assert_not_called()
        assert "guest agent for this channel is not" in orch.slack.post_ephemeral.call_args[0][2]


class TestGuestKeywordCommands:
    """``spawn``/``run``/``cron`` run with no caller check and before any LLM turn."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        ["spawn go read the config", "run /etc/spec.yaml", "cron remove all", "sessions"],
    )
    async def test_a_guest_reaches_no_keyword_command(self, text):
        slack = MagicMock()
        slack.post_message = AsyncMock()
        subagents = MagicMock()
        runner = MagicMock()
        cron = MagicMock()
        handled = await maybe_handle_keyword_command(
            text,
            slack,
            AsyncMock(),
            TRACKED,
            "1.0",
            "1.0",
            guest_session_key(GUEST, "1.0"),
            GUEST,
            None,
            subagent_manager=subagents,
            task_runner=runner,
            cron_service=cron,
            guest_user=GUEST,
        )
        assert handled is False
        slack.post_message.assert_not_called()
        # The services are never touched, so no command ran and none was refused
        # with a reply either -- the text goes on as ordinary chat.
        assert not subagents.method_calls
        assert not runner.method_calls
        assert not cron.method_calls

    @pytest.mark.asyncio
    async def test_the_owner_still_reaches_the_sessions_command(self):
        """CONTROL: the refusal is the guest flag, not a helper that handles nothing."""
        slack = MagicMock()
        slack.post_message = AsyncMock()
        sessions = AsyncMock()
        sessions.list_sessions = MagicMock(return_value=[])
        handled = await maybe_handle_keyword_command(
            "sessions", slack, sessions, TRACKED, "1.0", "1.0", "slack:1.0", OWNER, None
        )
        assert handled is True


class TestGuestChannelHistory:
    """Guest text must not enter the buffer an OWNER turn reads as context."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("activation", [ACTIVATION_MENTION, ACTIVATION_ALWAYS])
    async def test_a_guest_message_is_not_pushed_to_channel_history(self, activation):
        orch = _make_orch(_cfg(activation))
        hm = await _route(orch)
        hm.assert_called_once()  # admitted, so this is a skip and not a denial
        orch.channel_history.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_owner_message_is_still_pushed(self):
        """CONTROL: the push still happens, so the guest case is a real exclusion."""
        orch = _make_orch(_cfg(ACTIVATION_ALWAYS))
        hm = await _route(orch, user=OWNER)
        hm.assert_called_once()
        orch.channel_history.push.assert_called_once()


class TestGuestThreadClaimIsInvisibleToTheOwner:
    """A guest's index claim must not re-route an OWNER's later reply."""

    def test_the_guest_key_is_recognisable(self):
        assert is_guest_session_key(guest_session_key(GUEST, "9.0")) is True
        assert is_guest_session_key("slack:9.0") is False
        assert is_guest_session_key("dashboard:chat-1") is False
        assert is_guest_session_key("") is False

    def test_an_owner_turn_does_not_adopt_a_guest_claim(self):
        from kiro_crew.slack.handler import visible_thread_owner

        sessions = MagicMock()
        sessions.get_session_for_thread = MagicMock(return_value=guest_session_key(GUEST, "9.0"))
        assert visible_thread_owner(sessions, "9.0", "") is None

    def test_the_guest_itself_still_sees_its_own_claim(self):
        from kiro_crew.slack.handler import visible_thread_owner

        sessions = MagicMock()
        sessions.get_session_for_thread = MagicMock(return_value=guest_session_key(GUEST, "9.0"))
        assert visible_thread_owner(sessions, "9.0", GUEST) == guest_session_key(GUEST, "9.0")

    def test_a_dashboard_claim_is_untouched_for_both(self):
        """CONTROL: only a GUEST claim is filtered, so real linking still works."""
        from kiro_crew.slack.handler import visible_thread_owner

        sessions = MagicMock()
        sessions.get_session_for_thread = MagicMock(return_value="dashboard:chat-7")
        assert visible_thread_owner(sessions, "9.0", "") == "dashboard:chat-7"
        assert visible_thread_owner(sessions, "9.0", GUEST) == "dashboard:chat-7"


class TestGuestMentionInSomebodyElsesThread:
    @pytest.mark.asyncio
    async def test_a_guest_mention_inside_an_owner_thread_is_refused(self):
        """The reachability premise under the whole downstream class.

        A bare ``is_mention`` admitted this, and the admitted turn is the one that
        keys to the owner's session and answers inside the owner's thread.
        """
        orch = _make_orch()
        orch.sessions.get_session_for_thread = MagicMock(return_value="slack:9.0")
        hm = await _route(orch, is_mention=True, ts="11.0", thread="9.0")
        hm.assert_not_called()
        assert "Post a new message in the channel" in orch.slack.post_ephemeral.call_args[0][2]

    @pytest.mark.asyncio
    async def test_a_guest_mention_inside_an_unclaimed_thread_is_refused(self):
        """Answering would self-link it, so the owner would find it already claimed."""
        orch = _make_orch()
        orch.sessions.get_session_for_thread = MagicMock(return_value=None)
        hm = await _route(orch, is_mention=True, ts="11.0", thread="9.0")
        hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_guest_mention_inside_another_guests_thread_is_refused(self):
        orch = _make_orch()
        orch.sessions.get_session_for_thread = MagicMock(
            return_value=guest_session_key("U0GUEST02", "9.0")
        )
        hm = await _route(orch, is_mention=True, ts="11.0", thread="9.0")
        hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_top_level_guest_mention_is_still_admitted(self):
        """CONTROL: the tightening refuses threads, not every mention."""
        orch = _make_orch()
        hm = await _route(orch, is_mention=True, ts="11.0", thread=None)
        hm.assert_called_once()


class TestGuestQueuedMessageKeepsItsIdentity:
    @pytest.mark.asyncio
    async def test_an_enqueued_guest_message_carries_guest_user(self):
        """``_dispatch_queued`` rebuilds the turn from the queue entry alone.

        Omitting the flag re-dispatched the guest's text as an OWNER turn: guest
        session key, guest hooks, guest tool gate and the YOLO refusal all key off
        this one value.
        """
        orch = _make_orch()
        orch.sessions.enqueue = MagicMock(return_value=True)
        await _route(orch)
        assert orch.sessions.enqueue.call_args.kwargs["guest_user"] == GUEST

    @pytest.mark.asyncio
    async def test_a_busy_guest_session_also_carries_it(self):
        orch = _make_orch()
        orch.sessions.enqueue = MagicMock(return_value=True)
        orch._session_tasks[guest_session_key(GUEST, "10.0")] = MagicMock()
        await _route(orch)
        assert orch.sessions.enqueue.call_args.kwargs["guest_user"] == GUEST

    @pytest.mark.asyncio
    async def test_an_owner_message_enqueues_with_no_guest_flag(self):
        """CONTROL: the value is the sender's identity, not a constant."""
        orch = _make_orch()
        orch.sessions.enqueue = MagicMock(return_value=True)
        await _route(orch, user=OWNER)
        assert orch.sessions.enqueue.call_args.kwargs["guest_user"] == ""


class TestGuestTurnThreadsItsFlagEverywhere:
    """A downstream mechanism that loses the flag loses every guest protection."""

    def test_the_compaction_replay_passes_guest_user(self):
        """The replay re-enters ``handle_message``; without the flag it is an owner.

        Needles built from parts so this assertion cannot match its own line.
        """
        import inspect

        from kiro_crew.slack import handler as _handler

        src = inspect.getsource(_handler.handle_message)
        assert src.count("guest_user=" + "guest_user") >= 1

    def test_every_handle_message_keyword_is_forwarded_by_the_replay(self):
        """Structural: the replay must forward the same keywords it received.

        A parameter added to ``handle_message`` later and not forwarded here would
        silently reset to its default on a replay -- which is exactly how
        ``guest_user`` went missing.
        """
        import inspect

        from kiro_crew.slack import handler as _handler

        params = set(inspect.signature(_handler.handle_message).parameters)
        src = inspect.getsource(_handler.handle_message)
        replay = src[src.index("_COMPACTION_RETRY_NOTICE") :]
        forwarded = {p for p in params if f"{p}=" in replay}
        # Positional/self-referential parameters the replay passes positionally or
        # constructs itself, plus the text it replays.
        exempt = {
            "slack",
            "sessions",
            "channel",
            "text",
            "thread_ts",
            "msg_ts",
            "user_id",
            "_compaction_replay",
            "context_builder",
            "cron_service",
            "conversation_log",
            "consolidator",
            "subagent_manager",
            "task_runner",
            "ctx",
        }
        missing = params - forwarded - exempt
        assert not missing, f"compaction replay drops: {sorted(missing)}"


# ───────────── end to end, through a REAL session index ───────────────────────


class TestGuestEndToEndThroughARealSessionIndex:
    """The component tests above gate each site in isolation, with a mocked index.

    That is precisely why a whole class of defects survived them: the sites agree
    with their own mocks and disagree with each other. These drive the REAL
    ``SessionMap``, so the self-derived/``setdefault`` behaviour that decides who
    holds a contested thread is the production one.
    """

    @staticmethod
    def _orch_with_real_index():
        from kiro_crew.session_map import SessionMap

        smap = SessionMap()
        orch = _make_orch()
        orch.sessions.get_session_for_thread = smap.get_session_for_thread
        orch.sessions.set_slack_link = smap.set_slack_link
        orch.sessions.has_session = MagicMock(return_value=False)
        return orch, smap

    @pytest.mark.asyncio
    async def test_mention_then_self_link_then_follow_up(self):
        orch, smap = self._orch_with_real_index()

        # 1. A top-level guest mention is admitted and keyed to the guest.
        hm = await _route(orch, is_mention=True, ts="100.0", thread=None)
        hm.assert_called_once()
        assert hm.call_args.kwargs["guest_user"] == GUEST
        expected = guest_session_key(GUEST, "100.0")

        # 2. The self-link the turn performs, against the real index.
        smap.set_slack_link(expected, "100.0", TRACKED)
        assert smap.get_session_for_thread("100.0") == expected

        # 3. A bare follow-up in that thread is admitted, decided by the real index.
        hm2 = await _route(orch, is_mention=False, ts="101.0", thread="100.0")
        hm2.assert_called_once()
        assert hm2.call_args.kwargs["guest_user"] == GUEST

    @pytest.mark.asyncio
    async def test_a_second_guest_cannot_join_the_first_guests_thread(self):
        orch, smap = self._orch_with_real_index()
        smap.set_slack_link(guest_session_key(GUEST, "100.0"), "100.0", TRACKED)
        set_allowed_users({OWNER, GUEST, "U0GUEST02"})
        hm = await _route(orch, user="U0GUEST02", is_mention=True, ts="101.0", thread="100.0")
        hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_guest_mention_in_a_thread_the_owner_used_is_refused(self):
        orch, smap = self._orch_with_real_index()
        # The owner's own self-link, exactly as an owner turn writes it.
        smap.set_slack_link(canonical_key("200.0"), "200.0", TRACKED)
        assert smap.get_session_for_thread("200.0") == canonical_key("200.0")
        hm = await _route(orch, is_mention=True, ts="201.0", thread="200.0")
        hm.assert_not_called()
        assert "Post a new message in the channel" in orch.slack.post_ephemeral.call_args[0][2]

    @pytest.mark.asyncio
    async def test_the_owner_keeps_their_own_key_in_a_guest_claimed_thread(self):
        """Both key shapes are self-derived, so the guest's claim is NOT evicted.

        That is what makes the filter load-bearing rather than cosmetic: the index
        genuinely still answers with the guest's key here.
        """
        from kiro_crew.slack.handler import visible_thread_owner

        orch, smap = self._orch_with_real_index()
        guest_key = guest_session_key(GUEST, "100.0")
        smap.set_slack_link(guest_key, "100.0", TRACKED)

        # The owner's later self-link does NOT displace it (setdefault branch).
        smap.set_slack_link(canonical_key("100.0"), "100.0", TRACKED)
        assert smap.get_session_for_thread("100.0") == guest_key

        # So the owner turn must decline to adopt it, and the guest still sees it.
        assert visible_thread_owner(smap, "100.0", "") is None
        assert visible_thread_owner(smap, "100.0", GUEST) == guest_key

    @pytest.mark.asyncio
    async def test_an_owner_mention_in_a_guest_thread_is_still_answered(self):
        """CONTROL: the owner loses no capability, they only keep their own key."""
        orch, smap = self._orch_with_real_index()
        smap.set_slack_link(guest_session_key(GUEST, "100.0"), "100.0", TRACKED)
        hm = await _route(orch, user=OWNER, is_mention=True, ts="101.0", thread="100.0")
        hm.assert_called_once()
        assert hm.call_args.kwargs["guest_user"] == ""


class TestRouteLoopGuardsAreAtTheirSites:
    """Structural, and deliberately so.

    These three guards sit inside ``handle_message``'s route loop, past
    ``get_or_create`` and the provider handshake, so reaching them behaviourally
    needs a live ACP stack. The repo already gates one security property this way
    (``test_hooks.py`` scans for the shared kwargs extraction), so these follow that
    shape: assert the guard is spelled at its site. Each needle is built from parts,
    so these assertions' own lines cannot match and report themselves.
    """

    @staticmethod
    def _src():
        import inspect

        from kiro_crew.slack import handler as _handler

        return inspect.getsource(_handler.handle_message)

    def test_the_reroute_block_excludes_a_guest_turn(self):
        """It hands the turn to the thread's owner, which for a guest is not them.

        Without the guard, ``candidate_key`` becomes the canonical or owner key and
        ``session_store_for_turn`` then resolves the OWNER's memory store.
        """
        src = self._src()
        guard = "if not route_pinned and " + "not guest_user:"
        assert src.count(guard) == 2, "both halves of the reroute must exclude a guest"
        assert ("if not route_pinned:" + "\n") not in src, "an unguarded reroute half remains"

    def test_the_linked_thread_intercept_excludes_a_guest_turn(self):
        """It delivers into the owner's dashboard slot and returns before the gate."""
        src = self._src()
        assert ("if not guest_user and await " + "maybe_route_linked_thread(") in src

    def test_the_second_agent_resolution_excludes_a_guest_turn(self):
        """It reads the thread override map and then the OWNER's default agent."""
        src = self._src()
        # The guarded form, and no unguarded re-resolution left behind.
        assert ("if not guest_user:" + "\n            _agent = (") in src
        bad = "_agent = _thread_agents.get(session_key) or channel_agent"
        assert src.count(bad) == 1, "only the pre-gate resolution may be unguarded"

    def test_no_raw_index_read_survives_inside_handle_message(self):
        """Every read goes through the filter, which is what the docstring claims.

        The three that did NOT were found by review, not by a test: an owner's
        OPTIONS control was filed under a guest's key and the guest's next message
        expired it. This is the standing guard for that whole class, so a fourth
        raw read cannot be added silently. Needles built from parts so this
        assertion's own line cannot match.
        """
        import inspect

        from kiro_crew.slack import handler as _handler

        src = inspect.getsource(_handler.handle_message)
        raw = "sessions.get_session_for_thread(" + "reply_ts)"
        filtered = "visible_thread_owner(sessions, " + "reply_ts, guest_user)"
        assert raw not in src, "a raw thread-index read bypasses the guest filter"
        # CONTROL: the filtered form is present several times, so the absence above
        # is a routed read rather than a function that stopped reading the index.
        assert src.count(filtered) >= 4

    def test_the_guest_turn_mirrors_to_no_dashboard_slot(self):
        """The clearing assignment must be its OWN statement, right after the guard.

        ``linked_session_key = None if route_pinned else thread_owner_key`` CONTAINS
        the bare assignment as a substring, so a plain containment check matches that
        line and passes whether or not a guest is cleared at all. Anchor on the
        conditional line, then require the bare statement after it.
        """
        src = self._src()
        anchor = "linked_session_key = None if route_pinned else thread_owner_key"
        tail = src[src.index(anchor) + len(anchor) :]
        assert tail.lstrip().startswith("if guest_user:"), "the guard must follow the assignment"
        assert "\n        linked_session_key = None\n" in tail, "a guest turn must clear it"


# ─────────────── seam tests: assert on what CROSSES the seam ───────────────────
#
# Every test above this line asserts a value inside one layer. Three findings got
# through 130 of them because the layer BELOW re-derives the value asserted:
# `channel_agent` is a member key the provider never sees, a config `memory_store`
# is not what a session key resolves to, and `GUEST_SAFE_TOOLS` is not what the
# agent is allowed to see. These assert the payload at the boundary instead.


def _real_guest_cfg():
    """A real config whose guest member genuinely resolves (no mocks inside)."""
    from kiro_crew.config.sections import KiroCrewAgentConfig, MemoryStoreConfig

    cfg = KiroCrewConfig(
        slack_channels={TRACKED: ChannelConfig(activation=ACTIVATION_MENTION)},
        messaging=MessagingConfig(use_transport=False),
    )
    cfg.slack.guest_agent = GUEST_MEMBER
    cfg.memory_stores["guest-store"] = MemoryStoreConfig()
    cfg.agents[GUEST_MEMBER] = KiroCrewAgentConfig(
        kiro_agent=GUEST_AGENT_NAME, memory_store="guest-store"
    )
    return cfg


class TestGuestAgentSeam:
    """What agent name reaches the session layer, and does it survive below it.

    The strongest form would capture `SessionDeps.session_factory`'s `agent` kwarg,
    but nothing in the suite injects that factory and building the injection is more
    new scaffolding than this round should carry. So the path is closed in three
    assertions with no un-asserted gap between them: the member resolves to the guest
    TEMPLATE, the re-deriving layer leaves that template alone, and the template
    names a file that exists. Stated plainly because two of the three findings this
    replaces were exactly a gap between two individually-true assertions.
    """

    def test_the_member_resolves_to_the_guest_template_not_its_own_name(self):
        from kiro_crew.execution_context import resolve_member_execution

        execution = resolve_member_execution(_real_guest_cfg(), GUEST_MEMBER)
        assert execution.template_id == GUEST_AGENT_NAME
        # The member KEY is a different string from the template, and only the
        # template names a spec the backend can resolve.
        assert execution.template_id != GUEST_MEMBER

    def test_the_re_deriving_layer_does_not_substitute_the_guest_template(self):
        """`session_allocation` swaps `agent` for `preparation.template` when a
        revision exists, so a template that survives that call is the one spawned.
        """
        from kiro_crew.session_capabilities import prepare_runtime

        preparation = prepare_runtime(GUEST_AGENT_NAME, None, None)
        # No revision means the caller's agent is passed through untouched.
        assert not preparation.revision
        if preparation.template:
            assert preparation.template == GUEST_AGENT_NAME

    def test_the_template_name_resolves_to_a_file_that_exists_after_install(self, tmp_path):
        """A name with no spec on disk fails every turn with "not installed"."""
        from kiro_crew import agent as agent_mod
        from kiro_crew.agent_files import GUEST_AGENT_FILENAME

        with patch.object(agent_mod, "kiro_agents_dir_path", lambda: tmp_path):
            agent_mod._install_guest_agent()
        installed = tmp_path / GUEST_AGENT_FILENAME
        assert installed.is_file()
        assert installed.stem == GUEST_AGENT_NAME

    def test_the_guest_branch_hands_over_a_template_and_not_a_member_key(self):
        """Structural backstop on the one line that chose between the two.

        Needles built from parts so this assertion cannot match its own line.
        """
        import inspect

        from kiro_crew.slack import handler as _handler

        src = inspect.getsource(_handler.handle_message)
        assert ("_agent = _guest_execution." + "template_id") in src
        assert ("_agent = " + "channel_agent or None") not in src


class TestGuestStoreSeam:
    """What store the GUEST SESSION KEY resolves to, read back as production reads it.

    `store_of_session` is the function the turn's store resolution goes through, so
    these call it rather than inspecting config. The first case is the finding: a
    fresh guest key with nothing bound resolves BLANK, and blank is the owner's own
    memory and the operator's global lessons.
    """

    @staticmethod
    def _execution():
        from kiro_crew.execution_context import resolve_member_execution

        return resolve_member_execution(_real_guest_cfg(), GUEST_MEMBER)

    def test_an_unbound_guest_key_resolves_a_blank_store(self):
        """The bug, pinned as the reason the bind exists. Blank IS the owner's."""
        from kiro_crew.context import store_of_session

        key = guest_session_key(GUEST, "500.0")
        assert store_of_session(None, key) == ""

    def test_binding_the_member_execution_makes_the_guest_key_resolve_its_store(self):
        from kiro_crew.context import store_of_session
        from kiro_crew.execution_context import bind_session_execution

        key = guest_session_key(GUEST, "501.0")
        bind_session_execution(key, self._execution())
        assert store_of_session(None, key) == "guest-store"

    def test_the_first_turn_is_the_case_that_was_blank(self):
        """A FRESH key, bound once, resolves the member's store immediately.

        The first turn is the one with no prior record to fall back on, so it is the
        turn the missing bind silently redirected to the owner's memory.
        """
        from kiro_crew.context import store_of_session
        from kiro_crew.execution_context import bind_session_execution

        key = guest_session_key(GUEST, "502.0")
        assert store_of_session(None, key) == ""  # before: the leak
        bind_session_execution(key, self._execution())
        assert store_of_session(None, key) == "guest-store"  # after: the member's

    def test_the_bound_store_is_not_the_owner_default(self):
        from kiro_crew.context import store_of_session
        from kiro_crew.execution_context import bind_session_execution

        key = guest_session_key(GUEST, "503.0")
        bind_session_execution(key, self._execution())
        resolved = store_of_session(None, key)
        assert resolved != DEFAULT_MEMORY_STORE
        assert resolved != ""

    def test_the_turn_binds_before_it_resolves(self):
        """Ordering is the whole fix: resolving first reads the blank.

        Needles built from parts so this assertion cannot match its own line.
        """
        import inspect

        from kiro_crew.slack import handler as _handler

        src = inspect.getsource(_handler.handle_message)
        bind_at = src.index("bind_session_execution" + ", candidate_key")
        resolve_at = src.index("await session_store_for_turn" + "(context_builder, candidate_key)")
        assert bind_at < resolve_at


class TestGuestToolPayloadSeam:
    """What tool payload the PROVIDER receives, which is the spec file on disk.

    The provider reads the agent spec; it never sees `GUEST_SAFE_TOOLS`. So these
    assert the written file. An empty `tools` list mounts nothing, which made the
    gate guard a tool the model could never call.
    """

    @staticmethod
    def _installed(tmp_path):
        import json

        from kiro_crew import agent as agent_mod
        from kiro_crew.agent_files import GUEST_AGENT_FILENAME

        with patch.object(agent_mod, "kiro_agents_dir_path", lambda: tmp_path):
            agent_mod._install_guest_agent()
        return json.loads((tmp_path / GUEST_AGENT_FILENAME).read_text())

    def test_the_spec_mounts_exactly_web_search(self, tmp_path):
        spec = self._installed(tmp_path)
        assert spec["tools"] == ["web_search"]

    def test_the_spec_does_not_pre_approve_it(self, tmp_path):
        """`allowedTools` is the one path that never reaches the PreToolUse gate.

        Absent is required, not incidental: pre-approving the tool would mean
        `is_guest_safe_tool` never runs, and the gate is the terminal decision.
        """
        spec = self._installed(tmp_path)
        assert "allowedTools" not in spec
        assert "permissions" not in spec

    def test_the_spec_mounts_no_mcp_server(self, tmp_path):
        spec = self._installed(tmp_path)
        assert spec["mcpServers"] == {}

    def test_the_mounted_set_matches_what_the_gate_admits(self, tmp_path):
        """The two must agree, in both directions.

        A mounted tool the gate refuses wastes a turn; a gate entry that is not
        mounted can never be called. Either way one of them is wrong.
        """
        spec = self._installed(tmp_path)
        assert set(spec["tools"]) == set(GUEST_SAFE_TOOLS)

    def test_an_empty_tools_list_would_mount_nothing(self, tmp_path):
        """Why `[]` was the bug, pinned against the repo's own precedent.

        `kirocrew-lite` ships `tools: []` precisely to have no tools, so the empty
        list is not "unset" and does not inherit a default.
        """
        import inspect

        from kiro_crew import agent as agent_mod

        lite = inspect.getsource(agent_mod._install_lite_agent_fallback)
        assert '"tools": []' in lite
        spec = self._installed(tmp_path)
        assert spec["tools"] != []


class TestGuestOwnerTelemetry:
    """`status` answers with no LLM turn, so no tool gate exists to stop it."""

    @pytest.mark.asyncio
    async def test_a_guest_is_refused_the_status_keyword(self):
        from kiro_crew.slack import handler as _handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        with patch.object(_handler, "sel") as _sel:
            handled = await _handler.maybe_handle_keyword_command(
                "status",
                slack,
                AsyncMock(),
                TRACKED,
                "1.0",
                "1.0",
                guest_session_key(GUEST, "1.0"),
                GUEST,
                None,
                guest_user=GUEST,
            )
        assert handled is False  # the shared helper refuses every branch
        del _sel

    def test_every_text_branch_above_the_shared_helper_checks_its_caller(self):
        """The sweep, as a standing guard rather than a one-off reading.

        `status` sat above `maybe_handle_keyword_command`, so the helper's guest
        refusal never ran for it. This enumerates every branch in that region whose
        condition reads the inbound text and requires each to name a caller check,
        so the next branch added there cannot repeat it.
        """
        import inspect
        import re

        from kiro_crew.slack import handler as _handler

        lines = inspect.getsource(_handler.handle_message).splitlines()
        stop = next(i for i, l in enumerate(lines) if "maybe_handle_keyword_command(" in l)
        pat = re.compile(r"^\s*(el)?if\b.*\b(text|_cmd_text|_stripped)\b.*(==|startswith\()")

        def ind(s):
            return len(s) - len(s.lstrip())

        open_branches = []
        for i in range(stop):
            line = lines[i]
            if not pat.search(line):
                continue
            j = i + 1
            while j < stop and (
                not lines[j].strip()
                or lines[j].lstrip().startswith("#")
                or ind(lines[j]) > ind(line)
            ):
                j += 1
            body = "\n".join(lines[i:j])
            checked = (
                ("is_owner(" in body) or ("is_allowed_user(" in body) or ("guest_user" in body)
            )
            if not checked:
                open_branches.append((i + 1, line.strip()[:70]))
        assert not open_branches, f"text branches with no caller check: {open_branches}"
        # CONTROL: the sweep found branches at all, so an empty result is a pass
        # rather than a pattern that matched nothing.
        assert sum(1 for line in lines[:stop] if pat.search(line)) >= 3


class TestComposedInterceptorRefusal:
    """A composed gate can mint a presigned dashboard link, so a guest is refused."""

    def test_the_shipped_default_is_not_composed(self):
        from kiro_crew.slack.events import composed_interceptor_registered

        assert composed_interceptor_registered() is False

    def test_a_gate_overriding_intercept_message_is_composed(self):
        from kiro_crew.platform.defaults import DefaultSlackEnterpriseGate
        from kiro_crew.platform.interfaces import InterceptDecision
        from kiro_crew.slack import events as _events

        class _Composed(DefaultSlackEnterpriseGate):
            def intercept_message(self, orch, **kw):
                return InterceptDecision.REDIRECTED

        ctx = MagicMock()
        ctx.slack_gate = _Composed()
        with patch.object(_events, "current_context", lambda: ctx):
            assert _events.composed_interceptor_registered() is True

    def test_a_subclass_that_inherits_the_default_is_not_composed(self):
        from kiro_crew.platform.defaults import DefaultSlackEnterpriseGate
        from kiro_crew.slack import events as _events

        class _Inherits(DefaultSlackEnterpriseGate):
            pass

        ctx = MagicMock()
        ctx.slack_gate = _Inherits()
        with patch.object(_events, "current_context", lambda: ctx):
            assert _events.composed_interceptor_registered() is False

    def test_an_unreadable_gate_is_treated_as_composed(self):
        """Deny-by-default, matching the seam's own fail-closed contract."""
        from kiro_crew.slack import events as _events

        def _boom():
            raise RuntimeError("no context")

        with patch.object(_events, "current_context", _boom):
            assert _events.composed_interceptor_registered() is True

    @pytest.mark.asyncio
    async def test_a_guest_is_refused_while_a_composed_gate_is_registered(self):
        from kiro_crew.slack import events as _events

        orch = _make_orch()
        with patch.object(_events, "composed_interceptor_registered", lambda: True):
            hm = await _route(orch)
        hm.assert_not_called()
        assert "access gate that guests are not routed through" in (
            orch.slack.post_ephemeral.call_args[0][2]
        )

    @pytest.mark.asyncio
    async def test_the_owner_is_unaffected_by_that_refusal(self):
        """CONTROL: the refusal is scoped to guests, not to the channel."""
        from kiro_crew.slack import events as _events

        orch = _make_orch()
        with patch.object(_events, "composed_interceptor_registered", lambda: True):
            hm = await _route(orch, user=OWNER)
        hm.assert_called_once()
