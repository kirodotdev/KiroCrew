"""Tests for the embeddings module."""

from __future__ import annotations

import importlib.util
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.embeddings import (
    _HEALTH_CHECK_INTERVAL_SECS,
    _HEALTH_CHECK_RETRIES,
    EmbeddingClient,
    OllamaManager,
    _resolve_blocked_addr,
    _validate_url,
)


class TestUrlValidation:
    def test_localhost_accepted(self) -> None:
        _validate_url("http://localhost:11434")
        _validate_url("http://127.0.0.1:11434")

    def test_remote_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be localhost"):
            _validate_url("http://evil.com:11434")

    def test_credentials_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not contain credentials"):
            _validate_url("http://user@localhost:11434")
        with pytest.raises(ValueError, match="must not contain credentials"):
            _validate_url("http://localhost:11434?token=secret")


class TestSsrfProtection:
    """Talos 76640a75: even with allow_remote+https, internal/metadata IP
    *literals* must be rejected (SSRF) — with NO DNS on the sync/loop path.
    A DNS *name* is not resolved here (blocking getaddrinfo is forbidden on the
    event loop); the residual name-based / DNS-rebinding TOCTOU is accepted.
    Malformed / hostless URLs are denied (fail-closed)."""

    def test_metadata_ipv4_literal_rejected(self) -> None:
        # IMDS endpoint — must be blocked even with allow_remote=True + https.
        with pytest.raises(ValueError, match="SSRF protection"):
            _validate_url("https://169.254.169.254", allow_remote=True)

    def test_metadata_ipv6_literal_rejected(self) -> None:
        with pytest.raises(ValueError, match="SSRF protection"):
            _validate_url("https://[fd00:ec2::254]", allow_remote=True)

    def test_rfc1918_literals_rejected(self) -> None:
        for host in ("10.0.0.5", "172.16.3.4", "192.168.1.1"):
            with pytest.raises(ValueError, match="SSRF protection"):
                _validate_url(f"https://{host}:11434", allow_remote=True)

    def test_loopback_literal_rejected_when_remote_scheme(self) -> None:
        # 127.0.0.2 is a loopback literal but not the exact-string localhost
        # allowlist, so it reaches the literal check and is rejected.
        with pytest.raises(ValueError, match="SSRF protection"):
            _validate_url("https://127.0.0.2:11434", allow_remote=True)

    def test_unspecified_and_ipv4_mapped_rejected(self) -> None:
        with pytest.raises(ValueError, match="SSRF protection"):
            _validate_url("https://0.0.0.0:11434", allow_remote=True)
        # IPv4-mapped IPv6 form of the IMDS address must not slip through.
        with pytest.raises(ValueError, match="SSRF protection"):
            _validate_url("https://[::ffff:169.254.169.254]", allow_remote=True)

    def test_dns_name_not_resolved_on_sync_path(self, monkeypatch) -> None:
        # A DNS *name* is NOT resolved here (no blocking getaddrinfo on the
        # loop). It is allowed through the literal check; the residual
        # name-based / DNS-rebinding TOCTOU is an accepted risk. The module now
        # imports socket ONLY for the pure, non-blocking ``inet_aton`` literal
        # parse — it must never call ``getaddrinfo`` / ``gethostbyname``.
        import kiro_crew.embeddings as emb_mod

        def _boom(*_a, **_k):  # pragma: no cover - must not be reached
            raise AssertionError("DNS resolution attempted on sync path")

        monkeypatch.setattr(emb_mod.socket, "getaddrinfo", _boom)
        monkeypatch.setattr(emb_mod.socket, "gethostbyname", _boom)
        _validate_url("https://internal.corp.example:11434", allow_remote=True)

    def test_public_hostname_allowed(self) -> None:
        # A DNS name over https with allow_remote is permitted (no raise, no DNS).
        _validate_url("https://embeddings.example.com:443", allow_remote=True)

    def test_public_literal_allowed(self) -> None:
        # A public literal IP over https with allow_remote is permitted.
        _validate_url("https://93.184.216.34:443", allow_remote=True)

    def test_remote_still_requires_https_before_ssrf_check(self) -> None:
        # http remote is rejected for the scheme reason before the SSRF check.
        with pytest.raises(ValueError, match="must use https"):
            _validate_url("http://169.254.169.254", allow_remote=True)

    def test_remote_flag_still_required(self) -> None:
        # Without allow_remote, a private literal is rejected as non-localhost.
        with pytest.raises(ValueError, match="must be localhost"):
            _validate_url("https://10.0.0.5:11434")

    def test_malformed_url_denied_fail_closed(self) -> None:
        # A URL whose host cannot be parsed must be DENIED, never allowed.
        with pytest.raises(ValueError):
            _validate_url("https://", allow_remote=True)
        with pytest.raises(ValueError):
            _validate_url("not a url", allow_remote=True)

    def test_resolve_blocked_addr_helper(self) -> None:
        # Pure / non-blocking: IP literals classified directly; a DNS name
        # returns None WITHOUT any resolution.
        assert _resolve_blocked_addr("169.254.169.254") == "169.254.169.254"
        assert _resolve_blocked_addr("10.0.0.1") == "10.0.0.1"
        assert _resolve_blocked_addr("fd00:ec2::254") == "fd00:ec2::254"
        assert _resolve_blocked_addr("93.184.216.34") is None
        assert _resolve_blocked_addr("public.example.com") is None

    def test_alternate_ipv4_encodings_rejected(self) -> None:
        # Heimdall follow-up: ``ipaddress.ip_address`` only accepts the canonical
        # dotted-quad form, so alternate IPv4 literal encodings for loopback used
        # to fall through as if they were DNS names. ``inet_aton`` normalization
        # must now classify them as blocked (loopback = 127.0.0.1).
        for host in (
            "0x7f000001",  # hex loopback
            "2130706433",  # decimal loopback
            "017700000001",  # octal loopback
            "127.1",  # short-form loopback
        ):
            assert _resolve_blocked_addr(host) == host, host

    def test_alternate_imds_encodings_rejected(self) -> None:
        # Alternate encodings of the IMDS endpoint 169.254.169.254 (link-local)
        # must also be blocked after inet_aton normalization.
        for host in (
            "0xa9fea9fe",  # hex IMDS
            "2852039166",  # decimal IMDS
            "169.254.43518",  # mixed short-form IMDS
        ):
            assert _resolve_blocked_addr(host) == host, host

    def test_alternate_encodings_blocked_via_validate_url(self) -> None:
        # End-to-end: the alternate-encoding loopback/IMDS literals must be
        # rejected by _validate_url even with allow_remote=True + https.
        for host in ("0x7f000001", "2130706433", "0xa9fea9fe", "2852039166"):
            with pytest.raises(ValueError, match="SSRF protection"):
                _validate_url(f"https://{host}:11434", allow_remote=True)

    def test_public_decimal_literal_still_allowed(self) -> None:
        # A public address expressed in decimal (8.8.8.8 = 134744072) must still
        # be allowed — normalization must not over-block public destinations.
        assert _resolve_blocked_addr("134744072") is None

    def test_trailing_dot_literals_rejected(self) -> None:
        # Heimdall round-2 (CR-289119233 follow-up): a fully-qualified trailing-dot
        # literal (``169.254.169.254.`` / ``127.0.0.1.``) is rejected by both
        # ``ipaddress.ip_address`` and ``socket.inet_aton``, so it used to fall
        # through as a DNS name and slip past the SSRF check. After trailing-dot
        # normalization it must classify as blocked (link-local / loopback).
        assert _resolve_blocked_addr("169.254.169.254.") == "169.254.169.254"
        assert _resolve_blocked_addr("127.0.0.1.") == "127.0.0.1"

    def test_trailing_dot_public_literal_still_allowed(self) -> None:
        # The trailing-dot form of a public literal must still be allowed —
        # normalization must not over-block public destinations.
        assert _resolve_blocked_addr("93.184.216.34.") is None

    def test_trailing_dot_imds_blocked_via_validate_url(self) -> None:
        # End-to-end: the trailing-dot IMDS literal must be rejected by
        # _validate_url even with allow_remote=True + https.
        with pytest.raises(ValueError, match="SSRF protection"):
            _validate_url("https://169.254.169.254.:11434", allow_remote=True)


