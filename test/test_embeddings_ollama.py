"""The Ollama server embedding backend, its settings, and how the factory selects it.

No test opens a socket: every request goes through a fake opener that records it,
and readiness probes run through an injected starter instead of a thread.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from dataclasses import fields
from typing import Any, Callable

import pytest

import kiro_crew.embeddings as emb
from kiro_crew import embeddings_download, embeddings_ollama
from kiro_crew.config import memory_sections
from kiro_crew.config.memory_sections import MemoryConfig, _coerce_embedding_provider
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.embeddings_llama import LlamaCppEmbedder
from kiro_crew.embeddings_ollama import (
    OLLAMA_HTTP_PROVIDER,
    OllamaHttpEmbedder,
    OllamaSettings,
    _build_opener,
    _RefuseRedirects,
    _SigV4Signer,
    resolve_ollama_settings,
)
from kiro_crew.embeddings_settings import _MAX_EMBED_CHARS

_DIM = 4
_MODEL = "nomic-embed-text"


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _FakeOpener:
    """Answers ``/api/embed`` with one vector per input and ``/api/tags`` with *tags*."""

    def __init__(
        self,
        *,
        tags: "list[str] | None" = None,
        embed: "Callable[[dict], Any] | None" = None,
        error: "Exception | None" = None,
    ) -> None:
        self.tags = [_MODEL] if tags is None else tags
        self.embed = embed or (lambda body: {"embeddings": [[0.5] * _DIM for _ in body["input"]]})
        self.error = error
        self.requests: list[tuple[str, dict | None, float]] = []
        self.headers: list[dict[str, str]] = []

    def open(self, request: urllib.request.Request, timeout: float) -> _FakeResponse:
        url = request.full_url
        body = json.loads(request.data) if request.data else None
        self.requests.append((url, body, timeout))
        self.headers.append({k.lower(): v for k, v in request.header_items()})
        if self.error is not None:
            raise self.error
        if url.endswith("/api/tags"):
            payload: Any = {"models": [{"name": name} for name in self.tags]}
        else:
            payload = self.embed(body or {})
        return _FakeResponse(json.dumps(payload).encode())

    @property
    def embed_requests(self) -> list[dict]:
        return [body for url, body, _ in self.requests if url.endswith("/api/embed") and body]


class _CapturedProbes:
    """A probe starter that records each probe instead of running it on a thread."""

    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def __call__(self, target: Callable[[], None]) -> None:
        self.pending.append(target)

    def run_all(self) -> None:
        while self.pending:
            self.pending.pop(0)()


def _settings(environ: "dict[str, str] | None" = None, **overrides: Any) -> OllamaSettings:
    memory = {"ollama_model": _MODEL, "embedding_dim": _DIM, **overrides}
    return resolve_ollama_settings(memory, environ={} if environ is None else environ)


def _embedder(
    opener: _FakeOpener,
    probes: "_CapturedProbes | None" = None,
    signer: "_SigV4Signer | None" = None,
    **overrides: Any,
) -> OllamaHttpEmbedder:
    return OllamaHttpEmbedder(
        _settings(**overrides),
        opener=opener,
        start_probe=probes or _CapturedProbes(),
        signer=signer,
    )


# -- settings -----------------------------------------------------------------


class TestSettings:
    def test_defaults_need_only_a_model(self) -> None:
        settings = resolve_ollama_settings({})
        assert settings.url == "http://127.0.0.1:11434"
        assert settings.timeout_secs == 30.0
        assert settings.allow_remote is False
        assert settings.error == "memory.ollama_model is empty"
        assert resolve_ollama_settings({"ollama_model": _MODEL}).error == ""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:11434",
            "http://localhost:11434/",
            "http://[::1]:8080",
            "http://127.1.2.3",
        ],
    )
    def test_a_loopback_url_needs_no_opt_in(self, url: str) -> None:
        settings = _settings(ollama_url=url)
        assert settings.error == ""
        assert not settings.url.endswith("/")

    def test_a_path_prefix_is_kept_for_a_proxy(self) -> None:
        assert _settings(ollama_url="http://127.0.0.1:9000/ollama/").url == (
            "http://127.0.0.1:9000/ollama"
        )

    def test_a_remote_host_is_refused_without_the_opt_in(self) -> None:
        assert "ollama_allow_remote" in _settings(ollama_url="https://gpu.example:11434").error

    def test_a_remote_host_must_use_https_even_with_the_opt_in(self) -> None:
        settings = _settings(ollama_url="http://10.0.0.5:11434", ollama_allow_remote=True)
        assert "https://" in settings.error

    def test_a_remote_https_host_is_accepted_with_the_opt_in(self) -> None:
        settings = _settings(ollama_url="https://gpu.example:11434", ollama_allow_remote=True)
        assert settings.error == ""

    def test_only_a_real_true_opts_in(self) -> None:
        settings = _settings(ollama_url="https://gpu.example", ollama_allow_remote="true")
        assert settings.allow_remote is False
        assert settings.error

    @pytest.mark.parametrize(
        ("url", "fragment"),
        [
            ("ftp://127.0.0.1:11434", "http://"),
            ("http://user:pw@127.0.0.1:11434", "credentials"),
            ("http://127.0.0.1:11434?x=1", "query"),
            ("http://127.0.0.1:notaport", "not a valid URL"),
            ("http://", "no host"),
        ],
    )
    def test_an_unusable_url_is_refused(self, url: str, fragment: str) -> None:
        assert fragment in _settings(ollama_url=url).error

    @pytest.mark.parametrize(("raw", "expected"), [(0.1, 1.0), (9999, 600.0), (45, 45.0)])
    def test_the_timeout_is_clamped(self, raw: float, expected: float) -> None:
        assert _settings(ollama_timeout_secs=raw).timeout_secs == expected

    @pytest.mark.parametrize("raw", ["60", True, None, float("nan")])
    def test_an_unreadable_timeout_is_the_default(self, raw: object) -> None:
        assert _settings(ollama_timeout_secs=raw).timeout_secs == 30.0

    @pytest.mark.parametrize("raw", [0, -1, True, "768", 70_000])
    def test_an_unreadable_width_is_the_default(self, raw: object) -> None:
        assert resolve_ollama_settings({"ollama_model": _MODEL, "embedding_dim": raw}).dim == 1024

    @pytest.mark.parametrize("model", ["bad\nname", "x" * 257])
    def test_an_invalid_model_name_is_refused(self, model: str) -> None:
        assert _settings(ollama_model=model).error


class TestConfigAgreement:
    """The config schema restates the backend's constants; the two must not drift."""

    def test_the_provider_value_is_accepted_by_the_schema(self) -> None:
        assert OLLAMA_HTTP_PROVIDER in memory_sections.EMBEDDING_PROVIDERS

    def test_defaults_and_bounds_match(self) -> None:
        assert memory_sections.OLLAMA_DEFAULT_URL == embeddings_ollama.DEFAULT_OLLAMA_URL
        assert (
            memory_sections.OLLAMA_DEFAULT_TIMEOUT_SECS
            == embeddings_ollama.DEFAULT_OLLAMA_TIMEOUT_SECS
        )
        assert memory_sections.OLLAMA_TIMEOUT_MIN_SECS == embeddings_ollama.OLLAMA_TIMEOUT_MIN_SECS
        assert memory_sections.OLLAMA_TIMEOUT_MAX_SECS == embeddings_ollama.OLLAMA_TIMEOUT_MAX_SECS
        assert memory_sections.OLLAMA_AUTH_SCHEMES == embeddings_ollama.OLLAMA_AUTH_SCHEMES
        assert (
            memory_sections.OLLAMA_DEFAULT_AWS_SERVICE
            == embeddings_ollama.DEFAULT_OLLAMA_AWS_SERVICE
        )

    def test_the_dataclass_defaults_resolve_to_the_backend_defaults(self) -> None:
        defaults = {f.name: f.default for f in fields(MemoryConfig) if f.name.startswith("ollama_")}
        settings = resolve_ollama_settings(defaults, environ={})
        assert settings.url == defaults["ollama_url"]
        assert settings.timeout_secs == defaults["ollama_timeout_secs"]
        assert settings.allow_remote is defaults["ollama_allow_remote"]
        assert settings.auth == defaults["ollama_auth"]
        assert settings.aws_region == defaults["ollama_aws_region"]
        assert settings.aws_profile == defaults["ollama_aws_profile"]
        aws = resolve_ollama_settings({**defaults, "ollama_auth": "aws_sigv4"}, environ={})
        assert aws.aws_service == defaults["ollama_aws_service"]

    def test_the_loader_reads_and_clamps_the_ollama_keys(self) -> None:
        cfg = _build_memory_config(
            {
                "embedding_provider": "ollama_http",
                "ollama_url": "http://127.0.0.1:9000",
                "ollama_model": _MODEL,
                "ollama_timeout_secs": 0.01,
                "ollama_allow_remote": "yes",
            }
        )
        assert cfg.embedding_provider == "ollama_http"
        assert cfg.ollama_url == "http://127.0.0.1:9000"
        assert cfg.ollama_model == _MODEL
        assert cfg.ollama_timeout_secs == 1.0
        assert cfg.ollama_allow_remote is False

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ollama_http", "ollama_http"),
            ("llama_cpp", "llama_cpp"),
            # The retired value never selects the server backend.
            ("ollama", "llama_cpp"),
            ("none", "llama_cpp"),
            ("unknown", "llama_cpp"),
        ],
    )
    def test_provider_coercion(self, raw: str, expected: str) -> None:
        assert _coerce_embedding_provider(raw) == expected


