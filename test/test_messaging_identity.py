"""Unit tests for the shared per-turn identity publisher (messaging.identity).

These lock the publish semantics that every turn-running surface now delegates
to via ``publish_turn_identity`` (#232): publish with the session's host pid
and key, no-op when the pid is not yet known, and never let a failure break the
turn.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from kiro_crew.messaging import identity


class _Sessions:
    def __init__(self, pid: object) -> None:
        self._pid = pid

    def get_pid(self, key: str) -> object:
        return self._pid


def test_publishes_with_host_pid_and_key() -> None:
    sessions = _Sessions(4242)
    with patch.object(identity, "publish_session_pid") as pub:
        asyncio.run(identity.publish_turn_identity(sessions, "telegram:kirocrew:direct:7"))
        pub.assert_called_once_with(4242, "telegram:kirocrew:direct:7")


def test_no_publish_when_pid_unavailable() -> None:
    sessions = _Sessions(None)  # session not spawned yet -> get_pid None
    with patch.object(identity, "publish_session_pid") as pub:
        asyncio.run(identity.publish_turn_identity(sessions, "slack:kirocrew:C1:T1"))
        pub.assert_not_called()


def test_swallows_get_pid_error() -> None:
    sessions = MagicMock()
    sessions.get_pid.side_effect = RuntimeError("boom")
    with patch.object(identity, "publish_session_pid") as pub:
        # A get_pid / filesystem failure must never propagate out of a turn.
        asyncio.run(identity.publish_turn_identity(sessions, "discord:kirocrew:g:t"))
        pub.assert_not_called()


# ── Inbound channels-governance gate (HIGH: enforce on inbound dispatch) ──


def test_inbound_permitted_when_no_policy(monkeypatch, tmp_path) -> None:
    # Default OSS build: no channels policy → every transport's inbound permits,
    # so inbound handling is byte-identical to today.
    from kiro_crew.platform import governance_profiles as gp

    monkeypatch.setattr(gp, "_PROFILES_DIR", tmp_path / "profiles")
    gp.reset_store()
    try:
        assert asyncio.run(identity.channel_inbound_permitted("discord")) is True
        assert asyncio.run(identity.channel_inbound_permitted("telegram")) is True
    finally:
        gp.reset_store()


def test_inbound_denied_after_host_profile_hot_reload(monkeypatch, tmp_path) -> None:
    # The gap the startup-only gate left: a host-surface profile added AFTER the
    # transport connected must stop inbound dispatch on the very next message. The
    # ProfileStore hot-reloads by mtime, so the per-message recheck picks it up.
    import json

    from kiro_crew.platform import governance_profiles as gp

    pdir = tmp_path / "profiles"
    pdir.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
    gp.reset_store()
    try:
        # Initially permitted (no profile denies discord).
        assert asyncio.run(identity.channel_inbound_permitted("discord")) is True
        # Operator drops a host profile that allows ONLY slack → discord denied.
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
                }
            )
        )
        assert asyncio.run(identity.channel_inbound_permitted("discord")) is False
        # Slack itself stays permitted by that same allowlist.
        assert asyncio.run(identity.channel_inbound_permitted("slack")) is True
    finally:
        gp.reset_store()


def test_inbound_fail_closed_on_governance_error(monkeypatch) -> None:
    # An inbound is externally reachable, so an internal governance-evaluation
    # error must DENY (fail-closed), not dispatch an ungoverned turn.
    def _boom(*_a, **_k):
        raise RuntimeError("evaluation glitch")

    monkeypatch.setattr("kiro_crew.messaging.identity.governance_permits", _boom)
    assert asyncio.run(identity.channel_inbound_permitted("discord")) is False


def test_inbound_reraises_platform_composition_error(monkeypatch) -> None:
    # A broken CPP composition must surface (matches the host gate), not be
    # silently swallowed into a blanket deny.
    from kiro_crew.platform.context import PlatformCompositionError

    def _boom(*_a, **_k):
        raise PlatformCompositionError("companion mismatch")

    monkeypatch.setattr("kiro_crew.messaging.identity.governance_permits", _boom)
    import pytest

    with pytest.raises(PlatformCompositionError):
        asyncio.run(identity.channel_inbound_permitted("discord"))


def test_inbound_governed_deny_is_sel_audited(monkeypatch, tmp_path) -> None:
    # HIGH (GPT pass 1 #3): a GOVERNED inbound decision must leave a durable SEL
    # audit record (the blocking governance-audit rule). A host profile that
    # allows only slack → discord denied → one governance_decision SEL written.
    import json

    from kiro_crew.platform import governance_profiles as gp

    pdir = tmp_path / "profiles"
    pdir.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
    gp.reset_store()

    audited: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kw):
            audited.append(kw)

    monkeypatch.setattr("kiro_crew.messaging.identity.sel", lambda: _Sel())
    try:
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
                }
            )
        )
        assert asyncio.run(identity.channel_inbound_permitted("discord")) is False
        assert len(audited) == 1
        rec = audited[0]
        assert rec["outcome"] == "denied"
        assert rec["scope"] == "channels"
        # The audited item is the fully-qualified CONNECTION: a per-bot rule has to
        # be addressable, so the gate never queries the bare transport.
        assert rec["item"] == "discord/default"
        assert rec["tool_name"] == "inbound:discord"
    finally:
        gp.reset_store()


def test_inbound_governed_allow_denies_on_audit_failure(monkeypatch, tmp_path) -> None:
    # HIGH (GPT round-5 pass 1 #2): a GOVERNED ALLOW is audit-or-deny. If the SEL
    # write can't be persisted, the inbound must be DENIED (fail-closed), never
    # drive a turn unaudited — matching the host transport-start gate.
    import json

    from kiro_crew.platform import governance_profiles as gp

    pdir = tmp_path / "profiles"
    pdir.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
    gp.reset_store()

    class _Sel:
        def log_governance_decision(self, **kw):
            # Only the governed ALLOW is critical; simulate an unwritable SEL.
            raise OSError("SEL disk full")

    monkeypatch.setattr("kiro_crew.messaging.identity.sel", lambda: _Sel())
    try:
        # A host profile that ALLOWS slack → an inbound slack message is a GOVERNED
        # allow; the failing critical audit must flip it to denied.
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
                }
            )
        )
        assert asyncio.run(identity.channel_inbound_permitted("slack")) is False
    finally:
        gp.reset_store()


def test_inbound_ungoverned_permit_is_not_audited(monkeypatch, tmp_path) -> None:
    # Design review (#593): the UNGOVERNED default-permit must NOT be audited. This
    # gate is on the per-message hot path of five transports (including observe-mode
    # traffic the bot merely sees), so recording the default-permit would append one
    # HMAC-chained SEL row per message on every install with no governance
    # configured — hot-path write amplification that also drowns real governance
    # signal. There is no decision to record: nothing was governed. Governed
    # decisions and every deny ARE recorded (covered by the sibling tests).
    from kiro_crew.platform import governance_profiles as gp

    monkeypatch.setattr(gp, "_PROFILES_DIR", tmp_path / "profiles")
    gp.reset_store()

    audited: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kw):
            audited.append(kw)

    monkeypatch.setattr("kiro_crew.messaging.identity.sel", lambda: _Sel())
    try:
        assert asyncio.run(identity.channel_inbound_permitted("discord")) is True
        assert audited == [], (
            "the ungoverned default-permit must not write a SEL record — one row per "
            f"inbound message on a default install (got {audited})"
        )
    finally:
        gp.reset_store()


def test_inbound_ungoverned_permit_survives_audit_failure(monkeypatch, tmp_path) -> None:
    # An ungoverned allow is best-effort: a SEL write failure must NOT flip it to
    # denied (OSS availability doesn't hinge on SEL disk health) — only a GOVERNED
    # allow is audit-or-deny.
    from kiro_crew.platform import governance_profiles as gp

    monkeypatch.setattr(gp, "_PROFILES_DIR", tmp_path / "profiles")
    gp.reset_store()

    class _Sel:
        def log_governance_decision(self, **kw):
            raise OSError("SEL disk full")

    monkeypatch.setattr("kiro_crew.messaging.identity.sel", lambda: _Sel())
    try:
        # No policy → ungoverned allow → still permitted despite the audit failure.
        assert asyncio.run(identity.channel_inbound_permitted("discord")) is True
    finally:
        gp.reset_store()
