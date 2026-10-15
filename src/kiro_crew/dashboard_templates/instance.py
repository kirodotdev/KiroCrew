"""One crewmate's own dashboard: the template it COPIED, its own edits, and its history.

A template is shared and versioned by whoever wrote it. An **instance** is one
crewmate's copy of one template, and the copy is the point: a built-in template that
ships a new version does not silently change what a crewmate's Dashboard tab shows,
and a crewmate that edits its page is editing its own copy rather than everybody's
template.

**Three versions never mix**, which is why each is spelled out and none is called "the"
version:

``template.version``
    The version of the template that was copied. Frozen at adopt; an edit never moves
    it, because an edit does not make the crewmate the author of the template.
``instance_version``
    This copy's own counter. Every accepted change is ``+1``, and it never goes
    backwards -- see :func:`rollback`.
data seq
    The crew-log position the rendered VALUES were read at. It belongs to the data
    channel, not here, and this module neither reads nor stores it.

**The record is a file; the history is a fold.** ``instance.json`` under the member's
own space is the current value, and every accepted change also appends one
``dashboard/instance_changed`` entry to the member's DM session crew log. The entry
carries what changed, never the page -- so it is bounded by construction and can never
be refused for size -- and the page itself is kept per version in ``versions/<n>.json``,
which is what makes a rollback a read rather than a guess. :func:`history` folds those
entries from a SAVEPOINT beside the record: one fold step per entry at write time, and a
read that costs the retained rows rather than the length of the log.

**A rollback moves forward.** Rolling an instance at version 2 back to version 1 writes
version 1's payload as version **3**. Rewinding the counter instead would make the
instance version ambiguous -- two different pages would both have been "version 2" -- and
a client that caches by version would keep serving the page it was told was replaced.

**An edit that breaks the format is refused, not stored.** Every write runs the same
:func:`~kiro_crew.dashboard_templates.manifest.parse_manifest` and
``check_parity`` the registry runs, so a page binding a field its manifest does not
declare never becomes a version. :data:`STATE_ERROR` is therefore about a record that
was damaged or hand-edited after it landed, which is a real state a reader must be able
to see, not a shape this module will write.
"""

from __future__ import annotations

import contextvars
import dataclasses
import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterable, Iterator, Mapping
from urllib.parse import quote, urlencode

from kiro_crew import dashboard_frame, pinned_fs
from kiro_crew.atomic_write import (
    atomic_write,
    fsync_dir,
    fsync_open_dir,
    pinned_parent_replace_supported,
    read_bytes_with_retry,
    refuse_linked_parent,
)
from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.entry_types import (
    DASHBOARD_INSTANCE_ACTIONS,
    DASHBOARD_INSTANCE_ENTRY_TYPE,
)
from kiro_crew.crew_log.store import log_exception_text
from kiro_crew.dashboard_templates import catalog
from kiro_crew.dashboard_templates.manifest import (
    ManifestError,
    check_parity,
    parse_manifest,
)
from kiro_crew.owner_only_files import mkdirs_owner_only
from kiro_crew.platform_compat import open_create_or_existing, release_lock, try_acquire_lock

__all__ = [
    "ACTIONS",
    "AUTHORED_PAGE_REFUSAL",
    "LOCKED_REFUSAL",
    "DEFAULT_TEMPLATE_ID",
    "MAX_HISTORY_ROWS",
    "MAX_INSTANCE_HTML_BYTES",
    "MAX_PREVIEW_AGE_MS",
    "MAX_RETAINED_VERSIONS",
    "PREVIEW_PAGE_PARAM",
    "PREVIEW_PAGE_VALUE",
    "RENDERABLE_SOURCES",
    "SCHEMA_VERSION",
    "SESSION_KEY_PREFIX",
    "STATE_EMPTY",
    "STATE_ERROR",
    "STATE_LIVE",
    "STATE_STALE",
    "Instance",
    "InstanceError",
    "InstanceLocked",
    "InstanceRefused",
    "NothingStaged",
    "PACKAGE_TEMPLATE_ID",
    "Preview",
    "PreviewChanged",
    "adopt",
    "apply_package",
    "apply_preview",
    "default_instance",
    "default_template_id",
    "discard_preview",
    "discard_shown_preview",
    "edit",
    "history",
    "instance_dir",
    "is_session_key",
    "PinnedPackage",
    "keeps_older_package_rule",
    "pinned_package",
    "preview_url",
    "read",
    "rollback",
    "session_instance_key",
    "session_preview_url",
    "set_structure_lock",
    "stage_preview",
    "staged_preview",
    "versions",
]

logger = logging.getLogger(__name__)

#: The shape of what :func:`read` returns and ``instance.json`` holds. A record written
#: under a different number is not interpreted: the fields it carries may mean something
#: else, and guessing is how one damaged record becomes a wrong page.
SCHEMA_VERSION: Final[int] = 1

STATE_EMPTY: Final[str] = "empty"
STATE_LIVE: Final[str] = "live"
STATE_STALE: Final[str] = "stale"
STATE_ERROR: Final[str] = "error"

#: What a crewmate that never adopted a template renders. "No dashboard yet" is the
#: first state of EVERY crewmate, and an empty frame answers none of the questions a
#: person opened the tab with, so the default is served instead of nothing -- read from
#: the registry on demand, never written to disk, so adopting nothing stays adopting
#: nothing and a later version of this template reaches the reader without a migration.
DEFAULT_TEMPLATE_ID: Final[str] = "project-report"

#: What one accepted change can be. Closed, and clamped at every writer here, so the
#: entry type's declaration may enforce it. Imported from the crew log's registry rather
#: than restated: the declared enum and the writer's set are one tuple, so a fourth
#: action cannot be written here and refused there.
ACTIONS: Final[tuple[str, ...]] = DASHBOARD_INSTANCE_ACTIONS

#: The page's ceiling: the FRAME's, not a second number.
#:
#: One page goes through two size checks -- this one before it becomes a version, and
#: ``dashboard_frame``'s before it is composed into the document that renders it. Two
#: independent numbers meant the store could refuse a page the frame would happily
#: draw, and it did: ``project-report`` is 147,345 bytes and ships as
#: :data:`DEFAULT_TEMPLATE_ID`, so a crewmate that adopted anything else could never
#: come back to the page every crewmate starts on. A default that only a crewmate who
#: never left can see is not a default.
#:
#: So the frame's ceiling IS this ceiling, read from the one constant that states it.
#: The frame is where the cost is actually paid -- it assembles the document the
#: browser parses -- which makes it the honest place for the number, and a shared
#: constant cannot drift back into two.
#:
#: It bounds what one crewmate stores per version, multiplied by
#: :data:`MAX_RETAINED_VERSIONS`.
MAX_INSTANCE_HTML_BYTES: Final[int] = dashboard_frame.MAX_PAGE_BYTES

#: Past versions kept on disk, newest wins. A rollback can only reach a retained one,
#: and it says so rather than silently rolling to the oldest it still has.
MAX_RETAINED_VERSIONS: Final[int] = 20

#: History rows the fold retains, newest last. A history, not an archive: each row is
#: what changed, so an operator can see a crewmate editing its dashboard without the
#: fold holding every page it ever had.
MAX_HISTORY_ROWS: Final[int] = 50

#: The crew-log entry every accepted change appends. Imported rather than restated for
#: the reason the entry-type registry gives beside it: the fold in
#: :func:`_fold_history_from_log` matches on this value, and a type the fold does not
#: match drops a real change with nothing raised.
ENTRY_TYPE: Final[str] = DASHBOARD_INSTANCE_ENTRY_TYPE

#: The prefix of a ROOT session's store key. See :func:`session_instance_key`.
SESSION_KEY_PREFIX: Final[str] = "session:"
_SESSION_DIGEST_LEN: Final[int] = 32
_SESSION_DIGEST_RE: Final[re.Pattern[str]] = re.compile(rf"[0-9a-f]{{{_SESSION_DIGEST_LEN}}}")
_SESSION_DASHBOARDS_DIR: Final[str] = "session-dashboards"

_RECORD_FILE: Final[str] = "instance.json"
_HISTORY_FILE: Final[str] = "history.json"
_VERSIONS_SUBDIR: Final[str] = "versions"
_LOCK_FILE: Final[str] = ".lock"
_LOCK_TIMEOUT_SECS: Final[float] = 5.0
_LOCK_POLL_SECS: Final[float] = 0.02
#: How long a write waits for its history entry to reach the log. One constant, as the
#: contract requires: every wait here is this wait.
_APPEND_FLUSH_SECONDS: Final[float] = 5.0


class InstanceError(Exception):
    """A dashboard instance could not be read or written."""


class InstanceRefused(InstanceError):
    """A write was refused, with the reason a user can be told."""


