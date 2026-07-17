"""Embedding client and Ollama server manager.

Provides async HTTP client for Ollama embedding server and lifecycle
management. Model is configurable via ``embedding_model`` config field
(default: qwen3-embedding:0.6b).
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import logging
import os
import platform
import shlex
import shutil
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

import aiohttp

from kiro_crew import platform_compat
from kiro_crew.config.loader import KiroCrewConfig, config_dir, config_path
from kiro_crew.constants import OLLAMA_DOCKER_CONTAINER
from kiro_crew.platform import current_context, safe_context_call
from kiro_crew.sel import sel

# ── Optional dependency: botocore (AWS SigV4 signing) ──
# Used only by the unmanaged-Ollama → API Gateway path (embedding_auth=aws_sigv4).
try:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session as BotocoreSession

    _HAS_BOTOCORE = True
except ImportError:
    SigV4Auth = None  # type: ignore[assignment,misc]
    AWSRequest = None  # type: ignore[assignment,misc]
    BotocoreSession = None  # type: ignore[assignment,misc]
    _HAS_BOTOCORE = False

logger = logging.getLogger(__name__)

# ── Constants ──

# Public Ollama registry model. Documented fallback: "nomic-embed-text" (dim 768).
_OLLAMA_MODEL = "qwen3-embedding:0.6b"
# Documented fallback model when the configured embedding model can't be pulled
# (e.g. registry hiccup, model renamed/removed). Smaller, broadly available.
_FALLBACK_EMBEDDING_MODEL = "nomic-embed-text"
_DEFAULT_URL = "http://localhost:11434"
_DEFAULT_DIM = 1024  # Ollama qwen3:0.6b default dimension
_DEFAULT_TIMEOUT = 10.0
_WARN_INTERVAL = 60
_HEALTH_TIMEOUT = 30
_DOCKER_CONTAINER = OLLAMA_DOCKER_CONTAINER
_DOCKER_IMAGE = "ollama/ollama:latest"
_HEALTH_CHECK_RETRIES = 5
_HEALTH_CHECK_INTERVAL_SECS = 2
_VALID_AUTH_SCHEMES = frozenset({"none", "aws_sigv4"})


_needs_sudo_cache: bool = False


def _sigv4_sign(method: str, url: str, headers: dict, body: bytes | str) -> dict | None:
    """Return headers with AWS SigV4 auth for API Gateway.

    Returns None (and logs) on any failure so callers abort the request rather
    than sending unsigned. Reads AWS_REGION / KIROCREW_EMBEDDING_SERVICE env vars.
    """
    if not _HAS_BOTOCORE:
        logger.warning("botocore not installed; cannot SigV4-sign embedding requests")
        return None
    try:
        service = os.environ.get("KIROCREW_EMBEDDING_SERVICE", "execute-api")
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            logger.warning(
                "AWS_REGION/AWS_DEFAULT_REGION not set; defaulting to us-east-1 "
                "for SigV4 signing — expect 403s if the API Gateway is in another region"
            )
            region = "us-east-1"
        session = _get_botocore_session()
        if session is None:
            return None
        creds = session.get_credentials()
        if creds is None:
            logger.warning("No AWS credentials for SigV4 signing")
            return None
        if isinstance(body, str):
            body = body.encode()
        req = AWSRequest(method=method, url=url, data=body, headers=dict(headers))
        SigV4Auth(creds.get_frozen_credentials(), service, region).add_auth(req)
        return dict(req.headers)
    except Exception as e:
        logger.warning("SigV4 signing failed: %s", e)
        return None


@functools.lru_cache(maxsize=1)
def _get_botocore_session():
    """Cached botocore Session — reused across sign calls to avoid re-parsing
    config files and re-resolving the credential chain each request.
    botocore's resolver handles temporary-credential refresh internally.
    Returns None if botocore is not installed (matches _sigv4_sign optional-dep guard).
    """
    if not _HAS_BOTOCORE:
        return None
    return BotocoreSession()


def _persist_embedding_runtime(runtime: str) -> None:
    """Write embedding_runtime to config.json so new OllamaManager instances remember it."""
    path = config_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        logger.warning("Could not read config file; skipping runtime persistence")
        return
    try:
        data.setdefault("memory", {})["embedding_runtime"] = runtime
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception:
        logger.warning("Could not write config file; skipping runtime persistence")


def _resolve_blocked_addr(host: str) -> str | None:
    """Return the internal/metadata address *host* IS, or None. PURE / NON-BLOCKING.

    Performs **no DNS** — nothing here may run on the event loop and block. Only
    IP *literals* are inspected: if *host* is a literal IP that is private /
    loopback / link-local / reserved / multicast / unspecified, its string form
    is returned. This covers the IMDS endpoints (``169.254.169.254`` and
    ``fd00:ec2::254``), all RFC1918 space (``10/8``, ``172.16/12``,
    ``192.168/16``), ``127/8`` and ``::1``. IPv4 addresses mapped into IPv6
    (``::ffff:a.b.c.d``) are unwrapped so a mapped internal address cannot slip
    through.

    Returns None when *host* is a public IP literal **or** a DNS name — a name
    is deliberately *not* resolved here (blocking ``getaddrinfo`` on the loop is
    forbidden), so the DNS-based range check is skipped and the residual
    name-based / DNS-rebinding TOCTOU (a name pointing at a private/metadata
    address at request time) is an accepted risk.
    """
    # Drop any IPv6 zone/scope id (e.g. ``fe80::1%eth0``) before parsing, and
    # handle the bracket-less IPv6 ``urlparse`` hands back for ``https://[::1]``.
    addr_clean = host.split("%", 1)[0]
    # Strip a trailing dot (fully-qualified form, e.g. ``169.254.169.254.`` /
    # ``127.0.0.1.``). Both ``ipaddress.ip_address`` and ``socket.inet_aton``
    # reject a trailing-dot literal, so without this it would fall through as a
    # DNS name and ``_validate_url(allow_remote=True)`` would accept the IMDS /
    # loopback target (SSRF). The kernel/resolver treats the FQDN form as the
    # same address, so normalize it here before parsing (still **no DNS**).
    addr_clean = addr_clean.rstrip(".")
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(addr_clean)
    except ValueError:
        # ``ip_address`` only accepts the canonical dotted-quad / RFC-5952 forms,
        # so alternate IPv4 literal encodings — hex (``0x7f000001``), decimal
        # (``2130706433``), octal (``017700000001``), short-form (``127.1``) and
        # the IMDS variants (``0xa9fea9fe`` / ``2852039166`` / ``169.254.43518``)
        # — would fall through here as if they were DNS names and let aiohttp
        # connect straight to loopback/IMDS. ``inet_aton`` performs the same
        # permissive parse the C resolver / kernel would, so normalize through it
        # (pure string parse, **no DNS**) before deciding this is a name.
        try:
            packed = socket.inet_aton(addr_clean)
        except OSError:
            # Genuine DNS name (inet_aton rejects it). No on-loop resolution;
            # caller relies on the accepted name-based DNS-rebinding TOCTOU residual.
            return None
        except Exception:
            # Any other parse failure: fail CLOSED — treat as blocked.
            return addr_clean
        ip = ipaddress.IPv4Address(packed)
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return addr_clean
    return None


def _validate_url(url: str, *, allow_remote: bool = False) -> None:
    """Reject non-localhost embedding URLs unless explicitly configured.

    Even when ``allow_remote`` is set (and the URL is https), an IP-*literal*
    host is rejected if it is a private / loopback / link-local / reserved /
    metadata address (169.254.169.254, RFC1918, ::1, 127/8, fd00:ec2::254).
    This closes the SSRF gap where an owner-configured or attacker-influenced
    remote URL could target the instance metadata service or other internal
    endpoints — **without any DNS lookup**, so nothing here blocks the event
    loop. A malformed / empty host is denied (fail-closed). A DNS *name* is not
    resolved here; the residual name-based / DNS-rebinding TOCTOU is accepted.
    """
    if "@" in url or "token=" in url.lower():
        sel().log_tool_invocation(
            session_key="embedding_url_validation",
            tool_name="_validate_url",
            outcome="rejected_credentials",
            metadata={"reason": "credentials_in_url"},
        )
        raise ValueError("Embedding URL must not contain credentials")
    # Fail-closed: any parse error (malformed URL) denies rather than allows.
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
    except ValueError as exc:
        sel().log_tool_invocation(
            session_key="embedding_url_validation",
            tool_name="_validate_url",
            outcome="rejected_malformed",
            resources=url,
            metadata={"reason": "parse_error"},
        )
        raise ValueError(f"Embedding URL {url!r} is malformed; refusing (SSRF protection).") from exc
    if not host:
        # No parseable host -> nothing safe to allow. Deny (fail-closed).
        sel().log_tool_invocation(
            session_key="embedding_url_validation",
            tool_name="_validate_url",
            outcome="rejected_malformed",
            resources=url,
            metadata={"reason": "empty_host"},
        )
        raise ValueError(f"Embedding URL {url!r} has no host; refusing (SSRF protection).")
    if host not in ("localhost", "127.0.0.1", "::1"):
        if not allow_remote:
            sel().log_tool_invocation(
                session_key="embedding_url_validation",
                tool_name="_validate_url",
                outcome="rejected_remote",
                resources=url,
                metadata={"host": host},
            )
            raise ValueError(
                f"Embedding URL must be localhost, got {host!r}. "
                "Set embedding_url in ~/.kirocrew/config.json to allow a remote server."
            )
        if parsed.scheme == "http":
            sel().log_tool_invocation(
                session_key="embedding_url_validation",
                tool_name="_validate_url",
                outcome="rejected_http_remote",
                resources=url,
                metadata={"host": host, "scheme": "http"},
            )
            raise ValueError(
                "Remote embedding URLs must use https:// to prevent data leaks. " f"Got {url!r}"
            )
        blocked = _resolve_blocked_addr(host)
        if blocked is not None:
            sel().log_tool_invocation(
                session_key="embedding_url_validation",
                tool_name="_validate_url",
                outcome="rejected_internal_addr",
                resources=url,
                metadata={"host": host, "blocked": blocked},
            )
            raise ValueError(
                f"Remote embedding URL {host!r} is an internal address "
                f"{blocked!r}; refusing to send embedding data to a private, "
                "loopback, link-local, or metadata endpoint (SSRF protection)."
            )
        sel().log_tool_invocation(
            session_key="embedding_url_validation",
            tool_name="_validate_url",
            outcome="allow_remote",
            resources=url,
            metadata={"host": host},
        )


async def _ollama_has_model(url: str, model: str, timeout_s: int = 3) -> bool:
    """Check if Ollama at *url* has *model* loaded."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as s:
            async with s.get(f"{url}/api/tags") as r:
                if r.status != 200:
                    return False
                data = await r.json()
                models = [m.get("name", "") for m in data.get("models", [])]
                prefix = model.split(":")[0] + ":"
                return any(m.startswith(prefix) or m == model for m in models)
    except Exception:
        return False


