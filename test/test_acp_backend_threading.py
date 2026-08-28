"""Tests for capability-driven ACP backend threading through the factory.

Phase 2 of the pluggable-backend work: the provider factory and the knowledge
pool must decide what to send a backend from its declared capabilities, not from
"is the backend id empty". These tests pin the default (kiro) path byte-for-byte
alongside the new behaviour, because the whole series is only safe if an
un-opted-in installation is untouched.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.acp import backends
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_SELECTABLE,
)
from kiro_crew.config.loader import KiroCrewConfig, build_provider_factory
from kiro_crew.knowledge.llm_pool import _get_acp_backend


class _Captured:
    """Records the kwargs the factory would hand AcpProvider."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    """Intercept AcpProvider construction without spawning anything."""
    box = _Captured()

    import kiro_crew.providers.acp as providers_acp

    class _FakeProvider:
        def __init__(self, **kwargs: Any) -> None:
            box.kwargs = kwargs

    monkeypatch.setattr(providers_acp, "AcpProvider", _FakeProvider)
    monkeypatch.setattr(providers_acp, "SpecAdapterAcpProvider", _FakeProvider)
    return box


def _build(cfg: KiroCrewConfig, captured: _Captured) -> dict[str, Any]:
    factory = build_provider_factory(cfg)
    factory(session_key="s1", agent="kirocrew")
    return captured.kwargs


def test_default_backend_threads_tool_search_through(captured: _Captured) -> None:
    """The kiro path is unchanged: the configured toggle reaches the provider."""
    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = ""
    cfg.agent.tool_search = True

    kwargs = _build(cfg, captured)

    assert kwargs["acp_backend"] == ""
    assert kwargs["tool_search"] is True


def test_default_backend_honours_a_disabled_tool_search(captured: _Captured) -> None:
    """False must stay False, not collapse into None.

    False writes an explicit disable into the cli.json overlay, which is what
    makes the Kiro Crew toggle authoritative over the user's global settings.
    Collapsing it to None would silently defer to whatever they had.
    """
    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = ""
    cfg.agent.tool_search = False

    kwargs = _build(cfg, captured)

    assert kwargs["tool_search"] is False


def test_backend_without_tool_search_gets_none_not_false(captured: _Captured) -> None:
    """A backend that reads no cli.json must get "do not write", not "disable".

    None and False are different requests: False writes an explicit disable to a
    file the backend never reads, which is a pointless write into the work dir.
    """
    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX
    cfg.agent.tool_search = True

    kwargs = _build(cfg, captured)

    assert not backends.supports(ACP_BACKEND_CODEX, backends.CAP_TOOL_SEARCH)
    assert kwargs["tool_search"] is None


def test_registry_model_translation_skipped_when_ids_are_foreign(
    captured: _Captured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend whose ids are not registry keys receives the value untouched.

    Translating would either fold the id onto an unrelated kiro model or drop
    it, and the failure would surface as a rejected set_model at session start
    rather than here.
    """
    from kiro_crew import model_registry

    calls: list[str] = []

    def _spy(value: str) -> str:
        calls.append(value)
        return "translated"

    monkeypatch.setattr(model_registry, "to_acp_id", _spy)

    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX

    _build(cfg, captured)

    assert calls == [], "to_acp_id must not run for a backend without registry ids"


def test_registry_model_translation_still_runs_for_kiro(
    captured: _Captured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default path keeps its translation boundary."""
    from kiro_crew import model_registry

    calls: list[str] = []
    real = model_registry.to_acp_id

    def _spy(value: str) -> str:
        calls.append(value)
        return real(value)

    monkeypatch.setattr(model_registry, "to_acp_id", _spy)

    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = ""

    _build(cfg, captured)

    assert calls, "to_acp_id must still run on the kiro backend"


def test_unknown_backend_fails_at_factory_build_not_session_start() -> None:
    """An id with no descriptor must be refused while a human is still looking.

    Deferring it to session start turns a config typo into a failed first
    message, which is the failure mode _normalize_acp_backend already avoids for
    persisted values.
    """
    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = "not-a-backend"

    with pytest.raises(backends.UnknownAcpBackend):
        build_provider_factory(cfg)


class TestPoolBackendReader:
    """The knowledge pool builds its own workers and never sees the factory."""

    def test_absent_means_kiro(self) -> None:
        assert _get_acp_backend({}) == ""

    def test_selectable_value_is_honoured(self) -> None:
        for backend in ACP_BACKENDS_SELECTABLE:
            assert _get_acp_backend({"agent": {"acp_backend": backend}}) == backend

    def test_known_but_unselectable_degrades(self, monkeypatch) -> None:
        """The pool must not end up on a backend the chat path refused.

        Exercised against a CONTROLLED set rather than whichever backend is
        withheld today. This test named KAS, then codex, then derived the withheld
        one from the sets — and each time a backend graduated the test needed
        another edit, until every known backend became selectable and there was no
        instance left to assert on. The mechanism is what matters and it must keep
        working for the registry adapters that will be described but not
        selectable, so the set is pinned here instead of observed.
        """
        import kiro_crew.acp.backends as acp_backends

        monkeypatch.setattr(acp_backends, "selectable_ids", lambda: frozenset({ACP_BACKEND_KIRO}))
        for backend in ("kas", "codex", "claude"):
            assert _get_acp_backend({"agent": {"acp_backend": backend}}) == "", backend

    def test_dynamic_registry_backend_is_honoured(self, monkeypatch) -> None:
        import kiro_crew.acp.backends as acp_backends

        monkeypatch.setattr(
            acp_backends,
            "selectable_ids",
            lambda: frozenset({ACP_BACKEND_KIRO, "registry.example"}),
        )
        monkeypatch.setattr(
            acp_backends,
            "canonical_backend_id",
            lambda value: "registry.example" if value == "registry-alias" else value,
        )

        assert _get_acp_backend({"agent": {"acp_backend": "registry-alias"}}) == "registry.example"

    def test_a_selectable_backend_is_honoured(self) -> None:
        """The mirror case, so the test above cannot pass by degrading everything."""
        assert _get_acp_backend({"agent": {"acp_backend": ACP_BACKEND_KAS}}) == ACP_BACKEND_KAS

    def test_unknown_degrades(self) -> None:
        assert _get_acp_backend({"agent": {"acp_backend": "nonsense"}}) == ""

    def test_non_string_degrades(self) -> None:
        assert _get_acp_backend({"agent": {"acp_backend": 7}}) == ""