# -- embedding ----------------------------------------------------------------


class TestEmbed:
    def test_one_text_is_one_request_to_api_embed(self) -> None:
        opener = _FakeOpener()
        vector = _embedder(opener).embed("hello")
        assert vector == [0.5] * _DIM
        url, body, timeout = opener.requests[0]
        assert url == "http://127.0.0.1:11434/api/embed"
        assert body == {"model": _MODEL, "input": ["hello"], "truncate": True}
        assert timeout == 30.0

    def test_a_batch_is_split_and_kept_in_order(self) -> None:
        opener = _FakeOpener(
            embed=lambda body: {"embeddings": [[float(t[1:])] * _DIM for t in body["input"]]}
        )
        texts = [f"t{i}" for i in range(20)]
        vectors = _embedder(opener).embed_batch(texts)
        assert [len(b["input"]) for b in opener.embed_requests] == [8, 8, 4]
        assert vectors == [[float(i)] * _DIM for i in range(20)]

    def test_one_failed_batch_fails_the_call(self) -> None:
        calls = []

        def embed(body: dict) -> dict:
            calls.append(body)
            if len(calls) == 2:
                return {"error": "the input length exceeds the context length"}
            return {"embeddings": [[0.5] * _DIM for _ in body["input"]]}

        assert _embedder(_FakeOpener(embed=embed)).embed_batch(["x"] * 12) is None

    def test_long_input_is_clipped_like_the_in_process_backend(self) -> None:
        opener = _FakeOpener()
        _embedder(opener).embed("a" * (_MAX_EMBED_CHARS + 50))
        assert len(opener.embed_requests[0]["input"][0]) == _MAX_EMBED_CHARS

    @pytest.mark.parametrize("texts", [[], [""], ["  ", "\n"]])
    def test_blank_input_is_none_without_a_request(self, texts: list[str]) -> None:
        opener = _FakeOpener()
        assert _embedder(opener).embed_batch(texts) is None
        assert opener.requests == []

    @pytest.mark.parametrize(
        "payload",
        [
            {"embeddings": [[0.5] * _DIM, [0.5] * _DIM]},
            {"embeddings": [[0.5, "x", 0.5, 0.5]]},
            {"embeddings": [[0.5, True, 0.5, 0.5]]},
            {"embeddings": "nope"},
            ["not", "an", "object"],
        ],
    )
    def test_a_malformed_response_is_none(self, payload: object) -> None:
        assert _embedder(_FakeOpener(embed=lambda body: payload)).embed("x") is None

    def test_a_width_mismatch_is_none_and_named_once(self, caplog) -> None:
        embedder = _embedder(_FakeOpener(embed=lambda body: {"embeddings": [[0.5] * 768]}))
        with caplog.at_level(logging.ERROR, logger="kiro_crew.embeddings"):
            assert embedder.embed("x") is None
            assert embedder.embed("y") is None
        errors = [r for r in caplog.records if "memory.embedding_dim" in r.getMessage()]
        assert len(errors) == 1
        assert "set memory.embedding_dim to 768" in errors[0].getMessage()

    def test_an_oversized_response_is_none(self, monkeypatch) -> None:
        monkeypatch.setattr(embeddings_ollama, "_MAX_RESPONSE_BYTES", 16)
        assert _embedder(_FakeOpener()).embed("x") is None

    def test_an_unreachable_server_warns_once_per_outage(self, caplog) -> None:
        opener = _FakeOpener(error=urllib.error.URLError("connection refused"))
        embedder = _embedder(opener)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.embeddings"):
            assert embedder.embed("x") is None
            assert embedder.embed("y") is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "keyword search" in warnings[0].getMessage()

    def test_an_unusable_config_never_sends_text(self) -> None:
        opener = _FakeOpener()
        embedder = _embedder(opener, ollama_url="https://gpu.example")
        assert embedder.embed("secret") is None
        assert embedder.is_ready() is False
        embedder.prefetch()
        assert opener.requests == []

    def test_a_closed_backend_embeds_nothing(self) -> None:
        opener = _FakeOpener()
        embedder = _embedder(opener)
        embedder.close()
        embedder.close()
        assert embedder.embed("x") is None
        assert embedder.is_ready() is False
        assert opener.requests == []


