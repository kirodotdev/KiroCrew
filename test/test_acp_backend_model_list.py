"""/api/models must not shell out to kiro-cli on another ACP backend.

That subprocess is both impossible (the binary may be absent) and wrong (its ids
are kiro-namespace and the other backend rejects them). The advertised list from a
live session is the only correct source.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_AUTO_MODEL,
    ACP_BACKENDS_SELECTABLE,
)
from kiro_crew.dashboard.handlers import agents as agents_handler
from kiro_crew.providers.base import LLMEvent, LLMProvider


class _Provider(LLMProvider):
    def __init__(self, rows: list[dict[str, str]], backend: str = "") -> None:
        self._rows = rows
        self._backend = backend

    @property
    def backend(self) -> str:
        return self._backend

    def available_models(self) -> list[dict[str, str]]:
        return self._rows

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def stream(self, message: str):
        if False:
            yield LLMEvent(kind="complete")

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        return None

    async def reject_tool(self, request_id: str | int) -> None:
        return None

    def context_usage_pct(self) -> float:
        return 0.0


class _NestedProvider(_Provider):
    """A nonconforming nested client must not become an H14 capability probe."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        super().__init__([])
        self.client = _Provider(rows)


def _request(providers: list[Any]) -> MagicMock:
    sessions = MagicMock()
    sessions.active_providers = lambda: providers
    state = MagicMock()
    state.sessions = sessions
    request = MagicMock()
    request.app = {"state": state}
    return request


class TestAdvertisedModels:
    def test_ids_are_passed_through_verbatim(self) -> None:
        """A Codex row is spelled <model>[<effort>].

        Rewriting it would produce an id the adapter never advertised, which
        surfaces as "model unavailable" rather than a wire error.
        """
        rows = [{"modelId": "gpt-5.2[high]", "name": "GPT 5.2 (high)"}]
        models = agents_handler._advertised_alt_backend_models(_request([_Provider(rows)]))
        assert models == [
            {
                "model_name": "gpt-5.2[high]",
                "display_name": "GPT 5.2 (high)",
                "description": "",
            }
        ]

    def test_newest_session_wins(self) -> None:
        """An older session may predate a backend switch."""
        old = _Provider([{"modelId": "old-model"}])
        new = _Provider([{"modelId": "new-model"}])
        models = agents_handler._advertised_alt_backend_models(_request([old, new]))
        assert [m["model_name"] for m in models] == ["new-model"]

    def test_lists_are_not_merged_across_sessions(self) -> None:
        """Merging two namespaces would offer ids the active backend rejects."""
        a = _Provider([{"modelId": "a"}])
        b = _Provider([{"modelId": "b"}])
        models = agents_handler._advertised_alt_backend_models(_request([a, b]))
        assert len(models) == 1

    def test_an_empty_advertisement_falls_through_to_an_older_session(self) -> None:
        empty = _Provider([])
        older = _Provider([{"modelId": "found"}])
        models = agents_handler._advertised_alt_backend_models(_request([older, empty]))
        assert [m["model_name"] for m in models] == ["found"]

    def test_a_nested_client_is_not_probed_for_models(self) -> None:
        rows = [{"modelId": "nested"}]
        models = agents_handler._advertised_alt_backend_models(_request([_NestedProvider(rows)]))
        assert models == []

    def test_display_name_defaults_to_the_id(self) -> None:
        models = agents_handler._advertised_alt_backend_models(
            _request([_Provider([{"modelId": "bare"}])])
        )
        assert models[0]["display_name"] == "bare"

    def test_rows_without_an_id_are_dropped(self) -> None:
        rows = [{"name": "no id"}, {"modelId": ""}, {"modelId": "keep"}]
        models = agents_handler._advertised_alt_backend_models(_request([_Provider(rows)]))
        assert [m["model_name"] for m in models] == ["keep"]

    def test_no_sessions_yields_an_empty_list(self) -> None:
        assert agents_handler._advertised_alt_backend_models(_request([])) == []

    def test_a_provider_that_raises_is_skipped(self) -> None:
        class _Boom:
            def available_models(self) -> list[dict[str, str]]:
                raise RuntimeError("nope")

        good = _Provider([{"modelId": "ok"}])
        models = agents_handler._advertised_alt_backend_models(_request([good, _Boom()]))
        assert [m["model_name"] for m in models] == ["ok"]

    def test_a_missing_state_does_not_raise(self) -> None:
        request = MagicMock()
        request.app = {}
        assert agents_handler._advertised_alt_backend_models(request) == []

    def test_a_live_kiro_session_is_not_offered_as_codex(self) -> None:
        """A still-open kiro chat must not stamp its catalog as Codex.

        Newest-first without a backend filter did exactly that after the
        operator switched the default harness: the live kiro session was the
        newest non-empty advertisement, so /api/models returned kiro ids
        under ``backend: "codex"``.
        """
        kiro = _Provider([{"modelId": "gpt-5.6-sol"}], backend="")
        models = agents_handler._advertised_alt_backend_models(
            _request([kiro]), backend=ACP_BACKEND_CODEX
        )
        assert models == []

    def test_newest_matching_backend_wins_over_a_newer_other_harness(self) -> None:
        old_codex = _Provider([{"modelId": "gpt-5.2[high]"}], backend=ACP_BACKEND_CODEX)
        new_kiro = _Provider([{"modelId": "claude-opus-4.8"}], backend="")
        models = agents_handler._advertised_alt_backend_models(
            _request([old_codex, new_kiro]), backend=ACP_BACKEND_CODEX
        )
        assert [m["model_name"] for m in models] == ["gpt-5.2[high]"]

    def test_unfiltered_still_returns_the_newest_non_empty_list(self) -> None:
        """No backend argument keeps the original newest-first walk."""
        old = _Provider([{"modelId": "old-model"}], backend=ACP_BACKEND_CODEX)
        new = _Provider([{"modelId": "new-model"}], backend="")
        models = agents_handler._advertised_alt_backend_models(_request([old, new]))
        assert [m["model_name"] for m in models] == ["new-model"]


