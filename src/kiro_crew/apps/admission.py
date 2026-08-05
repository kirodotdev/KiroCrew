"""App admission control — the gate that decides whether an app may install.

This is a **contained** app-facing admission gate for the App Kit install /
update / enable / registry paths.  It is deliberately **distinct** from the
CPP-seam plugin admission engine (``kiro_crew.platform.admission``): that layer
gates signed *plugin* entry-points (banned / approved / require_signature /
capability ceiling, trust-root at ``~/.kiro/crew/admission_policy.json``); this
layer gates *App Kit* apps (install/update/enable/register/registry) using a
separate fleet-controlled policy the app can never source itself.  The two are
different trust roots for different subsystems.

Model (mirrors the plugin gate's decision core):

1. **Kill-switch (`banned`)** — a fleet can ban an app by name; the ban always
   wins, even in an otherwise-open policy.
2. **Marketplace allowlist (`approved`)** — when the policy carries a non-empty
   allowlist, only listed apps are admitted.
3. **Verify-before-run signature (`require_signature`)** — the app ships a
   signed manifest; admission verifies the signature (HMAC POC; a production
   impl uses an asymmetric publisher key) against a trust key the *policy*
   carries, before the app's files are copied / its onInstall script runs.

Trust-root: the policy is loaded from ``config_dir()/app_admission.json`` —
never from the app being admitted.  Interim deviation (documented as
accepted-risk): an ABSENT policy file **admits** (preserves the current
no-policy default) rather than failing closed; only a PRESENT-but-unreadable
file fails closed (deny-all).  The seeded-default mechanism that makes absence
itself fail-closed belongs to the CPP governance seam, not this gate.

See ``docs/system-specs/modules/security.md`` (App admission).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

from kiro_crew.config.loader import config_dir
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.apps.manifest import AppManifest

logger = logging.getLogger(__name__)

# Where a managed fleet drops the app admission policy.
_POLICY_FILENAME = "app_admission.json"

# Policy modes.
MODE_OPEN = "open"  # admit unless explicitly banned (public / no-policy default)
MODE_ENFORCE = "enforce"  # admit only what passes every active check


def _coerce_str_list(value: object) -> List[str]:
    """Coerce an untrusted JSON value into a list of strings.

    A string value is the single-element list ``[value]`` (NOT exploded into
    per-character entries the way ``list(v)`` would); a list/tuple has each
    element stringified; any other type yields an empty list.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _normalize_name(name: str) -> str:
    """Canonical form for kill-switch / allowlist membership comparisons.

    NFKC-normalizes, then casefolds and strips, so a ban on ``"evil-app"`` is
    not evaded by ``"Evil-App"``, ``"evil-app "`` (trailing whitespace), or a
    Unicode-equivalent form.
    """
    return unicodedata.normalize("NFKC", name).strip().casefold()


@dataclass(frozen=True)
class AppAdmissionPolicy:
    """The fleet-controlled trust root. Never sourced from an app."""

    mode: str = MODE_OPEN
    # Kill-switch. Always wins, in any mode.
    banned: List[str] = field(default_factory=list)
    # Marketplace allowlist. None = no allowlist (any non-banned app). A present
    # (even empty) list = only these names are admitted.
    approved: Optional[List[str]] = None
    # Verify-before-run signature requirement.
    require_signature: bool = False
    # signer -> shared secret (POC: HMAC; real impl: publisher public key).
    trust_keys: Dict[str, str] = field(default_factory=dict)

    @staticmethod
    def open_default() -> "AppAdmissionPolicy":
        """The no-policy default: admit everything (no fleet to enforce)."""
        return AppAdmissionPolicy(mode=MODE_OPEN)

    @staticmethod
    def from_dict(d: dict) -> "AppAdmissionPolicy":
        approved = d.get("approved", None)
        return AppAdmissionPolicy(
            mode=str(d.get("mode", MODE_OPEN)),
            banned=_coerce_str_list(d.get("banned", [])),
            approved=(_coerce_str_list(approved) if approved is not None else None),
            # A RESTRICTION, so it fails the other way from a capability grant
            # (see Permissions.from_dict): only a literal `false` turns it off, so
            # a malformed value keeps signature verification ON.
            require_signature=d.get("require_signature", False) is not False,
            trust_keys={str(k): str(v) for k, v in (d.get("trust_keys") or {}).items()},
        )