# -- identity and readiness ----------------------------------------------------


class TestIdentityAndReadiness:
    def test_the_space_is_the_model_and_width_not_the_url(self) -> None:
        local = _embedder(_FakeOpener())
        moved = _embedder(_FakeOpener(), ollama_url="http://localhost:9999")
        assert local.model_id == moved.model_id == f"ollama:{_MODEL}"
        assert local.dim == _DIM
        assert emb.embedding_space_signature(local.model_id, local.dim) == (
            emb.embedding_space_signature(moved.model_id, moved.dim)
        )

    def test_is_ready_never_blocks_and_starts_one_probe(self) -> None:
        opener, probes = _FakeOpener(), _CapturedProbes()
        embedder = _embedder(opener, probes)
        assert embedder.is_ready() is False
        assert embedder.is_ready() is False
        assert len(probes.pending) == 1
        assert opener.requests == []
        probes.run_all()
        assert embedder.is_ready() is True
        assert opener.requests[0][0] == "http://127.0.0.1:11434/api/tags"

    @pytest.mark.parametrize(("tags", "ready"), [([f"{_MODEL}:latest"], True), (["other"], False)])
    def test_the_probe_requires_the_configured_model(self, tags: list[str], ready: bool) -> None:
        probes = _CapturedProbes()
        embedder = _embedder(_FakeOpener(tags=tags), probes)
        embedder.prefetch()
        probes.run_all()
        assert embedder.is_ready() is ready

    def test_a_successful_embed_marks_ready_without_a_probe(self) -> None:
        opener, probes = _FakeOpener(), _CapturedProbes()
        embedder = _embedder(opener, probes)
        embedder.embed("x")
        assert embedder.is_ready() is True
        assert probes.pending == []

    def test_a_failed_embed_marks_not_ready(self) -> None:
        opener = _FakeOpener()
        embedder = _embedder(opener)
        embedder.embed("x")
        opener.error = urllib.error.URLError("gone")
        embedder.embed("y")
        assert embedder.is_ready() is False

    def test_wait_ready_probes_synchronously(self) -> None:
        embedder = _embedder(_FakeOpener())
        assert embedder.wait_ready(timeout=1.0) is True


