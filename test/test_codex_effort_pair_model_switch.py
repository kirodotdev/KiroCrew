"""A codex ``<model>[<effort>]`` pick from the advertised list must switch, not fail.

codex-acp 1.11 puts TWO spellings of one selection on the ``session/new`` wire:

* ``models.availableModels`` -- one entry per model x reasoning effort, spelled
  ``gpt-6-astra[max]`` (the legacy ``session/set_model`` vocabulary). This is the
  list ``_capture_available_models`` prefers, so it is what the picker shows and
  what ``AcpModelUnavailable`` quotes as "Available models".
* ``configOptions[model]`` -- the BARE ids ``gpt-6-astra``; the only vocabulary
  ``session/set_config_option("model", ...)`` accepts. The effort travels down a
  separate ``reasoning_effort`` option.

That second spelling is the whole reason ``effort_config_option_id`` exists:
claude-agent-acp calls the option ``effort`` and codex-acp calls it
``reasoning_effort``, so every effort site -- the dashboard's live change, the
startup application of a persisted slot level, the knowledge pool's apply, the
level reader behind the dropdown, and the effort half of a pair pick -- resolves
the id per backend. A site that names one spelling writes an option the other
adapter does not know; the adapter answers "unknown config option", which every
one of those callers reads as "no effort selector" and skips. The session then
runs an effort nobody chose while the UI reports the level the user picked.

Crew switched on the config option with the pair verbatim, codex answered a bare
``-32602``, and the user read: "``gpt-6-astra[max]`` is not available on your
account. Available models: ..., gpt-6-astra[max], ...". A bare id typed by hand
(NOT in the picker) worked, which is the inverse of what the dialog claimed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.client import (
    AcpClient,
    AcpError,
    AcpModelUnavailable,
    resolve_usable_model,
)
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    effort_config_option_id,
)
from kiro_crew.providers.acp import AcpProvider

#: The id codex-acp carries its reasoning effort under, read through the resolver
#: rather than spelled again here -- a second copy in the tests would agree with
#: itself while the production sites disagreed.
CODEX_EFFORT = effort_config_option_id(ACP_BACKEND_CODEX)
CLAUDE_EFFORT = effort_config_option_id(ACP_BACKEND_CLAUDE)

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
            "id": CODEX_EFFORT,
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
        if config_id == CODEX_EFFORT:
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


def test_the_effort_option_id_is_resolved_per_backend() -> None:
    """codex-acp is the exception; every other harness keeps the ``effort``
    spelling, including one registered later with no row of its own."""
    assert CODEX_EFFORT == "reasoning_effort"
    assert CLAUDE_EFFORT == "effort"
    assert effort_config_option_id(ACP_BACKEND_KIRO) == "effort"
    assert effort_config_option_id("a-harness-added-later") == "effort"


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
            (CODEX_EFFORT, "max"),
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

        assert (CODEX_EFFORT, "max") in applied
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

        assert applied[-1] == (CODEX_EFFORT, "max")
        assert client._model == "openai.gpt-6-astra"

    @pytest.mark.asyncio
    async def test_no_effort_option_advertised_applies_the_model_half_only(self, tmp_path) -> None:
        client = _codex_client(tmp_path)
        client._acp_config_options = [CODEX_1_11_SESSION_NEW["configOptions"][0]]
        applied: list[tuple[str, str]] = []
        client.set_config_option = _codex_acp_1_11(applied)  # type: ignore[method-assign]

        await client.set_model("openai.gpt-6-astra[max]")

        assert (CODEX_EFFORT, "max") not in applied
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


# ── the sibling effort sites: one spelling per backend, everywhere ──


def _effort_provider(backend: str, model: str, *, applied: list[tuple[str, str]]) -> AcpProvider:
    """A provider whose client records every ``set_config_option`` write."""
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    provider._client._model = model
    provider._client._work_dir = MagicMock()
    provider._client.send_command = AsyncMock()
    provider._client.supports_config_option = MagicMock(return_value=True)

    async def _set(config_id: str, value: str) -> None:
        applied.append((config_id, value))

    provider._client.set_config_option = _set
    return provider


class TestSlotEffortOverrideReachesCodex:
    """The dashboard's effort dropdown and the startup application of a persisted
    slot level both go through ``_set_effort_config_option``. On codex they must
    write ``reasoning_effort``: writing ``effort`` draws "unknown config option",
    which that method treats as "this adapter has no effort selector" and skips."""

    @pytest.mark.asyncio
    async def test_codex_effort_push_writes_the_codex_spelling(self) -> None:
        applied: list[tuple[str, str]] = []
        provider = _effort_provider(ACP_BACKEND_CODEX, "openai.gpt-6-astra", applied=applied)

        await provider._set_effort_config_option("high")

        assert applied == [(CODEX_EFFORT, "high")]

    @pytest.mark.asyncio
    async def test_claude_effort_push_keeps_the_default_spelling(self) -> None:
        applied: list[tuple[str, str]] = []
        provider = _effort_provider(ACP_BACKEND_CLAUDE, "claude-opus-4.7", applied=applied)

        await provider._set_effort_config_option("high")

        assert applied == [(CLAUDE_EFFORT, "high")]

    @pytest.mark.asyncio
    async def test_codex_effort_push_checks_the_codex_option_for_support(self) -> None:
        """The capability precheck asks about the id it is about to write. Asking
        about ``effort`` on codex answers False and skips a push that would have
        worked."""
        applied: list[tuple[str, str]] = []
        provider = _effort_provider(ACP_BACKEND_CODEX, "openai.gpt-6-astra", applied=applied)

        await provider._set_effort_config_option("high")

        provider._client.supports_config_option.assert_called_with(CODEX_EFFORT)

    @pytest.mark.asyncio
    async def test_codex_change_effort_writes_the_codex_spelling(self) -> None:
        applied: list[tuple[str, str]] = []
        provider = _effort_provider(ACP_BACKEND_CODEX, "openai.gpt-6-astra", applied=applied)

        assert await provider.change_effort("high") is True

        assert (CODEX_EFFORT, "high") in applied
        assert all(cid == CODEX_EFFORT for cid, _ in applied)

    @pytest.mark.asyncio
    async def test_a_codex_value_rejection_steps_down_the_ladder(self) -> None:
        """codex refuses a VALUE with a bare ``-32602`` and no message, so the
        ladder cannot recognise it by the option name the way claude's message
        allows. Read as a transport fault it propagates, and the dashboard resets
        the session instead of landing the highest level the model supports."""
        applied: list[tuple[str, str]] = []
        provider = _effort_provider(ACP_BACKEND_CODEX, "openai.gpt-6-astra", applied=applied)

        async def _refuse_max(config_id: str, value: str) -> None:
            applied.append((config_id, value))
            if value == "max":
                raise AcpError("JSON-RPC error: Invalid params", code=-32602)

        provider._client.set_config_option = _refuse_max

        await provider._set_effort_config_option("max")

        assert applied == [(CODEX_EFFORT, "max"), (CODEX_EFFORT, "xhigh")]

    @pytest.mark.asyncio
    async def test_a_code_only_rejection_descends_the_ladder_on_claude_too(self) -> None:
        """The ladder reads a bare ``-32602`` as a value verdict for EVERY backend,
        not only for the harness that sends no message.

        ``-32602`` is "invalid params", and for ``set_config_option`` the request
        shape is fixed while the value is the only thing that varies, so the code is
        a verdict about the value wherever it arrives. Reading it per harness would
        make this path disagree with the model push, which has always accepted the
        bare code from any backend -- one wire frame, two meanings on one session.

        The cost on a harness that does name the option is bounded: extra writes
        down the ladder, and if every level is refused the last error still
        surfaces (pinned below).
        """
        applied: list[tuple[str, str]] = []
        provider = _effort_provider(ACP_BACKEND_CLAUDE, "claude-opus-4.7", applied=applied)

        async def _refuse_max_by_code(config_id: str, value: str) -> None:
            applied.append((config_id, value))
            if value == "max":
                raise AcpError("JSON-RPC error: Invalid params", code=-32602)

        provider._client.set_config_option = _refuse_max_by_code

        await provider._set_effort_config_option("max")

        assert applied == [(CLAUDE_EFFORT, "max"), (CLAUDE_EFFORT, "xhigh")]

    @pytest.mark.asyncio
    async def test_a_ladder_refused_at_every_level_surfaces_the_last_error(self) -> None:
        """The descent is not a swallow: exhausting the ladder re-raises."""
        applied: list[tuple[str, str]] = []
        provider = _effort_provider(ACP_BACKEND_CLAUDE, "claude-opus-4.7", applied=applied)

        async def _refuse_every_level(config_id: str, value: str) -> None:
            applied.append((config_id, value))
            raise AcpError("JSON-RPC error: Invalid params", code=-32602)

        provider._client.set_config_option = _refuse_every_level

        with pytest.raises(AcpError, match="Invalid params"):
            await provider._set_effort_config_option("max")

        # Every level below the requested one was tried before giving up.
        assert [value for _, value in applied] == ["max", "xhigh", "high", "medium", "low"]

    @pytest.mark.asyncio
    async def test_a_transport_failure_on_the_effort_push_still_propagates(self) -> None:
        applied: list[tuple[str, str]] = []
        provider = _effort_provider(ACP_BACKEND_CODEX, "openai.gpt-6-astra", applied=applied)
        provider._client.set_config_option = AsyncMock(side_effect=AcpError("process died"))

        with pytest.raises(AcpError, match="process died"):
            await provider._set_effort_config_option("high")


class TestPersistedPairAndSlotEffortAgree:
    """The scenario the two spellings produced: a persisted ``model[max]`` pin and
    a slot effort of ``high`` on the same session. The model is applied first
    (which writes ``max``), then the slot override runs -- so the level the
    session ends on must be the slot's, which is what the UI reports."""

    @pytest.mark.asyncio
    async def test_the_slot_effort_is_the_last_level_written(self, tmp_path) -> None:
        """Driven through ``AcpProvider.start``, so the ORDER under test is the
        production statement rather than one this test performs itself. A start
        path that applied the slot effort before the model would land ``max``
        last and fail here.

        codex is a member of ACP_BACKENDS_ACP_RUNTIME, so ``start`` reaches the
        shared-runtime step and only then the effort push. The double therefore
        stands in for that step -- doing exactly what it does with a persisted pin,
        and nothing else this test needs -- which keeps the sequence the production
        method's while leaving the adapter unspawned. Standing in for the per-session
        client handshake instead would test an order codex does not take.
        """
        client = _codex_client(tmp_path, model="openai.gpt-6-astra[max]")
        applied: list[tuple[str, str]] = []
        client.set_config_option = _codex_acp_1_11(applied)  # type: ignore[method-assign]

        async def _start_runtime() -> None:
            # The model half of the pair reaches the wire on the runtime start,
            # which is what the real session/new + set_model sequence does with a
            # persisted pin.
            await client._apply_startup_model()

        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(acp_backend=ACP_BACKEND_CODEX)
        provider._client = client
        provider._start_kiro_runtime = _start_runtime  # type: ignore[method-assign]
        provider._effort_per_model = {"openai.gpt-6-astra[max]": "high"}

        assert provider.is_acp_runtime_backend is True

        await provider.start()

        efforts = [value for cid, value in applied if cid == CODEX_EFFORT]
        assert efforts == ["max", "high"]
        assert applied[-1] == (CODEX_EFFORT, "high")

    @pytest.mark.asyncio
    async def test_the_startup_effort_push_is_not_skipped_on_codex(self, tmp_path) -> None:
        """The push runs for codex even though codex is on the shared runtime.

        Which harnesses take the push is decided by
        ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION -- the set naming the CHANNEL -- and
        not by the transport, and this test is where the two answers differ.
        codex is a runtime member AND a channel member, so a gate that read the
        transport would skip the one channel a fresh codex session has for its slot
        level: it reads no cli.json overlay, so nothing else would carry it. The
        kiro family is the mirror image, a runtime member outside the channel set,
        and it still skips.
        """
        client = _codex_client(tmp_path, model="openai.gpt-6-astra[max]")
        applied: list[tuple[str, str]] = []
        client.set_config_option = _codex_acp_1_11(applied)  # type: ignore[method-assign]
        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(acp_backend=ACP_BACKEND_CODEX)
        provider._client = client
        provider._effort_per_model = {"openai.gpt-6-astra[max]": "high"}

        assert provider.is_acp_runtime_backend is True
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_ACP_RUNTIME
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
        await provider._apply_initial_effort()

        assert applied == [(CODEX_EFFORT, "high")]


