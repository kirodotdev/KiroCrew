"""The header's harness readout, and the credit gate that rides on it.

Two surfaces, one rule: a turn on a harness that bills its own vendor account
draws nothing down on the operator's Kiro credit plan, so the credit readout must
disappear AND the billed scrape that populates it must not run. The bug this
pins: the only "hide the pill" signal used to be ``available: False``, which the
gateway sets when kiro-cli is ABSENT FROM THE HOST — a host-presence test, not a
harness test. With kiro-cli installed and a registry adapter selected, the pill
kept rendering a Kiro balance nobody was spending.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.dashboard.handlers.sessions as sessions_mod
from kiro_crew.acp.backends import bills_kiro_credits
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_KIRO_CREDITS,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.dashboard.state import _harness_status, active_acp_backend


class TestBillsKiroCredits:
    def test_kiro_and_kas_bill_the_credit_plan(self) -> None:
        assert bills_kiro_credits(ACP_BACKEND_KIRO) is True
        assert bills_kiro_credits(ACP_BACKEND_KAS) is True

    @pytest.mark.parametrize(
        "backend",
        [
            ACP_BACKEND_CLAUDE,
            ACP_BACKEND_CODEX,
            ACP_BACKEND_GOOSE,
            ACP_BACKEND_OPENCODE,
            ACP_BACKEND_PI,
        ],
    )
    def test_spec_adapters_bill_their_own_vendor_account(self, backend: str) -> None:
        assert bills_kiro_credits(backend) is False

    def test_unknown_id_answers_false_instead_of_raising(self) -> None:
        # The caller is a readout. Hiding a number is a correct degraded state;
        # asserting another account's balance is not, and neither is a 500.
        assert bills_kiro_credits("some-registry-adapter-nobody-cached") is False

    def test_membership_set_is_a_subset_of_known_backends(self) -> None:
        # H8: every ACP_BACKENDS_* set is a subset of ACP_BACKENDS_KNOWN, so a
        # typo'd id cannot silently grant itself the operator's credit plan.
        assert ACP_BACKENDS_KIRO_CREDITS <= ACP_BACKENDS_KNOWN


class TestActiveAcpBackend:
    def test_reads_the_session_store_property(self) -> None:
        assert active_acp_backend(SimpleNamespace(acp_backend=ACP_BACKEND_CODEX)) == (
            ACP_BACKEND_CODEX
        )

    def test_kiro_spelling_survives_the_round_trip(self) -> None:
        # ACP_BACKEND_KIRO is "", so a falsy-collapse anywhere on this path turns
        # the first-class harness into "unknown".
        assert active_acp_backend(SimpleNamespace(acp_backend=ACP_BACKEND_KIRO)) == ""

    def test_missing_property_is_unknown_not_kiro(self) -> None:
        assert active_acp_backend(SimpleNamespace()) is None
        assert active_acp_backend(None) is None

    def test_non_string_is_unknown(self) -> None:
        # A MagicMock session store (most of the suite) answers with a Mock, not a
        # str — that is UNKNOWN, and must not be read as the kiro spelling.
        assert active_acp_backend(MagicMock()) is None


class TestHarnessStatus:
    def test_unknown_backend_omits_the_block(self) -> None:
        assert _harness_status(None) is None

    def test_kiro_is_labelled_and_bills_credits(self) -> None:
        block = _harness_status(ACP_BACKEND_KIRO)
        assert block is not None
        assert block["backend"] == ""
        assert block["label"] == "Kiro CLI"
        assert block["kiro_credits"] is True

    def test_spec_adapter_is_labelled_and_does_not_bill_credits(self) -> None:
        block = _harness_status(ACP_BACKEND_CODEX)
        assert block is not None
        assert block["label"] == "OpenAI Codex"
        assert block["kiro_credits"] is False

    def test_uncached_adapter_names_itself_and_hides_credits(self) -> None:
        # descriptor_for raises for an id with no descriptor and no launchable
        # registry entry. The readout degrades to the raw id rather than failing
        # the whole status payload.
        block = _harness_status("unknown-adapter")
        assert block == {
            "backend": "unknown-adapter",
            "label": "unknown-adapter",
            "kiro_credits": False,
        }


class TestStatusSnapshotCarriesHarness:
    """The payload the header reads. Carried here rather than on a dedicated
    endpoint because /api/status is already fetched on mount and re-pushed every
    5s, so the readout follows a harness switch with no poll of its own."""

    def _state(self, monkeypatch, tmp_path, sessions):
        from kiro_crew.dashboard.state import DashboardState

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        crons = MagicMock()
        crons.list_jobs.return_value = []
        lessons = MagicMock()
        lessons.load_all.return_value = []
        return DashboardState(
            sessions=sessions,
            crons=crons,
            lessons=lessons,
            start_time=time.time() - 10,
        )

    def test_reports_the_configured_harness(self, monkeypatch, tmp_path) -> None:
        state = self._state(
            monkeypatch, tmp_path, SimpleNamespace(count=0, acp_backend=ACP_BACKEND_CODEX)
        )
        assert state.status_snapshot()["harness"] == {
            "backend": ACP_BACKEND_CODEX,
            "label": "OpenAI Codex",
            "kiro_credits": False,
        }

    def test_kiro_reports_credits(self, monkeypatch, tmp_path) -> None:
        state = self._state(
            monkeypatch, tmp_path, SimpleNamespace(count=0, acp_backend=ACP_BACKEND_KIRO)
        )
        harness = state.status_snapshot()["harness"]
        assert isinstance(harness, dict) and harness["kiro_credits"] is True

    def test_unreadable_store_reports_null_not_kiro(self, monkeypatch, tmp_path) -> None:
        # An older store, or a double: the key is present and null so the browser
        # can tell UNKNOWN from a harness, and keeps its prior rendering.
        state = self._state(monkeypatch, tmp_path, MagicMock(count=0))
        snap = state.status_snapshot()
        assert "harness" in snap
        assert snap["harness"] is None


class TestUsageEndpointGate:
    """/api/sessions/usage must answer "unavailable" WITHOUT spawning the scrape
    when the harness bills elsewhere. The scrape is a billed
    `kiro-cli chat --no-interactive ... /usage` turn on a 30s timer, so this gate
    is real money, not just a hidden pill."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        cache, ts = sessions_mod._usage_cache, sessions_mod._usage_cache_ts
        yield
        sessions_mod._usage_cache, sessions_mod._usage_cache_ts = cache, ts

    def _request(self, backend: str | None):
        sessions = SimpleNamespace() if backend is None else SimpleNamespace(acp_backend=backend)
        request = MagicMock()
        request.app = {"state": SimpleNamespace(_background_tasks=set(), sessions=sessions)}
        return request

    async def _call(self, backend: str | None):
        # Stale cache holding a real Kiro reading: the gate has to beat the timer,
        # not merely coexist with it.
        sessions_mod._usage_cache = {"credits_used": 117.0, "credits_plan": 10.0}
        sessions_mod._usage_cache_ts = time.time() - (sessions_mod._USAGE_REFRESH_SECS + 1)
        with (
            patch.object(sessions_mod, "reject_if_kiro_unverified", AsyncMock(return_value=None)),
            patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock()) as fetch,
        ):
            resp = await sessions_mod.api_sessions_usage(self._request(backend))
        return resp, fetch

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
    async def test_spec_adapter_reports_unavailable_and_spends_nothing(self, backend: str) -> None:
        resp, fetch = await self._call(backend)
        assert resp.status == 200
        assert b'"available": false' in resp.body
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate_leaves_the_cache_intact(self) -> None:
        # Switching back to Kiro must still serve the last good value while the
        # first refresh runs, so the gate reports without mutating the cache.
        await self._call(ACP_BACKEND_CODEX)
        assert sessions_mod._usage_cache["credits_used"] == 117.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    async def test_credit_billing_harness_still_refreshes(self, backend: str) -> None:
        resp, fetch = await self._call(backend)
        assert resp.status == 200
        assert b'"credits_used": 117.0' in resp.body
        fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_unreadable_backend_keeps_existing_behaviour(self) -> None:
        # UNKNOWN must not hide a balance the operator is really spending.
        resp, fetch = await self._call(None)
        assert b'"credits_used": 117.0' in resp.body
        fetch.assert_called_once()