class InstanceLocked(InstanceRefused):
    """A structure change was refused because the person locked this dashboard."""


class NothingStaged(InstanceRefused):
    """An apply found no staged page: it was kept, discarded or expired."""


class PreviewChanged(InstanceRefused):
    """An apply named a staged page that has since been replaced by another."""


#: The ONE sentence the agent hears about the lock: as a structure change's refusal
#: and on every turn in its ``[DASHBOARD]`` block. Only the person can clear the lock,
#: from the crewmate's Profile card, so the one useful next step is to ask them.
LOCKED_REFUSAL: Final[str] = (
    "LOCKED by the person: the page layout is frozen. Do not apply, roll back "
    "or propose a different page; if they want one, ask them to unlock it on your "
    "Profile card. You can still preview a page for them to look at. "
    "Your dashboard_write values and the folded numbers still update."
)


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
    #: The person's structure lock, read from :func:`lock_path`. While set,
    #: :func:`_commit` refuses every new version, so adopt, apply, edit and rollback
    #: all answer :class:`InstanceLocked`.
    structure_locked: bool = False
    #: The crewmate's own ``kind="dashboard"`` artifact this version put in force, and
    #: the artifact version it pinned. Empty and 0 when the template copy is in force.
    package_slug: str = ""
    package_version: int = 0
    #: The digest naming the gateway-owned snapshot of that page (``crew-panels``).
    package_digest: str = ""
    #: True when this version names NO package on purpose -- a template apply or a
    #: rollback to a template version -- so a package bound to the crewmate does not
    #: draw over it. False on a record written before packages could be applied,
    #: which keeps that record's older reading (see :func:`keeps_older_package_rule`).
    package_released: bool = False

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
            "structure_locked": self.structure_locked,
        }


#: The ``template.id`` a history row carries for a version that put a package in
#: force. The same id :func:`member_dashboard_fields.package_manifest` gives the
#: package's translated manifest, so the history and the field list name one page.
PACKAGE_TEMPLATE_ID: Final[str] = "dashboard-package"


#: A package page snapshot's name inside ``crew-panels``: ``<slug>`` + this + a digest.
_PAGE_SNAPSHOT_INFIX: Final[str] = ".dashboard-page."
_PAGE_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")


def _package_field(raw: Mapping[str, Any]) -> tuple[str, int, str, bool]:
    """``(slug, version, digest, released)`` from a stored record's ``package`` key.

    Absent is a record written before packages could be applied: nothing pinned and
    nothing released. ``None`` is released. A pointer that is not well formed is read
    as released, so a hand-edited record falls back to its template copy rather than
    naming a page it cannot prove.
    """
    if "package" not in raw:
        return "", 0, "", False
    pointer = raw.get("package")
    if not isinstance(pointer, Mapping):
        return "", 0, "", True
    slug = pointer.get("slug")
    version = pointer.get("version")
    digest = pointer.get("digest")
    if (
        not isinstance(slug, str)
        or not slug
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or not isinstance(digest, str)
        or not _PAGE_DIGEST_RE.fullmatch(digest)
    ):
        return "", 0, "", True
    return slug, version, digest, False


def _page_snapshot_path(slug: str, digest: str) -> Path:
    """Where the gateway keeps one package page a crewmate put in force.

    In ``crew-panels`` beside the person's lock, NOT under ``members/<slug>/``: that
    tree is writable by the crewmate's own shell, and this file is the page that
    runs. The gateway is its only writer (:func:`apply_package`), so neither a later
    artifact write, an artifact deleted and saved again, nor the artifact store's
    version pruning changes the page a version pins.
    """
    from kiro_crew.agent_panel import panel_dir

    if is_session_key(slug):
        raise InstanceError("a session dashboard has no package page")
    if not _PAGE_DIGEST_RE.fullmatch(digest):
        raise InstanceError("a package page digest must be 32 hex characters")
    return panel_dir() / f"{slug}{_PAGE_SNAPSHOT_INFIX}{digest}.json"


def _page_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class PinnedPackage:
    """The package page a version put in force: its artifact, version and bytes."""

    slug: str
    version: int
    content: str


def pinned_package(record: Instance) -> PinnedPackage | None:
    """The package page *record* pins, read from the gateway's snapshot, or ``None``.

    ``None`` for a record that pins nothing and for a pin whose snapshot is missing,
    unreadable or does not hash to its digest -- the reader then serves the template
    copy rather than a page it cannot prove.

    The page read and the write check both ask this, so the page a person sees and
    the Model a ``dashboard_write`` is checked against are one package.
    """
    if not record.package_slug or not record.package_digest:
        return None
    try:
        content = read_bytes_with_retry(
            _page_snapshot_path(record.slug, record.package_digest)
        ).decode("utf-8")
    except (OSError, UnicodeDecodeError, InstanceError):
        logger.warning("dashboard: %r's pinned package page is unreadable", record.slug)
        return None
    if _page_digest(content) != record.package_digest:
        logger.warning("dashboard: %r's pinned package page does not match its pin", record.slug)
        return None
    return PinnedPackage(record.package_slug, record.package_version, content)


def keeps_older_package_rule(record: Instance) -> bool:
    """Whether a bound package is chosen by the rule from before packages were pinned.

    True only for a record that has no ``package`` key at all -- written before this
    change -- and is NOT locked. A locked older record keeps the template copy the
    person locked, for the page read and the write check alike.
    """
    return not record.package_slug and not record.package_released and not record.structure_locked


def _now_ms() -> int:
    return int(time.time() * 1000)


_STAGED_MS_LOCK = threading.Lock()
_last_staged_ms = 0


def _mint_staged_ms(*, after: int = 0) -> int:
    """A ``staged_ms`` past *after* and past every stamp this process handed out.

    The stamp is the person's Keep / Discard naming the page it was shown, so two
    stagings that read the same millisecond would look like one page and let a click
    act on a replacement nobody saw. Each stamp is therefore at least one past the
    last one handed out and past the page it replaces (*after*, which also covers a
    restart). It stays within a millisecond or two of the wall clock, so it still
    measures :data:`MAX_PREVIEW_AGE_MS`.
    """
    global _last_staged_ms
    with _STAGED_MS_LOCK:
        _last_staged_ms = max(_now_ms(), _last_staged_ms + 1, after + 1)
        return _last_staged_ms


def session_instance_key(slot_key: str) -> str:
    """The store key one ROOT session's dashboard instance is filed under.

    Every function in this module takes a store key where it says ``slug``: a member
    slug for a crewmate's page, or this key for a session's. The prefix carries a
    ``:``, which the member slug grammar refuses, so no crewmate's slug can ever name
    a session's directory or the other way round.

    A digest of the slot key rather than the key itself, because a slot key is not a
    path segment: channel slots carry dots, and nothing here should have to argue
    that a given key cannot climb out of the directory it names.
    """
    if not slot_key:
        raise InstanceError("a session dashboard instance needs a slot key")
    digest = hashlib.sha256(slot_key.encode("utf-8", "replace")).hexdigest()[:_SESSION_DIGEST_LEN]
    return f"{SESSION_KEY_PREFIX}{digest}"


def is_session_key(slug: str) -> bool:
    """Whether *slug* is a session's store key rather than a member slug."""
    return slug.startswith(SESSION_KEY_PREFIX)


def instance_dir(slug: str) -> Path:
    """Where one dashboard instance lives, for a crewmate or for a root session.

    A crewmate's is under the member's own space, beside everything else keyed by that
    slug, so a crewmate removed from the roster takes its dashboard with it. A session's
    is under one directory of session pages, named by the digest
    :func:`session_instance_key` made; a key whose digest is not exactly that shape is
    refused rather than joined onto a path.
    """
    if not slug:
        raise InstanceError("a dashboard instance needs a member slug")
    if is_session_key(slug):
        digest = slug[len(SESSION_KEY_PREFIX) :]
        if not _SESSION_DIGEST_RE.fullmatch(digest):
            raise InstanceError("a session dashboard key must come from session_instance_key")
        directory = data_home() / _SESSION_DASHBOARDS_DIR / digest
        _refuse_linked_session_dir(directory)
        return directory
    return data_home() / "members" / slug / "dashboard"


