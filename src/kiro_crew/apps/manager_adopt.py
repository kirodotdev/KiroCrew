"""Adopting a builtin-owned record onto its own repository, and recovering an interrupted one.

:func:`adopt_builtin_record` is ``update_app`` with an adoption provenance: the
tree, ``data/``, ``.app_secret`` and approved grants are carried exactly as an
update carries them, and the record becomes a registry record in the same file
transaction (``manager_install.update_app``). :func:`recover_interrupted_adoption`
puts back what a hard kill in the middle of that transaction left aside, and
:func:`sweep_completed_swap_leftovers` removes what a kill after its record was
durable left behind, for any update of any app.

Part of the ``kiro_crew.apps.manager`` family: callers and tests reach every name here
as ``kiro_crew.apps.manager.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from kiro_crew.apps.execution import APP_NAME_RE
from kiro_crew.apps.manager_builtins import _builtin_owns_install
from kiro_crew.apps.manager_install import AdoptionProvenance, app_lifecycle_lock, update_app
from kiro_crew.apps.manager_records import (
    INSTALLED_META_FILENAME,
    AppResult,
    _read_installed,
    _tree_swap_state,
    _update_in_progress,
    _update_marker_dir,
    _update_retired_tree_prefix,
    app_dir,
    apps_dir,
    hold_app_record_lock,
    holds_app_record_locks,
)
from kiro_crew.apps.manager_tree import _owned_data_dir, _remove_any_shape
from kiro_crew.platform_compat import is_link_or_junction, pid_exists

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


#: The ``<pid>-<8 hex>`` stem ``update_app`` gives a retired tree and its token.
_LEFTOVER_STEM_RE = re.compile(r"(\d+)-[0-9a-f]{8}")


def _retired_trees(name: str) -> list[Path]:
    """*name*'s retired trees: its prefix and then exactly a generated stem.

    A bare prefix match would also take another app's sidecars when that app's
    name is *name* plus ``-update-old-<digits>``.
    """
    parent = app_dir(name).parent
    prefix = _update_retired_tree_prefix(name)
    try:
        return sorted(
            p
            for p in parent.iterdir()
            if p.name.startswith(prefix) and _LEFTOVER_STEM_RE.fullmatch(p.name[len(prefix) :])
        )
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
    left untouched, and ``register_builtin_apps`` registers nothing over it.
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


#: An update marker (``.<name>-update-inflight``) or a retired tree
#: (``.<name>-update-old-<pid>-<8 hex>``), with the app name as group 1.
_SWAP_LEFTOVER_RE = re.compile(r"\.(.+)-update-(?:inflight|old-\d+-[0-9a-f]{8})")


def _leftover_owner_dead(stem: str) -> bool:
    """Whether the ``<pid>-<8 hex>`` *stem* of a token or retired tree names a dead process.

    Any other stem, this process, and a pid that still exists (or cannot be
    signalled) all count as alive: such a leftover is kept.
    """
    match = _LEFTOVER_STEM_RE.fullmatch(stem)
    if match is None:
        return False
    pid = int(match.group(1))
    return pid != os.getpid() and not pid_exists(pid)


def swap_leftover_names() -> list[str]:
    """App names that have an update marker or a retired tree beside their directory."""
    try:
        entries = [entry.name for entry in apps_dir().iterdir()]
    except OSError:
        return []
    names = {match.group(1) for match in map(_SWAP_LEFTOVER_RE.fullmatch, entries) if match}
    return sorted(name for name in names if APP_NAME_RE.fullmatch(name))


@holds_app_record_locks
def sweep_completed_swap_leftovers(name: str) -> bool:
    """Remove what a hard-killed update of *name* left that nothing can still need. Blocking.

    ``update_app`` writes the new ``installed.json`` and only then removes the
    retired tree and its token in ``.<name>-update-inflight``. A kill in between
    leaves both: the retired tree is read as a second record of the app, and the
    marker reads as an update still in flight, which withholds grants and keeps a
    former built-in from being released once it is uninstalled. The caller holds
    ``app_lifecycle_lock(name)``.

    A token is removed only when its process is gone, and the rest only when no
    token of a live process remains. A retired tree is removed only beside a
    readable live record in a directory that is not a link: the swap then
    completed (``data/`` and ``.app_secret`` were put back before the record was
    written), and removing it is the step ``update_app`` would have run next.
    Without a record the marker goes only when nothing is set aside (no retired
    tree, ``.<name>-data-tmp`` or ``.<name>-secret-tmp``), since it is what keeps
    ``register_builtin_apps`` and a later update off a killed swap's files.
    Returns True when it removed anything.
    """
    if _tree_swap_state(name)[0]:
        return False
    hold_app_record_lock(name)
    try:
        return _sweep_dead_leftovers(name)
    except OSError:
        logger.warning("Could not sweep the interrupted update of %s", name, exc_info=True)
        return False


def _sweep_dead_leftovers(name: str) -> bool:
    """The removal half of :func:`sweep_completed_swap_leftovers`; raises ``OSError``."""
    dest = app_dir(name)
    retired = _retired_trees(name)
    if is_link_or_junction(dest):
        return False
    if _read_installed(name) is None:
        set_aside = (dest.parent / f".{name}-data-tmp", dest.parent / f".{name}-secret-tmp")
        if retired or any(os.path.lexists(path) for path in set_aside):
            return False
    removed = False
    marker = _update_marker_dir(name)
    if is_link_or_junction(marker) or (os.path.lexists(marker) and not marker.is_dir()):
        return False
    if marker.is_dir():
        for token in sorted(marker.iterdir()):
            if _leftover_owner_dead(token.name):
                token.unlink(missing_ok=True)
                removed = True
        try:
            marker.rmdir()
        except OSError:
            return removed  # a live update still holds a token: it owns the rest
        removed = True
    for tree in retired:
        if _leftover_owner_dead(tree.name[len(_update_retired_tree_prefix(name)) :]):
            _remove_any_shape(tree)
            removed = True
    if removed:
        logger.info("Removed what an interrupted update of %s left beside its directory", name)
    return removed


async def sweep_interrupted_updates(names: Iterable[str] | None = None) -> None:
    """Run :func:`sweep_completed_swap_leftovers` for *names*, or every app with leftovers.

    Never raises. Each app under its ``app_lifecycle_lock``, off the event loop.
    Without *names* it lists the apps directory first: a tracked task kicked after
    the listener serves. Before APPLY only the few names
    ``builtin_adoption.apply_builtin_adoptions`` passes are swept.
    """
    if names is None:
        try:
            names = await asyncio.to_thread(swap_leftover_names)
        except Exception:  # noqa: BLE001 - a sweep failure must not escape the task
            logger.warning("Could not list interrupted app updates", exc_info=True)
            return
    for name in names:
        try:
            async with app_lifecycle_lock(name):
                await asyncio.to_thread(sweep_completed_swap_leftovers, name)
        except Exception:  # noqa: BLE001 - one app's leftovers must not stop the others
            logger.warning("Could not sweep the interrupted update of %s", name, exc_info=True)
