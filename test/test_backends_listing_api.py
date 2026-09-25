"""Unit tests for ``GET /api/backends`` (the per-chat backend picker's listing).

Exercises the three arrays the composer and the Settings panel read: the
selectable rows (id + label + which is the global default), the invalid operator
descriptors (id -> reasons), and the unroutable ones (id -> reason). The
operator-descriptor arrays are driven through the real boot-load against a temp
``harnesses.json`` with a stub executable on PATH, exactly as
``test_operator_harness_bootstrap`` does, so the endpoint is tested over the same
registry state a real gateway would build.

Every test restores the process-global registry state in a fixture (registered
ids, the operator register, the selectable pair, the diagnostic maps) so one
test's boot-load cannot leak into another.
"""

from __future__ import annotations

import json
import stat
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp import harness as harness_pkg
from kiro_crew.acp.harness import operator_registry as reg
from kiro_crew.agent_sdk import backends as b
from kiro_crew.dashboard.handlers.backends_listing import api_backends


@pytest.fixture(autouse=True)
def _host_provenance_accepted(monkeypatch):
    """Stub executables live under the test's temp tree, whose ancestors are the
    host's business (a CI runner's home, a developer's /tmp), not this suite's.
    The provenance rule itself -- outside the agent-writable trees, owned by the
    gateway user or root, unwritable by others -- is ``github_runner``'s and is
    tested there; here it is accepted so the routing logic under test is what
    decides. The provenance tests in ``test_routing_verification`` re-patch it.
    """
    from kiro_crew import github_runner

    def _accept_relaxed(candidate, *, require_protected=False):
        # Provenance requires the strict form (a root-owned, gateway-unwritable
        # install) of every harness executable and of a launcher's interpreter; a
        # temp-tree stub cannot be one on this host, so the rule is answered
        # "accepted" for the stubs and the routing logic under test decides. The
        # provenance tests re-patch it to refuse.
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _accept_relaxed)


@pytest.fixture
def clean_boot(tmp_path, monkeypatch):
    """Snapshot/restore every registry surface the boot-load writes.

    Pins ``KIROCREW_HOME`` so the routing-attestation store the selectability
    gate reads is this test's own file.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    baseline = set(b._baseline)
    selectable = set(b._selectable)
    yield
    b._reset_registered_backends()
    harness_pkg._reset_operator_register()
    reg._reset_operator_diagnostics()
    b._baseline.clear()
    b._baseline.update(baseline)
    b._selectable.clear()
    b._selectable.update(selectable)


def _stub_executable(tmp_path, name="my-acp"):
    """A stub that RESOLVES on every platform (it is never run): POSIX wants the
    execute bit; Windows has no execute bit and accepts a known runnable suffix,
    so the file takes ``.cmd`` there."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / (f"{name}.cmd" if sys.platform == "win32" else name)
    exe.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def _write_harnesses(tmp_path, mapping, *, attest: bool = True) -> str:
    """Write the descriptor file; by default also record a routing attestation
    for every valid routed entry, as a successful Verify would have (the
    selectability gate needs the gateway's own evidence, not the declaration)."""
    path = tmp_path / "harnesses.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    if attest:
        from kiro_crew.acp.harness.descriptor import descriptor_from_mapping
        from kiro_crew.acp.harness.routing_verification import record_attestation

        for harness_id, raw in mapping.items():
            d, _ = descriptor_from_mapping(raw, harness_id=harness_id)
            if d is not None and d.selectable:
                record_attestation(d, mechanism=d.routing, evidence={"fixture": True})
    return str(path)


def _make_app(*, owner: bool = True) -> web.Application:
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers.backends_listing import api_backend_verify

    app = web.Application()
    # The verify handler's owner predicate (kiro_prerequisite._is_dashboard_owner)
    # reads ``state.owner_id`` and the request's signed identity; a non-owner test
    # presents another caller.
    app["state"] = SimpleNamespace(owner_id="owner", push_slots_update=lambda: None)

    @web.middleware
    async def _identity(request: web.Request, handler):
        request["user"] = "owner" if owner else "someone-else"
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_identity)
    app.router.add_get("/api/backends", api_backends)
    app.router.add_post("/api/backends/{id}/verify", api_backend_verify)
    return app


