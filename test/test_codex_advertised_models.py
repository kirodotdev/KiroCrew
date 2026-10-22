"""The codex backend offers, and is held to, the model list codex-acp advertises.

Three seams, one failure they closed together. codex-acp advertises its models
only as a ``configOptions`` ``model`` select on ``session/new``, and that list is
the ONLY vocabulary ``session/set_config_option("model", ...)`` accepts. Before
these changes:

* the capture skipped the select (codex was not an
  ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` member), so no codex session ever
  learned what it could run;
* ``GET /api/models`` fell through to kiro-cli's ``--list-models`` catalog, so the
  picker offered kiro ids (``gpt-5.6-sol``, ``claude-opus-5``) codex has never
  heard of;
* a pick from that catalog reached the wire at startup, codex answered a bare
  ``{"code": -32602, "message": "Invalid params"}``, and the push helper -- which
  only knew claude-agent-acp's worded rejection -- re-raised it as a protocol
  failure. Session init died, three times, and the user saw
  ``JSON-RPC error: {'code': -32602, 'message': 'Invalid params'}``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.client import (
    DEFAULT_MODEL,
    AcpClient,
    AcpError,
    AcpModelUnavailable,
    _jsonrpc_error_code,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers import agents

#: What codex-acp puts on the wire at ``session/new``: no ``models`` object, the
#: list lives in the ``model`` select. Values are the ids the adapter accepts back.
CODEX_SESSION_NEW = {
    "sessionId": "codex-sess-1",
    "modes": {"currentModeId": "read-only", "availableModes": []},
    "configOptions": [
        {
            "id": "mode",
            "type": "select",
            "currentValue": "read-only",
            "options": [{"value": "read-only", "name": "Read only"}],
        },
        {
            "id": "model",
            "type": "select",
            "currentValue": "gpt-5.4",
            "options": [
                {"value": "gpt-5.4", "name": "gpt-5.4", "description": "Flagship"},
                {"value": "gpt-5.4-codex", "name": "gpt-5.4-codex", "description": ""},
                {"value": "gpt-5.5", "name": "gpt-5.5"},
            ],
        },
        {
            "id": "reasoning_effort",
            "type": "select",
            "currentValue": "medium",
            "options": [{"value": "medium", "name": "Medium"}],
        },
    ],
}


@pytest.fixture(autouse=True)
def _cold_advertised_cache(monkeypatch):
    """Every test here starts from an empty cross-session cache.

    ``model_registry._ADVERTISED_MODELS`` is a module global other tests on the
    same xdist worker feed; these tests assert on which BUCKET gets fed, so a
    warm one would pass or fail on a neighbour's leftovers.
    """
    monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    monkeypatch.setattr(model_registry, "_ADVERTISED_META", {})
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


# The genuine persist, captured before the autouse fixture stubs the name, for
# the one test that exercises a real disk write.
_REAL_PERSIST = model_registry.persist_advertised_models


def _codex_client(tmp_path, model: str = "") -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    client._session_id = "codex-sess-1"
    client._model = model
    return client


# ── Seam 1: the capability sets ──


def test_codex_is_an_advertised_model_selection_member() -> None:
    assert ACP_BACKEND_CODEX in sdk_backends.ACP_BACKENDS_ADVERTISED_MODEL_SELECTION
    assert capabilities_for(ACP_BACKEND_CODEX).resolves_model_from_advertised_list is True


def test_codex_has_its_own_registry_namespace() -> None:
    """The namespace is also the advertised-cache bucket: codex's ids must not
    land in the ``acp`` bucket a kiro-family harness would read back."""
    assert sdk_backends.model_registry_namespace(ACP_BACKEND_CODEX) == "codex"
    assert capabilities_for(ACP_BACKEND_CODEX).model_id_namespace == "codex"
    # The kiro-family answer is untouched (harness-parity H13).
    assert sdk_backends.model_registry_namespace(ACP_BACKEND_KIRO) == "acp"
    assert sdk_backends.model_registry_namespace("kas") == "acp"


def test_codex_membership_does_not_drag_in_the_settings_seed() -> None:
    """The two opt-ins are independent: codex writes no settings.local.json."""
    assert ACP_BACKEND_CODEX not in sdk_backends.ACP_BACKENDS_SEED_LOCAL_SETTINGS


# ── Seam 2: the capture ──


def test_codex_capture_harvests_the_config_options_model_select(tmp_path) -> None:
    client = _codex_client(tmp_path)

    client._capture_available_models(CODEX_SESSION_NEW)

    assert [m["modelId"] for m in client.available_models()] == [
        "gpt-5.4",
        "gpt-5.4-codex",
        "gpt-5.5",
    ]
    assert client._resolved_model_id == "gpt-5.4"


def test_codex_capture_feeds_the_codex_cache_bucket_only(tmp_path) -> None:
    client = _codex_client(tmp_path)

    client._capture_available_models(CODEX_SESSION_NEW)

    assert model_registry.advertised_models("codex") == ["gpt-5.4", "gpt-5.4-codex", "gpt-5.5"]
    assert model_registry.advertised_models("acp") == []
    assert client._advertised_models_changed is True


def test_kiro_capture_still_ignores_a_config_options_select(tmp_path) -> None:
    """The kiro path did not move (harness-parity H13)."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_KIRO)

    client._capture_available_models(CODEX_SESSION_NEW)

    assert client.available_models() == []
    assert model_registry.advertised_models("acp") == []


