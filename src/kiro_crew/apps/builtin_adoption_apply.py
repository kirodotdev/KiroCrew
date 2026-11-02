"""STAGE and APPLY passes of built-in adoption (see ``builtin_adoption``).

APPLY, per pin in name order, under the app's lifecycle lock:

0. recovery of a hard-killed swap, then the crash windows a ``granting`` marker names;
1. eligibility of the record (E2); a ledger that is ``staged`` for the pin commit;
2. the staged artifact copied into a private temp dir and ``treeSha256`` verified
   on that copy; the tree checks (E7, E8, E10), host (E3, E5), admission (E4) and
   permission parity (E9);
3. the trust plan (E6) and ONE trust-writer transaction: grant plus ``granting``
   marker;
4. the swap (``manager.adopt_builtin_record``); on failure exactly what step 3 wrote
   is rolled back; on success the marker becomes ``adopted``.

STAGE never mutates a record. Its retry state is the data-home ledger, with a
deterministic backoff (5 minutes, doubling, capped at 24 hours) on an injected
clock. Every transition writes one SEL row and one log line.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtin_adoption_checks import (
    NEVER_RETRIED_CODES,
    RECHECKED_CODES,
    TRANSIENT_CODES,
    admission_refusal,
    host_refusal,
    permission_refusal,
    pinned_tree_refusal,
    plan_grant,
)
from kiro_crew.apps.builtin_adoption_fetch import (
    AdoptionRefused,
    StagedArtifact,
    materialize_verified_tree,
    private_temp_dir,
    stage_pin,
)
from kiro_crew.apps.builtin_adoption_markers import AdoptionMarker
from kiro_crew.apps.builtin_adoption_pins import AdoptionPin, load_adoption_pins
from kiro_crew.apps.builtin_adoption_state import (
    AdoptionLedger,
    GrantPlan,
    TrustConflict,
    finalize_marker,
    read_adoption_markers,
    read_ledger,
    rollback_grant_and_marker,
    write_grant_and_marker,
    write_ledger,
)

logger = logging.getLogger("kiro_crew.apps.builtin_adoption")

BACKOFF_BASE = timedelta(minutes=5)
BACKOFF_CAP = timedelta(hours=24)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _git_available() -> bool:
    return shutil.which("git") is not None


def audit(name: str, commit: str, outcome: str, *, path: str = "-", code: str = "") -> None:
    """One SEL row for one adoption transition. Repository URLs are never logged."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="app_adoption",
            operation="app_builtin_adoption",
            outcome=outcome,
            resources=f"name={name!r} commit={commit[:12]} path={path} code={code or '-'}",
        )
    except Exception:  # noqa: BLE001 - losing the audit line must not change the outcome
        logger.warning("could not audit the adoption of %r", name, exc_info=True)


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def backoff_after(attempts: int) -> timedelta:
    """The wait after the *attempts*-th consecutive transient failure."""
    if attempts < 1:
        return timedelta(0)
    return min(BACKOFF_BASE * (2 ** min(attempts - 1, 16)), BACKOFF_CAP)


# --------------------------------------------------------------------------- #
# STAGE
# --------------------------------------------------------------------------- #


def _record_builtin_owned(name: str) -> bool:
    from kiro_crew.apps.manager import builtin_owns_installed

    return builtin_owns_installed(name)


def _shipped_manifest(name: str) -> Any:
    from kiro_crew.apps.execution import _shipped_builtin_apps
    from kiro_crew.apps.manifest import AppManifest

    for root, manifest_name in _shipped_builtin_apps():
        if manifest_name == name:
            try:
                return AppManifest.from_json_file(root / "app.json")
            except Exception:  # noqa: BLE001 - an unreadable wheel manifest has no version
                return None
    return None


def _load_manifest(tree: Path) -> Any:
    from kiro_crew.apps.manifest import AppManifest

    try:
        return AppManifest.from_json_file(tree / "app.json")
    except Exception:  # noqa: BLE001 - an unparseable manifest is an identity mismatch
        return None


def _tree_checks(pin: AdoptionPin, tree: Path) -> str | None:
    """The deterministic checks STAGE runs early, on its throwaway verified copy."""
    from kiro_crew.apps.manager import _read_installed

    record = _read_installed(pin.name)
    return pinned_tree_refusal(
        pin,
        _load_manifest(tree),
        tree,
        shipped_manifest=_shipped_manifest(pin.name),
        record_version=record.version if record is not None else "",
    )


def _stage_skip(ledger: AdoptionLedger | None, now: datetime) -> bool:
    if ledger is None:
        return False
    if ledger.state in ("staged", "adopted"):
        return True
    if ledger.state == "blocked":
        return True  # never-retried, or re-checked at APPLY from the staged artifact
    due = _parse_iso(ledger.next_attempt_at)
    return due is not None and now < due