class TestBackendsEndpointShape:
    """The payload shape every reader depends on."""

    @pytest.mark.asyncio
    async def test_returns_200_with_three_arrays(self) -> None:
        """GET /api/backends is 200 JSON carrying backends/invalid/unroutable arrays."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/backends")
            body = await resp.text()
            assert resp.status == 200, f"expected 200, got {resp.status}: {body}"
            assert "application/json" in resp.headers.get("Content-Type", "")
            data = await resp.json()
            for key in ("backends", "invalid", "unroutable"):
                assert isinstance(data.get(key), list), f"{key} must be a list"

    @pytest.mark.asyncio
    async def test_every_selectable_row_has_id_label_and_default_flag(self) -> None:
        """Each selectable row carries id, a non-empty label, and a bool default flag."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/backends")
            data = await resp.json()
            assert data["backends"], "the baseline build has at least kiro-cli selectable"
            for row in data["backends"]:
                assert set(row) == {"id", "label", "is_global_default"}
                assert isinstance(row["id"], str)
                assert row["label"], f"row {row['id']!r} must render a non-empty label"
                assert isinstance(row["is_global_default"], bool)

    @pytest.mark.asyncio
    async def test_exactly_one_global_default_and_it_is_selectable(self) -> None:
        """The default resolves through the selectability gate, so it is always a row."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/backends")
            data = await resp.json()
            defaults = [r for r in data["backends"] if r["is_global_default"]]
            assert len(defaults) == 1, f"expected one default, got {defaults}"

    @pytest.mark.asyncio
    async def test_rows_match_the_single_selectability_owner(self) -> None:
        """The listing's ids are exactly ``selectable_backend_values`` (H4: one gate)."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/backends")
            data = await resp.json()
            ids = [r["id"] for r in data["backends"]]
            assert ids == b.selectable_backend_values()


class TestBackendsEndpointDiagnostics:
    """The invalid / unroutable arrays, driven through the real boot-load."""

    @pytest.mark.asyncio
    async def test_valid_routed_descriptor_appears_as_a_selectable_row(
        self, clean_boot, tmp_path
    ) -> None:
        """A routed operator descriptor shows up in ``backends`` with its display name."""
        exe = _stub_executable(tmp_path)
        path = _write_harnesses(
            tmp_path,
            {
                "my-acp": {
                    "id": "my-acp",
                    "display_name": "My ACP",
                    "executable": str(exe),
                    "argv": ["{executable}", "serve"],
                    "agent_args": ["--agent", "{agent}"],
                    "routing": "agent_spec",
                }
            },
        )
        reg.load_and_register_operator_descriptors(path=path)
        async with TestClient(TestServer(_make_app())) as client:
            data = await (await client.get("/api/backends")).json()
        row = next((r for r in data["backends"] if r["id"] == "my-acp"), None)
        assert row is not None, "the routed descriptor must be offered"
        assert row["label"] == "My ACP"
        assert row["is_global_default"] is False
        # Not misfiled into either diagnostic array.
        assert all(r["id"] != "my-acp" for r in data["invalid"])
        assert all(r["id"] != "my-acp" for r in data["unroutable"])

    @pytest.mark.asyncio
    async def test_unroutable_descriptor_appears_in_unroutable_with_reason(
        self, clean_boot, tmp_path
    ) -> None:
        """A descriptor with no recognized routing is unroutable, not selectable."""
        exe = _stub_executable(tmp_path, name="no-route")
        path = _write_harnesses(
            tmp_path,
            {
                "no-route": {
                    "id": "no-route",
                    "display_name": "No Route",
                    "executable": str(exe),
                    "argv": ["{executable}"],
                    # routing omitted -> valid but unselectable
                }
            },
        )
        reg.load_and_register_operator_descriptors(path=path)
        async with TestClient(TestServer(_make_app())) as client:
            data = await (await client.get("/api/backends")).json()
        row = next((r for r in data["unroutable"] if r["id"] == "no-route"), None)
        assert row is not None, "the unroutable descriptor must be listed with a reason"
        assert row["reason"], "an unroutable row must carry a reason"
        assert row["label"] == "No Route"
        assert all(r["id"] != "no-route" for r in data["backends"])

    @pytest.mark.asyncio
    async def test_malformed_descriptor_appears_in_invalid_with_reasons(
        self, clean_boot, tmp_path
    ) -> None:
        """A malformed entry lands in ``invalid`` (reasons list) and nowhere else."""
        exe = _stub_executable(tmp_path)
        path = _write_harnesses(
            tmp_path,
            {
                "bad-one": {
                    # No executable/argv -> fails validation.
                    "id": "bad-one",
                    "agent_args": ["--agent", "{agent}"],
                    "routing": "agent_spec",
                },
                "good-one": {
                    "id": "good-one",
                    "executable": str(exe),
                    "argv": ["{executable}"],
                    "agent_args": ["--agent", "{agent}"],
                    "routing": "agent_spec",
                },
            },
        )
        reg.load_and_register_operator_descriptors(path=path)
        async with TestClient(TestServer(_make_app())) as client:
            data = await (await client.get("/api/backends")).json()
        # Listed by file position, never by its key: the key of an invalid entry is
        # operator text this authenticated-but-not-owner-gated listing must not echo.
        bad = next((r for r in data["invalid"] if r["id"] == "#1"), None)
        assert bad is not None, "the malformed descriptor must be recorded invalid"
        assert isinstance(bad["reasons"], list) and bad["reasons"], "invalid rows carry reasons"
        assert all(r["id"] != "bad-one" for r in data["invalid"])
        assert not any("bad-one" in reason for reason in bad["reasons"]), bad
        # The bad one costs only its own row: the sibling still becomes selectable.
        assert any(r["id"] == "good-one" for r in data["backends"])
        assert all(r["id"] != "bad-one" for r in data["backends"])