class TestTheSessionHandleTakesTheSameSplit:
    """The handle keeps its own copy of the ladder, and a source-text parity
    guard cannot tell wired-up code from dead code. Drive the real refusal."""

    @pytest.mark.asyncio
    async def test_a_codex_handle_applies_a_pair_as_two_writes(self) -> None:
        handle = MagicMock()
        handle._runtime = MagicMock()
        handle._runtime.acp_backend = ACP_BACKEND_CODEX
        handle._config_options = CODEX_1_11_SESSION_NEW["configOptions"]
        handle._advertised_model_ids = MagicMock(return_value=["openai.gpt-6-astra[max]"])
        applied: list[tuple[str, str]] = []
        handle.set_config_option = _codex_acp_1_11(applied)
        handle.supports_config_option = lambda config_id: any(
            opt["id"] == config_id for opt in CODEX_1_11_SESSION_NEW["configOptions"]
        )
        handle._push_model_config_option = lambda model_id, *, strict: (
            AcpSessionHandle._push_model_config_option(handle, model_id, strict=strict)
        )

        applied_id = await AcpSessionHandle._push_model_config_option(
            handle, "openai.gpt-6-astra[max]", strict=True
        )

        assert applied == [
            ("model", "openai.gpt-6-astra[max]"),
            ("model", "openai.gpt-6-astra"),
            (CODEX_EFFORT, "max"),
        ]
        assert applied_id == "openai.gpt-6-astra[max]"

    @pytest.mark.asyncio
    async def test_a_bare_model_refusal_respects_explicit_pick_strictness(self) -> None:
        handle = MagicMock()
        handle._runtime = MagicMock()
        handle._runtime.acp_backend = ACP_BACKEND_CODEX
        handle._advertised_model_ids = MagicMock(return_value=["gpt-6.1-sol[high]"])
        handle._resolved_model_id = "gpt-6-luna[high]"
        handle.set_config_option = AsyncMock(
            side_effect=AcpError("JSON-RPC error: Invalid params", code=-32602)
        )
        handle._push_model_config_option = lambda model_id, *, strict: (
            AcpSessionHandle._push_model_config_option(handle, model_id, strict=strict)
        )

        await AcpSessionHandle.set_model(handle, "gpt-6.1-sol", strict=False)

        with pytest.raises(AcpModelUnavailable) as exc_info:
            await AcpSessionHandle.set_model(handle, "gpt-6.1-sol", strict=True)

        message = str(exc_info.value)
        # The bare id is served by the advertised pairs, so a refusal is the
        # adapter contradicting its own list, never an account verdict.
        assert "not an account restriction" in message
        assert "not among the models" not in message
        assert "whoami" not in message

    @pytest.mark.asyncio
    async def test_a_non_member_handle_never_takes_the_split(self) -> None:
        handle = MagicMock()
        handle._runtime = MagicMock()
        handle._runtime.acp_backend = ACP_BACKEND_KIRO
        handle._config_options = CODEX_1_11_SESSION_NEW["configOptions"]
        handle._advertised_model_ids = MagicMock(return_value=["some-model[max]"])
        handle._resolved_model_id = "some-model"
        applied: list[tuple[str, str]] = []

        async def _refuse_all(config_id: str, value: str) -> None:
            applied.append((config_id, value))
            raise AcpError(f"Invalid value for config option {config_id}: {value}")

        handle.set_config_option = _refuse_all

        assert (
            await AcpSessionHandle._push_model_config_option(
                handle, "some-model[max]", strict=False
            )
            == ""
        )
        assert all(cid == "model" for cid, _ in applied)
        assert CODEX_EFFORT not in [cid for cid, _ in applied]