class EmbeddingClient:
    """Async HTTP client for a local Ollama embedding server."""

    def __init__(
        self,
        url: str = _DEFAULT_URL,
        dim: int = _DEFAULT_DIM,
        timeout: float = _DEFAULT_TIMEOUT,
        allow_remote: bool = False,
        model: str | None = None,
        auth: str = "none",
    ):
        # Model + endpoint are sourced from the active PlatformContext's
        # EmbeddingSource when the caller doesn't pin them.  The Default adapter
        # returns ``_OLLAMA_MODEL`` and a ``None`` endpoint, so a standalone
        # client with no explicit model/url is byte-for-byte today's local
        # Ollama client.  An explicit ``model=`` / ``url=`` from a caller (e.g.
        # ``cfg.memory.embedding_model``) always wins.  The Amazon companion
        # supplies its internal model + remote endpoint.
        model, ctx_endpoint = self._resolve_model_endpoint(model)
        # Only adopt a context endpoint that is a non-empty string AND only when
        # the caller did not pin a non-default url.  ``str.strip()`` guards the
        # empty-string-treated-as-None footgun (an EmbeddingSource returning ""
        # must mean "no endpoint", not "remote endpoint ''").  A context-supplied
        # endpoint is trusted-by-edition (the companion vouches for its own
        # internal host), so it implies allow_remote — this is the edition trust
        # boundary, not a caller override of a security gate.
        if isinstance(ctx_endpoint, str) and ctx_endpoint.strip() and url == _DEFAULT_URL:
            url = ctx_endpoint
            allow_remote = True
        _validate_url(url, allow_remote=allow_remote)
        self._url = url.rstrip("/")
        self.dim = dim
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._model = model
        if auth not in _VALID_AUTH_SCHEMES:
            raise ValueError(
                f"Unknown embedding auth scheme {auth!r}; expected one of {_VALID_AUTH_SCHEMES}"
            )
        self._auth = auth
        self._last_warn: float = 0

    @staticmethod
    def _resolve_model_endpoint(model: str | None) -> tuple[str, str | None]:
        """Resolve (model, endpoint) from the active PlatformContext.

        An explicit ``model`` from the caller always wins; only ``None`` falls
        through to ``current_context().embeddings.registry_model()`` (Default:
        ``_OLLAMA_MODEL``).  The endpoint is ``endpoint_url()`` (Default: None).
        Best-effort: a transient context failure degrades to today's local
        defaults rather than breaking embedding — but a ``PlatformCompositionError``
        is re-raised (fail-closed) so a non-standalone host that cannot compose
        does NOT silently point internal data at the public Ollama default.
        """

        def _resolve() -> tuple[str, str | None]:
            src = current_context().embeddings
            # Resolve both fields before committing either: registry_model() and
            # endpoint_url() must land atomically.  If endpoint_url() raises after
            # registry_model() already returned an edition model, committing the
            # model alone would point an amazon-specific model at the local Ollama
            # default endpoint (silent embed failures).
            resolved_model = src.registry_model() if model is None else model
            return resolved_model, src.endpoint_url()

        # Widen the fallback's declared type to a supertype of _resolve's
        # (str, str | None) return so safe_context_call's TypeVar binds to it and
        # accepts _resolve as a subtype. The bare literal (model, None) would
        # otherwise infer tuple[str | None, None] and clash with _resolve.
        fallback: tuple[str | None, str | None] = (model, None)
        resolved_model, endpoint = safe_context_call(
            _resolve,
            fallback=fallback,
            log_message="embeddings source lookup failed; using local defaults",
        )
        if resolved_model is None:
            resolved_model = _OLLAMA_MODEL
        return resolved_model, endpoint

    def _context_sign(self, method: str, url: str, headers: dict, body: bytes | str) -> dict | None:
        """Sign *url* via the active PlatformContext's EmbeddingSource, or None.

        The Default adapter returns None (unsigned local Ollama), so standalone
        falls through to the ``auth=="aws_sigv4"`` path in ``embed_batch``
        unchanged.  The Amazon companion returns signed headers.  Best-effort:
        any transient failure returns None so the caller's existing path takes
        over — but a ``PlatformCompositionError`` is re-raised (fail-closed).
        """
        return safe_context_call(
            lambda: current_context().embeddings.sign_request(method, url, headers, body),
            fallback=None,
            log_message="embeddings sign_request via context failed",
        )

    async def embed_one(self, text: str) -> list[float] | None:
        """Embed a single text. Returns None on any error."""
        result = await self.embed_batch([text])
        return result[0] if result else None

    async def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Embed multiple texts via Ollama API. Returns None on any error."""
        try:
            endpoint = f"{self._url}/api/embed"
            body = json.dumps({"model": self._model, "input": texts}).encode()
            headers = {"Content-Type": "application/json"}
            # Request signing is routed through the active PlatformContext's
            # EmbeddingSource.  The Default adapter returns None (unsigned), so a
            # standalone process with auth="none" sends the request exactly as
            # before, and a standalone process with auth="aws_sigv4" still uses
            # the local ``_sigv4_sign`` fallback below — byte-for-byte today's
            # behavior.  The Amazon companion returns SigV4 headers from its own
            # signer so it does not need ``embedding_auth`` configured.
            ctx_signed = self._context_sign("POST", endpoint, dict(headers), body)
            if ctx_signed is not None:
                headers = ctx_signed
            elif self._auth == "aws_sigv4":
                signed = _sigv4_sign("POST", endpoint, headers, body)
                if signed is None:
                    self._warn("SigV4 signing failed; aborting embed request")
                    return None
                headers = signed
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(endpoint, data=body, headers=headers) as resp:
                    if resp.status != 200:
                        self._warn("Ollama returned status %d", resp.status)
                        return None
                    data = await resp.json()
                    embeddings = data.get("embeddings")
                    if isinstance(embeddings, list) and len(embeddings) == len(texts):
                        logger.debug(
                            "Embedded %d texts, dim=%d, first=%s…",
                            len(texts),
                            len(embeddings[0]) if embeddings else 0,
                            texts[0][:60],
                        )
                        return embeddings
                    self._warn("Unexpected Ollama response shape: %s", type(data))
                    return None
        except Exception as e:
            self._warn("Embedding request failed: %s", e)
            return None

    async def health(self) -> bool:
        """Check if Ollama is running and the model is available."""
        return await _ollama_has_model(
            self._url, self._model, timeout_s=int(self._timeout.total or 3)
        )

    def _warn(self, msg: str, *args: object) -> None:
        now = time.monotonic()
        if now - self._last_warn >= _WARN_INTERVAL:
            logger.warning(msg, *args)
            self._last_warn = now


class OllamaManager:
    """Manages the local Ollama server and model lifecycle.

    The embedding model is pulled from the public Ollama registry via
    ``ollama pull`` (default ``qwen3-embedding:0.6b``).
    """

    def __init__(
        self, url: str = _DEFAULT_URL, base_dir: Path | None = None, model: str = _OLLAMA_MODEL
    ):
        self._url = url.rstrip("/")
        self._base = base_dir or config_dir()
        self._model = model
        self._process: asyncio.subprocess.Process | None = None
        # Read persisted runtime preference (set after GLIBC fallback)
        try:
            cfg = KiroCrewConfig.load()
            self._use_docker = cfg.memory.embedding_runtime == "docker"
        except Exception:
            self._use_docker = False
        self._needs_sudo = _needs_sudo_cache
        self._ollama_binary_override: str | None = None
        self._brew_bin_dir: str | None = None

    @property
    def ollama_binary(self) -> str | None:
        return self._ollama_binary_override or shutil.which("ollama")

    def _docker_bin(self) -> str | None:
        """Return docker binary path, or None."""
        return shutil.which("docker")

    async def _run_docker(self, *args: str, timeout: int = 60) -> tuple[int, str]:
        """Run a docker command, using sudo if needed. Returns (returncode, output).

        On success the second element is stdout; on failure it is stderr.
        """
        docker = self._docker_bin()
        if not docker:
            return 1, "docker not found"
        # Use sudo if previously detected as needed
        if self._needs_sudo:
            return await self._sudo_docker(*args, timeout=timeout)
        # Try without sudo first
        proc = await asyncio.create_subprocess_exec(
            docker,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            return 0, stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace")
        # Permission denied → retry with sudo
        if "permission denied" in err.lower() or "connect:" in err.lower():
            global _needs_sudo_cache
            logger.debug("Docker permission denied, retrying with sudo")
            self._needs_sudo = True
            _needs_sudo_cache = True
            return await self._sudo_docker(*args, timeout=timeout)
        return proc.returncode or 1, err

    async def _sudo_docker(self, *args: str, timeout: int = 60) -> tuple[int, str]:
        """Run docker with sudo. Returns (returncode, stdout) on success, (rc, stderr) on failure."""
        docker = self._docker_bin() or "docker"
        cmd_parts = [docker] + list(args)
        cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)
        full_cmd = f"sudo {cmd_str}"
        logger.debug("Running: %s", full_cmd)
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode or 0
        if rc != 0:
            err = stderr.decode(errors="replace").strip()
            logger.warning("sudo docker failed (rc=%d): %s", rc, err[:300])
            return rc, err
        return 0, stdout.decode(errors="replace").strip()

    async def is_server_running(self) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self._url}/api/tags") as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def model_available(self) -> bool:
        return await _ollama_has_model(self._url, self._model)

    async def install_ollama(self) -> bool:
        """Install Ollama via platform package manager. Docker fallback if _use_docker is set.

        Windows: local Ollama auto-install is not yet supported (only the
        brew/curl/docker branches below). Vector memory still works on Windows
        via a remote embedding endpoint or Docker; only the local auto-install
        is missing. Tracked in Mesh-2364 (https://taskei.amazon.dev/tasks/Mesh-2364).
        """

        if self._use_docker:
            return await self._install_docker_ollama()

        system = platform.system()
        logger.info("Installing Ollama on %s...", system)
        try:
            if system == "Darwin":
                if shutil.which("brew"):
                    # On Apple Silicon, try arch -arm64 first to handle Rosetta 2 emulation,
                    # then fall back to bare brew for Intel Macs where arch -arm64 fails.
                    # Use sysctl to detect native ARM and skip the arch prefix on Intel.
                    is_arm = False
                    sysctl = None
                    try:
                        sysctl = await asyncio.create_subprocess_exec(
                            "sysctl",
                            "-n",
                            "hw.optional.arm64",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        out, _ = await asyncio.wait_for(sysctl.communicate(), timeout=10)
                        is_arm = out.strip() == b"1"
                    except (OSError, asyncio.TimeoutError):
                        if sysctl is not None:
                            try:
                                sysctl.kill()
                            except (ProcessLookupError, OSError):
                                pass
                    cmds = (
                        [
                            ["arch", "-arm64", "brew", "install", "ollama"],
                            ["brew", "install", "ollama"],
                        ]
                        if is_arm
                        else [["brew", "install", "ollama"]]
                    )
                    for i, cmd in enumerate(cmds):
                        proc = None
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                *cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            stdout_data, stderr_data = await asyncio.wait_for(
                                proc.communicate(), timeout=1200
                            )
                            if proc.returncode == 0:
                                return True
                            stderr_tail = (
                                stderr_data.decode(errors="replace").strip().splitlines()[-5:]
                            )
                            if i < len(cmds) - 1:
                                logger.warning(
                                    "Command %s exited with code %s: %s, trying next option...",
                                    cmd,
                                    proc.returncode,
                                    "\n".join(stderr_tail),
                                )
                            else:
                                logger.warning(
                                    "Command %s exited with code %s: %s",
                                    cmd,
                                    proc.returncode,
                                    "\n".join(stderr_tail),
                                )
                        except (asyncio.TimeoutError, OSError) as exc:
                            if i < len(cmds) - 1:
                                logger.warning(
                                    "Failed running %s: %s, trying next option...", cmd, exc
                                )
                            else:
                                logger.warning("Failed running %s: %s", cmd, exc)
                            if isinstance(exc, asyncio.TimeoutError) and proc is not None:
                                try:
                                    proc.kill()
                                except (ProcessLookupError, OSError):
                                    pass
                                try:
                                    await asyncio.wait_for(proc.wait(), timeout=5)
                                except asyncio.TimeoutError:
                                    logger.warning("Process %s did not exit after kill", cmd[0])
                    return False
                # No brew — install via official macOS binary
                logger.info("Homebrew not found, installing Ollama via direct download...")
                proc = await asyncio.create_subprocess_shell(
                    "curl -fsSL -o /tmp/ollama https://ollama.com/download/ollama-darwin"
                    " && chmod +x /tmp/ollama"
                    " && sudo mkdir -p /usr/local/bin"
                    " && sudo mv /tmp/ollama /usr/local/bin/ollama",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=1200)
                return proc.returncode == 0
            elif system == "Linux":
                # Prefer brew (avoids glibc issues on AL2)
                if shutil.which("brew"):
                    proc = await asyncio.create_subprocess_exec(
                        "brew",
                        "install",
                        "ollama",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1200)
                    if proc.returncode == 0:
                        return True
                    logger.warning(
                        "brew install ollama failed (rc=%d): %s; trying curl fallback",
                        proc.returncode,
                        stderr.decode(errors="replace").strip(),
                    )
                # Fallback: official install script
                proc = await asyncio.create_subprocess_shell(
                    "curl -fsSL https://ollama.com/install.sh | sh",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=1200)
                if proc.returncode == 0:
                    # The install script creates a systemd service that conflicts with
                    # KiroCrew's own Ollama process management. Disable it.
                    disable = await asyncio.create_subprocess_shell(
                        "systemctl disable --now ollama 2>/dev/null || true",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(disable.communicate(), timeout=30)
                return proc.returncode == 0
            else:
                logger.error("Unsupported platform: %s", system)
                return False
        except Exception:
            logger.exception("Ollama install failed")
            return False

    async def _install_docker_ollama(self) -> bool:
        """Install Docker if needed and pull the Ollama image (AL2 fallback)."""
        docker = shutil.which("docker")
        if not docker:
            logger.info("Docker not found, attempting install...")
            try:
                # AL2: use amazon-linux-extras; AL2023: use dnf
                for cmd in (
                    "amazon-linux-extras install docker -y",
                    "yum install -y docker",
                ):
                    proc = await asyncio.create_subprocess_shell(
                        f"sudo {cmd}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
                    if proc.returncode == 0:
                        break
                else:
                    logger.error(
                        "Failed to install Docker. Install manually: sudo yum install docker"
                    )
                    return False
                # Start Docker service and add user to group (takes effect on next login)
                proc = await asyncio.create_subprocess_shell(
                    "sudo systemctl start docker && sudo usermod -aG docker $USER",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=30)
                # Group change won't take effect until re-login, so use sudo for this session
                global _needs_sudo_cache  # noqa: F811
                self._needs_sudo = True
                _needs_sudo_cache = True
            except Exception:
                logger.exception("Docker install failed")
                return False
            docker = shutil.which("docker")
            if not docker:
                logger.error("Docker still not in PATH after install")
                return False

        # Pull Ollama image
        logger.info("Pulling Ollama Docker image...")
        try:
            rc, err = await self._run_docker("pull", _DOCKER_IMAGE, timeout=600)
            return rc == 0
        except Exception:
            logger.exception("Failed to pull Ollama Docker image")
            return False

    async def _detect_docker_runtime(self) -> None:
        """Set _use_docker if the running server is our Docker container."""
        if not self._use_docker and self._docker_bin():
            try:
                rc, out = await self._run_docker(
                    "inspect", "-f", "{{.State.Running}}", _DOCKER_CONTAINER, timeout=5
                )
                if rc == 0 and "true" in out.lower():
                    self._use_docker = True
                    _persist_embedding_runtime("docker")
            except Exception:
                logger.debug("Could not detect Docker container; assuming native")

    async def start_server(self) -> bool:
        # Quick check — if server is already responding, return immediately
        if await self.is_server_running():
            await self._detect_docker_runtime()
            return True

        # Retry only when a running Docker container exists (it may still be initializing)
        if self._docker_bin():
            try:
                rc, out = await self._run_docker(
                    "inspect", "-f", "{{.State.Running}}", _DOCKER_CONTAINER, timeout=5
                )
                container_running = rc == 0 and "true" in out.lower()
            except Exception:
                container_running = False

            if container_running:
                for _attempt in range(_HEALTH_CHECK_RETRIES - 1):
                    await asyncio.sleep(_HEALTH_CHECK_INTERVAL_SECS)
                    if await self.is_server_running():
                        await self._detect_docker_runtime()
                        return True

        if self._use_docker:
            return await self._start_docker_server()

        binary = self.ollama_binary
        if not binary:
            logger.info("Ollama not found, attempting install...")
            if await self.install_ollama():
                binary = self.ollama_binary
        if not binary:

            system = platform.system()
            if system == "Darwin":
                logger.error("Ollama not found. Install with: brew install ollama")
            else:
                logger.error(
                    "Ollama not found. Install with: brew install ollama "
                    "or: curl -fsSL https://ollama.com/install.sh | sh"
                )
            return False
        logger.info("Starting Ollama server...")
        try:
            # Process-group isolation so _stop() can tree-kill. Pass both flags
            # EXPLICITLY (not via **dict unpack, which breaks mypy's Popen overload
            # resolution on the build fleet): start_new_session=True is silently
            # ignored on Windows, and creationflags resolves to 0 (no-op) on POSIX.
            self._process = await asyncio.create_subprocess_exec(
                binary,
                "serve",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **os.environ,
                    "OLLAMA_HOST": "127.0.0.1:11434",
                    **(
                        {"PATH": f"{self._brew_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
                        if getattr(self, "_brew_bin_dir", None)
                        else {}
                    ),
                },
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            for _ in range(_HEALTH_TIMEOUT):
                await asyncio.sleep(1)
                if self._process.returncode is not None:
                    # Process exited — capture stderr for diagnostics
                    _, stderr = await self._process.communicate()
                    err_msg = stderr.decode(errors="replace")[:500] if stderr else "no output"
                    # Check for GLIBC version mismatch (common on AL2)
                    if "GLIBC" in err_msg or "GLIBCXX" in err_msg:
                        self._process = None
                        if shutil.which("brew") and not getattr(self, "_glibc_retried", False):
                            self._glibc_retried = True
                            logger.warning(
                                "Ollama native binary requires newer glibc. "
                                "Attempting reinstall via brew..."
                            )
                            if await self.install_ollama():
                                # Set full path to brew ollama binary (no global PATH mutation)
                                brew_proc = await asyncio.create_subprocess_exec(
                                    "brew",
                                    "--prefix",
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                )
                                stdout, _ = await brew_proc.communicate()
                                if brew_proc.returncode == 0:
                                    brew_bin = os.path.join(stdout.decode().strip(), "bin")
                                    self._brew_bin_dir = brew_bin
                                    self._ollama_binary_override = os.path.join(brew_bin, "ollama")
                                return await self.start_server()
                        logger.warning("Falling back to Docker...")
                        self._use_docker = True
                        _persist_embedding_runtime("docker")
                        return await self._start_docker_server()
                    else:
                        logger.error("Ollama exited during startup: %s", err_msg)
                    self._process = None
                    # Check if ollama is already running system-wide
                    if await self.is_server_running():
                        logger.info("Ollama already running (system-wide)")
                        return True
                    return False
                if await self.is_server_running():
                    logger.info("Ollama server ready")
                    return True
            logger.warning("Ollama did not become healthy within %ds", _HEALTH_TIMEOUT)
            return False
        except Exception:
            logger.exception("Failed to start Ollama server")
            self._process = None
            return False

    async def _start_docker_server(self) -> bool:
        """Start Ollama via Docker container (AL2 glibc fallback)."""
        if not self._docker_bin():
            logger.info("Docker not found, attempting install...")
            if not await self._install_docker_ollama():
                return False
            if not self._docker_bin():
                return False

        # Check if container already exists
        try:
            rc, _ = await self._run_docker("inspect", _DOCKER_CONTAINER, timeout=10)
            if rc == 0:
                # Container exists — remove and recreate for clean state
                logger.info("Removing existing Ollama container...")
                await self._run_docker("rm", "-f", _DOCKER_CONTAINER, timeout=10)

            logger.info("Creating Ollama Docker container (sudo=%s)...", self._needs_sudo)
            rc, err = await self._run_docker(
                "run",
                "-d",
                "--name",
                _DOCKER_CONTAINER,
                "-p",
                "11434:11434",
                "-v",
                "kirocrew-ollama:/root/.ollama",
                _DOCKER_IMAGE,
                timeout=60,
            )
            if rc != 0:
                logger.error("Docker run failed (rc=%d): %s", rc, err[:300])
                return False
            logger.info("Docker container created successfully")
        except Exception:
            logger.exception("Failed to start Ollama Docker container")
            return False

        # Wait for health
        for _ in range(_HEALTH_TIMEOUT):
            await asyncio.sleep(1)
            if await self.is_server_running():
                logger.info("Ollama Docker container ready")
                return True
        logger.warning("Ollama Docker container did not become healthy within %ds", _HEALTH_TIMEOUT)
        return False

    def _log_pull_failure(self, detail: str) -> None:
        """Log an actionable message when ``ollama pull`` fails.

        Names the documented fallback model so the failure isn't silently
        swallowed — a public user with no network/registry access for the
        configured model can switch to ``nomic-embed-text`` (dim 768).
        """
        msg = (
            "Could not pull embedding model %r (%s). "
            "Memory/embeddings will be disabled until a model is available. "
            "To recover, set ``embedding_model`` to the documented fallback "
            "%r in ~/.kirocrew/config.json (or run ``ollama pull %s`` manually)."
        )
        if self._model == _FALLBACK_EMBEDDING_MODEL:
            # Already on the fallback — pointing at itself would be unhelpful.
            logger.error(
                "Could not pull embedding model %r (%s). "
                "Memory/embeddings will be disabled. Verify Ollama has network "
                "access to the public registry and try ``ollama pull %s`` manually.",
                self._model,
                detail,
                self._model,
            )
            return
        logger.error(msg, self._model, detail, _FALLBACK_EMBEDDING_MODEL, _FALLBACK_EMBEDDING_MODEL)

    async def pull_model(self) -> bool:
        """Pull the embedding model from the public Ollama registry.

        Runs ``ollama pull <model>`` (default ``qwen3-embedding:0.6b``). On
        failure, logs a clear actionable message naming the documented fallback
        ``nomic-embed-text`` (dim 768) rather than failing silently.
        """
        if await self.model_available():
            logger.info("Model %s already available", self._model)
            return True

        logger.info("Pulling model %s from the public Ollama registry...", self._model)

        # Docker runtime — pull inside the container.
        if self._use_docker:
            try:
                rc, err = await self._run_docker(
                    "exec", _DOCKER_CONTAINER, "ollama", "pull", self._model, timeout=1800
                )
                if rc != 0:
                    self._log_pull_failure(f"docker pull rc={rc}: {err[:300]}")
                    return False
                logger.info("Model %s pulled in Docker container", self._model)
                return True
            except Exception as e:
                self._log_pull_failure(f"docker pull raised: {e}")
                logger.debug("Docker 'ollama pull' traceback", exc_info=True)
                return False

        # Native binary path.
        binary = self.ollama_binary
        if not binary:
            logger.error("Ollama not found; cannot pull model %s", self._model)
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "pull",
                self._model,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
            if proc.returncode != 0:
                self._log_pull_failure(
                    f"rc={proc.returncode}: {stderr.decode(errors='replace')[-300:]}"
                )
                return False
            logger.info("Model %s pulled from public Ollama registry", self._model)
            return True
        except Exception as e:
            self._log_pull_failure(f"pull raised: {e}")
            logger.debug("ollama pull traceback", exc_info=True)
            return False

    async def ensure_running(self) -> bool:
        """Start server and load model if needed. Returns True when ready."""
        if not await self.start_server():
            return False
        if not await self.pull_model():
            return False
        return True

    async def stop(self) -> None:
        """Stop the Ollama server — kills our subprocess, Docker container, or orphan."""
        # Docker mode — stop the container
        if self._use_docker:
            try:
                await self._run_docker("stop", _DOCKER_CONTAINER, timeout=10)
                logger.info("Stopped Ollama Docker container")
            except Exception:
                pass
            return

        # Kill our subprocess if we spawned it
        if self._process and self._process.returncode is None:
            pid = self._process.pid
            logger.info("Stopping Ollama server (pid %d)", pid)
            try:
                # killpg(getpgid) on POSIX, taskkill /T on Windows — via
                # platform_compat (os.getpgid/os.killpg are POSIX-only).
                platform_compat.kill_process_tree(pid, platform_compat.SIGTERM)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    platform_compat.kill_process_tree(pid, platform_compat.SIGKILL)
                    await self._process.wait()
            except (ProcessLookupError, OSError):
                pass
            self._process = None
            return

        # No subprocess reference — skip orphan kill.
        # We only kill Ollama processes we spawned (tracked via self._process).
        # A blanket pkill would kill Ollama instances owned by other tools.
        logger.debug("No Ollama subprocess to stop (not spawned by us)")

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None


# 128 entries × 1024 floats/entry × 32 bytes/float (24-byte object + 8-byte ptr) ≈ 4 MB.
_EMBED_CACHE_MAX = 128


class _EmbedFailed(Exception):
    """Raised to prevent lru_cache from caching failed embedding attempts."""


def make_sync_embed_fn(
    url: str = _DEFAULT_URL,
    timeout: float = 3.0,
    model: str = _OLLAMA_MODEL,
    auth: str = "none",
) -> callable:  # type: ignore[valid-type]
    """Return a sync callable ``(str) -> list[float] | None`` for Ollama embeddings.

    Successful results are cached via ``functools.lru_cache`` keyed by input
    text. Same text always produces the same embedding vector for a given
    model, so caching is safe. Bounded to ``_EMBED_CACHE_MAX`` entries (see
    constant for size math). Failures (None) are not cached so transient
    errors are retried. Cache resets when a new callable is created (embedding
    disable/re-enable or gateway restart).

    Model + endpoint + signing are sourced from the active PlatformContext's
    EmbeddingSource — the SAME wiring the async ``EmbeddingClient`` uses — so the
    sync vector-memory path does not diverge by edition.  The Default adapter
    returns ``_OLLAMA_MODEL``, a ``None`` endpoint, and ``None`` signing, so a
    standalone process is byte-for-byte today's local Ollama client (the
    ``auth=="aws_sigv4"`` legacy fallback is preserved below).  The Amazon
    companion supplies its internal model, remote endpoint, and SigV4 signer.
    """

    # Resolve model + endpoint through the context (re-using the async client's
    # atomic resolver), then anchor the embed URL on the resolved endpoint.
    model, ctx_endpoint = EmbeddingClient._resolve_model_endpoint(model if model else None)
    if ctx_endpoint and url == _DEFAULT_URL:
        url = ctx_endpoint
    embed_url = f"{url.rstrip('/')}/api/embed"

    if auth not in _VALID_AUTH_SCHEMES:
        raise ValueError(
            f"Unknown embedding auth scheme {auth!r}; expected one of {_VALID_AUTH_SCHEMES}"
        )

    @functools.lru_cache(maxsize=_EMBED_CACHE_MAX)
    def _cached_embed(text: str) -> tuple[float, ...]:
        info = _cached_embed.cache_info()
        if info.misses % 20 == 0:
            logger.info(
                "Embedding cache: hits=%d misses=%d size=%d/%d",
                info.hits,
                info.misses,
                info.currsize,
                info.maxsize,
            )
        try:
            body = json.dumps({"model": model, "input": [text]}).encode()
            headers = {"Content-Type": "application/json"}
            # Sign through the context first (companion SigV4 signer); the
            # Default returns None so standalone falls through to the legacy
            # ``aws_sigv4`` path unchanged.
            ctx_signed = safe_context_call(
                lambda: current_context().embeddings.sign_request(
                    "POST", embed_url, dict(headers), body
                ),
                fallback=None,
                log_message="sync embeddings sign_request via context failed",
            )
            if ctx_signed is not None:
                headers = ctx_signed
            elif auth == "aws_sigv4":
                signed = _sigv4_sign("POST", embed_url, headers, body)
                if signed is None:
                    raise _EmbedFailed
                headers = signed
            req = urllib.request.Request(embed_url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                embeddings = data.get("embeddings", [])
                if embeddings and len(embeddings) == 1:
                    return tuple(embeddings[0])
                logger.warning(
                    "Ollama returned 200 but unexpected embeddings " "(count=%d, text_len=%d)",
                    len(embeddings),
                    len(text),
                )
        except Exception:
            logger.debug("Embed request failed", exc_info=True)
        raise _EmbedFailed

    def _embed(text: str) -> list[float] | None:
        try:
            return list(_cached_embed(text))
        except _EmbedFailed:
            return None

    return _embed
