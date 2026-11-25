"""Bring back the pages of an imported Bot's retained dashboard versions.

A Bot's dashboard record keeps up to ``instance.MAX_RETAINED_VERSIONS`` earlier
versions under ``members/<slug>/dashboard/versions/``, and ``instance.rollback`` to
one serves the gateway-owned page snapshot its pin names. The snapshots live in
``crew-panels/``, which an export never carries, so once the record is in place the
import rebuilds each missing one from the carried package, through the same judge the
current page gets (``portability_bot_stores._owned_package``, which requires the
package's LIVE version, and the pinned one, to be bound to this Bot, and checks the
pinned page against its digest) and the same uniqueness check
(``portability_bot_stores.BoundIndex.refusal``).

The carried package is ``portability_bot_types.carried_pin``'s: the record's own pin,
or for a record that runs its catalog template the pin of its newest retained version
that names a package. For such a record the package is published add-only after the
record is in place (:func:`_publish_template_package`); a refusal there stops the
publish and never changes the record's own outcome.

Every function here is called by ``portability_bot_stores`` (which imports them, so
the original path names them too) and reads that module's helpers at call time, so a
patch on ``portability_bot_stores`` reaches them. None of them raises.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.portability_bot_types import ImportedBot, version_file_number

if TYPE_CHECKING:
    from kiro_crew.portability_bot_stores import BoundIndex

logger = logging.getLogger("kiro_crew.portability_bot_stores")

#: The documents of a Bot's newest retained version files by ``n``, and how many of
#: those files could not be read (:func:`_retained_version_docs`).
Retained = tuple[dict[int, Mapping[str, Any]], int]


def _retained_version_docs(slug: str) -> Retained:
    """*slug*'s newest retained version files, read once for every consumer.

    Of the names ``instance._list_versions`` lists that are version names by
    ``portability_bot_types.version_file_number`` (ASCII digits only), the
    ``instance.MAX_RETAINED_VERSIONS`` highest by ``(n, name)`` (the set
    ``instance._prune_versions`` keeps), each read with ``instance._read_version`` in
    its own ``try``. A file that cannot be read (one nested past the decoder's limit
    included) is skipped, logged and counted. A listing that fails with
    ``FileNotFoundError`` (no ``versions/`` folder) is no documents and a count of 0;
    any other failure of the listing is no documents and a count of 1. Never raises.
    """
    from kiro_crew.dashboard_templates import instance

    try:
        names = instance._list_versions(slug)
    except FileNotFoundError:
        return {}, 0
    except (instance.InstanceError, OSError, ValueError):
        logger.warning("import: %r's dashboard versions could not be listed", slug, exc_info=True)
        return {}, 1
    found = sorted((n, name) for name in names if (n := version_file_number(name)) is not None)
    docs: dict[int, Mapping[str, Any]] = {}
    unread = 0
    for n, _name in found[-instance.MAX_RETAINED_VERSIONS :]:
        try:
            docs[n] = instance._read_version(slug, n)
        except (instance.InstanceError, OSError, ValueError, RecursionError):
            logger.warning("import: %r's dashboard version %d is unreadable", slug, n)
            unread += 1
    return docs, unread


def _installed_record_doc(slug: str) -> Mapping[str, Any]:
    """The installed (or kept) record of *slug* as a document; empty when unreadable."""
    from kiro_crew.dashboard_templates import instance

    try:
        return instance._read_record(slug) or {}
    except (instance.InstanceError, OSError, ValueError, RecursionError):
        return {}


def _rebuild_one(
    mc: Path, bot: ImportedBot, index: "BoundIndex", pin: tuple[str, int, str]
) -> bool:
    """Write the page snapshot of one retained pin of the carried package; True when written."""
    from kiro_crew import portability_bot_stores as _stores

    package, version, digest = pin
    here = _stores._owned_package(mc, package, bot.slug, version, digest, index)
    if here.owner or here.page or index.refusal(package, bot.slug):
        return False
    return not _stores._write_page(bot.slug, here.content)


def _rebuild_retained_pages(
    mc: Path,
    bot: ImportedBot,
    index: "BoundIndex",
    carried: str,
    current_digest: str,
    retained: Retained,
) -> tuple[int, int]:
    """Rebuild the missing page snapshots of *bot*'s retained versions: (rebuilt, not rebuilt).

    *retained* is the one :func:`_retained_version_docs` read the caller made for the
    carried pin. For each distinct digest its documents pin, other than
    *current_digest* (the record's own page, already judged; ``""`` for a record that
    runs its template), whose snapshot is missing: a pin of a package other than
    *carried* (``""`` when no package may serve one) is not rebuilt; a pin of the
    carried package is rebuilt only when ``_owned_package`` finds the package this
    Bot's (its live version and the pinned one bound to ``crewmate:<slug>``) and the
    pinned page matching its digest, and ``BoundIndex.refusal`` proves no other
    package is bound to the Bot. A refusal, a page reason or a failed write counts as
    not rebuilt. The pins are read without the instance lock; each page write takes it.
    A released or template-only version pins nothing. Never raises.
    """
    from kiro_crew import portability_bot_stores as _stores
    from kiro_crew.dashboard_templates import instance

    docs, _unread = retained
    pins: dict[str, tuple[str, int]] = {}
    for n in sorted(docs, reverse=True)[: instance.MAX_RETAINED_VERSIONS]:
        doc = docs[n]
        if not isinstance(doc, Mapping):
            continue
        package, version, digest, _released = instance._package_field(doc)
        if package and digest and digest != current_digest:
            pins.setdefault(digest, (package, version))
    rebuilt = missed = 0
    for digest, (package, version) in pins.items():
        try:
            if not _stores._page_missing(bot.slug, digest):
                continue
            if (
                package == carried
                and carried
                and _rebuild_one(mc, bot, index, (package, version, digest))
            ):
                rebuilt += 1
            else:
                missed += 1
        except Exception:  # noqa: BLE001 - one pin's failure is that pin's, never the import's
            logger.warning("import: %r's retained page was not rebuilt", bot.slug, exc_info=True)
            missed += 1
    return rebuilt, missed


def _publish_template_package(
    snap: Path,
    mc: Path,
    bot: ImportedBot,
    stage: Path,
    index: "BoundIndex",
    pin: tuple[str, int, str],
    *,
    owned: bool,
) -> tuple[str, bool]:
    """Publish the carried package of a record that runs its template: (reason, published).

    Runs once the record is installed or kept. The reason is ``""`` when the carried
    package *pin* names is installed here and is this Bot's. When ``artifacts/<P>`` is
    here, a Bot that was not here (*owned* False) is refused ``held_by_other_bot``, as a
    pinned record is (a package already here is never a Bot's that is new here); an
    owned Bot's must be its own with a page that passes (``_owned_package``) and no
    other package bound to it (``BoundIndex.refusal``). When it is not here, the archive
    must carry the pinned version (``_archive_carries``), and the staged package must
    not be bound elsewhere by the archive (``_staged_held``), must pass the uniqueness
    check and the staged gate (``_check_staged_artifact``) before it is published
    add-only; one that appears meanwhile is judged as one here. Any reason stops the
    publish and is logged; it never changes the record's row. Never raises: a failure
    is ``write_failed``.
    """
    from kiro_crew import portability_bot_stores as _stores
    from kiro_crew.artifacts import slug_is_well_formed

    package, version, digest = pin

    def here() -> str:
        if not owned:
            return _stores._HELD
        judged = _stores._owned_package(mc, package, bot.slug, version, digest, index)
        return judged.owner or judged.page or index.refusal(package, bot.slug)

    try:
        if not slug_is_well_formed(package):
            return "artifact_rejected", False
        if os.path.lexists(mc / "artifacts" / package):
            return here(), False
        if not _stores._archive_carries(snap, package, version):
            return "artifact_missing", False
        _stores._stage_artifact(snap, stage, package)
        art_dir = stage / "artifacts" / package
        reason = _stores._staged_held(art_dir, bot.slug) or index.refusal(package, bot.slug)
        if not reason:
            reason, _content = _stores._check_staged_artifact(
                art_dir, package, bot.slug, version, digest
            )
        if not reason:
            reason = _stores._publish_artifact(stage, mc, package)
            if reason == "exists":
                return here(), False
    except _stores._DECODE_FAILURES:
        logger.warning("import: %r's carried package was not published", bot.slug, exc_info=True)
        return "write_failed", False
    if reason:
        logger.info(
            "import: %r's carried package %r was not published: %s", bot.slug, package, reason
        )
        return reason, False
    return "", True