class TestEffortLevelsReadTheCodexSpelling:
    """``get_valid_effort_levels`` fills the dropdown. Reading a hard-coded
    ``effort`` id off a codex session returns an empty list, which every caller
    reads as "this model has no effort levels"."""

    def test_client_reads_the_codex_option_values(self, tmp_path) -> None:
        client = _codex_client(tmp_path)
        assert client.get_valid_effort_levels() == ["high", "xhigh", "max"]

    def test_client_reads_the_default_option_on_a_non_codex_backend(self, tmp_path) -> None:
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._acp_config_options = [
            {"id": CLAUDE_EFFORT, "options": [{"value": "high"}, {"value": "max"}]},
            {"id": CODEX_EFFORT, "options": [{"value": "xhigh"}]},
        ]
        assert client.get_valid_effort_levels() == ["high", "max"]

    def test_session_handle_reads_the_codex_option_values(self) -> None:
        handle = MagicMock()
        handle._runtime = MagicMock()
        handle._runtime.acp_backend = ACP_BACKEND_CODEX
        handle._config_options = CODEX_1_11_SESSION_NEW["configOptions"]

        levels = AcpSessionHandle.get_valid_effort_levels(handle)

        assert levels == ["high", "xhigh", "max"]

    def test_session_handle_reads_the_default_option_elsewhere(self) -> None:
        handle = MagicMock()
        handle._runtime = MagicMock()
        handle._runtime.acp_backend = ACP_BACKEND_KIRO
        handle._config_options = [
            {"id": CLAUDE_EFFORT, "options": [{"value": "low"}, {"value": "high"}]},
            {"id": CODEX_EFFORT, "options": [{"value": "max"}]},
        ]

        assert AcpSessionHandle.get_valid_effort_levels(handle) == ["low", "high"]


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
    exc = AcpModelUnavailable(
        "gpt-6-astra[max]",
        ["gpt-6-astra[high]", "gpt-6-astra[max]"],
        advertised_but_refused=True,
    )
    assert "advertised" in str(exc)
    assert "not an account restriction" in str(exc)
    assert "whoami" not in str(exc)


