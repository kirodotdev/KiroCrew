"""A codex ``<model>[<effort>]`` pick from the advertised list must switch, not fail.

codex-acp 1.11 puts TWO spellings of one selection on the ``session/new`` wire:

* ``models.availableModels`` -- one entry per model x reasoning effort, spelled
  ``gpt-6-astra[max]`` (the legacy ``session/set_model`` vocabulary). This is the
  list ``_capture_available_models`` prefers, so it is what the picker shows and
  what ``AcpModelUnavailable`` quotes as "Available models".
* ``configOptions[model]`` -- the BARE ids ``gpt-6-astra``; the only vocabulary
  ``session/set_config_option("model", ...)`` accepts. The effort travels down a
  separate ``reasoning_effort`` option.

Crew switched on the config option with the pair verbatim, codex answered a bare
``-32602``, and the user read: "``gpt-6-astra[max]`` is not available on your
account. Available models: ..., gpt-6-astra[max], ...". A bare id typed by hand
(NOT in the picker) worked, which is the inverse of what the dialog claimed.
"""

from __future__ import annotations

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpClient, AcpError, AcpModelUnavailable
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    REASONING_EFFORT_CONFIG_ID,
)

#: codex-acp 1.11 ``session/new``: both spellings, as the adapter emits them.
CODEX_1_11_SESSION_NEW = {
    "sessionId": "codex-sess-2",
    "models": {
        "currentModelId": "openai.gpt-6-astra[high]",
        "availableModels": [
            {
                "modelId": "openai.gpt-6-astra[high]",
                "name": "GPT-6 Astra (high)",
                "description": "Flagship. Greater reasoning depth.",
            },
            {
                "modelId": "openai.gpt-6-astra[xhigh]",
                "name": "GPT-6 Astra (xhigh)",
                "description": "Flagship. Extra reasoning depth.",
            },
            {
                "modelId": "openai.gpt-6-astra[max]",
                "name": "GPT-6 Astra (max)",
                "description": "Flagship. Maximum reasoning depth for the hardest problems.",
            },
            {
                "modelId": "openai.gpt-5.5-codex[medium]",
                "name": "GPT-5.5 Codex (medium)",
                "description": "Coding. Balanced.",
            },
        ],
    },
    "configOptions": [
        {
            "id": "model",
            "type": "select",
            "currentValue": "openai.gpt-6-astra",
            "options": [
                {"value": "openai.gpt-6-astra", "name": "GPT-6 Astra"},
                {"value": "openai.gpt-5.5-codex", "name": "GPT-5.5 Codex"},
            ],
        },
        {
            "id": REASONING_EFFORT_CONFIG_ID,
            "type": "select",
            "currentValue": "high",
            "options": [
                {"value": "high", "name": "High"},
                {"value": "xhigh", "name": "Xhigh"},
                {"value": "max", "name": "Max"},
            ],
        },
    ],
}

BARE_MODELS = {"openai.gpt-6-astra", "openai.gpt-5.5-codex"}
EFFORTS = {"high", "xhigh", "max"}


@pytest.fixture(autouse=True)
def _cold_advertised_cache(monkeypatch):
    monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


def _codex_client(tmp_path, model: str = "openai.gpt-6-astra[high]") -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    client._session_id = "codex-sess-2"
    client._model = model
    client._capture_available_models(CODEX_1_11_SESSION_NEW)
    client._acp_config_options = CODEX_1_11_SESSION_NEW["configOptions"]
    return client


def _codex_acp_1_11(applied: list[tuple[str, str]], *, refuse_effort: set[str] = frozenset()):
    """A ``set_config_option`` double behaving like codex-acp's applySessionConfigOption."""

    async def _set(config_id: str, value: str) -> None:
        applied.append((config_id, value))
        if config_id == "model":
            if value not in BARE_MODELS:  # applyModelChange: id must be a bare model
                raise AcpError("JSON-RPC error: Invalid params", code=-32602)
            return
        if config_id == REASONING_EFFORT_CONFIG_ID:
            if value not in EFFORTS or value in refuse_effort:
                raise AcpError("JSON-RPC error: Invalid params", code=-32602)
            return
        raise AcpError("JSON-RPC error: Invalid params", code=-32602)

    return _set