class TestBackendReader:
    def test_fails_closed_to_kiro(self) -> None:
        assert agents_handler._configured_acp_backend() in ("", "codex", "claude", "kas")


class TestEndpointContract:
    def test_the_degraded_code_is_machine_readable(self) -> None:
        """The client must tell an unfixable-by-retry refusal from a timeout.

        Asserted against the handler source because reaching the branch needs a
        full app fixture; the string is the contract the frontend keys on.
        """
        import inspect

        source = inspect.getsource(agents_handler.api_models)
        assert "acp_backend_models_unavailable" in source
        assert "503" in source

    def test_the_backend_branch_precedes_the_kiro_spawn(self) -> None:
        """Ordering is the point: the spawn must not happen at all."""
        import inspect

        source = inspect.getsource(agents_handler.api_models)
        branch_at = source.index("_advertised_alt_backend_models")
        spawn_at = source.index("--list-models")
        assert branch_at < spawn_at

    def test_the_auto_capability_is_reported_on_success_and_on_refusal(self) -> None:
        """The picker cannot infer ``auto`` from a model list it does not have.

        The degraded 503 IS the steady state of an adapter with no live session,
        so a flag sent only on success would be absent exactly when the picker has
        to decide whether it may synthesize an Auto row. Asserted against the
        source for the same reason as the sibling tests: reaching either branch
        needs a full app fixture.

        Counted as the whole assignment rather than the bare word: that pins both
        halves at once -- the key ships twice, and each value is READ from the
        membership set instead of restated as a literal -- and prose explaining
        either cannot make the count drift.
        """
        import inspect

        source = inspect.getsource(agents_handler.api_models)
        assert source.count('"serves_auto": _alt_backend in ACP_BACKENDS_AUTO_MODEL') == 2