def test_refusal_of_an_unadvertised_id_keeps_the_entitlement_hint() -> None:
    exc = AcpModelUnavailable("claude-opus-5", ["gpt-6-astra[high]"])
    assert "not available on your account" in str(exc)
    assert "whoami" in str(exc)


def test_an_advertised_id_alone_does_not_earn_the_mismatch_wording() -> None:
    """ "Advertised implies entitled" is established for codex-acp only. A harness
    that lists models an account cannot run must keep the entitlement wording, or
    a user on the wrong tier is told their account is fine and given no probe."""
    exc = AcpModelUnavailable("some-model", ["some-model", "other-model"])
    assert "not available on your account" in str(exc)
    assert "whoami" in str(exc)
    assert "not an account restriction" not in str(exc)


@pytest.mark.asyncio
async def test_a_codex_refusal_of_an_advertised_pair_reports_the_mismatch(tmp_path) -> None:
    """The caller supplies the verdict, so the wording follows membership rather
    than the mere presence of the id in the list."""
    client = _codex_client(tmp_path)
    client._acp_config_options = [CODEX_1_11_SESSION_NEW["configOptions"][0]]

    async def _refuse_all(config_id: str, value: str) -> None:
        raise AcpError("JSON-RPC error: Invalid params", code=-32602)

    client.set_config_option = _refuse_all  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable) as caught:
        await client.set_model("openai.gpt-6-astra[max]")

    assert "not an account restriction" in str(caught.value)
    assert "not available on your account" not in str(caught.value)