# ── the seam itself ──


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("openai.gpt-6-astra[max]", ("openai.gpt-6-astra", "max")),
        ("gpt-5.5-codex[medium]", ("gpt-5.5-codex", "medium")),
        ("  gpt-6[xhigh] ", ("gpt-6", "xhigh")),
        # No suffix, or a WINDOW suffix: nothing to split, id reaches the wire intact.
        ("openai.gpt-6-astra", ("openai.gpt-6-astra", "")),
        ("global.anthropic.claude-opus-5[1m]", ("global.anthropic.claude-opus-5[1m]", "")),
        ("some-model[200k]", ("some-model[200k]", "")),
        ("auto", ("auto", "")),
        ("", ("", "")),
    ],
)
def test_split_effort_suffix(model_id, expected) -> None:
    assert model_registry.split_effort_suffix(model_id) == expected


# ── the reported failure: an advertised pick is refused as one value ──


class TestAdvertisedPairSwitch:
    @pytest.mark.asyncio
    async def test_the_advertised_pair_is_what_the_picker_offers(self, tmp_path) -> None:
        """Premise of the bug: the picker list IS the bracketed list."""
        client = _codex_client(tmp_path)
        assert "openai.gpt-6-astra[max]" in client._advertised_model_ids()
        assert "openai.gpt-6-astra" not in client._advertised_model_ids()

    @pytest.mark.asyncio
    async def test_an_advertised_pair_pick_switches_model_then_effort(self, tmp_path) -> None:
        client = _codex_client(tmp_path)
        applied: list[tuple[str, str]] = []
        client.set_config_option = _codex_acp_1_11(applied)  # type: ignore[method-assign]

        await client.set_model("openai.gpt-6-astra[max]")

        # The pair is tried verbatim first (an adapter that takes it keeps
        # working), then split into the two writes codex-acp actually accepts.
        assert applied == [
            ("model", "openai.gpt-6-astra[max]"),
            ("model", "openai.gpt-6-astra"),
            (REASONING_EFFORT_CONFIG_ID, "max"),
        ]
        # Recorded under the ADVERTISED spelling so the picker highlights the row.
        assert client._model == "openai.gpt-6-astra[max]"
        assert client._resolved_model_id == "openai.gpt-6-astra[max]"

    @pytest.mark.asyncio
    async def test_a_bare_id_typed_by_hand_still_works_in_one_write(self, tmp_path) -> None:
        """The inverse the user saw ("astra 6 works in some sessions") stays true."""
        client = _codex_client(tmp_path)
        applied: list[tuple[str, str]] = []
        client.set_config_option = _codex_acp_1_11(applied)  # type: ignore[method-assign]

        await client.set_model("openai.gpt-6-astra")

        assert applied == [("model", "openai.gpt-6-astra")]
        assert client._model == "openai.gpt-6-astra"

    @pytest.mark.asyncio
    async def test_startup_pin_of_a_pair_is_applied_not_withheld(self, tmp_path) -> None:
        """A persisted ``[max]`` slot model is applied at spawn. Withholding it
        leaves the session on the adapter default while the UI shows [max]."""
        client = _codex_client(tmp_path, model="openai.gpt-6-astra[max]")
        applied: list[tuple[str, str]] = []
        client.set_config_option = _codex_acp_1_11(applied)  # type: ignore[method-assign]

        await client._apply_startup_model()

        assert (REASONING_EFFORT_CONFIG_ID, "max") in applied
        assert client._model == "openai.gpt-6-astra[max]"

    @pytest.mark.asyncio
    async def test_refused_effort_keeps_the_model_switch_and_records_the_bare_id(
        self, tmp_path
    ) -> None:
        """Model landed, effort refused: no raise (the switch DID happen), and the
        recorded id does not overclaim an effort the adapter did not apply."""
        client = _codex_client(tmp_path)
        applied: list[tuple[str, str]] = []
        client.set_config_option = _codex_acp_1_11(  # type: ignore[method-assign]
            applied, refuse_effort={"max"}
        )

        await client.set_model("openai.gpt-6-astra[max]")

        assert applied[-1] == (REASONING_EFFORT_CONFIG_ID, "max")
        assert client._model == "openai.gpt-6-astra"

    @pytest.mark.asyncio
    async def test_no_effort_option_advertised_applies_the_model_half_only(self, tmp_path) -> None:
        client = _codex_client(tmp_path)
        client._acp_config_options = [CODEX_1_11_SESSION_NEW["configOptions"][0]]
        applied: list[tuple[str, str]] = []
        client.set_config_option = _codex_acp_1_11(applied)  # type: ignore[method-assign]

        await client.set_model("openai.gpt-6-astra[max]")

        assert (REASONING_EFFORT_CONFIG_ID, "max") not in applied
        assert client._model == "openai.gpt-6-astra"

    @pytest.mark.asyncio
    async def test_a_transport_failure_on_the_effort_write_still_propagates(self, tmp_path) -> None:
        client = _codex_client(tmp_path)

        async def _set(config_id: str, value: str) -> None:
            if config_id == "model" and value in BARE_MODELS:
                return
            if config_id == "model":
                raise AcpError("JSON-RPC error: Invalid params", code=-32602)
            raise AcpError("process died", transient=True)

        client.set_config_option = _set  # type: ignore[method-assign]

        with pytest.raises(AcpError, match="process died"):
            await client.set_model("openai.gpt-6-astra[max]")

    @pytest.mark.asyncio
    async def test_a_pair_whose_model_half_is_unknown_is_still_typed(self, tmp_path) -> None:
        """Both spellings refused: the explicit-pick contract is unchanged."""
        client = _codex_client(tmp_path)
        applied: list[tuple[str, str]] = []
        client.set_config_option = _codex_acp_1_11(applied)  # type: ignore[method-assign]

        with pytest.raises(AcpModelUnavailable):
            await client.set_model("openai.gpt-7[max]")

        assert applied == [("model", "openai.gpt-7[max]"), ("model", "openai.gpt-7")]
        assert client._model == "openai.gpt-6-astra[high]"


