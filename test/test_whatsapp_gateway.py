"""WhatsApp gateway startup tests: enable gate, missing-extra, wiring, errors.

``maybe_start_whatsapp`` is the only public surface; it is driven with a
SimpleNamespace orchestrator and monkeypatched module symbols so neonize is
never imported and no real socket is opened.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

import kiro_crew.whatsapp.gateway as gw
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.whatsapp.gateway import (
    _check_configured_groups,
    _configured_group_jids,
    _resolve_approval_mode,
    maybe_start_whatsapp,
)


class FakeState:
    def __init__(self) -> None:
        self.whatsapp_connected: bool | None = None
        self.whatsapp_connect_error: str | None = None
        self.registered: list[Any] = []

    def register_channel_transport(self, transport: Any) -> None:
        self.registered.append(transport)


def _cfg(**wa):
    whatsapp = SimpleNamespace(
        db_path=wa.get("db_path", ""),
        dm_policy=wa.get("dm_policy", "self"),
        allowed_wa_ids=wa.get("allowed_wa_ids", []),
        groups=wa.get("groups", []),
    )
    agent = SimpleNamespace(default_agent="kirocrew", approval_mode="auto")
    messaging = SimpleNamespace(idle_reset_minutes=0, daily_reset_hour=-1, dm_scope="user")
    return SimpleNamespace(whatsapp=whatsapp, agent=agent, messaging=messaging)


def _orch(*, enabled=True, state=None, approval_mode=None, **wa):
    return SimpleNamespace(
        _whatsapp_enabled=enabled,
        dashboard_state=state,
        sessions=SimpleNamespace(),
        ctx_builder=SimpleNamespace(),
        _cfg=_cfg(**wa),
        _approval_mode=approval_mode,
        conv_log="log-sentinel",
    )


# ── approval mode resolution ────────────────────────────────────────────────
def test_resolve_approval_mode_yolo_is_auto():
    orch = SimpleNamespace(_approval_mode="yolo", _cfg=_cfg())
    assert _resolve_approval_mode(orch) == APPROVAL_AUTO


def test_resolve_approval_mode_explicit_auto():
    orch = SimpleNamespace(_approval_mode="auto", _cfg=_cfg())
    assert _resolve_approval_mode(orch) == APPROVAL_AUTO


def test_resolve_approval_mode_falls_back_to_cfg_interactive():
    cfg = _cfg()
    cfg.agent.approval_mode = "interactive"
    orch = SimpleNamespace(_approval_mode=None, _cfg=cfg)
    assert _resolve_approval_mode(orch) == APPROVAL_INTERACTIVE


# ── enable gate ─────────────────────────────────────────────────────────────
def test_disabled_channel_is_a_noop():
    orch = _orch(enabled=False)
    assert asyncio.run(maybe_start_whatsapp(orch)) is None


def test_missing_enabled_attr_defaults_off():
    orch = SimpleNamespace()  # no _whatsapp_enabled
    assert asyncio.run(maybe_start_whatsapp(orch)) is None


# ── missing optional extra ──────────────────────────────────────────────────
def test_missing_extra_reports_error_and_returns_none(monkeypatch):
    monkeypatch.setattr(gw, "neonize_available", lambda: False)
    state = FakeState()
    orch = _orch(state=state)
    assert asyncio.run(maybe_start_whatsapp(orch)) is None
    assert state.whatsapp_connected is False
    assert state.whatsapp_connect_error  # hint recorded


def test_missing_extra_without_state_is_still_a_noop(monkeypatch):
    monkeypatch.setattr(gw, "neonize_available", lambda: False)
    orch = _orch(state=None)
    assert asyncio.run(maybe_start_whatsapp(orch)) is None


# ── happy path wiring ───────────────────────────────────────────────────────
def _patch_success(monkeypatch):
    """Stub client + transport so connect() never touches neonize."""
    monkeypatch.setattr(gw, "neonize_available", lambda: True)
    monkeypatch.setattr(gw, "default_db_path", lambda home: "/tmp/wa/session.db")

    created: dict[str, Any] = {}

    class StubClient:
        def __init__(self, db_path):
            self.db_path = db_path
            self.on_state_change = None
            self.state = "connected"
            created["client"] = self

        async def list_groups(self):
            created.setdefault("list_groups_calls", 0)
            created["list_groups_calls"] += 1
            if isinstance(created.get("joined"), BaseException):
                raise created["joined"]
            return created.get("joined", [])

    class StubTransport:
        def __init__(self, client, dispatch, *, dm_policy, allowed_wa_ids, groups):
            self.client = client
            self.dispatch = dispatch
            self.dm_policy = dm_policy
            self.allowed_wa_ids = allowed_wa_ids
            self.groups = groups
            self.connected = False
            created["transport"] = self

        async def connect(self):
            self.connected = True

    monkeypatch.setattr(gw, "WhatsAppClient", StubClient)
    monkeypatch.setattr(gw, "WhatsAppTransport", StubTransport)
    return created


def test_start_wires_client_transport_dispatcher_and_connects(monkeypatch):
    created = _patch_success(monkeypatch)
    state = FakeState()
    orch = _orch(state=state)
    client = asyncio.run(maybe_start_whatsapp(orch))

    assert client is created["client"]
    transport = created["transport"]
    assert transport.connected is True
    assert transport.dm_policy == "self"
    assert state.registered == [transport]
    # A state observer was installed and toggles on "connected".
    assert callable(client.on_state_change)
    client.on_state_change("connected", "")
    assert state.whatsapp_connected is True
    client.on_state_change("logged_out", "unlinked")
    assert state.whatsapp_connected is False
    assert "logged_out" in (state.whatsapp_connect_error or "")


def test_start_pins_the_session_store_to_the_protected_path(monkeypatch, tmp_path):
    """A configured db_path must NOT move the store.

    The store holds whatsmeow's device keys, and its protection is a path match on
    the sensitive keystone (``whatsapp`` under the data home). Honouring an
    operator-supplied location would carry the credential outside the one control
    that stops a prompt-injected agent reading it, so the setting is inert and the
    path is always the default.
    """
    from kiro_crew.config.paths import data_home
    from kiro_crew.whatsapp.client import default_db_path

    built: list[str] = []

    class FakeClient:
        def __init__(self, db_path):
            built.append(str(db_path))
            self.state = "unpaired"
            self.on_state_change = None
            self.me = None

        async def connect(self):
            return None

    monkeypatch.setattr(gw, "WhatsAppClient", FakeClient)
    monkeypatch.setattr(gw, "neonize_available", lambda: True)
    orch = _orch(enabled=True, db_path=str(tmp_path / "elsewhere" / "session.db"))
    asyncio.run(maybe_start_whatsapp(orch))
    assert built == [str(default_db_path(data_home()))], (
        "the configured path was honoured, moving the credential out of the " "protected tree"
    )


def test_start_without_state_still_connects(monkeypatch):
    created = _patch_success(monkeypatch)
    orch = _orch(state=None)
    client = asyncio.run(maybe_start_whatsapp(orch))
    assert client is created["client"]
    assert created["transport"].connected is True


def test_start_passes_allowed_ids_and_groups(monkeypatch):
    created = _patch_success(monkeypatch)
    orch = _orch(state=FakeState())
    orch._cfg.whatsapp.allowed_wa_ids = ["447700900000"]
    orch._cfg.whatsapp.groups = [{"jid": "g1@g.us", "mode": "rules"}]
    asyncio.run(maybe_start_whatsapp(orch))
    transport = created["transport"]
    assert transport.allowed_wa_ids == ["447700900000"]
    assert transport.groups == [{"jid": "g1@g.us", "mode": "rules"}]


# ── failure path ────────────────────────────────────────────────────────────
def test_connect_failure_is_swallowed_and_recorded(monkeypatch):
    monkeypatch.setattr(gw, "neonize_available", lambda: True)
    monkeypatch.setattr(gw, "default_db_path", lambda home: "/tmp/wa/session.db")

    class StubClient:
        def __init__(self, db_path):
            self.on_state_change = None

    class BoomTransport:
        def __init__(self, *a, **kw):
            pass

        async def connect(self):
            raise RuntimeError("pairing socket refused")

    monkeypatch.setattr(gw, "WhatsAppClient", StubClient)
    monkeypatch.setattr(gw, "WhatsAppTransport", BoomTransport)

    state = FakeState()
    orch = _orch(state=state)
    assert asyncio.run(maybe_start_whatsapp(orch)) is None
    assert state.whatsapp_connected is False
    assert "pairing socket refused" in (state.whatsapp_connect_error or "")


def test_connect_failure_without_state_returns_none(monkeypatch):
    monkeypatch.setattr(gw, "neonize_available", lambda: True)
    monkeypatch.setattr(gw, "default_db_path", lambda home: "/tmp/wa/session.db")

    class StubClient:
        def __init__(self, db_path):
            self.on_state_change = None

    class BoomTransport:
        def __init__(self, *a, **kw):
            pass

        async def connect(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(gw, "WhatsAppClient", StubClient)
    monkeypatch.setattr(gw, "WhatsAppTransport", BoomTransport)
    orch = _orch(state=None)
    assert asyncio.run(maybe_start_whatsapp(orch)) is None


# ── configured-group membership check ───────────────────────────────────────
# A group entry whose JID the account cannot resolve is INVISIBLE, not broken:
# the gate drops every message from a JID it does not hold, so a mistyped entry
# and a quiet group look identical from the operator's chair. The check turns
# that into one line naming the JIDs, and its restraint matters as much as its
# reporting: a warning that fires on a transient API failure trains the operator
# to ignore it.


class GroupClient:
    """Minimal client exposing only what the check consumes."""

    def __init__(self, joined: Any) -> None:
        self._joined = joined
        self.calls = 0

    async def list_groups(self) -> list[dict]:
        self.calls += 1
        if isinstance(self._joined, BaseException):
            raise self._joined
        return self._joined


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_configured_group_jids_skips_junk_entries():
    """A hand-edited config reaches this unchanged, and it is read exactly as
    ``GroupGate`` reads it: ``str(entry["jid"]).strip()``, non-empty."""
    assert _configured_group_jids(
        [{"jid": "a@g.us"}, {"jid": "  "}, {"name": "no jid"}, "junk", None, {"jid": " b@g.us "}]
    ) == ["a@g.us", "b@g.us"]


def test_configured_group_jids_tolerates_a_non_list():
    assert _configured_group_jids(None) == []
    assert _configured_group_jids({"jid": "a@g.us"}) == []


def test_check_names_only_the_unmatched_groups_in_one_warning(caplog):
    client = GroupClient([{"jid": "here@g.us", "name": "Here"}])
    groups = [{"jid": "here@g.us"}, {"jid": "gone@g.us"}, {"jid": "typo@g.us"}]

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.whatsapp.gateway"):
        asyncio.run(_check_configured_groups(client, groups))

    warned = _warnings(caplog)
    assert len(warned) == 1, "one aggregated line, not one per group"
    assert "gone@g.us" in warned[0]
    assert "typo@g.us" in warned[0]
    assert "here@g.us" not in warned[0]
    assert "2 of 3" in warned[0]


def test_check_is_silent_when_every_configured_group_matches(caplog):
    client = GroupClient([{"jid": "a@g.us"}, {"jid": "b@g.us"}])

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.whatsapp.gateway"):
        asyncio.run(_check_configured_groups(client, [{"jid": "b@g.us"}]))

    assert _warnings(caplog) == []


def test_check_compares_exactly_so_a_case_variant_is_reported(caplog):
    """``GroupGate`` looks its entries up by exact string, so a JID that differs
    by case or a ``:device`` suffix is one the gate never matches either.
    Normalizing here would call the typo fine and leave the group mute."""
    client = GroupClient([{"jid": "abc@g.us"}])

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.whatsapp.gateway"):
        asyncio.run(_check_configured_groups(client, [{"jid": "ABC@g.us"}]))

    warned = _warnings(caplog)
    assert len(warned) == 1
    assert "ABC@g.us" in warned[0]


def test_check_stays_quiet_when_no_joined_groups_are_reported(caplog):
    """``list_groups()`` answers ``[]`` both for "in no groups" and for a swallowed
    ``get_joined_groups`` failure, so an empty answer cannot tell a stale JID from
    a probe that never ran. Naming every configured group on a transient API
    failure is what teaches an operator to ignore the warning."""
    client = GroupClient([])

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.whatsapp.gateway"):
        asyncio.run(_check_configured_groups(client, [{"jid": "gone@g.us"}]))

    assert _warnings(caplog) == []
    assert client.calls == 1, "the probe still ran; only the accusation is withheld"


def test_check_never_calls_the_client_without_configured_groups():
    client = GroupClient([{"jid": "a@g.us"}])

    asyncio.run(_check_configured_groups(client, []))

    assert client.calls == 0


def test_check_survives_a_raising_client(caplog):
    """It runs as a bare task nobody awaits, so a raise would surface only as an
    unretrieved task exception at collection time."""
    client = GroupClient(RuntimeError("socket gone"))

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.whatsapp.gateway"):
        asyncio.run(_check_configured_groups(client, [{"jid": "a@g.us"}]))

    warned = _warnings(caplog)
    assert len(warned) == 1
    assert "check failed" in warned[0]


# ── wiring: the check hangs off the CONNECTED transition ────────────────────


async def _drain_group_check() -> None:
    """Yield until the scheduled check task exists, then await it."""
    for _ in range(10):
        await asyncio.sleep(0)
        tasks = [t for t in asyncio.all_tasks() if t.get_name() == "whatsapp-group-check"]
        if tasks:
            await asyncio.gather(*tasks)
            return
    raise AssertionError("the group check was never scheduled")


@pytest.mark.asyncio
async def test_start_does_not_check_groups_before_the_connected_transition(monkeypatch):
    """``client.connect()`` returns once the attempt is merely UNDERWAY, so a check
    taken on the start path finds no joined groups and would name every configured
    one. It also keeps the round trip out of the sequence that starts the
    remaining channels."""
    created = _patch_success(monkeypatch)
    created["joined"] = [{"jid": "here@g.us"}]
    orch = _orch(state=FakeState())
    orch._cfg.whatsapp.groups = [{"jid": "gone@g.us"}]

    await maybe_start_whatsapp(orch)
    for _ in range(5):
        await asyncio.sleep(0)

    assert "list_groups_calls" not in created


@pytest.mark.asyncio
async def test_the_connected_transition_schedules_the_check(monkeypatch, caplog):
    created = _patch_success(monkeypatch)
    created["joined"] = [{"jid": "here@g.us"}]
    orch = _orch(state=FakeState())
    orch._cfg.whatsapp.groups = [{"jid": "here@g.us"}, {"jid": "gone@g.us"}]
    client = await maybe_start_whatsapp(orch)

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.whatsapp.gateway"):
        client.on_state_change("connected", "")
        await _drain_group_check()

    warned = _warnings(caplog)
    assert len(warned) == 1
    assert "gone@g.us" in warned[0]


@pytest.mark.asyncio
async def test_a_reconnect_does_not_repeat_the_warning(monkeypatch, caplog):
    """whatsmeow auto-reconnects, so CONNECTED arrives again and again over a long
    run. The operator gets told once."""
    created = _patch_success(monkeypatch)
    created["joined"] = [{"jid": "here@g.us"}]
    orch = _orch(state=FakeState())
    orch._cfg.whatsapp.groups = [{"jid": "gone@g.us"}]
    client = await maybe_start_whatsapp(orch)

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.whatsapp.gateway"):
        client.on_state_change("connected", "")
        await _drain_group_check()
        client.on_state_change("error", "disconnected (auto-reconnecting)")
        client.on_state_change("connected", "")
        for _ in range(5):
            await asyncio.sleep(0)

    assert len(_warnings(caplog)) == 1
    assert created["list_groups_calls"] == 1


@pytest.mark.asyncio
async def test_a_non_connected_transition_never_checks(monkeypatch):
    created = _patch_success(monkeypatch)
    orch = _orch(state=FakeState())
    orch._cfg.whatsapp.groups = [{"jid": "gone@g.us"}]
    client = await maybe_start_whatsapp(orch)

    client.on_state_change("pairing", "scan the QR code from your phone")
    client.on_state_change("logged_out", "unlinked")
    for _ in range(5):
        await asyncio.sleep(0)

    assert "list_groups_calls" not in created


@pytest.mark.asyncio
async def test_the_check_runs_with_no_dashboard_state(monkeypatch, caplog):
    """The observer carries two jobs and ``on_state_change`` is a single slot, so
    installing it only when a dashboard state exists would make the check
    conditional on an unrelated surface (a headless gateway has no state)."""
    created = _patch_success(monkeypatch)
    created["joined"] = [{"jid": "here@g.us"}]
    orch = _orch(state=None)
    orch._cfg.whatsapp.groups = [{"jid": "gone@g.us"}]
    client = await maybe_start_whatsapp(orch)

    assert callable(client.on_state_change)
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.whatsapp.gateway"):
        client.on_state_change("connected", "")
        await _drain_group_check()

    warned = _warnings(caplog)
    assert len(warned) == 1
    assert "gone@g.us" in warned[0]


def test_a_state_change_after_the_loop_closed_is_not_an_error(monkeypatch, caplog):
    """The badge observer outlives the loop that created it (shutdown races the
    transition). Losing a diagnostic must never surface as an error."""
    created = _patch_success(monkeypatch)
    created["joined"] = [{"jid": "here@g.us"}]
    orch = _orch(state=FakeState())
    orch._cfg.whatsapp.groups = [{"jid": "gone@g.us"}]
    client = asyncio.run(maybe_start_whatsapp(orch))

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.whatsapp.gateway"):
        client.on_state_change("connected", "")  # loop from asyncio.run() is closed

    assert _warnings(caplog) == []
    assert "loop closed" in caplog.text