@pytest.mark.asyncio
async def test_a_codex_refusal_of_a_bare_id_served_by_the_pairs_reports_the_mismatch(
    tmp_path,
) -> None:
    """A live pick arrives BARE (``openai.gpt-6-astra``) against a pair-only list,
    so exact membership alone would deny it the mismatch wording and fall through
    to the entitlement reading. The ladder's verdict folds the pair test in."""
    client = _codex_client(tmp_path)

    async def _refuse_all(config_id: str, value: str) -> None:
        raise AcpError("JSON-RPC error: Invalid params", code=-32602)

    client.set_config_option = _refuse_all  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable) as caught:
        await client.set_model("openai.gpt-6-astra")

    message = str(caught.value)
    assert "openai.gpt-6-astra" not in client._advertised_model_ids()
    assert "not an account restriction" in message
    assert "not among the models" not in message
    assert "whoami" not in message


@pytest.mark.asyncio
async def test_a_non_member_refusal_of_an_advertised_id_keeps_the_entitlement_wording(
    tmp_path,
) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._session_id = "claude-sess-2"
    client._model = "claude-opus-4-8"
    client._capture_available_models(
        {"models": {"availableModels": [{"modelId": "claude-opus-5-premium"}]}}
    )

    async def _refuse_all(config_id: str, value: str) -> None:
        raise AcpError(f"Invalid value for config option {config_id}: {value}")

    client.set_config_option = _refuse_all  # type: ignore[method-assign]

    with pytest.raises(AcpModelUnavailable) as caught:
        await client.set_model("claude-opus-5-premium")

    assert "claude-opus-5-premium" in client._advertised_model_ids()
    # Still an entitlement reading, not the pair-harness mismatch one ...
    assert "may not be available to the account" in str(caught.value)
    assert "not an account restriction" not in str(caught.value)
    # ... but without the kiro-cli sign-in advice, which a claude user cannot act on.
    assert "whoami" not in str(caught.value)


