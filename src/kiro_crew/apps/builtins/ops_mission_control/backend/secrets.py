"""Ops Mission Control — keystone-protected token store.

Third-party ops providers (PagerDuty, Datadog) authenticate with API tokens
rather than IAM, which is the case the standard secret-handling rules exist for:
store such credentials in a managed store, never hardcode them, never put them in
plaintext environment variables, and rotate them.

For a local-first agent with no control plane of its own, that maps onto:

**AWS access uses no stored credential at all.** The CloudWatch adapter uses the
ambient credential chain (profile / role / instance role). The app never accepts,
stores, or transmits an AWS access key — "IAM roles over keys" applied directly.

**Third-party tokens live on the keystone floor.** They go in
``<crew_home>/ops_mission_control_secrets.json``, whose filename is registered in
``security._CREW_SECRET_LEAVES``. That places it on the shared read+write
sensitive-path floor, so the AGENT'S OWN file tools and shell cannot read or write
it — the same mechanism that makes the governance ceiling un-disableable. The
dashboard PUT handler is the only writer and opens the path directly (it does not
route through the agent gate), so the operator's Settings UI still works.

Why not ``config.json``? Because Kiro Crew serves an app's ``data/config.json``
over ``/api/apps/<name>/config`` **without session auth** — a documented behavior
apps rely on to bootstrap their UI. A token in there would be readable by anything
that can reach the gateway port. And a token in the main ``config.json`` would be
writable by any auto-approved agent shell. Neither is acceptable for a credential
that can resolve a stranger's production pages.

**Optional rotation.** ``SecretBackend`` is a seam. The default is the local
keystone file; an AWS Secrets Manager backend lets users already on AWS get the
recommended ≤90-day rotation instead of a recommendation they cannot act on.

See ``docs/system-specs/modules/ops-mission-control.md`` (secret storage).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Protocol

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Filename on the crew home. MUST stay in sync with the entry added to
#: ``security._CREW_SECRET_LEAVES`` — the test suite asserts the two agree, so a
#: rename cannot silently drop the keystone protection.
SECRETS_FILENAME = "ops_mission_control_secrets.json"

#: Owner-only mode for the secret file (POSIX). Windows gets an owner-only DACL
#: via ``platform_compat.restrict_to_owner``.
_SECRET_FILE_MODE = 0o600

#: Value returned to callers in place of a stored secret. Secrets are write-only
#: over the API: the UI shows whether a field is set, never what it is.
REDACTED_PLACEHOLDER = "••••••••"

#: Provider-token shapes added to redaction so a token cannot ride out inside a
#: provider payload, a diagnosis, or a Slack message. Complements the core
#: AKIA/ASIA patterns rather than replacing them.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PagerDuty REST tokens: "u+" / "y_NbAkKc" style prefixes then 20+ chars.
    re.compile(r"\b[uy](?:\+|_)[A-Za-z0-9_\-+]{18,}\b"),
    # Datadog API key (32 hex) and application key (40 hex), as standalone words.
    re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE),
    # Datadog PREFIXED keys. Newer tenants issue keys as ``ddapp_<base62>`` /
    # ``ddapi_<base62>`` rather than bare hex, and a prefixed key is NOT hex, so
    # neither pattern above matches it. Found with a real tenant's credentials:
    # every synthetic fixture in the suite used bare hex, so the shape that ships
    # to actual users was the one shape nothing covered.
    re.compile(r"\bdd(?:app|api)_[A-Za-z0-9]{16,}\b"),
    # Generic carriers: "Bearer <token>", "token=<value>", "api-key: <value>".
    #
    # The separator is OPTIONAL, and that matters: this pattern required `[:=]`, so the
    # single most common form in a real log — `Authorization: Bearer sk-...`, where the
    # token follows a SPACE — passed straight through. Verified by handing a leaky evidence
    # adapter four credential shapes: AKIA, PagerDuty and Datadog keys were redacted and
    # `Bearer sk-...` reached the investigation brief, i.e. the model's prompt, in clear
    # text.
    #
    # Core's own bearer pattern requires the literal `Authorization` (deliberately — so a
    # bare `Bearer` cannot over-capture), so it did not catch this SHAPE either. But core is
    # not the weak link: it matches real vendor keys by their OWN patterns regardless of any
    # `Bearer` prefix — verified for OpenAI `sk-proj-`, Anthropic `sk-ant-`, Slack `xoxb-`,
    # Stripe `sk_live_`, GitHub `ghp_`, JWTs and AKIA. What slips through core is only an
    # opaque token with no recognizable prefix, which is exactly the residual case this
    # app-level carrier pattern exists to cover.
    #
    # `\s+` OR `\s*[:=]\s*` rather than making the separator a bare `\s*`: without the
    # alternation, `token` immediately followed by 12+ non-space chars would match ordinary
    # prose like "tokenization-heavy" and redact real diagnostic text.
    #
    # ``application[_-]?key`` is spelled out separately from ``app[_-]?key`` because
    # ``app[_-]?key`` does NOT match ``DD-APPLICATION-KEY`` — the real header Datadog
    # documents and the one an adapter author is most likely to echo into an error
    # string. Caught with a live tenant: the bare-hex patterns missed the prefixed key
    # AND this carrier missed the header naming it, so a real application key rode out
    # in a reproduced ``curl`` trace.
    re.compile(
        r"(?i)\b(bearer|token|api[_-]?key|app[_-]?key|application[_-]?key)\b"
        r"(?:\s*[:=]\s*|\s+)\S{12,}"
    ),
)


def secrets_path() -> Path:
    """Absolute path to the keystone secret file (honors ``KIROCREW_HOME``)."""
    return config_dir() / SECRETS_FILENAME


def redact_tokens(text: str) -> str:
    """Mask provider-token shapes in ``text``.

    Applied to every provider payload before it reaches a model prompt, a
    transcript, Slack, or the UI. This is an always-on floor with no policy key —
    matching the secure-field precedent, there is no legitimate reason to disable
    it, so exposing a toggle would only create a way to get it wrong.
    """
    if not text:
        return text
    out = text
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(REDACTED_PLACEHOLDER, out)
    return out


class SecretBackend(Protocol):
    """Storage seam for provider secrets.

    The default is the local keystone file. An AWS Secrets Manager backend can be
    registered instead so rotation is available to users who want it.
    """

    def get(self, provider_id: str, field_name: str) -> str: ...

    def put(self, provider_id: str, field_name: str, value: str) -> None: ...

    def delete(self, provider_id: str) -> bool: ...

    def configured_fields(self, provider_id: str) -> frozenset[str]: ...


#: Lock filename beside the secret file. See `_ConfigLock`/`_PolicyLock` for why the lock is a
#: sidecar rather than the file itself (atomic_write swaps inodes).
_SECRET_LOCK_SUFFIX = ".lock"


class _SecretLock:
    """Exclusive lock around a read-modify-write of the keystone secret file.

    Keyed on the file's OWN path (as a sidecar), so a pinned per-test path locks independently
    of the default home — the pinned-path isolation the backend documents must not be defeated
    by a shared lock. Routed through `platform_compat.file_lock` for Windows.
    """

    def __init__(self, secret_path: Path) -> None:
        self._lock_path = secret_path.with_name(secret_path.name + _SECRET_LOCK_SUFFIX)
        self._fd: int | None = None

    def __enter__(self) -> "_SecretLock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        platform_compat.acquire_lock(self._fd, exclusive=True)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                platform_compat.release_lock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


class KeystoneFileBackend:
    """Default backend: one owner-only JSON file on the keystone floor."""

    def __init__(self, path: Path | None = None) -> None:
        # An EXPLICIT path is pinned (that is the point of passing one); otherwise
        # the location is resolved per access via ``self._path``. Snapshotting
        # ``secrets_path()`` here instead would freeze the data home as it was at
        # module-import time: this backend is a module-level singleton, so every
        # later ``KIROCREW_HOME`` change is ignored and the whole process shares one
        # secrets file. That silently defeated per-test home isolation — a
        # "no secret configured must reject" assertion passed only because a sibling
        # test had written one, which is the exact failure mode a fail-closed test
        # exists to catch.
        self._pinned_path = path

    # -- internals ---------------------------------------------------------

    @property
    def _path(self) -> Path:
        return self._pinned_path if self._pinned_path is not None else secrets_path()

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for provider, fields in raw.items():
            if isinstance(fields, dict):
                out[str(provider)] = {str(k): str(v) for k, v in fields.items()}
        return out

    def _lock(self) -> "_SecretLock":
        return _SecretLock(self._path)

    def _write(self, data: dict[str, dict[str, str]]) -> None:
        payload = json.dumps(data, indent=2, sort_keys=True)
        atomic_write(self._path, payload, mode=_SECRET_FILE_MODE)
        # Fail-loud lockdown. ``atomic_write``'s mode covers POSIX; this also
        # applies an owner-only DACL on Windows, where POSIX mode bits are not
        # enforced. A lockdown failure must not leave a world-readable token on
        # disk, so we unlink and re-raise rather than continue.
        try:
            platform_compat.restrict_to_owner(self._path)
        except OSError:
            try:
                self._path.unlink()
            except OSError:
                logger.exception("failed to remove secret file after lockdown failure")
            raise

    # -- SecretBackend -----------------------------------------------------

    def get(self, provider_id: str, field_name: str) -> str:
        return self._read().get(provider_id, {}).get(field_name, "")

    def put(self, provider_id: str, field_name: str, value: str) -> None:
        # Locked read-modify-write: two provider credential saves would otherwise each write
        # onto a stale snapshot and the later atomic replace would DELETE the first secret while
        # both returned 200. On the credential store a lost update is a lost secret. Found in
        # review — the same class as the config/index/ledger/policy locks elsewhere in this app.
        with self._lock():
            data = self._read()
            data.setdefault(provider_id, {})[field_name] = value
            self._write(data)

    def delete(self, provider_id: str) -> bool:
        with self._lock():
            data = self._read()
            if provider_id not in data:
                return False
            del data[provider_id]
            self._write(data)
        return True

    def configured_fields(self, provider_id: str) -> frozenset[str]:
        fields = self._read().get(provider_id, {})
        return frozenset(name for name, value in fields.items() if str(value).strip())


_backend: SecretBackend = KeystoneFileBackend()


def register_secret_backend(backend: SecretBackend) -> None:
    """Swap the secret backend (e.g. for an AWS Secrets Manager adapter)."""
    global _backend
    _backend = backend
    # The interpolated value is `type(backend).__name__` — a CLASS NAME, never a secret.
    # Semgrep matches on the word "secret" in the message string, not on the argument.
    logger.info(  # nosemgrep: python-logger-credential-disclosure
        "ops-mission-control: secret backend set to %s", type(backend).__name__
    )


def get_secret(provider_id: str, field_name: str) -> str:
    """Read a secret. Callers must never log or echo the return value."""
    return _backend.get(provider_id, field_name)


def put_secret(provider_id: str, field_name: str, value: str) -> None:
    """Store a secret and audit the write (never the value)."""
    _backend.put(provider_id, field_name, value)
    sel().log_api_access(
        caller="core:ops-mission-control",
        operation="secret_put",
        outcome="success",
        resources=f"provider={provider_id} field={field_name}",
    )


def delete_secret(provider_id: str) -> bool:
    """Remove all secrets for a provider and audit the revocation."""
    removed = _backend.delete(provider_id)
    sel().log_api_access(
        caller="core:ops-mission-control",
        operation="secret_delete",
        outcome="success" if removed else "not_found",
        resources=f"provider={provider_id}",
    )
    return removed


def configured_fields(provider_id: str) -> frozenset[str]:
    """Which secret fields are set for a provider — names only, never values."""
    return _backend.configured_fields(provider_id)


def has_secrets(provider_id: str, required: tuple[str, ...]) -> bool:
    """True when every ``required`` secret field is present and non-empty."""
    present = configured_fields(provider_id)
    return all(name in present for name in required)


def describe_secrets(provider_id: str, fields: tuple[str, ...]) -> dict[str, Any]:
    """Write-only view of a provider's secrets for the settings UI.

    Reports whether each field is SET, never what it contains — the read path has
    no way to exfiltrate a stored token even to an authenticated caller.
    """
    present = configured_fields(provider_id)
    return {name: (REDACTED_PLACEHOLDER if name in present else "") for name in fields}
