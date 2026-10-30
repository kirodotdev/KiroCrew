"""The enterprise build's policy transport: the Kiro control plane, as the signed-in user.

Registered for :data:`~kiro_crew.platform.policy_distribution_source.ENTERPRISE_POLICY_SCHEME`
(RFC rfc-crew-enterprise-edition, D2 and the Enterprise gate). One fetch:

  POST <control-plane host for the profile's region>/
    X-Amz-Target: KiroControlPlaneBearerService.GetEffectiveEnterpriseManagementResourceWithToken
    Authorization: Bearer <access token>       (+ TokenType: EXTERNAL_IDP for an External IdP)
    body: {"profileArn", "clientSurface": "CREW", "clientVersion", "manifestVersionId"?}
  -> {"enterpriseManagementStatus": ENABLED | DISABLED | UNCHANGED | NOT_ELIGIBLE,
      "manifest": {"hash", "versionId", ...},
      "body": {"policy": <Trust V2 YAML>, "configuration": [...]}}

**Who is asking.** The Kiro sign-in in Crew's own vault (``kirocrew kas login``), read
and refreshed through :class:`~kiro_crew.auth.provider.KasAuthProvider` with the
``KIRO_API_KEY`` bypass off: an API key carries no profile, and the environment must not
stand in for the signed-in identity.

**Where.** ``control_plane_url(profile_region(arn))``: a host from the region table built
into this package. Nothing in the environment or config names it.

**Trust.** TLS with the default certificate and hostname checks (``pd._open``, which also
refuses redirects), the pinned host, and, when the reply carries ``manifest.hash``, the
SHA-256 of the body it came with. There is no signature to verify.

**Failure.** Every failure raises with a message naming what failed. The core turns that
into a refused boot in an enterprise build (``enterprise_boot.refuse_policy_boot``), or
keeps the installed ceiling on a live refresh.

``NOT_ELIGIBLE`` (an account with no enterprise management, such as Builder ID) is a
failure: an enterprise build requires an organisation's policy. ``DISABLED`` (enterprise
management with no policy published) answers a document that governs nothing, so every
control stays at the build's own locked default.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import ssl
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping, Optional, Tuple

from kiro_crew import __version__, edition
from kiro_crew.auth.login.control_plane import (
    ControlPlaneError,
    control_plane_url,
    cps_headers,
    profile_region,
)
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.enterprise_policy_translate import (
    TranslationError,
    neutral_document,
    translate,
)
from kiro_crew.platform.governance import MAX_POLICY_BYTES

logger = logging.getLogger(__name__)

TARGET = "KiroControlPlaneBearerService.GetEffectiveEnterpriseManagementResourceWithToken"
CLIENT_SURFACE = "CREW"

STATUS_ENABLED = "ENABLED"
STATUS_DISABLED = "DISABLED"
STATUS_UNCHANGED = "UNCHANGED"
STATUS_NOT_ELIGIBLE = "NOT_ELIGIBLE"

#: The policy is capped by the core; the envelope adds configuration and the manifest.
MAX_ENVELOPE_BYTES = 2 * MAX_POLICY_BYTES

#: ``identity.issuer`` of a fetched document: this prefix and the profile's digest.
ISSUER_PREFIX = "kiro-enterprise-management:"

_HASH_PREFIX = "sha256:"
#: Joins the scope and the neutral document's digest in a DISABLED answer's etag.
_DISABLED_TAG = "/disabled/"
_CONFIG_FIELDS = ("key", "value", "source", "composeBehavior")
_TOKEN_TYPE_EXTERNAL_IDP = "EXTERNAL_IDP"
_SIGN_IN = "sign in with `kirocrew kas login --provider idc --start-url <your start URL>`"
#: Seconds to wait for the vault read and token refresh.
_CREDENTIAL_TIMEOUT_SECS = 30.0


class EnterprisePolicyError(RuntimeError):
    """The organisation's policy could not be read. Its message is safe to print."""


class _Identity:
    """The signed-in user, as the control plane needs it. The token is never printed."""

    __slots__ = ("token", "profile_arn", "token_type")

    def __init__(self, token: str, profile_arn: str, token_type: str) -> None:
        self.token = token
        self.profile_arn = profile_arn
        self.token_type = token_type

    def __repr__(self) -> str:
        return "_Identity(<redacted>)"