class TestBackendVerifyEndpoint:
    """``POST /api/backends/{id}/verify``: the operator's one path to selectability."""

    @staticmethod
    def _routed(tmp_path):
        return {
            "my-acp": {
                "id": "my-acp",
                "display_name": "My ACP",
                "executable": str(_stub_executable(tmp_path)),
                "argv": ["{executable}", "serve"],
                "agent_args": ["--agent", "{agent}"],
                "routing": "agent_spec",
            }
        }

    @pytest.mark.asyncio
    async def test_an_unverified_descriptor_is_listed_unroutable_and_verifiable(
        self, clean_boot, tmp_path
    ) -> None:
        from kiro_crew.acp.harness.routing_verification import UNVERIFIED_REASON

        reg.load_and_register_operator_descriptors(
            path=_write_harnesses(tmp_path, self._routed(tmp_path), attest=False)
        )
        async with TestClient(TestServer(_make_app())) as client:
            data = await (await client.get("/api/backends")).json()
        assert all(r["id"] != "my-acp" for r in data["backends"])
        row = next(r for r in data["unroutable"] if r["id"] == "my-acp")
        assert row["reason"] == UNVERIFIED_REASON
        assert row["verifiable"] is True
        # A descriptor with no routing at all is unroutable but NOT verifiable.
        reg._reset_operator_diagnostics()
        b._reset_registered_backends()
        harness_pkg._reset_operator_register()
        reg.load_and_register_operator_descriptors(
            path=_write_harnesses(
                tmp_path, {"no-route": {"executable": "/opt/x", "argv": ["{executable}"]}}
            )
        )
        async with TestClient(TestServer(_make_app())) as client:
            data = await (await client.get("/api/backends")).json()
        assert next(r for r in data["unroutable"] if r["id"] == "no-route")["verifiable"] is False

    @pytest.mark.asyncio
    async def test_verify_is_owner_gated_and_audited(self, clean_boot, tmp_path, monkeypatch):
        reg.load_and_register_operator_descriptors(
            path=_write_harnesses(tmp_path, self._routed(tmp_path), attest=False)
        )
        async with TestClient(TestServer(_make_app(owner=False))) as client:
            resp = await client.post("/api/backends/my-acp/verify")
            assert resp.status == 403
            assert (await resp.json())["code"] == "dashboard_owner_required"

    @pytest.mark.asyncio
    async def test_verify_refuses_an_id_with_no_pending_claim(self, clean_boot, tmp_path):
        reg.load_and_register_operator_descriptors(
            path=_write_harnesses(
                tmp_path, self._routed(tmp_path)
            )  # attested -> already selectable
        )
        async with TestClient(TestServer(_make_app())) as client:
            for bad in ("claude", "nope", "my-acp"):
                resp = await client.post(f"/api/backends/{bad}/verify")
                assert resp.status == 404, bad
                assert (await resp.json())["code"] == "unknown_operator_backend"

    @pytest.mark.asyncio
    async def test_a_verified_probe_makes_the_row_selectable_without_a_restart(
        self, clean_boot, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.acp.harness import routing_verification as rv
        from kiro_crew.dashboard.handlers import backends_listing as bl

        reg.load_and_register_operator_descriptors(
            path=_write_harnesses(tmp_path, self._routed(tmp_path), attest=False)
        )

        async def fake_verify(descriptor, build_provider, **kw):
            assert descriptor.id == "my-acp"
            return rv.RoutingVerification(
                rv.VERDICT_VERIFIED, "asked once", permission_requests=1, elapsed_secs=1.0
            )

        monkeypatch.setattr(rv, "verify_routing", fake_verify)
        # The handler builds the production factory from config; keep the test off
        # the real loader.
        monkeypatch.setattr(bl, "_snapshot", bl._snapshot)

        from kiro_crew import config as config_pkg

        class _Cfg:
            agent = type("A", (), {"acp_backend": ""})()

        monkeypatch.setattr(config_pkg.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/backends/my-acp/verify")
            body = await resp.json()
            assert resp.status == 200, body
            assert body["verdict"] == "verified" and body["verified"] is True
            assert body["selectable"] is True
            data = await (await client.get("/api/backends")).json()
        assert any(r["id"] == "my-acp" for r in data["backends"])
        assert all(r["id"] != "my-acp" for r in data["unroutable"])
        assert rv.is_attested(reg.registered_operator_descriptor("my-acp")) is True
        # The record names the agent the probe ran under (no default agent here:
        # the no-agent spawn) and nothing else.
        assert rv.load_attestations()["my-acp"]["agents"] == [""]
        assert body["agent"] == ""
        # A second verify with no agent named has nothing pending to settle...
        async with TestClient(TestServer(_make_app())) as client:
            assert (await client.post("/api/backends/my-acp/verify")).status == 404
            # ...but a FURTHER agent can be verified on the selectable backend: the
            # attestation is per agent, so this is a new claim, and it ADDS to the
            # record rather than replacing the first agent.
            resp = await client.post("/api/backends/my-acp/verify", json={"agent": "reviewer"})
            body = await resp.json()
            assert resp.status == 200, body
            assert body["agent"] == "reviewer" and body["verified"] is True
            assert rv.load_attestations()["my-acp"]["agents"] == ["", "reviewer"]
            # An agent that is not a name is refused before anything runs.
            resp = await client.post("/api/backends/my-acp/verify", json={"agent": "../x"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_agent"
            assert rv.load_attestations()["my-acp"]["agents"] == ["", "reviewer"]

    @pytest.mark.asyncio
    async def test_the_probe_runs_under_the_named_agent(self, clean_boot, tmp_path, monkeypatch):
        """The agent in the body is the agent the probe's provider is built with,
        and the one the record is bound to -- not the configured default."""
        from kiro_crew.acp.harness import routing_verification as rv

        reg.load_and_register_operator_descriptors(
            path=_write_harnesses(tmp_path, self._routed(tmp_path), attest=False)
        )
        seen: list[str | None] = []

        class _Provider:
            def __init__(self, **kw):
                seen.append(kw.get("agent"))
                assert kw.get("acp_backend") == "my-acp"

        from kiro_crew.providers import acp as acp_mod

        monkeypatch.setattr(acp_mod, "AcpProvider", _Provider)

        async def fake_verify(descriptor, build_provider, **kw):
            build_provider("probe-session", str(tmp_path))
            return rv.RoutingVerification(
                rv.VERDICT_VERIFIED, "asked once", permission_requests=1, elapsed_secs=1.0
            )

        monkeypatch.setattr(rv, "verify_routing", fake_verify)
        from kiro_crew import config as config_pkg

        class _Cfg:
            agent = type("A", (), {"acp_backend": "", "default_agent": "dflt"})()

        monkeypatch.setattr(config_pkg.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/backends/my-acp/verify", json={"agent": "reviewer"})
            assert resp.status == 200, await resp.text()
        assert seen == ["reviewer"]
        assert rv.load_attestations()["my-acp"]["agents"] == ["reviewer"]

    @pytest.mark.asyncio
    async def test_an_inconclusive_probe_records_nothing(self, clean_boot, tmp_path, monkeypatch):
        from kiro_crew.acp.harness import routing_verification as rv

        reg.load_and_register_operator_descriptors(
            path=_write_harnesses(tmp_path, self._routed(tmp_path), attest=False)
        )

        async def fake_verify(descriptor, build_provider, **kw):
            return rv.RoutingVerification(rv.VERDICT_INCONCLUSIVE, "nothing attempted")

        monkeypatch.setattr(rv, "verify_routing", fake_verify)

        from kiro_crew import config as config_pkg

        class _Cfg:
            agent = type("A", (), {"acp_backend": ""})()

        monkeypatch.setattr(config_pkg.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/backends/my-acp/verify")
            body = await resp.json()
        assert resp.status == 200
        assert body["verdict"] == "inconclusive" and body["selectable"] is False
        assert "my-acp" not in b.selectable_backends()
        assert rv.load_attestations() == {}
