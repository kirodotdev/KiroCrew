"""The template registry: every dashboard template this gateway can offer.

**Two roots, one loader.** Templates ship with the product under ``builtin/<id>/``. An
operator may also place their own under ``<data home>/panel-templates/<id>/``. Both are
directories in the format :mod:`kiro_crew.dashboard_templates.manifest` describes, read
by :func:`~kiro_crew.dashboard_templates.manifest.load_template`. There is exactly one
place a malformed template is refused.

**The registry serves only pages a person put there.** A dashboard page runs its own
inline script against a crewmate's fold values -- a chart is script or it is nothing --
inside a frame that may navigate itself, so a page that renders is a page trusted with
the task titles and summaries it is handed. So the registry scans no directory an agent
can write. The built-in directory went through review. The operator folder is the one
the panel's own template overrides use, and every agent write path is refused on it:
the sandbox seals it read-only, the file tools refuse a write under it, and a linked
folder name is refused. An operator page is ``user``; a built-in id always wins over it,
and a linked page directory or file is refused rather than followed. See
``docs/request-for-change/rfc-crewmate-dynamic-dashboard.md``.

A page an agent writes, or one imported from a sender, renders from neither root.

**A broken template is reported, not raised.** :func:`list_templates` returns the
entries it could load AND the problems it could not, because one unparsable directory
must not hide every good template behind it. A registry that raises on the worst member
of a set is a registry that stops working the first time somebody hand-edits a file.

**The directory name is the id.** The manifest carries an ``id`` too, and the two must
agree: a directory holding a manifest that names a different id is two names for one
template, and a lookup by either name would find or miss it depending on which name the
caller happened to hold.

**``source`` is checked against where the file actually is.** A template in the
built-in directory may declare only ``builtin``, and one in the operator folder only
``user``. Without that check the field would be worth nothing to a surface that reads it
to mean "this shipped in the product".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kiro_crew import platform_compat
from kiro_crew.dashboard_templates.manifest import (
    ManifestError,
    TemplateManifest,
    load_template,
)

__all__ = [
    "BUILTIN_SOURCE",
    "PAGE_SINKS",
    "USER_SOURCE",
    "CatalogEntry",
    "Catalog",
    "UnknownTemplate",
    "builtin_dir",
    "list_templates",
    "load_one",
    "page_sink",
]

logger = logging.getLogger(__name__)

BUILTIN_SOURCE: Final[str] = "builtin"
#: A page the operator placed in the data home's ``panel-templates`` folder.
USER_SOURCE: Final[str] = "user"


class UnknownTemplate(KeyError):
    """No template of that id, naming what there is instead."""


@dataclass(frozen=True)
class CatalogEntry:
    """One loadable template: its checked manifest, its page, and where it came from.

    ``origin`` is :data:`BUILTIN_SOURCE` or :data:`USER_SOURCE` -- WHERE the directory
    sits, which is a fact about the filesystem. ``manifest.source`` is what the file
    says about itself, and the two are checked equal at load; they are kept apart so a
    reader can tell the claim from the location, so the claim is never the only check.
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


def _linked_part(directory: Path) -> str | None:
    """The first of *directory* and its two files that is a link, or ``None``.

    Checked with ``lstat`` semantics, so a link is seen rather than followed. The
    folder's fences attach to these NAMES; a link would hand the bytes to whatever
    can write its target.
    """
    for path in (directory, directory / "manifest.json", directory / "template.html"):
        if platform_compat.is_link_or_junction(path):
            return path.name
    return None


#: Every call by which a page can turn a value into markup or code. ONE list, read by
#: the operator-page check below and by ``test_dashboard_template_sink_ban.py`` over
#: the shipped pages, so the two cannot drift. A text match, not a parser: it can
#: refuse a page that only names a call in a comment, which is the safe way to be
#: wrong. A page with a real need for one goes through review with this list changed.
PAGE_SINKS: Final[tuple[str, ...]] = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "document.writeln",
    "eval(",
    "Function(",
    "createContextualFragment",
    "DOMParser",
    "parseFromString",
    "setHTMLUnsafe",
    "parseHTMLUnsafe",
    "srcdoc",
)

#: ``setTimeout`` or ``setInterval`` handed a string, which runs it as code.
_STRING_TIMER_RE: Final[re.Pattern[str]] = re.compile(r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]")


def page_sink(text: str) -> str | None:
    """The first :data:`PAGE_SINKS` call (or string timer) in *text*, else ``None``."""
    for sink in PAGE_SINKS:
        if sink in text:
            return sink
    match = _STRING_TIMER_RE.search(text)
    return match.group(0) if match else None


def _scan(root: Path, origin: str) -> tuple[list[CatalogEntry], list[tuple[str, str]]]:
    """Load every template directory under *root*. Never raises for a bad member."""
    entries: list[CatalogEntry] = []
    problems: list[tuple[str, str]] = []
    try:
        # `is_dir` follows a link, so a linked page directory is still listed here
        # and then refused below by name, rather than silently skipped.
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
        if origin == USER_SOURCE:
            linked = _linked_part(directory)
            if linked is not None:
                problems.append(
                    (name, f"{linked} is a link; an operator page must be a real directory")
                )
                continue
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
        if origin == USER_SOURCE:
            # An operator page goes live on apply with no review here, so the
            # "values are text" premise is checked on it like a bad manifest is.
            sink = page_sink(html)
            if sink is not None:
                problems.append(
                    (name, f"template.html uses {sink!r}; write values with textContent")
                )
                continue
        entries.append(CatalogEntry(manifest, html, directory, origin))
    return entries, problems


def list_templates() -> Catalog:
    """Every template this gateway can offer, plus every directory that would not load.

    Built-in pages first, then the operator's, from the data home's ``panel-templates``
    folder. The panel's own overrides live there as ``<id>.html`` files; this scan reads
    only directories, so the two never collide. A built-in id always wins: an operator
    page with the same id is reported, never served in its place.

    ``agent_panel`` is imported at call time, as ``instance.lock_path`` does, because it
    pulls in more of the tree than this module needs at import.
    """
    from kiro_crew.agent_panel import TEMPLATES_DIRNAME, PanelError, override_templates_dir

    entries, problems = _scan(builtin_dir(), BUILTIN_SOURCE)
    try:
        root = override_templates_dir()
    except PanelError as exc:
        problems.append((TEMPLATES_DIRNAME, str(exc)))
        return Catalog(tuple(entries), tuple(problems))
    shipped = {entry.id for entry in entries}
    user_entries, user_problems = _scan(root, USER_SOURCE)
    problems.extend(user_problems)
    for entry in user_entries:
        if entry.id in shipped:
            problems.append((entry.id, "a shipped page has this id, so the shipped page is served"))
            continue
        entries.append(entry)
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
