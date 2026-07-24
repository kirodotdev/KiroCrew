"""Plugin admission control — the gate that decides whether a plugin may load.

This is the policy layer on top of the structural discovery gates
(fail-closed discovery, contract-version, ADD-only floor). It lets a managed
fleet **reject or ban** a plugin before its code is ever imported, using a
defense-in-depth model:

1. **Kill-switch (`banned`)** — a fleet can ban a plugin by name; the ban always
   wins, even in an otherwise-open policy. This is the R-08 / M-09 remote-disable
   control.
2. **Marketplace allowlist (`approved`)** — when the policy carries a non-empty
   allowlist, only listed plugins are admitted. This is the marketplace review
   gate: a plugin is admitted only after it has been reviewed and added.
3. **Verify-before-run signature (`require_signature`)** — the plugin ships a
   signed manifest; admission verifies the signature against a trust key the
   *policy* (not the plugin) carries, before `ep.load()`. This is the R-11 /
   M-12 supply-chain control.
4. **Capability ceiling** — the manifest declares the capabilities the plugin
   requests (tools, network egress, credential paths). Admission rejects a
   plugin whose declared capabilities exceed the policy ceiling.

Trust-root invariant: the admission policy is loaded from a **fleet-controlled
source** (`KIROCREW_ADMISSION_POLICY` env path, or
`~/.kirocrew/admission_policy.json`), never from the plugin being admitted, so a
plugin cannot approve or un-ban itself. The public edition ships **no** policy →
default-open (admit), preserving today's behavior; a managed fleet ships a policy
and the gate enforces.

The plugin manifest is read **without importing the plugin module** (from the
installed distribution's files), so a malicious plugin's code never runs before
the admission decision.

See ``docs/system-specs/modules/platform-context.md`` (Plugin admission).
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import importlib.metadata as _md
import json
import logging
import os
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from kiro_crew.config.paths import config_dir

if TYPE_CHECKING:
    import importlib.metadata

logger = logging.getLogger(__name__)

# Where a managed fleet drops the admission policy. Env wins so a fleet push can
# point at a managed, read-only location.
_POLICY_ENV = "KIROCREW_ADMISSION_POLICY"
_POLICY_DEFAULT_LEAF = "admission_policy.json"


def _policy_default_path() -> Path:
    """Resolve the default admission-policy path lazily.

    Deferred (not a module-level ``config_dir()`` capture) so importing this
    trust-root module never fires ``config_dir()`` — and thus the one-time
    data-home migration — as an import side effect. The migration must happen
    only at the single chosen point (``ensure_data_home()`` in the CLI prologue).
    Callers (and tests) resolve through these accessors rather than a captured
    module-level constant.
    """
    return config_dir() / _POLICY_DEFAULT_LEAF


def _seed_marker_path() -> Path:
    return _policy_default_path().parent / ".migrations" / "admission_policy_seeded"


def _checksum_path() -> Path:
    return _policy_default_path().parent / ".migrations" / "admission_policy.sha256"


# The manifest a plugin ships (read import-free from its distribution files).
_MANIFEST_FILENAME = "kirocrew_plugin.json"

# Policy modes.
MODE_OPEN = "open"  # admit unless explicitly banned (public default)
MODE_ENFORCE = "enforce"  # admit only what passes every active check


def _coerce_str_list(value: object) -> List[str]:
    """Coerce an untrusted JSON value into a list of strings.

    Guards the ``{k: list(v)}`` footgun: a string value is the single-element
    list ``[value]`` (NOT exploded into per-character entries the way ``list(v)``
    would), and a list/tuple has each element stringified.  Any other type
    (number, bool, dict, None) yields an empty list so a malformed manifest is
    treated as declaring nothing rather than admitting a garbage capability set
    into the signed payload / ceiling check.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _normalize_name(name: str) -> str:
    """Canonical form for kill-switch / allowlist membership comparisons.

    NFKC-normalizes, then casefolds and strips, so a ban on ``"amazon-evil"`` is
    not evaded by an entry-point name of ``"Amazon-Evil"``, ``"amazon-evil "``
    (trailing whitespace from hand-edited JSON), OR a Unicode-equivalent form —
    e.g. an NFD-decomposed accent (``é`` as ``e`` + U+0301) or a compatibility
    ligature. A publisher controls its package's Unicode form, so without
    canonicalization an NFD plugin name slips past an NFC-form ban.
    """
    return unicodedata.normalize("NFKC", name).strip().casefold()