# -- transport ----------------------------------------------------------------


class TestTransport:
    def test_redirects_are_refused(self) -> None:
        req = urllib.request.Request("http://127.0.0.1:11434/api/embed", data=b"{}")
        assert (
            _RefuseRedirects().redirect_request(req, io.BytesIO(), 307, "", None, "http://x")
            is None
        )

    def test_a_loopback_url_ignores_proxy_variables(self, monkeypatch) -> None:
        monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
        loopback = _build_opener("http://127.0.0.1:11434")
        remote = _build_opener("https://gpu.example")
        # An empty ProxyHandler opens no scheme, so the opener keeps none and
        # build_opener does not add the environment-reading default.
        assert not any(isinstance(h, urllib.request.ProxyHandler) for h in loopback.handlers)
        assert any(
            h.proxies.get("http") == "http://proxy.example:3128"
            for h in remote.handlers
            if isinstance(h, urllib.request.ProxyHandler)
        )


# -- SigV4 --------------------------------------------------------------------

_GATEWAY = "https://abc123.execute-api.ap-southeast-2.amazonaws.com/ollama"


def _sigv4(**overrides: Any) -> dict[str, Any]:
    return {
        "ollama_url": _GATEWAY,
        "ollama_allow_remote": True,
        "ollama_auth": "aws_sigv4",
        "ollama_aws_region": "ap-southeast-2",
        **overrides,
    }


def _static_signer(token: "str | None" = None) -> _SigV4Signer:
    credentials = pytest.importorskip("botocore.credentials")
    creds = credentials.Credentials("AKIDEXAMPLE", "not-a-real-secret", token)
    return _SigV4Signer("ap-southeast-2", "execute-api", load_credentials=lambda: creds)


