"""An explicit slot effort must survive the codex ``<model>[<effort>]`` spelling fold.

Reported on a codex chat: the composer showed ``gpt-6.1-sol`` / Medium while every
reply footer read ``gpt-6.1-sol[low]``, and codex's own ``turn_context`` records
said ``effort=low`` for all five turns. Persisted slot state was
``model=gpt-6.1-sol, reasoning_effort=medium``.

The chain, on a cold start or a resume:

1. The config factory seeds the slot's effort under the slot's model, the BARE
   ``gpt-6.1-sol``.
2. codex advertises only pair rows (``gpt-6.1-sol[low]``, ``[medium]``, ...), so
   the shared-runtime start finds the bare pin "unusable" and folds it with
   ``resolve_pin_spelling``. Every row folds to the same catalog key and the
   tie-break picks the SHORTEST spelling: ``[low]``.
3. ``set_model("gpt-6.1-sol[low]")`` splits into ``model=gpt-6.1-sol`` plus
   ``reasoning_effort=low`` and records the pair.
4. ``_apply_initial_effort`` resolves the slot level for the RECORDED id, finds
   no override under ``gpt-6.1-sol[low]``, and pushes nothing.

So the session runs ``low`` and every reply is attributed to ``[low]``. The tests
drive ``AcpProvider.start`` with a stand-in for the runtime step that performs
step 2 and 3 with the production functions, then read what codex was sent and
what the provider reports as the served model.

The id recorded after the push has to stay an ADVERTISED spelling: codex
advertises only pair rows, and both the throttle-fallback restore
(``AcpSessionProvider.set_model`` -> ``model_is_unusable``) and the strict
one-liner canary compare ``served_model`` against that list by exact string. So
the pushed level is recorded as its advertised row (``gpt-6.1-sol[medium]``), and
when the list carries no such row the id stays as the fold left it.

The re-record runs ONCE, from ``_apply_initial_effort``. The invariant: once a
session has started, the recorded id changes only on a model push, and every id
the provider records is one ``available_models()`` advertises. A live
``change_effort`` writes the level but leaves the id alone, and ``set_model``
leaves the id the model push recorded (the one the fallback machinery publishes
as the active fallback model) rather than re-recording it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.runtime_models import (
    advertised_model_ids,
    model_is_unusable,
    resolve_pin_spelling,
)
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    effort_config_option_id,
)
from kiro_crew.dashboard.handlers.usage import read_turn_model
from kiro_crew.providers.acp import AcpProvider

CODEX_EFFORT = effort_config_option_id(ACP_BACKEND_CODEX)
BARE = "gpt-6.1-sol"
TARGET = "gpt-6.2-sol"
LEVELS = ("low", "medium", "high", "xhigh")


def _session_new(
    rows: tuple[str, ...],
    levels: tuple[str, ...] = LEVELS,
    models: tuple[str, ...] = (BARE,),
) -> dict:
    """A codex ``session/new`` answer with pair rows and an effort option."""
    return {
        "sessionId": "codex-sess",
        "models": {
            "currentModelId": f"{models[0]}[{rows[0]}]",
            "availableModels": [
                {"modelId": f"{model}[{lvl}]", "name": f"{model} ({lvl})"}
                for model in models
                for lvl in rows
            ],
        },
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": models[0],
                "options": [{"value": model, "name": model} for model in models],
            },
            {
                "id": CODEX_EFFORT,
                "type": "select",
                "currentValue": "medium",
                "options": [{"value": v, "name": v} for v in levels],
            },
        ],
    }


#: The reported catalog: one row per effort, never the bare id.
SESSION_NEW = _session_new(LEVELS)
#: A catalog with two codex models for live fallback switching.
SESSION_NEW_TWO_MODELS = _session_new(LEVELS, models=(BARE, TARGET))
#: A catalog that advertises no ``[medium]`` row while the effort option takes it.
SESSION_NEW_WITHOUT_MEDIUM = _session_new(("low", "high", "xhigh"))


@pytest.fixture(autouse=True)
def _cold_advertised_cache(monkeypatch):
    monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


def _codex(
    tmp_path,
    applied: list[tuple[str, str]],
    model: str = BARE,
    session_new: dict = SESSION_NEW,
) -> AcpClient:
    """A codex client whose ``set_config_option`` behaves like codex-acp's."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    client._session_id = "codex-sess"
    client._model = model
    client._capture_available_models(session_new)
    client._acp_config_options = session_new["configOptions"]

    allowed_models = {option["value"] for option in session_new["configOptions"][0]["options"]}

    async def _set(config_id: str, value: str) -> None:
        applied.append((config_id, value))
        if config_id == "model" and value not in allowed_models:
            raise AcpError("JSON-RPC error: Invalid params", code=-32602)
        if config_id == CODEX_EFFORT and value not in LEVELS:
            raise AcpError("JSON-RPC error: Invalid params", code=-32602)

    client.set_config_option = _set  # type: ignore[method-assign]
    return client