def _refuse_linked_session_dir(directory: Path) -> None:
    """Refuse a session page directory reached through a link or junction.

    A root session's agent runs in a sandbox that can create links inside the data
    home, while this module runs in the gateway, outside it. A link planted at
    ``session-dashboards/<digest>`` -- or at ``session-dashboards`` itself -- would
    have the gateway create, lock and write files wherever it points, including a
    folder the agent's own sandbox holds read-only.

    Two checks, before any directory is made. ``refuse_linked_parent`` walks every
    component below the data-home anchor with ``lstat`` (the only test that sees a
    Windows junction) and requires the resolved path to equal the one rebuilt from
    the anchor, so it covers the leaf directory as well as its parents. Then the
    resolved directory must still sit directly under the resolved session-pages
    root, which is what makes a link pointing elsewhere inside the data home fail
    too. The lstat walk is not race-free on its own; :func:`_locked` pins the
    directory with ``O_NOFOLLOW`` for the rest of the operation.
    """
    try:
        refuse_linked_parent(directory / _LOCK_FILE)
    except OSError as exc:
        raise InstanceError(
            "this session's dashboard directory is a link or sits behind one; refusing to "
            "write through it"
        ) from exc
    root = (data_home() / _SESSION_DASHBOARDS_DIR).resolve()
    resolved = directory.resolve()
    if resolved.parent != root or resolved.name != directory.name:
        raise InstanceError("this session's dashboard directory is outside the session pages")


def _refuse_linked_versions_dir(versions_dir: Path) -> None:
    """Refuse a session page's ``versions`` directory that is a link or junction.

    The guard where the platform cannot pin (Windows): a junction there could point at
    another dashboard's retained versions, and a commit through it would overwrite the
    payload that dashboard's rollback restores. The leaf checked is inside the
    directory, so the directory itself is one of the components the walk inspects.
    """
    try:
        refuse_linked_parent(versions_dir / "1.json")
    except OSError as exc:
        raise InstanceError(
            "this session's dashboard versions directory is a link or sits behind one; "
            "refusing to write through it"
        ) from exc


#: The pinned descriptor of the session page directory the current lock holds, or
#: ``None`` for a crewmate's page and for a platform that cannot pin. Every write
#: inside :func:`_locked` reads it, so nothing re-joins the directory by name.
_PINNED_DIR: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "dashboard_instance_pinned_dir", default=None
)


def _pinned_session_dir(directory: Path) -> int | None:
    """Create and open *directory* one ``O_NOFOLLOW`` component at a time.

    ``None`` where the platform cannot open relative to a directory descriptor, which
    leaves the ``lstat`` walk in :func:`_refuse_linked_session_dir` as the guard.
    """
    if not (pinned_fs.supports_pinned_walk() and pinned_parent_replace_supported()):
        return None
    root = data_home() / _SESSION_DASHBOARDS_DIR
    mkdirs_owner_only(data_home())
    try:
        root_fd = pinned_fs.create_and_open_dir_pinned(
            root, what="session dashboard pages", refusal=InstanceError
        )
        try:
            # Relative to root_fd, never by name: a session-pages root swapped for a
            # link after it was opened cannot move where the page directory lands.
            return pinned_fs.create_and_open_dir_at(
                root_fd, directory.name, what="session dashboard page", refusal=InstanceError
            )
        finally:
            os.close(root_fd)
    except InstanceError:
        raise
    except OSError as exc:
        raise InstanceError("this session's dashboard directory could not be opened") from exc


def _write(directory: Path, name: str, body: str) -> None:
    """Write *name* in *directory*, through the pinned descriptor when the lock holds one."""
    fd = _PINNED_DIR.get()
    if fd is not None:
        atomic_write(directory / name, body, fsync=True, parent_dir_fd=fd)
        return
    atomic_write(directory / name, body, fsync=True)


@contextmanager
def _locked(directory: Path, *, lock_file: str = _LOCK_FILE) -> Iterator[None]:
    """Exclusive lock over one instance directory, bounded against a live holder.

    The same shape the session ledger's lock has, and for the same reasons: a dedicated
    inode no write replaces, a bounded non-blocking poll, and a FAIL-CLOSED refusal
    rather than entering the critical section unserialized. Two writes to one instance
    must not interleave -- each reads the current version to compute the next, so an
    interleaving produces two writes claiming the same ``instance_version`` and one of
    the two pages is lost with nothing recorded.
    """
    pinned: int | None = None
    if directory.parent == data_home() / _SESSION_DASHBOARDS_DIR:
        _refuse_linked_session_dir(directory)
        pinned = _pinned_session_dir(directory)
    if pinned is None:
        mkdirs_owner_only(directory)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        if pinned is not None:
            fd = open_create_or_existing(lock_file, os.O_RDWR | nofollow, 0o600, dir_fd=pinned)
        else:
            fd = open_create_or_existing(directory / lock_file, os.O_RDWR | nofollow, 0o600)
    except OSError:
        if pinned is not None:
            os.close(pinned)
        raise
    token = _PINNED_DIR.set(pinned)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECS
        while not try_acquire_lock(fd, exclusive=True):
            if time.monotonic() >= deadline:
                raise InstanceError(
                    "this dashboard instance is being written by another writer; try again"
                )
            time.sleep(_LOCK_POLL_SECS)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        _PINNED_DIR.reset(token)
        os.close(fd)
        if pinned is not None:
            os.close(pinned)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


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


def default_template_id(slug: str, known: Iterable[str]) -> str | None:
    """The page an unadopted crewmate named *slug* gets, out of the ids in *known*.

    The page whose id IS the slug, when a shipped page has that id, and
    :data:`DEFAULT_TEMPLATE_ID` otherwise. This is the crew panel's rule that a
    template named after a crew is that crew's view, side effect included: a crewmate
    named like a shipped page gets that page. The slug is only ever compared against
    ids the catalog already loaded, and never names a directory.
    """
    ids = set(known)
    if slug and slug in ids:
        return slug
    return DEFAULT_TEMPLATE_ID if DEFAULT_TEMPLATE_ID in ids else None


def default_instance(slug: str) -> Instance | None:
    """The default page for *slug* as an UNADOPTED instance, or ``None`` if it is absent.

    The page is :func:`default_template_id`'s pick: one named for the crewmate, else
    :data:`DEFAULT_TEMPLATE_ID`. Both the read route and the ``[DASHBOARD]`` turn
    block resolve through here, so the page a reader sees and the page the crewmate
    is told about are one page.

    What a crewmate renders before it adopts anything. Deliberately not part of
    :func:`read`, which answers for the STORED record and nothing else: a fresh read
    saying "no template, version 0" is a fact two other surfaces need -- the snapshot
    route refuses on it, and the registry's own tests pin it -- and folding the default
    in there would make that answer depend on what happens to be in the registry.

    So the default is resolved by the surface that serves a PAGE to a reader, and it is
    marked for what it is: ``instance_version`` stays 0 and the state stays
    :data:`STATE_EMPTY`, because nothing was adopted and nothing was written to disk.
    Nothing is persisted either, which is the point -- a later version of the default
    template reaches every crewmate that never adopted one, with no migration.
    """
    by_id = catalog.list_templates().by_id
    # Shipped pages only: an operator's page is adopted by a person, never picked up
    # because its folder name happens to match a crewmate's slug.
    shipped = [tid for tid, e in by_id.items() if e.origin == catalog.BUILTIN_SOURCE]
    template_id = default_template_id(slug, shipped)
    if template_id is None:
        # A build that shipped the loader without the templates, which is the quiet
        # failure the packaging gates exist for. The caller's own empty state is the
        # right answer here, so this is reported and not raised.
        logger.warning(
            "dashboard: the default template %r is not in the registry", DEFAULT_TEMPLATE_ID
        )
        return None
    entry = by_id[template_id]
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


def _read_record(slug: str) -> dict[str, Any] | None:
    """The stored record, or ``None`` when there is none to read.

    An absent file is the ordinary state of a crewmate that never adopted a template,
    so it is not an error. A file that is not a JSON object under this schema IS an
    error and is reported as one by the caller -- read as :data:`STATE_ERROR` rather
    than as "never adopted", because the two need opposite answers from a human.
    """
    path = instance_dir(slug) / _RECORD_FILE
    try:
        raw = json.loads(read_bytes_with_retry(path).decode("utf-8"))
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
        return STATE_STALE, f"template {template_id!r} is no longer in the registry"
    if current.version > copied_version:
        return (
            STATE_STALE,
            f"template {template_id!r} is now version {current.version}; "
            f"this copy is version {copied_version}",
        )
    return STATE_LIVE, f"copied from {template_id!r} version {copied_version}"


def read(slug: str) -> Instance:
    """One crewmate's dashboard instance, as the person's lock says it is.

    While locked, the answer is the record SNAPSHOTTED into the lock file, not
    ``instance.json``: that file is writable by the crewmate's own shell, so a copy of
    an old version over it must not become the page the person locked.
    """
    snapshot = _lock_snapshot(slug)
    if snapshot is None:
        stored = _read_stored(slug)
        # Asked AGAIN after the writable record is read: a lock set in between means
        # that record may already be an agent's write made after the person locked.
        # No lock now means none existed when the record was read, so it stands.
        snapshot = _lock_snapshot(slug)
        if snapshot is None:
            return stored
    if "record" not in snapshot:
        # Present but unreadable. NOT a reason to read ``instance.json``, which an agent
        # can write: the page is withheld as broken, and the person can still unlock.
        return Instance(
            slug=slug,
            instance_version=0,
            template_id="",
            template_version=0,
            html="",
            manifest={},
            state=STATE_ERROR,
            state_reason="the dashboard is locked and its lock record cannot be read",
            updated_ms=0,
            structure_locked=True,
        )
    record = snapshot["record"]
    stored = _instance_from_raw(slug, record if isinstance(record, dict) else None)
    return dataclasses.replace(stored, structure_locked=True)