def _fail_closed_policy() -> AppAdmissionPolicy:
    """The restrictive default used when a present policy cannot be read.

    MODE_ENFORCE with an empty ``approved`` allowlist admits NOTHING beyond the
    always-applied kill-switch, and ``require_signature`` rejects even a signed
    app absent an allowlist entry. This is the fail-closed posture for a
    present-but-unreadable policy.
    """
    return AppAdmissionPolicy(mode=MODE_ENFORCE, require_signature=True, approved=[])


def load_app_admission_policy() -> AppAdmissionPolicy:
    """Load the fleet app admission policy from ``config_dir()/app_admission.json``.

    - ABSENT file → :func:`AppAdmissionPolicy.open_default` (admit): preserves
      the current no-policy default until the seeded-policy mechanism arrives
      with the CPP governance seam.
    - PRESENT-but-unreadable file → :func:`_fail_closed_policy` (deny-all): a
      fleet meant to enforce something; refuse to silently fall open.
    """
    path = config_dir() / _POLICY_FILENAME
    if not path.exists():
        return AppAdmissionPolicy.open_default()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("app_admission.json must be a JSON object")
    except Exception:
        logger.error("app admission policy at %s is unreadable; failing closed", path)
        try:
            sel().log_api_access(
                caller="app_admission",
                operation="policy_load",
                outcome="failed",
                resources=str(path),
                error="unreadable_policy_fail_closed",
                critical=True,
            )
        except Exception:
            logger.debug("admission fail-closed SEL emit unavailable", exc_info=True)
        return _fail_closed_policy()
    return AppAdmissionPolicy.from_dict(data)


def _signature_valid(manifest: "AppManifest", policy: AppAdmissionPolicy) -> bool:
    """Verify the manifest signature against a trust key the POLICY carries.

    POC uses HMAC-SHA256 with a per-signer shared secret. A production impl
    verifies an asymmetric signature against the signer's public key pinned in
    the fleet policy — the shape (policy holds the trust root, app holds only
    the signature) is identical.
    """
    signer = getattr(manifest, "signer", "")
    signature = getattr(manifest, "signature", "")
    secret = policy.trust_keys.get(signer)
    if not secret or not signature:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), manifest.signing_payload(), hashlib.sha256
    ).hexdigest()
    # ``signature`` is attacker-controlled; a non-ASCII value (e.g. "é") makes
    # hmac.compare_digest raise TypeError (it rejects non-ASCII str). Compare on
    # bytes so a malformed signature is a clean deny, not an unhandled 500 (DoS).
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8", "ignore"))


def verified_signer(manifest: "AppManifest | None") -> str:
    """Return the signer id when *manifest* carries a signature this fleet can
    verify, else ``""`` (unsigned, unknown signer, or a signature that failed).

    Read-only observation of the same check :func:`app_admission_denied` runs —
    it does NOT change whether a signature is *required*, and it never denies.
    Callers use it to record WHO signed an app they are about to install;
    ``""`` simply means the provenance carries no verified signer.
    """
    if manifest is None:
        return ""
    if not _signature_valid(manifest, load_app_admission_policy()):
        return ""
    return str(getattr(manifest, "signer", "") or "")


def app_admission_denied(
    name: str,
    manifest: "AppManifest | None" = None,
    action: str = "install",
) -> Optional[str]:
    """Decide whether *name* may be admitted. Returns a denial reason or None.

    Runs BEFORE the app's files are copied / its onInstall script runs, so a
    banned / non-allowlisted / unsigned app never lands on disk or executes.
    """
    policy = load_app_admission_policy()
    norm = _normalize_name(name)

    # 1) Kill-switch always wins, in any mode.
    banned_norm = {_normalize_name(b) for b in policy.banned}
    if norm in banned_norm:
        return f"app {name!r} is banned (kill-switch)"

    # Whether the fleet has configured ANY active enforcement beyond the open
    # default. Only when NOTHING is configured do we take the open fast path.
    enforcing = policy.mode != MODE_OPEN or policy.approved is not None or policy.require_signature
    if not enforcing:
        return None

    # 2) Marketplace allowlist (skipped when no allowlist is configured).
    if policy.approved is not None:
        approved_norm = {_normalize_name(a) for a in policy.approved}
        if norm not in approved_norm:
            return f"app {name!r} is not on the approved allowlist"

    # 3) Verify-before-run signature (skipped unless required). A missing
    #    manifest cannot carry a valid signature, so require_signature denies it.
    if policy.require_signature and (manifest is None or not _signature_valid(manifest, policy)):
        return f"app {name!r} signature invalid or unsigned"

    return None
