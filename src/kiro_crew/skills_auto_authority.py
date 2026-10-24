"""The private auto-skill authority: where it lives beneath the masked ``tag-grants``
directory, the provenance record that certifies it, the gateway-startup boundary that
provisions and process-certifies it, the operator retirement that moves it aside, and
the verdicts every staging, promotion and live mutation consults before it acts.

The process-wide certificate and refusal (``_STARTUP_AUTHORITY_BINDING`` and
``_STARTUP_AUTHORITY_REFUSAL``) are rebound here and nowhere else, so the facade serves
them from this module. The module-level checks build a loader with only ``_dir`` set;
``kiro_crew.skills`` hands its class over through ``_bind_loader_type`` once it has
defined it, because an owner never imports its facade.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.

Instance fields read: ``_dir``.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from kiro_crew.config.loader import config_dir
from kiro_crew.constants import AUTO_SKILL_PRIVATE_STATE_DIRNAME
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.security import is_sensitive_path
from kiro_crew.skills_auto_model import (
    _AUTHORITY_PROVENANCE_DOMAIN,
    _AUTHORITY_PROVENANCE_MAX_BYTES,
    _AUTHORITY_PROVENANCE_NAME,
    _AUTHORITY_PROVENANCE_PARENT,
    _AUTHORITY_PROVENANCE_VERSION,
    _AUTHORITY_RETIRE_COMMAND,
    _DELEGATED_SANDBOX_REFUSAL,
    _NO_REPLACE_RENAME_REFUSAL,
    _RENAME_PROBE_PREFIX,
    _SANDBOX_MASK_UNAVAILABLE_REFUSAL,
    _SANDBOX_OFF_REFUSAL,
    AUTO_CLAIMS_DIRNAME,
    AUTO_EVIDENCE_DIRNAME,
    AUTO_LIVE_QUARANTINE_DIRNAME,
    AUTO_LOCKS_DIRNAME,
    AUTO_PENDING_DIRNAME,
    AUTO_QUARANTINE_DIRNAME,
    AUTO_SKILL_NAMESPACE,
    _bounded_stale_claim_names,
    _CertifiedAuthorityBinding,
    _PinnedPrivateState,
    _PinnedSkillParent,
    _StaleClaimScanOverflow,
    _TaggedFileIdentity,
)
from kiro_crew.skills_auto_pinning import _DIR_FD_SUPPORTED, _identity_payload
from kiro_crew.skills_discovery import SKILLS_DIR_NAME

if TYPE_CHECKING:
    from kiro_crew.skills import SkillsLoader

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


#: The loader class whose instances carry the pinned helpers the module-level checks below
#: call. ``kiro_crew.skills`` binds ``SkillsLoader`` here once it has defined it.
_LOADER_TYPE: type[SkillsLoader] | None = None


def _bind_loader_type(loader_type: type[SkillsLoader]) -> None:
    """Record the loader class; ``kiro_crew.skills`` calls this once it defines the class."""
    global _LOADER_TYPE
    _LOADER_TYPE = loader_type


def _bare_loader(skills_dir: Path) -> SkillsLoader:
    """A loader holding only ``_dir``, for a check that runs before any loader exists."""
    loader_type = _LOADER_TYPE
    if loader_type is None:  # pragma: no cover - the facade binds it when it loads
        raise RuntimeError("kiro_crew.skills has not bound the loader class yet")
    loader = loader_type.__new__(loader_type)
    loader._dir = skills_dir
    return loader


_AUTHORITY_HOME_IDENTITIES_LOCK = threading.RLock()
_AUTHORITY_HOME_IDENTITIES: dict[str, tuple[str, _TaggedFileIdentity]] = {}
_STARTUP_AUTHORITY_BINDING: _CertifiedAuthorityBinding | None = None


#: Why this gateway's startup refused to provision or certify auto-skill
#: authority, when it did. Set only by
#: :func:`initialize_gateway_auto_skill_private_authority`; read by
#: :func:`auto_skill_promotion_disabled_reason`.
_STARTUP_AUTHORITY_REFUSAL: str | None = None


def _normalized_authority_path(path: Path) -> str:
    """Normalize one absolute spelling without resolving its symlinks."""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(str(path)))))


def _gateway_authority_homes() -> tuple[Path, Path]:
    """Return the selected canonical home and unresolved configured spelling."""
    from kiro_crew.config.paths import _valid_override_home

    selected = Path(config_dir())
    configured = selected
    raw_home = os.environ.get("KIROCREW_HOME")
    if raw_home and _valid_override_home() is not None:
        configured = Path(raw_home).expanduser()
    return selected, configured


def _auto_skill_authority_masked_view_reason() -> str | None:
    """Why this process cannot prove authority absence from its filesystem view."""
    from kiro_crew import sandbox  # deferred: the sandbox module is heavy and gateway-only

    evidence = sandbox.agent_confinement_evidence()
    if evidence is None:
        return None
    return (
        "the auto-skill authority cannot be observed from inside the agent sandbox "
        f"({evidence}); its masked view cannot prove the authority root absent"
    )


def _auto_skill_authority_root_exists() -> bool:
    """Whether this data home may name an auto-skill authority root.

    Root absence is accepted only at an unmasked gateway or operator boundary.
    Inside an agent sandbox, ``tag-grants`` may be an empty bind-masked or
    Seatbelt-confined view while a real authority and lock exist outside it, so
    that view can never prove absence and is reported as existing. Any unreadable,
    retargeted, linked, or otherwise indeterminate home likewise fails closed
    toward existence. Re-read on every call: an operator retirement or a gateway
    provisioning the root changes the answer.
    """
    if _auto_skill_authority_masked_view_reason() is not None:
        return True
    try:
        selected, configured = _gateway_authority_homes()
        configured = Path(os.path.abspath(os.path.expanduser(str(configured))))
        canonical_home = configured.resolve(strict=True)
        if selected.resolve(strict=True) != canonical_home:
            return True
        root = canonical_home / _AUTHORITY_PROVENANCE_PARENT / AUTO_SKILL_PRIVATE_STATE_DIRNAME
        return os.path.lexists(root)
    except (OSError, RuntimeError, ValueError):
        return True


def _auto_skill_authority_retire_hint() -> str:
    """The operator recovery that removes an unusable authority from this home."""
    return (
        "stop every Kiro Crew gateway and agent using this data home, then run "
        f"`{_AUTHORITY_RETIRE_COMMAND}` from the operator's own terminal; the command "
        "refuses claims in flight and moves the authority, its record and the public "
        "quarantine history aside for inspection"
    )


def _move_authority_aside(
    loader: SkillsLoader,
    provenance_parent: _PinnedSkillParent,
    *,
    root_identity: _TaggedFileIdentity,
    record_identity: _TaggedFileIdentity | None,
) -> tuple[Path, Path | None, tuple[Path, ...]]:
    """Rename the authority, its record and the public quarantines aside under one token.

    A public ``.quarantine`` or ``.live-quarantine`` name is history only while
    private ``evidence/`` holds the same name, and ``evidence/`` leaves with the
    root. Left in place, every such name would count as an active claim against
    the next fresh root, so a home past ``_ACTIVE_CLAIM_SCAN_LIMIT`` lifetime
    claims could never stage or recover again. Both public quarantines therefore
    move beside their evidence: renamed, never read and never deleted, and the
    third element lists where they went (empty when neither existed).

    The caller must hold no pin on the root itself: a Windows directory handle
    opened without ``FILE_SHARE_DELETE`` forbids renaming that directory, so the
    root is re-checked here by native identity instead. A failed rename or sync
    restores every entry already renamed under its own name.
    """
    token = secrets.token_hex(8)
    with contextlib.ExitStack() as stack:
        auto_parent: _PinnedSkillParent | None = None
        if os.path.lexists(loader._dir):
            skills_parent = stack.enter_context(
                loader._pin_skill_parent(loader._dir.resolve(strict=True))
            )
            try:
                auto_parent = stack.enter_context(
                    loader._pin_skill_child_parent(
                        skills_parent,
                        AUTO_SKILL_NAMESPACE,
                        create=False,
                    )
                )
            except FileNotFoundError:
                auto_parent = None
        moves: list[tuple[_PinnedSkillParent, str, _TaggedFileIdentity | None]] = [
            (provenance_parent, AUTO_SKILL_PRIVATE_STATE_DIRNAME, root_identity)
        ]
        if record_identity is not None:
            moves.append((provenance_parent, _AUTHORITY_PROVENANCE_NAME, record_identity))
        if auto_parent is not None:
            moves.extend(
                (auto_parent, name, None)
                for name in (AUTO_QUARANTINE_DIRNAME, AUTO_LIVE_QUARANTINE_DIRNAME)
                if loader._pinned_child_exists(auto_parent, name)
            )
        moved: list[tuple[_PinnedSkillParent, str, _TaggedFileIdentity | None]] = []
        try:
            for parent, name, identity in moves:
                loader._rename_skill_child_no_replace(
                    parent,
                    name,
                    parent,
                    f"{name}.stale-{token}",
                    expected_identity=identity,
                )
                moved.append((parent, name, identity))
            loader._sync_pinned_parent(provenance_parent)
            if auto_parent is not None and any(parent is auto_parent for parent, *_ in moved):
                loader._sync_pinned_parent(auto_parent)
        except (OSError, RuntimeError, ValueError):
            unrestored: list[str] = []
            rollback_error: BaseException | None = None
            for parent, name, identity in reversed(moved):
                try:
                    loader._rename_skill_child_no_replace(
                        parent,
                        f"{name}.stale-{token}",
                        parent,
                        name,
                        expected_identity=identity,
                    )
                    loader._sync_pinned_parent(parent)
                except (OSError, RuntimeError, ValueError) as exc:
                    unrestored.append(str(parent.path / f"{name}.stale-{token}"))
                    rollback_error = rollback_error or exc
            if rollback_error is not None:
                raise OSError(
                    errno.EIO,
                    "authority retirement failed and could not be undone; still "
                    f"set aside: {', '.join(unrestored)}: {rollback_error}",
                ) from rollback_error
            raise
    history = tuple(
        parent.path / f"{name}.stale-{token}"
        for parent, name, _identity in moved
        if parent is not provenance_parent
    )
    return (
        provenance_parent.path / f"{AUTO_SKILL_PRIVATE_STATE_DIRNAME}.stale-{token}",
        (
            provenance_parent.path / f"{_AUTHORITY_PROVENANCE_NAME}.stale-{token}"
            if record_identity is not None
            else None
        ),
        history,
    )


def _active_authority_claim_names(
    loader: SkillsLoader,
    provenance_parent: _PinnedSkillParent,
) -> list[str]:
    """Names in an untrusted authority root's ``claims/``, read with one bounded scan.

    ``claims/`` holds only claims in flight; every retention path moves a claim
    to ``evidence/`` last. History (``evidence/``, the public quarantines and
    retained claim locks) grows by one name per claim and is never read here, so
    a long-lived home cannot outgrow this check. A root that is not a real
    directory holds no journal any recovery could open, so it has no claims.
    Raises ``_StaleClaimScanOverflow`` past ``_STALE_CLAIM_SCAN_LIMIT`` names and
    ``OSError`` when the root or ``claims/`` cannot be inspected.
    """
    root_path = provenance_parent.path / AUTO_SKILL_PRIVATE_STATE_DIRNAME
    root_info = loader._stat_pinned_child(provenance_parent, AUTO_SKILL_PRIVATE_STATE_DIRNAME)
    if not stat.S_ISDIR(root_info.st_mode) or is_link_or_junction(root_path):
        return []
    with loader._pin_skill_child_parent(
        provenance_parent,
        AUTO_SKILL_PRIVATE_STATE_DIRNAME,
        create=False,
    ) as root:
        try:
            with loader._pin_skill_child_parent(
                root,
                AUTO_CLAIMS_DIRNAME,
                create=False,
            ) as claims:
                target: int | Path = claims.fd if _DIR_FD_SUPPORTED else claims.path
                names = _bounded_stale_claim_names(target, label="claims")
        except FileNotFoundError:
            return []
    return sorted(name for name in names if not name.startswith(_RENAME_PROBE_PREFIX))


def _retire_untrusted_authority(
    loader: SkillsLoader,
    canonical_home: Path,
) -> tuple[Path, Path | None, tuple[Path, ...]]:
    """Move an authority root aside on a host where no process can promote.

    Reached only while ``_auto_skill_sandbox_excludes_every_promoter`` holds:
    every process on this data home refuses certification, so no certified
    promoter can hold the root, and its provenance is never consulted. A stale
    record after a ``cp -a``, rsync or backup restore, and a valid root whose
    retired history outgrew the stale-state scan, are both moved aside intact,
    as ``_set_aside_stale_authority`` does at a startup reseed. Idleness is
    decided from active ``claims/`` only. A claim journal found there refuses,
    because moving its root would leave that claim unrecoverable.
    """
    authority = f"{_AUTHORITY_PROVENANCE_PARENT}/{AUTO_SKILL_PRIVATE_STATE_DIRNAME}"
    record = f"{_AUTHORITY_PROVENANCE_PARENT}/{_AUTHORITY_PROVENANCE_NAME}"
    pending = f"{SKILLS_DIR_NAME}/{AUTO_SKILL_NAMESPACE}/{AUTO_PENDING_DIRNAME}"
    quarantine = f"{SKILLS_DIR_NAME}/{AUTO_SKILL_NAMESPACE}/{AUTO_QUARANTINE_DIRNAME}"
    live_quarantine = f"{SKILLS_DIR_NAME}/{AUTO_SKILL_NAMESPACE}/{AUTO_LIVE_QUARANTINE_DIRNAME}"
    by_hand = (
        "To recover by hand: keep every Kiro Crew gateway and agent using this data "
        f"home stopped; move {authority} and {record} out of the data home, keeping "
        f"both for inspection; to requeue a candidate for review, move its "
        f"{quarantine}/<slug>--<token> directory back to {pending}/<slug>; then move "
        f"{quarantine} and {live_quarantine} out with them, because their names are "
        "history only beside that authority's evidence. By-name live mutation and "
        "pending dismissal resume once the root is gone"
    )
    with contextlib.ExitStack() as stack:
        home_parent = stack.enter_context(loader._pin_skill_parent(canonical_home))
        provenance_parent = stack.enter_context(
            loader._pin_skill_child_parent(
                home_parent,
                _AUTHORITY_PROVENANCE_PARENT,
                create=False,
            )
        )
        try:
            claims = _active_authority_claim_names(loader, provenance_parent)
        except FileNotFoundError:
            raise OSError(
                errno.ENOENT,
                f"no auto-skill authority root exists at {authority}; by-name live "
                "mutation and pending dismissal already apply on this data home",
            ) from None
        except (_StaleClaimScanOverflow, OSError) as exc:
            raise OSError(
                errno.EBUSY,
                f"auto-skill authority claims could not be inspected ({exc}); "
                f"retirement refused. {by_hand}",
            ) from None
        if claims:
            raise OSError(
                errno.EBUSY,
                f"auto-skill authority has {len(claims)} claim(s) in flight "
                f"({', '.join(claims[:3])}); retirement refused. {by_hand}",
            )
        root_identity = loader._pinned_child_identity(
            provenance_parent,
            AUTO_SKILL_PRIVATE_STATE_DIRNAME,
        )
        if root_identity is None:
            raise OSError(errno.EBUSY, "authority root changed before operator retirement")
        try:
            loader._stat_pinned_child(provenance_parent, _AUTHORITY_PROVENANCE_NAME)
        except FileNotFoundError:
            record_identity = None
        else:
            record_identity = loader._pinned_child_identity(
                provenance_parent,
                _AUTHORITY_PROVENANCE_NAME,
            )
            if record_identity is None:
                raise OSError(
                    errno.EBUSY,
                    "authority provenance record changed before operator retirement",
                )
        retired = _move_authority_aside(
            loader,
            provenance_parent,
            root_identity=root_identity,
            record_identity=record_identity,
        )
    logger.warning(
        "Auto-skill authority retired without verification on a host where no process "
        "can promote: the root was moved aside to %s, with its public quarantine "
        "history (%s)",
        retired[0],
        ", ".join(str(path) for path in retired[2]) or "none present",
    )
    return retired


def _retire_verified_authority(
    loader: SkillsLoader,
    canonical_home: Path,
    configured: Path,
) -> tuple[Path, Path | None, tuple[Path, ...]]:
    """Verify provenance and idleness, then move the authority aside.

    Used wherever a certified promoter could exist, so an unverifiable root is
    never moved: it refuses with the stopped-installation handoff unchanged.
    """
    binding = loader._ensure_private_authority(
        canonical_home,
        configured_home=configured,
        create=False,
    )
    verify_auto_skill_private_authority(binding)

    with contextlib.ExitStack() as stack:
        home_parent = stack.enter_context(loader._pin_skill_parent(canonical_home))
        provenance_parent = stack.enter_context(
            loader._pin_skill_child_parent(
                home_parent,
                _AUTHORITY_PROVENANCE_PARENT,
                create=False,
            )
        )
        # The root pin lives only for the verification: Windows refuses to rename
        # a directory whose handle lacks ``FILE_SHARE_DELETE``, so the renames
        # re-check the root by its certified native identity instead.
        with loader._pin_skill_child_parent(
            provenance_parent,
            AUTO_SKILL_PRIVATE_STATE_DIRNAME,
            create=False,
        ) as private:
            record_identity = loader._pinned_child_identity(
                provenance_parent,
                _AUTHORITY_PROVENANCE_NAME,
            )
            if (
                home_parent.native_identity != binding.home_identity
                or private.native_identity != binding.root_identity
                or record_identity is None
                or not loader._authority_record_valid(
                    provenance_parent,
                    configured,
                    canonical_home,
                    home_parent.native_identity,
                    private.native_identity,
                )
            ):
                raise loader._authority_handoff(
                    "authority or provenance changed before operator retirement"
                )
        claims = loader._stale_authority_claims(provenance_parent)
        if claims is None:
            raise OSError(
                errno.EBUSY,
                "auto-skill authority claims could not be inspected; retirement refused",
            )
        if claims:
            raise OSError(
                errno.EBUSY,
                f"auto-skill authority has {len(claims)} claim(s) in flight "
                f"({', '.join(claims[:3])}); retirement refused",
            )
        return _move_authority_aside(
            loader,
            provenance_parent,
            root_identity=binding.root_identity,
            record_identity=record_identity,
        )


def retire_auto_skill_private_authority() -> tuple[Path, Path | None, tuple[Path, ...]]:
    """Move an idle auto-skill authority aside for operator recovery.

    The CLI caller holds the data home's gateway lock, so no gateway can start or
    remain active during this operation. Where a certified promoter could exist,
    provenance, selected-home identity, root identity, and the bounded no-claim
    verdict are rechecked while their parents stay pinned. Where
    ``_auto_skill_sandbox_excludes_every_promoter`` holds, no process on this data
    home can be certified, so an unverifiable root is moved aside without being
    trusted and idleness is read from active ``claims/`` only. The root, its
    record and the public ``.quarantine`` and ``.live-quarantine`` directories
    all take one ``.stale-<token>`` suffix and remain available for inspection,
    so a later fresh root starts with no claim history. Returns the root, the
    record (``None`` when there was none) and the set-aside quarantines. No fresh
    authority is provisioned.
    """
    global _STARTUP_AUTHORITY_BINDING, _STARTUP_AUTHORITY_REFUSAL

    selected, configured = _gateway_authority_homes()
    configured = Path(os.path.abspath(os.path.expanduser(str(configured))))
    canonical_home = configured.resolve(strict=True)
    if selected.resolve(strict=True) != canonical_home:
        raise OSError(
            errno.EBUSY,
            "configured data-home spelling retargeted away from the selected data home",
        )
    loader = _bare_loader(canonical_home / SKILLS_DIR_NAME)
    if _auto_skill_sandbox_excludes_every_promoter():
        retired = _retire_untrusted_authority(loader, canonical_home)
    else:
        retired = _retire_verified_authority(loader, canonical_home, configured)

    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        _STARTUP_AUTHORITY_BINDING = None
        _STARTUP_AUTHORITY_REFUSAL = None
    return retired


def initialize_auto_skill_private_authority(
    skills_root: Path | None = None,
    *,
    data_home: Path | None = None,
    configured_home: Path | None = None,
) -> _CertifiedAuthorityBinding:
    """Exclusively provision and process-certify auto-skill authority.

    Production calls this only through the common gateway startup boundary;
    explicit calls remain the test-fixture seam that models that startup.
    """
    global _STARTUP_AUTHORITY_BINDING
    selected_home = Path(data_home) if data_home is not None else config_dir()
    configured = Path(configured_home) if configured_home is not None else selected_home
    configured = Path(os.path.abspath(os.path.expanduser(str(configured))))
    canonical_home = configured.resolve(strict=True)
    selected_canonical = selected_home.resolve(strict=True)
    if canonical_home != selected_canonical:
        raise OSError(
            errno.EBUSY,
            "configured data-home spelling retargeted away from the selected data home",
        )
    root = skills_root or canonical_home / SKILLS_DIR_NAME
    loader = _bare_loader(root)
    binding = loader._ensure_private_authority(
        canonical_home,
        configured_home=configured,
        create=True,
    )
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        existing = _STARTUP_AUTHORITY_BINDING
        if existing is not None and existing != binding:
            raise loader._authority_handoff(
                "a different auto-skill authority was already certified in this process"
            )
        _STARTUP_AUTHORITY_BINDING = binding
    verify_auto_skill_private_authority(binding)
    return binding


def _agent_sandbox_is_delegated() -> bool:
    """Whether agents here run in a sandbox whose hidden paths Kiro Crew cannot set.

    The authority is protected by the ``tag-grants`` mask Kiro Crew's own Linux
    namespace and macOS Seatbelt profile apply. A delegated sandbox (native
    Windows, or macOS with the Kiro CLI internal sandbox) applies no such mask, so
    an agent's shell could reach the authority and approve its own candidate.
    """
    from kiro_crew import sandbox  # deferred: the sandbox module is heavy and gateway-only

    return sandbox.spawn_delegates_masking()


def _auto_skill_authority_sandbox_refusal() -> str | None:
    """Why the authority cannot be protected from an agent spawned now."""
    if _agent_sandbox_is_delegated():
        return _DELEGATED_SANDBOX_REFUSAL
    from kiro_crew import sandbox  # deferred: the sandbox module is heavy and gateway-only

    try:
        mode = sandbox.configured_sandbox_mode()
        masked = sandbox.credential_mask_applies(mode)
    except (OSError, RuntimeError, ValueError):
        return _SANDBOX_MASK_UNAVAILABLE_REFUSAL
    if masked:
        return None
    return _SANDBOX_OFF_REFUSAL if mode == "off" else _SANDBOX_MASK_UNAVAILABLE_REFUSAL


def initialize_gateway_auto_skill_private_authority() -> _CertifiedAuthorityBinding:
    """Run the process-wide auto-skill authority initialization boundary.

    Fails closed, and records why, where the protecting credential mask does not
    apply, where agent sandboxing is delegated, or where provisioning or
    certification refuses. The caller keeps the gateway running or stops a
    standalone mutating command before it advances state.
    """
    global _STARTUP_AUTHORITY_BINDING, _STARTUP_AUTHORITY_REFUSAL
    sandbox_refusal = _auto_skill_authority_sandbox_refusal()
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        if sandbox_refusal is not None:
            # A live mode change can expose the authority after startup. Revoke
            # this process's certificate before any later promotion can use it.
            _STARTUP_AUTHORITY_BINDING = None
            if (
                _auto_skill_authority_root_exists()
                and _auto_skill_sandbox_excludes_every_promoter()
            ):
                sandbox_refusal = (
                    f"{sandbox_refusal}; an auto-skill authority still exists on this "
                    f"data home. {_auto_skill_authority_retire_hint()}"
                )
            _STARTUP_AUTHORITY_REFUSAL = sandbox_refusal
            raise OSError(errno.ENOTSUP, sandbox_refusal)
        if _STARTUP_AUTHORITY_BINDING is not None:
            verify_auto_skill_private_authority(_STARTUP_AUTHORITY_BINDING)
            return _STARTUP_AUTHORITY_BINDING
        selected, configured = _gateway_authority_homes()
        try:
            binding = initialize_auto_skill_private_authority(
                data_home=selected,
                configured_home=configured,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _STARTUP_AUTHORITY_REFUSAL = str(exc) or type(exc).__name__
            raise
        probe = _bare_loader(binding.canonical_home / SKILLS_DIR_NAME)
        try:
            probe._probe_rename_under_authority(binding)
        except (OSError, RuntimeError, ValueError):
            logger.warning("Auto-skill no-replace rename probe failed", exc_info=True)
            # Fail closed once, here: with the primitive missing a candidate
            # could be staged but never approved or dismissed through a claim.
            _STARTUP_AUTHORITY_BINDING = None
            _STARTUP_AUTHORITY_REFUSAL = _NO_REPLACE_RENAME_REFUSAL
            raise OSError(errno.ENOTSUP, _NO_REPLACE_RENAME_REFUSAL) from None
        _STARTUP_AUTHORITY_REFUSAL = None
        return binding


def auto_skill_promotion_disabled_reason() -> str | None:
    """Why this process cannot stage or promote auto-skills, or ``None`` when it can.

    Staging, approval, unattended update application and claim-based dismissal
    all need the certified authority; this names the reason they will refuse, so
    a caller can report it instead of a generic failure.
    """
    try:
        require_auto_skill_private_authority()
    except (OSError, RuntimeError, ValueError) as exc:
        return _STARTUP_AUTHORITY_REFUSAL or str(exc) or "auto-skill authority is unavailable"
    return None


def _no_replace_refusal_still_binds_data_home() -> bool:
    """Re-probe the data-home-wide refusal only from an unmasked boundary.

    A probe inside the masked ``tag-grants`` view does not inspect the real
    authority root and can never grant lock-free mutation.
    """
    if _auto_skill_authority_masked_view_reason() is not None:
        return False
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        if _STARTUP_AUTHORITY_REFUSAL != _NO_REPLACE_RENAME_REFUSAL:
            return False
    try:
        selected, configured = _gateway_authority_homes()
        configured = Path(os.path.abspath(os.path.expanduser(str(configured))))
        canonical_home = configured.resolve(strict=True)
        if selected.resolve(strict=True) != canonical_home:
            return False
        probe = _bare_loader(canonical_home / SKILLS_DIR_NAME)
        binding = probe._ensure_private_authority(
            canonical_home,
            configured_home=configured,
            create=False,
        )
        probe._probe_rename_under_authority(binding)
    except NotImplementedError:
        return True
    except OSError as exc:
        unsupported = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}
        unsupported.add(getattr(errno, "EOPNOTSUPP", errno.ENOTSUP))
        return exc.errno in unsupported
    except (RuntimeError, ValueError):
        return False
    return False


def _auto_skill_sandbox_excludes_every_promoter() -> bool:
    """Whether a host-wide sandbox condition makes an existing root unusable.

    This reading selects the startup diagnostic and retirement hint only. It
    never grants lock-free mutation: a certified process may already hold a
    target lock when the setting changes. Three causes are shared widely enough
    to make retirement actionable: delegated agent sandboxing, an effective
    ``agent.sandbox`` of ``off``, and a genuine backend-less host. A transient
    backend probe failure, foreign outer sandbox, or unreadable predicate keeps
    the generic retry guidance instead.

    Never cached: a live sandbox or backend change must affect the next startup
    diagnostic.
    """
    if _agent_sandbox_is_delegated():
        return True
    if _auto_skill_authority_sandbox_refusal() is None:
        return False
    from kiro_crew import sandbox  # deferred: the sandbox module is heavy and gateway-only

    try:
        if sandbox.effective_sandbox_mode(sandbox.configured_sandbox_mode()) == "off":
            return True
        return sandbox.unavailable_kind() == "no_backend"
    except (OSError, RuntimeError, ValueError):
        return False


def _auto_skill_promotion_ruled_out() -> bool:
    """Whether lock-free by-name mutation is safe for this data home now.

    An absent authority root is re-read on every call and proves no process can
    begin an authority-backed promotion only when this process has an unmasked
    filesystem view. The other grant is the data-home-wide no-replace refusal
    while a fresh unmasked probe still proves the publication primitive
    unavailable. Inside an agent sandbox both verdicts are indeterminate, so
    every mutation requires the authority-backed target lock and fails closed if
    that hidden lock cannot be acquired. A startup refusal or host-wide sandbox
    reading cannot bypass a lock a certified process may already hold.
    """
    if not _auto_skill_authority_root_exists():
        return True
    return _no_replace_refusal_still_binds_data_home()


def require_auto_skill_private_authority() -> _CertifiedAuthorityBinding:
    """Return the startup certificate after revalidating its mask and identity."""
    global _STARTUP_AUTHORITY_BINDING, _STARTUP_AUTHORITY_REFUSAL
    sandbox_refusal = _auto_skill_authority_sandbox_refusal()
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        if sandbox_refusal is not None:
            _STARTUP_AUTHORITY_BINDING = None
            _STARTUP_AUTHORITY_REFUSAL = sandbox_refusal
            raise OSError(errno.ENOTSUP, sandbox_refusal)
        binding = _STARTUP_AUTHORITY_BINDING
    if binding is None:
        raise OSError(
            errno.EBUSY,
            "auto-skill private authority has no gateway-startup certificate",
        )
    verify_auto_skill_private_authority(binding)
    return binding


def verify_auto_skill_private_authority(binding: _CertifiedAuthorityBinding) -> None:
    """Revalidate the startup binding without creating or repairing any path."""
    configured = Path(_normalized_authority_path(binding.configured_home))
    current_canonical = configured.resolve(strict=True)
    selected_canonical = Path(config_dir()).resolve(strict=True)
    if _normalized_authority_path(current_canonical) != _normalized_authority_path(
        binding.canonical_home
    ) or _normalized_authority_path(selected_canonical) != _normalized_authority_path(
        binding.canonical_home
    ):
        raise OSError(
            errno.EBUSY,
            "configured data-home spelling retargeted after startup certification",
        )
    loader = _bare_loader(binding.canonical_home / SKILLS_DIR_NAME)
    with contextlib.ExitStack() as stack:
        home_parent = stack.enter_context(loader._pin_skill_parent(binding.canonical_home))
        provenance_parent = stack.enter_context(
            loader._pin_skill_child_parent(
                home_parent,
                _AUTHORITY_PROVENANCE_PARENT,
                create=False,
            )
        )
        private = stack.enter_context(
            loader._pin_skill_child_parent(
                provenance_parent,
                AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                create=False,
            )
        )
        if (
            home_parent.native_identity != binding.home_identity
            or private.native_identity != binding.root_identity
            or private.path != binding.root_path
            or not loader._authority_record_valid(
                provenance_parent,
                configured,
                binding.canonical_home,
                binding.home_identity,
                binding.root_identity,
            )
            or not loader._pinned_parent_matches(home_parent)
            or not loader._pinned_parent_matches(private)
            or not loader._pinned_parent_matches(provenance_parent)
        ):
            raise loader._authority_handoff(
                "certified authority changed between initialization and use"
            )


def _reset_auto_skill_private_authority_for_tests() -> None:
    """Reset process startup state for an isolated test data home."""
    global _STARTUP_AUTHORITY_BINDING, _STARTUP_AUTHORITY_REFUSAL
    with _AUTHORITY_HOME_IDENTITIES_LOCK:
        _STARTUP_AUTHORITY_BINDING = None
        _STARTUP_AUTHORITY_REFUSAL = None
        _AUTHORITY_HOME_IDENTITIES.clear()


def _authority_record_body(
    configured_home: Path,
    canonical_home: Path,
    home_identity: _TaggedFileIdentity,
    root_identity: _TaggedFileIdentity,
) -> dict[str, object]:
    return {
        "version": _AUTHORITY_PROVENANCE_VERSION,
        "configured_home": _normalized_authority_path(configured_home),
        "canonical_home": _normalized_authority_path(canonical_home),
        "selected_home": _identity_payload(home_identity),
        "authority_root": _identity_payload(root_identity),
    }


def _authority_record_mac(body: dict[str, object]) -> str:
    from kiro_crew.dashboard import token_secret

    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(
        token_secret._get_secret(),
        _AUTHORITY_PROVENANCE_DOMAIN + encoded,
        hashlib.sha256,
    ).hexdigest()


def _authority_record_valid(
    self,
    provenance_parent: _PinnedSkillParent,
    configured_home: Path,
    canonical_home: Path,
    home_identity: _TaggedFileIdentity,
    root_identity: _TaggedFileIdentity,
) -> bool:
    raw = self._read_pinned_regular_file(
        provenance_parent,
        _AUTHORITY_PROVENANCE_NAME,
        max_bytes=_AUTHORITY_PROVENANCE_MAX_BYTES,
    )
    if raw is None:
        return False
    try:
        record = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    mac = record.pop("mac", None)
    if not isinstance(mac, str):
        return False
    expected_body = self._authority_record_body(
        configured_home,
        canonical_home,
        home_identity,
        root_identity,
    )
    if record != expected_body:
        return False
    try:
        expected_mac = self._authority_record_mac(expected_body)
        return hmac.compare_digest(mac, expected_mac)
    except (OSError, RuntimeError, ValueError):
        return False


def _authority_handoff(reason: str) -> OSError:
    return OSError(
        errno.EBUSY,
        "auto-skill private authority requires stopped-installation recovery: "
        f"{reason}; stop every Kiro Crew gateway and agent using this data home, "
        "preserve the existing authority root and provenance record untouched, "
        "and retry only after operator inspection",
    )


def _ensure_private_authority(
    self,
    canonical_home: Path,
    *,
    configured_home: Path | None = None,
    create: bool,
) -> _CertifiedAuthorityBinding:
    """Verify or provision authority beneath the pre-existing hidden parent."""
    configured = configured_home or canonical_home
    configured = Path(os.path.abspath(os.path.expanduser(str(configured))))
    authority_parent_path = canonical_home / _AUTHORITY_PROVENANCE_PARENT
    private_path = authority_parent_path / AUTO_SKILL_PRIVATE_STATE_DIRNAME
    provenance_path = authority_parent_path / _AUTHORITY_PROVENANCE_NAME
    if not is_sensitive_path(str(private_path)) or not is_sensitive_path(str(provenance_path)):
        raise OSError("auto-skill authority or provenance is not agent-denied")
    with contextlib.ExitStack() as stack:
        home_parent = stack.enter_context(self._pin_skill_parent(canonical_home))
        home_key = _normalized_authority_path(configured)
        canonical_key = _normalized_authority_path(canonical_home)
        selected_binding = (canonical_key, home_parent.native_identity)
        with _AUTHORITY_HOME_IDENTITIES_LOCK:
            selected = _AUTHORITY_HOME_IDENTITIES.get(home_key)
            if selected is not None and selected != selected_binding:
                raise self._authority_handoff(
                    "selected data-home path or identity changed during this process"
                )
            _AUTHORITY_HOME_IDENTITIES.setdefault(home_key, selected_binding)
        provenance_parent = stack.enter_context(
            self._pin_skill_child_parent(
                home_parent,
                _AUTHORITY_PROVENANCE_PARENT,
                create=create,
            )
        )
        try:
            root_info = self._stat_pinned_child(
                provenance_parent,
                AUTO_SKILL_PRIVATE_STATE_DIRNAME,
            )
        except FileNotFoundError:
            root_info = None
        try:
            self._stat_pinned_child(
                provenance_parent,
                _AUTHORITY_PROVENANCE_NAME,
            )
            record_exists = True
        except FileNotFoundError:
            record_exists = False

        stale_reason: str | None = None
        if root_info is None:
            if record_exists:
                stale_reason = "provenance exists but authority root is absent"
        elif not stat.S_ISDIR(root_info.st_mode) or is_link_or_junction(private_path):
            raise self._authority_handoff("authority root is linked or not a directory")
        elif not record_exists:
            stale_reason = "authority root has no certified provenance"
        else:
            with self._pin_skill_child_parent(
                provenance_parent,
                AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                create=False,
            ) as existing:
                if self._authority_record_valid(
                    provenance_parent,
                    configured,
                    canonical_home,
                    home_parent.native_identity,
                    existing.native_identity,
                ):
                    self._restrict_private_parent(existing)
                    if (
                        not self._pinned_parent_matches(home_parent)
                        or not self._pinned_parent_matches(existing)
                        or not self._pinned_parent_matches(provenance_parent)
                    ):
                        raise self._authority_handoff(
                            "certified authority changed during final verification"
                        )
                    return _CertifiedAuthorityBinding(
                        configured_home=configured,
                        canonical_home=canonical_home,
                        home_identity=home_parent.native_identity,
                        root_path=existing.path,
                        root_identity=existing.native_identity,
                        provenance_path=provenance_path,
                    )
            stale_reason = (
                "authority provenance, selected-home identity, or root identity mismatches"
            )
        if stale_reason is not None:
            if not create:
                raise self._authority_handoff(stale_reason)
            # A restored backup, a host move, a regenerated token key or a
            # renumbering remount all land here with nothing wrong in the
            # data. Reseed when no claim is in flight; otherwise refuse with
            # the exact recovery (``_set_aside_stale_authority``).
            self._set_aside_stale_authority(
                provenance_parent,
                stale_reason,
                root_present=root_info is not None,
                record_present=record_exists,
            )

        if not create:
            raise self._authority_handoff(
                "authority root is absent; the startup initializer must provision it"
            )
        created_out: list[bool] = []
        private = stack.enter_context(
            self._pin_skill_child_parent(
                provenance_parent,
                AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                create=True,
                created_out=created_out,
            )
        )
        if created_out != [True]:
            raise self._authority_handoff("authority root appeared before exclusive create")
        self._restrict_private_parent(private)
        self._sync_pinned_parent(provenance_parent)
        home_identity = home_parent.native_identity
        root_identity = private.native_identity
        body = self._authority_record_body(
            configured,
            canonical_home,
            home_identity,
            root_identity,
        )
        body["mac"] = self._authority_record_mac(body)
        self._write_pinned_new_file(
            provenance_parent,
            _AUTHORITY_PROVENANCE_NAME,
            (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            mode=0o600,
        )
        self._sync_pinned_parent(provenance_parent)
        if not self._authority_record_valid(
            provenance_parent,
            configured,
            canonical_home,
            home_identity,
            root_identity,
        ):
            raise self._authority_handoff("fresh provenance did not verify")
        if (
            not self._pinned_parent_matches(home_parent)
            or not self._pinned_parent_matches(private)
            or not self._pinned_parent_matches(provenance_parent)
        ):
            raise self._authority_handoff("fresh authority changed after provenance durability")
        return _CertifiedAuthorityBinding(
            configured_home=configured,
            canonical_home=canonical_home,
            home_identity=home_identity,
            root_path=private.path,
            root_identity=root_identity,
            provenance_path=provenance_path,
        )


def _stale_authority_claims(self, provenance_parent: _PinnedSkillParent) -> list[str] | None:
    """Names of in-flight stale claims, or ``None`` when inspection is unknown.

    Every directory scan is bounded by ``_STALE_CLAIM_SCAN_LIMIT``. Public
    candidate and old-live quarantines, plus retained claim-lock names, are
    terminal only when the stale authority's private evidence directory has
    the same claim name. An unmatched name is an interrupted claim, including
    the crash window after pending-to-quarantine rename and before private
    claim materialization. Nothing in the stale root is trusted as authority.
    """

    def child_names(
        parent: _PinnedSkillParent,
        name: str,
        *,
        label: str,
    ) -> set[str]:
        try:
            with self._pin_skill_child_parent(parent, name, create=False) as child:
                target: int | Path = child.fd if _DIR_FD_SUPPORTED else child.path
                return _bounded_stale_claim_names(target, label=label)
        except FileNotFoundError:
            return set()

    retired_roots = {AUTO_CLAIMS_DIRNAME, AUTO_EVIDENCE_DIRNAME, AUTO_LOCKS_DIRNAME}
    try:
        live: set[str] = set()
        claims: set[str] = set()
        evidence: set[str] = set()
        claim_locks: set[str] = set()
        try:
            with self._pin_skill_child_parent(
                provenance_parent,
                AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                create=False,
            ) as root:
                listing: int | Path = root.fd if _DIR_FD_SUPPORTED else root.path
                top = _bounded_stale_claim_names(listing, label="authority root")
                live = {
                    name
                    for name in top - retired_roots
                    if not name.startswith(_RENAME_PROBE_PREFIX)
                }
                claims = {
                    name
                    for name in child_names(root, AUTO_CLAIMS_DIRNAME, label="claims")
                    if not name.startswith(_RENAME_PROBE_PREFIX)
                }
                evidence = child_names(root, AUTO_EVIDENCE_DIRNAME, label="evidence")
                try:
                    with self._pin_skill_child_parent(
                        root,
                        AUTO_LOCKS_DIRNAME,
                        create=False,
                    ) as locks:
                        claim_locks = child_names(
                            locks,
                            AUTO_CLAIMS_DIRNAME,
                            label="claim locks",
                        )
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            # A missing private root carries no evidence that any public
            # quarantine entry retired, so every such entry remains in flight.
            pass

        claim_surfaces: dict[str, set[str]] = {name: {name} for name in claims}
        public_quarantine: set[str] = set()
        public_live_quarantine: set[str] = set()
        if os.path.lexists(self._dir):
            with self._pin_skill_parent(self._dir.resolve(strict=True)) as skills_parent:
                try:
                    with self._pin_skill_child_parent(
                        skills_parent,
                        AUTO_SKILL_NAMESPACE,
                        create=False,
                    ) as auto_parent:
                        public_quarantine = child_names(
                            auto_parent,
                            AUTO_QUARANTINE_DIRNAME,
                            label="public candidate quarantine",
                        )
                        public_live_quarantine = child_names(
                            auto_parent,
                            AUTO_LIVE_QUARANTINE_DIRNAME,
                            label="public live quarantine",
                        )
                except FileNotFoundError:
                    pass

        for name in public_quarantine - evidence:
            claim_surfaces.setdefault(name, set()).add(f"{AUTO_QUARANTINE_DIRNAME}/{name}")
        for name in public_live_quarantine - evidence:
            claim_surfaces.setdefault(name, set()).add(f"{AUTO_LIVE_QUARANTINE_DIRNAME}/{name}")
        for lock_name in claim_locks:
            claim_name = lock_name.removesuffix(".lock")
            if not lock_name.endswith(".lock") or claim_name not in evidence:
                claim_surfaces.setdefault(claim_name, set()).add(
                    f"{AUTO_LOCKS_DIRNAME}/{AUTO_CLAIMS_DIRNAME}/{lock_name}"
                )
        live.update("; ".join(sorted(surfaces)) for surfaces in claim_surfaces.values())
        return sorted(live)
    except _StaleClaimScanOverflow:
        raise
    except (OSError, RuntimeError, ValueError):
        return None


def _set_aside_stale_authority(
    self,
    provenance_parent: _PinnedSkillParent,
    reason: str,
    *,
    root_present: bool,
    record_present: bool,
) -> None:
    """Move an idle stale authority and its record aside, or refuse with recovery.

    Provenance binds the selected home's and the root's native identities
    under ``token_signing.key``, so a restored backup, a ``cp -a`` or rsync
    host move, a regenerated key or a remount that renumbers devices leaves it
    unverifiable for good. A stale root is never trusted again. With no claim
    in flight it is renamed aside intact under a ``.stale-<token>`` name in the
    same masked parent, never read for authority and never deleted, and the
    caller provisions a fresh root: the quarantine-and-reseed the ``chat_tag``
    grant store beside it already applies. An in-flight claim's journal lives
    in that untrusted root, so startup refuses and names the exact recovery.
    """
    scan_problem: str | None = None
    try:
        live = self._stale_authority_claims(provenance_parent)
    except _StaleClaimScanOverflow as exc:
        live = None
        scan_problem = str(exc)
    if live is None or live:
        in_flight = (
            (
                f"its stale claim state is indeterminate ({scan_problem})"
                if scan_problem is not None
                else "its claims could not be inspected"
            )
            if live is None
            else f"{len(live)} claim(s) are still in flight ({', '.join(live[:3])})"
        )
        authority = f"{_AUTHORITY_PROVENANCE_PARENT}/{AUTO_SKILL_PRIVATE_STATE_DIRNAME}"
        record = f"{_AUTHORITY_PROVENANCE_PARENT}/{_AUTHORITY_PROVENANCE_NAME}"
        pending = f"{SKILLS_DIR_NAME}/{AUTO_SKILL_NAMESPACE}/{AUTO_PENDING_DIRNAME}"
        quarantine = f"{SKILLS_DIR_NAME}/{AUTO_SKILL_NAMESPACE}/{AUTO_QUARANTINE_DIRNAME}"
        raise OSError(
            errno.EBUSY,
            f"auto-skill authority provenance no longer matches this data home ({reason}) "
            f"and {in_flight}, so startup will not reseed it. To recover: stop every "
            f"Kiro Crew gateway and agent using this data home; move {authority} and "
            f"{record} out of the data home, keeping both for inspection; to requeue "
            f"an in-flight candidate for review, move its {quarantine}/<slug>--<token> "
            f"directory back to {pending}/<slug>; then start the gateway, which "
            "provisions a fresh authority. Auto-skill staging and promotion stay off "
            "until then",
        )
    token = secrets.token_hex(8)
    for present, name in (
        (root_present, AUTO_SKILL_PRIVATE_STATE_DIRNAME),
        (record_present, _AUTHORITY_PROVENANCE_NAME),
    ):
        if present:
            self._rename_skill_child_no_replace(
                provenance_parent,
                name,
                provenance_parent,
                f"{name}.stale-{token}",
            )
    self._sync_pinned_parent(provenance_parent)
    logger.warning(
        "Auto-skill authority provenance did not verify (%s) and no claim was in "
        "flight: the previous authority was moved aside as *.stale-%s under %s and a "
        "fresh authority is provisioned",
        reason,
        token,
        provenance_parent.path,
    )


def _preflight_private_state(
    self,
    *,
    require_sensitive: bool,
) -> tuple[Path, _CertifiedAuthorityBinding]:
    """Authenticate public and direct private ancestry before mutation."""
    binding = require_auto_skill_private_authority()
    canonical_home = binding.canonical_home
    skills_root_exists = os.path.lexists(self._dir)
    if is_link_or_junction(self._dir):
        raise OSError("skills root is a link or reparse point")
    canonical_skills = self._dir.resolve(strict=skills_root_exists)
    home_info = os.lstat(canonical_home)
    if not stat.S_ISDIR(home_info.st_mode) or is_link_or_junction(canonical_home):
        raise OSError("crew data home is not one real directory")

    private_root = self._private_root()
    projected_private = (
        private_root.resolve(strict=True)
        if os.path.lexists(private_root)
        else (canonical_home / _AUTHORITY_PROVENANCE_PARENT / AUTO_SKILL_PRIVATE_STATE_DIRNAME)
    )
    if require_sensitive and (
        not is_sensitive_path(str(private_root)) or not is_sensitive_path(str(projected_private))
    ):
        raise OSError("private root is not agent-denied")

    with contextlib.ExitStack() as stack:
        if skills_root_exists:
            skills_parent = stack.enter_context(self._pin_skill_parent(canonical_skills))
            try:
                auto_parent = stack.enter_context(
                    self._pin_skill_child_parent(
                        skills_parent,
                        AUTO_SKILL_NAMESPACE,
                        create=False,
                    )
                )
            except FileNotFoundError:
                auto_parent = None
            if auto_parent is not None:
                for sibling_name in (
                    AUTO_PENDING_DIRNAME,
                    AUTO_QUARANTINE_DIRNAME,
                    AUTO_LIVE_QUARANTINE_DIRNAME,
                ):
                    try:
                        stack.enter_context(
                            self._pin_skill_child_parent(
                                auto_parent,
                                sibling_name,
                                create=False,
                            )
                        )
                    except FileNotFoundError:
                        pass

        verify_auto_skill_private_authority(binding)
        home_parent = stack.enter_context(self._pin_skill_parent(canonical_home))
        if home_parent.native_identity != binding.home_identity:
            raise self._authority_handoff(
                "selected data-home changed between provenance verification and use"
            )
        provenance_parent = stack.enter_context(
            self._pin_skill_child_parent(
                home_parent,
                _AUTHORITY_PROVENANCE_PARENT,
                create=False,
            )
        )
        try:
            private = stack.enter_context(
                self._pin_skill_child_parent(
                    provenance_parent,
                    AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                    create=False,
                )
            )
        except FileNotFoundError as exc:
            raise self._authority_handoff(
                "authority root disappeared after provenance verification"
            ) from exc
        if private.native_identity != binding.root_identity or private.path != binding.root_path:
            raise self._authority_handoff(
                "authority root changed between provenance verification and use"
            )

        existing_children: dict[str, _PinnedSkillParent] = {}
        for child_name in (
            AUTO_CLAIMS_DIRNAME,
            AUTO_EVIDENCE_DIRNAME,
            AUTO_LOCKS_DIRNAME,
        ):
            try:
                existing_children[child_name] = stack.enter_context(
                    self._pin_skill_child_parent(
                        private,
                        child_name,
                        create=False,
                    )
                )
            except FileNotFoundError:
                pass
        locks = existing_children.get(AUTO_LOCKS_DIRNAME)
        if locks is not None:
            try:
                stack.enter_context(
                    self._pin_skill_child_parent(
                        locks,
                        AUTO_CLAIMS_DIRNAME,
                        create=False,
                    )
                )
            except FileNotFoundError:
                pass
    return canonical_skills, binding


@contextlib.contextmanager
def _pin_private_state(
    self,
    *,
    create: bool,
    require_sensitive: bool = False,
) -> Iterator[_PinnedPrivateState]:
    """Pin public and private authority parents from canonical roots."""
    canonical_skills, binding = self._preflight_private_state(
        require_sensitive=require_sensitive,
    )
    canonical_home = binding.canonical_home
    if create and not os.path.lexists(self._dir):
        self._dir.mkdir(parents=True, exist_ok=True)
        if is_link_or_junction(self._dir) or self._dir.resolve(strict=True) != canonical_skills:
            raise OSError("skills root changed during creation")
    private_root = self._private_root()
    with contextlib.ExitStack() as stack:
        home_parent = stack.enter_context(self._pin_skill_parent(canonical_home))
        if home_parent.native_identity != binding.home_identity:
            raise self._authority_handoff("selected data-home changed before authority use")
        provenance_parent = stack.enter_context(
            self._pin_skill_child_parent(
                home_parent,
                _AUTHORITY_PROVENANCE_PARENT,
                create=False,
            )
        )
        private = stack.enter_context(
            self._pin_skill_child_parent(
                provenance_parent,
                AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                create=False,
            )
        )
        if private.native_identity != binding.root_identity or private.path != binding.root_path:
            raise self._authority_handoff("authority root changed before use")
        skills_parent = stack.enter_context(self._pin_skill_parent(canonical_skills))
        auto_parent = stack.enter_context(
            self._pin_skill_child_parent(
                skills_parent,
                AUTO_SKILL_NAMESPACE,
                create=create,
            )
        )
        public_parents: dict[str, _PinnedSkillParent] = {}
        for public_name in (
            AUTO_PENDING_DIRNAME,
            AUTO_QUARANTINE_DIRNAME,
            AUTO_LIVE_QUARANTINE_DIRNAME,
        ):
            public_parent = stack.enter_context(
                self._pin_skill_child_parent(
                    auto_parent,
                    public_name,
                    create=create,
                )
            )
            if not self._pinned_parent_matches(public_parent):
                raise OSError(f"{public_name} root is not a real directory")
            public_parents[public_name] = public_parent
        pending = public_parents[AUTO_PENDING_DIRNAME]
        quarantine = public_parents[AUTO_QUARANTINE_DIRNAME]
        live_quarantine = public_parents[AUTO_LIVE_QUARANTINE_DIRNAME]

        if auto_parent.native_identity.volume != private.native_identity.volume:
            raise OSError(errno.EXDEV, "public and private skill authority are cross-device")
        claims = stack.enter_context(
            self._pin_skill_child_parent(
                private,
                AUTO_CLAIMS_DIRNAME,
                create=create,
            )
        )
        evidence = stack.enter_context(
            self._pin_skill_child_parent(
                private,
                AUTO_EVIDENCE_DIRNAME,
                create=create,
            )
        )
        locks = stack.enter_context(
            self._pin_skill_child_parent(
                private,
                AUTO_LOCKS_DIRNAME,
                create=create,
            )
        )
        claim_locks = stack.enter_context(
            self._pin_skill_child_parent(
                locks,
                AUTO_CLAIMS_DIRNAME,
                create=create,
            )
        )
        resolved_private = private_root.resolve(strict=True)
        if require_sensitive and (
            not is_sensitive_path(str(private_root)) or not is_sensitive_path(str(resolved_private))
        ):
            raise OSError("private root is not agent-denied")
        resolved_private.relative_to(canonical_home)
        yield _PinnedPrivateState(
            data_home=home_parent,
            auto=auto_parent,
            pending=pending,
            quarantine=quarantine,
            live_quarantine=live_quarantine,
            private=private,
            claims=claims,
            evidence=evidence,
            locks=locks,
            claim_locks=claim_locks,
        )


def _private_state_roots_safe(self, *, create: bool, require_sensitive: bool = False) -> bool:
    """Authenticate the complete private hierarchy under retained parent pins."""
    try:
        self._preflight_private_state(
            require_sensitive=require_sensitive,
        )
        if not create and not os.path.lexists(self._private_root()):
            return True
        with self._pin_private_state(
            create=create,
            require_sensitive=require_sensitive,
        ):
            return True
    except (OSError, RuntimeError, ValueError) as exc:
        disabled = auto_skill_promotion_disabled_reason()
        if disabled is not None:
            # Authority itself is unavailable (no certificate, a startup
            # refusal, a revoked mask): the tree layout is not the problem,
            # so the link remedy below would send the operator the wrong way.
            logger.warning(
                "Auto-skill private state under %s is unavailable: %s",
                self._private_root(),
                disabled,
            )
            return False
        logger.error(
            "Refusing unsafe auto-skill private state under %s: %s. Remove links or "
            "junctions inside the skills tree; to relocate it, link the Kiro Crew "
            "data-home root instead, then retry.",
            self._private_root(),
            exc,
        )
        return False


def _probe_rename_under_authority(self, binding: _CertifiedAuthorityBinding) -> None:
    """Raise unless an atomic no-replace rename works in the certified root.

    Startup's copy of the claim-time probe. Claims, restores and publication
    all need the primitive, so a data home on a filesystem without it (NFS,
    older ZFS, many FUSE mounts) is refused once at certification instead of
    accepting candidates that could then be neither approved nor dismissed.
    """
    token = secrets.token_hex(16)
    source_name = f"{_RENAME_PROBE_PREFIX}{token}-source"
    destination_name = f"{_RENAME_PROBE_PREFIX}{token}-destination"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with contextlib.ExitStack() as stack:
        home = stack.enter_context(self._pin_skill_parent(binding.canonical_home))
        parent = stack.enter_context(
            self._pin_skill_child_parent(home, _AUTHORITY_PROVENANCE_PARENT, create=False)
        )
        root = stack.enter_context(
            self._pin_skill_child_parent(
                parent,
                AUTO_SKILL_PRIVATE_STATE_DIRNAME,
                create=False,
            )
        )
        if root.native_identity != binding.root_identity:
            raise OSError("authority root changed before the rename probe")
        try:
            if _DIR_FD_SUPPORTED:
                fd = os.open(source_name, flags, 0o600, dir_fd=root.fd)
            else:
                fd = os.open(str(root.path / source_name), flags, 0o600)
            os.close(fd)
            self._rename_skill_child_no_replace(root, source_name, root, destination_name)
        finally:
            for probe_name in (source_name, destination_name):
                try:
                    probe = self._stat_pinned_child(root, probe_name)
                    self._unlink_skill_child(
                        root,
                        probe_name,
                        expected=probe,
                        expected_identity=self._pinned_child_identity(root, probe_name),
                    )
                except OSError:
                    continue


def _probe_no_replace_rename(self) -> bool:
    """Verify atomic no-replace support under one retained claims parent."""
    token = secrets.token_hex(16)
    source_name = f"{_RENAME_PROBE_PREFIX}{token}-source"
    destination_name = f"{_RENAME_PROBE_PREFIX}{token}-destination"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        with self._pin_private_state(create=True) as state:
            claims = state.claims
            if _DIR_FD_SUPPORTED:
                fd = os.open(source_name, flags, 0o600, dir_fd=claims.fd)
            else:
                if not self._pinned_parent_matches(claims):
                    raise OSError("claims parent changed before rename probe")
                fd = os.open(str(claims.path / source_name), flags, 0o600)
            os.close(fd)
            fd = None
            self._rename_skill_child_no_replace(
                claims,
                source_name,
                claims,
                destination_name,
            )
            destination = self._stat_pinned_child(claims, destination_name)
            destination_identity = self._pinned_child_identity(claims, destination_name)
            return self._unlink_skill_child(
                claims,
                destination_name,
                expected=destination,
                expected_identity=destination_identity,
            )
    except OSError:
        logger.warning(
            "Atomic no-replace rename is unavailable under %s. Move KIROCREW_HOME to a "
            "local filesystem that supports renameat2(RENAME_NOREPLACE) on Linux or "
            "renameatx_np(RENAME_EXCL) on macOS, then retry.",
            self._claims_root(),
            exc_info=True,
        )
        return False
    finally:
        if fd is not None:
            os.close(fd)
        try:
            with self._pin_private_state(create=False) as state:
                for probe_name in (source_name, destination_name):
                    try:
                        probe = self._stat_pinned_child(state.claims, probe_name)
                    except OSError:
                        continue
                    probe_identity = self._pinned_child_identity(
                        state.claims,
                        probe_name,
                    )
                    self._unlink_skill_child(
                        state.claims,
                        probe_name,
                        expected=probe,
                        expected_identity=probe_identity,
                    )
        except OSError:
            logger.debug("Could not remove a rename probe", exc_info=True)