def _stage_one_sync_prelude(pin: AdoptionPin) -> AdoptionLedger | None:
    ledger = read_ledger(pin.name)
    if ledger is not None and ledger.commit != pin.commit:
        ledger = None  # a new pin starts over
    return ledger


async def _stage_one(pin: AdoptionPin, now: datetime) -> str:
    if not await asyncio.to_thread(_record_builtin_owned, pin.name):
        return "skipped"
    ledger = await asyncio.to_thread(_stage_one_sync_prelude, pin)
    if _stage_skip(ledger, now):
        return "skipped"
    previous = ledger
    ledger = ledger or AdoptionLedger(name=pin.name, commit=pin.commit)
    result = await stage_pin(
        pin,
        git_available=_git_available(),
        verify=lambda tree: _tree_checks(pin, tree),
    )
    if result.ok and result.staged is not None:
        from kiro_crew.config.loader import config_dir

        try:
            rel = result.staged.path.relative_to(config_dir()).as_posix()
        except ValueError:
            rel = result.staged.path.as_posix()
        ledger.state, ledger.reason, ledger.next_attempt_at = "staged", "", ""
        ledger.staged = {"kind": result.staged.kind, "path": rel}
        await asyncio.to_thread(write_ledger, ledger)
        audit(pin.name, pin.commit, "staged", path=result.staged.kind)
        logger.info("Adoption of %s staged from %s", pin.name, result.staged.kind)
        return "staged"
    code = result.code or "network_unreachable"
    if code in NEVER_RETRIED_CODES:
        ledger.state, ledger.reason, ledger.next_attempt_at = "blocked", code, ""
        await asyncio.to_thread(write_ledger, ledger)
        audit(pin.name, pin.commit, "blocked", path=result.detail or "-", code=code)
        logger.warning("Adoption of %s blocked for this release: %s", pin.name, code)
        return "blocked"
    ledger.attempts += 1
    ledger.state, ledger.reason = "pending", code
    ledger.next_attempt_at = (now + backoff_after(ledger.attempts)).isoformat()
    await asyncio.to_thread(write_ledger, ledger)
    if previous is None or previous.state != "pending" or previous.reason != code:
        audit(pin.name, pin.commit, "stage_failed", path=result.detail or "-", code=code)
        logger.info("Adoption of %s could not stage (%s); retrying later", pin.name, code)
    return "pending"


async def staging_pass(*, now: Callable[[], datetime]) -> dict[str, str]:
    """One STAGE pass over every pin in name order: ``{name: outcome}``."""
    pins = load_adoption_pins()
    outcomes: dict[str, str] = {}
    for name in sorted(pins):
        try:
            outcomes[name] = await _stage_one(pins[name], now())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one pin must not stop the others
            logger.exception("Adoption staging of %s failed", name)
            outcomes[name] = "error"
    return outcomes


def _seconds_until_next(now: datetime) -> float | None:
    """Seconds until the earliest pending retry, or None when nothing is pending."""
    waits: list[float] = []
    for name, pin in load_adoption_pins().items():
        ledger = read_ledger(name)
        if ledger is None or ledger.commit != pin.commit or ledger.state != "pending":
            continue
        if not _record_builtin_owned(name):
            continue
        due = _parse_iso(ledger.next_attempt_at)
        waits.append(0.0 if due is None else max((due - now).total_seconds(), 0.0))
    return min(waits) if waits else None