# ── live selection on the shared-session path ──
#
# Dashboard codex chats run on ``AcpSessionProvider`` (codex shares runtimes), and
# its ``set_model`` pre-flights an explicit pick against the advertised list. codex
# advertises ONLY ``<model>[<effort>]`` pairs, so the bare id the picker sends once
# a session is live (``gpt-6.1-sol``) matched nothing and was refused as "not
# available on your account", with the `kiro-cli whoami` hint, before the switch
# ever reached the adapter. A pick made before the session existed never took that
# path, which is why only the second and later picks failed.

#: The catalog from the reported gateway log, trimmed to two models. gpt-6-luna has
#: no ``[ultra]`` entry, which makes it the "effort this model does not serve" case.
CODEX_PAIR_CATALOG = [
    *(f"gpt-6.1-sol[{e}]" for e in ("low", "medium", "high", "xhigh", "max", "ultra")),
    *(f"gpt-6-luna[{e}]" for e in ("low", "medium", "high", "xhigh", "max")),
]


def _shared_codex_provider(
    advertised: list[str] = CODEX_PAIR_CATALOG, *, backend: str = ACP_BACKEND_CODEX
):
    from kiro_crew.acp.session_provider import AcpSessionProvider

    handle = MagicMock()
    handle.session_id = "codex-shared-1"
    handle.available_models = [{"modelId": m, "name": m} for m in advertised]
    handle.set_model = AsyncMock()
    handle.cancel = AsyncMock()
    handle.refresh_available_models = AsyncMock(return_value=[])
    runtime = MagicMock()
    runtime.acp_backend = backend
    runtime.is_alive.return_value = True
    return AcpSessionProvider(handle, runtime), handle


