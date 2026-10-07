"""The template registry: every dashboard template this gateway can offer.

**One source.** Templates ship with the product under ``builtin/<id>/``, each a
directory in the format :mod:`kiro_crew.dashboard_templates.manifest` describes, read by
:func:`~kiro_crew.dashboard_templates.manifest.load_template`. There is exactly one
place a malformed template is refused, and exactly one place a servable page can come
from.

**The registry serves only pages that shipped with the product.** A dashboard page runs
its own inline script against a crewmate's fold values -- a chart is script or it is
nothing -- inside a frame that may navigate itself, so a page that renders is a page
trusted with the task titles and summaries it is handed. The only provenance that
carries a human review is a directory in this repo. A registry that also scanned a
writable directory would serve a page nobody here looked at, and whatever could write
that directory would choose what a crewmate's dashboard executes.

A template somebody writes themselves is a P2 follow-up, and it needs a wrapper
document minted by this gateway -- one that holds the authored markup without granting
it the frame's own navigation -- before any such page can render. See
``docs/request-for-change/rfc-crewmate-dynamic-dashboard.md``.

**A broken template is reported, not raised.** :func:`list_templates` returns the
entries it could load AND the problems it could not, because one unparsable directory
must not hide every good template behind it. A registry that raises on the worst member
of a set is a registry that stops working the first time somebody hand-edits a file.

**The directory name is the id.** The manifest carries an ``id`` too, and the two must
agree: a directory holding a manifest that names a different id is two names for one
template, and a lookup by either name would find or miss it depending on which name the
caller happened to hold.

**``source`` is checked against where the file actually is.** A template in this
directory may declare only ``builtin``. Without that check the field would be worth
nothing to a surface that reads it to mean "this shipped in the product".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kiro_crew.dashboard_templates.manifest import (
    ManifestError,
    TemplateManifest,
    load_template,
)

__all__ = [
    "BUILTIN_SOURCE",
    "CatalogEntry",
    "Catalog",
    "UnknownTemplate",
    "builtin_dir",
    "list_templates",
    "load_one",
]

logger = logging.getLogger(__name__)

BUILTIN_SOURCE: Final[str] = "builtin"


class UnknownTemplate(KeyError):
    """No template of that id, naming what there is instead."""


@dataclass(frozen=True)
class CatalogEntry:
    """One loadable template: its checked manifest, its page, and where it came from.

    ``origin`` is :data:`BUILTIN_SOURCE` -- WHERE the directory sits, which is a fact
    about the filesystem. ``manifest.source`` is what the file says about itself, and
    the two are checked equal at load; they are kept apart so a reader can tell the
    claim from the location, and so a second origin can be added without the claim
    becoming the only check.
    """

    manifest: TemplateManifest
    html: str
    directory: Path
    origin: str

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> int:
        return self.manifest.version

    def listing(self) -> dict[str, object]:
        """The row a chooser draws: identity and shape, never the page itself.

        The html is the large half and no chooser renders it, so a list of every
        template would otherwise carry every page. ``fields`` and ``folds`` are what
        a reader picks on -- what the template shows and what it needs -- and both
        come off the parsed manifest rather than being restated.
        """
        return {
            "id": self.manifest.id,
            "version": self.manifest.version,
            "title": self.manifest.title,
            "description": self.manifest.description,
            "source": self.manifest.source,
            "origin": self.origin,
            "fields": sorted(self.manifest.fields),
            "folds": sorted(self.manifest.folds),
            "agentic": sorted(n for n, f in self.manifest.fields.items() if f.agentic),
        }


@dataclass(frozen=True)
class Catalog:
    """What one scan found: the templates that load, and the directories that do not."""

    entries: tuple[CatalogEntry, ...]
    #: ``(directory name, why)`` per directory that could not be served. Reported
    #: rather than raised, so one hand-edited manifest cannot empty the registry.
    problems: tuple[tuple[str, str], ...]

    @property
    def by_id(self) -> dict[str, CatalogEntry]:
        return {entry.id: entry for entry in self.entries}


def builtin_dir() -> Path:
    """Where the templates that ship with the product live.

    Resolved from this module's own location so a wheel install and a source checkout
    answer the same way. The directory may be absent in a checkout that has none yet,
    which is an empty registry rather than an error -- the machinery ships before the
    templates do.
    """
    return Path(__file__).resolve().parent / "builtin"


def _scan(root: Path, origin: str) -> tuple[list[CatalogEntry], list[tuple[str, str]]]:
    """Load every template directory under *root*. Never raises for a bad member."""
    entries: list[CatalogEntry] = []
    problems: list[tuple[str, str]] = []
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        # An absent root is the ordinary empty state: the machinery ships before the
        # templates do. An unreadable one is a real fault, and it is reported rather
        # than raised for the same reason a bad member is.
        if root.exists():
            logger.warning("dashboard templates: cannot list %s", root, exc_info=True)
        return entries, problems
    for directory in children:
        name = directory.name
        try:
            manifest, html = load_template(directory)
        except ManifestError as exc:
            problems.append((name, str(exc)))
            continue
        if manifest.id != name:
            problems.append(
                (name, f"manifest id {manifest.id!r} does not match its directory name")
            )
            continue
        if manifest.source != origin:
            problems.append(
                (
                    name,
                    f"source {manifest.source!r} is not {origin!r}, which is where "
                    "this directory actually is",
                )
            )
            continue
        entries.append(CatalogEntry(manifest, html, directory, origin))
    return entries, problems


def list_templates() -> Catalog:
    """Every template this gateway can offer, plus every directory that would not load."""
    entries, problems = _scan(builtin_dir(), BUILTIN_SOURCE)
    return Catalog(tuple(entries), tuple(problems))


def load_one(template_id: str) -> CatalogEntry:
    """The template *template_id* names, or :class:`UnknownTemplate`.

    Goes through :func:`list_templates` rather than opening the directory the id spells.
    That costs a scan and buys two things: a directory whose manifest fails any of the
    scan's checks is unknown here rather than served by a path that skipped them, and
    "unknown" can say what IS known.
    """
    catalog = list_templates()
    try:
        return catalog.by_id[template_id]
    except KeyError:
        known = sorted(catalog.by_id)
        raise UnknownTemplate(
            f"no dashboard template {template_id!r}; available: {known}"
        ) from None
