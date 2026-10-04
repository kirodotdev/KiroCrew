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

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterator, Mapping

from kiro_crew.atomic_write import atomic_write, fsync_dir
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
from kiro_crew.platform_compat import release_lock, try_acquire_lock

__all__ = [
    "ACTIONS",
    "DEFAULT_TEMPLATE_ID",
    "MAX_HISTORY_ROWS",
    "MAX_INSTANCE_HTML_BYTES",
    "MAX_RETAINED_VERSIONS",
    "RENDERABLE_SOURCES",
    "SCHEMA_VERSION",
    "STATE_EMPTY",
    "STATE_ERROR",
    "STATE_LIVE",
    "STATE_STALE",
    "Instance",
    "InstanceError",
    "InstanceRefused",
    "adopt",
    "default_instance",
    "edit",
    "history",
    "instance_dir",
    "read",
    "rollback",
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

#: The page's ceiling. The frame renders this into a sandboxed iframe, so the cost of a
#: large one is paid by the browser; the number bounds what one crewmate can store per
#: version, multiplied by :data:`MAX_RETAINED_VERSIONS`.
MAX_INSTANCE_HTML_BYTES: Final[int] = 64 * 1024

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


def _now_ms() -> int:
    return int(time.time() * 1000)


def instance_dir(slug: str) -> Path:
    """Where one crewmate's dashboard instance lives.

    Under the member's own space, beside everything else keyed by that slug, so a
    crewmate removed from the roster takes its dashboard with it.
    """
    if not slug:
        raise InstanceError("a dashboard instance needs a member slug")
    return data_home() / "members" / slug / "dashboard"


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """Exclusive lock over one instance directory, bounded against a live holder.

    The same shape the session ledger's lock has, and for the same reasons: a dedicated
    inode no write replaces, a bounded non-blocking poll, and a FAIL-CLOSED refusal
    rather than entering the critical section unserialized. Two writes to one instance
    must not interleave -- each reads the current version to compute the next, so an
    interleaving produces two writes claiming the same ``instance_version`` and one of
    the two pages is lost with nothing recorded.
    """
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / _LOCK_FILE
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
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
        os.close(fd)


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


def default_instance(slug: str) -> Instance | None:
    """:data:`DEFAULT_TEMPLATE_ID` as an UNADOPTED instance, or ``None`` if it is absent.

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
    try:
        entry = catalog.load_one(DEFAULT_TEMPLATE_ID)
    except catalog.UnknownTemplate:
        # A build that shipped the loader without the templates, which is the quiet
        # failure the packaging gates exist for. The caller's own empty state is the
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


def _read_record(slug: str) -> dict[str, Any] | None:
    """The stored record, or ``None`` when there is none to read.

    An absent file is the ordinary state of a crewmate that never adopted a template,
    so it is not an error. A file that is not a JSON object under this schema IS an
    error and is reported as one by the caller -- read as :data:`STATE_ERROR` rather
    than as "never adopted", because the two need opposite answers from a human.
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
        return STATE_STALE, f"template {template_id!r} is no longer in the registry"
    if current.version > copied_version:
        return (
            STATE_STALE,
            f"template {template_id!r} is now version {current.version}; "
            f"this copy is version {copied_version}",
        )
    return STATE_LIVE, f"copied from {template_id!r} version {copied_version}"


def read(slug: str) -> Instance:
    """One crewmate's dashboard instance, with its state derived at read time.

    The state is DERIVED rather than stored. A stored state would be a claim about the
    registry made when the instance was last written, and the registry moves on its
    own: a built-in template shipping a new version makes every copy of it stale without
    touching one instance file, so a stored flag would say ``live`` forever.

    Costs the record plus one registry scan -- the fields, never the log.
    """
    raw = _read_record(slug)
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
        raw = json.loads(path.read_text(encoding="utf-8"))
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
        raw = json.loads(path.read_text(encoding="utf-8"))
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
#: chart is script or it is nothing -- inside a frame the dashboard's own
#: ``frame-src`` lets navigate itself. So a page that executes is a page trusted with
#: the task titles and summaries it is given, and provenance is the only signal that
#: says whether it was ever reviewed by anyone here.
#:
#: ``builtin`` ships with the product and ``user`` was written on this machine. A
#: ``shared`` manifest came out of :mod:`~kiro_crew.dashboard_templates.share`, which
#: stamps that source on every imported file: an arbitrary page from an arbitrary
#: sender, which IMPORT stores and lists but which nothing here will render.
#:
#: P2's template picker is where an imported page gets looked at before it can be
#: adopted; until that review surface exists, the honest answer to "adopt this file
#: somebody sent me" is a refusal that says so.
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
            f"a {parsed.source!r} template cannot be adopted: an imported page is not "
            "reviewed by anything here, and a rendered page runs its own script against "
            "this crewmate's task titles and summaries"
        )
    return dict(manifest)


def _prune_versions(directory: Path) -> None:
    """Drop all but the newest :data:`MAX_RETAINED_VERSIONS` payloads."""
    kept = sorted(
        (int(p.stem), p) for p in directory.iterdir() if p.suffix == ".json" and p.stem.isdigit()
    )
    for _n, path in kept[: max(0, len(kept) - MAX_RETAINED_VERSIONS)]:
        try:
            path.unlink()
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
) -> Instance:
    """Write one new instance version. The lock is already held.

    ORDER IS LOAD-BEARING. The version payload is written first, then the record that
    points at it: a crash between them leaves a payload nothing references, which is
    inert, while the other order leaves a record naming a version that does not exist --
    and a rollback to it would fail for a version the record says is current.
    """
    directory = instance_dir(slug)
    versions_dir = directory / _VERSIONS_SUBDIR
    versions_dir.mkdir(parents=True, exist_ok=True)
    current = _read_record(slug) or {}
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
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    atomic_write(versions_dir / f"{next_version}.json", body, fsync=True)
    atomic_write(directory / _RECORD_FILE, body, fsync=True)
    fsync_dir(directory)
    _prune_versions(versions_dir)

    entry = {
        "slug": slug,
        "instance_version": next_version,
        "action": action,
        "template_id": template_id,
        "template_version": template_version,
        "from_version": from_version,
        "fields": len(declared) if isinstance(declared, dict) else 0,
        "html_bytes": len(html.encode("utf-8")),
        "at_ms": at_ms,
    }
    _append_history(slug, entry, session_id)
    return read(slug)


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
        atomic_write(
            instance_dir(slug) / _HISTORY_FILE,
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            fsync=True,
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
    """
    if html is None and manifest is None:
        raise InstanceRefused("an edit must change the page, the manifest, or both")
    with _locked(instance_dir(slug)):
        current = read(slug)
        if current.instance_version == 0:
            raise InstanceRefused("this crewmate has no dashboard yet; adopt a template first")
        new_html = current.html if html is None else html
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
        return _commit(
            slug,
            action="rolled_back",
            template_id=str(template.get("id") or ""),
            template_version=int(template.get("version") or 0),
            html=html,
            manifest=checked,
            from_version=to_version,
            session_id=session_id,
        )
