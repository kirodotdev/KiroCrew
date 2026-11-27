"""LTS: the ``embedding_model_use`` term decides which embedding sources memory may use.

RFC ``docs/request-for-change/rfc-crew-lts-edition.md`` D13. The term's resource is the
source (``bundled``, ``custom``, ``ollama``), translated into ``memory.embedding_sources``.
Each case is asked where Crew builds or applies a backend: the factory
``default_embedding_backend`` (config and env model paths, the Ollama provider), the
cached singleton ``get_shared_embedder``, the dashboard apply route and the status route.
A non-loopback Ollama host also needs ``network.egress``. The ``oss`` edition never reads
the policy on this path.

The local policy tiers and the host profile are in
``test_lts_batch1_ceiling_outranks_local.py``.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest
from handlers_memory_coverage_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _body,
    _fake_sel,
    _make_request,
    _make_state,
    _reset_setup_status,
)

import kiro_crew.dashboard.handlers.memory as mem_mod
import kiro_crew.embeddings as emb
from kiro_crew import edition_locks
from kiro_crew import embedding_source_governance as gov
from kiro_crew.embeddings_ollama import OllamaHttpEmbedder
from kiro_crew.embeddings_settings import _MODEL_ID, _REJECTED_MODEL_SENTINEL
from kiro_crew.platform import enterprise_policy_translate as t
from kiro_crew.platform import governance
from kiro_crew.platform.governance_parts import tiers

_TERM = "embedding_model_use"
_LOCAL = "http://127.0.0.1:11434"
_REMOTE = "https://ollama.corp.example"


def _rule(effect: str, match: str = "", capability: str = _TERM) -> str:
    return (
        f"{{capability: {capability}, effect: {effect}"
        + (f", match: {match}" if match else "")
        + "}"
    )


def _central(*rules: str):
    """The ceiling an LTS boot installs for these remote rules, baseline filled in."""
    body = "".join(f"  - {r}\n" for r in rules) if rules else "  []\n"
    parsed = governance.parse_policy(t.translate("rules:\n" + body))
    return tiers._with_lts_baseline(dataclasses.replace(parsed, tier=governance.TIER_CENTRAL))


@pytest.fixture(name="lts")
def lts_fixture(_floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


@pytest.fixture(name="policy")
def policy_fixture(lts, monkeypatch):
    """Install the remote rules as the process ceiling; returns the setter."""
    from kiro_crew.platform import context as pc

    def install(*rules: str) -> None:
        ceiling = _central(*rules)
        monkeypatch.setattr(pc, "current_context", lambda: SimpleNamespace(governance=ceiling))

    install()
    return install


@pytest.fixture(name="memory")
def memory_fixture(monkeypatch, _floor_monkeypatch):
    """The raw ``memory`` config section the factory reads; mutate it in place."""
    section: dict = {}
    _floor_monkeypatch.delenv("KIROCREW_EMBED_MODEL_PATH", raising=False)
    monkeypatch.setattr(emb, "_read_memory_config", lambda: section)
    emb.reset_shared_embedder()
    yield section
    emb.reset_shared_embedder()


def _ollama(memory: dict, url: str = _LOCAL) -> None:
    memory.update(
        embedding_provider="ollama_http",
        ollama_model="nomic-embed-text",
        ollama_url=url,
        ollama_allow_remote=True,
    )


def _refused_local(backend: Any) -> bool:
    return backend.model_path.name == _REJECTED_MODEL_SENTINEL


def _allowed() -> dict:
    return {s: gov.source_permitted(s) for s in gov.SOURCES}


# --- the term, through the translator and the evaluator ----------------------------


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ((), {"bundled"}),
        ((_rule("allow"),), {"bundled", "custom", "ollama"}),
        ((_rule("allow", "['*']"),), {"bundled", "custom", "ollama"}),
        ((_rule("allow", "[ollama]"),), {"ollama"}),
        ((_rule("allow", "[bundled, custom]"),), {"bundled", "custom"}),
        ((_rule("allow", "['c*']"),), {"custom"}),
        ((_rule("allow", "[nomic-embed-text]"),), set()),
        ((_rule("allow"), _rule("deny", "[ollama]")), {"bundled", "custom"}),
        ((_rule("deny", "[ollama]"),), {"bundled"}),
        ((_rule("deny"),), set()),
        ((_rule("deny", capability="all"),), set()),
        # An allow on a meta-capability reaches only the KAS table's terms.
        ((_rule("allow", capability="all"),), {"bundled"}),
    ],
)
def test_the_rule_picks_the_sources(policy, rules, expected):
    policy(*rules)
    assert {s for s, ok in _allowed().items() if ok} == expected


def test_an_unreadable_ceiling_refuses_every_source(lts, monkeypatch):
    from kiro_crew.platform import context as pc

    def broken():
        raise RuntimeError("ceiling unreadable")

    monkeypatch.setattr(pc, "current_context", broken)
    assert _allowed() == {s: False for s in gov.SOURCES}


# --- the factory: every configured source --------------------------------------------


class TestTheFactory:
    def test_no_rule_builds_the_bundled_model(self, policy, memory):
        backend = emb.default_embedding_backend()
        assert not _refused_local(backend)
        assert backend.model_id == _MODEL_ID

    def test_a_refused_bundled_model_keeps_its_identity_and_loads_nothing(self, policy, memory):
        policy(_rule("allow", "[ollama]"))
        backend = emb.default_embedding_backend()
        assert _refused_local(backend)
        assert backend.model_id == _MODEL_ID
        assert backend.is_ready() is False

    @pytest.mark.parametrize("where", ["config", "env"])
    def test_a_custom_path_is_refused_with_no_rule(
        self, policy, memory, tmp_path, monkeypatch, where
    ):
        model = tmp_path / "m.gguf"
        model.write_bytes(b"\0" * 64)
        if where == "env":
            monkeypatch.setenv("KIROCREW_EMBED_MODEL_PATH", str(model))
        else:
            memory["embed_model_path"] = str(model)
        spec = emb.resolve_custom_model()
        assert spec is not None and spec.error_code == gov.LOCKED_CODE
        backend = emb.default_embedding_backend()
        # Keyword search, never the bundled model in its place.
        assert _refused_local(backend)
        assert backend.model_id != _MODEL_ID

    def test_an_allowed_custom_path_is_resolved_as_before(self, policy, memory, tmp_path):
        policy(_rule("allow", "[custom]"))
        memory["embed_model_path"] = str(tmp_path / "missing.gguf")
        spec = emb.resolve_custom_model()
        assert spec is not None and spec.error_code not in ("", gov.LOCKED_CODE)

    def test_ollama_is_refused_with_no_rule(self, policy, memory):
        _ollama(memory)
        backend = emb.default_embedding_backend()
        assert isinstance(backend, OllamaHttpEmbedder)
        assert backend.is_ready() is False
        assert emb.resolve_ollama_settings(memory).error == gov.refused_reason(gov.OLLAMA)

    def test_a_loopback_ollama_is_not_asked_about_egress(self, policy, memory, monkeypatch):
        policy(_rule("allow", "[ollama]"))
        _ollama(memory)

        def no_egress(host: str) -> bool:
            raise AssertionError(f"egress asked about {host}")

        monkeypatch.setattr(gov, "egress_permitted", no_egress)
        assert emb.resolve_ollama_settings(memory).error == ""
        backend = emb.default_embedding_backend()
        assert backend._policy_allowed is True

    def test_a_remote_ollama_needs_network_egress(self, policy, memory):
        policy(_rule("allow", "[ollama]"))
        _ollama(memory, _REMOTE)
        assert "network egress" in emb.resolve_ollama_settings(memory).error
        policy(
            _rule("allow", "[ollama]"), _rule("allow", "[ollama.corp.example]", "network_egress")
        )
        assert emb.resolve_ollama_settings(memory).error == ""

    def test_local_allow_remote_does_not_widen_egress(self, policy, memory):
        policy(_rule("allow"))
        _ollama(memory, _REMOTE)
        assert memory["ollama_allow_remote"] is True
        assert "network egress" in emb.resolve_ollama_settings(memory).error


# --- the cached singleton: a policy change takes effect without a restart -------------


class TestTheCachedEmbedder:
    def test_a_deny_retires_the_running_source_and_an_allow_brings_it_back(self, policy, memory):
        policy(_rule("allow", "[ollama]"))
        _ollama(memory)
        first = emb.get_shared_embedder()
        assert first._policy_allowed is True
        assert emb.get_shared_embedder() is first

        policy(_rule("allow", "[bundled]"))
        refused = emb.get_shared_embedder()
        assert refused is not first
        assert first._closed is True
        assert refused.is_ready() is False
        assert emb.get_shared_embedder() is refused

        policy(_rule("allow", "[ollama]"))
        again = emb.get_shared_embedder()
        assert again is not refused and again._policy_allowed is True

    def test_an_egress_deny_retires_a_remote_server(self, policy, memory):
        egress = _rule("allow", "[ollama.corp.example]", "network_egress")
        policy(_rule("allow", "[ollama]"), egress)
        _ollama(memory, _REMOTE)
        first = emb.get_shared_embedder()
        assert first._policy_allowed is True
        policy(_rule("allow", "[ollama]"))
        assert emb.get_shared_embedder() is not first

    @pytest.mark.parametrize("build", ["bundled", "custom"])
    def test_a_dashboard_applied_model_is_rechecked_once_it_serves(
        self, policy, memory, tmp_path, build
    ):
        policy(_rule("allow", "[bundled, custom]"))
        if build == "bundled":
            candidate = emb.build_gated_bundled()
        else:
            model = tmp_path / "m.gguf"
            model.write_bytes(b"\0" * 64)
            candidate = emb.build_gated_candidate(model)
        emb.install_shared_embedder(candidate)
        policy(_rule("allow", "[ollama]"))
        # Gated: it serves nothing, and the apply in flight owns it.
        assert emb.get_shared_embedder() is candidate
        assert emb.activate_shared_embedder() is True
        assert emb.get_shared_embedder() is not candidate

    def test_the_tag_is_the_answer_the_build_used(self, policy, memory, monkeypatch):
        """A policy change between the build's check and its tag is seen next read."""
        policy(_rule("allow", "[ollama]"))
        _ollama(memory)
        real = emb.build_ollama_backend

        def build_then_revoke(section):
            backend = real(section)
            policy(_rule("allow", "[bundled]"))
            return backend

        monkeypatch.setattr(emb, "build_ollama_backend", build_then_revoke)
        backend = emb.default_embedding_backend()
        assert backend._policy_allowed is True
        emb.install_shared_embedder(backend)
        monkeypatch.setattr(emb, "build_ollama_backend", real)
        assert emb.get_shared_embedder() is not backend

    def test_a_retired_server_sends_no_more_chunks(self, policy, memory):
        policy(_rule("allow", "[ollama]"))
        _ollama(memory)
        backend = emb.default_embedding_backend()
        sent: list = []

        def request(chunk):
            sent.append(chunk)
            backend.close()
            return [[0.0] * backend.dim for _ in chunk]

        backend._request = request
        backend._failing = False
        assert backend.embed_batch([f"t{i}" for i in range(20)]) is None
        assert len(sent) == 1

    def test_an_unparsable_ollama_url_falls_back_without_raising(self, policy, memory):
        policy(_rule("allow", "[ollama]"))
        _ollama(memory, "https://[::1")
        backend = emb.get_shared_embedder()
        assert backend.is_ready() is False