def fetch(request: pd.FetchRequest) -> pd.FetchedPolicy:
    """Read the organisation's policy for the signed-in profile, or raise.

    *request.url* and *request.headers* are not used: the address is the build's, and
    the credential is the sign-in's.
    """
    if not edition.is_enterprise():
        raise PlatformCompositionError(
            f"policy source scheme {request.url.partition(':')[0]!r} is available only in "
            "an enterprise build"
        )
    if _pinned:
        # Before the credential read, so a sign-out is refused as an account change
        # rather than retried as an unreachable service.
        assert_signed_in_unchanged()
    identity = signed_in_identity()
    try:
        url = control_plane_url(profile_region(identity.profile_arn))
    except ControlPlaneError as exc:
        raise EnterprisePolicyError(f"{exc}; {_SIGN_IN}") from None
    scope = _identity_scope(identity.profile_arn)
    if scope != pinned_scope():
        assert_signed_in_unchanged()  # raises: the pin is another account's
    sent_version = _version_for(request.etag, scope)
    raw = _post(url, identity, sent_version, request.timeout_secs)
    status, version_id, policy = parse_envelope(raw, sent_version_id=sent_version)
    etag = f"{scope}:{version_id}" if version_id else ""
    if status == STATUS_UNCHANGED:
        return pd.FetchedPolicy(etag=etag, not_modified=True)
    if status == STATUS_DISABLED:
        document = neutral_document()
    else:
        try:
            document = translate(policy or "")
        except TranslationError as exc:
            raise EnterprisePolicyError(
                f"the organisation's policy cannot be applied: {exc}"
            ) from None
    # The profile the document was read for, so a cached copy is never served to another.
    document["identity"] = {"issuer": ISSUER_PREFIX + scope}
    body = _encode(document)
    if status == STATUS_DISABLED:
        # DISABLED has no versionId, so the document's own digest stands in for one. The
        # tag puts no ':' after the scope, so _version_for never sends it to the service.
        etag = f"{scope}{_DISABLED_TAG}{hashlib.sha256(body).hexdigest()[:16]}"
        if request.etag == etag:
            # The same profile's same DISABLED document is cached: nothing to install.
            return pd.FetchedPolicy(etag=etag, not_modified=True)
    return pd.FetchedPolicy(body=body, etag=etag)