# ── Seam 3: the -32602 value rejection ──


class TestJsonRpcErrorCode:
    def test_reads_an_int_code(self) -> None:
        assert _jsonrpc_error_code({"code": -32602, "message": "Invalid params"}) == -32602

    @pytest.mark.parametrize(
        "frame",
        [None, "Invalid params", {"message": "no code"}, {"code": "-32602"}, {"code": True}],
    )
    def test_every_other_shape_is_none(self, frame) -> None:
        assert _jsonrpc_error_code(frame) is None


@pytest.mark.asyncio
async def test_wait_for_response_carries_the_error_code(tmp_path) -> None:
    """The frame's code rides on the exception, not only in the redacted text."""
    client = AcpClient(work_dir=tmp_path)
    client._read_message = AsyncMock(
        return_value=JsonRpcMessage(id=7, error={"code": -32602, "message": "Invalid params"})
    )

    with pytest.raises(AcpError) as info:
        await client._wait_for_response(7, timeout=5.0)

    assert info.value.code == -32602
    assert "JSON-RPC error" in str(info.value)


def test_acp_error_code_defaults_to_none() -> None:
    assert AcpError("plain").code is None
    assert AcpError("plain", transient=True).code is None


def _refusing_with(code: int | None, message: str):
    """A ``set_config_option`` double that refuses every value the codex way."""
    applied: list[tuple[str, str]] = []

    async def _set_config_option(config_id: str, value: str) -> None:
        applied.append((config_id, value))
        raise AcpError(message, code=code)

    return _set_config_option, applied


class TestStartupModelPushOnCodex:
    @pytest.mark.asyncio
    async def test_bare_invalid_params_withholds_and_keeps_the_session(self, tmp_path) -> None:
        """The reported failure: a stale kiro id pushed to codex must not kill init."""
        client = _codex_client(tmp_path, model="gpt-5.6-sol")
        client._resolved_model_id = "gpt-5.4"
        set_option, applied = _refusing_with(
            -32602, "JSON-RPC error: {'code': -32602, 'message': 'Invalid params'}"
        )
        client.set_config_option = set_option  # type: ignore[method-assign]

        await client._apply_startup_model()

        assert applied == [("model", "gpt-5.6-sol")]
        # Recorded as inheriting, so the warm-pool re-apply never re-offers it.
        assert client._model == DEFAULT_MODEL
        assert client._resolved_model_id == "gpt-5.4"

    @pytest.mark.asyncio
    async def test_bare_invalid_params_on_an_explicit_pick_is_typed(self, tmp_path) -> None:
        """``set_model`` is the user's own pick: refusal is ``AcpModelUnavailable``,
        never a generic error the caller would answer with a session reset."""
        client = _codex_client(tmp_path, model="gpt-5.4")
        client._capture_available_models(CODEX_SESSION_NEW)
        set_option, _applied = _refusing_with(-32602, "JSON-RPC error: Invalid params")
        client.set_config_option = set_option  # type: ignore[method-assign]

        with pytest.raises(AcpModelUnavailable) as info:
            await client.set_model("claude-opus-5")

        assert info.value.advertised == ["gpt-5.4", "gpt-5.4-codex", "gpt-5.5"]
        assert client._model == "gpt-5.4"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [-32601, -32603, -32000, None])
    async def test_any_other_code_is_still_a_protocol_failure(self, tmp_path, code) -> None:
        """Only Invalid params is a value verdict; everything else propagates."""
        client = _codex_client(tmp_path, model="gpt-5.4")
        set_option, _applied = _refusing_with(code, "JSON-RPC error: something else")
        client.set_config_option = set_option  # type: ignore[method-assign]

        with pytest.raises(AcpError, match="something else"):
            await client._apply_startup_model()

    @pytest.mark.asyncio
    async def test_the_worded_claude_rejection_still_counts(self, tmp_path) -> None:
        """claude-agent-acp's message-shaped rejection is not displaced by the code."""
        client = _codex_client(tmp_path, model="gpt-5.4")
        set_option, applied = _refusing_with(None, "Invalid value for config option model: x")
        client.set_config_option = set_option  # type: ignore[method-assign]

        await client._apply_startup_model()

        assert applied == [("model", "gpt-5.4")]
        assert client._model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_an_accepted_value_is_recorded(self, tmp_path) -> None:
        client = _codex_client(tmp_path, model="gpt-5.4-codex")
        applied: list[tuple[str, str]] = []

        async def _accept(config_id: str, value: str) -> None:
            applied.append((config_id, value))

        client.set_config_option = _accept  # type: ignore[method-assign]

        await client._apply_startup_model()

        assert applied == [("model", "gpt-5.4-codex")]
        assert client._model == "gpt-5.4-codex"