class TestAutoModelMembership:
    """``auto`` is a kiro-namespace id, so membership is opt-in (H6)."""

    def test_the_kiro_agent_family_serves_auto(self) -> None:
        assert ACP_BACKEND_KIRO in ACP_BACKENDS_AUTO_MODEL
        assert ACP_BACKEND_KAS in ACP_BACKENDS_AUTO_MODEL

    def test_spec_adapters_do_not(self) -> None:
        """claude advertises ``default``; codex advertises ``openai.*`` ids.

        Neither has an ``auto`` row, and offering one renders a sole option that
        is rejected at the wire.
        """
        assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_AUTO_MODEL
        assert ACP_BACKEND_CODEX not in ACP_BACKENDS_AUTO_MODEL
        assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_AUTO_MODEL

    def test_membership_is_a_deliberate_subset_of_selectable(self) -> None:
        """A backend nobody can select cannot claim a capability."""
        assert ACP_BACKENDS_AUTO_MODEL <= ACP_BACKENDS_SELECTABLE

    def test_a_new_backend_does_not_inherit_the_capability(self) -> None:
        """The whole point of opt-in membership.

        An unknown registry adapter must not acquire ``auto`` by being absent
        from some negation elsewhere.
        """
        assert "some-future-adapter" not in ACP_BACKENDS_AUTO_MODEL


class TestProviderBackend:
    def test_reads_the_provider_attribute(self) -> None:
        assert agents_handler._provider_backend(_Provider([], backend=ACP_BACKEND_CODEX)) == (
            ACP_BACKEND_CODEX
        )

    def test_does_not_probe_a_nested_client(self) -> None:
        nested = _NestedProvider([{"modelId": "x"}])
        nested.client._backend = ACP_BACKEND_CLAUDE
        assert agents_handler._provider_backend(nested) == ""

    def test_absence_is_unknown(self) -> None:
        assert agents_handler._provider_backend(None) is None


class TestModelsListScope:
    def test_a_live_slot_uses_that_session_not_the_configured_default(
        self, monkeypatch: Any
    ) -> None:
        """An open kiro chat must keep listing kiro models after a Codex save."""
        monkeypatch.setattr(agents_handler, "_configured_acp_backend", lambda: ACP_BACKEND_CODEX)
        live = _Provider([{"modelId": "gpt-5.6-sol"}], backend="")
        sessions = MagicMock()
        sessions.get_provider = MagicMock(return_value=live)
        request = MagicMock()
        request.query = {"slot": "chat-1"}
        request.app = {"state": MagicMock(sessions=sessions)}
        backend, provider = agents_handler._models_list_scope(request)
        assert backend == ""
        assert provider is live

    def test_a_live_adapter_slot_is_not_listed_as_kiro(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(agents_handler, "_configured_acp_backend", lambda: "")
        live = _Provider([{"modelId": "gpt-5.2[high]"}], backend=ACP_BACKEND_CODEX)
        sessions = MagicMock()
        sessions.get_provider = MagicMock(return_value=live)
        request = MagicMock()
        request.query = {"slot": "chat-2"}
        request.app = {"state": MagicMock(sessions=sessions)}
        backend, provider = agents_handler._models_list_scope(request)
        assert backend == ACP_BACKEND_CODEX
        assert provider is live

    def test_a_slot_without_a_live_provider_falls_back_to_config(self, monkeypatch: Any) -> None:
        """A new tab has not spawned yet; list what the next session will run."""
        monkeypatch.setattr(agents_handler, "_configured_acp_backend", lambda: ACP_BACKEND_CODEX)
        sessions = MagicMock()
        sessions.get_provider = MagicMock(return_value=None)
        request = MagicMock()
        request.query = {"slot": "chat-new"}
        request.app = {"state": MagicMock(sessions=sessions)}
        backend, provider = agents_handler._models_list_scope(request)
        assert backend == ACP_BACKEND_CODEX
        assert provider is None

    def test_no_slot_uses_the_configured_backend(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(agents_handler, "_configured_acp_backend", lambda: ACP_BACKEND_CLAUDE)
        request = MagicMock()
        request.query = {}
        request.app = {}
        backend, provider = agents_handler._models_list_scope(request)
        assert backend == ACP_BACKEND_CLAUDE
        assert provider is None
