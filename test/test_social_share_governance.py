"""``capabilities.social_share`` — the switch that withdraws "Share as image".

The share card is rendered and exported in the browser; its egress is the intent
buttons, which hand the reply's caption to X / LinkedIn in a URL. There is no
server-side share action to refuse, so the control is the dashboard entry and the
only place the dashboard can learn the ceiling's answer is
``GET /api/dashboard/config`` (``social_share_enabled``). These tests pin:

* :class:`TestScopeCatalog` — adding the scope is a DATA change: one row.
* :class:`TestProbe` — the probe through the REAL evaluator: ungoverned and
  policy-silent hosts permit; a policy pin denies; a PROFILE bound to the
  dashboard surface denies too (every layer is honoured — this is a per-request
  dashboard question, not a startup probe); an unevaluable ceiling denies
  (fail-closed); the surface key is the pinned dashboard one, never a caller value.
* :class:`TestDashboardConfigEndpoint` — the GET reports the answer, EVERY
  decision leaves a ``governance_decision`` SEL row, and the PUT tolerates the
  round-tripped read-only field without persisting it.
* :class:`TestGovernanceGenerationFrame` — the WS ``slots`` frame carries the
  ceiling generation the client invalidates ``dashboardConfig`` on.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import social_share

_SCOPE = "capabilities.social_share"

#: A ceiling that pins the entry off.
_PIN_DOC: dict = {
    "version": 1,
    "boot": {"fail_closed": True},
    "capabilities": {"social_share": {"enabled": False}},
}


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point the data home at ``tmp_path`` so no test touches a real config."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def fake_sel(monkeypatch):
    """One fake behind BOTH SEL lookups on this path: the handler module's ``sel``
    (tool-invocation rows) and ``kiro_crew.sel.sel`` (the governance_decision row,
    looked up lazily at the source module by ``vet_and_audit`` and the probe)."""
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    fake = MagicMock()
    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    with patch("kiro_crew.dashboard.handlers.sel", return_value=fake):
        yield fake


def _install_policy(monkeypatch, doc: dict | None) -> None:
    """Install *doc* as the boot-frozen ceiling for the duration of a test."""
    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    ceiling = parse_policy(doc) if doc is not None else None

    class _Ctx:
        governance = ceiling

    monkeypatch.setattr(pc, "current_context", lambda: _Ctx())


def _governance_rows(fake: MagicMock) -> list[dict]:
    return [c[1] for c in fake.log_governance_decision.call_args_list]


# ── The catalog row ───────────────────────────────────────────────────────


class TestScopeCatalog:
    def test_row_is_registered_as_a_default_on_capability(self) -> None:
        from kiro_crew.platform.governance import CAPABILITY, SCOPE_CATALOG

        spec = SCOPE_CATALOG[_SCOPE]
        assert spec.kind == CAPABILITY
        # A policy that governs some OTHER capabilities.* row must not silently
        # withdraw a menu entry it never mentioned.
        assert spec.capability_default is True

    def test_a_policy_can_actually_express_the_pin(self) -> None:
        from kiro_crew.platform.governance import CapabilityGate, parse_policy

        gate = parse_policy(_PIN_DOC).get(_SCOPE)
        assert isinstance(gate, CapabilityGate)
        assert gate.enabled is False


# ── The probe ─────────────────────────────────────────────────────────────


class TestProbe:
    def test_ungoverned_host_permits(self, isolated_home, fake_sel, monkeypatch) -> None:
        _install_policy(monkeypatch, None)
        assert social_share.is_share_denied() is False

    def test_policy_silent_about_sharing_still_permits(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        _install_policy(
            monkeypatch,
            {"version": 1, "boot": {"fail_closed": True}, "apps": {"mode": "deny", "deny": ["x"]}},
        )
        assert social_share.is_share_denied() is False

    def test_a_policy_pin_denies(self, isolated_home, fake_sel, monkeypatch) -> None:
        _install_policy(monkeypatch, _PIN_DOC)
        assert social_share.is_share_denied() is True

    def test_a_profile_bound_to_the_dashboard_surface_denies(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        """A denied decision from ANY layer withdraws the entry. Unlike the startup
        probes (telemetry, tailnet) this answers a per-request dashboard question,
        so a Level-2 profile narrowing the dashboard surface must bind here."""
        from kiro_crew.platform import governance_profiles as gp
        from kiro_crew.platform.governance import CapabilityGate, Profile

        monkeypatch.setattr(
            gp,
            "resolve_active_scope",
            lambda *a, **k: Profile(
                name="narrow", controls={_SCOPE: CapabilityGate(enabled=False)}
            ),
        )
        _install_policy(monkeypatch, {"version": 1, "boot": {"fail_closed": True}})
        assert social_share.is_share_denied() is True

    def test_evaluates_on_the_pinned_dashboard_surface_never_a_caller_value(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        """The surface key is what a profile binds on; letting a request header pick
        it would let a caller dodge a dashboard-bound profile."""
        seen: list[dict] = []

        def _spy(scope, item, **kw):
            seen.append({"scope": scope, "item": item, **kw})
            return MagicMock(permitted=True)

        monkeypatch.setattr(social_share, "vet_and_audit", _spy)
        assert social_share.is_share_denied() is False
        assert len(seen) == 1
        assert seen[0]["scope"] == _SCOPE
        assert seen[0]["session_key"] == "dashboard:ui"
        assert seen[0]["tool_name"] == social_share.AUDIT_TOOL
        assert seen[0]["fail_closed"] is True

    def test_evaluation_error_fails_CLOSED(self, isolated_home, fake_sel, monkeypatch) -> None:
        """An unevaluable ceiling must NOT offer the post-to-a-third-party button."""
        from kiro_crew.platform import context as pc
        from kiro_crew.platform import governance_profiles as gp
        from kiro_crew.platform.governance import parse_policy

        monkeypatch.setattr(
            gp,
            "resolve_active_scope",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        class _Ctx:
            governance = parse_policy({"version": 1, "boot": {"fail_closed": True}})

        monkeypatch.setattr(pc, "current_context", lambda: _Ctx())
        assert social_share.is_share_denied() is True

    def test_an_unexpected_probe_error_also_fails_closed_and_is_audited(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        """``vet_and_audit`` never ran, so the seam wrote nothing: the probe must
        record the denial it is about to act on itself."""

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(social_share, "vet_and_audit", _boom)
        assert social_share.is_share_denied() is True
        rows = _governance_rows(fake_sel)
        assert len(rows) == 1
        assert rows[0]["scope"] == _SCOPE
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["tool_name"] == social_share.AUDIT_TOOL
        assert "fail-closed" in rows[0]["reason"]


# ── The endpoint ──────────────────────────────────────────────────────────


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


@pytest.fixture()
def handler_app(cfg_file, fake_sel):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    app = web.Application()
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    app.router.add_get("/api/dashboard/config", api_dashboard_config)
    return as_owner(app)


class TestDashboardConfigEndpoint:
    @pytest.mark.asyncio
    async def test_get_reports_enabled_on_an_ungoverned_host(
        self, handler_app, isolated_home, monkeypatch
    ) -> None:
        _install_policy(monkeypatch, None)
        async with TestClient(TestServer(handler_app)) as client:
            resp = await client.get("/api/dashboard/config")
            assert resp.status == 200
            assert (await resp.json())["social_share_enabled"] is True

    @pytest.mark.asyncio
    async def test_get_reports_disabled_when_the_policy_pins_it(
        self, handler_app, isolated_home, monkeypatch
    ) -> None:
        _install_policy(monkeypatch, _PIN_DOC)
        async with TestClient(TestServer(handler_app)) as client:
            resp = await client.get("/api/dashboard/config")
            assert resp.status == 200
            assert (await resp.json())["social_share_enabled"] is False

    @pytest.mark.asyncio
    async def test_every_decision_is_audited(
        self, handler_app, fake_sel, isolated_home, monkeypatch
    ) -> None:
        """Each answer the dashboard acts on leaves a ``governance_decision`` row —
        grant and denial alike, one per evaluation, through the shared seam."""
        _install_policy(monkeypatch, _PIN_DOC)
        async with TestClient(TestServer(handler_app)) as client:
            for _ in range(2):
                assert (await (await client.get("/api/dashboard/config")).json())[
                    "social_share_enabled"
                ] is False
            rows = _governance_rows(fake_sel)
            assert [r["outcome"] for r in rows] == ["denied", "denied"]
            assert {r["scope"] for r in rows} == {_SCOPE}
            assert {r["tool_name"] for r in rows} == {social_share.AUDIT_TOOL}
            assert {r["session_key"] for r in rows} == {"dashboard:ui"}

            _install_policy(monkeypatch, None)
            assert (await (await client.get("/api/dashboard/config")).json())[
                "social_share_enabled"
            ] is True
            rows = _governance_rows(fake_sel)
            assert [r["outcome"] for r in rows] == ["denied", "denied", "allowed"]

    @pytest.mark.asyncio
    async def test_round_tripped_field_does_not_reject_an_unrelated_save(
        self, handler_app, cfg_file, isolated_home, monkeypatch
    ) -> None:
        """Both settings surfaces PUT the spread GET body back, so a read-only field
        has to be dropped rather than rejected or every toggle save 400s."""
        _install_policy(monkeypatch, None)
        async with TestClient(TestServer(handler_app)) as client:
            body = await (await client.get("/api/dashboard/config")).json()
            assert "social_share_enabled" in body
            body["restore_sessions"] = True
            resp = await client.put("/api/dashboard/config", json=body)
            assert resp.status == 200, await resp.text()
            # …and the PUT never persisted it: governance, not config, owns the answer.
            assert "social_share_enabled" not in cfg_file.read_text(encoding="utf-8")


# ── The WS generation frame ───────────────────────────────────────────────


class TestGovernanceGenerationFrame:
    def test_the_slots_frame_carries_the_ceiling_generation(self) -> None:
        """Both slots frames come from ``_slots_ws_frame``; the key the client
        invalidates ``dashboardConfig`` on must be in it."""
        from kiro_crew.dashboard.state import _slots_ws_frame

        frame = json.loads(
            _slots_ws_frame(
                [],
                yolo=False,
                channel_trusted=False,
                gitlab_hosts_gen=1,
                folders=[],
                folders_gen=2,
                governance_gen=7,
            )
        )
        assert frame["governanceGeneration"] == 7