def test_the_sentinel_path_never_loads_even_when_a_file_is_there(policy, memory, tmp_path):
    from kiro_crew import embeddings_llama

    policy(_rule("allow", "[ollama]"))
    backend = emb.default_embedding_backend()
    assert _refused_local(backend)
    backend._model_path = tmp_path / _REJECTED_MODEL_SENTINEL
    backend._model_path.write_bytes(b"\0" * (1 << 20))
    started: list = []
    original = embeddings_llama.threading.Thread

    def spy(*args: Any, **kwargs: Any):
        started.append(kwargs.get("name"))
        return original(*args, **kwargs)

    embeddings_llama.threading.Thread = spy  # type: ignore[assignment]
    try:
        backend._kick_background_load()
    finally:
        embeddings_llama.threading.Thread = original  # type: ignore[assignment]
    assert "kc-embed-load" not in started


# --- the dashboard routes --------------------------------------------------------------


class TestTheDashboard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["", "/models/m.gguf"])
    async def test_the_apply_route_refuses_a_refused_source_with_409(self, policy, memory, path):
        policy(_rule("allow", "[ollama]"))
        req = _make_request(_make_state(), method="POST", json_body={"path": path})
        resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 409
        assert _body(resp)["code"] == "locked_by_policy"

    @pytest.mark.asyncio
    async def test_the_apply_route_refuses_a_custom_file_with_no_rule(self, policy, memory):
        body = {"path": "/models/m.gguf", "validate_only": True}
        req = _make_request(_make_state(), method="POST", json_body=body)
        resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 409
        assert _body(resp)["error"] == gov.refused_reason(gov.CUSTOM)

    @pytest.mark.asyncio
    async def test_the_status_route_names_a_refused_bundled_model(self, policy, memory):
        policy(_rule("allow", "[ollama]"))
        req = _make_request(_make_state(), method="GET")
        body = _body(await mem_mod.api_memory_embedding_status(req))
        assert body["setup_error_code"] == "locked_by_policy"
        assert body["server_healthy"] is False


# --- oss: no policy read on the embedding path -------------------------------------


class TestOssNeverReadsThePolicy:
    @pytest.fixture(autouse=True)
    def _policy_raises(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr("kiro_crew.edition.EDITION", "oss")

        def boom(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("the embedding path read the policy on oss")

        _floor_monkeypatch.setattr(edition_locks, "policy_permits", boom)

    def test_every_source_builds_without_a_policy_read(self, memory, tmp_path):
        assert _allowed() == {s: True for s in gov.SOURCES}
        assert gov.egress_permitted("ollama.corp.example") is True
        assert not _refused_local(emb.default_embedding_backend())
        memory["embed_model_path"] = str(tmp_path / "missing.gguf")
        assert emb.resolve_custom_model().error_code != gov.LOCKED_CODE
        memory.clear()
        _ollama(memory, _REMOTE)
        assert emb.resolve_ollama_settings(memory).error == ""
        backend = emb.get_shared_embedder()
        assert not hasattr(backend, "_embedding_source")
        assert emb.get_shared_embedder() is backend

    @pytest.mark.asyncio
    async def test_the_apply_route_reads_no_policy(self, memory):
        body = {"path": "", "validate_only": True}
        req = _make_request(_make_state(), method="POST", json_body=body)
        resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 200