# ── The picker: GET /api/models on codex ──


def _request(*providers) -> MagicMock:
    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: list(providers)))
    request = MagicMock()
    request.app = {"state": state}
    return request


def _codex_provider(tmp_path) -> MagicMock:
    """A provider double wrapping a client that captured a real codex session/new."""
    client = _codex_client(tmp_path)
    client._capture_available_models(CODEX_SESSION_NEW)
    provider = MagicMock()
    # ``_advertised_cc_models`` selects on the capability record, and a
    # MagicMock's attributes are all truthy -- so hand it the real record.
    provider.capabilities = capabilities_for(ACP_BACKEND_CODEX)
    provider.available_models = MagicMock(return_value=client.available_models())
    return provider


def _names(rows: list[dict]) -> list[str]:
    return [r["model_name"] for r in rows]


def test_codex_picker_lists_the_live_session_advertised_ids(tmp_path) -> None:
    rows = agents._advertised_backend_models(_request(_codex_provider(tmp_path)), ACP_BACKEND_CODEX)

    assert _names(rows) == ["auto", "gpt-5.4", "gpt-5.4-codex", "gpt-5.5"]
    assert rows[1]["display_name"] == "gpt-5.4"
    assert rows[1]["description"] == "Flagship"
    assert all(isinstance(r["context_window"], int) and r["context_window"] > 0 for r in rows)


def test_codex_picker_reads_the_cross_session_cache_when_no_session_is_live() -> None:
    """A dashboard restarted after a codex session still offers the real list."""
    model_registry.refresh_advertised_models("codex", ["gpt-5.4", "gpt-5.5"])

    rows = agents._advertised_backend_models(_request(), ACP_BACKEND_CODEX)

    assert _names(rows) == ["auto", "gpt-5.4", "gpt-5.5"]


def test_codex_picker_never_reads_the_kiro_bucket() -> None:
    model_registry.refresh_advertised_models("acp", ["claude-opus-5", "gpt-5.6-sol"])

    rows = agents._advertised_backend_models(_request(), ACP_BACKEND_CODEX)

    assert _names(rows) == ["auto"]


def test_codex_picker_cold_offers_auto_alone() -> None:
    """No session yet and nothing cached: ``auto`` (inherit codex's default) only.
    The frontend refetches on the first session spawn."""
    assert _names(agents._advertised_backend_models(_request(), ACP_BACKEND_CODEX)) == ["auto"]


def test_codex_picker_resurrects_the_configured_default_only_when_nothing_is_known() -> None:
    cold = agents._advertised_backend_models(
        _request(), ACP_BACKEND_CODEX, configured_default="gpt-5.4-codex"
    )
    assert _names(cold) == ["auto", "gpt-5.4-codex"]
    assert cold[1]["description"] == "Configured default"

    model_registry.refresh_advertised_models("codex", ["gpt-5.4"])
    known = agents._advertised_backend_models(
        _request(), ACP_BACKEND_CODEX, configured_default="gpt-5.6-sol"
    )
    # The stale kiro pin is exactly the row that kills the session: not offered.
    assert _names(known) == ["auto", "gpt-5.4"]