# ── harness parity: the split is an opt-in codex capability ──


def test_the_split_is_declared_as_a_codex_capability() -> None:
    assert ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS == frozenset({ACP_BACKEND_CODEX})


@pytest.mark.asyncio
async def test_a_non_member_backend_never_takes_the_split(tmp_path) -> None:
    """claude-agent-acp switches on the same config option but advertises no
    pair ids: a refused bracketed value stays refused, and the ladder never
    writes a stripped base model or a ``reasoning_effort`` it did not ask for."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._session_id = "claude-sess-1"
    client._model = "claude-opus-4-8[1m]"
    client._capture_available_models(
        {"models": {"availableModels": [{"modelId": "claude-opus-4-8[1m]"}]}}
    )
    applied: list[tuple[str, str]] = []

    async def _refuse_all(config_id: str, value: str) -> None:
        applied.append((config_id, value))
        raise AcpError(f"Invalid value for config option {config_id}: {value}")

    client.set_config_option = _refuse_all  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable):
        await client.set_model("claude-opus-4-8[max]")

    assert all(cid == "model" for cid, _ in applied)
    assert ("model", "claude-opus-4-8") not in applied
    assert client._model == "claude-opus-4-8[1m]"


# ── the error text, for the residual case ──


def test_refusal_of_an_advertised_id_is_not_blamed_on_the_account() -> None:
    exc = AcpModelUnavailable("gpt-6-astra[max]", ["gpt-6-astra[high]", "gpt-6-astra[max]"])
    assert "advertised" in str(exc)
    assert "not an account restriction" in str(exc)
    assert "whoami" not in str(exc)


def test_refusal_of_an_unadvertised_id_keeps_the_entitlement_hint() -> None:
    exc = AcpModelUnavailable("claude-opus-5", ["gpt-6-astra[high]"])
    assert "not available on your account" in str(exc)
    assert "whoami" in str(exc)
