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

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp import harness as harness_pkg
from kiro_crew.acp.harness import operator_registry as reg
from kiro_crew.agent_sdk import backends as b
from kiro_crew.dashboard.handlers.backends_listing import api_backends


@pytest.fixture
def clean_boot():
    """Snapshot/restore every registry surface the boot-load writes."""
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
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / name
    exe.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def _write_harnesses(tmp_path, mapping) -> str:
    path = tmp_path / "harnesses.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return str(path)


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/backends", api_backends)
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
                    "routing": "agent_spec",
                },
                "good-one": {
                    "id": "good-one",
                    "executable": str(exe),
                    "argv": ["{executable}"],
                    "routing": "agent_spec",
                },
            },
        )
        reg.load_and_register_operator_descriptors(path=path)
        async with TestClient(TestServer(_make_app())) as client:
            data = await (await client.get("/api/backends")).json()
        bad = next((r for r in data["invalid"] if r["id"] == "bad-one"), None)
        assert bad is not None, "the malformed descriptor must be recorded invalid"
        assert isinstance(bad["reasons"], list) and bad["reasons"], "invalid rows carry reasons"
        # The bad one costs only its own row: the sibling still becomes selectable.
        assert any(r["id"] == "good-one" for r in data["backends"])
        assert all(r["id"] != "bad-one" for r in data["backends"])
