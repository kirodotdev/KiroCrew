"""Embedding backend that calls an Ollama server's ``/api/embed`` endpoint.

Selected with ``memory.embedding_provider = "ollama_http"``. Vector memory and the
knowledge library then embed through the server named by ``memory.ollama_url``
instead of the bundled in-process model, so a machine without the RAM for the
local model, or one that already runs Ollama on a GPU, can still embed.

The retired value ``"ollama"`` still coerces to ``"llama_cpp"``: configs carried
over from older releases hold it, and honouring it here would move those
installs onto a server they may not run.

Data boundary: memory and knowledge text is sent to the server. A loopback URL
keeps it on this machine. Any other host needs ``memory.ollama_allow_remote``
AND an ``https`` URL, so text never crosses a network in the clear or without an
explicit opt-in. Redirects are refused, and a loopback URL bypasses proxy
environment variables, so a request never lands somewhere other than the
configured endpoint.

Authentication: ``memory.ollama_auth = "aws_sigv4"`` signs every request, the
readiness probe included, with AWS Signature Version 4, for a server behind an
IAM-authorized endpoint such as API Gateway. Credentials come from botocore's
default chain (environment, shared config and SSO, ``credential_process``,
instance role), optionally for the profile named by ``memory.ollama_aws_profile``;
config holds only names, never a secret. A request that cannot be signed is not
sent.
"""

from __future__ import annotations

import importlib
import importlib.util
import ipaddress
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import IO, Any, Callable

from kiro_crew.config.memory_sections import (
    OLLAMA_AUTH_SCHEMES,
    OLLAMA_DEFAULT_AWS_SERVICE,
    OLLAMA_DEFAULT_URL,
    OLLAMA_HTTP_PROVIDER,
)
from kiro_crew.embeddings_llama import EmbeddingBackend
from kiro_crew.embeddings_settings import (
    _DEFAULT_DIM,
    _MAX_EMBED_CHARS,
    PRIORITY_NORMAL,
    _read_memory_config,
    embedding_work,
)

logger = logging.getLogger("kiro_crew.embeddings")

#: Longest one ``/api/embed`` request may take; a caller's own deadline cuts it shorter.
_REQUEST_TIMEOUT_SECS = 30.0
#: Texts per ``/api/embed`` request. Knowledge chunks can fill the model's whole
#: context, so a small batch keeps one request inside the timeout on a CPU server.
_BATCH_SIZE = 8
#: A batch of 8 vectors of 65,536 floats is about 5 MB of JSON; anything far past
#: that is not an embedding response.
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
#: How long a readiness answer is trusted before the next ``is_ready`` re-probes.
_READY_TTL_OK_SECS = 30.0
_READY_TTL_FAIL_SECS = 5.0
_PROBE_TIMEOUT_SECS = 5.0
#: A region, service or profile name: what AWS allows in each, with no separator
#: a signature scope or a profile lookup could misread.
_AWS_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: Statuses that can mean the signing credentials went stale on disk.
_AUTH_REJECTED = (401, 403)