class TestSharedSessionLiveSelection:
    @pytest.mark.asyncio
    async def test_a_bare_model_served_only_as_pairs_is_switched_to(self) -> None:
        provider, handle = _shared_codex_provider()

        await provider.set_model("gpt-6.1-sol")

        # Sent as picked: the effort stays the slot's, re-applied by the caller,
        # rather than one this check chose from the catalog.
        handle.set_model.assert_awaited_once_with("gpt-6.1-sol", strict=True)
        handle.refresh_available_models.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_explicit_pair_pick_is_sent_with_its_effort(self) -> None:
        provider, handle = _shared_codex_provider()

        await provider.set_model("gpt-6.1-sol[xhigh]")

        handle.set_model.assert_awaited_once_with("gpt-6.1-sol[xhigh]", strict=True)

    @pytest.mark.asyncio
    async def test_a_pair_with_an_effort_the_model_lacks_is_still_refused(self) -> None:
        """Explicit effort is part of the pick: no substitution of another level."""
        provider, handle = _shared_codex_provider()

        with pytest.raises(AcpModelUnavailable):
            await provider.set_model("gpt-6-luna[ultra]")

        handle.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_model_codex_does_not_serve_is_refused_without_kiro_advice(self) -> None:
        provider, handle = _shared_codex_provider()

        with pytest.raises(AcpModelUnavailable) as caught:
            await provider.set_model("gpt-7-nova")

        handle.set_model.assert_not_awaited()
        msg = str(caught.value)
        assert "gpt-6.1-sol[low]" in msg
        assert "whoami" not in msg
        assert "Builder ID" not in msg

    @pytest.mark.asyncio
    async def test_a_bare_pick_after_stop_still_switches(self) -> None:
        provider, handle = _shared_codex_provider()

        await provider.cancel_session()
        await provider.set_model("gpt-6.1-sol")

        handle.set_model.assert_awaited_once_with("gpt-6.1-sol", strict=True)

    @pytest.mark.asyncio
    async def test_the_pair_fold_is_codex_only(self) -> None:
        """A harness that does not advertise pairs keeps exact membership."""
        provider, handle = _shared_codex_provider(backend=ACP_BACKEND_KIRO)

        with pytest.raises(AcpModelUnavailable) as caught:
            await provider.set_model("gpt-6.1-sol")

        handle.set_model.assert_not_awaited()
        assert "whoami" in str(caught.value)


@pytest.mark.asyncio
async def test_the_handle_sends_a_bare_pick_without_choosing_an_effort() -> None:
    """The handle applies what the provider accepted.

    Folding the bare id onto the catalog's preferred spelling would put
    ``gpt-6.1-sol[low]`` on the wire and write ``reasoning_effort=low`` that
    nobody picked.
    """
    handle = MagicMock()
    handle._runtime = MagicMock()
    handle._runtime.acp_backend = ACP_BACKEND_CODEX
    handle._advertised_model_ids = MagicMock(return_value=list(CODEX_PAIR_CATALOG))
    applied: list[tuple[str, str]] = []

    async def _push(model_id: str, *, strict: bool) -> str:
        applied.append(("model", model_id))
        return model_id

    handle._push_model_config_option = _push

    await AcpSessionHandle.set_model(handle, "gpt-6.1-sol", strict=True)

    assert applied == [("model", "gpt-6.1-sol")]
    assert handle._model == "gpt-6.1-sol"


@pytest.mark.asyncio
async def test_the_handle_resolves_a_non_strict_bare_pair_model() -> None:
    """Startup and restore retain the resolver behavior used on main."""
    handle = MagicMock()
    handle._runtime = MagicMock()
    handle._runtime.acp_backend = ACP_BACKEND_CODEX
    handle._advertised_model_ids = MagicMock(return_value=list(CODEX_PAIR_CATALOG))
    applied: list[tuple[str, str]] = []

    async def _push(model_id: str, *, strict: bool) -> str:
        applied.append(("model", model_id))
        return model_id

    handle._push_model_config_option = _push
    expected = resolve_usable_model("gpt-6.1-sol", CODEX_PAIR_CATALOG)

    await AcpSessionHandle.set_model(handle, "gpt-6.1-sol")

    assert applied == [("model", expected)]
    assert handle._model == expected


def test_unavailable_wording_keeps_the_kiro_sign_in_hint_for_host_sign_in_backends() -> None:
    # The hint follows host_auth: a harness on the host kiro-cli identity store
    # (kiro, KAS) keeps it, one that signs in separately (codex) does not.
    kiro = AcpModelUnavailable("gpt-7-nova", CODEX_PAIR_CATALOG, backend=ACP_BACKEND_KIRO)
    kas = AcpModelUnavailable("gpt-7-nova", CODEX_PAIR_CATALOG, backend=ACP_BACKEND_KAS)
    codex = AcpModelUnavailable("gpt-7-nova", CODEX_PAIR_CATALOG, backend=ACP_BACKEND_CODEX)

    assert "whoami" in str(kiro)
    assert "whoami" in str(kas)
    assert str(kas) == str(kiro)
    assert "whoami" not in str(codex)
    assert "Builder ID" not in str(codex)
    assert "gpt-6.1-sol[low]" in str(codex)