def document_scope(body: bytes) -> str:
    """The profile digest a fetched document was read for, or ``""``."""
    try:
        issuer = json.loads(body.decode("utf-8"))["identity"]["issuer"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        return ""
    if not isinstance(issuer, str) or not issuer.startswith(ISSUER_PREFIX):
        return ""
    return issuer[len(ISSUER_PREFIX) :]


#: The profile digest this process was pinned to, handed to its children. Internal:
#: set by an enterprise process when it pins, never read as a setting. A cache-only
#: child takes its pin from here; any other process pins from its own vault.
PROFILE_PIN_ENV = "KIROCREW_POLICY_PROFILE_PIN"

ACCOUNT_CHANGED = "the signed-in Kiro account changed since Kiro Crew started; restart Kiro Crew"

#: The pin of a process that is not a cache-only child; ``""`` until it pins.
_pinned = ""


def pinned_scope() -> str:
    """The profile this process is governed as, fixed for its life, or ``""``.

    The first call pins the profile signed in then. The owner ruled that a sign-in
    change while Crew runs needs no live handling beyond refusing it: every install
    compares the live sign-in with this pin (:func:`assert_signed_in_unchanged`).
    """
    global _pinned
    if cache_only_process():
        return os.environ.get(PROFILE_PIN_ENV, "").strip()
    if not _pinned:
        # Held in this process, never read back from the environment: a re-exec'd
        # gateway inherits the environment and must pin its own sign-in.
        _pinned = signed_in_scope()
        if _pinned:
            os.environ[PROFILE_PIN_ENV] = _pinned  # for this process's children
    return _pinned


def assert_signed_in_unchanged(body: Optional[bytes] = None) -> None:
    """Refuse when the live sign-in, or *body*'s profile, is not the pinned one.

    The refusal is audited, and a running gateway is then stopped: its ceiling was
    chosen for an account other than the one signed in now. A cache-only child has no
    sign-in of its own, so only *body* is checked there.
    """
    pin = pinned_scope()
    live_ok = cache_only_process() or signed_in_scope() == pin
    if pin and live_ok and (body is None or document_scope(body) == pin):
        return
    from kiro_crew.platform.governance_health import mark_governance_incident
    from kiro_crew.platform.policy_distribution import central_ceiling_installed

    logger.critical("Enterprise policy refused: %s", ACCOUNT_CHANGED)
    mark_governance_incident("failed_closed", "enterprise_policy:account_changed")
    if central_ceiling_installed():
        # Stops this process the way a shutdown request does, on every platform. It
        # is not a kill site: no other process is signalled.
        signal.raise_signal(signal.SIGTERM)
    raise PlatformCompositionError(ACCOUNT_CHANGED)


def cache_only_process() -> bool:
    """Does this process take its ceiling from its gateway's cache (no sign-in here)?"""
    from kiro_crew.platform.policy_distribution import cache_only

    return cache_only()


def signed_in_scope() -> str:
    """The digest of the profile signed in now, or ``""`` when there is none.

    A plain vault read: no refresh, no network. Anything unreadable answers ``""``,
    which matches no document, so the cache is refused and a fetch reports the cause.
    """
    try:
        from kiro_crew.auth.bridge import default_token_store

        stored = default_token_store().resolve()
    except Exception:
        return ""
    if stored is None or not stored.profile_arn:
        return ""
    return _identity_scope(stored.profile_arn)


def signed_in_identity() -> _Identity:
    """The vault's Kiro sign-in, refreshed if due, or raise :class:`EnterprisePolicyError`."""
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated

    provider = KasAuthProvider(default_token_store(), allow_env_api_key=False)
    try:
        token = _run(provider.current())
    except NotAuthenticated:
        raise EnterprisePolicyError(f"no Kiro sign-in was found; {_SIGN_IN}") from None
    except Exception as exc:
        raise EnterprisePolicyError(
            f"the Kiro sign-in could not be read or refreshed ({type(exc).__name__}); {_SIGN_IN}"
        ) from None
    if not token.profile_arn:
        raise EnterprisePolicyError(
            f"the Kiro sign-in has no profile, so it is not an organisation account; {_SIGN_IN}"
        )
    token_type = _TOKEN_TYPE_EXTERNAL_IDP if token.auth_method == "external_idp" else ""
    return _Identity(token.access_token, token.profile_arn, token_type)


def _run(coro: Any) -> Any:
    """Run *coro* to completion on its own loop in a worker thread.

    The core calls fetchers from synchronous code that may itself be running on an
    event loop thread (boot) or on the refresher's thread, so running a loop here
    directly could find one already running.
    """
    box: Dict[str, Any] = {}

    def work() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            box["error"] = exc
        finally:
            loop.close()

    worker = threading.Thread(target=work, name="enterprise-policy-credential", daemon=True)
    worker.start()
    worker.join(_CREDENTIAL_TIMEOUT_SECS)
    if worker.is_alive():
        raise TimeoutError("the Kiro sign-in did not answer in time")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _post(url: str, identity: _Identity, sent_version: str, timeout: float) -> bytes:
    payload: Dict[str, str] = {
        "profileArn": identity.profile_arn,
        "clientSurface": CLIENT_SURFACE,
        "clientVersion": __version__,
    }
    if sent_version:
        payload["manifestVersionId"] = sent_version
    headers = cps_headers(TARGET, identity.token, token_type=identity.token_type)
    headers["Accept"] = "application/json"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers
    )
    try:
        with pd._open(req, timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read(MAX_ENVELOPE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise EnterprisePolicyError(_http_failure(exc)) from None
    except urllib.error.URLError as exc:
        raise EnterprisePolicyError(_unreachable(exc.reason)) from None
    except (OSError, ValueError) as exc:
        raise EnterprisePolicyError(_unreachable(exc)) from None
    if status != 200:
        raise EnterprisePolicyError(f"the governance service answered HTTP {status}")
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise EnterprisePolicyError(
            f"the governance service's answer exceeds {MAX_ENVELOPE_BYTES} bytes"
        )
    return raw


def _unreachable(reason: object) -> str:
    """Why the service could not be reached, never quoting the exception text."""
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "the governance service's TLS certificate did not verify for its pinned host"
    if isinstance(reason, BaseException):
        return f"the governance service could not be reached ({type(reason).__name__})"
    return "the governance service could not be reached"


def _http_failure(exc: urllib.error.HTTPError) -> str:
    """The message for an HTTP error, naming the AWS error type and never the body."""
    error_type = ""
    try:
        data = json.loads((exc.read(64 * 1024) or b"{}").decode("utf-8", "replace"))
        if isinstance(data, dict):
            error_type = str(data.get("__type") or "")
    except (OSError, ValueError):
        pass
    if not error_type and exc.headers is not None:
        error_type = str(exc.headers.get("x-amzn-ErrorType") or "")
    error_type = error_type.rpartition("#")[2].partition(":")[0]
    if not error_type.isidentifier():
        error_type = ""
    named = f" {error_type}" if error_type else ""
    if exc.code in (401, 403) or error_type in ("AccessDeniedException", "UnauthorizedException"):
        return (
            f"the governance service refused the Kiro sign-in (HTTP {exc.code}{named}); {_SIGN_IN}"
        )
    if 300 <= exc.code < 400:
        return f"the governance service answered a redirect (HTTP {exc.code}), which is refused"
    return f"the governance service answered HTTP {exc.code}{named}"


def parse_envelope(raw: bytes, *, sent_version_id: str = "") -> Tuple[str, str, Optional[str]]:
    """``(status, versionId, policy)`` from the control plane's answer, or raise."""
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise EnterprisePolicyError("the governance service's answer is not JSON") from None
    if not isinstance(envelope, dict):
        raise EnterprisePolicyError("the governance service's answer is not a JSON object")
    status = envelope.get("enterpriseManagementStatus")
    manifest = envelope.get("manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    version_id = manifest.get("versionId")
    version_id = version_id if isinstance(version_id, str) else ""
    if status == STATUS_NOT_ELIGIBLE:
        raise EnterprisePolicyError(
            "the signed-in Kiro account has no enterprise management, and this enterprise "
            f"build requires an organisation's policy; {_SIGN_IN}"
        )
    if status == STATUS_DISABLED:
        return STATUS_DISABLED, "", None
    if status == STATUS_UNCHANGED:
        # Only an answer to the version this client sent keeps the cached copy.
        if not sent_version_id or version_id != sent_version_id:
            raise EnterprisePolicyError(
                "the governance service reported no change for a version not sent"
            )
        return STATUS_UNCHANGED, version_id, None
    if status != STATUS_ENABLED:
        raise EnterprisePolicyError(
            f"the governance service answered an unknown status {str(status)[:40]!r}"
        )
    body = envelope.get("body")
    body = body if isinstance(body, dict) else {}
    policy = body.get("policy")
    if not isinstance(policy, str):
        raise EnterprisePolicyError("the governance service's answer carries no policy")
    declared = manifest.get("hash")
    if declared is not None:
        configuration = _configuration(body.get("configuration"))
        if declared != _HASH_PREFIX + body_digest(policy, configuration):
            raise EnterprisePolicyError(
                "the governance service's answer does not match its own sha256 hash"
            )
    return STATUS_ENABLED, version_id, policy


def body_digest(policy: str, configuration: Tuple[Mapping[str, str], ...] = ()) -> str:
    """The manifest digest of an answer's body, as hex.

    SHA-256 over a 4-byte big-endian length and the UTF-8 bytes of the policy, then of
    each configuration entry's fields, the entries in key order.
    """
    digest = hashlib.sha256()

    def add(value: str) -> None:
        data = value.encode("utf-8")
        digest.update(len(data).to_bytes(4, "big"))
        digest.update(data)

    add(policy)
    for entry in sorted(configuration, key=lambda e: e["key"]):
        for name in _CONFIG_FIELDS:
            add(entry[name])
    return digest.hexdigest()


def _configuration(raw: object) -> Tuple[Mapping[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(
        isinstance(entry, dict) and all(isinstance(entry.get(n), str) for n in _CONFIG_FIELDS)
        for entry in raw
    ):
        raise EnterprisePolicyError("the governance service's configuration entries are malformed")
    return tuple({name: entry[name] for name in _CONFIG_FIELDS} for entry in raw)


def _identity_scope(profile_arn: str) -> str:
    """A short digest of the profile, so one profile's cached version is never sent for another."""
    return hashlib.sha256(profile_arn.encode("utf-8")).hexdigest()[:16]


def _version_for(etag: str, scope: str) -> str:
    """The cached ``versionId`` to send, only when the cache is this profile's."""
    prefix, sep, version = etag.partition(":")
    return version if sep and prefix == scope else ""


def _encode(doc: Mapping[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True).encode("utf-8")
