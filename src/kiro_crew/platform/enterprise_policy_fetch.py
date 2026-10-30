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

**Who is asking.** The one enterprise sign-in (:func:`kiro_crew.enterprise_sign_in.resolve`,
RFC D4): Crew's own vault (``kirocrew kas login``), refreshed through
:class:`~kiro_crew.auth.provider.KasAuthProvider` with the ``KIRO_API_KEY`` bypass off.
An API
key carries no profile, and the environment never stands in for the signed-in identity.
The same resolver answers the start check and the account lock below, so the three
cannot name different accounts.

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

import hashlib
import json
import logging
import os
import signal
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping, Optional, Tuple

from kiro_crew import __version__, edition
from kiro_crew import enterprise_sign_in as _sign_in
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


def _sign_in_remedy() -> str:
    return f"sign in with {_sign_in.sign_in_hint()}"


class EnterprisePolicyError(RuntimeError):
    """The organisation's policy could not be read. Its message is safe to print.

    ``cause`` is one of :mod:`kiro_crew.enterprise_sign_in`'s ``CAUSE_*`` values, or
    ``""`` when none applies. It is also recorded for :func:`last_failure_cause`.
    """

    def __init__(self, message: str, cause: str = "") -> None:
        super().__init__(message)
        self.cause = cause
        _note_cause(cause, message)


#: ``(cause, message)`` of this process's latest refused fetch or account check.
_last_failure: Tuple[str, str] = ("", "")


def _note_cause(cause: str, message: str) -> None:
    global _last_failure
    if cause and message:
        _last_failure = (cause, message)


def last_failure_cause(error_text: str) -> str:
    """The ``CAUSE_*`` behind *error_text*, or ``""`` when it is not a recorded failure.

    The policy core re-raises a fetcher's failure as a plain ``PlatformCompositionError``
    message, so the boot error finds the cause by the recorded failure's own message
    inside that text. A cause whose message is not in *error_text* belongs to an earlier
    failure (one the cache recovered from, say) and is not returned.
    """
    cause, message = _last_failure
    return cause if cause and message in error_text else ""


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
        raise EnterprisePolicyError(
            f"{exc}; {_sign_in_remedy()}", _sign_in.CAUSE_NOT_ELIGIBLE
        ) from None
    scope = _identity_scope(identity.profile_arn)
    if scope != pinned_scope():
        assert_signed_in_unchanged()  # raises: the pin is another account's
    sent_version = _version_for(request.etag, scope)
    raw = _post(url, identity, sent_version, request.timeout_secs)
    try:
        status, version_id, policy = parse_envelope(raw, sent_version_id=sent_version)
    except EnterprisePolicyError as exc:
        if not exc.cause:
            # A malformed answer: the rules cannot be applied.
            _note_cause(_sign_in.CAUSE_BAD_RULES, str(exc))
        raise
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
                f"the organisation's policy cannot be applied: {exc}", _sign_in.CAUSE_BAD_RULES
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
    cache_only = cache_only_process()
    if not pin and not cache_only:
        # Never pinned: nothing was signed in when this process first asked. That is
        # a missing sign-in (or one the resolver refused), not an account change.
        # (``pinned_scope`` only pins a non-empty scope, so the next call retries.)
        from kiro_crew.platform.governance_health import mark_governance_incident

        reason = _refusal_now()
        logger.critical("Enterprise policy refused: %s", reason)
        mark_governance_incident("failed_closed", f"enterprise_policy:{reason.cause}")
        _note_cause(reason.cause, str(reason))
        raise PlatformCompositionError(str(reason))
    live_ok = cache_only or signed_in_scope() == pin
    if not live_ok:
        # The scope differs from the pin. Only a sign-out or another account is an
        # account change; an unreadable store or no profile is refused
        # with its own cause and does not stop the gateway. Classified by its own read,
        # so an empty scope alone never counts as a change.
        _refuse_unless_account_changed(pin)
    if pin and live_ok and (body is None or document_scope(body) == pin):
        return
    from kiro_crew.platform.governance_health import mark_governance_incident
    from kiro_crew.platform.policy_distribution import central_ceiling_installed

    logger.critical("Enterprise policy refused: %s", ACCOUNT_CHANGED)
    mark_governance_incident("failed_closed", "enterprise_policy:account_changed")
    _note_cause(_sign_in.CAUSE_ACCOUNT_CHANGED, ACCOUNT_CHANGED)
    if central_ceiling_installed():
        # Stops this process the way a shutdown request does, on every platform. It
        # is not a kill site: no other process is signalled.
        signal.raise_signal(signal.SIGTERM)
    raise PlatformCompositionError(ACCOUNT_CHANGED)


