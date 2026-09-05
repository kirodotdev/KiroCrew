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
from kiro_crew.apps.builtins.ops_mission_control.backend.models import CorruptDocumentError
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Filename on the crew home. MUST stay in sync with the entry added to
#: ``security._CREW_SECRET_LEAVES`` — the test suite asserts the two agree, so a
#: rename cannot silently drop the keystone protection.
SECRETS_FILENAME = "ops_mission_control_secrets.json"

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

    @staticmethod
    def _coerce(raw: Any, *, strict: bool = False) -> dict[str, dict[str, str]]:
        """Normalize a parsed store to ``provider -> {field: value}``, all strings.

        Shared by both readers below so the only thing that can differ between
        them is which read FAILURES are allowed to answer "empty".

        ``strict`` is the update path, and its rule is the one three review
        rounds converged on for the incident index (see
        ``store._coerce_index``): deserialize, re-serialize, and refuse if
        anything that was on disk did not survive. The lenient coercion drops a
        provider whose entry is not an object and retypes a field that is not a
        string -- right for a LOOKUP, where the worst outcome is "not
        configured", and destruction on the update path, where the caller
        rewrites the whole file from what this returns. The refusal names the
        entry's POSITION and nothing else: in a malformed document ANY part --
        the provider key included -- can be a pasted credential, so no
        document content may ride on an exception that crosses into responses
        and logs. Found in review (GPT 5.6), which produced the counterexample
        ``{"<token>": "scalar"}``.
        """
        if not isinstance(raw, dict):
            if strict:
                raise CorruptDocumentError("secret store root is not a JSON object", "", 0)
            return {}
        out: dict[str, dict[str, str]] = {}
        for provider, fields in raw.items():
            if isinstance(fields, dict):
                out[str(provider)] = {str(k): str(v) for k, v in fields.items()}
        if strict:
            for index, (provider, fields) in enumerate(raw.items()):
                if out.get(str(provider)) != fields:
                    raise CorruptDocumentError(
                        f"secret store entry at position {index} would not survive "
                        "a read-write cycle",
                        "",
                        0,
                    )
        return out

    def _read(self) -> dict[str, dict[str, str]]:
        """Every stored secret, or ``{}`` when there is nothing readable.

        A LOOKUP read: ``get``/``configured_fields`` answer "not configured"
        rather than raising, so the Settings UI still renders and a provider
        whose token cannot be loaded is refused by the fail-closed
        ``has_secrets`` check instead of 500ing the route. See
        :meth:`_read_for_update` for why a mutation may not stand on the same
        answer.

        An absent file is silent -- no secret has been stored yet, not a fault.
        Anything else is logged, because the state this degrades into looks
        exactly like health: every provider reads as unconfigured, polling
        stops, and nothing else would prompt an operator to look.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # The message is a fixed literal with NO interpolated value — the only
            # dynamic content is the traceback, and a decode error's rendering never
            # includes the document. Semgrep matches on the word "secret" in the
            # message string, not on the argument (see the sibling annotation on
            # the backend-set log line below).
            logger.warning(  # nosemgrep: python-logger-credential-disclosure
                "ops-mission-control: secret store unreadable; every provider will "
                "read as unconfigured",
                exc_info=True,
            )
            return {}
        if not isinstance(raw, dict):
            # The same degradation reached without a parse failure -- same
            # silence problem, same log line. Fixed literal, no content.
            logger.warning(  # nosemgrep: python-logger-credential-disclosure
                "ops-mission-control: secret store root is not an object; every "
                "provider will read as unconfigured"
            )
            return {}
        return self._coerce(raw)

    def _read_for_update(self) -> dict[str, dict[str, str]]:
        """The store a read-modify-write is allowed to publish over.

        ``put`` and ``delete`` rewrite the WHOLE file from what they read, so an
        empty base is not "no secrets to carry forward" -- it is "delete every
        provider token already stored". Only a MISSING file makes that true. An
        unreadable one (a transient EACCES/EIO, a scanner holding the handle on
        Windows) is a store we still have, and this file is the only copy that
        exists: a provider token is not derivable from anything else on the box,
        so truncating it means the operator must mint new credentials at
        PagerDuty and Datadog, and every poll fails closed until they do. The
        error propagates and the mutation is abandoned instead.

        Corruption propagates too (#7805, mirroring #7794's decision for the
        incident index): "cannot merge into" is not "safe to destroy". A
        truncated store still holds most of its tokens verbatim -- readable
        right up to the moment a rewrite replaces them -- and a refusal costs
        one skipped mutation and a visible error. Every corruption door raises
        the one named type, :class:`CorruptDocumentError`: a parse failure, a
        byte stream that is not UTF-8 (``UnicodeDecodeError`` is a
        ``ValueError`` but NOT a ``JSONDecodeError``, so unwrapped it slips
        past every corruption clause at the callers), and -- via the strict
        coercion -- valid JSON whose content would not survive a read-write
        cycle.

        One deliberate divergence from the index reader it mirrors: the
        refusal carries ``""`` where that one forwards ``exc.doc``, and the
        exception CHAIN is severed rather than kept. The doc is the raw file
        text, and HERE that text is the credential store -- and the chain is
        not cosmetics, because ``__cause__`` (or ``__context__`` under ``from
        None``) keeps the original parser exception alive with the full
        document on it. What debugging loses is repaid in the message: the
        parser's own line/column are folded into the text, which is the part a
        log actually renders.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            # The REAL location is folded into the message text, because the
            # scrubbed empty ``doc`` below makes the exception's own rendering
            # recompute a meaningless "line 1 column 1".
            corrupt_msg = (
                f"{exc.msg} (at line {exc.lineno} column {exc.colno} of the stored document)"
            )
        except UnicodeDecodeError as exc:
            corrupt_msg = f"secret store is not valid UTF-8: {exc.reason}"
        else:
            return self._coerce(raw, strict=True)
        # Raised OUTSIDE the except block, deliberately: ``raise ... from exc``
        # keeps the original exception -- whose ``doc``/``object`` attribute IS
        # the raw credential file -- reachable through ``__cause__``, and even
        # ``from None`` leaves it on ``__context__``. Constructing the refusal
        # after the handler has exited severs the chain entirely, so no copy of
        # the store's bytes rides on the exception that crosses into route
        # handlers and log formatters. Found in review (GPT 5.6).
        raise CorruptDocumentError(corrupt_msg, "", 0)

    def _lock(self) -> "_SecretLock":
        return _SecretLock(self._path)

    def _write(self, data: dict[str, dict[str, str]]) -> None:
        payload = json.dumps(data, indent=2, sort_keys=True)
        # Fail-loud lockdown BEFORE any content lands: ``restrict_to_owner=True``
        # applies the owner-only DACL to the temp file before the payload
        # reaches it (a post-rename lockdown left every stored provider token
        # readable under the inherited DACL on Windows for the write window,
        # issue #5285) and implies the owner-only POSIX mode. The default
        # ``restrict_on_error="raise"`` refuses to publish a token file it
        # cannot protect.
        #
        # No cleanup on failure: every failure inside ``atomic_write`` —
        # lockdown, payload write (ENOSPC), rename — happens BEFORE the final
        # path is touched, so an unprotectable file never exists at
        # ``self._path`` at all. The unlink the old code ran on lockdown
        # failure existed to remove a NEW store already PUBLISHED at a wide
        # DACL; that state is unreachable now, and keeping the unlink would
        # instead delete the PREVIOUS, healthy, already-locked-down store —
        # every stored provider token — on one transient lockdown failure.
        atomic_write(self._path, payload, restrict_to_owner=True)

    # -- SecretBackend -----------------------------------------------------

    def get(self, provider_id: str, field_name: str) -> str:
        return self._read().get(provider_id, {}).get(field_name, "")

    def put(self, provider_id: str, field_name: str, value: str) -> None:
        # Locked read-modify-write: two provider credential saves would otherwise each write
        # onto a stale snapshot and the later atomic replace would DELETE the first secret while
        # both returned 200. On the credential store a lost update is a lost secret. Found in
        # review — the same class as the config/index/ledger/policy locks elsewhere in this app.
        #
        # The base is ``_read_for_update``, not ``_read``: the lock serializes
        # writers but says nothing about a read that FAILED, and this write
        # replaces the whole file. See that method for why one transient EACCES
        # must abandon the save rather than publish an empty store over it.
        with self._lock():
            data = self._read_for_update()
            data.setdefault(provider_id, {})[field_name] = value
            self._write(data)

    def delete(self, provider_id: str) -> bool:
        # ``_read_for_update`` for the same reason as ``put``, plus one specific to
        # revocation: on an unreadable store the lenient read reported the provider
        # absent, so this returned False and the audit logged ``not_found`` — telling
        # the operator there was nothing to revoke while the live token was still on
        # disk and still working. A raise is the honest answer.
        with self._lock():
            data = self._read_for_update()
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