@dataclass(frozen=True)
class OllamaSettings:
    """The validated ``memory.ollama_*`` settings. ``error`` is empty when usable."""

    url: str
    model: str
    dim: int
    error: str = ""
    auth: str = "none"
    aws_region: str = ""
    aws_service: str = OLLAMA_DEFAULT_AWS_SERVICE
    aws_profile: str = ""


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _url_error(url: str, allow_remote: bool) -> str:
    """Why *url* may not be used, or ``""`` when it may."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return "memory.ollama_url is not a valid URL"
    if parts.scheme not in ("http", "https"):
        return "memory.ollama_url must start with http:// or https://"
    if not host:
        return "memory.ollama_url has no host"
    if parts.username is not None or parts.password is not None:
        return "memory.ollama_url must not carry credentials"
    if parts.query or parts.fragment:
        return "memory.ollama_url must not carry a query or fragment"
    if _is_loopback_host(host):
        return ""
    if not allow_remote:
        return (
            "memory.ollama_url is not a loopback address; set "
            "memory.ollama_allow_remote to send memory text to another host"
        )
    if parts.scheme != "https":
        return "memory.ollama_url must use https:// for a non-loopback host"
    return ""


def _raw_str(memory: dict, key: str) -> str:
    value = memory.get(key)
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class _AuthSettings:
    auth: str
    aws_region: str = ""
    aws_service: str = OLLAMA_DEFAULT_AWS_SERVICE
    aws_profile: str = ""
    error: str = ""


def _auth_settings(memory: dict, environ: "dict[str, str]") -> _AuthSettings:
    """The ``memory.ollama_auth*`` settings and why they are unusable (``error``)."""
    auth = _raw_str(memory, "ollama_auth") or "none"
    if auth not in OLLAMA_AUTH_SCHEMES:
        schemes = ", ".join(OLLAMA_AUTH_SCHEMES)
        return _AuthSettings(auth, error=f"memory.ollama_auth must be one of: {schemes}")
    if auth == "none":
        return _AuthSettings(auth)
    region = (
        _raw_str(memory, "ollama_aws_region")
        or environ.get("AWS_REGION", "").strip()
        or environ.get("AWS_DEFAULT_REGION", "").strip()
    )
    service = _raw_str(memory, "ollama_aws_service") or OLLAMA_DEFAULT_AWS_SERVICE
    profile = _raw_str(memory, "ollama_aws_profile")
    error = ""
    if not region:
        error = "memory.ollama_aws_region is empty and AWS_REGION is not set"
    elif not _AWS_NAME.fullmatch(region):
        error = "memory.ollama_aws_region is not a valid region name"
    elif not _AWS_NAME.fullmatch(service):
        error = "memory.ollama_aws_service is not a valid service name"
    elif profile and not _AWS_NAME.fullmatch(profile):
        error = "memory.ollama_aws_profile is not a valid profile name"
    elif importlib.util.find_spec("botocore") is None:
        error = "memory.ollama_auth 'aws_sigv4' needs botocore (pip install 'boto3>=1.34,<2')"
    return _AuthSettings(auth, region, service, profile, error)


def resolve_ollama_settings(
    memory: dict, environ: "dict[str, str] | None" = None
) -> OllamaSettings:
    """Validate the raw ``memory`` config section into :class:`OllamaSettings`.

    Read from the raw section, like every other knob the backend factory uses,
    and with the same width rule as a custom model. *environ* (default
    ``os.environ``) supplies ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` when
    ``memory.ollama_aws_region`` is empty.
    """
    raw_url = memory.get("ollama_url")
    url = raw_url.strip().rstrip("/") if isinstance(raw_url, str) else ""
    url = url or OLLAMA_DEFAULT_URL
    raw_model = memory.get("ollama_model")
    model = raw_model.strip() if isinstance(raw_model, str) else ""
    raw_dim = memory.get("embedding_dim")
    dim = (
        raw_dim
        if isinstance(raw_dim, int) and not isinstance(raw_dim, bool) and raw_dim > 0
        else _DEFAULT_DIM
    )
    error = _url_error(url, memory.get("ollama_allow_remote") is True)
    if not error and not model:
        error = "memory.ollama_model is empty"
    auth = _auth_settings(memory, dict(os.environ if environ is None else environ))
    return OllamaSettings(
        url,
        model,
        dim,
        error or auth.error,
        auth=auth.auth,
        aws_region=auth.aws_region,
        aws_service=auth.aws_service,
        aws_profile=auth.aws_profile,
    )


def ollama_backend_selected(memory: "dict | None" = None) -> bool:
    """True when the raw ``memory`` section (default: the config's) selects this backend."""
    memory = _read_memory_config() if memory is None else memory
    return memory.get("embedding_provider") == OLLAMA_HTTP_PROVIDER


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an error so a request never leaves the configured URL."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> "urllib.request.Request | None":
        return None


def _build_opener(url: str) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_RefuseRedirects()]
    if _is_loopback_host(urllib.parse.urlsplit(url).hostname or ""):
        # A proxy from the environment would carry loopback-bound text off the host.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


class _SigningError(Exception):
    """A request could not be signed, so it is not sent."""


class _SigV4Signer:
    """Signs requests with AWS Signature Version 4 through botocore.

    botocore is imported on first use: it is an optional dependency, and only a
    configuration that selects ``aws_sigv4`` needs it. The session is built once
    and reused, so refreshable credentials (SSO, ``credential_process``, an
    instance role) refresh themselves instead of being resolved per request.
    :meth:`reset` drops it, so static keys rewritten on disk are read again.
    """

    def __init__(
        self,
        region: str,
        service: str,
        profile: str = "",
        *,
        load_credentials: "Callable[[], Any] | None" = None,
    ) -> None:
        self._region = region
        self._service = service
        self._profile = profile
        self._load_credentials = load_credentials or self._session_credentials
        self._lock = threading.Lock()
        self._session: Any = None

    def _session_credentials(self) -> Any:
        # botocore sessions are not thread-safe; credential resolution runs under the lock.
        with self._lock:
            if self._session is None:
                session_mod = importlib.import_module("botocore.session")
                self._session = session_mod.Session(profile=self._profile or None)
            return self._session.get_credentials()

    def reset(self) -> None:
        """Forget the cached session; the next request resolves credentials again."""
        with self._lock:
            self._session = None

    def headers(self, method: str, url: str, body: "bytes | None", headers: dict) -> dict:
        """*headers* plus the signature headers; raises :class:`_SigningError`."""
        try:
            auth_mod = importlib.import_module("botocore.auth")
            request_mod = importlib.import_module("botocore.awsrequest")
            credentials = self._load_credentials()
            if credentials is None:
                raise _SigningError("no AWS credentials were found")
            request = request_mod.AWSRequest(
                method=method, url=url, data=body, headers=dict(headers)
            )
            signer = auth_mod.SigV4Auth(
                credentials.get_frozen_credentials(), self._service, self._region
            )
            signer.add_auth(request)
            return dict(request.headers.items())
        except _SigningError:
            raise
        except Exception as exc:
            raise _SigningError(f"could not sign the request: {exc}") from exc


def _finite_vector(value: object, dim: int) -> "list[float] | None":
    if not isinstance(value, list) or len(value) != dim:
        return None
    out: list[float] = []
    for x in value:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            return None
        out.append(float(x))
    return out


class OllamaHttpEmbedder(EmbeddingBackend):
    """:class:`EmbeddingBackend` served by an Ollama server over HTTP.

    The vector space is ``ollama:<model>`` at ``memory.embedding_dim``. The URL is
    not part of it: the same model on another server produces the same vectors,
    so moving the server does not re-embed anything.

    ``is_ready`` never blocks. It answers from the last probe of ``/api/tags`` and
    starts a background probe when that answer is stale, because callers poll it
    from the event loop. A failed embed marks the backend not ready at once; a
    successful one marks it ready. While the last answer is a failure, an embed
    sends nothing and returns None until a background probe sees the server again,
    so an outage costs one timeout, not one per call.
    """

    def __init__(
        self,
        settings: OllamaSettings,
        *,
        opener: "urllib.request.OpenerDirector | None" = None,
        start_probe: "Callable[[Callable[[], None]], None] | None" = None,
        signer: "_SigV4Signer | None" = None,
    ) -> None:
        self._settings = settings
        # An unusable config never sends; its URL may not even parse.
        self._opener = opener or _build_opener("" if settings.error else settings.url)
        self._start_probe = start_probe or self._start_probe_thread
        if signer is None and settings.auth == "aws_sigv4" and not settings.error:
            signer = _SigV4Signer(settings.aws_region, settings.aws_service, settings.aws_profile)
        self._signer = signer
        self._model_id = f"ollama:{settings.model}"
        self._lock = threading.Lock()
        self._ready = False
        self._checked_at: float | None = None
        self._probing = False
        self._failing = False
        self._closed = False

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._settings.dim

    # -- readiness ---------------------------------------------------------

    def _usable(self) -> bool:
        return not self._closed and not self._settings.error

    def is_ready(self) -> bool:
        if not self._usable():
            return False
        with self._lock:
            ready = self._ready
            ttl = _READY_TTL_OK_SECS if ready else _READY_TTL_FAIL_SECS
            stale = self._checked_at is None or time.monotonic() - self._checked_at >= ttl
        if stale:
            self._kick_probe()
        return ready

    def prefetch(self) -> None:
        if self._usable():
            self._kick_probe()

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Probe synchronously when not ready; for callers that may block."""
        if not self._usable():
            return False
        with self._lock:
            if self._ready:
                return True
        bound = _PROBE_TIMEOUT_SECS if timeout is None else min(timeout, _PROBE_TIMEOUT_SECS)
        self._record_probe(self._probe(bound))
        with self._lock:
            return self._ready

    @staticmethod
    def _start_probe_thread(target: "Callable[[], None]") -> None:
        threading.Thread(target=target, name="ollama-embed-probe", daemon=True).start()

    def _kick_probe(self) -> None:
        with self._lock:
            if self._probing:
                return
            self._probing = True
        self._start_probe(self._run_probe)

    def _run_probe(self) -> None:
        try:
            ok = self._probe(_PROBE_TIMEOUT_SECS)
        finally:
            with self._lock:
                self._probing = False
        self._record_probe(ok)

    def _record_probe(self, ok: bool) -> None:
        with self._lock:
            self._ready = ok and not self._closed
            self._checked_at = time.monotonic()

    def _open(self, path: str, body: "bytes | None", timeout: float) -> Any:
        """Open ``<url><path>``, signed when ``aws_sigv4`` is configured.

        GET without *body*, POST with it. Raises :class:`_SigningError` before any
        byte is sent when the request cannot be signed.
        """
        url = f"{self._settings.url}{path}"
        method = "GET" if body is None else "POST"
        headers = {} if body is None else {"Content-Type": "application/json"}
        if self._signer is not None:
            headers = self._signer.headers(method, url, body, headers)
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if self._signer is not None and exc.code in _AUTH_REJECTED:
                self._signer.reset()
            raise

    def _probe(self, timeout: float) -> bool:
        """True when the server answers and lists the configured model."""
        try:
            with self._open("/api/tags", None, timeout) as resp:
                payload = json.loads(resp.read(_MAX_RESPONSE_BYTES))
            models = payload.get("models") if isinstance(payload, dict) else None
            names = {
                m.get("name")
                for m in (models if isinstance(models, list) else [])
                if isinstance(m, dict)
            }
        except Exception as exc:
            self._note_failure(f"readiness probe failed: {exc}")
            return False
        wanted = self._settings.model
        if wanted in names or f"{wanted}:latest" in names:
            return True
        self._note_failure(f"the server does not list model {wanted!r}")
        return False

    # -- embedding ---------------------------------------------------------

    def _note_failure(self, reason: str) -> None:
        # One WARNING per outage, not one per call: an unreachable server is retried
        # on every embed and every stale is_ready.
        with self._lock:
            first = not self._failing
            self._failing = True
            self._ready = False
            self._checked_at = time.monotonic()
        level = logging.WARNING if first else logging.DEBUG
        logger.log(
            level,
            "Ollama embedding backend at %s unavailable (%s); memory falls back to keyword search",
            self._settings.url,
            reason,
        )

    def _note_success(self) -> None:
        with self._lock:
            recovered = self._failing
            self._failing = False
            self._ready = not self._closed
            self._checked_at = time.monotonic()
        if recovered:
            logger.info("Ollama embedding backend at %s is serving again", self._settings.url)

    def _request(self, texts: "list[str]") -> "list[list[float]] | None":
        work = embedding_work.get()
        timeout = _REQUEST_TIMEOUT_SECS
        if work is not None:
            timeout = min(timeout, work.deadline - time.monotonic())
            if timeout <= 0:
                return None
        body = json.dumps(
            {
                "model": self._settings.model,
                "input": [t[:_MAX_EMBED_CHARS] for t in texts],
                # Explicit so a long input is cut to the model's context on the
                # server instead of failing the whole batch.
                "truncate": True,
            }
        ).encode("utf-8")
        try:
            with self._open("/api/embed", body, timeout) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError("response too large")
            payload = json.loads(raw)
        except Exception as exc:
            # The EmbeddingBackend contract is None on ANY failure.
            self._note_failure(f"embed request failed: {exc}")
            return None
        vectors = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            self._note_failure("the response did not hold one vector per input")
            return None
        out: list[list[float]] = []
        for value in vectors:
            vector = _finite_vector(value, self._settings.dim)
            if vector is None:
                self._report_bad_vector(value)
                return None
            out.append(vector)
        self._note_success()
        return out

    def _report_bad_vector(self, value: object) -> None:
        width = len(value) if isinstance(value, list) else None
        if width is None or width == self._settings.dim:
            self._note_failure("the response held a malformed vector")
            return
        # memory.embedding_dim needs a restart, so this backend cannot serve again:
        # stop it rather than refill nothing forever while reporting ready.
        self.close()
        logger.error(
            "Ollama model %r returns %d-dimensional vectors but memory.embedding_dim "
            "is %d; set memory.embedding_dim to %d and restart",
            self._settings.model,
            width,
            self._settings.dim,
            width,
        )

    def embed(self, text: str, *, priority: int = PRIORITY_NORMAL) -> "list[float] | None":
        result = self.embed_batch([text], priority=priority)
        return result[0] if result else None

    def embed_batch(
        self, texts: "list[str]", *, priority: int = PRIORITY_NORMAL
    ) -> "list[list[float]] | None":
        """Embed *texts* in batches of ``_BATCH_SIZE``; None if any batch fails.

        The same empty-input rule as the in-process backend: no texts, or only
        blank ones, is None. *priority* is accepted for the interface; the server
        orders its own queue.
        """
        if not self._usable() or not texts or not any(t.strip() for t in texts):
            return None
        with self._lock:
            failing = self._failing
        if failing and not self.is_ready():
            return None
        out: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            chunk = self._request(list(texts[start : start + _BATCH_SIZE]))
            if chunk is None:
                return None
            out.extend(chunk)
        return out

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._ready = False


def build_ollama_backend(memory: dict) -> OllamaHttpEmbedder:
    """Build the backend from the raw ``memory`` section, logging an unusable config once."""
    settings = resolve_ollama_settings(memory)
    if settings.error:
        logger.error(
            "Ollama embedding backend unusable (%s); embeddings unavailable, "
            "memory falls back to keyword search",
            settings.error,
        )
    else:
        logger.info(
            "Using Ollama embedding backend %s (model=%s, dim=%d, auth=%s)",
            settings.url,
            settings.model,
            settings.dim,
            settings.auth,
        )
    return OllamaHttpEmbedder(settings)
