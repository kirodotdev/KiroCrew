"""The template registry: every dashboard template this gateway can offer, from both places.

Two sources, one loader. Built-ins ship in the repo under ``builtin/<id>/``; user
templates live under the data home in ``dashboard-templates/<id>/``. Both are
directories in the format :mod:`kiro_crew.dashboard_templates.manifest` describes, and
both are read by :func:`~kiro_crew.dashboard_templates.manifest.load_template` -- so a
template that loads here loads the same way whoever wrote it, and there is one place
where a malformed one is refused.

**A broken template is reported, not raised.** :func:`list_templates` returns the
entries it could load AND the problems it could not, because one unparsable directory
must not hide every good template behind it. A registry that raises on the worst member
of a set is a registry that stops working the first time somebody hand-edits a file.

**The directory name is the id.** The manifest carries an ``id`` too, and the two must
agree: a directory holding a manifest that names a different id is two names for one
template, and a lookup by either name would find or miss it depending on which name the
caller happened to hold.

**``source`` is checked against where the file actually is.** A user directory may
declare ``user`` (written here) or ``shared`` (imported from elsewhere); the built-in
directory may declare only ``builtin``. Without that check a user template could claim
to be a built-in, and a surface that trusts ``source`` to mean "this shipped in the
product" would be reading an agent-written field.

**A built-in id wins.** A user template whose id collides with a built-in is not loaded
and the collision is reported as a problem, rather than shadowing the built-in silently:
a built-in is what the product's own screenshots and docs refer to, and a template that
quietly replaces one makes those wrong with nothing to read.
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
    "SHARED_SOURCE",
    "USER_SOURCE",
    "CatalogEntry",
    "Catalog",
    "UnknownTemplate",
    "builtin_dir",
    "list_templates",
    "load_one",
    "user_dir",
]

logger = logging.getLogger(__name__)

BUILTIN_SOURCE: Final[str] = "builtin"
USER_SOURCE: Final[str] = "user"
SHARED_SOURCE: Final[str] = "shared"

#: What a template directory found in each place may declare as its ``source``. A
#: user directory takes two values because an imported template keeps saying it came
#: from elsewhere (:mod:`kiro_crew.dashboard_templates.share`) while living in the
#: same directory as a locally written one.
_ALLOWED_SOURCES: Final[dict[str, frozenset[str]]] = {
    BUILTIN_SOURCE: frozenset({BUILTIN_SOURCE}),
    USER_SOURCE: frozenset({USER_SOURCE, SHARED_SOURCE}),
}

#: The user directory under the data home. One level, one directory per template id.
USER_SUBDIR: Final[str] = "dashboard-templates"


class UnknownTemplate(KeyError):
    """No template of that id, naming what there is instead."""


@dataclass(frozen=True)
class CatalogEntry:
    """One loadable template: its checked manifest, its page, and where it came from.

    ``origin`` is :data:`BUILTIN_SOURCE` or :data:`USER_SOURCE` -- WHERE the directory
    sits, which is a fact about the filesystem. ``manifest.source`` is what the file
    says about itself, and the two are checked equal-or-compatible at load; they are
    kept apart so a reader can tell the claim from the location.
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


def user_dir() -> Path:
    """Where templates the user adopted or imported live, under the data home.

    Read through ``config.paths.data_home`` on every call rather than cached: a test
    and an isolated gateway each set their own home, and a cached value would serve
    the first one to ask for the life of the process.
    """
    from kiro_crew.config.paths import data_home

    return data_home() / USER_SUBDIR


def _scan(root: Path, origin: str) -> tuple[list[CatalogEntry], list[tuple[str, str]]]:
    """Load every template directory under *root*. Never raises for a bad member."""
    entries: list[CatalogEntry] = []
    problems: list[tuple[str, str]] = []
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        # An absent root is the ordinary empty state (no built-ins yet, no user
        # templates yet). An unreadable one is a real fault, but it is still one
        # source of two and must not take the other down with it.
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
        allowed = _ALLOWED_SOURCES[origin]
        if manifest.source not in allowed:
            problems.append(
                (
                    name,
                    f"source {manifest.source!r} is not one of {sorted(allowed)} "
                    f"for a {origin} template",
                )
            )
            continue
        entries.append(CatalogEntry(manifest, html, directory, origin))
    return entries, problems


def list_templates() -> Catalog:
    """Every template this gateway can offer, built-ins first, plus what would not load."""
    entries, problems = _scan(builtin_dir(), BUILTIN_SOURCE)
    builtin_ids = {entry.id for entry in entries}
    user_entries, user_problems = _scan(user_dir(), USER_SOURCE)
    problems += user_problems
    for entry in user_entries:
        if entry.id in builtin_ids:
            # Named, not shadowed. See the module docstring: a user template that
            # replaces a built-in makes the product's own documentation wrong with
            # nothing anywhere to read about it.
            problems.append((entry.id, "a built-in template already uses this id"))
            continue
        entries.append(entry)
    return Catalog(tuple(entries), tuple(problems))


def load_one(template_id: str) -> CatalogEntry:
    """The template *template_id* names, or :class:`UnknownTemplate`.

    Goes through :func:`list_templates` rather than opening the directory the id spells.
    That costs a scan and buys the collision rule: a direct open would serve a user
    template whose id collides with a built-in, which is exactly the case the scan
    refuses. It is also what makes "unknown" able to say what IS known.
    """
    catalog = list_templates()
    try:
        return catalog.by_id[template_id]
    except KeyError:
        known = sorted(catalog.by_id)
        raise UnknownTemplate(
            f"no dashboard template {template_id!r}; available: {known}"
        ) from None