class TestEmbeddingClient:
    def test_init_validates_url(self) -> None:
        EmbeddingClient("http://localhost:11434")
        with pytest.raises(ValueError):
            EmbeddingClient("http://remote.server:11434")

    @pytest.mark.asyncio
    async def test_health_returns_false_on_error(self) -> None:
        client = EmbeddingClient("http://localhost:19999")
        assert not await client.health()

    @pytest.mark.asyncio
    async def test_health_checks_configured_model(self) -> None:
        """health() must check the model passed at init, not the hardcoded default."""
        client = EmbeddingClient("http://localhost:19999", model="snowflake-arctic-embed2")
        mock = AsyncMock(return_value=True)
        with patch("kiro_crew.embeddings._ollama_has_model", mock):
            result = await client.health()
        mock.assert_called_once()
        assert mock.call_args[0][1] == "snowflake-arctic-embed2"
        assert result is True

    @pytest.mark.asyncio
    async def test_embed_one_returns_none_on_error(self) -> None:
        client = EmbeddingClient("http://localhost:19999")
        result = await client.embed_one("test text")
        assert result is None

    @pytest.mark.asyncio
    async def test_embed_batch_returns_none_on_error(self) -> None:
        client = EmbeddingClient("http://localhost:19999")
        result = await client.embed_batch(["a", "b"])
        assert result is None


