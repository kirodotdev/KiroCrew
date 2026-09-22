"""The opencode picker offers what opencode advertises, not the kiro catalog.

Before the ``ACP_BACKEND_OPENCODE`` branch in ``GET /api/models``, opencode
fell through to kiro-cli's ``--list-models`` catalog -- the exact failure
``test_codex_advertised_models.py`` records for codex: the picker offered ids
the adapter never heard of, and the configured pin (``gpt-5.6-luna``) reached
the wire only to be rejected at startup.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from kiro_crew import model_registry
from kiro_crew.acp.types import ACP_BACKEND_OPENCODE
from kiro_crew.acp_backends import model_registry_namespace
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers import agents

OPENCODE_NAMESPACE = model_registry_namespace(ACP_BACKEND_OPENCODE)

ADVERTISED = ["opencode/big-pickle", "opencode/mimo-v2.5-free"]


def _request(*providers) -> MagicMock:
    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: list(providers)))
    request = MagicMock()
    request.app = {"state": state}
    return request


def _opencode_provider(ids: list[str] | None = None) -> MagicMock:
    provider = MagicMock()
    # ``_advertised_cc_models`` selects on the capability record, and a
    # MagicMock's attributes are all truthy -- so hand it the real record.
    provider.capabilities = capabilities_for(ACP_BACKEND_OPENCODE)
    provider.available_models = MagicMock(
        return_value=[{"modelId": i, "name": i, "description": ""} for i in (ids or [])]
    )
    return provider


def _names(rows: list[dict]) -> list[str]:
    return [r["model_name"] for r in rows]


def test_opencode_picker_lists_the_live_session_advertised_ids() -> None:
    rows = agents._opencode_models(_request(_opencode_provider(ADVERTISED)))

    assert _names(rows) == ["auto", *ADVERTISED]
    assert all(isinstance(r["context_window"], int) and r["context_window"] > 0 for r in rows)


def test_opencode_picker_reads_the_cross_session_cache_when_no_session_is_live() -> None:
    model_registry.refresh_advertised_models(OPENCODE_NAMESPACE, ADVERTISED)

    rows = agents._opencode_models(_request())

    assert _names(rows) == ["auto", *ADVERTISED]


def test_opencode_picker_never_reads_the_kiro_bucket() -> None:
    model_registry.refresh_advertised_models("acp", ["claude-opus-5", "gpt-5.6-sol"])

    rows = agents._opencode_models(_request())

    assert _names(rows) == ["auto"]


def test_opencode_picker_cold_offers_auto_alone() -> None:
    assert _names(agents._opencode_models(_request())) == ["auto"]


def test_opencode_picker_resurrects_the_configured_default_only_when_nothing_is_known() -> None:
    cold = agents._opencode_models(_request(), configured_default="opencode/big-pickle")
    assert _names(cold) == ["auto", "opencode/big-pickle"]
    assert cold[1]["description"] == "Configured default"

    model_registry.refresh_advertised_models(OPENCODE_NAMESPACE, ["opencode/big-pickle"])
    known = agents._opencode_models(_request(), configured_default="gpt-5.6-luna")
    # The stale kiro pin is exactly the row that kills the session: not offered.
    assert _names(known) == ["auto", "opencode/big-pickle"]
