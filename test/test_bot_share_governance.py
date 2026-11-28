"""``capabilities.bot_share`` -- the switch that withdraws Bot export and Bot import.

A ``.kirobot`` file carries a Bot's rules, goals, cron jobs with their scripts and the
skills its sender wrote, and neither direction was covered by any control. These tests
pin:

* :class:`TestScopeCatalog` -- the row is one default-on capability, and the enterprise
  baseline turns it off.
* :class:`TestProbe` -- the probe through the REAL evaluator: ungoverned and
  policy-silent hosts permit, a policy pin denies, an unevaluable ceiling denies
  (fail-closed), and the surface is the pinned dashboard one.
* :class:`TestRoutes` -- the export route (and the lessons draft that feeds it) and
  the import route refuse a denied fleet with 403 ``governance_denied`` before any
  body is read, audit the decision, and let a permitted fleet through to its own
  checks.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import governance_capability_checks
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import bot_share
from kiro_crew.dashboard.handlers import change_cards, member_export

_SCOPE = "capabilities.bot_share"

#: A ceiling that pins both directions off.
_PIN_DOC: dict = {
    "version": 1,
    "boot": {"fail_closed": True},
    "capabilities": {"bot_share": {"enabled": False}},
}

#: A ceiling that governs some OTHER capability and never names this one.
_SILENT_DOC: dict = {
    "version": 1,
    "boot": {"fail_closed": True},
    "capabilities": {"social_share": {"enabled": False}},
}


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_sel(monkeypatch):
    """One fake behind the SEL lookups on this path, so each audit row is observable."""
    fake = MagicMock()
    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    return fake


def _install_policy(monkeypatch, doc: dict | None) -> None:
    """Install *doc* as the boot-frozen ceiling for the duration of a test."""
    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    ceiling = parse_policy(doc) if doc is not None else None

    class _Ctx:
        governance = ceiling

    monkeypatch.setattr(pc, "current_context", lambda: _Ctx())


def _rows(fake: MagicMock) -> list[dict]:
    return [c[1] for c in fake.log_governance_decision.call_args_list]


# -- The catalog row ----------------------------------------------------------


class TestScopeCatalog(governance_capability_checks.RowIsRegisteredAsADefaultOnCapability):
    def test_a_policy_can_actually_express_the_pin(self) -> None:
        from kiro_crew.platform.governance import CapabilityGate, parse_policy

        gate = parse_policy(_PIN_DOC).get(_SCOPE)
        assert isinstance(gate, CapabilityGate)
        assert gate.enabled is False

    def test_the_enterprise_baseline_turns_it_off(self) -> None:
        from kiro_crew.platform.governance_parts.baseline import BASELINE_CONTROLS

        assert BASELINE_CONTROLS[_SCOPE]["enabled"] is False


# -- The probe ---------------------------------------------------------------


class TestProbe:
    def test_ungoverned_host_permits(self, isolated_home, fake_sel, monkeypatch) -> None:
        _install_policy(monkeypatch, None)
        assert bot_share.is_bot_share_denied() is False

    def test_a_policy_that_never_names_the_row_permits(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        _install_policy(monkeypatch, _SILENT_DOC)
        assert bot_share.is_bot_share_denied() is False

    def test_a_policy_pin_denies_and_the_decision_is_audited(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        _install_policy(monkeypatch, _PIN_DOC)

        assert bot_share.is_bot_share_denied() is True

        rows = _rows(fake_sel)
        assert [(r["scope"], r["outcome"]) for r in rows] == [(_SCOPE, "denied")]
        assert rows[0]["tool_name"] == bot_share.AUDIT_TOOL

    def test_an_unevaluable_ceiling_denies_and_says_so(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        def _boom(*_a, **_kw):
            raise RuntimeError("ceiling unreadable")

        monkeypatch.setattr(bot_share, "vet_and_audit", _boom)

        assert bot_share.is_bot_share_denied() is True

        rows = _rows(fake_sel)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "denied"
        assert "fail-closed" in rows[0]["reason"]

    def test_the_surface_is_the_pinned_dashboard_one(self, monkeypatch) -> None:
        seen: dict = {}

        def _spy(scope, item, **kw):
            seen.update(kw, scope=scope, item=item)
            return MagicMock(permitted=True)

        monkeypatch.setattr(bot_share, "vet_and_audit", _spy)
        bot_share.is_bot_share_denied()

        assert seen["scope"] == _SCOPE
        # A caller-controlled session header never picks the surface.
        assert seen["session_key"] == "dashboard:ui"
        assert seen["fail_closed"] is True


# -- The routes --------------------------------------------------------------


def _app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/members/{slug}/export", member_export.api_member_export)
    app.router.add_post(
        "/api/members/{slug}/export/lessons-draft", member_export.api_member_export_lessons_draft
    )
    app.router.add_post("/api/members/import", change_cards.api_members_import)
    return as_owner(app)


async def _post(path: str, **kw):
    client = TestClient(TestServer(_app()))
    await client.start_server()
    try:
        response = await client.post(path, **kw)
        return response.status, await response.json()
    finally:
        await client.close()


class TestRoutes:
    @pytest.mark.parametrize(
        ("path", "kw"),
        [
            ("/api/members/scout/export", {"json": {"member": "scout"}}),
            ("/api/members/scout/export", {"json": {"member": "scout", "preview": True}}),
            ("/api/members/scout/export/lessons-draft", {"json": {"member": "scout"}}),
            (
                "/api/members/import",
                {"data": b"not read", "headers": {"Content-Type": "application/zip"}},
            ),
        ],
        ids=["export-download", "export-preview", "lessons-draft", "import"],
    )
    @pytest.mark.asyncio
    async def test_a_denied_fleet_is_refused_before_any_work(
        self, isolated_home, fake_sel, monkeypatch, path, kw
    ) -> None:
        _install_policy(monkeypatch, _PIN_DOC)

        status, body = await _post(path, **kw)

        assert status == 403
        assert body["code"] == "governance_denied"
        assert body["error"] == bot_share.DENIED_MESSAGE
        assert [r["outcome"] for r in _rows(fake_sel)] == ["denied"]

    @pytest.mark.parametrize(
        ("path", "kw", "code"),
        [
            ("/api/members/scout/export", {"data": b"{not json"}, "invalid_json"),
            (
                "/api/members/import",
                {"data": b"", "headers": {"Content-Type": "application/zip"}},
                "invalid_bundle",
            ),
        ],
        ids=["export", "import"],
    )
    @pytest.mark.asyncio
    async def test_a_permitted_fleet_reaches_the_routes_own_checks(
        self, isolated_home, fake_sel, monkeypatch, path, kw, code
    ) -> None:
        _install_policy(monkeypatch, None)

        _status, body = await _post(path, **kw)

        # The route's own refusal for this body, not the governance one.
        assert body["code"] == code, body

    @pytest.mark.asyncio
    async def test_a_non_owner_gets_the_owner_refusal_before_the_gate_is_asked(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        _install_policy(monkeypatch, _PIN_DOC)

        status, body = await _post(
            "/api/members/scout/export",
            json={"member": "scout"},
            headers={"X-Test-User": "someone-else"},
        )

        assert status == 403
        assert body.get("code") != "governance_denied"
        # The gate was never evaluated, so no governance decision was recorded.
        assert _rows(fake_sel) == []

    @pytest.mark.asyncio
    async def test_a_denied_import_stages_nothing(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        from kiro_crew import crewmate_stage

        _install_policy(monkeypatch, _PIN_DOC)

        status, body = await _post(
            "/api/members/import",
            data=b"PK\x03\x04 not a real file",
            headers={"Content-Type": "application/zip"},
        )

        assert (status, body["code"]) == (403, "governance_denied")
        assert not crewmate_stage.stage_dir().exists()