class TestOllamaManager:
    def test_ollama_binary_not_found(self) -> None:
        mgr = OllamaManager()
        with patch("shutil.which", return_value=None):
            assert mgr.ollama_binary is None

    def test_ollama_binary_found(self) -> None:
        mgr = OllamaManager()
        with patch("shutil.which", return_value="/opt/homebrew/bin/ollama"):
            assert mgr.ollama_binary == "/opt/homebrew/bin/ollama"

    def test_is_running_false_initially(self) -> None:
        mgr = OllamaManager()
        assert not mgr.is_running()

    @pytest.mark.asyncio
    async def test_start_fails_without_binary(self) -> None:
        mgr = OllamaManager("http://localhost:19999")  # unreachable port
        # Patch ollama_binary → None so the "no binary" branch is taken.
        # Also mock install_ollama to return False immediately: without this
        # the real install_ollama runs brew/curl commands that time out in CI.
        with (
            patch.object(
                type(mgr), "ollama_binary", new_callable=lambda: property(lambda self: None)
            ),
            patch.object(mgr, "install_ollama", new_callable=AsyncMock, return_value=False),
        ):
            assert not await mgr.start_server()

    @pytest.mark.asyncio
    async def test_stop_noop_when_not_running(self) -> None:
        mgr = OllamaManager()
        await mgr.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_is_server_running_false_when_down(self) -> None:
        mgr = OllamaManager("http://localhost:19999")
        assert not await mgr.is_server_running()

    @pytest.mark.asyncio
    async def test_model_available_false_when_down(self) -> None:
        mgr = OllamaManager("http://localhost:19999")
        assert not await mgr.model_available()

    def test_default_model(self) -> None:
        mgr = OllamaManager()
        assert mgr._model == "qwen3-embedding:0.6b"

    def test_custom_model(self) -> None:
        mgr = OllamaManager(model="snowflake-arctic-embed2")
        assert mgr._model == "snowflake-arctic-embed2"

    @pytest.mark.asyncio
    async def test_model_available_checks_configured_model(self) -> None:
        """model_available() must check the model passed at init, not the hardcoded default."""
        mgr = OllamaManager("http://localhost:19999", model="snowflake-arctic-embed2")
        mock = AsyncMock(return_value=True)
        with patch("kiro_crew.embeddings._ollama_has_model", mock):
            result = await mgr.model_available()
        mock.assert_called_once_with("http://localhost:19999", "snowflake-arctic-embed2")
        assert result is True


