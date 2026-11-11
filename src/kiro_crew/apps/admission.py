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
   The signature covers the whole manifest, whose ``fileDigests`` lists every
   packaged file, so wherever a tree is at hand admission also checks that the
   tree is exactly the signed one (``apps.signed_content``): the source before
   a copy, the checkout before its build and before ``onInstall``, and the
   installed app at every activation and before each executable surface runs
   (:func:`installed_content_refusal`).

Trust-root: the policy is loaded from ``config_dir()/app_admission.json`` —
never from the app being admitted.  Interim deviation (documented as
accepted-risk): an ABSENT policy file **admits** (preserves the current
no-policy default) rather than failing closed; only a PRESENT-but-unreadable
file fails closed (deny-all).  The seeded-default mechanism that makes absence
itself fail-closed belongs to the CPP governance seam, not this gate.

See ``docs/system-specs/modules/security.md`` (App admission).
"""

from __future__ import annotations

import hmac
import logging
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from kiro_crew.apps.signed_content import (
    InventoryError,
    _InventoryUnavailable,
    app_signature,
    inventory_mismatch,
    packaged_inventory,
    source_runtime_entry,
    unpackaged_entry,
)
from kiro_crew.config.loader import config_dir
from kiro_crew.sel import sel
from kiro_crew.user_json import loads_user_json

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
        trust_keys = d.get("trust_keys")
        if trust_keys is None:
            trust_keys = {}
        if not isinstance(trust_keys, dict):
            # Raised inside load_app_admission_policy's fail-closed boundary, so a
            # malformed map becomes the restrictive policy instead of a crash.
            raise ValueError("app admission trust_keys must map signer names to keys")
        approved = d.get("approved", None)
        return AppAdmissionPolicy(
            mode=str(d.get("mode", MODE_OPEN)),
            banned=_coerce_str_list(d.get("banned", [])),
            approved=(_coerce_str_list(approved) if approved is not None else None),
            # A RESTRICTION, so it fails the other way from a capability grant
            # (see Permissions.from_dict): only a literal `false` turns it off, so
            # a malformed value keeps signature verification ON.
            require_signature=d.get("require_signature", False) is not False,
            trust_keys={str(k): str(v) for k, v in trust_keys.items()},
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
        data = loads_user_json(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("app_admission.json must be a JSON object")
        return AppAdmissionPolicy.from_dict(data)
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
    try:
        expected = app_signature(secret, manifest)
    except (TypeError, ValueError):  # a document that is not JSON data signs nothing
        return False
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


#: Admissions that load an app already installed, judged by its recorded version
#: when the manifest names none, and by the installed tree when no root is given.
_ACTIVATION_ACTIONS = frozenset({"boot", "enable", "restart"})


def signed_content_refusal(name: str, manifest: "AppManifest", app_root: Path) -> Optional[str]:
    """Why the tree at *app_root* is not the one *manifest* signs, or None.

    Compares against the ``fileDigests`` of the document the signature covers
    (``AppManifest.source_document``), so what is checked is exactly what was
    signed. The caller has already verified the signature. Blocking.
    """
    document = getattr(manifest, "source_document", None) or {}
    declared = document.get("fileDigests", {}) if document else manifest.fileDigests
    mismatch = inventory_mismatch(app_root, declared)
    if not mismatch:
        return None
    reason = f"app {name!r} files do not match its signature: {mismatch}"
    return _InventoryUnavailable(reason) if isinstance(mismatch, _InventoryUnavailable) else reason


def _within(path: Path, root: Path) -> bool:
    """Whether *path* is *root* or under it, as spelled or once resolved.

    Either spelling counts, so a caller holding a resolved path (a data home
    behind a link) and one holding the link itself are both judged. An
    unresolvable path is judged too, never waved through.
    """
    if Path(os.path.abspath(path)).is_relative_to(os.path.abspath(root)):
        return True
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError):
        return True


def _installed_root(name: str) -> Path:
    from kiro_crew.apps.manager import app_dir

    return app_dir(name)


def _activation_root(name: str) -> Path:
    """The publisher tree, not metadata alone, for a registry self-managed app."""
    from kiro_crew.apps.manager import _read_installed

    record = _read_installed(name)
    if record is None or record.resources != "app" or not record.source.startswith("registry:"):
        return _installed_root(name)
    from kiro_crew.apps.registry_pipeline.recovery import app_source_dir
    from kiro_crew.apps.registry_pipeline.store_art import (
        _contained_join,
        _is_safe_registry_subdir,
    )

    subdirectory = record.sourceSubdirectory
    if not _is_safe_registry_subdir(subdirectory):
        raise OSError("installed app source subdirectory is unsafe")
    root = _contained_join(app_source_dir(name), subdirectory)
    if root is None:
        raise OSError("installed app source subdirectory escapes its checkout")
    return root


def _activation_tree(name: str) -> Optional[Path]:
    """The tree an activation compares with the signature, or None when the gateway holds none.

    A self-registered app (self-managed, not from the registry) keeps only
    metadata in its installed directory; its packaged files run outside the
    gateway. When that directory holds no packaged file there is nothing to
    compare, so this returns None. The record alone grants nothing: any packaged
    file present is compared, and code the gateway would run from the directory
    is still checked by :func:`installed_content_refusal`.
    """
    root = _activation_root(name)
    if root != _installed_root(name):
        return root
    from kiro_crew.apps.manager import _read_installed

    record = _read_installed(name)
    if record is None or record.resources != "app" or record.source.startswith("registry:"):
        return root
    try:
        held = packaged_inventory(root)
    except InventoryError:  # unreadable or undescribable: let the comparison report it
        return root
    return root if held else None


def signed_execution_root(name: str, fallback: Path) -> Path:
    """Use the publisher checkout for signed self-managed execution, not metadata."""
    if not signature_required() or fallback != _installed_root(name):
        return fallback
    from kiro_crew.apps.execution import shipped_builtin_app_root

    if shipped_builtin_app_root(name) is not None:
        return fallback
    return _activation_root(name)


def signature_required() -> bool:
    """Whether the fleet admission policy requires signed apps. Blocking (one stat)."""
    return load_app_admission_policy().require_signature


def signed_tree_applies(name: str, executed: Path) -> bool:
    """Whether code of *name* at *executed* is judged as a signed installed tree.

    True under a signature policy when *executed* lies in the installed app
    directory or its registry checkout, and is not shipped builtin code. The
    verifier decides which publisher root owns that execution. Builtin provenance
    is decided from the executed path alone (``execution.is_builtin_app``), never
    from the installed record, which lives in the same writable folder.
    """
    if not signature_required():
        return False
    from kiro_crew.apps.execution import is_builtin_app
    from kiro_crew.apps.registry_pipeline.recovery import app_source_dir

    within_app = _within(Path(executed), _installed_root(name)) or _within(
        Path(executed), app_source_dir(name)
    )
    return within_app and not is_builtin_app(app_name=name, app_root=Path(executed))


def verified_installed_manifest(
    name: str,
    executed: Path,
    manifest: "AppManifest | None" = None,
    runs: Optional[str] = None,
    *,
    declarative: bool = False,
) -> "tuple[Optional[str], AppManifest | None]":
    """``(refusal, verified manifest)`` for installed code about to run.

    Returns ``(None, None)`` when no signature check applies
    (:func:`signed_tree_applies`). Otherwise *manifest*, the object whose fields
    the caller is about to execute (the installed ``app.json`` when None), must
    carry a valid signature and the installed tree must match its
    ``fileDigests``; *runs*, a lifecycle script's text, must be one of that
    manifest's ``setup`` scripts. *declarative* marks a registration that runs
    nothing from the tree (message or agent crons, URL-only MCP servers; the
    caller decides, ``bridges_sources._registration_denied``): for the installed
    directory of a self-registered app that holds no packaged file
    (:func:`_activation_tree` is None, the rule activation uses) only the
    signature is checked; any packaged file present is still compared. Refusals
    are audited here, including direct calls and calls through
    :func:`installed_content_refusal`; audit failure never permits execution.
    Blocking: an async caller offloads it.
    """
    if not signed_tree_applies(name, executed):
        return None, None
    if manifest is None:
        from kiro_crew.apps.manager import get_app_manifest

        manifest = get_app_manifest(name)
    if manifest is None or not _signature_valid(manifest, load_app_admission_policy()):
        refusal: Optional[str] = f"app {name!r} signature invalid or unsigned"
    else:
        try:
            root = _activation_root(name)
            # Same rule as activation: a self-registered app's metadata
            # directory with no packaged file holds no tree to compare.
            metadata_only = (
                declarative
                and runs is None
                and Path(executed) == _installed_root(name)
                and _activation_tree(name) is None
            )
        except (OSError, ValueError, RuntimeError):
            refusal = _InventoryUnavailable(f"app {name!r} publisher tree is unavailable")
        else:
            if metadata_only:
                refusal = None
            elif not _within(Path(executed), root) and Path(executed) != _installed_root(name):
                refusal = f"app {name!r} execution is outside its signed publisher tree"
            else:
                refusal = signed_content_refusal(name, manifest, root)
                if refusal is None and root != _installed_root(name):
                    from kiro_crew.apps.manifest import AppManifest

                    try:
                        publisher = AppManifest.from_json_file(root / "app.json")
                        if publisher.signing_payload() != manifest.signing_payload():
                            refusal = f"app {name!r} publisher manifest differs from its installed signature"
                    except OSError:
                        refusal = _InventoryUnavailable(
                            f"app {name!r} publisher manifest is unavailable"
                        )
                    except (ValueError, TypeError):
                        refusal = f"app {name!r} publisher manifest is invalid"
            if refusal is None:
                scripts = {v for v in manifest.setup.to_dict().values() if isinstance(v, str)}
                if runs is not None and runs not in scripts:
                    refusal = f"app {name!r} script is not one its signed manifest declares"
    if refusal:
        try:
            sel().log_api_access(
                caller="app_admission",
                operation="installed_content_verification",
                outcome="denied",
                source="app",
                resources=f"app={name!r} executed={str(executed)!r}",
                error=refusal,
            )
        except Exception:
            logger.debug("installed content denial audit failed", exc_info=True)
        return refusal, None
    return None, manifest


def installed_content_refusal(
    name: str,
    executed: Path,
    manifest: "AppManifest | None" = None,
    runs: Optional[str] = None,
    *,
    declarative: bool = False,
) -> Optional[str]:
    """Why installed code of *name* at *executed* must not run now, or None.

    Asked by each executable surface just before it runs installed app code: the
    backend spawn, every lifecycle script, every hook module load and every agent,
    skill, cron and MCP registration (:func:`verified_installed_manifest` has the
    rules). It detects a packaged file or manifest changed since signing at the
    moment it runs. It is not a boundary against something that writes the
    installed tree between those moments (see the security spec's residuals).
    With no signature policy it costs one stat and never reads the tree.
    *declarative* is for registrations that run nothing from the tree
    (:func:`verified_installed_manifest`). Blocking: an async caller offloads it.
    """
    return verified_installed_manifest(name, executed, manifest, runs, declarative=declarative)[0]


def app_version_refusal(
    name: str, manifest: "AppManifest | None", version: str = "", action: str = ""
) -> Optional[str]:
    """Why the governance ``app_versions`` list refuses *name* at its manifest version.

    *version* is the caller's own version for the app, used when *manifest* has
    none (a registration that sends a version but no manifest). With neither, an
    activation (*action* ``boot``, ``enable`` or ``restart``) uses the installed
    record's version, so a registration whose manifest names no version is judged
    by the version it was registered with. An install or update never falls back.

    Asked as ``<app>@<version>`` by every admission (install, update, boot, restart,
    enable), so a version the policy drops stops loading too. An unnamed scope
    permits on oss; on LTS it answers the baseline, which is off. Blocking.
    """
    from kiro_crew.extension_governance import app_version_denied

    version = str(getattr(manifest, "version", "") or version or "")
    if not version and action in _ACTIVATION_ACTIONS:
        from kiro_crew.apps.manager import _read_installed

        try:
            record = _read_installed(name)
        except Exception:  # noqa: BLE001 - an unreadable record leaves it unknown
            record = None
        version = str(getattr(record, "version", "") or "")
    denied = app_version_denied(name, version)
    return f"app {name!r} version {version!r} is not permitted: {denied}" if denied else None


#: Kinds :func:`signature_refusal_kind` answers. Only ``CONTENT_MISMATCH`` proves
#: tampering: a trusted key's signature verified and the packaged files differ.
CONTENT_MISMATCH = "content_mismatch"
UNSIGNED = "unsigned"
UNTRUSTED_SIGNER = "untrusted_signer"
INVALID_SIGNATURE = "invalid_signature"


class _SignatureRefusal(str):
    """A trusted key's signature verified, but the packaged content does not match it."""

    kind = CONTENT_MISMATCH


class _UnverifiedSignature(str):
    """No signature this fleet can verify: unsigned, untrusted signer, or one that fails.

    Proves nothing about the app's files (a signature made with an older scheme
    lands here), so a boot refuses it for that boot only.
    """

    kind: str

    def __new__(cls, reason: str, kind: str) -> "_UnverifiedSignature":
        refusal = super().__new__(cls, reason)
        refusal.kind = kind
        return refusal


def _is_signature_refusal(reason: Optional[str]) -> bool:
    """Whether *reason* proves the packaged content was changed after a trusted signing.

    The only refusal a boot persists as a disable. Classified by type, never by prose.
    """
    return isinstance(reason, _SignatureRefusal)


def signature_refusal_kind(reason: Optional[str]) -> Optional[str]:
    """The signature kind of an admission refusal, or None for any other refusal."""
    if isinstance(reason, (_SignatureRefusal, _UnverifiedSignature)):
        return reason.kind
    return None


def _unverified_kind(manifest: "AppManifest", policy: AppAdmissionPolicy) -> str:
    """Why :func:`_signature_valid` failed, for the user's next step."""
    if not getattr(manifest, "signature", ""):
        return UNSIGNED
    if not policy.trust_keys.get(getattr(manifest, "signer", "")):
        return UNTRUSTED_SIGNER
    return INVALID_SIGNATURE


def app_admission_denied(
    name: str,
    manifest: "AppManifest | None" = None,
    action: str = "install",
    *,
    check_version: bool = True,
    version: str = "",
    app_root: Optional[Path] = None,
    fresh_checkout: bool = False,
) -> Optional[str]:
    """Decide whether *name* may be admitted. Returns a denial reason or None.

    Runs BEFORE the app's files are copied / its onInstall script runs, so a
    banned / non-allowlisted / unsigned app never lands on disk or executes.
    Blocking (it reads the admission policy and the governance profile store):
    an async caller offloads it. ``check_version=False`` skips
    :func:`app_version_refusal`, for a caller on the loop that already asked it
    off the loop. *version* is the caller's own version, used when *manifest* has
    none.

    *app_root* is the tree *manifest* came from. Under ``require_signature`` it
    must be exactly the signed tree (:func:`signed_content_refusal`); an
    activation (*action* ``boot``, ``enable`` or ``restart``) with no root checks
    the installed copy or the recorded publisher checkout for a self-managed
    registry app; a self-registered app whose installed directory holds no
    packaged file has no tree in the gateway (:func:`_activation_tree`). A
    caller holding only a manifest (the registry's
    pre-clone check, a self-registration) has no tree, and the check runs where
    the tree appears. *fresh_checkout* marks *app_root* as a clone made by this
    install, which holds only committed content: under ``require_signature`` it
    may hold nothing under a name the signed file list leaves out
    (``signed_content.unpackaged_entry``), since the build and ``onInstall`` run
    there in place. Install/update sources also refuse root runtime entries that
    survive the copy (``data`` and ``app-crons.json``); registry publisher trees
    refuse ``.app_secret`` too, at every phase, since scripts run there in place.
    These source checks never apply to installed activation roots.
    """
    policy = load_app_admission_policy()
    norm = _normalize_name(name)

    # 1) Kill-switch always wins, in any mode.
    banned_norm = {_normalize_name(b) for b in policy.banned}
    if norm in banned_norm:
        return f"app {name!r} is banned (kill-switch)"

    if check_version:
        refusal = app_version_refusal(name, manifest, version, action)
        if refusal:
            return refusal

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
    if policy.require_signature:
        if manifest is None:
            # The manifest reader also returns None on temporary I/O failures.
            # Refuse this activation without claiming a proven signature failure.
            return f"app {name!r} signature invalid or unsigned"
        if not _signature_valid(manifest, policy):
            return _UnverifiedSignature(
                f"app {name!r} signature invalid or unsigned", _unverified_kind(manifest, policy)
            )
        root = app_root
        if root is None and action in _ACTIVATION_ACTIONS:
            try:
                root = _activation_tree(name)
            except (OSError, ValueError, RuntimeError):
                return _InventoryUnavailable(f"app {name!r} publisher tree is unavailable")
        if root is not None and fresh_checkout:
            planted = unpackaged_entry(root)
            if planted:
                return f"app {name!r} checkout holds content its signature cannot cover: {planted}"
        if root is not None and action in {"install", "update", "install_from_registry"}:
            planted = source_runtime_entry(root, runs_in_place=action == "install_from_registry")
            if planted:
                return f"app {name!r} source holds unsigned runtime content: {planted}"
        if root is not None and not fresh_checkout and action == "install_from_registry":
            from kiro_crew.apps.signed_checkout import tracked_unpackaged_entry

            planted = tracked_unpackaged_entry(root)
            if planted:
                return f"app {name!r} checkout holds publisher content its signature cannot cover: {planted}"
        if root is not None:
            refusal = signed_content_refusal(name, manifest, root)
            if isinstance(refusal, _InventoryUnavailable):
                return refusal
            return _SignatureRefusal(refusal) if refusal else None

    return None
