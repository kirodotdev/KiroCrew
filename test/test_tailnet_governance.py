"""``capabilities.tailnet_origin`` — the tailnet pin the running app cannot lift.

The tailnet origin derivation puts a MagicDNS name into the CSRF origin allowlist
and the DNS-rebinding ``Host`` barrier, so the operator switch
(``dashboard.tailscale.enabled``) is a control the app — and its agent — can reach,
and an enterprise ceiling is the one that outranks it. These tests are weighted
accordingly:

* :class:`TestGovernancePin` drives the probe through the REAL evaluator rather
  than stubbing it, so a change to the scope catalog or the composition algebra
  fails here. It pins all three shapes ``beacon.TestGovernancePin`` pins: a real
  policy pin, the transient profile race that must NOT read as a pin, and the
  fail-closed degrade.
* :class:`TestResolveChokepoint`, :class:`TestPatchChokepoint` and
  :class:`TestCliChokepoint` cover the three write/act chokepoints. Any one alone
  is a half-control, so each is tested for BOTH directions: refusing ``true`` and
  still allowing ``false`` (tightest-wins).
* :class:`TestStatusState` pins all four ``state`` values, because the frontend
  never re-derives them.
* :class:`TestBothStartupSitesStash` exists because an earlier round of this
  feature already shipped a bug from touching one startup site and not the other.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import tailnet

_SCOPE = "capabilities.tailnet_origin"

#: A ceiling that pins the derivation off.
_PIN_DOC: dict = {
    "version": 1,
    "boot": {"fail_closed": True},
    "capabilities": {"tailnet_origin": {"enabled": False}},
}

_HOST = "desk.tail1a2b3c.ts.net"


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point the data home at ``tmp_path`` so no test touches a real config."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _install_policy(monkeypatch, doc: dict | None) -> None:
    """Install *doc* as the boot-frozen ceiling for the duration of a test."""
    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    ceiling = parse_policy(doc) if doc is not None else None

    class _Ctx:
        governance = ceiling

    monkeypatch.setattr(pc, "current_context", lambda: _Ctx())


# ── The catalog row ───────────────────────────────────────────────────────


class TestScopeCatalog:
    """Adding the scope must be a DATA change: one row, nothing else."""

    def test_row_is_registered_as_a_default_on_capability(self) -> None:
        from kiro_crew.platform.governance import CAPABILITY, SCOPE_CATALOG

        spec = SCOPE_CATALOG[_SCOPE]
        assert spec.kind == CAPABILITY
        # Default True or a policy that governs some OTHER capabilities.* row
        # would silently forbid a feature it never mentioned.
        assert spec.capability_default is True

    def test_a_policy_can_actually_express_the_pin(self) -> None:
        """The loader parses the row without a matcher/scale registry entry."""
        from kiro_crew.platform.governance import CapabilityGate, parse_policy

        gate = parse_policy(_PIN_DOC).get(_SCOPE)
        assert isinstance(gate, CapabilityGate)
        assert gate.enabled is False


# ── The probe ─────────────────────────────────────────────────────────────


class TestGovernancePin:
    def test_ungoverned_host_is_not_pinned(self, isolated_home, monkeypatch) -> None:
        _install_policy(monkeypatch, None)
        assert tailnet.is_governance_pinned_off() is False

    def test_policy_silent_about_tailnet_still_permits(self, isolated_home, monkeypatch) -> None:
        """capability_default=True — a ceiling governing other scopes must not
        incidentally forbid a feature it never mentioned."""
        _install_policy(
            monkeypatch,
            {"version": 1, "boot": {"fail_closed": True}, "apps": {"mode": "deny", "deny": ["x"]}},
        )
        assert tailnet.is_governance_pinned_off() is False

    def test_a_real_policy_pin_is_detected(self, isolated_home, monkeypatch) -> None:
        """Narrowing to ``layer == "policy"`` is only correct if a genuine Level-1
        pin still reports ``policy``. ``resolve`` wraps the CapabilityGate's own
        ``layer="both"``, so this asserts the wrapping rather than assuming it."""
        from kiro_crew.platform.governance import parse_policy, resolve

        decision = resolve(parse_policy(_PIN_DOC), None, _SCOPE, "")
        assert decision.permitted is False
        assert decision.layer == "policy", "the probe's layer check depends on this"

        _install_policy(monkeypatch, _PIN_DOC)
        assert tailnet.is_governance_pinned_off() is True

    def test_a_transient_profile_race_is_not_an_admin_pin(
        self, isolated_home, monkeypatch
    ) -> None:
        """A deny-all PROFILE on an UNGOVERNED host must not read as a policy pin.

        ``resolve_active_scope`` hands back a synthetic ``_deny_all_unloaded:…``
        profile when the profile store is unprimed and another thread holds its
        non-blocking reload lock. There is no policy on such a host, so reporting a
        pin would make the startup warning, the 403 and the CLI refusal all blame an
        administrator who does not exist.

        This is the failure the fail-closed ``except`` CANNOT catch: it arrives as
        an ordinary ``permitted=False`` Decision, not an exception — which is why
        the probe keys on ``layer``, not on ``permitted`` alone.
        """
        from kiro_crew.platform import governance_profiles as gp

        monkeypatch.setattr(
            gp, "resolve_active_scope", lambda *a, **k: gp.deny_all_profile("_deny_all_unloaded:x")
        )
        _install_policy(monkeypatch, None)
        assert tailnet.is_governance_pinned_off() is False

    def test_a_profile_layer_deny_on_a_governed_host_is_also_not_a_pin(
        self, isolated_home, monkeypatch
    ) -> None:
        """Same rule with a real ceiling present, so the ``layer`` test — not the
        ``ceiling is None`` short-circuit — is what produces the answer."""
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
        assert tailnet.is_governance_pinned_off() is False

    def test_evaluation_error_fails_CLOSED(self, isolated_home, monkeypatch) -> None:
        """An unevaluable ceiling must NOT widen the origin allowlist.

        The wrong-DENY costs a convenience the host did not have anyway; the
        wrong-PERMIT grows the set of origins the gateway accepts authenticated
        state-changing requests from, on a fleet that forbade it.
        """
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
        assert tailnet.is_governance_pinned_off() is True

    def test_an_unexpected_probe_error_also_fails_closed(self, isolated_home, monkeypatch) -> None:
        """The probe's own ``except`` takes the same disposition as a degrade.

        ``governance_permits`` converts its internal errors into a Decision, so this
        handler is only reached when something outside that contract raises (a
        ``PlatformCompositionError``, which is documented to propagate, or the
        import itself). That is still an unevaluable ceiling.
        """

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(tailnet, "governance_permits", _boom)
        assert tailnet.is_governance_pinned_off() is True


# ── Chokepoint (a): the action ────────────────────────────────────────────


class TestResolveChokepoint:
    @pytest.mark.asyncio
    async def test_pin_stops_the_derivation_and_the_daemon_call(
        self, isolated_home, monkeypatch, caplog
    ) -> None:
        """No name, and the daemon is not even consulted."""
        probed: list[str] = []

        def _probe() -> str:
            probed.append("probed")
            return _HOST

        monkeypatch.setattr(tailnet, "self_dns_name", _probe)
        _install_policy(monkeypatch, _PIN_DOC)
        with caplog.at_level("WARNING"):
            assert await tailnet.resolve_tailnet_host(True) == ""
        assert probed == [], "a pinned host must not spawn the tailnet CLI"
        # A DISTINCT warning from the enabled-but-unresolved one: sending the
        # operator to `tailscale status` would be a wild goose chase.
        rendered = caplog.text
        assert "administrator" in rendered and _SCOPE in rendered
        assert "restart the gateway" not in rendered

    @pytest.mark.asyncio
    async def test_unpinned_host_still_derives(self, isolated_home, monkeypatch) -> None:
        monkeypatch.setattr(tailnet, "self_dns_name", lambda: _HOST)
        _install_policy(monkeypatch, None)
        assert await tailnet.resolve_tailnet_host(True) == _HOST

    @pytest.mark.asyncio
    async def test_disabled_short_circuits_before_the_probe(
        self, isolated_home, monkeypatch
    ) -> None:
        """The off path must stay free: no ceiling resolution, no thread hop."""
        monkeypatch.setattr(
            tailnet,
            "is_governance_pinned_off",
            lambda **_k: (_ for _ in ()).throw(AssertionError("must not be consulted")),
        )
        assert await tailnet.resolve_tailnet_host(False) == ""


# ── The endpoint's state machine ──────────────────────────────────────────


def _status_app(host: str = "", resolved_at: int = 0) -> web.Application:
    from kiro_crew.dashboard.handlers import api_tailnet_status

    app = web.Application()
    app["tailnet_host"] = host
    app["tailnet_resolved_at"] = resolved_at
    app.router.add_get("/api/tailnet/status", api_tailnet_status)
    return app


async def _get_status(monkeypatch, *, enabled: bool, pinned: bool, host: str, resolved_at: int = 0):
    from kiro_crew.dashboard.handlers import tailnet as handler_mod

    # The handler resolves the probe through ``kiro_crew.dashboard.tailnet``, so
    # patching it there is what the route actually consults.
    monkeypatch.setattr(tailnet, "is_governance_pinned_off", lambda **_k: pinned)
    monkeypatch.setattr(
        handler_mod,
        "KiroCrewConfig",
        SimpleNamespace(
            load=lambda: SimpleNamespace(
                dashboard=SimpleNamespace(tailscale=SimpleNamespace(enabled=enabled))
            )
        ),
    )
    async with TestClient(TestServer(_status_app(host, resolved_at))) as client:
        resp = await client.get("/api/tailnet/status")
        assert resp.status == 200
        return await resp.json()


class TestStatusState:
    """All four ``state`` values, derived in the BACKEND (the frontend never re-derives)."""

    @pytest.mark.asyncio
    async def test_pinned_outranks_enabled(self, isolated_home, monkeypatch) -> None:
        body = await _get_status(monkeypatch, enabled=True, pinned=True, host=_HOST)
        assert body["state"] == "pinned"
        assert body["governance_pinned"] is True

    @pytest.mark.asyncio
    async def test_pinned_outranks_disabled_too(self, isolated_home, monkeypatch) -> None:
        """A pinned host must not be told to go flip a switch it already agrees with."""
        body = await _get_status(monkeypatch, enabled=False, pinned=True, host="")
        assert body["state"] == "pinned"

    @pytest.mark.asyncio
    async def test_off(self, isolated_home, monkeypatch) -> None:
        body = await _get_status(monkeypatch, enabled=False, pinned=False, host="")
        assert body["state"] == "off"
        assert body == {
            "enabled": False,
            "governance_pinned": False,
            "host": "",
            "origin": "",
            "resolved_at": 0,
            "state": "off",
        }

    @pytest.mark.asyncio
    async def test_unresolved(self, isolated_home, monkeypatch) -> None:
        body = await _get_status(monkeypatch, enabled=True, pinned=False, host="")
        assert body["state"] == "unresolved"
        assert body["origin"] == "" and body["resolved_at"] == 0

    @pytest.mark.asyncio
    async def test_active_carries_origin_and_timestamp(self, isolated_home, monkeypatch) -> None:
        body = await _get_status(
            monkeypatch, enabled=True, pinned=False, host=_HOST, resolved_at=1786100000
        )
        assert body["state"] == "active"
        assert body["origin"] == f"https://{_HOST}"
        assert body["resolved_at"] == 1786100000

    @pytest.mark.asyncio
    async def test_never_500s_on_an_unreadable_config(self, isolated_home, monkeypatch) -> None:
        """An unreadable config is exactly when the operator wants this card."""
        from kiro_crew.dashboard.handlers import tailnet as handler_mod

        monkeypatch.setattr(tailnet, "is_governance_pinned_off", lambda **_k: False)
        monkeypatch.setattr(
            handler_mod,
            "KiroCrewConfig",
            SimpleNamespace(load=lambda: (_ for _ in ()).throw(OSError("boom"))),
        )
        async with TestClient(TestServer(_status_app(_HOST, 5))) as client:
            resp = await client.get("/api/tailnet/status")
            assert resp.status == 200
            body = await resp.json()
        # Degrades toward "off": never claim an origin is trusted unprovably.
        assert body["enabled"] is False and body["state"] == "off"

    def test_state_precedence_is_a_pure_function(self) -> None:
        from kiro_crew.dashboard.handlers.tailnet import _derive_state

        assert _derive_state(pinned=True, enabled=True, host=_HOST) == "pinned"
        assert _derive_state(pinned=False, enabled=False, host=_HOST) == "off"
        assert _derive_state(pinned=False, enabled=True, host="") == "unresolved"
        assert _derive_state(pinned=False, enabled=True, host=_HOST) == "active"


# ── Chokepoint (b): the dashboard PATCH allowlist ─────────────────────────


def _patch_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auto_update": False}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_path)
    return cfg_path


class TestPatchChokepoint:
    @staticmethod
    def _pin(monkeypatch, pinned: bool) -> None:
        from kiro_crew.dashboard.handlers import core as core_mod

        monkeypatch.setattr(core_mod, "_tailnet_governance_pinned_off", lambda: pinned)

    @pytest.mark.asyncio
    async def test_enable_is_refused_with_403(self, tmp_config, monkeypatch) -> None:
        """Without this, a pinned host stores ``true`` behind a control that does
        nothing: ``resolve_tailnet_host`` already refuses to derive."""
        self._pin(monkeypatch, True)
        async with TestClient(TestServer(_patch_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew", json={"path": "dashboard.tailscale.enabled", "value": True}
            )
            assert resp.status == 403
            assert "administrator" in (await resp.json())["error"]
        # The refusal precedes the read-modify-write entirely.
        assert "tailscale" not in tmp_config.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_disable_is_still_allowed_under_a_pin(self, tmp_config, monkeypatch) -> None:
        """Tightest-wins: a narrower local choice composes with the ceiling, and
        refusing it would strand the user if the policy were later lifted."""
        self._pin(monkeypatch, True)
        async with TestClient(TestServer(_patch_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew", json={"path": "dashboard.tailscale.enabled", "value": False}
            )
            assert resp.status == 200
        written = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert written["dashboard"]["tailscale"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_unpinned_host_can_still_enable(self, tmp_config, monkeypatch) -> None:
        self._pin(monkeypatch, False)
        async with TestClient(TestServer(_patch_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew", json={"path": "dashboard.tailscale.enabled", "value": True}
            )
            assert resp.status == 200
        written = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert written["dashboard"]["tailscale"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_a_hand_written_tailnet_name_is_not_editable(self, tmp_config) -> None:
        """Only the boolean is reachable. A caller-supplied name would hand the
        CSRF origin allowlist an attacker-chosen value."""
        async with TestClient(TestServer(_patch_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew",
                json={"path": "dashboard.tailscale.host", "value": "evil.example"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_bool_is_rejected(self, tmp_config) -> None:
        async with TestClient(TestServer(_patch_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew", json={"path": "dashboard.tailscale.enabled", "value": "on"}
            )
            assert resp.status == 400


# ── Chokepoint (c): the CLI ───────────────────────────────────────────────


class TestCliChokepoint:
    """``kirocrew config set`` is a second write path, and ``--local`` writes the
    overlay, which takes PRECEDENCE over the base file — so leaving it ungated
    would make the generic setter the one way to store ``true`` on a pinned host."""

    @staticmethod
    def _args(value: str, local: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            config_action="set",
            key="dashboard.tailscale.enabled",
            value=value,
            local=local,
            file=None,
        )

    @staticmethod
    def _pin(monkeypatch, pinned: bool) -> None:
        # **kwargs, not a fixed-arity lambda: the enforcement sites pass
        # ``audit_tool=`` so the decision is SEL-audited.
        monkeypatch.setattr(tailnet, "is_governance_pinned_off", lambda **_k: pinned)

    @pytest.mark.parametrize("local", [False, True])
    def test_enable_is_refused_under_a_pin(self, isolated_home, monkeypatch, local) -> None:
        from kiro_crew.cli_config import _config_cmd

        self._pin(monkeypatch, True)
        with pytest.raises(SystemExit) as exc:
            _config_cmd(self._args("true", local=local))
        assert exc.value.code == 1
        for name in ("config.json", "config.local.json"):
            path = isolated_home / name
            if path.exists():
                assert "tailscale" not in path.read_text(encoding="utf-8")

    def test_disable_is_still_allowed_under_a_pin(self, isolated_home, monkeypatch) -> None:
        from kiro_crew.cli_config import _config_cmd

        self._pin(monkeypatch, True)
        _config_cmd(self._args("false"))
        data = json.loads((isolated_home / "config.json").read_text(encoding="utf-8"))
        assert data["dashboard"]["tailscale"]["enabled"] is False

    def test_unpinned_host_can_still_enable(self, isolated_home, monkeypatch) -> None:
        from kiro_crew.cli_config import _config_cmd

        self._pin(monkeypatch, False)
        _config_cmd(self._args("true"))
        data = json.loads((isolated_home / "config.json").read_text(encoding="utf-8"))
        assert data["dashboard"]["tailscale"]["enabled"] is True


# ── The stash, at BOTH startup sites ──────────────────────────────────────


class TestBothStartupSitesStash:
    """``_tailnet_host`` was a LOCAL in two startup functions.

    Stashing it in only one is exactly the bug an earlier round of this feature
    shipped, so this is asserted structurally on the source rather than by booting
    two servers: the failure mode is a MISSING statement, and a structural check
    catches it in either function without needing a live gateway.
    """

    @staticmethod
    def _func(name: str) -> ast.AsyncFunctionDef:
        from kiro_crew.dashboard import server as server_mod

        tree = ast.parse(Path(server_mod.__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} not found in dashboard/server.py")

    @staticmethod
    def _app_keys_assigned(func: ast.AsyncFunctionDef) -> set[str]:
        keys: set[str] = set()
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "app"
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    keys.add(target.slice.value)
        return keys

    @pytest.mark.parametrize("startup", ["start_dashboard", "start_api_server"])
    def test_startup_site_stashes_host_and_timestamp(self, startup) -> None:
        keys = self._app_keys_assigned(self._func(startup))
        assert "tailnet_host" in keys, f"{startup} must stash the startup-resolved host"
        assert "tailnet_resolved_at" in keys, f"{startup} must stash the resolution timestamp"

    def test_the_route_is_registered(self) -> None:
        from kiro_crew.dashboard import handlers
        from kiro_crew.dashboard import server as server_mod

        assert hasattr(handlers, "api_tailnet_status")
        src = Path(server_mod.__file__).read_text(encoding="utf-8")
        assert '"/api/tailnet/status", handlers.api_tailnet_status' in src
