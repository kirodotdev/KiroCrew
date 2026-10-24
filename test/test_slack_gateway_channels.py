"""GatewayOrchestrator channel transports at boot.

Covers the Slack socket-mode connect and its dashboard-only fallback, the
``channels`` governance gate on each transport start, the badge an enabled channel
that cannot start gets, the enabled-but-uncredentialed WARNING on the real start
path (with the ratchet that keeps each collapsed enable predicate and its
skip-reason probe aligned), and the inbound-spool replay the start schedules.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _make_orchestrator,
)

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack import gateway as gw
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.testing.wait import await_until


class TestInboundReplayResolvesItsSpoolWhenScheduled:
    """The detached replay pass reads the spool THIS boot resolved, not a later one.

    ``_start_channel_transports`` schedules ``_replay_spooled_inbound`` as a detached
    task, and the pass reads the spool on a worker thread (``asyncio.to_thread``).
    Resolving ``spool_path()`` there means resolving the data home at whatever
    moment the thread runs -- under the suite, after the starting test's
    ``KIROCREW_HOME`` pin is gone. Five full runs left
    ``~/.kiro/crew/inbound-spool/refused.jsonl.lock`` in the operator's REAL data
    home exactly this way. The scheduler now resolves the path on the loop as it
    creates the task; this pins that the path the pass receives is the one in
    force at schedule time.
    """

    @pytest.mark.asyncio
    async def test_the_spool_path_is_fixed_when_the_task_is_created(self, monkeypatch, tmp_path):
        from kiro_crew.messaging import inbound_spool

        seen: list[Path | None] = []

        async def _record(*, transports, path=None, now=None):
            seen.append(path)
            return inbound_spool.ReplayReport()

        monkeypatch.setattr(inbound_spool, "replay_spooled", _record)
        monkeypatch.setattr(gw, "_channel_transport_permitted", lambda member: False)
        boot_home = tmp_path / "boot-home"
        monkeypatch.setenv("KIROCREW_HOME", str(boot_home))
        orch = _make_orchestrator()

        await orch._start_channel_transports()
        # The environment moves on BEFORE the detached task gets its first step --
        # the shape a finished test's teardown has from the task's point of view.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "later-home"))
        replay = orch._inbound_replay_task
        assert replay is not None
        await await_until(replay)

        assert seen == [boot_home / "inbound-spool" / "refused.jsonl"], (
            "the replay pass was handed a spool resolved after scheduling -- a worker "
            "thread would read (and lock) whatever data home the environment names then"
        )

    @pytest.mark.asyncio
    async def test_replay_uses_live_slack_client_without_global_registration(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.messaging import inbound_spool
        from kiro_crew.slack.transport import SlackTransport

        seen: list[dict[str, object]] = []

        async def _record(*, transports, path=None, now=None):
            seen.append(dict(transports))
            return inbound_spool.ReplayReport()

        monkeypatch.setattr(inbound_spool, "replay_spooled", _record)
        orch = _make_orchestrator(slack_enabled=True, owner_id="U_OWNER")
        live_client = MagicMock()
        orch.slack = live_client
        teams = object()
        orch.dashboard_state = SimpleNamespace(channel_transports={"teams": teams})

        await orch._replay_spooled_inbound(spool=tmp_path / "refused.jsonl")

        assert len(seen) == 1
        assert seen[0]["teams"] is teams
        slack = seen[0]["slack"]
        assert isinstance(slack, SlackTransport)
        assert slack.client is live_client
        assert slack.may_send_to("C1", "1700.0", principal="U_OWNER") is True
        assert slack.may_send_to("C1", "1700.0", principal="U_REVOKED") is False
        live_client.ensure_channel_team = AsyncMock()
        live_client.post_message = AsyncMock(return_value="1701.0")
        assert await slack.send_message("C1", "notice", "1700.0") == "1701.0"
        live_client.ensure_channel_team.assert_awaited_once_with("C1")
        live_client.post_message.assert_awaited_once_with("C1", "notice", "1700.0")
        assert orch.dashboard_state.channel_transports == {
            "teams": teams
        }, "Slack replay must not widen the ordinary shared send registry"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _connect_slack resilience (Slack connect must never crash the gateway)
# ═══════════════════════════════════════════════════════════════════════════


class TestConnectSlackResilience:
    """A failing Slack socket-mode connect must fall back to dashboard-only."""

    @pytest.mark.asyncio
    async def test_returns_false_when_slack_disabled(self):
        orch = _make_orchestrator()
        orch._socket_client = None
        assert await orch._connect_slack() is False

    @pytest.mark.asyncio
    async def test_returns_true_on_successful_connect(self, capsys):
        orch = _make_orchestrator()
        orch._socket_client = MagicMock()
        orch._socket_client.connect = AsyncMock()
        assert await orch._connect_slack() is True
        orch._socket_client.connect.assert_awaited_once()
        assert "connected to Slack" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_connect_timeout_is_non_fatal(self, capsys):
        # Reproduces the proxy/timeout crash: connect raises TimeoutError.
        orch = _make_orchestrator()
        orch._socket_client = MagicMock()
        orch._socket_client.connect = AsyncMock(side_effect=asyncio.TimeoutError)
        # Must NOT raise — gateway continues in dashboard-only mode.
        assert await orch._connect_slack() is False
        assert "dashboard-only" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        # CancelledError is BaseException, not Exception — real cancellation
        # must still propagate (we only swallow ordinary connect failures).
        orch = _make_orchestrator()
        orch._socket_client = MagicMock()
        orch._socket_client.connect = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await orch._connect_slack()


class TestUnreadyChannelBadge:
    """An ENABLED channel that cannot start owes the operator a reason.

    Its factory returns None silently, so ``channel_status`` reported
    ``{connected: False, error: ""}``, byte-identical to a channel nobody
    configured, which System > Services deliberately filters out. The badge is what
    makes the two distinguishable, and it names the missing credential rather than
    only producing a row.
    """

    def _orch_with(self, **sections):
        from kiro_crew.messaging.registry import ChannelDescriptor

        orch = _make_orchestrator()
        orch.dashboard_state = MagicMock()
        for name, values in sections.items():
            section = getattr(orch._cfg, name)
            for key, value in values.items():
                object.__setattr__(section, key, value)
        orch._cfg.load_credentials = lambda: {}
        boot = tuple(ChannelDescriptor(channel_type=name, start=AsyncMock()) for name in sections)
        return orch, boot

    def test_an_enabled_channel_missing_its_token_is_badged_with_the_reason(self):
        orch, boot = self._orch_with(telegram={"enabled": True, "bot_token": ""})
        orch._badge_unready_channels(boot)
        error = orch.dashboard_state.telegram_connect_error
        assert "Enabled but not started" in error
        # The actionable half: WHICH credential, so the operator is not left
        # guessing which of several a channel needs.
        assert "TELEGRAM_BOT_TOKEN" in error

    @pytest.mark.parametrize(
        ("enabled", "bot_token"),
        [
            # A disabled channel has nothing to report, and a row for it would be
            # the noise the Services filter exists to remove.
            pytest.param(False, "", id="test_a_disabled_channel_is_not_badged"),
            # A credentialed channel is the gateway's own outcome to report.
            pytest.param(True, "12345:AA", id="test_a_ready_channel_is_not_badged"),
        ],
    )
    def test_channel_is_not_badged(self, enabled: bool, bot_token: str):
        orch, boot = self._orch_with(telegram={"enabled": enabled, "bot_token": bot_token})
        orch._badge_unready_channels(boot)
        assert not isinstance(orch.dashboard_state.telegram_connect_error, str)

    def test_a_channel_outside_the_bootable_set_is_not_badged(self):
        """Slack is host-managed (``start=None``); its own connect path reports it."""
        orch, _ = self._orch_with(telegram={"enabled": True, "bot_token": ""})
        orch._badge_unready_channels(())
        assert not isinstance(orch.dashboard_state.telegram_connect_error, str)

    def test_the_badge_never_breaks_boot(self):
        """Best-effort by construction: a diagnostic must not stop a transport."""
        orch, boot = self._orch_with(telegram={"enabled": True, "bot_token": ""})
        orch._cfg.load_credentials = MagicMock(side_effect=RuntimeError("cred store down"))
        orch._badge_unready_channels(boot)
        orch._cfg.load_credentials.assert_called_once_with()
        assert not isinstance(orch.dashboard_state.telegram_connect_error, str)

    def test_no_dashboard_state_is_tolerated(self):
        orch, boot = self._orch_with(telegram={"enabled": True, "bot_token": ""})
        orch.dashboard_state = None
        orch._cfg.load_credentials = MagicMock(return_value={})
        orch._badge_unready_channels(boot)
        # The missing dashboard is the gate: the credential store is never read.
        orch._cfg.load_credentials.assert_not_called()


class TestChannelTransportStartGate:
    """`_start_channel_transports` gates each non-Slack transport start on the
    ``channels`` governance scope, using the same member ids as the send/receive
    chokepoints. Clients are mocked — no real network connections are opened.
    """

    def _install_policy(self, policy_body):
        import dataclasses

        from kiro_crew.platform import context as ctx_mod
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.governance import parse_policy

        base = build_default_context(KiroCrewConfig.load())
        ceiling = parse_policy(policy_body) if policy_body is not None else None
        ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))

    @staticmethod
    def _enable_all_transports(orch):
        # The start gate now evaluates governance ONLY for config-enabled
        # transports (enabled-only eval), so a test that expects a transport to
        # reach maybe_start_* must mark it enabled — a transport is credential-
        # gated in real use anyway. Set the four flags the orchestrator would set
        # from config so the governance decision (not an off switch) is what
        # decides whether each transport starts.
        for _m in ("wecom", "telegram", "discord", "webex"):
            setattr(orch, f"_{_m}_enabled", True)

    def _patch_starts(self, stack, *, discord_ret=None):
        import contextlib as _cl  # local import; keeps module import block untouched

        assert isinstance(stack, _cl.ExitStack)  # documents the contract
        # The registry rewrite (PR ③) removed the module-level maybe_start_*
        # bindings from slack.gateway — the roster now comes from
        # kiro_crew.channels. Tests inject a descriptor tuple through
        # _start_channel_transports(descriptors=...) instead of patching names;
        # the mocks and every assertion below are unchanged.
        from kiro_crew.messaging.registry import ChannelDescriptor

        mocks = {
            "wecom": AsyncMock(),
            "telegram": AsyncMock(),
            "discord": AsyncMock(return_value=discord_ret),
            "webex": AsyncMock(),
        }
        self._descriptors = tuple(
            ChannelDescriptor(channel_type=name, start=mock) for name, mock in mocks.items()
        )
        return mocks

    def teardown_method(self):
        from kiro_crew.platform import context as ctx_mod
        from kiro_crew.platform import governance_profiles as gp

        ctx_mod.reset_context()
        gp.reset_store()

    @pytest.mark.asyncio
    async def test_denied_transport_is_skipped_and_client_stays_none(self):
        import contextlib

        # Policy allows only discord → telegram/wecom/webex must NOT start.
        self._install_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
            }
        )
        orch = _make_orchestrator()
        self._enable_all_transports(orch)
        discord_client = MagicMock(name="discord_client")
        with contextlib.ExitStack() as stack:
            mocks = self._patch_starts(stack, discord_ret=discord_client)
            await orch._start_channel_transports(descriptors=self._descriptors)

        # Denied members: maybe_start_* never invoked, clients stay None.
        mocks["wecom"].assert_not_awaited()
        mocks["telegram"].assert_not_awaited()
        mocks["webex"].assert_not_awaited()
        assert orch._wecom_client is None
        assert orch._telegram_client is None
        assert orch._webex_client is None
        # Allowed member: started, client wired.
        mocks["discord"].assert_awaited_once()
        assert orch._discord_client is discord_client

    @pytest.mark.asyncio
    async def test_no_policy_starts_every_transport_as_today(self):
        import contextlib

        # Default OSS build: no policy governing channels → all maybe_start_*
        # invoked exactly as before (byte-identical default behavior).
        self._install_policy(None)
        orch = _make_orchestrator()
        self._enable_all_transports(orch)
        with contextlib.ExitStack() as stack:
            mocks = self._patch_starts(stack)
            await orch._start_channel_transports(descriptors=self._descriptors)

        mocks["wecom"].assert_awaited_once()
        mocks["telegram"].assert_awaited_once()
        mocks["discord"].assert_awaited_once()
        mocks["webex"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_host_profile_deny_skips_transport(self, tmp_path, monkeypatch):
        import contextlib
        import json

        from kiro_crew.platform import governance_profiles as gp

        # Policy ALLOWS telegram + discord, but a surface:host profile narrows to
        # discord only → telegram must NOT start. This exercises the full
        # _start_channel_transports path (through the executor) and proves the
        # gate binds the host profile (session_key=HOST_SESSION_KEY); an empty key
        # would classify to "unknown" and silently ignore this profile.
        self._install_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["telegram", "discord"]}},
            }
        )
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(gp, "_PROFILES_DIR", profiles_dir)
        gp.reset_store()

        orch = _make_orchestrator()
        self._enable_all_transports(orch)
        discord_client = MagicMock(name="discord_client")
        with contextlib.ExitStack() as stack:
            mocks = self._patch_starts(stack, discord_ret=discord_client)
            await orch._start_channel_transports(descriptors=self._descriptors)

        # Host profile narrows telegram out even though the policy allowed it.
        mocks["telegram"].assert_not_awaited()
        assert orch._telegram_client is None
        # discord is in BOTH policy and profile → starts.
        mocks["discord"].assert_awaited_once()
        assert orch._discord_client is discord_client

    @pytest.mark.asyncio
    async def test_disabled_transport_not_evaluated_for_governance(self, monkeypatch):
        import contextlib

        from kiro_crew.slack import gateway as gw

        # Enabled-only eval: a config-disabled transport is NEVER passed to the
        # governance gate (avoids a spurious deny-SEL for a channel that would
        # never connect anyway). Permissive policy, but only telegram enabled →
        # the gate is queried for telegram alone; the other three never start.
        self._install_policy(None)
        orch = _make_orchestrator()
        orch._telegram_enabled = True  # only telegram enabled
        orch._wecom_enabled = False
        orch._discord_enabled = False
        orch._webex_enabled = False

        queried = []
        real_gate = gw._channel_transport_permitted

        def _spy(member):
            queried.append(member)
            return real_gate(member)

        monkeypatch.setattr(gw, "_channel_transport_permitted", _spy)
        with contextlib.ExitStack() as stack:
            mocks = self._patch_starts(stack)
            await orch._start_channel_transports(descriptors=self._descriptors)

        # Only the enabled transport was evaluated + started.
        assert queried == ["telegram"]
        mocks["telegram"].assert_awaited_once()
        mocks["wecom"].assert_not_awaited()
        mocks["discord"].assert_not_awaited()
        mocks["webex"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slack_connect_denied_by_policy_drops_socket_client(self):
        # BLOCKING (GPT #593): Slack is a GOVERNED transport like every other
        # channel. A `channels` policy that denies `slack` must stop it from
        # CONNECTING — not merely drop its inbound messages — and must drop the
        # socket client so nothing can reconnect it later.
        self._install_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
            }
        )
        orch = _make_orchestrator()
        socket_client = MagicMock(name="socket_client")
        socket_client.connect = AsyncMock()
        orch._socket_client = socket_client

        connected = await orch._connect_slack()

        assert connected is False, "a channels deny must stop the Slack connect"
        socket_client.connect.assert_not_awaited()
        assert orch._socket_client is None, "the denied socket client must be dropped"

    @pytest.mark.asyncio
    async def test_slack_connect_permitted_with_no_policy_connects_as_today(self):
        # Default-build invariant: with no `channels` policy the Slack connect is
        # byte-identical to today (the gate permits and the socket client connects).
        self._install_policy(None)
        orch = _make_orchestrator()
        socket_client = MagicMock(name="socket_client")
        socket_client.connect = AsyncMock()
        orch._socket_client = socket_client

        connected = await orch._connect_slack()

        assert connected is True
        socket_client.connect.assert_awaited_once()
        assert orch._socket_client is socket_client


# ─── Channel skip-reason warning on the PRODUCTION start path ──


_UNCREDENTIALED_PROBE_EXEMPTIONS = {
    # Slack has credential operands, but no cfg.slack.enabled setting: an
    # absent token pair means "not configured", rather than an enabled channel
    # that was silently skipped.
    "slack": "token-driven enablement with no config enabled flag",
    # These transports are config-only. Their runtime pairing/prerequisite
    # diagnostics live in the channel implementation, not in credential env.
    "whatsapp": "config-only enablement; pairing state is not a credential operand",
    "imessage": "config-only enablement through the signed-in Messages.app",
}


def _gateway_class() -> ast.ClassDef:
    tree = ast.parse(Path(gw.__file__).read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GatewayOrchestrator"
    )


def _gateway_defs() -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The orchestrator's methods: the facade class body's, then the module-level
    functions of the ``gateway_runtime`` owners the facade binds onto the class."""
    defs = [
        node
        for node in _gateway_class().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for path in sorted((Path(gw.__file__).parent / "gateway_runtime").glob("*.py")):
        defs += [
            node
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
    return defs


def _gateway_method(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(node for node in _gateway_defs() if node.name == name)


def _collapsed_enabled_operands() -> dict[str, set[str]]:
    """Return self-attribute operands read by each collapsed enabled flag.

    Scans ``__init__`` AND every ``_hoist_<channel>`` method: the per-channel
    hoists are where each ``_<channel>_enabled`` predicate is now written, so a
    scan of the constructor alone would find no predicate at all and the ratchet
    would pass vacuously.
    """
    found: dict[str, set[str]] = {}
    methods = [
        node
        for node in _gateway_defs()
        if node.name == "__init__" or node.name.startswith("_hoist_")
    ]
    assert methods, "no __init__ / _hoist_* methods found on the orchestrator"
    for method in methods:
        for node in ast.walk(method):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr.startswith("_")
                and target.attr.endswith("_enabled")
            ):
                continue
            channel = target.attr.removeprefix("_").removesuffix("_enabled")
            found[channel] = {
                child.attr
                for child in ast.walk(node.value)
                if isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "self"
            }
    return found


def _uncredentialed_probe_operands() -> dict[str, set[str]]:
    """Return the self-attributes whose presence each production probe checks."""
    assignment = next(
        node
        for node in ast.walk(_gateway_method("_start_channel_transports"))
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "uncredentialed_probe_rows"
    )
    assert isinstance(assignment.value, ast.Tuple)
    found: dict[str, set[str]] = {}
    for row in assignment.value.elts:
        assert isinstance(row, ast.Tuple) and len(row.elts) == 4
        channel_node, _, _, credentials_node = row.elts
        assert isinstance(channel_node, ast.Constant) and isinstance(channel_node.value, str)
        assert isinstance(credentials_node, ast.Tuple)
        operands = set()
        for pair in credentials_node.elts:
            assert isinstance(pair, ast.Tuple) and len(pair.elts) == 2
            presence = pair.elts[1]
            assert isinstance(presence, ast.Call)
            assert isinstance(presence.func, ast.Name) and presence.func.id == "bool"
            assert len(presence.args) == 1 and not presence.keywords
            operand = presence.args[0]
            assert isinstance(operand, ast.Attribute)
            assert isinstance(operand.value, ast.Name) and operand.value.id == "self"
            operands.add(operand.attr)
        found[channel_node.value] = operands
    return found


class TestUncredentialedProbeRatchet:
    """Keep collapsed enable predicates and their skip-reason probes aligned."""

    def test_every_rostered_channel_is_accounted_for(self) -> None:
        from kiro_crew.channels import builtin_channel_descriptors

        rostered = {descriptor.channel_type for descriptor in builtin_channel_descriptors()}
        accounted = set(_uncredentialed_probe_operands()) | set(_UNCREDENTIALED_PROBE_EXEMPTIONS)
        assert rostered == accounted, (
            "rostered channels must have an uncredentialed probe row or an "
            "explicit config-only/token-driven exemption; "
            f"missing={sorted(rostered - accounted)}, "
            f"stale={sorted(accounted - rostered)}"
        )

    def test_each_probe_tracks_the_predicate_operands(self) -> None:
        enabled = _collapsed_enabled_operands()
        probes = _uncredentialed_probe_operands()
        mismatched = {
            channel: {
                "predicate": sorted(enabled.get(channel, set())),
                "probe": sorted(operands),
            }
            for channel, operands in probes.items()
            if enabled.get(channel) != operands
        }
        assert not mismatched, (
            "uncredentialed probe rows must name exactly the self-attribute "
            f"operands read by their _<channel>_enabled predicate: {mismatched}"
        )

    def test_exemptions_are_not_credential_probe_rows(self) -> None:
        overlap = set(_uncredentialed_probe_operands()) & set(_UNCREDENTIALED_PROBE_EXEMPTIONS)
        assert not overlap, (
            "a channel cannot be both credential-probed and exempt: " f"{sorted(overlap)}"
        )


# One row per collapsed-flag channel the enabled-but-uncredentialed WARNING
# covers: (channel_type, names the WARNING must carry, names it must NOT carry,
# creds entries + cfg mutations that make the channel FULLY credentialed).
# The name lists are spelled exactly as the warning must emit them, i.e. the
# credential operands each _<channel>_enabled predicate actually reads.
_UNCREDENTIALED_CHANNEL_ROWS = (
    pytest.param(
        "wecom",
        ("WECOM_BOT_ID", "WECOM_SECRET"),
        (),
        {"WECOM_BOT_ID": "wecom-bot-value", "WECOM_SECRET": "wecom-secret-value"},
        (),
        id="wecom",
    ),
    pytest.param(
        "telegram",
        ("TELEGRAM_BOT_TOKEN",),
        (),
        {"TELEGRAM_BOT_TOKEN": "telegram-token-value"},
        (),
        id="telegram",
    ),
    pytest.param(
        "weixin",
        # account_id is not a secret (it comes from the QR setup flow) but the
        # predicate reads it, so it is named like any other missing operand.
        ("WEIXIN_TOKEN", "weixin.account_id"),
        (),
        {"WEIXIN_TOKEN": "weixin-token-value"},
        (("weixin", "account_id", "weixin-account-value"),),
        id="weixin",
    ),
    pytest.param(
        "feishu",
        ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
        (),
        {
            "FEISHU_APP_ID": "feishu-app-id-value",
            "FEISHU_APP_SECRET": "feishu-app-secret-value",
        },
        (),
        id="feishu",
    ),
    pytest.param(
        "discord",
        ("DISCORD_BOT_TOKEN",),
        (),
        {"DISCORD_BOT_TOKEN": "discord-token-value"},
        (),
        id="discord",
    ),
    pytest.param(
        "webex",
        ("WEBEX_BOT_TOKEN",),
        (),
        {"WEBEX_BOT_TOKEN": "webex-token-value"},
        (),
        id="webex",
    ),
    pytest.param(
        "teams",
        ("MICROSOFT_APP_ID", "MICROSOFT_APP_PASSWORD"),
        # _teams_enabled never reads the tenant id, so the warning must not
        # send the operator to a field that cannot start the channel.
        ("MICROSOFT_APP_TENANT_ID",),
        {
            "MICROSOFT_APP_ID": "teams-app-id-value",
            "MICROSOFT_APP_PASSWORD": "teams-password-value",
        },
        (),
        id="teams",
    ),
)

_UNCREDENTIALED_CHANNEL_TYPES = (
    "wecom",
    "telegram",
    "weixin",
    "feishu",
    "discord",
    "webex",
    "teams",
)


class TestChannelSkipReasonAtTransportStart:
    """The enabled-but-uncredentialed WARNING must fire on the real start path.

    The channel registry's enabled-only gate never calls a channel factory
    when ``_<channel>_enabled`` is False — for a disabled AND for an
    enabled-but-uncredentialed channel alike — so a factory-level log can
    never be reached in production. The skip reason is therefore logged by
    ``_start_channel_transports`` at the decision point, which runs AFTER
    ``KIROCREW_READY`` (outside the boot-path window), via the seven-channel
    table feeding ``warn_if_channel_uncredentialed``. These pin that wiring
    for every collapsed-flag channel, generalizing the WeCom-only
    class; the helper's message contract is
    pinned in ``test_wecom_gateway.py``.

    Rows that make a ``_<channel>_enabled`` flag True (fully-credentialed
    cases) stub the governance gate to deny, so ``registry.start_channels``
    skips every factory and no transport or network is ever touched; the
    WARNING under test fires before either.

    Capture is scoped to WARNING+: the wiring contract here is the
    warning-level diagnostic (exactly one, or none), so an unrelated DEBUG/INFO
    line some channel module may grow on the skip path must not flake these.
    Helper-level COMPLETE silence (no records at any level) stays pinned in
    ``test_wecom_gateway.py``.
    """

    def _build(
        self,
        *,
        creds: dict[str, str],
        cfg_mut: tuple[tuple[str, str, object], ...] = (),
    ) -> GatewayOrchestrator:
        cfg = KiroCrewConfig()
        for section, attr, value in cfg_mut:
            setattr(getattr(cfg, section), attr, value)
        with patch.object(cfg, "load_credentials", return_value=creds):
            return GatewayOrchestrator(cfg)

    async def _start(self, orch: GatewayOrchestrator, monkeypatch) -> None:
        from kiro_crew.slack import gateway as gw

        warn = gw.warn_if_channel_uncredentialed

        def warn_with_presence(channel_type, settings_name, cfg_enabled, presence):
            assert all(type(present) is bool for _, present in presence)
            warn(channel_type, settings_name, cfg_enabled, presence)

        monkeypatch.setattr(gw, "warn_if_channel_uncredentialed", warn_with_presence)
        monkeypatch.setattr(gw, "_channel_transport_permitted", lambda member: False)
        await orch._start_channel_transports()
        # `_start_channel_transports` detaches `_replay_spooled_inbound` as a
        # background task on purpose (a slow platform send must not hold the
        # gateway's start open), so it can still be resolving `data_home()` on
        # the event loop after this test's `KIROCREW_HOME` pin is torn down —
        # the same "background worker resolves its path when it runs" class as
        # the safety-override breadcrumb publisher. Drain it here the same way
        # `_shutdown()` does, so no write happens once the pin is gone.
        replay = orch._inbound_replay_task
        if replay is not None and not replay.done():
            replay.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await await_until(replay)

    def _channel_records(self, caplog) -> list[logging.LogRecord]:
        names = {f"kiro_crew.{c}.gateway" for c in _UNCREDENTIALED_CHANNEL_TYPES}
        return [r for r in caplog.records if r.name in names]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel,names,forbidden,full_creds,full_cfg_mut", _UNCREDENTIALED_CHANNEL_ROWS
    )
    async def test_enabled_without_credentials_warns_at_default_level(
        self, caplog, monkeypatch, channel, names, forbidden, full_creds, full_cfg_mut
    ) -> None:
        orch = self._build(
            creds={"KIROCREW_OWNER_ID": "U_OWNER"},
            cfg_mut=((channel, "enabled", True),),
        )
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        records = self._channel_records(caplog)
        # Exactly one WARNING, on this channel's own gateway logger, and no
        # cross-talk onto any sibling channel's logger.
        assert len(records) == 1
        assert records[0].name == f"kiro_crew.{channel}.gateway"
        assert records[0].levelno == logging.WARNING
        msg = records[0].getMessage()
        for name in names:
            assert name in msg
        for name in forbidden:
            assert name not in msg

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel,creds,cfg_mut,named,not_named,present_values",
        (
            pytest.param(
                "wecom",
                {"WECOM_BOT_ID": "wecom-bot-value"},
                (),
                ("WECOM_SECRET",),
                ("WECOM_BOT_ID",),
                ("wecom-bot-value",),
                id="wecom-secret-missing",
            ),
            pytest.param(
                "weixin",
                {"WEIXIN_TOKEN": "weixin-token-value"},
                (),
                ("weixin.account_id",),
                ("WEIXIN_TOKEN",),
                ("weixin-token-value",),
                id="weixin-account-id-missing",
            ),
            pytest.param(
                "weixin",
                {},
                (("weixin", "account_id", "weixin-account-value"),),
                ("WEIXIN_TOKEN",),
                ("weixin.account_id",),
                ("weixin-account-value",),
                id="weixin-token-missing",
            ),
            pytest.param(
                "teams",
                {"MICROSOFT_APP_ID": "teams-app-id-value"},
                (),
                ("MICROSOFT_APP_PASSWORD",),
                ("MICROSOFT_APP_ID",),
                ("teams-app-id-value",),
                id="teams-password-missing",
            ),
        ),
    )
    async def test_a_partial_configuration_names_only_the_missing_credential(
        self, caplog, monkeypatch, channel, creds, cfg_mut, named, not_named, present_values
    ) -> None:
        orch = self._build(
            creds={"KIROCREW_OWNER_ID": "U_OWNER", **creds},
            cfg_mut=((channel, "enabled", True), *cfg_mut),
        )
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        records = self._channel_records(caplog)
        assert len(records) == 1
        msg = records[0].getMessage()
        for name in named:
            assert name in msg
        for name in not_named:
            assert name not in msg
        # Credential VALUES must never be logged, only the variable names.
        for value in present_values:
            assert value not in msg

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel,names,forbidden,full_creds,full_cfg_mut", _UNCREDENTIALED_CHANNEL_ROWS
    )
    async def test_a_disabled_channel_is_completely_silent(
        self, caplog, monkeypatch, channel, names, forbidden, full_creds, full_cfg_mut
    ) -> None:
        # Even with every credential present: disabled means silence.
        orch = self._build(
            creds={"KIROCREW_OWNER_ID": "U_OWNER", **full_creds},
            cfg_mut=full_cfg_mut,
        )
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        assert self._channel_records(caplog) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel,names,forbidden,full_creds,full_cfg_mut", _UNCREDENTIALED_CHANNEL_ROWS
    )
    async def test_a_fully_credentialed_channel_is_silent(
        self, caplog, monkeypatch, channel, names, forbidden, full_creds, full_cfg_mut
    ) -> None:
        orch = self._build(
            creds={"KIROCREW_OWNER_ID": "U_OWNER", **full_creds},
            cfg_mut=((channel, "enabled", True), *full_cfg_mut),
        )
        assert getattr(orch, f"_{channel}_enabled") is True  # flag really computed
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        assert self._channel_records(caplog) == []

    @pytest.mark.asyncio
    async def test_teams_tenant_id_is_not_a_credential_operand(self, caplog, monkeypatch) -> None:
        # The trap the table must not fall into: the tenant id sits in config
        # right next to the two operands that count, but _teams_enabled never
        # reads it — app id + password present with NO tenant is fully
        # credentialed, so the probe must stay SILENT rather than send the
        # operator to a field that does not gate the channel.
        orch = self._build(
            creds={
                "KIROCREW_OWNER_ID": "U_OWNER",
                "MICROSOFT_APP_ID": "teams-app-id-value",
                "MICROSOFT_APP_PASSWORD": "teams-password-value",
            },
            cfg_mut=(("teams", "enabled", True),),
        )
        assert orch._teams_enabled is True
        assert orch._teams_tenant_id == ""
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        assert self._channel_records(caplog) == []

    @pytest.mark.asyncio
    async def test_telegram_deprecated_accounts_suppress_the_warning(
        self, caplog, monkeypatch
    ) -> None:
        # With telegram.accounts set the channel is stopped by DEPRECATED
        # CONFIG, not by the missing token, and that state already has its own
        # warning at config-load time — pointing the operator at
        # TELEGRAM_BOT_TOKEN here would misname the actual blocker.
        orch = self._build(
            creds={"KIROCREW_OWNER_ID": "U_OWNER"},
            cfg_mut=(
                ("telegram", "enabled", True),
                ("telegram", "accounts", {"legacy-bot": object()}),
            ),
        )
        with caplog.at_level(logging.WARNING):
            await self._start(orch, monkeypatch)
        assert self._channel_records(caplog) == []