def _read_stored(slug: str) -> Instance:
    """The instance as ``instance.json`` holds it, with no regard to the lock."""
    return _instance_from_raw(slug, _read_record(slug))


def _updated_ms(raw: dict[str, Any]) -> int:
    """The record's ``updated_ms``, or 0 when it is not an integer.

    A record the crewmate's shell rewrote can hold anything here, and a read that
    raised on it would take the whole tab down, its Unlock button included.
    """
    value = raw.get("updated_ms")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _instance_from_raw(slug: str, raw: dict[str, Any] | None) -> Instance:
    """One crewmate's dashboard instance, with its state derived at read time.

    The state is DERIVED rather than stored. A stored state would be a claim about the
    registry made when the instance was last written, and the registry moves on its
    own: a built-in template shipping a new version makes every copy of it stale without
    touching one instance file, so a stored flag would say ``live`` forever.

    Costs the record plus one registry scan -- the fields, never the log.
    """
    if raw is None:
        return _empty(slug, "no template adopted yet")
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
            updated_ms=_updated_ms(raw),
        )
    state, reason = _state_of(template_id, template_version, manifest, html)
    package_slug, package_version, package_digest, package_released = _package_field(raw)
    return Instance(
        slug=slug,
        instance_version=version,
        template_id=template_id,
        template_version=template_version,
        html=html,
        manifest=manifest,
        state=state,
        state_reason=reason,
        updated_ms=_updated_ms(raw),
        package_slug=package_slug,
        package_version=package_version,
        package_digest=package_digest,
        package_released=package_released,
    )


def versions(slug: str) -> tuple[int, ...]:
    """Instance versions still on disk, oldest first. What a rollback may reach."""
    directory = instance_dir(slug) / _VERSIONS_SUBDIR
    found: list[int] = []
    try:
        children = list(directory.iterdir())
    except OSError:
        return ()
    for child in children:
        if child.suffix != ".json":
            continue
        try:
            found.append(int(child.stem))
        except ValueError:
            continue
    return tuple(sorted(found))


def _read_version(slug: str, version: int) -> dict[str, Any]:
    path = instance_dir(slug) / _VERSIONS_SUBDIR / f"{version}.json"
    try:
        raw = json.loads(read_bytes_with_retry(path).decode("utf-8"))
    except FileNotFoundError:
        kept = versions(slug)
        raise InstanceRefused(
            f"instance version {version} is no longer kept; kept versions: {list(kept)}"
        ) from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstanceError(f"instance version {version} cannot be read: {exc}") from None
    if not isinstance(raw, dict):
        raise InstanceError(f"instance version {version} is not an object")
    return raw


# --------------------------------------------------------------------------
# the history fold
# --------------------------------------------------------------------------


def _history_start() -> dict[str, Any]:
    """The fold's empty state: the retained rows, and how far each unit was folded."""
    return {"schema": SCHEMA_VERSION, "rows": [], "seen": {}}


def _history_row(data: Mapping[str, Any]) -> dict[str, Any]:
    """One entry's contribution, clamped on the way in.

    Clamped HERE as well as at the writer: these bytes come off a file this process
    does not control, so a planted or damaged line is exactly the input the writer's
    own rule never saw.
    """
    action = str(data.get("action") or "")
    return {
        "instance_version": int(data.get("instance_version") or 0),
        "action": action if action in ACTIONS else "",
        "template_id": str(data.get("template_id") or "")[:64],
        "template_version": int(data.get("template_version") or 0),
        "from_version": int(data.get("from_version") or 0),
        "fields": int(data.get("fields") or 0),
        "html_bytes": int(data.get("html_bytes") or 0),
        "at_ms": int(data.get("at_ms") or 0),
    }


def _history_step(state: dict[str, Any], data: Mapping[str, Any]) -> None:
    """Fold ONE entry in. Keeps :data:`MAX_HISTORY_ROWS`, newest last."""
    rows = state["rows"]
    rows.append(_history_row(data))
    if len(rows) > MAX_HISTORY_ROWS:
        del rows[: len(rows) - MAX_HISTORY_ROWS]