async def staging_loop(
    *,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> None:
    """Run :func:`staging_pass` until nothing is pending; one pass always runs."""
    clock = now or _utcnow
    pause = sleep or asyncio.sleep
    if not load_adoption_pins():
        return
    while True:
        await staging_pass(now=clock)
        wait = await asyncio.to_thread(_seconds_until_next, clock())
        if wait is None:
            return
        await pause(min(max(wait, 1.0), BACKOFF_CAP.total_seconds()))


# --------------------------------------------------------------------------- #
# APPLY
# --------------------------------------------------------------------------- #


def _record_eligible(name: str) -> bool:
    """E2: a safe builtin-owned record whose directory the swap may move."""
    from kiro_crew.apps.manager import (
        _builtin_owns_install,
        _check_path_safety,
        _read_installed,
        _temp_data_name_obstruction,
        app_dir,
        gateway_data_dir_obstruction,
    )
    from kiro_crew.platform_compat import is_link_or_junction

    if not _check_path_safety(name):
        return False
    record = _read_installed(name)
    if record is None or not _builtin_owns_install(record):
        return False
    path = app_dir(name)
    if path.is_symlink() or is_link_or_junction(path):
        return False
    if gateway_data_dir_obstruction(path):
        return False
    return not _temp_data_name_obstruction(path.parent / f".{name}-data-tmp")


def _record_matches_pin(name: str, pin: AdoptionPin) -> bool:
    from kiro_crew.apps.builtin_adoption import record_is_adopted_from
    from kiro_crew.apps.manager import _read_installed

    record = _read_installed(name)
    return (
        record is not None
        and record_is_adopted_from(record, pin.repository)
        and record.sourceCommit == pin.commit
    )


def _staged_artifact(ledger: AdoptionLedger) -> StagedArtifact | None:
    from kiro_crew.config.loader import config_dir

    kind, rel = ledger.staged.get("kind"), ledger.staged.get("path", "")
    if kind not in ("git", "archive") or not rel:
        return None
    path = (config_dir() / rel).resolve()
    if not path.is_relative_to(config_dir().resolve()):
        return None
    return StagedArtifact(kind, path)  # type: ignore[arg-type]


def _prepare_tree(pin: AdoptionPin, staged: StagedArtifact) -> tuple[Path, Path, Any]:
    """Materialize and verify the pinned tree in a private temp dir. Blocking."""
    tmp = private_temp_dir("apply")
    tree = tmp / "tree"
    try:
        materialize_verified_tree(pin, staged, tree)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp, tree, _load_manifest(tree)


def _config_refusals(pin: AdoptionPin, manifest: Any) -> str | None:
    """E3, E5, E4 and E9, in that order, re-run every boot. Blocking."""
    return (
        host_refusal(pin, manifest)
        or admission_refusal(pin.name, manifest)
        or permission_refusal(pin.name, manifest)
    )


def _set_ledger(ledger: AdoptionLedger, state: str, reason: str = "") -> None:
    ledger.state, ledger.reason = state, reason
    if state != "pending":
        ledger.next_attempt_at = ""
    write_ledger(ledger)


async def _block(ledger: AdoptionLedger, pin: AdoptionPin, code: str, kind: str) -> str:
    previous = (ledger.state, ledger.reason)
    if code in NEVER_RETRIED_CODES:
        ledger.staged = {}
    await asyncio.to_thread(_set_ledger, ledger, "blocked", code)
    if previous != ("blocked", code):
        audit(pin.name, pin.commit, "blocked", path=kind, code=code)
        log = logger.warning if code in NEVER_RETRIED_CODES else logger.info
        log("Adoption of %s blocked: %s", pin.name, code)
    return "blocked"


async def _apply_one(pin: AdoptionPin, marker: AdoptionMarker | None) -> str:
    from kiro_crew.apps.manager import recover_interrupted_adoption

    name = pin.name
    if marker is not None and marker["state"] == "granting" and marker["commit"] == pin.commit:
        if await asyncio.to_thread(recover_interrupted_adoption, name):
            audit(name, pin.commit, "recovered")
        if await asyncio.to_thread(_record_matches_pin, name, pin):
            await finalize_marker(name, pin)
            ledger = await asyncio.to_thread(read_ledger, name)
            if ledger is not None:
                await asyncio.to_thread(_set_ledger, ledger, "adopted")
            audit(name, pin.commit, "finalized")
            return "finalized"
    if not await asyncio.to_thread(_record_eligible, name):
        return "skipped"
    ledger = await asyncio.to_thread(read_ledger, name)
    if ledger is None or ledger.commit != pin.commit:
        return "not_staged"
    # A transient APPLY failure (``config_unreadable``, ``disk_full``) keeps its
    # staged artifact, so it is re-checked every boot like a cured refusal.
    if ledger.state == "blocked" and ledger.reason not in RECHECKED_CODES | TRANSIENT_CODES:
        return "blocked"
    if ledger.state not in ("staged", "blocked"):
        return "not_staged"
    staged = _staged_artifact(ledger)
    if staged is None:
        await asyncio.to_thread(_set_ledger, ledger, "pending")
        return "not_staged"
    try:
        tmp, tree, manifest = await asyncio.to_thread(_prepare_tree, pin, staged)
    except AdoptionRefused as refused:
        return await _block(ledger, pin, refused.code, staged.kind)
    except OSError:
        logger.info("Staged tree of %s is gone; staging again", name, exc_info=True)
        ledger.staged = {}
        await asyncio.to_thread(_set_ledger, ledger, "pending")
        return "not_staged"
    try:
        return await _apply_verified(pin, ledger, marker, tree, manifest, staged.kind)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)


