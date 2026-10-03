"""Share one template as one file: export it, import it back somewhere else.

A template is a DIRECTORY of two files, which is right on disk and wrong to send: a
person pasting a template into a chat, attaching it to a ticket or committing it beside
a design doc has one thing to carry, and two files that must stay together is a pair
that arrives separated. So the share format is one JSON document holding both halves.

**It is validated on the way in, not on the way out.** An export writes what the
registry already loaded, so it is valid by construction. An import is the untrusted
direction -- the file came from somewhere this gateway does not control -- and it is
parsed and parity-checked BEFORE anything is written, so a malformed share never becomes
a directory the registry then has to report as a problem.

**An import does not overwrite.** A file whose id collides with a template already here
is refused and names the collision; the caller renames it with ``as_id`` or deletes the
existing one. Overwriting silently is how a shared file replaces the template a
crewmate's dashboard was copied from, and because an instance holds its own COPY the
dashboard would keep rendering while the registry did not hold what it was made from.

**An imported template declares ``shared``.** It lives in the user directory and is
editable like a user template, but a reader asking where it came from gets an answer
other than "somebody here wrote it". The registry enforces that this is a legal claim
for that directory, so nothing has to trust the field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.dashboard_templates import catalog
from kiro_crew.dashboard_templates.manifest import (
    ManifestError,
    check_parity,
    parse_manifest,
)

__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "MAX_SHARE_BYTES",
    "ShareRefused",
    "export_template",
    "import_template",
]

#: The document's self-identification. A reader that finds another value stops rather
#: than guessing: the keys below may mean something else in another format, and a
#: template assembled out of a misread file is a page nobody wrote.
FORMAT: Final[str] = "kirocrew.dashboard.template"
FORMAT_VERSION: Final[int] = 1

#: The whole share document's ceiling. Bounds what one import can be asked to parse,
#: and sits above the instance page ceiling so a template that can be adopted can
#: always be shared.
MAX_SHARE_BYTES: Final[int] = 256 * 1024


class ShareRefused(ValueError):
    """An export or import was refused, with the reason a user can be told."""


def export_template(template_id: str) -> str:
    """The share document for *template_id*, as text.

    Works for a built-in as well as a user template: a built-in is the usual thing
    somebody wants to send, and the copy they receive declares ``shared`` rather than
    ``builtin`` so their registry does not claim the product shipped it.

    An unknown id raises :class:`~kiro_crew.dashboard_templates.catalog.UnknownTemplate`
    unwrapped. Wrapping it in :class:`ShareRefused` would give one fact -- no template
    of that name -- two refusal codes depending on which surface asked, so a caller
    could not treat "not found" as one case.
    """
    entry = catalog.load_one(template_id)
    raw = json.loads((entry.directory / "manifest.json").read_text(encoding="utf-8"))
    document = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "manifest": raw,
        "html": entry.html,
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)


def _decode(text: str) -> tuple[dict[str, Any], str]:
    """The manifest and page a share document carries, or :class:`ShareRefused`."""
    size = len(text.encode("utf-8"))
    if size > MAX_SHARE_BYTES:
        raise ShareRefused(f"the file is {size} bytes, over the {MAX_SHARE_BYTES}-byte ceiling")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ShareRefused(f"not a JSON document: {exc}") from None
    if not isinstance(document, dict):
        raise ShareRefused("a shared template must be a JSON object")
    if document.get("format") != FORMAT:
        raise ShareRefused(f"format {document.get('format')!r} is not {FORMAT!r}")
    if document.get("format_version") != FORMAT_VERSION:
        raise ShareRefused(
            f"format version {document.get('format_version')!r} is not {FORMAT_VERSION}"
        )
    manifest, html = document.get("manifest"), document.get("html")
    if not isinstance(manifest, dict):
        raise ShareRefused("the document carries no manifest object")
    if not isinstance(html, str) or not html.strip():
        raise ShareRefused("the document carries no page")
    return manifest, html


def import_template(text: str, *, as_id: str = "") -> catalog.CatalogEntry:
    """Write a share document into the user directory and return what the registry now has.

    *as_id* renames the template on the way in, which is the answer to a collision and
    the reason a refusal does not mean the file is unusable.

    The return value comes from a FRESH registry scan rather than from the bytes just
    written. That is what proves the import is actually serveable: the same loader, the
    same directory-name rule and the same source rule the registry applies to everything
    else, run against the files on disk instead of against the caller's intent.
    """
    manifest, html = _decode(text)
    manifest = dict(manifest)
    if as_id:
        manifest["id"] = as_id
    # An imported template is ``shared`` wherever it came from. Rewritten rather than
    # required, so a built-in somebody exported arrives usable instead of being refused
    # for making a claim that is true where it was written and false here.
    manifest["source"] = catalog.SHARED_SOURCE
    try:
        parsed = parse_manifest(manifest)
        check_parity(parsed, html)
    except ManifestError as exc:
        raise ShareRefused(f"this shared template does not load: {exc}") from None
    existing = catalog.list_templates().by_id
    if parsed.id in existing:
        where = existing[parsed.id].origin
        raise ShareRefused(
            f"a {where} template already uses the id {parsed.id!r}; "
            "import it under another id to keep both"
        )
    directory: Path = catalog.user_dir() / parsed.id
    directory.mkdir(parents=True, exist_ok=True)
    # The page first, then the manifest. A crash between them leaves a directory the
    # registry reports as a problem either way, but this order never leaves a manifest
    # promising fields no page binds -- which is the one state that could be mistaken
    # for a template whose author simply had not finished the layout.
    atomic_write(directory / "template.html", html, fsync=True)
    atomic_write(
        directory / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        fsync=True,
    )
    fsync_dir(directory)
    try:
        return catalog.load_one(parsed.id)
    except catalog.UnknownTemplate as exc:  # pragma: no cover - written and then not found
        raise ShareRefused(f"the import landed but the registry will not serve it: {exc}") from None