class TestSyncEmbedCache:
    """Tests for make_sync_embed_fn LRU caching behavior."""

    def _mock_response(self, embedding: list[float]) -> patch:
        """Return a patch that makes urlopen return a valid embedding response.

        Single BytesIO — only safe when caching prevents repeated reads.
        Use ``_mock_raw_response`` for multi-call tests.
        """
        import io
        import json as _json

        body = _json.dumps({"embeddings": [embedding]}).encode()
        mock_resp = io.BytesIO(body)
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s  # type: ignore[attr-defined]
        mock_resp.__exit__ = lambda s, *a: None  # type: ignore[attr-defined]
        return patch("urllib.request.urlopen", return_value=mock_resp)

    def _mock_raw_response(self, body_dict: dict) -> patch:
        """Return a patch that makes urlopen return a fresh response per call."""
        import io
        import json as _json

        body = _json.dumps(body_dict).encode()

        def _factory(*a, **kw):
            resp = io.BytesIO(body)
            resp.status = 200
            resp.__enter__ = lambda s: s  # type: ignore[attr-defined]
            resp.__exit__ = lambda s, *a: None  # type: ignore[attr-defined]
            return resp

        return patch("urllib.request.urlopen", side_effect=_factory)

    def test_caches_successful_result(self) -> None:
        from kiro_crew.embeddings import make_sync_embed_fn

        embed = make_sync_embed_fn()
        fake_vec = [0.1] * 1024
        with self._mock_response(fake_vec) as mock_urlopen:
            first = embed("hello")
            second = embed("hello")
        assert first == fake_vec
        assert second == fake_vec
        assert mock_urlopen.call_count == 1

    def test_returns_none_on_failure(self) -> None:
        from kiro_crew.embeddings import make_sync_embed_fn

        embed = make_sync_embed_fn()
        with patch("urllib.request.urlopen", side_effect=ConnectionError):
            result = embed("hello")
        assert result is None

    def test_retries_after_failure(self) -> None:
        from kiro_crew.embeddings import make_sync_embed_fn

        embed = make_sync_embed_fn()
        fake_vec = [0.2] * 1024
        with patch("urllib.request.urlopen", side_effect=ConnectionError):
            assert embed("hello") is None
        with self._mock_response(fake_vec) as mock_urlopen:
            result = embed("hello")
        assert result == fake_vec
        assert mock_urlopen.call_count == 1

    def test_malformed_response_returns_none_and_not_cached(self) -> None:
        from kiro_crew.embeddings import make_sync_embed_fn

        embed = make_sync_embed_fn()
        with self._mock_raw_response({"embeddings": []}) as mock_urlopen:
            assert embed("hello") is None
            assert embed("hello") is None
        assert mock_urlopen.call_count == 2  # not cached — retried both times

    def test_multi_embedding_response_returns_none_and_not_cached(self) -> None:
        from kiro_crew.embeddings import make_sync_embed_fn

        embed = make_sync_embed_fn()
        body = {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
        with self._mock_raw_response(body) as mock_urlopen:
            assert embed("hello") is None
            assert embed("hello") is None
        assert mock_urlopen.call_count == 2  # not cached — retried both times


class TestInstallOllama:
    """Tests for install_ollama Rosetta 2 / arch detection logic."""

    @staticmethod
    def _mock_subprocess(returncode: int = 0, stdout: bytes = b""):
        """Return an AsyncMock that behaves like asyncio.subprocess.Process."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        proc.returncode = returncode
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        return proc

    @staticmethod
    async def _passthrough_wait_for(coro, timeout=None):
        """Await the coroutine directly, bypassing real timeout machinery."""
        return await coro

    @pytest.mark.asyncio
    async def test_arm_sysctl_detected_tries_arch_first(self) -> None:
        """On ARM Mac, should try arch -arm64 brew first."""
        sysctl_proc = self._mock_subprocess(returncode=0, stdout=b"1\n")
        brew_proc = self._mock_subprocess(returncode=0)

        async def fake_exec(*args, **kwargs):
            if args[0] == "sysctl":
                return sysctl_proc
            return brew_proc

        mgr = OllamaManager()
        mgr._use_docker = False
        with patch("platform.system", return_value="Darwin"), \
             patch("shutil.which", return_value="/opt/homebrew/bin/brew"), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec) as mock_exec, \
             patch("asyncio.wait_for", side_effect=self._passthrough_wait_for):
            result = await mgr.install_ollama()

        assert result is True
        # First call is sysctl, second is arch -arm64 brew install
        assert mock_exec.call_args_list[1][0][:2] == ("arch", "-arm64")

    @pytest.mark.asyncio
    async def test_intel_sysctl_skips_arch_prefix(self) -> None:
        """On Intel Mac, sysctl returns 0 (not ARM), should only try bare brew."""
        sysctl_proc = self._mock_subprocess(returncode=0, stdout=b"0\n")
        brew_proc = self._mock_subprocess(returncode=0)

        async def fake_exec(*args, **kwargs):
            if args[0] == "sysctl":
                return sysctl_proc
            return brew_proc

        mgr = OllamaManager()
        mgr._use_docker = False
        with patch("platform.system", return_value="Darwin"), \
             patch("shutil.which", return_value="/usr/local/bin/brew"), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec) as mock_exec, \
             patch("asyncio.wait_for", side_effect=self._passthrough_wait_for):
            result = await mgr.install_ollama()

        assert result is True
        # sysctl call + one bare brew call (no arch -arm64)
        assert len(mock_exec.call_args_list) == 2
        assert mock_exec.call_args_list[1][0][0] == "brew"

    @pytest.mark.asyncio
    async def test_proc_none_on_oserror_no_kill(self) -> None:
        """If create_subprocess_exec raises OSError, proc stays None — no kill called."""
        sysctl_proc = self._mock_subprocess(returncode=0, stdout=b"0\n")

        async def fake_exec(*args, **kwargs):
            if args[0] == "sysctl":
                return sysctl_proc
            raise OSError("No such file")

        mgr = OllamaManager()
        mgr._use_docker = False
        with patch("platform.system", return_value="Darwin"), \
             patch("shutil.which", return_value="/usr/local/bin/brew"), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await mgr.install_ollama()

        assert result is False
        sysctl_proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_kills_proc_when_assigned(self) -> None:
        """On timeout, proc.kill() should be called if proc was assigned."""
        import asyncio as _asyncio

        sysctl_proc = self._mock_subprocess(returncode=0, stdout=b"0\n")
        brew_proc = self._mock_subprocess(returncode=0)

        async def fake_exec(*args, **kwargs):
            if args[0] == "sysctl":
                return sysctl_proc
            return brew_proc

        async def fake_wait_for(coro, timeout=None):
            result = await coro
            if timeout == 10:  # sysctl call — pass through
                return result
            raise _asyncio.TimeoutError()

        mgr = OllamaManager()
        mgr._use_docker = False
        with patch("platform.system", return_value="Darwin"), \
             patch("shutil.which", return_value="/usr/local/bin/brew"), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("asyncio.wait_for", side_effect=fake_wait_for):
            result = await mgr.install_ollama()

        assert result is False
        brew_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_arm_fallback_on_first_failure(self) -> None:
        """On ARM, if arch -arm64 fails, should fall back to bare brew."""
        sysctl_proc = self._mock_subprocess(returncode=0, stdout=b"1\n")
        fail_proc = self._mock_subprocess(returncode=1)
        ok_proc = self._mock_subprocess(returncode=0)

        call_idx = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if args[0] == "sysctl":
                return sysctl_proc
            # First brew call fails, second succeeds
            return fail_proc if call_idx == 2 else ok_proc

        mgr = OllamaManager()
        mgr._use_docker = False
        with patch("platform.system", return_value="Darwin"), \
             patch("shutil.which", return_value="/opt/homebrew/bin/brew"), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec) as mock_exec, \
             patch("asyncio.wait_for", side_effect=self._passthrough_wait_for):
            result = await mgr.install_ollama()

        assert result is True
        # sysctl + arch brew (fail) + bare brew (success) = 3 calls
        assert len(mock_exec.call_args_list) == 3

    @pytest.mark.asyncio
    async def test_arm_timeout_first_falls_back_to_bare_brew(self) -> None:
        """On ARM, if arch -arm64 times out, should kill it and try bare brew."""
        import asyncio as _asyncio

        sysctl_proc = self._mock_subprocess(returncode=0, stdout=b"1\n")
        arm_proc = self._mock_subprocess(returncode=0)
        bare_proc = self._mock_subprocess(returncode=0)

        call_idx = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if args[0] == "sysctl":
                return sysctl_proc
            return arm_proc if call_idx == 2 else bare_proc

        async def fake_wait_for(coro, timeout=None):
            result = await coro
            if timeout == 10:  # sysctl call — pass through
                return result
            # Only timeout on the first brew call (arch -arm64), not on proc.wait() or bare brew
            if timeout == 1200 and call_idx == 2:
                raise _asyncio.TimeoutError()
            return result

        mgr = OllamaManager()
        mgr._use_docker = False
        with patch("platform.system", return_value="Darwin"), \
             patch("shutil.which", return_value="/opt/homebrew/bin/brew"), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec) as mock_exec, \
             patch("asyncio.wait_for", side_effect=fake_wait_for):
            result = await mgr.install_ollama()

        assert result is True
        arm_proc.kill.assert_called_once()
        # sysctl + arch brew (timeout) + bare brew (success) = 3 calls
        assert len(mock_exec.call_args_list) == 3
        assert mock_exec.call_args_list[2][0][0] == "brew"


class TestEmbeddingRuntimePersistence:
    """Tests for the embedding_runtime config persistence (AL2 Docker fallback)."""

    def test_init_reads_docker_runtime_from_config(self) -> None:
        """OllamaManager should set _use_docker=True when config says 'docker'."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "docker"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()):
            mgr = OllamaManager()
        assert mgr._use_docker is True

    def test_init_defaults_native_runtime(self) -> None:
        """OllamaManager should set _use_docker=False when config says 'native'."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "native"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()):
            mgr = OllamaManager()
        assert mgr._use_docker is False

    def test_init_handles_config_load_failure(self) -> None:
        """OllamaManager should default to _use_docker=False if config load fails."""
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", side_effect=Exception("boom")):
            mgr = OllamaManager()
        assert mgr._use_docker is False

    def test_persist_embedding_runtime_writes_config(self, tmp_path) -> None:
        """_persist_embedding_runtime should write to config.json."""
        from kiro_crew.embeddings import _persist_embedding_runtime

        config_file = tmp_path / "config.json"
        with patch("kiro_crew.embeddings.config_path", return_value=config_file):
            _persist_embedding_runtime("docker")

        data = json.loads(config_file.read_text())
        assert data["memory"]["embedding_runtime"] == "docker"

    def test_persist_embedding_runtime_preserves_existing(self, tmp_path) -> None:
        """_persist_embedding_runtime should not clobber existing config keys."""
        from kiro_crew.embeddings import _persist_embedding_runtime

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"memory": {"embedding_provider": "ollama"}}))
        with patch("kiro_crew.embeddings.config_path", return_value=config_file):
            _persist_embedding_runtime("docker")

        data = json.loads(config_file.read_text())
        assert data["memory"]["embedding_provider"] == "ollama"
        assert data["memory"]["embedding_runtime"] == "docker"

    @pytest.mark.asyncio
    async def test_glibc_fallback_persists_docker_runtime(self) -> None:
        """start_server() GLIBC fallback should persist 'docker' to config."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "native"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        # Simulate: native binary crashes with GLIBC error, no brew, Docker fallback succeeds
        glibc_stderr = b"GLIBC_2.28 not found"
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", glibc_stderr))
        proc.returncode = 1
        proc.pid = 12345

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()), \
             patch.object(OllamaManager, "is_server_running", return_value=False), \
             patch.object(OllamaManager, "ollama_binary", new_callable=lambda: property(lambda self: "/usr/local/bin/ollama")), \
             patch("shutil.which", return_value=None), \
             patch("asyncio.create_subprocess_exec", return_value=proc), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch.object(OllamaManager, "_start_docker_server", new_callable=AsyncMock, return_value=True) as mock_docker, \
             patch("kiro_crew.embeddings._persist_embedding_runtime") as mock_persist:
            mgr = OllamaManager("http://localhost:19999")
            result = await mgr.start_server()

        assert result is True
        assert mgr._use_docker is True
        mock_persist.assert_called_once_with("docker")
        mock_docker.assert_called_once()

    @pytest.mark.asyncio
    async def test_docker_runtime_skips_native_binary(self) -> None:
        """When config has embedding_runtime=docker, start_server should go straight to Docker."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "docker"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()), \
             patch.object(OllamaManager, "is_server_running", return_value=False), \
             patch.object(OllamaManager, "_docker_bin", return_value=None), \
             patch.object(OllamaManager, "_start_docker_server", new_callable=AsyncMock, return_value=True) as mock_docker, \
             patch("asyncio.create_subprocess_exec") as mock_exec:
            mgr = OllamaManager("http://localhost:19999")
            result = await mgr.start_server()

        assert result is True
        mock_docker.assert_called_once()
        # Native binary should NOT have been launched
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_pull_model_docker_uses_docker_exec(self) -> None:
        """When _use_docker=True, pull_model should pull via ``docker exec ... ollama pull``."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "docker"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()), \
             patch("shutil.which", return_value="/usr/bin/docker"), \
             patch.object(OllamaManager, "model_available", new_callable=AsyncMock, return_value=False), \
             patch.object(OllamaManager, "_run_docker", new_callable=AsyncMock, return_value=(0, "")) as mock_docker:
            mgr = OllamaManager("http://localhost:19999")
            assert mgr._use_docker is True
            result = await mgr.pull_model()

        assert result is True
        # Should have pulled via docker exec, not the native ollama binary.
        assert any(
            call.args[:2] == ("exec", "kirocrew-ollama") and "pull" in call.args
            for call in mock_docker.call_args_list
        )

    @pytest.mark.asyncio
    async def test_pull_model_failure_names_fallback_model(self, caplog) -> None:
        """A failed ``ollama pull`` must log the documented fallback (nomic-embed-text)."""
        import logging

        from kiro_crew.embeddings import _FALLBACK_EMBEDDING_MODEL

        mgr = OllamaManager("http://localhost:19999", model="qwen3-embedding:0.6b")
        mgr._use_docker = False

        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"manifest not found"))
        proc.returncode = 1

        with patch.object(OllamaManager, "model_available", new_callable=AsyncMock, return_value=False), \
             patch.object(type(mgr), "ollama_binary", new_callable=lambda: property(lambda self: "/usr/local/bin/ollama")), \
             patch("asyncio.create_subprocess_exec", return_value=proc), \
             patch("asyncio.wait_for", new=AsyncMock(return_value=(b"", b"manifest not found"))), \
             caplog.at_level(logging.ERROR, logger="kiro_crew.embeddings"):
            result = await mgr.pull_model()

        assert result is False
        # The actionable message must name the documented fallback model.
        assert _FALLBACK_EMBEDDING_MODEL in caplog.text
        assert _FALLBACK_EMBEDDING_MODEL == "nomic-embed-text"

    @pytest.mark.asyncio
    async def test_start_server_detects_existing_docker_container(self) -> None:
        """When server is already running via Docker container, start_server should set _use_docker=True."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "native"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()), \
             patch.object(OllamaManager, "is_server_running", return_value=True), \
             patch.object(OllamaManager, "_docker_bin", return_value="/usr/bin/docker"), \
             patch.object(OllamaManager, "_run_docker", new_callable=AsyncMock, return_value=(0, "true")) as mock_inspect, \
             patch("kiro_crew.embeddings._persist_embedding_runtime") as mock_persist:
            mgr = OllamaManager("http://localhost:19999")
            assert mgr._use_docker is False  # starts native
            result = await mgr.start_server()

        assert result is True
        assert mgr._use_docker is True  # detected Docker container
        mock_inspect.assert_called_once_with("inspect", "-f", "{{.State.Running}}", "kirocrew-ollama", timeout=5)
        mock_persist.assert_called_once_with("docker")

    def test_persist_embedding_runtime_corrupt_json_bails_out(self, tmp_path) -> None:
        """_persist_embedding_runtime should bail if config contains unparseable JSON."""
        from kiro_crew.embeddings import _persist_embedding_runtime

        config_file = tmp_path / "config.json"
        config_file.write_text("{invalid json!!")
        with patch("kiro_crew.embeddings.config_path", return_value=config_file):
            _persist_embedding_runtime("docker")

        # File should be unchanged — we bailed out on read failure
        assert config_file.read_text() == "{invalid json!!"

    def test_persist_embedding_runtime_non_dict_json_no_crash(self, tmp_path) -> None:
        """_persist_embedding_runtime should not crash if config is a JSON array."""
        from kiro_crew.embeddings import _persist_embedding_runtime

        config_file = tmp_path / "config.json"
        config_file.write_text("[1, 2, 3]")
        with patch("kiro_crew.embeddings.config_path", return_value=config_file):
            _persist_embedding_runtime("docker")

        # File should be unchanged — setdefault on list raises AttributeError, caught
        assert config_file.read_text() == "[1, 2, 3]"

    def test_persist_embedding_runtime_write_failure_no_crash(self, tmp_path) -> None:
        """_persist_embedding_runtime should not crash if write fails."""
        from kiro_crew.embeddings import _persist_embedding_runtime

        config_file = tmp_path / "config.json"
        config_file.write_text("{}")
        with patch("kiro_crew.embeddings.config_path", return_value=config_file), \
             patch("pathlib.Path.write_text", side_effect=PermissionError("denied")):
            _persist_embedding_runtime("docker")
        # No exception raised — function handled it gracefully

    @pytest.mark.asyncio
    async def test_start_server_docker_detect_timeout_no_crash(self) -> None:
        """start_server should not crash if docker inspect times out."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "native"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        import asyncio
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()), \
             patch.object(OllamaManager, "is_server_running", return_value=True), \
             patch.object(OllamaManager, "_docker_bin", return_value="/usr/bin/docker"), \
             patch.object(OllamaManager, "_run_docker", new_callable=AsyncMock, side_effect=asyncio.TimeoutError()):
            mgr = OllamaManager("http://localhost:19999")
            result = await mgr.start_server()

        assert result is True
        assert mgr._use_docker is False  # stayed native, no crash

    @pytest.mark.asyncio
    async def test_start_server_docker_detect_stopped_container(self) -> None:
        """start_server should not set _use_docker if container exists but is stopped."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "native"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()), \
             patch.object(OllamaManager, "is_server_running", return_value=True), \
             patch.object(OllamaManager, "_docker_bin", return_value="/usr/bin/docker"), \
             patch.object(OllamaManager, "_run_docker", new_callable=AsyncMock, return_value=(0, "false")):
            mgr = OllamaManager("http://localhost:19999")
            result = await mgr.start_server()

        assert result is True
        assert mgr._use_docker is False  # container stopped, don't use docker

    @pytest.mark.asyncio
    async def test_start_server_retry_succeeds_with_docker_container(self) -> None:
        """start_server retries when docker container exists and succeeds on second check."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "native"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()), \
             patch.object(OllamaManager, "is_server_running", side_effect=[False, True]), \
             patch.object(OllamaManager, "_docker_bin", return_value="/usr/bin/docker"), \
             patch.object(OllamaManager, "_run_docker", new_callable=AsyncMock, return_value=(0, "true")), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch.object(OllamaManager, "_detect_docker_runtime", new_callable=AsyncMock):
            mgr = OllamaManager("http://localhost:19999")
            result = await mgr.start_server()

        assert result is True
        mock_sleep.assert_called_with(_HEALTH_CHECK_INTERVAL_SECS)

    @pytest.mark.asyncio
    async def test_start_server_retry_exhausted_falls_through(self) -> None:
        """start_server falls through to _start_docker_server when retries exhausted."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "native"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()), \
             patch.object(OllamaManager, "is_server_running", return_value=False), \
             patch.object(OllamaManager, "_docker_bin", return_value="/usr/bin/docker"), \
             patch.object(OllamaManager, "_run_docker", new_callable=AsyncMock, return_value=(0, "true")), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch.object(OllamaManager, "_start_docker_server", new_callable=AsyncMock, return_value=True):
            mgr = OllamaManager("http://localhost:19999")
            mgr._use_docker = True
            result = await mgr.start_server()

        assert result is True
        assert mock_sleep.call_count == _HEALTH_CHECK_RETRIES - 1

    @pytest.mark.asyncio
    async def test_start_server_docker_inspect_exception_skips_retry(self) -> None:
        """start_server skips retry loop when docker inspect raises an exception."""
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            embedding_runtime: str = "native"

        @dataclass
        class FakeCfg:
            memory: FakeMemory = None

            def __post_init__(self):
                self.memory = FakeMemory()

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=FakeCfg()), \
             patch.object(OllamaManager, "is_server_running", return_value=False), \
             patch.object(OllamaManager, "_docker_bin", return_value="/usr/bin/docker"), \
             patch.object(OllamaManager, "_run_docker", new_callable=AsyncMock, side_effect=RuntimeError("connection refused")), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch.object(OllamaManager, "_start_docker_server", new_callable=AsyncMock, return_value=True):
            mgr = OllamaManager("http://localhost:19999")
            mgr._use_docker = True
            result = await mgr.start_server()

        assert result is True
        mock_sleep.assert_not_called()


# ── SigV4 auth validation and signing tests ──


class TestAuthValidation:
    """Auth scheme validation in EmbeddingClient and make_sync_embed_fn."""

    def test_embedding_client_rejects_unknown_auth(self) -> None:
        with pytest.raises(ValueError, match="Unknown embedding auth scheme"):
            EmbeddingClient("http://localhost:11434", auth="aws_sig4")

    def test_embedding_client_accepts_none(self) -> None:
        c = EmbeddingClient("http://localhost:11434", auth="none")
        assert c._auth == "none"

    def test_embedding_client_accepts_sigv4(self) -> None:
        c = EmbeddingClient("http://localhost:11434", auth="aws_sigv4")
        assert c._auth == "aws_sigv4"

    def test_make_sync_embed_fn_rejects_unknown_auth(self) -> None:
        from kiro_crew.embeddings import make_sync_embed_fn
        with pytest.raises(ValueError, match="Unknown embedding auth scheme"):
            make_sync_embed_fn(auth="sigv4")

    def test_make_sync_embed_fn_accepts_valid_auth(self) -> None:
        from kiro_crew.embeddings import make_sync_embed_fn
        fn = make_sync_embed_fn(auth="none")
        assert callable(fn)
        fn2 = make_sync_embed_fn(auth="aws_sigv4")
        assert callable(fn2)


class TestSigV4Sign:
    """Tests for _sigv4_sign and _get_botocore_session."""

    def test_get_botocore_session_returns_none_without_botocore(self, monkeypatch) -> None:
        """When botocore is not installed, _get_botocore_session returns None."""
        from kiro_crew.embeddings import _get_botocore_session
        _get_botocore_session.cache_clear()
        monkeypatch.setattr("kiro_crew.embeddings._HAS_BOTOCORE", False)
        assert _get_botocore_session() is None

    @pytest.mark.skipif(
        not importlib.util.find_spec("botocore"),
        reason="botocore not installed (optional voice dep)",
    )
    def test_get_botocore_session_returns_session(self) -> None:
        """When botocore IS installed, _get_botocore_session returns a Session."""
        from kiro_crew.embeddings import _get_botocore_session
        _get_botocore_session.cache_clear()
        sess = _get_botocore_session()
        from botocore.session import Session
        assert isinstance(sess, Session)

    def test_sigv4_sign_success(self, monkeypatch) -> None:
        from kiro_crew.embeddings import _sigv4_sign
        monkeypatch.setattr("kiro_crew.embeddings._HAS_BOTOCORE", True)
        mock_creds = MagicMock()
        mock_creds.get_frozen_credentials.return_value = MagicMock(
            access_key="AKID", secret_key="SECRET", token=None
        )
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = mock_creds
        # Mock AWSRequest to capture headers and return them
        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "AWS4-HMAC-SHA256 ...", "Content-Type": "application/json"}
        monkeypatch.setattr("kiro_crew.embeddings.AWSRequest", lambda **kw: mock_request)
        monkeypatch.setattr("kiro_crew.embeddings.SigV4Auth", MagicMock())
        monkeypatch.setattr("kiro_crew.embeddings._get_botocore_session", lambda: mock_session)
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        result = _sigv4_sign("POST", "https://api.example.com/api/embed", {"Content-Type": "application/json"}, b'{}')
        assert result is not None
        assert "Authorization" in result

    def test_sigv4_sign_no_credentials(self, monkeypatch) -> None:
        from kiro_crew.embeddings import _sigv4_sign
        monkeypatch.setattr("kiro_crew.embeddings._HAS_BOTOCORE", True)
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = None
        monkeypatch.setattr("kiro_crew.embeddings._get_botocore_session", lambda: mock_session)
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        result = _sigv4_sign("POST", "https://api.example.com/api/embed", {}, b'{}')
        assert result is None

    def test_sigv4_sign_exception(self, monkeypatch) -> None:
        from kiro_crew.embeddings import _sigv4_sign
        monkeypatch.setattr("kiro_crew.embeddings._HAS_BOTOCORE", True)
        monkeypatch.setattr("kiro_crew.embeddings._get_botocore_session", MagicMock(side_effect=RuntimeError("boom")))
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        result = _sigv4_sign("POST", "https://api.example.com/api/embed", {}, b'{}')
        assert result is None

    def test_sigv4_sign_region_fallback(self, monkeypatch) -> None:
        from kiro_crew.embeddings import _sigv4_sign
        monkeypatch.setattr("kiro_crew.embeddings._HAS_BOTOCORE", True)
        mock_creds = MagicMock()
        mock_creds.get_frozen_credentials.return_value = MagicMock(
            access_key="AKID", secret_key="SECRET", token=None
        )
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = mock_creds
        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "AWS4-HMAC-SHA256 ..."}
        monkeypatch.setattr("kiro_crew.embeddings.AWSRequest", lambda **kw: mock_request)
        monkeypatch.setattr("kiro_crew.embeddings.SigV4Auth", MagicMock())
        monkeypatch.setattr("kiro_crew.embeddings._get_botocore_session", lambda: mock_session)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        result = _sigv4_sign("POST", "https://api.example.com/api/embed", {"Content-Type": "application/json"}, b'{}')
        # Falls back to us-east-1 with a warning, but still signs
        assert result is not None

    def test_sigv4_sign_string_body(self, monkeypatch) -> None:
        from kiro_crew.embeddings import _sigv4_sign
        monkeypatch.setattr("kiro_crew.embeddings._HAS_BOTOCORE", True)
        mock_creds = MagicMock()
        mock_creds.get_frozen_credentials.return_value = MagicMock(
            access_key="AKID", secret_key="SECRET", token=None
        )
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = mock_creds
        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "AWS4-HMAC-SHA256 ..."}
        monkeypatch.setattr("kiro_crew.embeddings.AWSRequest", lambda **kw: mock_request)
        monkeypatch.setattr("kiro_crew.embeddings.SigV4Auth", MagicMock())
        monkeypatch.setattr("kiro_crew.embeddings._get_botocore_session", lambda: mock_session)
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        result = _sigv4_sign("POST", "https://api.example.com/api/embed", {}, '{"text":"hello"}')
        assert result is not None


class TestUnmanagedEmbeddingPath:
    """Tests for the unmanaged (external) Ollama embedding wiring in gateway."""

    @pytest.mark.asyncio
    async def test_unmanaged_path_wires_embed_fn(self, monkeypatch) -> None:
        """When embedding_managed=False, _start_ollama sets embed_fn without launching Ollama."""
        cfg = MagicMock()
        cfg.memory.embedding_managed = False
        cfg.memory.embedding_url = "http://localhost:11434"
        cfg.memory.allow_remote_embedding = False
        cfg.memory.embedding_timeout_secs = 5.0
        cfg.memory.embedding_model = "test-model"
        cfg.memory.embedding_auth = "none"

        captured = {}

        def fake_make_sync(url, timeout, model, auth):
            captured.update(url=url, timeout=timeout, model=model, auth=auth)
            return lambda text: [0.1] * 10

        monkeypatch.setattr("kiro_crew.slack.gateway.make_sync_embed_fn", fake_make_sync)
        monkeypatch.setattr("kiro_crew.slack.gateway._validate_url", lambda url, allow_remote: None)

        # Minimal orchestrator mock
        orch = MagicMock()
        orch._cfg = cfg
        orch.vector_memory = MagicMock(embed_fn=None)

        from kiro_crew.slack.gateway import GatewayOrchestrator
        await GatewayOrchestrator._start_ollama(orch)

        assert captured["url"] == "http://localhost:11434"
        assert captured["timeout"] == 5.0
        assert captured["model"] == "test-model"
        assert captured["auth"] == "none"
        assert orch.vector_memory.embed_fn is not None

    @pytest.mark.asyncio
    async def test_unmanaged_path_rejects_bad_url(self, monkeypatch) -> None:
        """When external URL fails validation, embeddings are disabled."""
        cfg = MagicMock()
        cfg.memory.embedding_managed = False
        cfg.memory.embedding_url = "http://evil.com:11434"
        cfg.memory.allow_remote_embedding = False

        def strict_validate(url, allow_remote):
            raise ValueError("must be localhost")

        monkeypatch.setattr("kiro_crew.slack.gateway._validate_url", strict_validate)

        orch = MagicMock()
        orch._cfg = cfg
        orch.vector_memory = MagicMock(spec=[])
        orch.vector_memory.embed_fn = None

        from kiro_crew.slack.gateway import GatewayOrchestrator
        await GatewayOrchestrator._start_ollama(orch)

        # embed_fn should NOT have been set
        assert orch.vector_memory.embed_fn is None