@dataclass(frozen=True)
class PluginManifest:
    """A plugin's self-declaration, read before its code is imported."""

    name: str
    publisher: str = ""
    version: str = ""
    # Requested capabilities, e.g. {"tools": [...], "egress": [...], "paths": [...]}.
    capabilities: Dict[str, List[str]] = field(default_factory=dict)
    # Detached signature over the canonical manifest (sans this field).
    signature: str = ""

    @staticmethod
    def from_dict(d: dict) -> "PluginManifest":
        return PluginManifest(
            name=str(d.get("name", "")),
            publisher=str(d.get("publisher", "")),
            version=str(d.get("version", "")),
            capabilities={
                str(k): _coerce_str_list(v) for k, v in (d.get("capabilities") or {}).items()
            },
            signature=str(d.get("signature", "")),
        )

    def signing_payload(self) -> bytes:
        """Canonical bytes the signature covers (manifest minus the signature)."""
        body = {
            "name": self.name,
            "publisher": self.publisher,
            "version": self.version,
            "capabilities": {k: sorted(v) for k, v in sorted(self.capabilities.items())},
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class AdmissionPolicy:
    """The fleet-controlled trust root. Never sourced from a plugin."""

    mode: str = MODE_OPEN
    require_signature: bool = False
    # publisher -> shared secret (POC: HMAC; real impl: publisher public key).
    trust_keys: Dict[str, str] = field(default_factory=dict)
    # Marketplace allowlist. None = no allowlist (any non-banned plugin). A
    # present (even empty) list = only these names are admitted.
    approved: Optional[List[str]] = None
    # Kill-switch. Always wins, in any mode.
    banned: List[str] = field(default_factory=list)
    # Per-capability ceiling, e.g. {"egress": ["*.example.com"], "paths": []}.
    # A declared capability value not covered by the ceiling rejects the plugin.
    capability_ceiling: Dict[str, List[str]] = field(default_factory=dict)

    @staticmethod
    def open_default() -> "AdmissionPolicy":
        """The public edition's policy: admit everything (no fleet to enforce)."""
        return AdmissionPolicy(mode=MODE_OPEN)

    @staticmethod
    def from_dict(d: dict) -> "AdmissionPolicy":
        approved = d.get("approved", None)
        return AdmissionPolicy(
            mode=str(d.get("mode", MODE_OPEN)),
            require_signature=bool(d.get("require_signature", False)),
            trust_keys={str(k): str(v) for k, v in (d.get("trust_keys") or {}).items()},
            approved=(_coerce_str_list(approved) if approved is not None else None),
            banned=_coerce_str_list(d.get("banned", [])),
            capability_ceiling={
                str(k): _coerce_str_list(v)
                for k, v in (d.get("capability_ceiling") or {}).items()
            },
        )


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason: str
    manifest: Optional[PluginManifest] = None


# One-shot marker (``_SEED_MARKER``) and seeded-policy sha256 sidecar
# (``_CHECKSUM_PATH``) are resolved lazily via the module ``__getattr__`` above
# (derived from ``_policy_default_path()``), for the same import-side-effect
# reason. ``_SEED_MARKER`` records that the first-run seed already ran, so a
# DELETION of the seeded policy afterward is NOT silently re-seeded (fail
# closed); ``_CHECKSUM_PATH`` exists only for the file WE seeded — env-override /
# fleet-managed policies carry no sidecar contract.

# The permissive policy body seeded at first run.  ``mode=open`` + ``approved``
# absent (None) reproduces today's admit-any-non-banned behavior, so a fresh
# install is unaffected — the security gain is that the file's PRESENCE is now
# the intended-open signal, and its ABSENCE fails closed.
_DEFAULT_POLICY_BODY: Dict[str, object] = {
    "mode": MODE_OPEN,
    "require_signature": False,
    "banned": [],
    "capability_ceiling": {},
    "_comment": (
        "Default permissive admission policy seeded by KiroCrew at first run. "
        "Deleting this file DISABLES plugin admission (fail-closed): "
        "load_admission_policy() then returns MODE_ENFORCE with an empty allowlist "
        "and the dashboard governance indicator shows Disabled. To restrict "
        "admission, set mode='enforce' and populate 'approved' / 'require_signature'."
    ),
}


def _fail_closed_policy() -> AdmissionPolicy:
    """The restrictive default used whenever the policy cannot be verified.

    MODE_ENFORCE with an empty ``approved`` allowlist admits NOTHING beyond the
    always-applied kill-switch, and ``require_signature`` rejects even a signed
    plugin absent an allowlist entry.  This is the fail-closed posture for a
    missing or unreadable policy (fail-closed).
    """
    return AdmissionPolicy(mode=MODE_ENFORCE, require_signature=True, approved=[])


def seed_default_policy() -> bool:
    """One-time: write the permissive default policy file at first run.

    Establishes the invariant that "file present & permissive =
    intended open state" is distinguishable from "file absent = tampering /
    misconfig = fail closed".  Guarded by a one-shot marker so that DELETING the
    policy afterward is never silently re-seeded — a later
    ``load_admission_policy`` then returns :func:`_fail_closed_policy` and the
    governance health indicator flips to Disabled.

    Never clobbers an existing policy file (a managed fleet may ship its own) and
    is skipped entirely when the ``KIROCREW_ADMISSION_POLICY`` env override is
    set.  The marker is written even when no file was created, so a later start
    cannot re-seed / clobber.  Best-effort: returns True only when it wrote the
    seed; never raises (first-run is best-effort).
    """
    seed_marker = _seed_marker_path()
    default_path = _policy_default_path()
    checksum_path = _checksum_path()
    try:
        if seed_marker.exists():
            return False
        env_set = bool(os.environ.get(_POLICY_ENV, "").strip())
        wrote = False
        if not env_set and not default_path.exists():
            body_text = json.dumps(_DEFAULT_POLICY_BODY, indent=2) + "\n"
            default_path.parent.mkdir(parents=True, exist_ok=True)
            default_path.write_text(body_text, encoding="utf-8")
            # Record the integrity baseline for the file we just wrote so a later
            # modification is detectable at load (advisory — see load below).
            checksum_path.parent.mkdir(parents=True, exist_ok=True)
            checksum_path.write_text(
                hashlib.sha256(body_text.encode("utf-8")).hexdigest() + "\n",
                encoding="utf-8",
            )
            wrote = True
        seed_marker.parent.mkdir(parents=True, exist_ok=True)
        seed_marker.write_text(
            datetime.now(tz=timezone.utc).isoformat() + "\n", encoding="utf-8"
        )
        return wrote
    except Exception:
        logger.warning("first-run: admission policy seed failed", exc_info=True)
        return False


def _posture_of(policy: AdmissionPolicy) -> str:
    """Classify a parsed policy for the governance health indicator.

    "enforcing" when the fleet configured ANY active enforcement (mirrors the
    ``enforcing`` fast-path in :func:`evaluate_admission`); "permissive"
    otherwise (the seeded open default — governance present but not restricting).
    """
    enforcing = (
        policy.mode != MODE_OPEN
        or policy.approved is not None
        or policy.require_signature
        or bool(policy.capability_ceiling)
    )
    return "enforcing" if enforcing else "permissive"


def _record_admission_posture(posture: str) -> None:
    """Best-effort: publish the admission posture to the health signal."""
    try:
        from kiro_crew.platform.governance_health import set_admission_posture

        set_admission_posture(posture)
    except Exception:
        logger.debug("admission posture publish unavailable", exc_info=True)


def _audit_admission_fail_closed(reason: str) -> None:
    """Best-effort: record an admission fail-closed / integrity event.

    Emits an ERROR-level SEL ``governance_degraded`` record with
    ``failed_closed=True`` (critical / synchronous) and sets the process-global
    governance health signal so the dashboard indicator reflects the trip.  Never
    raises — admission loading must not be broken by an audit failure, and this
    runs on the bootstrap path where SEL may not be fully initialized.
    """
    try:
        from kiro_crew.platform.governance_health import mark_governance_incident

        mark_governance_incident("failed_closed", detail=f"admission:{reason}")
    except Exception:
        logger.debug("governance health mark unavailable", exc_info=True)
    try:
        from kiro_crew.sel import sel

        # session_key "_host": this in-process admission load is a host action,
        # not driven by any user-facing surface (mirrors HOST_SESSION_KEY).
        sel().log_governance_degraded(
            session_key="_host",
            chokepoint="admission_policy_load",
            scope="apps.admission",
            reason=reason,
            failed_closed=True,
        )
    except Exception:
        logger.debug("admission fail-closed SEL emit unavailable", exc_info=True)


def _verify_seed_integrity(policy_bytes: bytes) -> None:
    """Advisory integrity check on the seeded default policy.

    Compares the on-disk policy against the sha256 baseline written at seed time.
    A mismatch is RECORDED (ERROR log + critical SEL + health mark) but does NOT
    override the parsed policy: the file is user-owned, so its checksum cannot be
    a trust anchor (an attacker running as the user could recompute the sidecar),
    and hard-denying on mismatch would only break legitimate operator edits.  The
    value here is DETECTION + observability, not enforcement.  No sidecar (older
    seed / manually-created file) → nothing to verify.  Never raises.
    """
    checksum_path = _checksum_path()
    try:
        if not checksum_path.exists():
            return
        expected = checksum_path.read_text(encoding="utf-8").strip()
        actual = hashlib.sha256(policy_bytes).hexdigest()
        if not hmac.compare_digest(expected, actual):
            logger.error(
                "admission policy at %s does not match its seed checksum "
                "(modified since first-run); recording integrity event",
                _policy_default_path(),
            )
            # Detection only: record for the audit trail but do
            # NOT drive the dashboard to "degraded".  The seeded policy is
            # user-owned and MEANT to be edited (e.g. to turn on enforcement), so
            # a mismatch is EXPECTED on legitimate edits — flagging every edit as
            # degraded would be alert fatigue.  "degraded" is reserved for genuine
            # fail-closed / error trips (absent, unreadable, chokepoint errors).
            try:
                from kiro_crew.sel import sel

                sel().log_api_access(
                    caller="_host",
                    operation="admission_policy_integrity",
                    outcome="mismatch",
                    source="startup",
                    error="modified_since_seed",
                )
            except Exception:
                logger.debug("admission integrity SEL emit unavailable", exc_info=True)
    except Exception:
        logger.debug("admission integrity check failed to run", exc_info=True)


def load_admission_policy() -> AdmissionPolicy:
    """Load the fleet admission policy — fail-closed when it cannot be verified.

    A MISSING policy (no ``KIROCREW_ADMISSION_POLICY`` env override and no file at
    the default path) returns :func:`_fail_closed_policy` (MODE_ENFORCE, empty
    allowlist) rather than admitting everything.  A permissive default file is
    seeded once at first run by :func:`seed_default_policy` (via
    ``agent.run_first_run_setup``), so a fresh install still admits plugins by
    intent; deleting that file therefore DISABLES admission (fail-closed) instead
    of silently reverting to open.  A present-but-unreadable file is
    likewise fail-closed.
    """
    raw = os.environ.get(_POLICY_ENV, "").strip()
    default_path = _policy_default_path()
    path: Optional[Path] = None
    if raw:
        path = Path(raw)
    elif default_path.exists():
        path = default_path
    if path is None:
        # No env override and no file at the default path.  Something the
        # first-run seed (or a managed fleet) should have placed is absent —
        # fail closed rather than silently admit everything.
        logger.error(
            "no admission policy found (env %s unset, %s absent); failing closed",
            _POLICY_ENV,
            default_path,
        )
        _record_admission_posture("unverified")
        _audit_admission_fail_closed("no_policy_file")
        return _fail_closed_policy()
    try:
        raw_text = path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception:
        # A present-but-unreadable policy is a fail-closed signal: a fleet meant
        # to enforce something. Refuse to silently fall open.
        logger.error("admission policy at %s is unreadable; failing closed", path)
        _record_admission_posture("unverified")
        _audit_admission_fail_closed("unreadable")
        return _fail_closed_policy()
    # Advisory integrity check on the file WE seeded (not env-override paths).
    if path == default_path:
        _verify_seed_integrity(raw_text.encode("utf-8"))
    policy = AdmissionPolicy.from_dict(data)
    _record_admission_posture(_posture_of(policy))
    return policy


def _read_plugin_manifest(ep: "importlib.metadata.EntryPoint") -> Optional[PluginManifest]:
    """Read a plugin's manifest WITHOUT importing its module.

    Locates the manifest among the entry point's installed distribution files.
    Returns None when the plugin ships no manifest.
    """
    dist = getattr(ep, "dist", None)
    if dist is None:
        # Python 3.9 fallback: scan distributions for one owning this entry.
        try:
            for cand in _md.distributions():
                for e in cand.entry_points:
                    if e.group == ep.group and e.name == ep.name and e.value == ep.value:
                        dist = cand
                        break
                if dist is not None:
                    break
        except Exception:
            logger.debug("could not resolve distribution for entry point", exc_info=True)
            return None
    if dist is None:
        return None
    # 1) Try a packaged data file shipped inside the distribution.
    try:
        for f in dist.files or []:
            if f.name == _MANIFEST_FILENAME:
                text = f.read_text()  # type: ignore[attr-defined]
                if text:
                    return PluginManifest.from_dict(json.loads(text))
    except Exception:
        logger.debug("manifest file scan failed", exc_info=True)
    # 2) Fall back to a dist-info metadata file of the same name.
    try:
        text = dist.read_text(_MANIFEST_FILENAME)
        if text:
            return PluginManifest.from_dict(json.loads(text))
    except Exception:
        logger.debug("manifest dist-info read failed", exc_info=True)
    return None


def _signature_valid(manifest: PluginManifest, policy: AdmissionPolicy) -> bool:
    """Verify the manifest signature against a trust key the POLICY carries.

    POC uses HMAC-SHA256 with a per-publisher shared secret. A production
    implementation verifies an asymmetric signature against the publisher's
    public key pinned in the fleet policy — the shape (policy holds the trust
    root, plugin holds only the signature) is identical.
    """
    secret = policy.trust_keys.get(manifest.publisher)
    if not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), manifest.signing_payload(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, manifest.signature)