class TestAuthSettings:
    def test_no_auth_is_the_default(self) -> None:
        settings = _settings()
        assert settings.auth == "none"
        assert settings.error == ""

    @pytest.mark.parametrize("auth", ["aws_sigv5", "AWS_SIGV4", "iam"])
    def test_an_unknown_scheme_stops_the_backend(self, auth: str) -> None:
        assert "must be one of: none, aws_sigv4" in _settings(ollama_auth=auth).error

    def test_the_region_comes_from_config_first(self) -> None:
        settings = _settings(environ={"AWS_REGION": "us-west-2"}, **_sigv4())
        assert (settings.aws_region, settings.error) == ("ap-southeast-2", "")

    @pytest.mark.parametrize(
        ("environ", "region"),
        [
            ({"AWS_REGION": "us-west-2", "AWS_DEFAULT_REGION": "eu-west-1"}, "us-west-2"),
            ({"AWS_DEFAULT_REGION": "eu-west-1"}, "eu-west-1"),
        ],
    )
    def test_the_region_falls_back_to_the_environment(
        self, environ: dict[str, str], region: str
    ) -> None:
        settings = _settings(environ=environ, **_sigv4(ollama_aws_region=""))
        assert (settings.aws_region, settings.error) == (region, "")

    def test_no_region_stops_the_backend(self) -> None:
        assert "AWS_REGION" in _settings(**_sigv4(ollama_aws_region="")).error

    def test_the_service_defaults_to_api_gateway(self) -> None:
        assert _settings(**_sigv4()).aws_service == "execute-api"
        assert _settings(**_sigv4(ollama_aws_service="lambda")).aws_service == "lambda"

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("ollama_aws_region", "ap/southeast"),
            ("ollama_aws_service", "execute api"),
            ("ollama_aws_profile", "dev\nprofile"),
        ],
    )
    def test_a_malformed_name_stops_the_backend(self, key: str, value: str) -> None:
        assert "not a valid" in _settings(**_sigv4(**{key: value})).error

    def test_missing_botocore_stops_the_backend(self, monkeypatch) -> None:
        real_find_spec = embeddings_ollama.importlib.util.find_spec

        def find_spec(name: str, *args: Any) -> Any:
            return None if name == "botocore" else real_find_spec(name, *args)

        monkeypatch.setattr(embeddings_ollama.importlib.util, "find_spec", find_spec)
        assert "botocore" in _settings(**_sigv4()).error

    def test_no_signer_is_built_without_aws_sigv4(self) -> None:
        assert _embedder(_FakeOpener())._signer is None