def _refuse_unless_account_changed(pin: str) -> None:
    """Raise (no stop) unless the selection now names no sign-in or another account."""
    reason: Optional[_sign_in.SignInError] = None
    try:
        scope = _identity_scope(_sign_in.select().profile_arn)
    except _sign_in.SignInError as exc:
        if exc.cause == _sign_in.CAUSE_NO_SIGN_IN:
            return  # a sign-out: the caller treats it as an account change
        reason = exc
    except Exception as exc:
        reason = _sign_in.SignInError(
            f"the Kiro sign-in could not be read ({type(exc).__name__}); {_sign_in_remedy()}",
            _sign_in.CAUSE_SIGN_IN_UNUSABLE,
        )
    else:
        if scope != pin:
            return  # another account: the caller treats it as an account change
        reason = _sign_in.SignInError(
            f"the Kiro sign-in changed while it was being read; {_sign_in_remedy()}",
            _sign_in.CAUSE_SIGN_IN_UNUSABLE,
        )
    logger.critical("Enterprise policy refused: %s", reason)
    _note_cause(reason.cause, str(reason))
    raise PlatformCompositionError(str(reason))


def _refusal_now() -> _sign_in.SignInError:
    """Why the resolver finds no sign-in right now (the error it raises, not raised)."""
    try:
        _sign_in.resolve()
    except _sign_in.SignInError as exc:
        return exc
    except Exception as exc:
        return _sign_in.SignInError(
            f"the Kiro sign-in could not be read ({type(exc).__name__}); {_sign_in_remedy()}",
            _sign_in.CAUSE_SIGN_IN_UNUSABLE,
        )
    # Nothing was pinned, so nothing can have changed: a sign-in that appeared while
    # this process was starting. Not an account change; the next start pins it.
    return _sign_in.SignInError(
        "the Kiro sign-in appeared while Kiro Crew was starting; start Kiro Crew again",
        _sign_in.CAUSE_SIGN_IN_UNUSABLE,
    )


def cache_only_process() -> bool:
    """Does this process take its ceiling from its gateway's cache (no sign-in here)?"""
    from kiro_crew.platform.policy_distribution import cache_only

    return cache_only()


def signed_in_scope() -> str:
    """The digest of the profile the one enterprise sign-in names now, or ``""``.

    :func:`kiro_crew.enterprise_sign_in.select`, the same selection the start check,
    the gate and the fetch read: which account is stored, so a lapsed sign-in still
    names its account and reaches the fetch as ``sign_in_unusable``, never as an
    account change. No sign-in, or anything unreadable answers
    ``""``, which matches no document, so the cache is refused and a fetch reports the
    cause.
    """
    try:
        return _identity_scope(_sign_in.select().profile_arn)
    except Exception:
        return ""


def signed_in_identity() -> _Identity:
    """The one enterprise sign-in with a live token, or raise :class:`EnterprisePolicyError`."""
    try:
        sign_in = _sign_in.resolve(with_token=True)
    except _sign_in.SignInError as exc:
        raise EnterprisePolicyError(str(exc), exc.cause) from None
    except Exception as exc:
        raise EnterprisePolicyError(
            f"the Kiro sign-in could not be read or refreshed ({type(exc).__name__}); {_sign_in_remedy()}",
            _sign_in.CAUSE_SIGN_IN_UNUSABLE,
        ) from None
    return _Identity(sign_in.token, sign_in.profile_arn, sign_in.token_type)


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
        refused = exc.code in (401, 403)
        message = _http_failure(exc)
        if "refused the Kiro sign-in" in message:
            refused = True
        cause = _sign_in.CAUSE_REFUSED if refused else _sign_in.CAUSE_UNREACHABLE
        raise EnterprisePolicyError(message, cause) from None
    except urllib.error.URLError as exc:
        raise EnterprisePolicyError(_unreachable(exc.reason), _sign_in.CAUSE_UNREACHABLE) from None
    except (OSError, ValueError) as exc:
        raise EnterprisePolicyError(_unreachable(exc), _sign_in.CAUSE_UNREACHABLE) from None
    if status != 200:
        raise EnterprisePolicyError(
            f"the governance service answered HTTP {status}", _sign_in.CAUSE_UNREACHABLE
        )
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise EnterprisePolicyError(
            f"the governance service's answer exceeds {MAX_ENVELOPE_BYTES} bytes",
            _sign_in.CAUSE_BAD_RULES,
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
        return f"the governance service refused the Kiro sign-in (HTTP {exc.code}{named}); {_sign_in_remedy()}"
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
            f"build requires an organisation's policy; {_sign_in_remedy()}",
            _sign_in.CAUSE_NOT_ELIGIBLE,
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


def profile_scope(profile_arn: str) -> str:
    """The digest the account lock pins for *profile_arn* (see :func:`pinned_scope`)."""
    return _identity_scope(profile_arn)


def _version_for(etag: str, scope: str) -> str:
    """The cached ``versionId`` to send, only when the cache is this profile's."""
    prefix, sep, version = etag.partition(":")
    return version if sep and prefix == scope else ""


def _encode(doc: Mapping[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True).encode("utf-8")
