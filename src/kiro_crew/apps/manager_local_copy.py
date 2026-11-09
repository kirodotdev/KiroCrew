"""Make a local copy of a marketplace app: the explicit way to change it.

A marketplace app (``source = "market:<ref>@<n>"``) refuses a folder update and
a self-registration over its name (``app_update_market_source``), so its listing
provenance is never overwritten silently. :func:`make_local_copy` is the
visible way out. The copy is the person's own LOCAL app: no ``market:`` source,
no zip SHA-256, no marketplace trust grant, and no marketplace updates.

The copy keeps the app's name and needs ``confirm_replace=True``. The record
is rewritten as local and the app's trust grant is removed, so enabling it
asks for a new local grant. The app's ``data/`` directory, which holds its
state, settings and credential files, stays in place. The record's source is
``local-copy:<market source>``: not a path, so a default update without a
folder is refused, and the lineage stays readable. Changed code is then
applied with the ordinary folder update (``POST /api/apps/{name}/update``),
which a local app accepts. The app must be disabled first, so nothing running
loses its trust grant under it.

Every check runs before anything is written, so a refusal changes nothing.
"""

from __future__ import annotations

from dataclasses import replace

from kiro_crew.apps.manager_records import (
    AppResult,
    _read_installed,
    _write_installed,
    hold_app_record_lock,
    holds_app_record_locks,
)
from kiro_crew.apps.manager_uninstall import _drop_trust_grant
from kiro_crew.apps.market_source import is_market_source
from kiro_crew.sel import sel

#: The ``source`` prefix a local copy records ahead of the marketplace source.
LOCAL_COPY_PREFIX = "local-copy:"


def _refuse(name: str, code: str, error: str) -> AppResult:
    return AppResult(ok=False, name=name, error=error, error_code=code)


def make_local_copy(name: str, *, confirm_replace: bool = False) -> AppResult:
    """Turn marketplace app *name* into a local app (see the module docstring)."""
    existing = _read_installed(name)
    if existing is None:
        return _refuse(name, "app_not_installed", f"app {name!r} is not installed")
    if not is_market_source(existing.source):
        return _refuse(name, "app_local_copy_not_market", f"{name!r} is not a marketplace app")
    return _copy_in_place(name, confirm_replace=confirm_replace)


@holds_app_record_locks
def _copy_in_place(name: str, *, confirm_replace: bool) -> AppResult:
    if confirm_replace is not True:
        return _refuse(
            name,
            "app_local_copy_confirm_required",
            f"making {name!r} a local app under the same name needs an explicit confirm",
        )
    hold_app_record_lock(name)
    existing = _read_installed(name)
    if existing is None or not is_market_source(existing.source):
        return _refuse(name, "app_local_copy_not_market", f"{name!r} is not a marketplace app")
    # Read under the record lock, so an enable that lands after the first read
    # is seen here and refuses instead of leaving an enabled app with no grant.
    if existing.enabled:
        return _refuse(
            name,
            "app_local_copy_disable_first",
            f"disable {name!r} before making a local copy",
        )
    try:
        _drop_trust_grant(name)
    except RuntimeError as exc:
        return _refuse(name, "app_local_copy_trust_unwritable", str(exc))
    _write_installed(
        name,
        replace(
            existing,
            source=LOCAL_COPY_PREFIX + existing.source,
            origin="local",
            sourceUrl="",
            sourceRegistry="",
            sourceCommit="",
            sourceSigner="",
        ),
    )
    sel().log_api_access(
        caller="app_local_copy",
        operation="in_place",
        outcome="success",
        resources=f"name={name!r} from={existing.source!r}",
    )
    return AppResult(ok=True, name=name)
