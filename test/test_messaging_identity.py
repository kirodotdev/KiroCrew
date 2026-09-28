"""Unit tests for the shared per-turn identity publisher (messaging.identity).

These lock the publish semantics that every turn-running surface now delegates
to via ``publish_turn_identity``: publish with the session's host pid
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
        assert rec["item"] == "discord"
        assert rec["tool_name"] == "inbound:discord"
    finally:
        gp.reset_store()


def test_inbound_governed_allow_denies_on_audit_failure(monkeypatch, tmp_path) -> None:
    # A GOVERNED ALLOW is audit-or-deny. If the SEL
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
    # The UNGOVERNED default-permit must NOT be audited. This
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


# ── Outbound channels-governance gate (a send is not a received message) ──


def test_outbound_permitted_when_no_policy(monkeypatch, tmp_path) -> None:
    # Default OSS build: no channels policy → every transport's outbound permits,
    # so sends are byte-identical to a build without this gate.
    from kiro_crew.platform import governance_profiles as gp

    monkeypatch.setattr(gp, "_PROFILES_DIR", tmp_path / "profiles")
    gp.reset_store()
    try:
        assert asyncio.run(identity.channel_outbound_permitted("discord")) is True
        assert asyncio.run(identity.channel_outbound_permitted("telegram")) is True
    finally:
        gp.reset_store()


def test_outbound_fail_closed_on_governance_error(monkeypatch) -> None:
    # The caller is about to write to a destination whose standing it cannot
    # establish, so an evaluation error DENIES.
    def _boom(*_a, **_k):
        raise RuntimeError("evaluation glitch")

    monkeypatch.setattr("kiro_crew.messaging.identity.governance_permits", _boom)
    assert asyncio.run(identity.channel_outbound_permitted("discord")) is False


def test_outbound_reraises_platform_composition_error(monkeypatch) -> None:
    # Matches the inbound sibling and the host gate: a broken composition surfaces.
    from kiro_crew.platform.context import PlatformCompositionError

    def _boom(*_a, **_k):
        raise PlatformCompositionError("companion mismatch")

    monkeypatch.setattr("kiro_crew.messaging.identity.governance_permits", _boom)
    import pytest

    with pytest.raises(PlatformCompositionError):
        asyncio.run(identity.channel_outbound_permitted("discord"))


def _decision(permitted: bool, layer: str):
    return type("_D", (), {"permitted": permitted, "layer": layer, "rule": "r", "reason": "why"})()


def test_outbound_rows_name_the_outbound_direction(monkeypatch) -> None:
    # A send filed under an ingress name is unreadable to whoever later asks why a
    # message did not go out, so the direction is part of the record.
    rows: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kw):
            rows.append(kw)

    monkeypatch.setattr("kiro_crew.messaging.identity.sel", lambda: _Sel())
    monkeypatch.setattr(
        "kiro_crew.messaging.identity.governance_permits",
        lambda *a, **k: _decision(False, "policy"),
    )
    assert asyncio.run(identity.channel_outbound_permitted("discord")) is False
    assert [r["tool_name"] for r in rows] == ["outbound:discord"]
    assert rows[0]["outcome"] == "denied"


def test_the_governed_outbound_allow_row_is_written_critically(monkeypatch) -> None:
    # The unguarded call site is what makes the failure propagate, but the flag is
    # what tells SEL to treat the write as one that may not be dropped. Nothing else
    # asserts it: a fake store that raises on every call fails the same way with the
    # flag absent, so without this pin it could be removed silently.
    rows: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kw):
            rows.append(kw)

    monkeypatch.setattr("kiro_crew.messaging.identity.sel", lambda: _Sel())
    monkeypatch.setattr(
        "kiro_crew.messaging.identity.governance_permits",
        lambda *a, **k: _decision(True, "policy"),
    )
    assert asyncio.run(identity.channel_outbound_permitted("discord")) is True
    assert [r["tool_name"] for r in rows] == ["outbound:discord"]
    assert rows[0].get("critical") is True


def test_an_unrecordable_governed_allow_fails_closed(monkeypatch) -> None:
    # A governed allow nobody can record is not an allow: the row IS the permission
    # event, and an egress decision is the one a reader needs most. Matches the
    # inbound sibling, whose governed-allow write is critical and unguarded, so an
    # unwritable store cannot leave one direction recorded and the other silent.
    class _Sel:
        def log_governance_decision(self, **kw):
            raise OSError("read-only file system")

    monkeypatch.setattr("kiro_crew.messaging.identity.sel", lambda: _Sel())
    monkeypatch.setattr(
        "kiro_crew.messaging.identity.governance_permits",
        lambda *a, **k: _decision(True, "policy"),
    )
    assert asyncio.run(identity.channel_outbound_permitted("discord")) is False


def test_an_unwritable_audit_store_still_lets_a_deny_answer(monkeypatch) -> None:
    # The refusal already stands, so the deny row is best-effort: audit-store disk
    # health must not turn a clean "no" into a degraded one. Asserting False alone
    # would prove nothing -- a deny is False either way -- so this watches WHICH
    # handler absorbed the write failure: the deny's own, not the outer degrade path.
    degraded: list[str] = []

    class _Sel:
        def log_governance_decision(self, **kw):
            raise OSError("read-only file system")

    monkeypatch.setattr("kiro_crew.messaging.identity.sel", lambda: _Sel())
    monkeypatch.setattr(
        "kiro_crew.messaging.identity.audit_governance_degraded",
        lambda *a, **k: degraded.append(a[0] if a else ""),
    )
    monkeypatch.setattr(
        "kiro_crew.messaging.identity.governance_permits",
        lambda *a, **k: _decision(False, "policy"),
    )
    assert asyncio.run(identity.channel_outbound_permitted("discord")) is False
    assert degraded == []


def test_an_unrecordable_governed_allow_reports_itself_degraded(monkeypatch) -> None:
    # The mirror of the pin above: the allow's write is critical, so its failure must
    # reach the outer handler and be recorded as a degraded refusal rather than pass
    # silently. Together the two pins fix each write's criticality in place.
    degraded: list[str] = []

    class _Sel:
        def log_governance_decision(self, **kw):
            raise OSError("read-only file system")

    monkeypatch.setattr("kiro_crew.messaging.identity.sel", lambda: _Sel())
    monkeypatch.setattr(
        "kiro_crew.messaging.identity.audit_governance_degraded",
        lambda *a, **k: degraded.append(a[0] if a else ""),
    )
    monkeypatch.setattr(
        "kiro_crew.messaging.identity.governance_permits",
        lambda *a, **k: _decision(True, "policy"),
    )
    assert asyncio.run(identity.channel_outbound_permitted("discord")) is False
    assert degraded == ["outbound:discord"]


def test_an_ungoverned_outbound_allow_writes_no_row(monkeypatch) -> None:
    # Nothing was governed, so there is no decision to record, and a row per send
    # on an install with no policy would be hot-path write amplification.
    rows: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kw):
            rows.append(kw)

    monkeypatch.setattr("kiro_crew.messaging.identity.sel", lambda: _Sel())
    monkeypatch.setattr(
        "kiro_crew.messaging.identity.governance_permits",
        lambda *a, **k: _decision(True, ""),
    )
    assert asyncio.run(identity.channel_outbound_permitted("discord")) is True
    assert rows == []