def _provider(client: AcpClient, overrides: dict[str, str]) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=ACP_BACKEND_CODEX)
    provider._client = client
    provider._effort_per_model = dict(overrides)
    configured = client._model

    async def _start_runtime() -> None:
        # The model step of ``_start_kiro_runtime_impl`` for a pin the pair-only
        # list does not carry literally: fold it with the production function,
        # then apply the folded id the way ``handle.set_model`` does.
        advertised = client._advertised_model_ids()
        send = configured
        if configured not in advertised:
            send = resolve_pin_spelling(configured, advertised)
        if send:
            await client.set_model(send)

    provider._start_kiro_runtime = _start_runtime  # type: ignore[method-assign]
    return provider


def _efforts(applied: list[tuple[str, str]]) -> list[str]:
    return [value for cid, value in applied if cid == CODEX_EFFORT]


def _assert_served_is_advertised(provider: AcpProvider) -> None:
    """The property the fallback restore and the strict canary depend on."""
    advertised = advertised_model_ids(provider.available_models())
    assert advertised, "the catalog under test must advertise rows"
    assert provider.served_model in advertised
    assert model_is_unusable(provider.served_model, advertised) is False


def test_codex_is_the_pair_id_harness_under_test() -> None:
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS


class TestColdStart:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("resumed", [False, True])
    async def test_a_bare_pin_with_explicit_medium_runs_medium(self, tmp_path, resumed) -> None:
        """The reported session: bare model, slot effort Medium."""
        applied: list[tuple[str, str]] = []
        provider = _provider(_codex(tmp_path, applied), {BARE: "medium"})
        provider.resumed = resumed

        await provider.start()

        # The level codex ends on is the slot's, whatever the fold wrote first.
        assert _efforts(applied)[-1] == "medium"
        # The reply footer reads served_model: the row for the level pushed,
        # which the catalog advertises, so the fallback restore and the strict
        # canary (both exact-string against the advertised list) still match.
        assert provider.served_model == f"{BARE}[medium]"
        _assert_served_is_advertised(provider)
        assert provider._resolve_effort() == "medium"

    @pytest.mark.asyncio
    async def test_a_pushed_level_without_an_advertised_row_leaves_the_folded_row(
        self, tmp_path
    ) -> None:
        """The effort option takes ``medium`` but no ``[medium]`` row exists: the
        recorded id stays the row the fold wrote, which the catalog advertises,
        rather than a bare id outside the list. The effort still runs."""
        applied: list[tuple[str, str]] = []
        client = _codex(tmp_path, applied, session_new=SESSION_NEW_WITHOUT_MEDIUM)
        provider = _provider(client, {BARE: "medium"})

        await provider.start()

        assert _efforts(applied)[-1] == "medium"
        assert provider.served_model == f"{BARE}[low]"
        _assert_served_is_advertised(provider)
        assert provider._effort_per_model == {BARE: "medium"}
        assert provider._resolve_effort() == "medium"

    @pytest.mark.asyncio
    async def test_a_restart_with_a_fresh_provider_keeps_medium(self, tmp_path) -> None:
        """A gateway restart rebuilds the provider from the persisted slot."""
        for _ in range(2):
            applied: list[tuple[str, str]] = []
            provider = _provider(_codex(tmp_path, applied), {BARE: "medium"})
            await provider.start()
            assert _efforts(applied)[-1] == "medium"
            assert provider.served_model == f"{BARE}[medium]"
            _assert_served_is_advertised(provider)

    @pytest.mark.asyncio
    async def test_a_picked_pair_row_without_an_override_keeps_its_effort(self, tmp_path) -> None:
        """A row chosen on purpose, with no separate slot level, is left alone."""
        applied: list[tuple[str, str]] = []
        provider = _provider(_codex(tmp_path, applied, model=f"{BARE}[high]"), {})

        await provider.start()

        assert _efforts(applied) == ["high"]
        assert provider.served_model == f"{BARE}[high]"

    @pytest.mark.asyncio
    async def test_a_picked_pair_row_whose_suffix_matches_the_override_is_unchanged(
        self, tmp_path
    ) -> None:
        applied: list[tuple[str, str]] = []
        provider = _provider(
            _codex(tmp_path, applied, model=f"{BARE}[high]"), {f"{BARE}[high]": "high"}
        )

        await provider.start()

        assert _efforts(applied)[-1] == "high"
        assert provider.served_model == f"{BARE}[high]"

    @pytest.mark.asyncio
    async def test_a_bare_pin_with_no_level_at_all_pushes_nothing_extra(self, tmp_path) -> None:
        applied: list[tuple[str, str]] = []
        provider = _provider(_codex(tmp_path, applied), {})

        await provider.start()

        # Whatever the model step wrote stands; no slot push follows it.
        assert len(_efforts(applied)) <= 1
        assert provider._resolve_effort() is None


