"""Adopting a builtin-owned record onto its own repository, and recovering an interrupted one.

:func:`adopt_builtin_record` is ``update_app`` with an adoption provenance: the
tree, ``data/``, ``.app_secret`` and approved grants are carried exactly as an
update carries them, and the record becomes a registry record in the same file
transaction (``manager_install.update_app``). :func:`recover_interrupted_adoption`
puts back what a hard kill in the middle of that transaction left aside.

Part of the ``kiro_crew.apps.manager`` family: callers and tests reach every name here
as ``kiro_crew.apps.manager.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from kiro_crew.apps.manager_builtins import _builtin_owns_install
from kiro_crew.apps.manager_install import AdoptionProvenance, update_app
from kiro_crew.apps.manager_records import (
    INSTALLED_META_FILENAME,
    AppResult,
    _read_installed,
    _update_in_progress,
    _update_marker_dir,
    _update_retired_tree_prefix,
    app_dir,
    hold_app_record_lock,
    holds_app_record_locks,
)
from kiro_crew.apps.manager_tree import _owned_data_dir
from kiro_crew.platform_compat import is_link_or_junction

logger = logging.getLogger("kiro_crew.apps.manager")


@holds_app_record_locks
def adopt_builtin_record(name: str, source: Path, *, repository: str, commit: str) -> AppResult:
    """Swap *name*'s builtin-owned record to the verified tree at *source*. Blocking.

    Refuses (``not_builtin_owned``) a record a user installed under the name, and
    (``adoption_would_widen``, from ``update_app``) a tree whose manifest would hold
    back a grant or add ``sessionApproval``. Nothing is mutated on a refusal.
    """
    hold_app_record_lock(name)
    existing = _read_installed(name)
    if existing is None or not _builtin_owns_install(existing):
        return AppResult(
            ok=False,
            name=name,
            error=f"app {name!r} is not a built-in record",
            error_code="not_builtin_owned",
        )
    return update_app(
        source,
        expected_name=name,
        source_repository=repository,
        _adoption=AdoptionProvenance(repository=repository, commit=commit),
    )


def _retired_trees(name: str) -> list[Path]:
    parent = app_dir(name).parent
    prefix = _update_retired_tree_prefix(name)
    try:
        return sorted(p for p in parent.iterdir() if p.name.startswith(prefix))
    except OSError:
        return []


@holds_app_record_locks
def recover_interrupted_adoption(name: str) -> bool:
    """Restore the tree a hard-killed adoption swap left aside. Blocking.

    The caller has already established that an adoption of *name* was in flight
    (a ``granting`` marker). Acts only when an update marker is present and the
    live record is gone, and only on an unambiguous state: exactly one retired
    tree, and no ``data/`` beside a moved-aside data copy. Returns True when it
    restored anything; an ambiguous state is logged (``recovery_ambiguous``) and
    left untouched, so the wheel copy keeps serving.
    """
    if not _update_in_progress(name):
        return False
    hold_app_record_lock(name)
    dest = app_dir(name)
    if (dest / INSTALLED_META_FILENAME).is_file():
        return False
    retired = _retired_trees(name)
    tmp_data = dest.parent / f".{name}-data-tmp"
    tmp_secret = dest.parent / f".{name}-secret-tmp"
    if len(retired) != 1 or not (retired[0] / INSTALLED_META_FILENAME).is_file():
        logger.warning("recovery_ambiguous: %s has %d retired trees", name, len(retired))
        return False
    if is_link_or_junction(dest) or is_link_or_junction(retired[0]):
        logger.warning("recovery_ambiguous: %s or its retired tree is a link", name)
        return False
    partial_data = dest / "data"
    if (retired[0] / "data").exists() and tmp_data.exists():
        logger.warning("recovery_ambiguous: %s has data in two places", name)
        return False
    if dest.exists() and partial_data.exists() and tmp_data.exists():
        logger.warning("recovery_ambiguous: %s has data in two places", name)
        return False
    # A kill after the copy: the half-written replacement tree goes, after
    # anything preserved that was already put back into it is moved aside again.
    if dest.exists():
        if _owned_data_dir(partial_data):
            shutil.move(str(partial_data), str(tmp_data))
        partial_secret = dest / ".app_secret"
        if partial_secret.is_file() and not tmp_secret.exists():
            shutil.move(str(partial_secret), str(tmp_secret))
        shutil.rmtree(dest)
    retired[0].rename(dest)
    if _owned_data_dir(tmp_data) and not (dest / "data").exists():
        shutil.move(str(tmp_data), str(dest / "data"))
    if tmp_secret.is_file() and not (dest / ".app_secret").exists():
        shutil.move(str(tmp_secret), str(dest / ".app_secret"))
    shutil.rmtree(_update_marker_dir(name), ignore_errors=True)
    logger.info("Recovered the tree of an interrupted adoption of %s", name)
    return True