def test_codex_picker_does_not_duplicate_auto_or_a_configured_advertised_id() -> None:
    model_registry.refresh_advertised_models("codex", ["auto", "gpt-5.4", "gpt-5.4"])

    rows = agents._advertised_backend_models(
        _request(), ACP_BACKEND_CODEX, configured_default="gpt-5.4"
    )

    assert _names(rows) == ["auto", "gpt-5.4"]


def test_codex_picker_ignores_a_provider_without_the_capability(tmp_path) -> None:
    """A kiro session's list is a different vocabulary and must not be offered."""
    kiro = MagicMock()
    kiro.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    kiro.available_models = MagicMock(return_value=[{"modelId": "claude-opus-5"}])

    assert _names(agents._advertised_backend_models(_request(kiro), ACP_BACKEND_CODEX)) == ["auto"]


def test_codex_picker_ignores_a_claude_session_that_holds_the_same_capability() -> None:
    """A retained claude session is the near miss the capability gate cannot catch.

    claude also resolves its model from its advertised list, so the capability is
    true of both harnesses. Only ``model_id_namespace`` says whose ids these are.
    Offering claude's list on codex would put back exactly the rows that kill the
    session, since codex refuses every id it did not advertise.
    """
    claude = MagicMock()
    claude.capabilities = capabilities_for(ACP_BACKEND_CLAUDE)
    claude.available_models = MagicMock(
        return_value=[{"modelId": "claude-opus-5", "name": "Opus 5", "description": ""}]
    )

    assert _names(agents._advertised_backend_models(_request(claude), ACP_BACKEND_CODEX)) == [
        "auto"
    ]


def test_codex_picker_prefers_the_newest_codex_session(tmp_path) -> None:
    """Two live codex sessions: the later one's snapshot wins.

    ``active_providers()`` walks live sessions in creation order, and a session
    started before a plan change still holds the list captured at its own
    ``session/new``. Reading the oldest would keep offering the stale set.
    """
    older = MagicMock()
    older.capabilities = capabilities_for(ACP_BACKEND_CODEX)
    older.available_models = MagicMock(
        return_value=[{"modelId": "gpt-5.3", "name": "gpt-5.3", "description": ""}]
    )

    rows = agents._advertised_backend_models(
        _request(older, _codex_provider(tmp_path)), ACP_BACKEND_CODEX
    )

    assert _names(rows) == ["auto", "gpt-5.4", "gpt-5.4-codex", "gpt-5.5"]


@pytest.mark.asyncio
async def test_api_models_routes_the_codex_backend_to_the_advertised_list(monkeypatch, tmp_path):
    """The handler branch: codex never reaches the kiro-cli ``--list-models`` spawn."""
    import json

    monkeypatch.setattr(
        agents.KiroCrewConfig,
        "load",
        staticmethod(
            lambda: SimpleNamespace(agent=SimpleNamespace(acp_backend=ACP_BACKEND_CODEX, model=""))
        ),
    )

    async def _never_spawn(*_a, **_k):  # pragma: no cover - the assertion is that it is unreached
        raise AssertionError("codex must not spawn kiro-cli --list-models")

    monkeypatch.setattr(agents, "reject_if_kiro_unverified", _never_spawn)

    resp = await agents.api_models(_request(_codex_provider(tmp_path)))

    assert resp.status == 200
    assert _names(json.loads(resp.body)) == ["auto", "gpt-5.4", "gpt-5.4-codex", "gpt-5.5"]


# ── The shared-runtime handle path (the path a dashboard codex chat takes) ──
#
# Every dashboard chat runs on AcpSessionHandle, not the direct-spawn AcpClient.
# That path captured the advertised list onto the live handle but never fed the
# CROSS-SESSION cache, so the codex picker emptied to auto + default the moment
# no codex session was live (a harness switch away and back, or a restart). The
# feed below is the fix.


def _feeding_handle(backend: str, models: list[dict]):
    """A minimal stand-in carrying only what ``_feed_advertised_model_cache`` reads."""
    return SimpleNamespace(
        _runtime=SimpleNamespace(acp_backend=backend),
        _available_models=models,
        _advertised_models_changed=False,
    )


def test_handle_feed_populates_the_codex_cross_session_cache() -> None:
    from kiro_crew.acp import session_handle_model_selection as sh_models

    handle = _feeding_handle(
        ACP_BACKEND_CODEX,
        [{"modelId": "gpt-5.4"}, {"modelId": "gpt-5.5"}],
    )

    sh_models._feed_advertised_model_cache(handle)

    # Survives the live session: the picker's cold fallback now reads these.
    assert model_registry.advertised_models("codex") == ["gpt-5.4", "gpt-5.5"]
    assert model_registry.advertised_models("acp") == []
    assert handle._advertised_models_changed is True


