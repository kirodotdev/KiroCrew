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
   Under this flag only signature scheme 2 (``apps.package_signature``) is
   accepted: it signs the whole manifest and the SHA-256 of every packaged
   file, and the files are checked wherever the caller names the app's root.

Trust-root: the policy is loaded from ``config_dir()/app_admission.json`` —
never from the app being admitted.  Interim deviation (documented as
accepted-risk): an ABSENT policy file **admits** (preserves the current
no-policy default) rather than failing closed; only a PRESENT-but-unreadable
file fails closed (deny-all).  The seeded-default mechanism that makes absence
itself fail-closed belongs to the CPP governance seam, not this gate.

See ``docs/system-specs/modules/security.md`` (App admission).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

from kiro_crew.apps.package_signature import (
    FILES_KEY,
    declared_code_paths,
    declares_scheme_2,
    inventory_refusal,
    package_signature,
    package_signing_payload,
    unsigned_code_path,
)
from kiro_crew.apps.signed_first import FD_ENV, LIST_ENV
from kiro_crew.config.loader import config_dir
from kiro_crew.platform.admission import canonical_signing_bytes
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


def load_app_admission_policy(*, audit: bool = True) -> AppAdmissionPolicy:
    """Load the fleet app admission policy from ``config_dir()/app_admission.json``.

    - ABSENT file → :func:`AppAdmissionPolicy.open_default` (admit): preserves
      the current no-policy default until the seeded-policy mechanism arrives
      with the CPP governance seam.
    - PRESENT-but-unreadable file → :func:`_fail_closed_policy` (deny-all): a
      fleet meant to enforce something; refuse to silently fall open.

    ``audit=False`` is for a caller on a repeating path (an execution boundary):
    the failure is logged and audited by install and enable admission, not on
    every call.
    """
    path = config_dir() / _POLICY_FILENAME
    if not path.exists():
        return AppAdmissionPolicy.open_default()
    try:
        data = loads_user_json(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("app_admission.json must be a JSON object")
    except Exception:
        if not audit:
            return _fail_closed_policy()
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


def _package_signature_valid(manifest: "AppManifest", policy: AppAdmissionPolicy) -> bool:
    """Scheme 2: the signature covers the whole parsed ``app.json`` object.

    The object (``AppManifest.document``) carries the ``signedFiles`` inventory,
    so this proves the inventory is the publisher's; :func:`_package_files_refusal`
    then compares it with the files on disk.
    """
    document = getattr(manifest, "document", None)
    if not isinstance(document, dict) or not declares_scheme_2(document):
        return False
    secret = policy.trust_keys.get(str(getattr(manifest, "signer", "") or ""))
    signature = str(getattr(manifest, "signature", "") or "")
    if not secret or not signature:
        return False
    expected = package_signature(secret, document)
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8", "surrogatepass"))


def _out_of_install_ui_grant(name: str) -> bool:
    """Whether the operator's dev-mode grant covers a ``ui/`` linked OUT of the install.

    The existing dev-mode authorization (``dev_mode.dev_mode_granted_root``: the
    operator's grant record, the app's ``dev`` flag and governance), bound to the
    current resolved ``ui`` root exactly as the UI route checks it, and only for a
    root that resolves outside the app's install directory: the
    ``--confirm-out-of-install-root`` grant that only an operator at the host can
    make. An in-install ``ui/`` stays checked whatever its dev flag says.
    """
    from kiro_crew.apps.dev_mode import dev_mode_granted_root
    from kiro_crew.apps.manager import app_dir, apps_dir

    try:
        granted = dev_mode_granted_root(name)
        current = os.path.realpath(app_dir(name) / "ui")
        install = Path(os.path.realpath(apps_dir())) / name
    except Exception:  # noqa: BLE001 - an unprovable grant grants nothing
        logger.debug("dev-mode grant for %s could not be read", name, exc_info=True)
        return False
    return granted is not None and granted == current and not Path(current).is_relative_to(install)


def _ui_skip(name: str, manifest: "AppManifest", app_root: Path):
    """The inventory entries the dev-mode grant leaves unchecked, or None for none.

    Only under :func:`_out_of_install_ui_grant`, only for the installed app
    directory, and only for ``ui/`` entries that are not code: every ``ui/`` path
    the manifest names as code, an agent-read resource, a script or a command
    target (``declared_code_paths``, less the UI entry modules dev mode serves
    live) stays checked. Compared case-folded, so a
    case-insensitive volume cannot spell a kept path past the list.
    """
    from kiro_crew.apps.manager import app_dir

    try:
        if Path(app_root).resolve() != app_dir(name).resolve():
            return None
    except OSError:
        return None
    if not _out_of_install_ui_grant(name):
        return None
    kept = [
        candidate.casefold()
        for group in declared_code_paths(manifest, Path(app_root), include_ui=False)
        for candidate in group
        if candidate.casefold().startswith("ui/")
    ]

    def skip(rel: str) -> bool:
        folded = rel.casefold()
        if not folded.startswith("ui/"):
            return False
        return not any(folded == p or folded.startswith(p + "/") for p in kept)

    return skip


#: Audit classifications for a signed-package refusal. The detailed reason names the
#: app and a publisher-written path, so it goes to the caller and never into the
#: audit row (:func:`admission_audit_error`).
PACKAGE_UNVERIFIED = "signed_package_unverified"
PACKAGE_MISMATCH = "signed_package_mismatch"


class PackageRefusal(str):
    """A signed-package refusal: the detailed reason, carrying its audit classification."""

    classification: str

    def __new__(cls, reason: str, classification: str) -> "PackageRefusal":
        refusal = super().__new__(cls, reason)
        refusal.classification = classification
        return refusal


def admission_audit_resources(denied: str, resources: str) -> str:
    """``resources`` for the audit row of a refusal: no app name for a package refusal."""
    return "signed_package" if isinstance(denied, PackageRefusal) else resources


def admission_refusal_text(denied: str) -> str:
    """``blocked by admission policy: <denied>``, keeping a package refusal's class.

    Callers return this to the dashboard routes, whose audit rows then write the
    classification (:func:`admission_audit_error`) rather than the text.
    """
    text = f"blocked by admission policy: {denied}"
    if isinstance(denied, PackageRefusal):
        return PackageRefusal(text, denied.classification)
    return text


def admission_audit_error(denied: str) -> str:
    """``error`` for the audit row of a refusal.

    A signed-package refusal is written as its fixed classification, never its
    reason, which names the app and a publisher-written path; any other refusal is
    written as before.
    """
    return denied.classification if isinstance(denied, PackageRefusal) else denied


def _package_files_refusal(
    name: str, manifest: "AppManifest", app_root: Path
) -> Optional[PackageRefusal]:
    """Why the files under *app_root* differ from the signed package, or None.

    Every code path the signed manifest names under the app root must be in its
    ``signedFiles`` (``unsigned_code_path``), and every listed file must match. A
    self-registered app (:func:`_self_registered`) has no package on disk, so only
    its manifest signature is checked, as before.
    """
    if _self_registered(name, app_root, manifest):
        return None
    try:
        reason = unsigned_code_path(Path(app_root), manifest)
        if reason is None:
            skip = _ui_skip(name, manifest, app_root)
            reason = inventory_refusal(Path(app_root), manifest.document, skip=skip)
    except Exception:  # noqa: BLE001 - files that cannot be read are not the signed files
        logger.warning("signed-package file check for an app could not run", exc_info=True)
        reason = "the packaged files could not be read"
    if not reason:
        return None
    return PackageRefusal(
        f"app {name!r} package does not match its signature: {reason}", PACKAGE_MISMATCH
    )


#: What a self-registered app's installed folder holds: the manifest it posted, its
#: record, and the gateway's own leaves (``app-crons.json`` is the cron definitions
#: the gateway writes from that manifest). Anything else is a package to check. The
#: approved set and the owner's disable are kept under ``member-records/``;
#: ``approved-grants.json`` stays listed because the one-time import leaves an older
#: install's copy in the folder.
_SELF_REGISTERED_ENTRIES = frozenset(
    {"app.json", "installed.json", "approved-grants.json", ".app_secret", "data", "app-crons.json"}
)


def _code_file_in_folder(manifest: "AppManifest", app_root: Path) -> bool:
    """Whether a code path the signed manifest names is an existing file in *app_root*.

    A self-registered app's agents, skills and commands live outside its installed
    folder, so naming them is fine; one that resolves to something present inside the
    folder is code that could run from it.
    """
    real_root = Path(os.path.realpath(app_root))
    for group in declared_code_paths(manifest, Path(app_root)):
        for normal in group:
            candidate = Path(normal) if os.path.isabs(normal) else real_root / normal
            try:
                resolved = Path(os.path.realpath(candidate))
            except (OSError, ValueError):
                continue
            if resolved.is_relative_to(real_root) and os.path.lexists(candidate):
                return True
    return False


def _self_registered(name: str, app_root: Path, manifest: "AppManifest") -> bool:
    """Whether *app_root* is the installed folder of an app that registered itself.

    ``register_external_app`` records ``lifecycle: "app"``: the app runs from its own
    location and its installed folder holds only the manifest it posted, so there is
    no package there to compare with an inventory. The record is the app's to write,
    so it is never enough alone: no code path the verified manifest names may be an
    existing file in the folder, and the folder must hold nothing beyond
    :data:`_SELF_REGISTERED_ENTRIES`. Otherwise the full checks run.
    """
    from kiro_crew.apps.manager import _read_installed, app_dir

    try:
        if Path(app_root).resolve() != app_dir(name).resolve():
            return False
        record = _read_installed(name)
        if record is None or record.lifecycle != "app":
            return False
        if _code_file_in_folder(manifest, app_root):
            return False
        return set(os.listdir(app_root)) <= _SELF_REGISTERED_ENTRIES
    except Exception:  # noqa: BLE001 - an unreadable record or folder is not self-registered
        return False


def _unsigned_reason(name: str) -> PackageRefusal:
    return PackageRefusal(
        f"app {name!r} signature invalid or unsigned (this fleet requires signature "
        "scheme 2 over the manifest and every packaged file: re-sign with "
        "`kirocrew app sign`)",
        PACKAGE_UNVERIFIED,
    )


def verified_signer(manifest: "AppManifest | None") -> str:
    """Return the signer id when *manifest* carries a signature this fleet can
    verify, else ``""`` (unsigned, unknown signer, or a signature that failed).

    Read-only observation of the same check :func:`app_admission_denied` runs —
    it does NOT change whether a signature is *required*, and it never denies.
    Callers use it to record WHO signed an app they are about to install;
    ``""`` simply means the provenance carries no verified signer. Either
    scheme counts here: this records who signed, not whether files match.
    """
    if manifest is None:
        return ""
    policy = load_app_admission_policy()
    if not (_signature_valid(manifest, policy) or _package_signature_valid(manifest, policy)):
        return ""
    return str(getattr(manifest, "signer", "") or "")


def signed_packages_required() -> bool:
    """Whether the fleet sets ``require_signature``; True when the policy cannot be read.

    For the execution side, which reads the policy on every decision: the read
    writes no audit row (install and enable admission record an unreadable
    policy), and an unreadable policy reads as required, as admission does.
    """
    try:
        return load_app_admission_policy(audit=False).require_signature
    except Exception:  # noqa: BLE001 - an unreadable policy fails closed
        logger.debug("app admission policy unreadable", exc_info=True)
        return True


def builtin_admission_exempt(name: str, origin: str) -> bool:
    """Whether an installed app skips admission as a shipped builtin.

    Without a signature policy this is the record's ``origin == "builtin"``, as
    before. Under ``require_signature`` the record is writable by the app, so the
    exemption also needs shipped-package provenance: a package this build ships
    under that name. Never raises: when the policy cannot be read the answer is
    the provenance one.
    """
    if origin != "builtin":
        return False
    from kiro_crew.apps.execution import shipped_builtin_app_root

    try:
        if not load_app_admission_policy(audit=False).require_signature:
            return True
        return _runs_only_shipped_code(name, shipped_builtin_app_root(name))
    except Exception:  # noqa: BLE001 - a builtin is exempt only by what this build ships
        logger.debug("builtin exemption for %s could not be read", name, exc_info=True)
        try:
            return _runs_only_shipped_code(name, shipped_builtin_app_root(name))
        except Exception:  # noqa: BLE001
            return False


#: Manifest sections that name what an app runs. An installed builtin is exempt only
#: while these match the shipped manifest; anything else is checked as an app.
_EXECUTED_SECTIONS = ("backend", "mcpServers", "crons", "setup", "openCommand")


def _runs_only_shipped_code(name: str, shipped_root: Optional[Path]) -> bool:
    """Whether everything *name*'s installed manifest runs is this build's shipped code.

    True only with a shipped package (*shipped_root*, from the gateway's own registry)
    whose manifest, in the form the builtin install writes it, names the same executed
    sections as the installed one, and whose
    module-style backend entry resolves inside that package
    (``execution.shipped_builtin_module_path``). An installed manifest that names a
    different command, a module outside the package (``http.server``) or a path
    outside it is not exempt and takes the full signature and inventory checks.
    """
    if shipped_root is None:
        return False
    from kiro_crew.apps.discovery import _manifest_to_builtin_dict
    from kiro_crew.apps.execution import shipped_builtin_module_path
    from kiro_crew.apps.manager import get_app_manifest
    from kiro_crew.apps.manifest import AppManifest, is_module_style_entry_point

    # The installed copy is what ``register_builtin_apps`` writes: the shipped manifest
    # through ``_manifest_to_builtin_dict``, as JSON. Compared in that same form, so
    # only an added or changed executed item differs.
    shipped = json.loads(
        json.dumps(_manifest_to_builtin_dict(AppManifest.from_json_file(shipped_root / "app.json")))
    )
    installed_manifest = get_app_manifest(name)
    installed = installed_manifest.document if installed_manifest is not None else shipped
    if any(installed.get(key) != shipped.get(key) for key in _EXECUTED_SECTIONS):
        return False
    entry = str((shipped.get("backend") or {}).get("entryPoint") or "")
    if not entry:
        return True
    if not is_module_style_entry_point(entry, shipped_root):
        return False
    return shipped_builtin_module_path(name, entry) is not None


def _acted_on_refusal(name: str, acted_on: object, verified: "AppManifest") -> Optional[str]:
    """Why the manifest the caller acts on is not the verified one, or None.

    The caller read ``app.json`` before the boundary, and the boundary reads it
    again to verify it; without this a swap between the two reads would verify one
    file and run another. Compared as canonical JSON bytes, so ``1``, ``1.0`` and
    ``true`` differ. A ``to_dict`` projection is compared whole except
    ``contributes``, which the app listing rewrites (``contain_command_agents``).
    """
    if acted_on is None or (isinstance(acted_on, Mapping) and not acted_on):
        return f"app {name!r} has no manifest to check against its signature"
    document = getattr(acted_on, "document", None)
    if isinstance(document, dict):
        if package_signing_payload(document) == package_signing_payload(verified.document):
            return None
    elif isinstance(acted_on, Mapping):
        mine = {k: v for k, v in verified.to_dict().items() if k != "contributes"}
        theirs = {k: v for k, v in acted_on.items() if k != "contributes"}
        if canonical_signing_bytes(theirs) == canonical_signing_bytes(mine):
            return None
    return f"app {name!r} manifest changed while it was being checked"


def installed_package_refusal(
    name: str, app_root: Path, acted_on: object
) -> Optional[PackageRefusal]:
    """The signed-package check at an execution or registration boundary.

    None unless the fleet sets ``require_signature``. Code that runs from a shipped
    builtin package (``execution.is_builtin_app``) is exempt. Otherwise the
    installed ``app.json`` must carry a valid scheme-2 signature, *acted_on* (the
    manifest, or its ``to_dict`` projection, the caller is about to act on) must be
    that verified manifest, and the installed app directory must match the signed
    package. Never raises: a check that cannot run is a refusal, as at every other
    execution gate.
    """
    try:
        policy = load_app_admission_policy(audit=False)
        if not policy.require_signature:
            return None
        from kiro_crew.apps.execution import is_builtin_app

        if is_builtin_app(app_root=app_root, app_name=name):
            return None
        from kiro_crew.apps.manager import app_dir, get_app_manifest

        manifest = get_app_manifest(name)
        if manifest is None or not _package_signature_valid(manifest, policy):
            return _unsigned_reason(name)
        mismatch = _acted_on_refusal(name, acted_on, manifest)
        if mismatch is not None:
            return PackageRefusal(mismatch, PACKAGE_UNVERIFIED)
        return _package_files_refusal(name, manifest, app_dir(name))
    except Exception:  # noqa: BLE001 - an unverifiable package must not run
        logger.warning("signed-package check for an app could not run", exc_info=True)
        return PackageRefusal(
            f"app {name!r} signed-package check could not run", PACKAGE_UNVERIFIED
        )


def signed_npm_install_argv(base: list[str], pkg_dir: Path, *, ci_argv: list[str]) -> list[str]:
    """The gateway's npm install command for *pkg_dir*: *base* unless the policy is on.

    Under the fleet signature policy the signed ``package-lock.json`` must decide the
    versions and stay byte-identical, so *ci_argv* (an ``npm ci``) runs when one
    exists; without one, *base* plus ``--no-save`` keeps ``package.json`` as signed.
    Blocking: reads the admission policy.
    """
    if not signed_packages_required():
        return base
    if (Path(pkg_dir) / "package-lock.json").is_file():
        return ci_argv
    return [*base, "--no-save"]


def verified_signed_inventory(name: str) -> Optional[dict[str, str]]:
    """The ``signedFiles`` inventory (path to SHA-256) of *name*'s installed manifest.

    None without the fleet signature policy. Under it, the inventory the installed
    ``app.json`` carries when its scheme-2 signature verifies, else empty: an
    inventory nobody signed names nothing as signed. Blocking.
    """
    try:
        policy = load_app_admission_policy(audit=False)
        if not policy.require_signature:
            return None
        from kiro_crew.apps.manager import get_app_manifest

        manifest = get_app_manifest(name)
        if manifest is None or not _package_signature_valid(manifest, policy):
            return {}
        inventory = manifest.document.get(FILES_KEY)
        if not isinstance(inventory, Mapping):
            return {}
        return {str(key): str(value) for key, value in inventory.items()}
    except Exception:  # noqa: BLE001 - an unverifiable inventory lists nothing
        logger.debug("signed inventory for an app could not be read", exc_info=True)
        return {}


def verified_signed_files(name: str) -> Optional[frozenset[str]]:
    """The keys of :func:`verified_signed_inventory`; None without the policy. Blocking."""
    inventory = verified_signed_inventory(name)
    return None if inventory is None else frozenset(inventory)


#: Bytecode cache dirs held by long-lived children, by spawn key (``backend:<app>``,
#: ``mcp:<app>``): replaced on the next spawn of the same key, removed on release.
_held_pycache_dirs: dict[str, str] = {}
_held_pycache_lock = threading.Lock()
#: Tasks waiting on a child the gateway does not otherwise wait for, so they live.
_pycache_reapers: set[Any] = set()


def new_pycache_dir(name: str) -> Optional[str]:
    """A fresh, empty, owner-only bytecode cache dir for ONE spawn of *name*'s Python.

    None unless the fleet sets ``require_signature``. Under it, a new ``mkdtemp``
    directory (mode 0700) under ``<data home>/run/app-pycache/``: a gateway-owned
    path that sandboxed children see read-only, never the app folder and never a
    path shared between spawns, so nothing planted there can stand in for a signed
    ``.py``. Children run with ``-B`` / ``PYTHONDONTWRITEBYTECODE``, so it stays
    empty. Remove it when the child exits (:func:`remove_pycache_dir` and the hold,
    release and wait helpers below). Blocking.
    """
    if not signed_packages_required():
        return None
    from kiro_crew.owner_only_files import ensure_directory

    parent = config_dir() / "run" / "app-pycache"
    ensure_directory(parent)
    return tempfile.mkdtemp(prefix=f"{name}-", dir=parent)


def remove_pycache_dir(path: Optional[str]) -> None:
    """Remove a dir :func:`new_pycache_dir` made; nothing for None. Never raises."""
    if path:
        shutil.rmtree(path, ignore_errors=True)


def hold_pycache_dir(key: str, path: Optional[str]) -> None:
    """Record *path* as the dir spawn *key* uses, removing the one it used before."""
    with _held_pycache_lock:
        previous = _held_pycache_dirs.pop(key, None)
        if path:
            _held_pycache_dirs[key] = path
    if previous != path:
        remove_pycache_dir(previous)


def held_pycache_dir(key: str) -> Optional[str]:
    """The dir spawn *key* holds, or None."""
    with _held_pycache_lock:
        return _held_pycache_dirs.get(key)


def release_pycache_dir(key: str) -> None:
    """Remove the dir spawn *key* holds, once its child has exited."""
    hold_pycache_dir(key, None)


def release_pycache_dir_after(proc: Any, path: Optional[str]) -> None:
    """Remove *path* once the asyncio child *proc* exits (call on the event loop)."""
    if not path:
        return
    import asyncio

    async def _remove_after_exit() -> None:
        try:
            await proc.wait()
        finally:
            await asyncio.to_thread(remove_pycache_dir, path)

    task = asyncio.get_running_loop().create_task(_remove_after_exit())
    _pycache_reapers.add(task)
    task.add_done_callback(_pycache_reapers.discard)


def mcp_pycache_dir(name: str, *, renew: bool) -> Optional[str]:
    """The bytecode cache dir *name*'s stdio servers name in their launch config.

    kiro-cli starts those servers, so the gateway never sees one exit; the dir lives
    per registration instead. *renew* (a registration) makes a fresh one and removes
    the previous; otherwise (an agent-config rebuild) the held one is reused, or one
    is made when none is held. Deregistration releases it (``mcp:<name>``). None
    without the fleet signature policy. Blocking.
    """
    key = f"mcp:{name}"
    held = None if renew else held_pycache_dir(key)
    if held is not None and os.path.isdir(held):
        return held
    fresh = new_pycache_dir(name)
    hold_pycache_dir(key, fresh)
    return fresh


def signed_pycache_env(prefix: Optional[str]) -> dict[str, str]:
    """Environment for a child that may run app Python, given its :func:`new_pycache_dir`.

    Empty for None (no signature policy). Otherwise Python reads no bytecode from
    the app folder and writes none, so a ``__pycache__`` planted beside a signed
    ``.py`` is never run in place of that ``.py``. Python ignores these under
    ``-E``/``-I``; where the gateway builds the interpreter's argv it adds
    :func:`signed_pycache_flags` too.
    """
    if not prefix:
        return {}
    return {"PYTHONPYCACHEPREFIX": prefix, "PYTHONDONTWRITEBYTECODE": "1"}


def signed_pycache_flags(prefix: Optional[str]) -> list[str]:
    """Interpreter options with the same effect, honoured under ``-E`` and ``-I``.

    Empty for None. Insert right after the interpreter in an argv the gateway builds.
    """
    if not prefix:
        return []
    return ["-B", "-X", f"pycache_prefix={prefix}"]


def signed_first_payload(
    name: str, *, script: bytes | None = None, hidden_dirs: tuple[str, ...] = ()
) -> Optional[str]:
    """The checked list a child's signed-first shim reads, as JSON text.

    The app, its resolved root and every entry of the verified ``signedFiles`` with
    its signed SHA-256: the shim re-hashes them before the target runs (an
    extensionless script or a ``.pyz`` included), and its finder compares only the
    names an import can load. ``unchecked`` names the entries the shim does not
    re-hash: exactly the ``ui/`` files the dev-mode grant leaves out at admission
    (:func:`_ui_skip`), and the files under *hidden_dirs*, directories this spawn
    hides from the child, which the gateway checked at this spawn. *script* is a cron
    script body the gateway already verified, which the launcher runs in place of
    reading the file again. None without the fleet signature policy and when the
    verified inventory is empty. Blocking.
    """
    inventory = verified_signed_inventory(name)
    if not inventory:
        return None
    from kiro_crew.apps.manager import app_dir, get_app_manifest

    root = os.path.realpath(app_dir(name))
    manifest = get_app_manifest(name)
    if manifest is not None and _self_registered(name, Path(app_dir(name)), manifest):
        # A real self-registered app runs from its own location; its installed folder
        # holds only metadata, as at admission, so it gets no shim and no list.
        return None
    skip = _ui_skip(name, manifest, Path(app_dir(name))) if manifest is not None else None
    hidden = tuple(os.path.realpath(path) for path in hidden_dirs)

    def _unchecked(rel: str) -> bool:
        if skip is not None and skip(rel):
            return True
        full = os.path.join(root, *Path(rel).parts)
        return any(full == path or full.startswith(path + os.sep) for path in hidden)

    checked: dict[str, object] = {
        "app": name,
        "root": root,
        "files": dict(sorted(inventory.items())),
        "unchecked": sorted(rel for rel in inventory if _unchecked(rel)),
    }
    if script is not None:
        checked["script"] = base64.b64encode(script).decode("ascii")
    return json.dumps(checked)


def _signed_first_shim() -> str:
    from kiro_crew.apps import signed_first

    return os.path.abspath(signed_first.__file__)


def signed_first_python_args(name: str, args: list) -> list:
    """A Python child's *args* (options, then its target) behind the signed-first shim.

    Unchanged without the fleet signature policy. Under it, ``signed_first.py
    <name>`` goes at the launch target, so the child resolves a signed module name
    to the signed file (:mod:`kiro_crew.apps.signed_first`). The list itself is
    handed over at the spawn (:func:`signed_first_pipe`, or
    :func:`signed_first_launch_env` for a launch config); the shim refuses to run
    without it. Insert after the interpreter in an argv the gateway builds. Blocking.
    """
    if signed_first_payload(name) is None:
        return args
    from kiro_crew.apps.bridges import _normalize_attached_m, _py_target_index

    normalized = _normalize_attached_m(_split_option_clusters(args))
    index = _py_target_index(normalized)
    if index is None:
        # shortcut: a shape with no target the shim can take (-x, a `--` before a
        # dash-led operand, no target) keeps the stock import order; upgrade if an
        # app ships one.
        return args
    return [*normalized[:index], _signed_first_shim(), name, *normalized[index:]]


#: Python options that take a value: the rest of their word, else the next word.
_VALUE_OPTIONS = frozenset("mcXW")


def _split_option_clusters(args: list) -> list:
    """*args* with each clustered short option split the way Python consumes it.

    ``-Bm worker`` and ``-Bmworker`` become ``-B -m worker``; ``-Bc code`` becomes
    ``-B -c code``; ``-uXutf8`` becomes ``-u -X utf8``. A value option takes the rest
    of its word, else the next word. Only the option prefix before the launch
    target is read, and ``-m``/``-c`` end it.
    """
    out: list = []
    index = 0
    while index < len(args):
        arg = args[index]
        if not isinstance(arg, str) or not arg.startswith("-") or arg in ("-", "--"):
            break
        if arg.startswith("--"):
            out.append(arg)
            index += 1
            if arg == "--check-hash-based-pycs" and index < len(args):
                out.append(args[index])
                index += 1
            continue
        index += 1
        for position, letter in enumerate(arg[1:], start=1):
            out.append("-" + letter)
            if letter not in _VALUE_OPTIONS:
                continue
            rest = arg[position + 1 :]
            if rest:
                out.append(rest)
            elif index < len(args):
                out.append(args[index])
                index += 1
            if letter in "mc":
                return [*out, *args[index:]]
            break
    return [*out, *args[index:]]


def signed_first_list_path(name: str) -> Path:
    """Where the gateway writes *name*'s checked list for the stdio servers kiro-cli starts.

    Inside the kiro agents tree, which every sandboxed spawn already mounts read-only
    (``sandbox_tiers._resolved_kiro_agents_targets``), so a child cannot rewrite it.
    The agent scans read only ``*.json`` entries at the top of that tree.
    """
    from kiro_crew.config.paths import kiro_agents_dir

    return kiro_agents_dir() / ".kirocrew-signed-first" / f"{name}.json"


def remove_signed_first_list(name: str) -> None:
    """Remove *name*'s stdio checked-list file (deregistration). Never raises."""
    with contextlib.suppress(OSError):
        signed_first_list_path(name).unlink()


def signed_first_launch_env(name: str, args: list) -> dict[str, str]:
    """The launch-config environment naming the checked list, for a child kiro-cli starts.

    Empty unless *args* carry the shim. The gateway cannot hand such a child a pipe,
    so it writes the list to :func:`signed_first_list_path` and the environment holds
    only that path: a launch's environment size never grows with the inventory.
    """
    if _signed_first_shim() not in args:
        return {}
    payload = signed_first_payload(name)
    if payload is None:
        return {}
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.owner_only_files import ensure_directory

    path = signed_first_list_path(name)
    ensure_directory(path.parent)
    atomic_write(path, payload)
    return {LIST_ENV: str(path)}


@contextlib.contextmanager
def signed_first_pipe(
    name: str,
    argv: list[str],
    *,
    script: bytes | None = None,
    hidden_dirs: tuple[str, ...] = (),
) -> Iterator[tuple[dict[str, str], dict[str, Any]]]:
    """``(env, popen_kwargs)`` that hand the checked list to a child the gateway spawns.

    Both empty unless *argv* carries the shim, so a launch without it is spawned
    exactly as before. Otherwise the list goes into a pipe whose read end only that
    child inherits (``pass_fds`` on POSIX, a ``handle_list`` on Windows) and whose
    number :data:`FD_ENV` names; no file is written. The gateway's own ends close
    when the block exits. Blocking.
    """
    payload = (
        signed_first_payload(name, script=script, hidden_dirs=hidden_dirs)
        if _signed_first_shim() in argv
        else None
    )
    if payload is None:
        yield {}, {}
        return
    read_fd, write_fd = os.pipe()
    data = payload.encode("utf-8")

    def _write() -> None:
        # A thread, so a list larger than the pipe buffer never blocks the spawn;
        # it ends with EPIPE when the child exits unread.
        try:
            with os.fdopen(write_fd, "wb") as stream:
                stream.write(data)
        except OSError:
            pass

    writer = threading.Thread(target=_write, name=f"signed-first-{name}", daemon=True)
    writer.start()
    try:
        if sys.platform == "win32":
            import msvcrt
            import subprocess

            handle = msvcrt.get_osfhandle(read_fd)
            os.set_handle_inheritable(handle, True)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [handle]}
            yield {FD_ENV: str(handle)}, {"startupinfo": startup, "close_fds": True}
        else:
            yield {FD_ENV: str(read_fd)}, {"pass_fds": (read_fd,)}
    finally:
        os.close(read_fd)


