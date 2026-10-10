"""One crewmate's own dashboard record, as a reader sees it.

A template is shared and versioned by whoever wrote it. An **instance** is one
crewmate's copy of one template, and the copy is the point: a built-in template that
ships a new version does not silently change what a crewmate's Dashboard tab shows.

**This module READS.** It decodes the record under the member's own space, derives
which of four states that copy is in, and reads back a staged page. Nothing here
writes: no adopt, no edit, no rollback, no staging and no history, so the only thing a
reader of this module has to trust is the decode.

**:func:`staged_preview` has no writer left**, which is why it is still here rather
than gone. Its one caller is the member dashboard's own ``?preview=1`` read, and that
caller is in a file this module does not own. The function answers ``None`` for a
preview that was never staged, so with nothing staging one it answers ``None`` always
-- unreachable rather than absent, and removed with its caller in one step.

**Two versions never mix**, which is why each is spelled out and neither is called "the"
version:

``template.version``
    The version of the template that was copied.
``instance_version``
    This copy's own counter.

A third number, the crew-log position the rendered VALUES were read at, belongs to the
data channel and this module neither reads nor stores it.

**The state is derived, never stored.** A stored state would be a claim about the
registry made when the record was last written, and the registry moves on its own: a
built-in template shipping a new version makes every copy of it stale without touching
one record. :data:`STATE_ERROR` is a record that was damaged or hand-edited, which is a
real state a reader must be able to see.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from kiro_crew.config.paths import data_home
from kiro_crew.dashboard_templates import catalog
from kiro_crew.dashboard_templates.manifest import (
    ManifestError,
    check_parity,
    parse_manifest,
)

__all__ = [
    "DEFAULT_TEMPLATE_ID",
    "MAX_PREVIEW_AGE_MS",
    "RENDERABLE_SOURCES",
    "SCHEMA_VERSION",
    "STATE_EMPTY",
    "STATE_ERROR",
    "STATE_LIVE",
    "STATE_STALE",
    "Instance",
    "InstanceError",
    "InstanceRefused",
    "Preview",
    "default_instance",
    "instance_dir",
    "preview_url",
    "read",
    "staged_preview",
]

logger = logging.getLogger(__name__)

#: The shape of what :func:`read` returns and the record holds. A record written under a
#: different number is not interpreted: the fields it carries may mean something else,
#: and guessing is how one damaged record becomes a wrong page.
SCHEMA_VERSION: Final[int] = 1

STATE_EMPTY: Final[str] = "empty"
STATE_LIVE: Final[str] = "live"
STATE_STALE: Final[str] = "stale"
STATE_ERROR: Final[str] = "error"

#: What a crewmate that adopted nothing renders, resolved from the registry on demand
#: and never written to disk. An empty frame answers none of the questions a person
#: opened the tab with, so the default is served instead of nothing -- and a registry
#: that serves no such id answers ``None``, which is the ordinary state of a build that
#: ships the loader without templates.
DEFAULT_TEMPLATE_ID: Final[str] = "project-report"

_RECORD_FILE: Final[str] = "instance.json"

#: Template provenances whose page may become a crewmate's live dashboard. One value.
#:
#: A rendered page runs its own inline script against the crewmate's fold values -- a
#: chart is script or it is nothing -- inside a frame that can navigate itself. So a
#: page that executes is a page trusted with every task title and summary it is handed,
#: and it can carry those somewhere by navigating. Provenance is the only signal that
#: says whether anyone here ever looked at the page, and ``builtin`` -- a directory in
#: this repo, which went through review -- is the only provenance that carries one.
#:
#: A page written by an agent, or imported from a sender, is therefore not renderable.
#: Making one renderable needs a wrapper document minted by this gateway, which holds
#: the authored markup while withholding the frame's own navigation.
RENDERABLE_SOURCES: Final[frozenset[str]] = frozenset({catalog.BUILTIN_SOURCE})


class InstanceError(Exception):
    """A dashboard instance record could not be read."""


class InstanceRefused(InstanceError):
    """A request against an instance was refused, with the reason a user can be told.

    Separate from :class:`InstanceError` because the two are different answers for a
    client: a refusal is the caller's input and the same request will be refused again,
    while an error is this gateway's state and a retry may work. The member dashboard's
    own ``_refusal`` maps them to 409 and 500 on that distinction.
    """


@dataclass(frozen=True)
class Instance:
    """One crewmate's dashboard as a reader sees it.

    ``manifest`` is the COPIED manifest, decoded. It is the raw mapping rather than a
    :class:`~kiro_crew.dashboard_templates.manifest.TemplateManifest` because a record in
    :data:`STATE_ERROR` is one whose manifest does NOT parse -- a typed field there would
    have to be absent exactly when a reader most needs to see what is in the file.
    """

    slug: str
    instance_version: int
    template_id: str
    template_version: int
    html: str
    manifest: Mapping[str, Any]
    state: str
    #: One sentence saying why the state is what it is, for a surface that must explain
    #: an empty or broken dashboard instead of rendering a blank frame.
    state_reason: str
    updated_ms: int

    def wire(self) -> dict[str, Any]:
        """The response body ``GET /api/members/{slug}/dashboard`` returns.

        Exactly the shape CONTRACT-v3 fixes between this module and the frame, plus
        ``state_reason``: the four states are what the frame BRANCHES on, and the
        sentence is what it can show a person. Nothing is added beyond that -- the
        frame reads values over the data channel, not from here.
        """
        return {
            "instance_version": self.instance_version,
            "template": {"id": self.template_id, "version": self.template_version},
            "html": self.html,
            "manifest": dict(self.manifest),
            "state": self.state,
            "state_reason": self.state_reason,
        }


def instance_dir(slug: str) -> Path:
    """Where one crewmate's dashboard record lives.

    Under the member's own space, beside everything else keyed by that slug, so a
    crewmate removed from the roster takes its dashboard with it.
    """
    if not slug:
        raise InstanceError("a dashboard instance needs a member slug")
    return data_home() / "members" / slug / "dashboard"


def _empty(slug: str, reason: str) -> Instance:
    return Instance(
        slug=slug,
        instance_version=0,
        template_id="",
        template_version=0,
        html="",
        manifest={},
        state=STATE_EMPTY,
        state_reason=reason,
        updated_ms=0,
    )


def default_instance(slug: str) -> Instance | None:
    """:data:`DEFAULT_TEMPLATE_ID` as an UNADOPTED instance, or ``None`` if it is absent.

    What a crewmate renders before it adopts anything. Deliberately not part of
    :func:`read`, which answers for the STORED record and nothing else: a fresh read
    saying "no template, version 0" is a fact other surfaces need, and folding the
    default in there would make that answer depend on what happens to be in the
    registry.

    So the default is resolved by the surface that serves a PAGE to a reader, and it is
    marked for what it is: ``instance_version`` stays 0 and the state stays
    :data:`STATE_EMPTY`, because nothing was adopted and nothing was written to disk.
    """
    try:
        entry = catalog.load_one(DEFAULT_TEMPLATE_ID)
    except catalog.UnknownTemplate:
        # A build whose registry serves no such id. The caller's own empty state is the
        # right answer here, so this is reported and not raised.
        logger.warning(
            "dashboard: the default template %r is not in the registry", DEFAULT_TEMPLATE_ID
        )
        return None
    return Instance(
        slug=slug,
        instance_version=0,
        template_id=entry.id,
        template_version=entry.version,
        html=entry.html,
        manifest=_manifest_json(entry),
        state=STATE_EMPTY,
        state_reason=f"no template adopted; showing the default {entry.id!r}",
        updated_ms=0,
    )


def _manifest_json(entry: catalog.CatalogEntry) -> dict[str, Any]:
    """The template's manifest as it sits on disk, or an empty mapping.

    Read back from the file rather than rebuilt from the parsed object. The parsed form
    is lossy by design -- it keeps what the loader checks -- and a reader wants what the
    directory actually holds, so a field the loader ignores still travels with it.

    ``load_one`` decoded this file already, so a failure here means it changed between
    the two reads. That is reported and not raised: the caller is serving a default
    page, and an empty manifest is the state its own reader already handles.
    """
    try:
        raw = json.loads((entry.directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("dashboard: template %r cannot be read", entry.id, exc_info=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_record(slug: str) -> dict[str, Any] | None:
    """The stored record, or ``None`` when there is none to read.

    An absent file is the ordinary state of a crewmate that adopted no template, so it
    is not an error. A file that is not a JSON object under this schema IS an error and
    is reported as one by the caller -- read as :data:`STATE_ERROR` rather than as
    "never adopted", because the two need opposite answers from a human.
    """
    path = instance_dir(slug) / _RECORD_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstanceError(f"dashboard instance for {slug!r} cannot be read: {exc}") from None
    if not isinstance(raw, dict):
        raise InstanceError(f"dashboard instance for {slug!r} is not an object")
    return raw


def _state_of(template_id: str, copied_version: int, manifest: Any, html: str) -> tuple[str, str]:
    """Which of the four states this stored copy is in, and the sentence for it.

    Asked in the order a reader needs: a copy that cannot be parsed cannot be rendered
    whatever the registry says about its template, so :data:`STATE_ERROR` is decided
    first and the registry is not consulted for it.
    """
    try:
        parsed = parse_manifest(manifest)
        check_parity(parsed, html)
    except ManifestError as exc:
        return STATE_ERROR, f"the stored copy no longer loads: {exc}"
    try:
        current = catalog.load_one(template_id)
    except catalog.UnknownTemplate:
        # The copy is complete and still renders -- nothing is missing from it -- but it
        # cannot be compared against or refreshed from its source, which is
        # precisely what the frame's stale band exists to say.
        return STATE_STALE, f"template {template_id!r} is not in the registry"
    if current.version > copied_version:
        return (
            STATE_STALE,
            f"template {template_id!r} is now version {current.version}; "
            f"this copy is version {copied_version}",
        )
    return STATE_LIVE, f"copied from {template_id!r} version {copied_version}"


def read(slug: str) -> Instance:
    """One crewmate's dashboard instance, with its state derived at read time.

    Costs the record plus one registry scan -- the fields, never the log.
    """
    raw = _read_record(slug)
    if raw is None:
        return _empty(slug, "no template adopted")
    if raw.get("schema") != SCHEMA_VERSION:
        return Instance(
            slug=slug,
            instance_version=0,
            template_id="",
            template_version=0,
            html="",
            manifest={},
            state=STATE_ERROR,
            state_reason=(
                f"the record is schema {raw.get('schema')!r}, and this gateway reads "
                f"schema {SCHEMA_VERSION}"
            ),
            updated_ms=0,
        )
    template = raw.get("template")
    template = template if isinstance(template, dict) else {}
    template_id = str(template.get("id") or "")
    template_version = template.get("version")
    template_version = template_version if isinstance(template_version, int) else 0
    html = raw.get("html")
    html = html if isinstance(html, str) else ""
    manifest = raw.get("manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    version = raw.get("instance_version")
    version = version if isinstance(version, int) and version > 0 else 0
    if not version or not template_id:
        return Instance(
            slug=slug,
            instance_version=version,
            template_id=template_id,
            template_version=template_version,
            html=html,
            manifest=manifest,
            state=STATE_ERROR,
            state_reason="the record names no template or no instance version",
            updated_ms=int(raw.get("updated_ms") or 0),
        )
    state, reason = _state_of(template_id, template_version, manifest, html)
    return Instance(
        slug=slug,
        instance_version=version,
        template_id=template_id,
        template_version=template_version,
        html=html,
        manifest=manifest,
        state=state,
        state_reason=reason,
        updated_ms=int(raw.get("updated_ms") or 0),
    )


# --------------------------------------------------------------------------
# the staged preview, read only
# --------------------------------------------------------------------------


#: The staged page, beside the record and never inside it.
#:
#: A preview is a page somebody is being SHOWN before anything is recorded, so it
#: cannot be a version: a version is what a rollback can reach, and staging one would
#: put a page the person then declined into their history as a change they made. It
#: also means a preview never moves ``instance_version``, so a client that caches by
#: version is not invalidated by a page that was only looked at.
_PREVIEW_FILE: Final[str] = "preview.json"

#: How long a staged preview stays applicable.
#:
#: Staging and applying are two agent cycles with a PERSON between them, so the window
#: is minutes rather than seconds. Past it the preview is dropped rather than served:
#: "yes" to a page somebody looked at an hour ago is a yes to a page whose fold values
#: have since moved, and serving it would show what they saw rather than what is.
MAX_PREVIEW_AGE_MS: Final[int] = 30 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _preview_path(slug: str) -> Path:
    return instance_dir(slug) / _PREVIEW_FILE


def preview_url(slug: str) -> str:
    """The link a person opens to SEE the staged page.

    The ordinary dashboard read with ``preview=1``, not a route of its own. That route
    is already owner-only and a staged page carries the same crewmate's fold values as
    the live one, so a second route would be a second place to get that gate right. A
    capability token would be a second credential for data its holder can already read.
    """
    return f"/api/members/{slug}/dashboard?preview=1"


@dataclass(frozen=True)
class Preview:
    """One page staged for a look: which template it is, and when it was staged."""

    slug: str
    #: The catalog template this page came from. Always a real id: the only thing that
    #: can be staged is a template the catalog serves.
    template_id: str
    template_version: int
    html: str
    manifest: Mapping[str, Any]
    staged_ms: int

    def wire(self) -> dict[str, Any]:
        """What a caller is told about the staged page. NEVER the page itself.

        The html is the large half, no caller renders it, and the person who has to
        decide reads it in the frame through :func:`preview_url`. So this carries
        identity and shape, which is the same cut a catalog listing makes.
        """
        declared = self.manifest.get("fields")
        return {
            "template_id": self.template_id,
            "template_version": self.template_version,
            "title": str(self.manifest.get("title") or ""),
            "fields": sorted(declared) if isinstance(declared, dict) else [],
            "html_bytes": len(self.html.encode("utf-8")),
            "staged_ms": self.staged_ms,
            "preview_url": preview_url(self.slug),
        }


def staged_preview(slug: str) -> Preview | None:
    """The page staged for *slug*, or ``None`` when there is none to show.

    ``None`` covers five cases that are one case to every caller: nothing was staged,
    the file will not decode, it was written under another schema, it names no
    template, and it is older than :data:`MAX_PREVIEW_AGE_MS`. Each means there is no
    page this gateway will show, and the answer a caller can act on is the same one.

    Nothing in this module stages a page, so today the first case is the only one that
    can arise. The function is kept because its caller is, and the caller lives in a
    file this module does not own.
    """
    try:
        raw = json.loads(_preview_path(slug).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
        return None
    html = raw.get("html")
    manifest = raw.get("manifest")
    if not isinstance(html, str) or not isinstance(manifest, dict):
        return None
    staged_ms = int(raw.get("staged_ms") or 0)
    if staged_ms <= 0 or _now_ms() - staged_ms > MAX_PREVIEW_AGE_MS:
        return None
    template = raw.get("template")
    template = template if isinstance(template, dict) else {}
    template_id = str(template.get("id") or "")
    if not template_id:
        # A preview file that names no template. There is nothing this gateway will
        # show from it, and "stage it again" is the same answer the four cases above
        # get.
        return None
    return Preview(
        slug=slug,
        template_id=template_id,
        template_version=int(template.get("version") or 0),
        html=html,
        manifest=manifest,
        staged_ms=staged_ms,
    )