def test_handle_feed_skips_a_non_advertised_backend() -> None:
    """kiro reaches its ids through --list-models, not this feed (harness-parity H6)."""
    from kiro_crew.acp import session_handle_model_selection as sh_models

    handle = _feeding_handle(ACP_BACKEND_KIRO, [{"modelId": "claude-opus-4.8"}])

    sh_models._feed_advertised_model_cache(handle)

    assert model_registry.advertised_models("acp") == []
    assert handle._advertised_models_changed is False


def test_handle_feed_is_a_no_op_when_nothing_was_advertised() -> None:
    from kiro_crew.acp import session_handle_model_selection as sh_models

    handle = _feeding_handle(ACP_BACKEND_CODEX, [])

    sh_models._feed_advertised_model_cache(handle)

    assert model_registry.advertised_models("codex") == []
    assert handle._advertised_models_changed is False


@pytest.mark.asyncio
async def test_handle_persist_offloads_only_when_changed(monkeypatch) -> None:
    from kiro_crew.acp import session_handle_model_selection as sh_models

    calls = []
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: calls.append(1))

    handle = SimpleNamespace(_advertised_models_changed=True)
    await sh_models._persist_advertised_models_if_changed(handle)
    assert calls == [1]
    # The flag clears so a second session-init step does not re-persist.
    assert handle._advertised_models_changed is False
    await sh_models._persist_advertised_models_if_changed(handle)
    assert calls == [1]


# ---------------------------------------------------------------------------
# Native display labels survive to the cold picker path.
#
# With no live session, the claude_code picker reads the advertised snapshot
# alone. ``refresh_advertised_models(rows=...)`` keeps each id's
# ``name``/``description`` in ``_ADVERTISED_META``, and ``advertised_model_rows``
# serves them to that cold path, so the picker shows the backend's own names
# (``Sonnet 5``) rather than raw ids (``claude-sonnet-5``).
# ---------------------------------------------------------------------------


def _rich_rows() -> list[dict]:
    return [
        {"modelId": "sonnet", "name": "Sonnet", "description": "Everyday"},
        {"modelId": "claude-sonnet-5", "name": "Claude Sonnet 5", "description": "Current"},
    ]


def test_refresh_with_rows_persists_native_labels() -> None:
    changed = model_registry.refresh_advertised_models(
        "claude_code", [r["modelId"] for r in _rich_rows()], rows=_rich_rows()
    )
    assert changed is True
    rows = model_registry.advertised_model_rows("claude_code")
    by_id = {r["model_name"]: r for r in rows}
    assert by_id["claude-sonnet-5"]["display_name"] == "Claude Sonnet 5"
    assert by_id["claude-sonnet-5"]["description"] == "Current"
    # The id-only contract is unchanged for every entitlement caller.
    assert model_registry.advertised_models("claude_code") == ["sonnet", "claude-sonnet-5"]


def test_refresh_without_rows_leaves_labels_untouched() -> None:
    """A legacy id-only call keeps the id list and does not clear existing labels."""
    model_registry.refresh_advertised_models(
        "claude_code", ["sonnet", "claude-sonnet-5"], rows=_rich_rows()
    )
    # Same ids, no rows: a no-op on ids, labels preserved.
    changed = model_registry.refresh_advertised_models("claude_code", ["sonnet", "claude-sonnet-5"])
    assert changed is False
    assert (
        model_registry.advertised_model_rows("claude_code")[1]["display_name"] == "Claude Sonnet 5"
    )


def test_label_only_change_reports_changed_so_it_repersists() -> None:
    model_registry.refresh_advertised_models(
        "claude_code", ["sonnet", "claude-sonnet-5"], rows=_rich_rows()
    )
    relabeled = [
        {"modelId": "sonnet", "name": "Sonnet", "description": "Everyday"},
        {"modelId": "claude-sonnet-5", "name": "Claude Sonnet 5.5", "description": "Newer"},
    ]
    changed = model_registry.refresh_advertised_models(
        "claude_code", ["sonnet", "claude-sonnet-5"], rows=relabeled
    )
    assert changed is True  # ids identical, but a label moved -> persist warranted


