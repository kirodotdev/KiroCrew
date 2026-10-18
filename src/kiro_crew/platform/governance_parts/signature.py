"""Proving a security policy's ``identity.signature``.

``policy_signing_payload`` (the bytes an issuer signs), the verification state the loader
records on the ceiling, the trust settings read from the policy trust root, the SEL audit
of each verification, and ``assert_policy_signature_satisfied``, the opt-in gate that
refuses a ceiling whose signature was not proven. A new trust rule goes here.

Served through :mod:`kiro_crew.platform.governance`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import hmac
import logging
from typing import Dict, Mapping, Optional, Tuple

from kiro_crew.platform.admission import (
    canonical_signing_bytes,
    ed25519_verify,
    hmac_signature,
    read_policy_trust_root,
)
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_health import mark_governance_incident
from kiro_crew.platform.governance_parts.carriers import (
    SIGNATURE_UNSIGNED,
    SIGNATURE_UNVERIFIED,
    SIGNATURE_VERIFIED,
    GovernanceCeiling,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.platform.governance")


def policy_signing_payload(data: Mapping[str, object]) -> bytes:
    """Canonical bytes an ``identity.signature`` covers, for the policy *document*.

    Routed through ``admission.canonical_signing_bytes`` — the SAME
    sorted-keys/compact-separators/UTF-8 canonicalization
    ``PluginManifest.signing_payload`` uses — so the two trust roots cannot drift
    apart, and a reviewer verifies consistency by reading one function.

    Coverage is the **whole document minus ``identity.signature``** (the signature
    cannot cover itself; ``identity.issuer`` IS covered, so an attacker cannot
    re-label a validly-signed policy as coming from a different issuer).  Signing
    the raw document rather than a projection of the parsed
    :class:`GovernanceCeiling` is deliberate and does two things a projection
    could not:

    * it covers keys THIS build does not know — a companion-registered scope, or a
      future schema addition — so stripping or editing one is still detected,
      whereas a projection would silently omit it; and
    * it keeps the payload scope-name-agnostic, honoring the module's core
      invariant that adding a scope is a ``SCOPE_CATALOG`` data change and never
      an edit to shared machinery.

    Because coverage is byte-canonical over the parsed JSON (not over the file's
    literal bytes), re-indenting or reordering keys does NOT break a signature,
    while changing any value or adding/removing any key does.
    """
    body = {k: v for k, v in data.items() if k != "identity"}
    identity = data.get("identity")
    if isinstance(identity, dict):
        # Preserve every identity field EXCEPT the signature, so issuer (and any
        # future field such as an expiry or key id) is inside the signed payload.
        rest = {k: v for k, v in identity.items() if k != "signature"}
        if rest:
            body["identity"] = rest
    elif identity is not None:
        # A non-object identity is a schema error parse_policy will reject; keep it
        # inside the payload rather than silently dropping it from coverage.
        body["identity"] = identity
    return canonical_signing_bytes(body)


def _policy_signature_state(
    data: Mapping[str, object],
    trust_keys: Mapping[str, str],
    public_keys: Optional[Mapping[str, str]] = None,
) -> Tuple[str, str]:
    """Classify a policy document's signature.  Returns ``(state, detail)``.

    Pure and I/O-free: the caller supplies the trust keys, so this is directly
    unit-testable and the loader keeps the single responsibility of deciding what
    to DO with the verdict.  The key is selected by ``identity.issuer``.

    An issuer with an entry in *public_keys* (``trust_public_keys``) is verified
    with Ed25519 over the canonical payload and with nothing else: it NEVER falls
    back to its ``trust_keys`` secret, because a fallback would let anyone who can
    read that secret mint a document the asymmetric key was provisioned to stop.
    An issuer without a public key keeps the HMAC-SHA256 check, compared with
    ``hmac.compare_digest`` like ``admission._signature_valid``.
    """
    identity = data.get("identity")
    identity_map: Mapping[str, object] = identity if isinstance(identity, dict) else {}
    signature = str(identity_map.get("signature", "")).strip()
    issuer = str(identity_map.get("issuer", "")).strip()
    if not signature:
        return SIGNATURE_UNSIGNED, "no identity.signature"
    if not issuer:
        # A signature with no issuer names no key, so nothing can verify it.
        return SIGNATURE_UNVERIFIED, "identity.signature present but identity.issuer is empty"
    # Select by MEMBERSHIP, not by a truthy value: an issuer named with an unusable
    # key (``""`` from ``_coerce_public_keys``) must still never reach the HMAC path.
    public_keys = public_keys or {}
    if issuer in public_keys:
        public_key = public_keys[issuer]
        if ed25519_verify(public_key, policy_signing_payload(data), signature):
            return SIGNATURE_VERIFIED, f"issuer {issuer!r} (ed25519)"
        return (
            SIGNATURE_UNVERIFIED,
            f"signature does not verify against the Ed25519 public key for issuer {issuer!r}",
        )
    secret = trust_keys.get(issuer)
    if not secret:
        return SIGNATURE_UNVERIFIED, f"no trust key for issuer {issuer!r}"
    expected = hmac_signature(secret, policy_signing_payload(data))
    # Compare BYTES, not str: ``hmac.compare_digest`` raises TypeError on a str
    # containing any non-ASCII character, and a policy file's signature is
    # attacker- or paste-controlled text.  A raised TypeError here is not a
    # denial — it escapes ``load_security_policy`` as a plain (non-
    # ``PlatformCompositionError``) exception, so the boot handler does not treat
    # it as fatal and the later ``safe_context_call`` degrades to the no-ceiling
    # fallback: a tampered policy would yield an UNGOVERNED host even with
    # ``require_policy_signature`` on, inverting the flag. Encoding both sides
    # keeps every malformed signature an ordinary UNVERIFIED verdict.
    #
    # ``surrogatepass`` for the same reason: ``json.loads`` accepts a lone surrogate
    # (``"\udc80"``) and a strict ``encode`` raises UnicodeEncodeError -- a ValueError,
    # not a composition error, with the same ungoverned outcome. The ladder verifies a
    # user-owned home file BENEATH the central document on every load, so without this
    # one byte in that file would take the fleet ceiling out of the process.
    if hmac.compare_digest(
        expected.encode("utf-8"), signature.encode("utf-8", errors="surrogatepass")
    ):
        return SIGNATURE_VERIFIED, f"issuer {issuer!r}"
    return SIGNATURE_UNVERIFIED, f"signature does not match trust key for issuer {issuer!r}"


def _policy_trust_settings() -> Tuple[bool, Dict[str, str], Dict[str, str]]:
    """Read the operator-controlled trust root.

    Returns ``(require_policy_signature, trust_keys, trust_public_keys)`` from ONE
    read, so a verification never pairs a symmetric map and a public-key map taken
    from two different versions of a trust root mid-push.

    Sourced from the **admission policy** (``KIROCREW_ADMISSION_POLICY`` env path,
    else ``<data home>/admission_policy.json``) rather than from a new key store,
    for three reasons:

    * a document must not be the authority on whether it has to be authentic — a
      ``require_signature`` flag INSIDE ``security_policy.json`` would be
      self-referential, and an attacker rewriting the policy would simply clear
      it;
    * the admission policy is already the fleet-controlled trust root of this
      package, already carries ``trust_keys``, and is already on the
      ``is_sensitive_path`` keystone (the agent can neither read nor write it), so
      it inherits every protection the plugin trust root has; and
    * one key store means an operator provisions keys once.

    Reads via ``admission.read_policy_trust_root`` — the SIDE-EFFECT-FREE reader —
    not ``load_admission_policy``: the latter records the dashboard admission
    posture and emits a critical ``governance_degraded`` SEL on an absent policy,
    which is correct once per process at boot and wrong here, because this runs on
    a repeating path (``gatewayd`` re-loads the security policy per app call) and
    would flip the governance indicator to degraded on every one.

    Never raises, and an unreadable admission policy yields no keys and a
    ``False`` opt-in: an admission-policy problem is already handled loudly and
    fail-closed in admission's own domain, and it must not additionally make the
    security ceiling unloadable through a second path.
    """
    try:
        adm = read_policy_trust_root()
        return (
            bool(adm.require_policy_signature),
            dict(adm.trust_keys),
            dict(adm.trust_public_keys),
        )
    except Exception:
        logger.debug("policy trust settings unavailable", exc_info=True)
        return False, {}, {}


def _policy_signature_required() -> bool:
    """True when the admission policy explicitly opted in.

    Routed through :func:`_policy_trust_settings` — and thus
    ``admission.AdmissionPolicy.from_dict`` and its strict ``_coerce_flag``
    reader — so the opt-in flag and the ``trust_keys`` the verifier consults
    are read by ONE parser.  Two independent readers of the same field is how
    a well-formed trust root carrying ``"require_policy_signature": null``
    can log a fail-closed warning on one path while the enforcement path
    silently reads the gate as off: with the shared reader, a flag that is
    present but not a real JSON boolean reads fail-closed as opted-IN.

    A trust root that is absent, unreadable, or not a JSON object still reads
    as **no opt-in**, and that is deliberate rather than a gap.  An attacker
    who can write ``admission_policy.json`` is explicitly out of this
    feature's threat model (see the threat-model note in
    ``docs/system-specs/modules/governance.md``) — such a process would simply
    set the flag to ``false``, which is well-formed JSON, so fail-closing on a
    *malformed* file only catches a clumsy version of an attack the design
    already concedes.  What a corrupt trust root actually indicates in
    practice is a non-atomic fleet push or a hand-edit typo — a reliability
    event — and the useful response to that is to log loudly and behave
    predictably, which ``read_policy_trust_root`` already does.  ``kirocrew
    doctor`` surfaces it.

    Never raises.
    """
    return _policy_trust_settings()[0]


def _audit_policy_signature(state: str, detail: str, path_label: str) -> None:
    """Best-effort audit of the policy signature verdict.  Never raises.

    A ``verified`` outcome is a debug-level non-event; the interesting records are
    an unverified signature (someone TRIED to assert provenance and it did not
    hold) and the fail-closed trip.  Mirrors ``admission._audit_admission_fail_closed``
    /``_verify_seed_integrity``: SEL for the durable trail, and the process-global
    health mark only for the genuine fail-closed case so a merely-advisory
    mismatch does not flip the dashboard to degraded on every boot.
    """
    if state == SIGNATURE_VERIFIED:
        logger.debug("security policy signature verified (%s)", detail)
        return
    try:

        sel().log_api_access(
            caller="_host",
            operation="security_policy_signature",
            outcome=state,
            source="startup",
            # WHICH policy and WHY both live here rather than in the log line below.
            resources=path_label,
            error=detail,
        )
    except Exception:
        logger.debug("policy signature SEL emit unavailable", exc_info=True)
    if state == SIGNATURE_UNVERIFIED:
        # Neither the label nor the reason is interpolated. Both carry text this process
        # did not author — the label is whatever the caller was resolving (for the fetch
        # tier a URL that may itself be a credential) and the reason names the issuer the
        # DOCUMENT claimed — and the log ring is served by ``GET /api/logs``, which the
        # agent's own browser tooling can drive. The SEL record above is on the keystone
        # and is not reachable that way, so it is where an audit reason belongs; this line
        # exists so the state is visible in an operator's console.
        logger.warning(
            "the security policy carries an UNVERIFIED signature; treating the ceiling as "
            "unauthenticated. The security_policy_signature audit record names which "
            "policy and why."
        )


def _verify_policy_signature(data: Mapping[str, object], *, source: str) -> str:
    """Compute and audit the signature state for a policy document.

    **Computes the verdict; does not enforce it.**  Enforcement is
    :func:`assert_policy_signature_satisfied`, which runs once on the FINAL
    composed ceiling.  The split matters because ``load_security_policy`` walks a
    precedence chain (env → companion bundle → operator home) and runs more than
    once per boot with different arguments: the core's loader-less pass falls
    through to the HOME tier and would raise on it, even when the edition's later
    pass would have selected a correctly-signed BUNDLE that outranks it.  A tier
    the final ceiling never came from must not be able to abort boot, so every
    load-time verdict here is preliminary and only the surviving one is judged.

    Returns the state to record on the ceiling.  Never raises.
    """
    _, trust_keys, public_keys = _policy_trust_settings()
    state, detail = _policy_signature_state(data, trust_keys, public_keys)
    _audit_policy_signature(state, detail, source)
    return state


def _mark_policy_signature_incident(detail: str) -> None:
    """Best-effort: record the fail-closed trip on the governance health signal."""
    try:

        mark_governance_incident("failed_closed", detail=f"security_policy_signature:{detail}")
    except Exception:
        logger.debug("governance health mark unavailable", exc_info=True)


def assert_policy_signature_satisfied(ceiling: Optional[GovernanceCeiling]) -> None:
    """Enforce ``require_policy_signature`` against a FINAL, selected ceiling.

    The single enforcement point for the opt-in.  :func:`_verify_policy_signature`
    only *computes* each tier's verdict at load time; this judges the one that
    actually survived precedence, and it judges both failure shapes:

    * a ceiling whose ``signature_state`` is not ``verified`` — someone either did
      not sign it or signed it with a key this trust root does not hold; and
    * **no ceiling at all** — absence must not satisfy a mandated signature, or a
      fleet that lost or never shipped its ``security_policy.json`` runs with no
      ceiling whatsoever, the exact failure the flag exists to prevent.

    Enforcing here rather than in the loader is what makes tier precedence work.
    ``load_security_policy`` folds the central document over env → companion bundle →
    operator home and runs more than once per boot with different arguments (the core with no
    ``bundled_loader``, an edition with one).  A raise inside it fires on whichever
    tier that particular pass happened to reach: the core's loader-less pass falls
    through to an unsigned HOME file and would abort even when the edition's later
    pass selects a correctly-signed BUNDLE that outranks it.  Only the composed
    result knows which tier won, so only the composed result can be judged.

    Callers: boot (on ``ctx.governance``) and every non-boot consumer that re-loads
    the policy on a live path.  With the flag off (the default, and what the
    downstream edition ships) this is a no-op — an unsigned policy still loads and
    still governs, which is the compatibility contract.
    """
    if ceiling is not None and ceiling.signature_state == SIGNATURE_VERIFIED:
        return
    if not _policy_signature_required():
        return
    if ceiling is None:
        _mark_policy_signature_incident("no-policy:require_policy_signature set")
        raise PlatformCompositionError(
            "require_policy_signature is set in the admission policy but no "
            "security policy is present at any tier (central, env, bundled, or home); "
            "boot is refused (fail-closed) rather than running ungoverned. "
            "Install a signed security_policy.json, or clear "
            "require_policy_signature to return to advisory verification."
        )
    _mark_policy_signature_incident(f"{ceiling.signature_state}:require set")
    raise PlatformCompositionError(
        f"the active security policy's signature is {ceiling.signature_state!r}, not "
        "'verified'; require_policy_signature is set in the admission policy, so "
        "boot is refused (fail-closed). Sign the policy with the trust key for its "
        "identity.issuer, or clear require_policy_signature to return to advisory "
        "verification."
    )