def _read_history_state(slug: str) -> dict[str, Any] | None:
    path = instance_dir(slug) / _HISTORY_FILE
    try:
        raw = json.loads(read_bytes_with_retry(path).decode("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
        return None
    if not isinstance(raw.get("rows"), list) or not isinstance(raw.get("seen"), dict):
        return None
    return raw


def _fold_history_from_log(slug: str, session_id: str) -> dict[str, Any]:
    """Rebuild the fold from the crew log. The SAVEPOINT's repair path, not its read path.

    Reached only when the savepoint is missing or was written under another schema. The
    ordinary read answers from the savepoint and the ordinary write steps it, so this is
    what makes the savepoint a cache of the log rather than a second record beside it.
    """
    state = _history_start()
    if not session_id:
        return state
    try:
        from kiro_crew.crew_log.projection import open_session_log
    except Exception:  # pragma: no cover - crew log unavailable
        return state
    try:
        handle = open_session_log(session_id)
        if handle is None:
            return state
        for entry in handle.iter_from(1):
            if getattr(entry, "type", "") != ENTRY_TYPE:
                continue
            data = getattr(entry, "data", None)
            if isinstance(data, dict) and str(data.get("slug") or "") == slug:
                _history_step(state, data)
        state["seen"][session_id] = True
    except Exception:
        # The history is not the record: a log that cannot be read costs the rows and
        # not the dashboard, exactly as the panel fold's listing failure does. The
        # traceback is rendered to TEXT because this frame names a handle: an exc_info
        # record keeps the frame, the frame keeps the handle, and a handler that keeps
        # records would then hold its write lease.
        log_exception_text(
            logger, logging.WARNING, "dashboard instance: could not fold history for %r", slug
        )
    return state


def history(slug: str, *, session_id: str = "") -> tuple[dict[str, Any], ...]:
    """This instance's change history, newest last.

    Answered from the savepoint beside the record, which every write steps. *session_id*
    serves only to REBUILD a savepoint that is missing, so a caller with no session in
    hand still gets whatever was folded before.
    """
    state = _read_history_state(slug)
    if state is None:
        state = _fold_history_from_log(slug, session_id)
    rows = state.get("rows")
    return tuple(row for row in rows if isinstance(row, dict)) if isinstance(rows, list) else ()


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


#: Template provenances whose page may become a crewmate's live dashboard.
#:
#: A rendered page runs its own inline script against the crewmate's fold values -- a
#: chart is script or it is nothing -- inside a frame that can navigate itself. So a
#: page that executes is a page trusted with every task title and summary it is handed,
#: and it can carry those somewhere by navigating. Provenance says whether a person put
#: the page there. Two do:
#:
#: * ``builtin`` -- a directory in this repo, which went through review;
#: * ``user`` -- a directory the operator placed in the data home's ``panel-templates``
#:   folder, which no agent write path reaches (see :mod:`catalog`).
#:
#: ``shared`` -- a page imported from a sender -- is not renderable, and neither is a
#: page an agent wrote: not at preview, not at adopt. See
#: ``docs/request-for-change/rfc-crewmate-dynamic-dashboard.md``.
#:
#: A TEMPLATE INSTANCE is what this set governs, and an agent-written template page is
#: refused here on exactly that reasoning. A ``kind="dashboard"`` PACKAGE is a separate
#: surface with a separate rule: the "supersedes P2" amendment in that same RFC admits a
#: page its own owner wrote, because only that owner may write it, values reach it as
#: text, the frame carries only its model's allowlist, and the owner agent reaches the
#: network through its own tools anyway. That amendment turns on the author, so it leaves
#: this set alone: a sender's page, and any page an agent other than the dashboard's
#: owner wrote, is still unrenderable.
RENDERABLE_SOURCES: Final[frozenset[str]] = frozenset({catalog.BUILTIN_SOURCE, catalog.USER_SOURCE})


def _check(manifest: Any, html: str) -> dict[str, Any]:
    """Validate a payload before it can become a version. Returns the decoded manifest."""
    if not isinstance(html, str) or not html.strip():
        raise InstanceRefused("a dashboard page cannot be empty")
    size = len(html.encode("utf-8"))
    if size > MAX_INSTANCE_HTML_BYTES:
        raise InstanceRefused(
            f"the page is {size} bytes, over the {MAX_INSTANCE_HTML_BYTES}-byte ceiling"
        )
    try:
        parsed = parse_manifest(manifest)
        check_parity(parsed, html)
    except ManifestError as exc:
        raise InstanceRefused(f"this page and manifest do not load: {exc}") from None
    if parsed.source not in RENDERABLE_SOURCES:
        raise InstanceRefused(
            f"a {parsed.source!r} template cannot be adopted: only a page that shipped "
            "with the product, or one the operator placed in the panel-templates "
            "folder, was put there by a person, and a rendered page runs its own "
            "script against this crewmate's task titles and summaries"
        )
    return dict(manifest)


def _prune_versions(directory: Path, dir_fd: int | None = None) -> None:
    """Drop all but the newest :data:`MAX_RETAINED_VERSIONS` payloads.

    With *dir_fd* the listing and the unlinks go through that pinned descriptor, so a
    session page's versions are never reached by name.
    """
    names = os.listdir(dir_fd) if dir_fd is not None else [p.name for p in directory.iterdir()]
    kept = sorted(
        (int(n[: -len(".json")]), n)
        for n in names
        if n.endswith(".json") and n[: -len(".json")].isdigit()
    )
    for _n, name in kept[: max(0, len(kept) - MAX_RETAINED_VERSIONS)]:
        try:
            if dir_fd is not None:
                os.unlink(name, dir_fd=dir_fd)
            else:
                (directory / name).unlink()
        except OSError:  # pragma: no cover - a losing race with another pruner
            pass


def _commit(
    slug: str,
    *,
    action: str,
    template_id: str,
    template_version: int,
    html: str,
    manifest: Mapping[str, Any],
    from_version: int,
    session_id: str,
    package: Mapping[str, Any] | None = None,
) -> Instance:
    """Write one new instance version. The lock is already held.

    *package* is ``{"slug", "version"}`` when this version puts the crewmate's own
    package in force, and ``None`` otherwise. The key is written EITHER WAY, so a
    template version says it released any package rather than leaving the reader to
    guess (see :func:`keeps_older_package_rule`). The template copy is stored beside the
    pointer in both cases, so a rollback to a package version still has a page to
    re-check.

    ORDER IS LOAD-BEARING. The version payload is written first, then the record that
    points at it: a crash between them leaves a payload nothing references, which is
    inert, while the other order leaves a record naming a version that does not exist --
    and a rollback to it would fail for a version the record says is current.
    """
    directory = instance_dir(slug)
    versions_dir = directory / _VERSIONS_SUBDIR
    pinned = _PINNED_DIR.get()
    unpinned_session = pinned is None and directory.parent == data_home() / _SESSION_DASHBOARDS_DIR
    if unpinned_session:
        _refuse_linked_versions_dir(versions_dir)
    if pinned is None:
        mkdirs_owner_only(versions_dir)
    if unpinned_session:
        _refuse_linked_versions_dir(versions_dir)
    current = _read_record(slug) or {}
    # Asked HERE, under the instance lock every caller already holds, so a lock set
    # between a caller's own read and this write still refuses it.
    if is_locked(slug):
        raise InstanceLocked(LOCKED_REFUSAL)
    previous = current.get("instance_version")
    previous = previous if isinstance(previous, int) and previous > 0 else 0
    next_version = previous + 1
    at_ms = _now_ms()
    stored_manifest = dict(manifest)
    declared = stored_manifest.get("fields")
    payload = {
        "schema": SCHEMA_VERSION,
        "slug": slug,
        "instance_version": next_version,
        "template": {"id": template_id, "version": template_version},
        "html": html,
        "manifest": stored_manifest,
        "action": action,
        "from_version": from_version,
        "updated_ms": at_ms,
        "package": (
            {
                "slug": str(package["slug"]),
                "version": int(package["version"]),
                "digest": str(package["digest"]),
            }
            if package
            else None
        ),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if pinned is not None:
        # The versions subdirectory is opened from the pinned page directory, so it
        # is never reached by name either.
        try:
            os.mkdir(_VERSIONS_SUBDIR, 0o700, dir_fd=pinned)
        except FileExistsError:
            pass
        flags = pinned_fs.dir_flags()
        versions_fd = os.open(_VERSIONS_SUBDIR, flags, dir_fd=pinned)
        try:
            atomic_write(
                versions_dir / f"{next_version}.json", body, fsync=True, parent_dir_fd=versions_fd
            )
            _write(directory, _RECORD_FILE, body)
            # Mirror the ``else`` branch's ``fsync_dir(directory)``: force the page
            # directory's entries out, tolerating the filesystems that cannot express a
            # directory fsync (network mounts answer EINVAL/ENOTSUP) while still raising
            # a genuine I/O failure. A bare ``os.fsync`` here would turn every apply on
            # such a mount into a 503 that skips the history append, even though the
            # rename plus the file fsync already made the write durable.
            fsync_open_dir(pinned, directory)
            _prune_versions(versions_dir, versions_fd)
        finally:
            os.close(versions_fd)
    else:
        atomic_write(versions_dir / f"{next_version}.json", body, fsync=True)
        _write(directory, _RECORD_FILE, body)
        fsync_dir(directory)
        _prune_versions(versions_dir)

    # A committed version replaces whatever was staged for a look: a preview shown
    # beside a page that has since changed would offer the person a Keep for a page
    # that is not the next step. Under the same lock the commit holds.
    discard_preview(slug)
    entry = {
        "slug": slug,
        "instance_version": next_version,
        "action": action,
        # A package version is named as the package in the history, not as the
        # template copy stored under it, so "go back" reads which page each was.
        "template_id": PACKAGE_TEMPLATE_ID if package else template_id,
        "template_version": int(package["version"]) if package else template_version,
        "from_version": from_version,
        "fields": len(declared) if isinstance(declared, dict) else 0,
        "html_bytes": len(html.encode("utf-8")),
        "at_ms": at_ms,
    }
    _append_history(slug, entry, session_id)
    record = read(slug)
    _sync_upkeep(record)
    return record


def _sync_upkeep(record: Instance) -> None:
    """Copy the crewmate's upkeep helper scripts for the version just committed.

    Every adopt, apply, rollback and edit reaches here, so a page swap copies the new
    page's helpers and drops the old page's in the same call. Handed the record, so the
    scripts are built without a second read of the store while its lock is held. A
    root session's own dashboard (:func:`is_session_key`) has no crewmate shell, so it
    gets none. Best effort: the version is already written.
    """
    if is_session_key(record.slug):
        return
    try:
        from kiro_crew import dashboard_upkeep

        dashboard_upkeep.sync(record.slug, page=(record, parse_manifest(dict(record.manifest))))
    except Exception:
        logger.warning("dashboard instance: upkeep scripts not synced", exc_info=True)


def _append_history(slug: str, entry: dict[str, Any], session_id: str) -> None:
    """Append the change to the crew log AND step the savepoint.

    The savepoint is stepped whether or not the append lands. The log is the durable
    record of the change and the savepoint is its cache, but the RECORD of this change
    is ``instance.json``, which is already written -- so a history row the log refused
    must still be visible beside the version it describes, or the dashboard would show
    a version its own history does not mention.
    """
    state = _read_history_state(slug) or _fold_history_from_log(slug, session_id)
    _history_step(state, entry)
    try:
        _write(
            instance_dir(slug),
            _HISTORY_FILE,
            json.dumps(state, ensure_ascii=False, sort_keys=True),
        )
    except OSError:
        logger.warning("dashboard instance: could not write the history savepoint", exc_info=True)
    if not session_id:
        return
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        crew_log_emit.on_dashboard_instance_changed(session_id, entry)
        crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS)
    except Exception:
        logger.warning("dashboard instance: could not append the history entry", exc_info=True)


def adopt(slug: str, template_id: str, *, session_id: str = "") -> Instance:
    """Copy the template *template_id* into *slug*'s own dashboard.

    A COPY, which is what makes an instance an instance: the page and the manifest are
    stored here, so the template's author shipping a new version leaves this dashboard
    showing what the crewmate adopted until it adopts again.

    Adopting over an existing instance is allowed and is an ordinary version bump: the
    previous version stays on disk, so a crewmate that adopted the wrong template rolls
    back to the one it had.
    """
    try:
        entry = catalog.load_one(template_id)
    except catalog.UnknownTemplate as exc:
        raise InstanceRefused(str(exc)) from None
    manifest = json.loads(json.dumps(_manifest_json(entry)))
    html = entry.html
    _check(manifest, html)
    with _locked(instance_dir(slug)):
        return _commit(
            slug,
            action="adopted",
            template_id=entry.id,
            template_version=entry.version,
            html=html,
            manifest=manifest,
            from_version=0,
            session_id=session_id,
        )


def _pointer_of(record: Instance) -> dict[str, Any] | None:
    """*record*'s package pointer in the shape :func:`_commit` takes, or ``None``."""
    if not record.package_slug:
        return None
    return {
        "slug": record.package_slug,
        "version": record.package_version,
        "digest": record.package_digest,
    }


def _write_page_snapshot(slug: str, content: str) -> str:
    """Store *content* as a gateway-owned package page for *slug*; return its digest."""
    digest = _page_digest(content)
    path = _page_snapshot_path(slug, digest)
    if not path.exists():
        # BYTES, so the file hashes to its digest on every platform: a str is
        # written in text mode, and on Windows that turns the stored form's
        # newlines into CRLF and the pin stops matching.
        atomic_write(path, content.encode("utf-8"), fsync=True)
    return digest


def _referenced_digests(slug: str) -> set[str]:
    """Every page digest the current record, the lock and the kept versions pin.

    COMPLETE OR RAISES. A record, lock or version that cannot be listed or read may
    pin a page, so an incomplete scan raises (``OSError`` / :class:`InstanceError`)
    and the caller prunes nothing, rather than deleting a page still pinned.
    """
    found: set[str] = set()
    lock = _lock_snapshot(slug)
    if lock is not None and "record" not in lock:
        raise InstanceError("the structure lock is unreadable, so its pin is unknown")
    raws: list[Any] = [_read_record(slug), (lock or {}).get("record")]
    try:
        names = os.listdir(instance_dir(slug) / _VERSIONS_SUBDIR)
    except FileNotFoundError:
        names = []
    for name in names:
        stem = name[: -len(".json")] if name.endswith(".json") else ""
        if stem.isdigit():
            raws.append(_read_version(slug, int(stem)))
    for raw in raws:
        if isinstance(raw, Mapping):
            digest = _package_field(raw)[2]
            if digest:
                found.add(digest)
    return found


def _prune_page_snapshots(slug: str) -> None:
    """Drop the package pages no kept version, current record or lock still pins.

    Best effort: a snapshot left behind is inert, and one still pinned is never
    dropped, so a rollback reaches every page its kept versions name.
    """
    from kiro_crew.agent_panel import panel_dir

    try:
        keep = _referenced_digests(slug)
        prefix = f"{slug}{_PAGE_SNAPSHOT_INFIX}"
        for child in panel_dir().iterdir():
            name = child.name
            if not (name.startswith(prefix) and name.endswith(".json")):
                continue
            if name[len(prefix) : -len(".json")] not in keep:
                child.unlink()
    except (OSError, InstanceError):
        logger.warning("dashboard: %r's old package pages were not pruned", slug, exc_info=True)


def apply_package(
    slug: str,
    *,
    package_slug: str,
    package_version: int,
    content: str,
    session_id: str = "",
) -> Instance:
    """Put the crewmate's own package version in force, as a NEW instance version.

    No person-approval step: the page is the owner crewmate's own package, written
    through the store's owner gate, and the frame contains it however it was written
    (no network, ``allow-scripts`` only, values as text). What a person keeps is the
    history: every apply is a version, and :func:`rollback` goes back to any of them
    in one call -- to an earlier package version or to the template page.

    *content* is the package's canonical stored bytes. They are kept as a
    gateway-owned snapshot (:func:`_page_snapshot_path`) and the version pins its
    digest, so the page in force is exactly the bytes saved -- whatever later happens
    to the artifact.

    The version also stores a template copy -- the current one, or the default page
    for a crewmate that never adopted one -- so the record stays readable by every
    reader of a template instance, and a rollback to this version has a page to
    re-check.
    """
    if not package_slug or isinstance(package_version, bool) or package_version < 1:
        raise InstanceRefused("a package apply needs the package's slug and a version")
    if not isinstance(content, str) or not content:
        raise InstanceRefused("a package apply needs the package's stored content")
    if is_locked(slug):
        raise InstanceLocked(LOCKED_REFUSAL)
    # ONE critical section for the snapshot write, the commit that pins it and the
    # prune: a concurrent apply's prune cannot see the new snapshot before the record
    # that pins it, so it can never delete the page this apply is committing.
    with _locked(instance_dir(slug)):
        digest = _write_page_snapshot(slug, content)
        current = read(slug)
        base: Instance | None = current
        if current.instance_version == 0 or current.state not in (
            STATE_LIVE,
            STATE_STALE,
        ):
            base = default_instance(slug)
        if base is None:
            raise InstanceRefused(
                "this gateway ships no default dashboard page to keep beside the package"
            )
        checked = _check(dict(base.manifest), base.html)
        record = _commit(
            slug,
            action="adopted",
            template_id=base.template_id,
            template_version=base.template_version,
            html=base.html,
            manifest=checked,
            from_version=0,
            session_id=session_id,
            package={"slug": package_slug, "version": package_version, "digest": digest},
        )
        _prune_page_snapshots(slug)
    return record


def _manifest_json(entry: catalog.CatalogEntry) -> dict[str, Any]:
    """The template's manifest as it sits on disk.

    Read back from the file rather than rebuilt from the parsed object. The parsed form
    is lossy by design -- it keeps what the loader checks -- and an instance must store
    what it COPIED, so a field the loader ignores today still travels with the copy.
    """
    try:
        raw = json.loads((entry.directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # `load_one` decoded this file already, so reaching here means it changed
        # between the two reads. A refusal names the template; the bare decode error
        # would carry past every caller as an unhandled ValueError.
        raise InstanceRefused(f"template {entry.id!r} cannot be read: {exc}") from None
    if not isinstance(raw, dict):  # pragma: no cover - load_template already refused this
        raise InstanceRefused(f"template {entry.id!r} has no manifest object")
    return raw


#: The refusal :func:`edit` gives a caller that tried to replace the page.
#:
#: Its own sentence rather than :data:`AUTHORED_PAGE_REFUSAL`, which tells a caller to
#: name a template instead -- advice that fits a preview and not an edit, where a
#: template is already named and the ask is to keep the name over different bytes.
EDITED_PAGE_REFUSAL: Final[str] = (
    "a dashboard's page cannot be edited. The stored record carries the page AND the "
    "`source` label that says where the page came from, so replacing one while "
    "keeping the other would make the record vouch for bytes nobody reviewed -- and "
    "the page is what runs, against this crewmate's own task titles and summaries. "
    "The manifest alone may be edited. To change the PAGE, preview a template from "
    "dashboard_templates and apply it, or dashboard_rollback to a version this "
    "crewmate already had"
)


def edit(
    slug: str,
    *,
    html: str | None = None,
    manifest: Mapping[str, Any] | None = None,
    session_id: str = "",
) -> Instance:
    """Change this crewmate's own copy. At least one of *html* or *manifest*.

    Both halves are checked TOGETHER against the parity rule even when only one is
    given, because that rule is about the pair: a page edited to bind a new field is
    refused until the manifest declares it, which is the whole reason an edit cannot
    produce a dashboard with an unowned empty cell.

    The template id and version are untouched. An edit does not make the crewmate the
    template's author, and moving the template version here would make the instance
    claim a parity with a template it does not match.

    **An html change is REFUSED.** The stored record carries both the page and the
    ``source`` label, so an edit that rewrote the page while leaving the label alone
    would produce a record that says ``builtin`` over bytes nobody reviewed -- and
    the label is what every reader downstream asks. Keeping the id while replacing
    the page is a different ask from previewing an authored page, so it gets its own
    sentence, :data:`EDITED_PAGE_REFUSAL`, rather than :data:`AUTHORED_PAGE_REFUSAL`:
    that one tells a caller to name a template, advice that fits a preview and not an edit.

    The parameter is kept rather than dropped so the refusal lives HERE, for the
    reason :func:`stage_preview` keeps its two: a caller added later cannot slip a
    page in by forgetting a check of its own. A MANIFEST-only edit is still allowed
    -- it can name fields and fold paths, so the worst a wrong one does is draw a
    cell nothing fills, and the parity rule refuses even that.
    """
    if html is None and manifest is None:
        raise InstanceRefused("an edit must change the page, the manifest, or both")
    if html is not None:
        raise InstanceRefused(EDITED_PAGE_REFUSAL)
    with _locked(instance_dir(slug)):
        current = read(slug)
        if current.instance_version == 0:
            raise InstanceRefused("this crewmate has no dashboard yet; adopt a template first")
        new_html = current.html
        new_manifest = dict(current.manifest) if manifest is None else dict(manifest)
        checked = _check(new_manifest, new_html)
        return _commit(
            slug,
            action="edited",
            template_id=current.template_id,
            template_version=current.template_version,
            html=new_html,
            manifest=checked,
            from_version=current.instance_version,
            session_id=session_id,
            # An edit changes the template copy's manifest, not which page is in
            # force, so a package in force stays in force.
            package=_pointer_of(current),
        )


def rollback(slug: str, to_version: int, *, session_id: str = "") -> Instance:
    """Restore instance version *to_version* as a NEW version.

    Forward, never backward -- see the module docstring. The restored payload is
    re-checked before it is committed: it passed when it was stored, and a format the
    gateway has tightened must refuse it here rather than install a page that does not
    load.
    """
    if not isinstance(to_version, int) or to_version < 1:
        raise InstanceRefused(f"instance version {to_version!r} must be a positive integer")
    with _locked(instance_dir(slug)):
        current = read(slug)
        if current.instance_version == 0:
            raise InstanceRefused("this crewmate has no dashboard yet; adopt a template first")
        if to_version == current.instance_version:
            raise InstanceRefused(f"instance version {to_version} is already the current one")
        if to_version > current.instance_version:
            raise InstanceRefused(
                f"instance version {to_version} does not exist; "
                f"the current version is {current.instance_version}"
            )
        payload = _read_version(slug, to_version)
        template = payload.get("template")
        template = template if isinstance(template, dict) else {}
        html = payload.get("html")
        manifest = payload.get("manifest")
        if not isinstance(html, str) or not isinstance(manifest, dict):
            raise InstanceError(f"instance version {to_version} holds no page to restore")
        checked = _check(manifest, html)
        # THE VERSION'S OWN PAGE IN FORCE comes back with it: a package version
        # restores its pinned package, and a template version -- including one written
        # before packages could be applied -- restores the template, releasing any
        # package. That is what makes one rollback the way back from either page.
        slug_v, version_v, digest_v, _released = _package_field(payload)
        return _commit(
            slug,
            action="rolled_back",
            template_id=str(template.get("id") or ""),
            template_version=int(template.get("version") or 0),
            html=html,
            manifest=checked,
            from_version=to_version,
            session_id=session_id,
            package={"slug": slug_v, "version": version_v, "digest": digest_v} if slug_v else None,
        )


#: The lock file's suffix inside the crew-panels store. A slug is ``[a-z0-9-]``, so no
#: published panel record (``<slug>.json``) can share a lock file's name.
_LOCK_SUFFIX: Final[str] = ".dashboard-lock"


def lock_path(slug: str) -> Path:
    """Where the person's structure lock for *slug* lives. Present means locked.

    NOT beside ``instance.json``: ``members/<slug>/`` is writable by the crewmate's own
    sandboxed shell, so a flag there is one an agent could clear by editing a file.
    The lock is an OWNER decision about what an agent may change, which is the class
    the ``crew-panels`` store exists for: the sandbox masks that directory, the
    file-tool gate fences it (``security.paths``), the launcher pre-creates it, and
    :func:`kiro_crew.agent_panel.panel_dir` refuses an aliased name. The gateway is
    its only reader and writer.
    """
    from kiro_crew.agent_panel import panel_dir

    if not slug:
        raise InstanceError("a dashboard instance needs a member slug")
    if is_session_key(slug):
        # A session's page has no crewmate and no Profile card to lock it from; its
        # key also carries a ``:``, which is not a file name on every platform.
        raise InstanceError("a session dashboard has no structure lock")
    return panel_dir() / f"{slug}{_LOCK_SUFFIX}"


def _lock_snapshot(slug: str) -> dict[str, Any] | None:
    """The lock file's contents, or ``None`` when *slug* is unlocked.

    FAIL-CLOSED: only a missing file is "unlocked". Any other failure -- a permission
    error, a damaged file, an aliased store -- reads as locked with no snapshot, so a
    store that cannot answer never lets an agent's change through, and the person
    shown "locked" can unlock it from the Profile card. A session's page is never
    locked: it has no lock path at all.
    """
    if is_session_key(slug):
        return None
    try:
        text = lock_path(slug).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("dashboard: the structure lock for %r is unreadable", slug, exc_info=True)
        return {}
    try:
        raw = json.loads(text)
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def is_locked(slug: str) -> bool:
    """Whether the person has locked *slug*'s page. Unreadable reads locked."""
    return _lock_snapshot(slug) is not None


#: The setter's own lock file, beside the lock in ``crew-panels``. Not the instance
#: directory's ``.lock``: creating that would write under ``members/<slug>/``.
_LOCK_GUARD_SUFFIX: Final[str] = ".dashboard-lock.guard"


def set_structure_lock(slug: str, locked: bool) -> Instance:
    """Set or clear the person's structure lock on *slug*'s dashboard.

    The only writer of :func:`lock_path`, and reached only from the owner-gated
    dashboard route: no MCP tool calls it, so an agent can be refused by the lock and
    can never lift it. The lock is not part of any version, so a rollback cannot
    restore an old lock state, and it holds for a crewmate that never adopted.

    EVERY write here is inside ``crew-panels``. Nothing is created, written or deleted
    under ``members/<slug>/``, a tree the crewmate's shell can redirect, so neither a
    lock nor an unlock can be steered into touching a file somewhere else. That is
    also why a lock change is not a history row: the history savepoint lives in that
    tree, and the lock file itself is the record of the lock.
    """
    if not isinstance(locked, bool):
        raise InstanceRefused("locked must be true or false")
    path = lock_path(slug)
    with _locked(path.parent, lock_file=f"{slug}{_LOCK_GUARD_SUFFIX}"):
        if is_locked(slug) != locked:
            if locked:
                # The record as it is NOW, which :func:`read` serves until the unlock.
                try:
                    record = _read_record(slug)
                except InstanceError:
                    raise InstanceRefused(
                        "this dashboard's record cannot be read, so it cannot be locked"
                    ) from None
                snapshot = {"slug": slug, "at_ms": _now_ms(), "record": record}
                atomic_write(path, json.dumps(snapshot, ensure_ascii=False), fsync=True)
            else:
                path.unlink()
            fsync_dir(path.parent)
    try:
        current = read(slug)
    except InstanceError:
        # An unreadable record does not undo a lock change that already landed.
        current = _empty(slug, "the record cannot be read")
    return dataclasses.replace(current, structure_locked=locked)


# --------------------------------------------------------------------------
# the staged preview
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
#: is minutes rather than seconds. Past it the preview is dropped rather than applied:
#: "yes" to a page somebody looked at an hour ago is a yes to a page whose fold values
#: have since moved, and applying it would install what they saw rather than what they
#: agreed to.
MAX_PREVIEW_AGE_MS: Final[int] = 30 * 60 * 1000


def _preview_path(slug: str) -> Path:
    return instance_dir(slug) / _PREVIEW_FILE


#: The Members page query that opens the Dashboard tab on the STAGED page:
#: ``?dashboard=preview``. Mirrored by ``DASHBOARD_PREVIEW_PARAM`` and
#: ``DASHBOARD_PREVIEW_VALUE`` in ``website/src/pages/members/dashboardPreview.ts``.
PREVIEW_PAGE_PARAM: Final[str] = "dashboard"
PREVIEW_PAGE_VALUE: Final[str] = "preview"


def preview_url(member: str) -> str:
    """The link a person opens to SEE the staged page: a page, never the JSON read.

    The Members page with this crewmate open and its Dashboard tab showing the staged
    page under a preview label. The tab reads it from the ordinary dashboard route with
    ``preview=1`` rather than a route of its own: that route is already owner-only and
    a staged page carries the same crewmate's fold values as the live one, so a second
    route would be a second place to get that gate right.

    *member* is the exact crew name, not the slug. Slugification is lossy, so the
    page and the read it makes both refuse a slug alone (``missing_member``).
    """
    query = urlencode({"member": member, PREVIEW_PAGE_PARAM: PREVIEW_PAGE_VALUE}, quote_via=quote)
    return f"/members?{query}"


def session_preview_url(slot_key: str) -> str:
    """:func:`preview_url` for a root session's page, which is read by its slot.

    The store key is a one-way digest, so the link is built from the slot key the
    caller already holds rather than recovered from the key.
    """
    return f"/api/chat/slots/{quote(slot_key, safe='')}/dashboard?preview=1"


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

    def wire(self, member: str) -> dict[str, Any]:
        """What a caller is told about the staged page. NEVER the page itself.

        The html is the large half, no caller renders it, and the person who has to
        decide reads it in the frame through :func:`preview_url`. So this carries
        identity and shape, which is the same cut a catalog listing makes.

        *member* is the exact crew name the link opens; a staged page is stored per
        slug and the slug alone does not name a crewmate.
        """
        declared = self.manifest.get("fields")
        return {
            "template_id": self.template_id,
            "template_version": self.template_version,
            "title": str(self.manifest.get("title") or ""),
            "fields": sorted(declared) if isinstance(declared, dict) else [],
            "html_bytes": len(self.html.encode("utf-8")),
            "staged_ms": self.staged_ms,
            "preview_url": preview_url(member),
        }


#: The refusal a caller gets for passing a page it wrote. Spelled out once, because
#: both this module and the MCP tool above it answer the same question and an agent
#: that reads two different answers tries the second one.
AUTHORED_PAGE_REFUSAL: Final[str] = (
    "a template call takes a template_id only: name a template from "
    "dashboard_templates and put it live with dashboard_apply. A page you write is "
    "not a template. To put a page you composed on your Dashboard, save it with "
    "dashboard_save as your own dashboard package, then dashboard_apply it"
)


def stage_preview(
    slug: str,
    *,
    template_id: str | None = None,
    html: str | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> Preview:
    """Check a template and stage it for a look. NO version is written.

    *template_id* names a template the catalog serves, and that is the only thing that
    can be staged. *html* and *manifest* are accepted as parameters so the refusal lives
    HERE rather than only in the callers: an authored page reaching this function by any
    route gets :data:`AUTHORED_PAGE_REFUSAL`, so a second caller added later cannot
    stage one by forgetting a check of its own.

    The page is checked by the same :func:`_check` every committed version runs, so a
    template directory that was hand-edited into a page whose bindings do not match its
    manifest is refused before anybody is asked to look at it, rather than at
    :func:`apply_preview` after a person has already said yes.

    Staging REPLACES any previous preview. One page is offered at a time because one
    question is asked at a time: "keep this one" has to name something, and a store
    holding three staged pages makes that answer ambiguous.
    """
    if html is not None or manifest is not None:
        raise InstanceRefused(AUTHORED_PAGE_REFUSAL)
    if template_id is None or not template_id.strip():
        raise InstanceRefused(
            "a preview needs a template_id -- dashboard_templates lists the ones this "
            "gateway serves"
        )
    try:
        entry = catalog.load_one(template_id)
    except catalog.UnknownTemplate as exc:
        raise InstanceRefused(str(exc)) from None
    # Round-tripped through JSON like `adopt` does, so the staged copy cannot share a
    # mutable sub-object with the catalog entry this process is still holding.
    staged_manifest = json.loads(json.dumps(_manifest_json(entry)))
    staged_html = entry.html
    _check(staged_manifest, staged_html)
    with _locked(instance_dir(slug)):
        # Minted under the slug's lock, beyond the page it replaces, so the stamp the
        # person's click names can only ever match the page that was shown.
        shown = staged_preview(slug)
        staged_ms = _mint_staged_ms(after=shown.staged_ms if shown is not None else 0)
        payload = {
            "schema": SCHEMA_VERSION,
            "slug": slug,
            "template": {"id": entry.id, "version": entry.version},
            "html": staged_html,
            "manifest": staged_manifest,
            "staged_ms": staged_ms,
        }
        _write(
            instance_dir(slug),
            _PREVIEW_FILE,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    return Preview(
        slug=slug,
        template_id=entry.id,
        template_version=entry.version,
        html=staged_html,
        manifest=staged_manifest,
        staged_ms=staged_ms,
    )


def _preview_payload(slug: str) -> dict[str, Any] | None:
    """The staged file's decoded payload, or ``None`` when there is nothing to apply.

    The four cases :func:`staged_preview` documents: nothing staged, text that will not decode, another schema, and older than
    :data:`MAX_PREVIEW_AGE_MS`. Each means no page this gateway will install, and the
    answer a caller can act on is the same one -- stage it again.
    """
    try:
        raw = json.loads(read_bytes_with_retry(_preview_path(slug)).decode("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
        return None
    try:
        staged_ms = int(raw.get("staged_ms") or 0)
    except (TypeError, ValueError, OverflowError):
        # A fifth bad case alongside the four above: a hand-forged envelope whose
        # ``staged_ms`` is not a number. A gateway writer always stamps an int, so this
        # is only reachable from a forged file -- answered the same way, stage it again,
        # rather than letting the conversion escape as an uncaught 500.
        return None
    if staged_ms <= 0 or _now_ms() - staged_ms > MAX_PREVIEW_AGE_MS:
        return None
    raw["staged_ms"] = staged_ms
    return raw


def staged_preview(slug: str) -> Preview | None:
    """The TEMPLATE page staged for *slug*, or ``None`` when there is none to apply.

    ``None`` covers four cases that are one case to every caller: nothing was staged,
    the file will not decode, it was written under another schema, and it is older than
    :data:`MAX_PREVIEW_AGE_MS`. Each means there is no page this gateway will install,
    and the answer a caller can act on is the same one -- stage it again.

    A package staged by an older gateway answers ``None`` here too: its payload carries
    no ``html`` and no ``manifest``. A package is never staged here -- ``dashboard_save``
    puts it in force through :func:`apply_package`.
    """
    raw = _preview_payload(slug)
    if raw is None:
        return None
    html = raw.get("html")
    manifest = raw.get("manifest")
    if not isinstance(html, str) or not isinstance(manifest, dict):
        return None
    staged_ms = int(raw["staged_ms"])
    template = raw.get("template")
    template = template if isinstance(template, dict) else {}
    template_id = str(template.get("id") or "")
    if not template_id:
        # A preview file left by an older gateway that staged a page the caller wrote.
        # It names no template, so there is nothing this one will install from it, and
        # "stage it again" is the same answer the four cases above get.
        return None
    return Preview(
        slug=slug,
        template_id=template_id,
        template_version=int(template.get("version") or 0),
        html=html,
        manifest=manifest,
        staged_ms=staged_ms,
    )


def discard_preview(slug: str) -> None:
    """Drop the staged page. Idempotent, and never raises for one that is not there."""
    pinned = _PINNED_DIR.get()
    try:
        if pinned is not None:
            os.unlink(_PREVIEW_FILE, dir_fd=pinned)
            return
        _preview_path(slug).unlink()
    except OSError:
        pass


def discard_shown_preview(slug: str, *, expected_staged_ms: int) -> None:
    """Drop the staged page the person was shown, and only that one.

    The person's Discard. Checked inside the lock like :func:`apply_preview`'s Keep,
    so a page staged after the look is refused :class:`PreviewChanged` instead of
    being thrown away unseen. Nothing staged any more is not a refusal: the person's
    "not that one" is already true.
    """
    with _locked(instance_dir(slug)):
        preview = staged_preview(slug)
        if preview is None:
            return
        if preview.staged_ms != expected_staged_ms:
            raise PreviewChanged(
                "a different page was staged after this one was shown; look at the new "
                "preview before discarding it"
            )
        discard_preview(slug)


def apply_preview(
    slug: str, *, session_id: str = "", expected_staged_ms: int | None = None
) -> Instance:
    """Commit the staged page as *slug*'s dashboard, and return the new instance.

    The only writer behind "keep this one". The staged template becomes a new instance
    version; nothing is written to any catalog, because the only page that can be
    staged is one the catalog already serves.

    The action recorded is ``adopted``, which is what happened, and it keeps the
    history's own vocabulary closed -- see :data:`ACTIONS` -- so no new entry-type value
    ships to describe a change the existing three already name.

    The page is re-checked against the manifest that will actually be STORED. The staged
    pair passed at staging, but staging and applying are two cycles with a person
    between them, and the parity rule is about the pair being committed.

    The preview is DISCARDED once the version lands, so "keep this one" cannot be
    answered twice and quietly write two versions of the same page. The discard
    happens INSIDE the lock, immediately after the commit: released first, this call
    would delete whatever preview is staged when it gets around to it, so an apply
    racing a preview would throw away the page that was just staged -- and a second
    apply arriving in the same window would consume the first one's preview before
    it was deleted at all.

    *expected_staged_ms* is the person's Keep button naming the page it was drawn
    beside. Checked INSIDE the lock, so a page staged again between the look and the
    click is refused with :class:`PreviewChanged` rather than installed unseen.
    """
    with _locked(instance_dir(slug)):
        preview = staged_preview(slug)
        if preview is None:
            raise NothingStaged(
                "there is no page staged to keep; preview one first, and stage it again "
                "if it has been sitting for more than "
                f"{MAX_PREVIEW_AGE_MS // 60_000} minutes"
            )
        if expected_staged_ms is not None and preview.staged_ms != expected_staged_ms:
            raise PreviewChanged(
                "a different page was staged after this one was shown; look at the new "
                "preview before keeping it"
            )
        checked = _check(preview.manifest, preview.html)
        instance = _commit(
            slug,
            action="adopted",
            template_id=preview.template_id,
            template_version=preview.template_version,
            html=preview.html,
            manifest=checked,
            from_version=0,
            session_id=session_id,
        )
        discard_preview(slug)
    return instance