def _capabilities_within_ceiling(
    manifest: PluginManifest, policy: AdmissionPolicy
) -> Optional[str]:
    """Return a rejection reason if any declared capability exceeds the ceiling."""
    for cap, requested in manifest.capabilities.items():
        ceiling = policy.capability_ceiling.get(cap)
        if ceiling is None:
            # Capability category not granted at all by the fleet.
            if requested:
                return f"capability {cap!r} not permitted by fleet policy"
            continue
        if "*" in ceiling:
            continue
        # Ceiling entries are case-SENSITIVE globs (e.g. "*.example.com" matches
        # "api.example.com"), matching the documented policy shape. Use
        # fnmatchcase (NOT fnmatch) so the admission decision is deterministic
        # across platforms — plain fnmatch runs through os.path.normcase, which
        # case-folds on macOS but not Linux, so the same policy would admit/reject
        # differently per OS. A requested value exceeds the ceiling only if NO
        # ceiling pattern matches it.
        over = [r for r in requested if not any(fnmatch.fnmatchcase(r, pat) for pat in ceiling)]
        if over:
            return f"capability {cap!r} requests {over} beyond ceiling {ceiling}"
    return None


def evaluate_admission(
    ep: "importlib.metadata.EntryPoint", policy: AdmissionPolicy
) -> AdmissionDecision:
    """Decide whether the plugin behind *ep* may load. Runs before ``ep.load()``."""
    manifest = _read_plugin_manifest(ep)
    plugin_name = manifest.name if manifest else ep.name

    # 1) Kill-switch always wins, in any mode.  Match case/whitespace-insensitively
    #    so a ban cannot be evaded by a name-case or trailing-whitespace mismatch
    #    (the manifest name and the entry-point name are both checked).
    banned_norm = {_normalize_name(b) for b in policy.banned}
    if _normalize_name(plugin_name) in banned_norm or _normalize_name(ep.name) in banned_norm:
        return AdmissionDecision(False, f"plugin {plugin_name!r} is banned (kill-switch)", manifest)

    # Whether the fleet has configured ANY active enforcement beyond the open
    # default.  A capability_ceiling counts: an operator who sets a ceiling
    # expects it enforced even in open mode (otherwise the ceiling is a silent
    # no-op).  Only when NOTHING is configured do we take the open fast path.
    enforcing = (
        policy.mode != MODE_OPEN
        or policy.approved is not None
        or policy.require_signature
        or bool(policy.capability_ceiling)
    )
    if not enforcing:
        # Truly-open policy: admit (the ban check above still applied).
        return AdmissionDecision(True, "admitted (open policy)", manifest)

    # From here the fleet is enforcing something; a manifest is required so the
    # checks below (allowlist / signature / capability ceiling) have something
    # to evaluate.
    if manifest is None:
        return AdmissionDecision(
            False, f"plugin {ep.name!r} ships no {_MANIFEST_FILENAME} manifest", None
        )

    # 2) Marketplace allowlist (skipped when no allowlist is configured).
    #    Both the manifest name AND the entry-point name must appear on the
    #    allowlist.  Checking only the manifest name would let a malicious
    #    package spoof an approved identity via kirocrew_plugin.json; checking
    #    ep.name anchors admission to the verifiable distribution identity.
    approved_norm = {_normalize_name(a) for a in policy.approved} if policy.approved else set()
    if policy.approved is not None:
        manifest_approved = _normalize_name(plugin_name) in approved_norm
        ep_approved = _normalize_name(ep.name) in approved_norm
        if not manifest_approved or not ep_approved:
            return AdmissionDecision(
                False,
                f"plugin {plugin_name!r} (ep={ep.name!r}) is not on the approved allowlist",
                manifest,
            )

    # 3) Verify-before-run signature (skipped unless required).
    if policy.require_signature and not _signature_valid(manifest, policy):
        return AdmissionDecision(
            False, f"plugin {plugin_name!r} signature invalid or unsigned", manifest
        )

    # 4) Capability ceiling — enforced whenever a ceiling is configured, in any
    #    mode (this is what makes a ceiling meaningful under an open policy).
    cap_reason = _capabilities_within_ceiling(manifest, policy)
    if cap_reason:
        return AdmissionDecision(False, f"plugin {plugin_name!r}: {cap_reason}", manifest)

    return AdmissionDecision(True, f"admitted ({policy.mode} policy)", manifest)