#: Admissions that load an app already installed, judged by its recorded version
#: when the manifest names none.
_ACTIVATION_ACTIONS = frozenset({"boot", "enable", "restart"})


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


def app_admission_denied(
    name: str,
    manifest: "AppManifest | None" = None,
    action: str = "install",
    *,
    check_version: bool = True,
    version: str = "",
    app_root: Optional[Path] = None,
) -> Optional[str]:
    """Decide whether *name* may be admitted. Returns a denial reason or None.

    Runs BEFORE the app's files are copied / its onInstall script runs, so a
    banned / non-allowlisted / unsigned app never lands on disk or executes.
    Blocking (it reads the admission policy and the governance profile store):
    an async caller offloads it. ``check_version=False`` skips
    :func:`app_version_refusal`, for a caller on the loop that already asked it
    off the loop. *version* is the caller's own version, used when *manifest* has
    none. *app_root* is the directory holding the package *manifest* came from;
    under ``require_signature`` its files must match the signed inventory. A
    caller with no package on disk (a self-registration, a registry prefetch)
    passes none and only the manifest signature is checked.
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
    #    Only scheme 2 binds the files, so a scheme-1 signature is refused here.
    if policy.require_signature:
        if manifest is None or not _package_signature_valid(manifest, policy):
            return _unsigned_reason(name)
        if app_root is not None:
            return _package_files_refusal(name, manifest, app_root)

    return None