class TestSigV4:
    def test_an_embed_request_is_signed_for_the_region_and_service(self) -> None:
        opener = _FakeOpener()
        vector = _embedder(opener, signer=_static_signer(), **_sigv4()).embed("hello")
        assert vector == [0.5] * _DIM
        headers = opener.headers[0]
        auth = headers["authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
        assert "/ap-southeast-2/execute-api/aws4_request" in auth
        assert "SignedHeaders=content-type;host;x-amz-date" in auth
        assert headers["x-amz-date"]
        assert "x-amz-security-token" not in headers
        assert opener.requests[0][0] == f"{_GATEWAY}/api/embed"

    def test_a_session_token_is_sent_and_signed(self) -> None:
        opener = _FakeOpener()
        _embedder(opener, signer=_static_signer(token="session-token"), **_sigv4()).embed("x")
        headers = opener.headers[0]
        assert headers["x-amz-security-token"] == "session-token"
        assert "x-amz-security-token" in headers["authorization"]

    def test_the_readiness_probe_is_signed_too(self) -> None:
        opener, probes = _FakeOpener(), _CapturedProbes()
        embedder = _embedder(opener, probes, signer=_static_signer(), **_sigv4())
        embedder.prefetch()
        probes.run_all()
        assert embedder.is_ready() is True
        assert opener.requests[0][0] == f"{_GATEWAY}/api/tags"
        assert "SignedHeaders=host;x-amz-date" in opener.headers[0]["authorization"]

    def test_a_signature_matches_botocore_for_the_same_request(self) -> None:
        auth = pytest.importorskip("botocore.auth")
        awsrequest = pytest.importorskip("botocore.awsrequest")
        credentials = pytest.importorskip("botocore.credentials")
        signer = _static_signer()
        body = b'{"model": "m"}'
        signed = signer.headers("POST", f"{_GATEWAY}/api/embed", body, {"Content-Type": "x"})
        expected = awsrequest.AWSRequest(
            method="POST",
            url=f"{_GATEWAY}/api/embed",
            data=body,
            headers={"Content-Type": "x", "X-Amz-Date": signed["X-Amz-Date"]},
        )
        sig = auth.SigV4Auth(
            credentials.Credentials("AKIDEXAMPLE", "not-a-real-secret"),
            "execute-api",
            "ap-southeast-2",
        )
        # add_auth re-stamps X-Amz-Date from the clock; sign the canonical request
        # at the signer's own timestamp instead.
        expected.context["timestamp"] = signed["X-Amz-Date"]
        canonical = sig.canonical_request(expected)
        string_to_sign = sig.string_to_sign(expected, canonical)
        assert signed["Authorization"].endswith(
            f"Signature={sig.signature(string_to_sign, expected)}"
        )

    def test_no_credentials_means_nothing_is_sent(self, caplog) -> None:
        opener = _FakeOpener()
        signer = _SigV4Signer("ap-southeast-2", "execute-api", load_credentials=lambda: None)
        embedder = _embedder(opener, signer=signer, **_sigv4())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.embeddings"):
            assert embedder.embed("secret") is None
        assert opener.requests == []
        assert any("no AWS credentials" in r.getMessage() for r in caplog.records)

    def test_a_signing_error_means_nothing_is_sent(self) -> None:
        def broken() -> Any:
            raise RuntimeError("The config profile (missing) could not be found")

        opener = _FakeOpener()
        signer = _SigV4Signer("ap-southeast-2", "execute-api", load_credentials=broken)
        assert _embedder(opener, signer=signer, **_sigv4()).embed("secret") is None
        assert opener.requests == []


# -- selection ----------------------------------------------------------------


class TestSelection:
    @pytest.fixture(autouse=True)
    def _no_env_model(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_EMBED_MODEL_PATH", raising=False)

    def test_the_provider_selects_the_server_backend(self, monkeypatch) -> None:
        memory = {"embedding_provider": "ollama_http", "ollama_model": _MODEL, "embedding_dim": 768}
        monkeypatch.setattr(emb, "_read_memory_config", lambda: memory)
        backend = emb.default_embedding_backend()
        assert isinstance(backend, OllamaHttpEmbedder)
        assert (backend.model_id, backend.dim) == (f"ollama:{_MODEL}", 768)

    def test_the_provider_wins_over_a_custom_model_path(self, monkeypatch, tmp_path) -> None:
        memory = {
            "embedding_provider": "ollama_http",
            "ollama_model": _MODEL,
            "embed_model_path": str(tmp_path / "m.gguf"),
        }
        monkeypatch.setattr(emb, "_read_memory_config", lambda: memory)
        assert isinstance(emb.default_embedding_backend(), OllamaHttpEmbedder)

    def test_the_retired_value_keeps_the_in_process_backend(self, monkeypatch) -> None:
        monkeypatch.setattr(emb, "_read_memory_config", lambda: {"embedding_provider": "ollama"})
        backend = emb.default_embedding_backend()
        try:
            assert isinstance(backend, LlamaCppEmbedder)
        finally:
            backend.close()

    def test_an_unusable_config_still_names_the_server_space(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(
            emb, "_read_memory_config", lambda: {"embedding_provider": "ollama_http"}
        )
        with caplog.at_level(logging.ERROR, logger="kiro_crew.embeddings"):
            backend = emb.default_embedding_backend()
        assert isinstance(backend, OllamaHttpEmbedder)
        assert backend.is_ready() is False
        assert any("memory.ollama_model is empty" in r.getMessage() for r in caplog.records)

    def test_the_bundled_model_is_not_downloaded_for_the_server_backend(self, monkeypatch) -> None:
        monkeypatch.setattr(
            emb,
            "_read_memory_config",
            lambda: {"embedding_provider": "ollama_http", "ollama_model": _MODEL},
        )

        def _must_not_run() -> None:
            raise AssertionError("the download manager was consulted")

        monkeypatch.setattr(embeddings_download, "model_download_manager", _must_not_run)
        assert embeddings_download.start_background_model_download() is None

    def test_the_facade_serves_the_backend(self) -> None:
        assert emb.OllamaHttpEmbedder is OllamaHttpEmbedder