class TestLiveChanges:
    @pytest.mark.asyncio
    async def test_a_live_effort_change_leaves_the_recorded_row_alone(self, tmp_path) -> None:
        """Effort ``high`` is written live; the recorded ``[low]`` id does not
        move, since the id changes only on a model push once the session runs."""
        applied: list[tuple[str, str]] = []
        client = _codex(tmp_path, applied, model=f"{BARE}[low]")
        client._resolved_model_id = f"{BARE}[low]"
        provider = _provider(client, {})

        assert await provider.change_effort("high") is True

        assert applied[-1] == (CODEX_EFFORT, "high")
        assert provider.served_model == f"{BARE}[low]"
        assert client._resolved_model_id == f"{BARE}[low]"
        _assert_served_is_advertised(provider)
        # Stored under the recorded spelling, which ``_resolve_effort`` reads
        # first, so the next resolve answers the level just pushed.
        assert provider._effort_per_model == {f"{BARE}[low]": "high"}
        assert provider._resolve_effort() == "high"

    @pytest.mark.asyncio
    async def test_a_later_change_on_a_recorded_row_is_stored_under_that_row(
        self, tmp_path
    ) -> None:
        """Cold start records ``[medium]``; a live change to high writes its
        override under that row and leaves the recorded id where it is."""
        applied: list[tuple[str, str]] = []
        provider = _provider(_codex(tmp_path, applied), {BARE: "medium"})
        await provider.start()
        assert provider.served_model == f"{BARE}[medium]"

        assert await provider.change_effort("high") is True

        assert _efforts(applied)[-1] == "high"
        assert provider.served_model == f"{BARE}[medium]"
        assert provider._effort_per_model == {BARE: "medium", f"{BARE}[medium]": "high"}
        assert provider._resolve_effort() == "high"
        _assert_served_is_advertised(provider)

    def test_the_advertised_spelling_wins_over_the_built_row(self, tmp_path) -> None:
        """The row is matched case-insensitively and recorded as advertised."""
        client = _codex(tmp_path, [], model=f"{BARE}[low]")
        client._capture_available_models(
            {
                "models": {
                    "currentModelId": f"{BARE}[low]",
                    "availableModels": [
                        {"modelId": f"{BARE}[low]", "name": "low"},
                        {"modelId": f"{BARE}[MEDIUM]", "name": "medium"},
                    ],
                }
            }
        )
        provider = _provider(client, {f"{BARE}[low]": "medium"})

        provider._record_pushed_effort("medium")

        assert provider.served_model == f"{BARE}[MEDIUM]"
        assert provider._effort_per_model == {BARE: "medium"}
        assert provider._resolve_effort() == "medium"

    @pytest.mark.asyncio
    async def test_a_live_switch_to_the_bare_model_lands_the_slot_level(self, tmp_path) -> None:
        applied: list[tuple[str, str]] = []
        client = _codex(tmp_path, applied, model=f"{BARE}[high]")
        provider = _provider(client, {BARE: "medium"})

        await provider.set_model(BARE)

        assert _efforts(applied)[-1] == "medium"
        # A bare pick goes through the client's own model push, which records
        # the bare id itself; the re-record only acts on a pair recording.
        assert "[high]" not in provider.served_model

    @pytest.mark.asyncio
    async def test_a_model_switch_keeps_the_id_the_model_push_recorded(self, tmp_path) -> None:
        """The carried level reaches the target live, and ``served_model`` is the
        id ``set_model`` recorded, the wire id the fallback machinery publishes as
        the active fallback model, so the restore probe compares like with like."""
        applied: list[tuple[str, str]] = []
        client = _codex(tmp_path, applied, session_new=SESSION_NEW_TWO_MODELS)
        provider = _provider(client, {BARE: "medium"})
        await provider.start()
        assert provider.served_model == f"{BARE}[medium]"
        applied.clear()
        pushed = client.set_model
        recorded_by_push: list[str] = []

        async def _spy(model_id: str) -> None:
            await pushed(model_id)
            recorded_by_push.append(client._model)

        client.set_model = _spy  # type: ignore[method-assign]

        await provider.set_model(f"{TARGET}[low]")

        assert _efforts(applied)[-1] == "medium"
        assert recorded_by_push == [f"{TARGET}[low]"]
        assert provider.served_model == recorded_by_push[-1]
        assert client._resolved_model_id == recorded_by_push[-1]
        _assert_served_is_advertised(provider)
        # The carried level is live only; the target's own override stays unset.
        assert provider._effort_per_model == {BARE: "medium"}

    @pytest.mark.asyncio
    async def test_clear_effort_removes_a_folded_bare_override(self, tmp_path) -> None:
        applied: list[tuple[str, str]] = []
        provider = _provider(
            _codex(tmp_path, applied, model=f"{BARE}[medium]"),
            {BARE: "medium"},
        )

        assert await provider.clear_effort() is False

        assert provider._effort_per_model == {}
        assert provider._resolve_effort() is None