def test_label_less_rows_report_no_change_on_repeat() -> None:
    """Rows that carry no label admit to an empty map, which matches no entry, so a repeat is a no-op."""
    rows = [{"modelId": "sonnet"}, {"modelId": "claude-sonnet-5"}]
    ids = [r["modelId"] for r in rows]
    assert model_registry.refresh_advertised_models("claude_code", ids, rows=rows) is True
    assert model_registry.refresh_advertised_models("claude_code", ids, rows=rows) is False
    assert "claude_code" not in model_registry._ADVERTISED_META


def test_a_cached_label_is_clipped_to_the_same_bound() -> None:
    big = model_registry.ADVERTISED_LABEL_MAX_CHARS
    model_registry.refresh_advertised_models(
        "claude_code",
        ["claude-big-1"],
        rows=[{"modelId": "claude-big-1", "name": "N" * 10_000, "description": "D" * 10_000}],
    )
    row = model_registry.advertised_model_rows("claude_code")[0]
    assert row["display_name"] == "N" * big
    assert row["description"] == "D" * big


def test_live_and_cached_rows_agree() -> None:
    """The live payload and the cached snapshot yield identical picker rows, oversized labels included."""
    payload = [
        *_rich_rows(),
        {
            "modelId": "x" * (model_registry.ADVERTISED_MODEL_ID_MAX_CHARS + 1),
            "name": "Huge",
            "description": "",
        },
        {"modelId": "claude-big-1", "name": "N" * 10_000, "description": "D" * 10_000},
    ]
    model_registry.refresh_advertised_models(
        "claude_code", [r["modelId"] for r in payload], rows=payload
    )
    assert model_registry.live_advertised_model_rows(
        payload
    ) == model_registry.advertised_model_rows("claude_code")


def test_cold_sidecar_roundtrip_keeps_labels(tmp_path, monkeypatch) -> None:
    """A persisted sidecar carrying the label block reloads with native labels."""
    sidecar = tmp_path / "provider_models.json"
    monkeypatch.setattr(model_registry, "_advertised_models_cache_path", lambda: sidecar)
    # Write the exact superset shape persist_advertised_models produces: legacy
    # ``{ns: [ids]}`` plus the reserved label block.
    sidecar.write_text(
        model_registry.json.dumps(
            {
                "claude_code": ["sonnet", "claude-sonnet-5"],
                "__labels__": {
                    "claude_code": {
                        "sonnet": {"name": "Sonnet", "description": "Everyday"},
                        "claude-sonnet-5": {"name": "Claude Sonnet 5", "description": "Current"},
                    }
                },
            }
        )
    )
    model_registry._ADVERTISED_MODELS.clear()
    model_registry._ADVERTISED_META.clear()
    model_registry._load_advertised_models()
    rows = {r["model_name"]: r for r in model_registry.advertised_model_rows("claude_code")}
    assert rows["claude-sonnet-5"]["display_name"] == "Claude Sonnet 5"
    assert rows["sonnet"]["description"] == "Everyday"


def test_persist_writes_the_label_block(tmp_path, monkeypatch) -> None:
    """``persist_advertised_models`` emits the reserved ``__labels__`` block."""
    sidecar = tmp_path / "provider_models.json"
    monkeypatch.setattr(model_registry, "_advertised_models_cache_path", lambda: sidecar)
    model_registry.refresh_advertised_models(
        "claude_code", [r["modelId"] for r in _rich_rows()], rows=_rich_rows()
    )
    _REAL_PERSIST()  # the autouse fixture stubbed the public name to a no-op
    data = model_registry.json.loads(sidecar.read_text())
    assert data["claude_code"] == ["sonnet", "claude-sonnet-5"]
    assert data["__labels__"]["claude_code"]["claude-sonnet-5"]["name"] == "Claude Sonnet 5"


def test_legacy_sidecar_without_label_block_loads(tmp_path, monkeypatch) -> None:
    """A legacy sidecar (``{ns: [ids]}``, no label block) loads with id-fallback labels."""
    sidecar = tmp_path / "provider_models.json"
    sidecar.write_text(model_registry.json.dumps({"codex": ["gpt-6-astra", "gpt-6-nova"]}))
    monkeypatch.setattr(model_registry, "_advertised_models_cache_path", lambda: sidecar)
    model_registry._ADVERTISED_MODELS.clear()
    model_registry._ADVERTISED_META.clear()
    model_registry._load_advertised_models()
    assert model_registry.advertised_models("codex") == ["gpt-6-astra", "gpt-6-nova"]
    # No labels persisted: rows fall back to the id as display_name.
    rows = model_registry.advertised_model_rows("codex")
    assert rows[0]["display_name"] == "gpt-6-astra"
