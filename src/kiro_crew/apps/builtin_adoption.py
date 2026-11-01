"""Former built-in adoption: a builtin-owned record moves, in place, to its own repository.

A release that pins an app in ``adoption-pins.json`` still ships its wheel copy. On
a host whose record is builtin-owned, the gateway fetches the pinned tree in the
background after the listener serves (STAGE), and at the NEXT boot, before any
route is registered, swaps the record onto it (APPLY). From then on the wheel copy
is *released*: it registers no route, runs no hook and is no root any chooser
resolves (:func:`builtin_released`). A host that cannot adopt keeps running the
wheel copy unchanged.

This module stays light at import (``dashboard/routes/system.py`` imports it at
module level): the boot work is in ``builtin_adoption_apply``, imported on use.
Nothing here names an app. Owning spec: app-kit-platform.md § Former built-in
adoption.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from kiro_crew.apps.builtin_adoption_pins import adoption_pin, adoption_pin_names

if TYPE_CHECKING:
    from kiro_crew.apps.builtin_adoption_markers import AdoptionMarker
    from kiro_crew.apps.builtin_adoption_pins import AdoptionPin
    from kiro_crew.apps.manager import InstalledApp

logger = logging.getLogger(__name__)


def builtin_released(name: str) -> bool:
    """True when the wheel's built-in copy of *name* must not register, serve or execute.

    Can only REMOVE first-party status, never grant it (the rule of
    ``execution.builtin_app_names``): every input is immutable (the wheel pin
    table), sealed (``config.json``), or ``installed.json``, which already decides
    stand-down today.
    """
    from kiro_crew.apps.manager import (
        INSTALLED_META_FILENAME,
        _absence_is_genuine,
        _builtin_owns_install,
        _read_installed,
        _update_in_progress,
        app_dir,
    )

    record = _read_installed(name)
    if record is not None:
        # Adopted, or a user app shadowing the name.
        return not _builtin_owns_install(record)
    meta_path = app_dir(name) / INSTALLED_META_FILENAME
    if meta_path.exists() or not _absence_is_genuine(meta_path) or _update_in_progress(name):
        return False  # unreadable or mid-swap: keep today's behaviour
    # No record: released only once uninstalled after adoption. A fresh install of a
    # pinned name registers the built-in as today, then adopts like any other record.
    try:
        from kiro_crew.apps.builtin_adoption_state import read_adoption_markers

        marker = read_adoption_markers().get(name)
        return marker is not None and marker["state"] == "adopted"
    except Exception:  # noqa: BLE001 - an unreadable marker keeps today's behaviour
        return False


def builtin_package_released(package: str) -> bool:
    """:func:`builtin_released` for a ``BUILTIN_NAMES`` package dir, without importing it.

    The manifest name is read from the shipped ``app.json`` of the package whose
    directory is *package*; no manifest found keeps today's behaviour (False).
    """
    from kiro_crew.apps.execution import _shipped_builtin_apps

    try:
        for root, manifest_name in _shipped_builtin_apps():
            if root.name == package:
                return builtin_released(manifest_name)
    except Exception:  # noqa: BLE001 - an unanswerable question keeps today's routes
        logger.debug("builtin package release check failed for %s", package, exc_info=True)
    return False


def released_builtin_packages(packages: list[str] | tuple[str, ...]) -> frozenset[str]:
    """The *packages* whose app :func:`builtin_released` answers True for, in one walk.

    The boot route loop asks this once for every ``BUILTIN_NAMES`` package, so the
    shipped manifests are read once rather than once per package. A package the
    question cannot be answered for is left out (today's behaviour: it registers).
    """
    from kiro_crew.apps.execution import _shipped_builtin_apps

    wanted = set(packages)
    released: set[str] = set()
    try:
        names = {root.name: name for root, name in _shipped_builtin_apps() if root.name in wanted}
    except Exception:  # noqa: BLE001 - an unanswerable question keeps today's routes
        logger.debug("builtin package release check failed", exc_info=True)
        return frozenset()
    for package, manifest_name in names.items():
        try:
            if builtin_released(manifest_name):
                released.add(package)
        except Exception:  # noqa: BLE001 - one unreadable record keeps its routes
            logger.debug("builtin release check failed for %s", package, exc_info=True)
    return frozenset(released)


def _same_target(a: str, b: str) -> bool:
    from kiro_crew.apps.registry_pipeline.git_targets import _same_git_target

    return bool(a) and bool(b) and _same_git_target(a, b)


def record_is_adopted_from(record: Any, repository: str) -> bool:
    """Whether *record* is a non-builtin record installed from *repository*."""
    from kiro_crew.apps.manager import _builtin_owns_install

    return (
        record is not None
        and not _builtin_owns_install(record)
        and _same_target(getattr(record, "sourceUrl", ""), repository)
    )


def adoption_repository(name: str, markers: dict[str, AdoptionMarker] | None = None) -> str:
    """The repository *name* is pinned or was adopted onto, or ``""``."""
    pin = adoption_pin(name)
    if pin is not None:
        return pin.repository
    marker = (markers if markers is not None else _markers_or_empty()).get(name)
    return marker["repository"] if marker else ""


def is_adopted_record(name: str, record: Any) -> bool:
    """Whether *record* is *name*'s record adopted onto its pinned or marked repository."""
    return record_is_adopted_from(record, adoption_repository(name))


def _markers_or_empty() -> dict[str, AdoptionMarker]:
    try:
        from kiro_crew.apps.builtin_adoption_state import read_base_adoption_markers

        return read_base_adoption_markers()
    except Exception:  # noqa: BLE001 - a row is display state; unknown shows nothing
        logger.debug("adoption markers unreadable for the app listing", exc_info=True)
        return {}


def adoption_rows_context() -> dict[str, AdoptionMarker] | None:
    """The markers ``list_apps`` passes to every :func:`adoption_row`, or None when idle.

    None means nothing is pinned and nothing was ever adopted, so no row carries
    an ``adoption`` field and the listing reads nothing more.
    """
    markers = _markers_or_empty()
    if not markers and not adoption_pin_names():
        return None
    return markers


def adoption_row(
    name: str,
    record: InstalledApp | None,
    *,
    min_kirocrew_version: str = "",
    markers: dict[str, AdoptionMarker] | None = None,
) -> dict[str, Any] | None:
    """The ``adoption`` field of *name*'s ``/api/apps`` row (spec section 0.3), or None.

    Present only for a name with a valid pin or a marker. ``legacyNav`` is copied
    from the wheel pin, and only for a record adopted from the pin's repository.
    """
    from kiro_crew.apps.builtin_adoption_checks import ALL_REASON_CODES
    from kiro_crew.apps.builtin_adoption_state import read_ledger
    from kiro_crew.apps.manager import _builtin_owns_install
    from kiro_crew.apps.version import check_min_version

    pin = adoption_pin(name)
    markers = markers if markers is not None else _markers_or_empty()
    if record is None or (pin is None and name not in markers):
        return None
    repository = adoption_repository(name, markers)
    if not _builtin_owns_install(record):
        if not record_is_adopted_from(record, repository):
            return None  # a user app occupies the name; adoption never applies
        row: dict[str, Any] = {"state": "adopted"}
        if pin is not None:
            row["legacyNav"] = {"route": pin.legacy_route, "icon": pin.legacy_icon}
        if min_kirocrew_version and check_min_version(min_kirocrew_version) is not None:
            row["hostTooOld"] = True
        return row
    if pin is None:
        return None
    ledger = read_ledger(name)
    if ledger is None or ledger.commit != pin.commit:
        return {"state": "pending"}
    state = ledger.state if ledger.state in ("pending", "staged", "blocked") else "pending"
    row = {"state": state}
    if state in ("pending", "blocked") and ledger.reason in ALL_REASON_CODES:
        row["reason"] = ledger.reason
    return row


async def apply_builtin_adoptions() -> dict[str, str]:
    """APPLY every staged pin (network-free; boot, before any route is registered).

    Returns ``{name: outcome}``. Never raises except ``PlatformCompositionError``
    and ``CancelledError``; returns immediately when nothing is pinned or granting.
    """
    from kiro_crew.apps.builtin_adoption_apply import apply_all

    return await apply_all()


async def run_staging_pass(*, now: Callable[[], datetime]) -> dict[str, str]:
    """One STAGE pass over every pin; never mutates an app record."""
    from kiro_crew.apps.builtin_adoption_apply import staging_pass

    return await staging_pass(now=now)


async def stage_builtin_adoptions(
    *,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> None:
    """Run STAGE passes in the background until nothing is pending."""
    from kiro_crew.apps.builtin_adoption_apply import staging_loop

    await staging_loop(now=now, sleep=sleep)


def pinned_tree_refusal(
    pin: AdoptionPin, manifest: Any, tree_root: Any, **kwargs: Any
) -> str | None:
    """Re-export of ``builtin_adoption_checks.pinned_tree_refusal`` (E7, E8, E10)."""
    from kiro_crew.apps.builtin_adoption_checks import pinned_tree_refusal as _refusal

    return _refusal(pin, manifest, tree_root, **kwargs)