class TestSharedRuntime:
    """On the shared runtime ``AcpProvider._client`` is an ``AcpSessionProvider``,
    and the recorded id lives on the ``AcpSessionHandle`` behind it."""

    def test_re_record_reaches_the_handle_resolved_id(self) -> None:
        """Both handle fields the footer and the backfill read end up on the
        advertised ``[medium]`` row."""
        handle = MagicMock()
        handle._model = f"{BARE}[low]"
        handle._resolved_model_id = f"{BARE}[low]"
        handle.available_models = SESSION_NEW["models"]["availableModels"]
        type(handle).model = property(lambda self: self._model)
        runtime = MagicMock()
        runtime.acp_backend = ACP_BACKEND_CODEX
        session_provider = AcpSessionProvider(handle, runtime)

        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(acp_backend=ACP_BACKEND_CODEX)
        provider._client = session_provider
        provider._effort_per_model = {f"{BARE}[low]": "medium"}

        provider._record_pushed_effort("medium")

        assert handle._model == f"{BARE}[medium]"
        assert handle._resolved_model_id == f"{BARE}[medium]"
        assert provider._effort_per_model == {BARE: "medium"}
        # The reply footer's reader prefers a resolved id anywhere in the chain.
        assert read_turn_model(provider) == f"{BARE}[medium]"


class TestScope:
    @pytest.mark.asyncio
    async def test_no_effort_option_leaves_the_recorded_row_alone(self, tmp_path) -> None:
        """Nothing written means nothing to re-record: the row still names the
        effort the session runs."""
        applied: list[tuple[str, str]] = []
        client = _codex(tmp_path, applied, model=f"{BARE}[low]")
        client._acp_config_options = [SESSION_NEW["configOptions"][0]]
        provider = _provider(client, {BARE: "medium"})

        await provider._apply_initial_effort()

        assert _efforts(applied) == []
        assert client._model == f"{BARE}[low]"

    def test_a_workspace_default_under_the_bare_id_does_not_answer_for_a_row(
        self, tmp_path
    ) -> None:
        """Only the explicit slot level crosses the fold; defaults keep their
        documented precedence below an explicit row pick."""
        provider = _provider(_codex(tmp_path, [], model=f"{BARE}[high]"), {})
        provider._effort_defaults = {BARE: "medium"}

        assert provider._resolve_effort() is None

    def test_a_non_pair_harness_does_not_strip_a_bracket(self, tmp_path) -> None:
        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(acp_backend=ACP_BACKEND_CLAUDE)
        provider._client.backend = ACP_BACKEND_CLAUDE
        provider._client._model = "claude-opus-4.7[high]"
        provider._effort_per_model = {"claude-opus-4.7": "low"}

        assert provider._pair_id_base(provider._client._model) == ""
