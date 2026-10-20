"""Tell the user once when an installed app has a newer version published.

The store already SHOWS pending updates: Discover carries a count on its rail
row, derived from the same listing rows this module reads. A badge only speaks
to someone already looking at it, so this adds the other half -- a bell note
that reaches a user who is not in the store.

ONCE is the whole difficulty. The listing that feeds this runs again on every
dashboard load, so "announce what is pending" would re-announce the same
version forever. A ledger under the data home records the version last
announced per app, and only a DIFFERENT version announces again.

A first observation adopts instead of announcing. With no ledger yet, every
pending update looks new, and a user who installed this with five updates
waiting would get five notes about versions that have been out for weeks. The
badge already states that standing backlog; the bell is for what changes from
here. Same choice ``issue_radar``'s watcher makes for a repository's
pre-existing issue backlog, for the same reason.

Persist before you publish. The ledger is written BEFORE any note is built for
delivery, and a ledger that cannot be written produces no note at all: the
next listing would read the same stale ledger and announce again, on every
dashboard load, for as long as the write keeps failing. One missed note is the
cost of the other ordering, and the note is redundant with the badge anyway.

Split in two halves because they run in different places. ``prepare`` reads and
writes the ledger, so the route runs it off the loop. ``deliver`` calls
``state.notify``, which must run ON the serving loop: off it, the notification
coordinator persists inline instead of through its FIFO executor, and a rewrite
queued by a concurrent delete can then overwrite the new note -- which the
ledger, already written, would never let come back.

One lock around the whole read-modify-write. The listing is served
concurrently (the store page and the shell badge query land together), and two
unserialized passes that each read the same ledger would each write back only
their own view, so the second write erases the entry the first just committed
and that version is announced again on the next load. Holding the lock for the
read as well as the write is what makes "announce only what is not recorded" a
decision against the ledger as it actually stands. A lock that cannot be taken
fails closed into the total contract below: no note, nothing written.

Adds no network call and no timer. It reads rows the listing already built, and
runs where that listing is already served.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.apps.registry_pipeline.catalog import _version_newer
from kiro_crew.apps.update_status import publishable_version
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.platform_lock_compat import file_lock, open_lock_file

logger = logging.getLogger(__name__)

#: Version last announced per app, under the data home. Deliberately NOT under
#: ``cache/``: dropping a cache is a supported repair, and dropping this one
#: would re-announce every pending update as though it were new.
LEDGER_FILENAME = "announced-app-updates.json"

#: Legacy notification kind, kept as the one the gateway's own update notice
#: uses so the frontend's ``note.kind`` filters see a single spelling for
#: "something can be updated". The CHANNEL is this module's own; see
#: ``SYSTEM_CHANNELS``.
NOTE_KIND = "update"
NOTE_CHANNEL = "system.apps"


def _ledger_path() -> Path:
    return config_dir() / LEDGER_FILENAME


def _ledger_lock_path() -> Path:
    return _ledger_path().with_suffix(".lock")


def _read_ledger() -> dict[str, str] | None:
    """Versions already announced, or ``None`` when no ledger exists yet.

    ``None`` and ``{}`` mean different things and the caller acts on the
    difference: nothing recorded yet adopts the current backlog silently, while
    an empty ledger is a real record that nothing is outstanding.

    An unreadable or malformed file reads as ``{}`` rather than ``None``. Both
    answers are wrong in some way, and this is the direction that fails toward
    one duplicate note instead of toward silence: treating a corrupt ledger as
    "first run" would adopt whatever is pending and never mention it.
    """
    path = _ledger_path()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("could not read the announced-app-updates ledger", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _write_ledger(versions: dict[str, str]) -> bool:
    """Write the ledger. Returns whether it is now on disk.

    A ``False`` here means no note may go out: see "persist before you publish"
    in the module docstring.
    """
    try:
        atomic_write(_ledger_path(), json.dumps(versions, indent=2, sort_keys=True) + "\n")
    except OSError:
        logger.warning("could not write the announced-app-updates ledger", exc_info=True)
        return False
    return True


def _updatable(row: dict[str, Any]) -> bool:
    """Whether the store could actually update this listing row.

    The same three conditions the Discover badge counts (``countUpdatables`` in
    ``useAppsData.ts``), so the note and the badge can never disagree about what
    is pending. ``lifecycle`` matters most: an ``app``- or ``locked``-lifecycle
    app updates itself, so announcing one would point the user at an Update
    button the store cannot press.
    """
    return bool(
        row.get("installed") and row.get("updateAvailable") and row.get("lifecycle") == "gateway"
    )


def _pending(rows: list[Any]) -> dict[str, tuple[str, str]]:
    """``name -> (published version, installed version)`` for updatable rows.

    Both versions go through ``publishable_version``: they end up in a note the
    gateway speaks, and the published one is text a registry author controls. A
    row whose published version fails the check is not announced at all.
    """
    pending: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict) or not _updatable(row):
            continue
        name = row.get("name")
        published = publishable_version(row.get("version"))
        if not isinstance(name, str) or not name or not published:
            continue
        pending[name] = (published, publishable_version(row.get("installedVersion")))
    return pending


def _upkeep(
    known: dict[str, str], rows: list[Any], pending: dict[str, tuple[str, str]]
) -> dict[str, str]:
    """The ledger after this listing: what to keep, what to drop, what to add.

    An entry is dropped only on evidence it is finished with -- the app's row is
    here and says it is uninstalled, or that the installed version has caught
    up with the one recorded. An entry whose app is simply ABSENT from
    this listing is kept: a listing can lack a row for reasons that say nothing
    about the app (a registry briefly unreachable, a fallback source), and
    forgetting the entry then would announce the same version again the next
    time the row comes back.
    """
    by_name = {r["name"]: r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)}
    kept: dict[str, str] = {}
    for name, recorded in known.items():
        row = by_name.get(name)
        if row is None:
            kept[name] = recorded
            continue
        if not row.get("installed"):
            continue
        installed = str(row.get("installedVersion") or "")
        if installed and not _version_newer(recorded, installed):
            continue
        kept[name] = recorded
    for name, (published, _) in pending.items():
        kept[name] = published
    return kept


def _note(name: str, published: str, installed: str) -> dict[str, Any]:
    """One note, ready for ``state.notify``.

    Titled by the app's NAME, not its display name. A display name on an
    external row is content that row's author controls, and a bell note is read
    as the gateway speaking. The name is the trust-keyed identifier, validated
    on install, and it is what the link resolves, so the note cannot describe
    one app and open another.

    The version leads the title rather than trailing a sentence, so the part the
    feed truncates first is the word "available", not the version.
    """
    body = (f"You have {installed}. " if installed else "") + "Open it to update."
    return {
        "title": f"{name} {published} available",
        "body": body,
        "meta": {"app": name, "latestVersion": published, "installedVersion": installed},
        "url": f"/apps/detail/{name}",
    }


def prepare_app_update_notes(rows: list[Any]) -> list[dict[str, Any]]:
    """Update the ledger from *rows* and return the notes that are now due.

    Blocking (ledger I/O): run off the loop. Hand the result to
    :func:`deliver_app_update_notes` on the loop.

    *rows* are listing rows already carrying ``updateAvailable`` and the install
    status (``_enrich_with_install_status``), so nothing is fetched or re-read.

    Total by contract: every failure is logged and answers ``[]``. This runs on
    the path that serves the store, and a store that 500s because a note could
    not be worked out is a worse outcome than a missing note.
    """
    try:
        pending = _pending(rows)
        lock_path = _ledger_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open_lock_file(lock_path) as fd, file_lock(fd, exclusive=True, required=True):
            known = _read_ledger()
            if known is None:
                _write_ledger({name: published for name, (published, _) in pending.items()})
                return []
            due = [
                (name, published, installed)
                for name, (published, installed) in sorted(pending.items())
                if known.get(name) != published
            ]
            ledger = _upkeep(known, rows, pending)
            if ledger != known and not _write_ledger(ledger):
                return []
        return [_note(name, published, installed) for name, published, installed in due]
    except Exception:
        logger.warning("could not work out which app updates to announce", exc_info=True)
        return []


def deliver_app_update_notes(state: Any, notes: list[dict[str, Any]]) -> list[str]:
    """Send *notes* through ``state.notify``. Call ON the serving loop.

    Returns the app names delivered. One note failing does not stop the rest,
    and nothing raises: the store response this rides on must not fail on it.
    """
    delivered: list[str] = []
    for note in notes:
        app = note["meta"]["app"]
        try:
            state.notify(
                NOTE_KIND,
                note["title"],
                note["body"],
                meta=note["meta"],
                url=note["url"],
                channel=NOTE_CHANNEL,
            )
        except Exception:
            logger.warning("could not deliver the update note for app %r", app, exc_info=True)
            continue
        delivered.append(app)
    return delivered