async def _apply_verified(
    pin: AdoptionPin,
    ledger: AdoptionLedger,
    marker: AdoptionMarker | None,
    tree: Path,
    manifest: Any,
    kind: str,
) -> str:
    from kiro_crew.apps.manager import adopt_builtin_record, invalidate_orphan_cache

    name = pin.name
    refusal = await asyncio.to_thread(_tree_checks, pin, tree) or await asyncio.to_thread(
        _config_refusals, pin, manifest
    )
    # A ``granting`` marker for this commit is a crash after the grant and before
    # the swap: its grant is reused, and its flags stay the ones the rollback reads.
    reused = (
        marker
        if marker is not None and marker["state"] == "granting" and marker["commit"] == pin.commit
        else None
    )
    plan: GrantPlan | str | None = None
    if refusal is None and reused is None:
        plan = await asyncio.to_thread(plan_grant, name, pin)
        if isinstance(plan, str):
            refusal = plan
    if refusal is not None:
        if reused is not None:
            await _rollback(name, pin, reused)
        return await _block(ledger, pin, refusal, kind)
    written: AdoptionMarker
    if reused is not None:
        written = reused
    else:
        assert isinstance(plan, GrantPlan)
        try:
            written = await write_grant_and_marker(name, pin, plan=plan)
        except TrustConflict:
            return await _block(ledger, pin, "trust_conflict", kind)
        except Exception as exc:  # noqa: BLE001 - classified below
            code = _trust_write_code(exc)
            if code is None:
                raise
            return await _block(ledger, pin, code, kind)
    result = await asyncio.to_thread(
        adopt_builtin_record, name, tree, repository=pin.repository, commit=pin.commit
    )
    if not result.ok:
        await _rollback(name, pin, written)
        audit(name, pin.commit, "apply_failed", path=kind, code=result.error_code or "")
        if result.error_code == "adoption_would_widen":
            return await _block(ledger, pin, "permission_widening", kind)
        logger.warning("Adoption of %s failed; the built-in keeps running: %s", name, result.error)
        return "apply_failed"
    await finalize_marker(name, pin)
    await asyncio.to_thread(_set_ledger, ledger, "adopted")
    audit(name, pin.commit, "applied", path=kind)
    logger.info("Adopted %s from its repository at %s", name, pin.commit[:12])
    await asyncio.to_thread(_refresh_after_swap, name)
    invalidate_orphan_cache()
    return "applied"


def _trust_write_code(exc: Exception) -> str | None:
    from kiro_crew.dashboard.handlers.security_audit import ConfigCorruptError
    from kiro_crew.dashboard.handlers.security_trusted_apps import TrustSettingOverlayOwned

    if isinstance(exc, TrustSettingOverlayOwned):
        return "trust_overlay_owned"
    if isinstance(exc, ConfigCorruptError):
        return "config_unreadable"
    return None


async def _rollback(name: str, pin: AdoptionPin, marker: AdoptionMarker) -> None:
    try:
        await rollback_grant_and_marker(name, marker)
    except Exception:  # noqa: BLE001 - the grant stays; the refusal still stands
        logger.error("Could not roll back the adoption grant of %s", name, exc_info=True)
        return
    audit(name, pin.commit, "rolled_back")


def _refresh_after_swap(name: str) -> None:
    from kiro_crew.apps.manager import refresh_app_config_keys

    refresh_app_config_keys()
    try:
        from kiro_crew.apps.routes import invalidate_app_secret_cache

        invalidate_app_secret_cache(name)
    except Exception:  # noqa: BLE001 - the routes module may not be importable at boot
        logger.debug("routes cache for %s not invalidated after the swap", name, exc_info=True)


def _active() -> tuple[dict[str, AdoptionPin], dict[str, AdoptionMarker]] | None:
    pins = load_adoption_pins()
    try:
        markers = read_adoption_markers()
    except Exception:  # noqa: BLE001 - an unreadable config adopts nothing this boot
        logger.warning("adoption markers unreadable; nothing is adopted this boot", exc_info=True)
        return None
    if not pins and not any(m["state"] == "granting" for m in markers.values()):
        return None
    return pins, markers


async def apply_all() -> dict[str, str]:
    """APPLY every pin in name order: ``{name: outcome}``."""
    from kiro_crew.apps.manager import app_lifecycle_lock
    from kiro_crew.platform import PlatformCompositionError

    active = await asyncio.to_thread(_active)
    if active is None:
        return {}
    pins, markers = active
    outcomes: dict[str, str] = {}
    for name in sorted(pins):
        try:
            async with app_lifecycle_lock(name):
                outcomes[name] = await _apply_one(pins[name], markers.get(name))
        except (asyncio.CancelledError, PlatformCompositionError):
            raise
        except Exception:  # noqa: BLE001 - one pin must not stop the boot
            logger.exception("Adoption of %s failed; the built-in keeps running", name)
            outcomes[name] = "error"
    return {name: outcome for name, outcome in outcomes.items() if outcome != "skipped"}
