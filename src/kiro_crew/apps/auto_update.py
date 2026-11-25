"""Install newer versions of installed apps without a click, when the owner asked for it.

``app_updates.mode`` decides it for every app (``off`` or ``on``), and
``apps.<name>.auto_update`` overrides it for one: ``always`` behaves as ``on``,
``never`` pins the app at its installed version. Only ``on`` reaches this
module's apply step; ``off`` stays with the listing-driven note in
:mod:`kiro_crew.apps.update_notify`, which leaves ``on`` apps to this module so
one release is never announced twice. A check runs every
:data:`CHECK_INTERVAL_SECS`.

Each check refreshes the published documents the store reads (the catalog and
every configured registry index, as Discover's refresh does), then compares
each eligible installed app against :func:`update_status.published_index`, the
same precedence the store and the app's own update banner use.

An eligible app is installed from a registry or the catalog (``registry:``
source), has ``lifecycle: "gateway"`` (an ``app`` or ``locked`` app updates
itself), and has a published version strictly newer than its own. Downgrades
are never applied: ``_version_newer`` is strict.

The apply step is ``kiro_crew.apps.routes.apply_app_update``, the Update button's
own sequence, run inside :func:`update_consent.refusing_permission_widening`.
So the trust and provenance pins, admission, data preservation, cron and
resource re-registration and the restore on failure are the button's, and the
one difference is that a version asking for a permission the installed one
lacks is refused and turned into a needs-consent note instead. So is an update
that would end the memory consent the owner gave the installed version
(``memory_provider.consent.update_would_end_consent``), since the owner would
be asked again.

It waits for idle the way the gateway's own auto-update does: an app is put off
to the next check while one of its sessions has a turn in flight, one of its
cron jobs or Job SDK runs is running, or a startup hook it launched has not
settled. Nothing is interrupted. Once the gateway is stopping no update starts,
and one still cloning or building is put off before its backend stops.

A ledger under the data home records, per app, the last version this module
tried and what came of it. A version that needed consent or failed is noted
once, not on every check. A failure is retried on later checks, since a
network or build error can clear on its own; one that needed consent is not,
because only the owner can change that answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.apps.update_status import publishable_version

logger = logging.getLogger(__name__)

#: The loop's sleep, bound here so a test can drive the loop without a real clock.
_sleep = asyncio.sleep

#: Apps whose update is between its first step and its last right now.
_APPLYING: set[str] = set()
#: Set by gateway shutdown: no further update starts in this process.
_STOP = {"requested": False}
#: How many restarts are waiting to replace or end this process. While any is,
#: no update starts.
_RESTART_HOLD = {"holders": 0}
#: How often :func:`settle_for_restart` looks again while an update applies.
_SETTLE_POLL_SECS = 0.25
#: The reason an update is put off while a restart holds the loop.
RESTART_HOLD_REASON = "the gateway is restarting"
#: The reason an update is put off once the gateway is stopping.
STOP_REASON = "the gateway is stopping"


def request_stop() -> None:
    """Start no further update. One past its take-down runs to its end."""
    _STOP["requested"] = True


def stop_reason() -> str:
    """:data:`STOP_REASON` once :func:`request_stop` ran, else ``""``."""
    return STOP_REASON if _STOP["requested"] else ""


def applying() -> bool:
    """Whether an update is mid-apply, which shutdown must wait for, not cancel."""
    return bool(_APPLYING)


def release_restart_hold() -> None:
    """Give back one hold from :func:`settle_for_restart`."""
    _RESTART_HOLD["holders"] = max(0, _RESTART_HOLD["holders"] - 1)


async def settle_for_restart(timeout: float) -> bool:
    """Take a hold, then wait up to *timeout* seconds for the update applying.

    The hold comes first and stays through the wait, so no further update
    starts while the restart waits; the caller releases it. Never cancels the
    update: its copy runs in a worker thread a cancel cannot stop. Returns
    ``True`` once none applies. Returns ``False`` at the deadline, the same
    outcome shutdown accepts for an update that outlives its budget.
    """
    _RESTART_HOLD["holders"] += 1
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _APPLYING:
        if loop.time() >= deadline:
            return False
        await _sleep(_SETTLE_POLL_SECS)
    return True


def restart_hold_reason() -> str:
    """:data:`RESTART_HOLD_REASON` while a restart holds the loop, else ``""``."""
    return RESTART_HOLD_REASON if _RESTART_HOLD["holders"] else ""


def reset_for_boot() -> None:
    """Forget an earlier boot's stop request, restart holds and recorded outcomes.

    The gateway calls this before it starts :func:`run_forever`, so a second boot
    in the same process does not inherit the first one's stop and return before
    its first check. The applying set is kept: it names an update a worker thread
    may still be copying, and that update's own ``finally`` takes it out.
    """
    _STOP["requested"] = False
    _RESTART_HOLD["holders"] = 0
    _RECORDED.clear()


def reset_for_tests() -> None:
    """:func:`reset_for_boot`, and forget every app marked applying as well."""
    reset_for_boot()
    _APPLYING.clear()


LEDGER_FILENAME = "auto-app-updates.json"

#: Ledger outcomes. ``needs_consent`` and ``no_change`` are final for their
#: published version; ``failed`` retries.
OUTCOME_UPDATED = "updated"
OUTCOME_NEEDS_CONSENT = "needs_consent"
OUTCOME_FAILED = "failed"
#: The update ran but the installed version did not move past the old one: the
#: source it clones names an older version than the index advertises. Retrying
#: would rebuild and restart the app on every check for the same result.
OUTCOME_NO_CHANGE = "no_change"
_SETTLED = frozenset({OUTCOME_NEEDS_CONSENT, OUTCOME_NO_CHANGE})
#: Failed tries at one published version before it settles. Each try stops and
#: restarts the app, so a refusal that never changes must not repeat forever;
#: a newer published version is tried afresh.
MAX_FAILED_ATTEMPTS = 3

#: First check after gateway start. Long enough that boot work (app enables,
#: cron registration, the first catalog fetch) has finished.
FIRST_CHECK_DELAY_SECS = 300

#: How often the loop re-reads the config. A check runs only once its interval
#: has passed, so this bounds how long a setting change takes to apply, not how
#: often registries are fetched.
POLL_SECS = 900

#: Time between two checks: every 6 hours. A recorded decision keeps this out of
#: the settings (``docs/decisions/``, installed apps update on one setting).
CHECK_INTERVAL_SECS = 6 * 3600

NOTE_KIND = "update"
NOTE_CHANNEL = "system.apps"


def effective_mode(config: Any, name: str) -> str:
    """``off``, ``on`` or ``never`` for the app *name* under *config*.

    Total: a config without the sections, or an entry of the wrong shape, reads
    as the global mode, and an unreadable global mode as ``off``.
    """
    apps = getattr(config, "apps", None)
    entry = apps.get(name) if isinstance(apps, dict) else None
    override = getattr(entry, "auto_update", "")
    if override == "never":
        return "never"
    if override == "always":
        return "on"
    mode = getattr(getattr(config, "app_updates", None), "mode", "off")
    return "on" if mode == "on" else "off"


def checks_wanted(config: Any) -> bool:
    """Whether any app could be updated automatically under *config*."""
    if effective_mode(config, "") == "on":
        return True
    apps = getattr(config, "apps", None)
    return isinstance(apps, dict) and any(
        getattr(entry, "auto_update", "") == "always" for entry in apps.values()
    )


def check_interval_secs() -> int:
    """:data:`CHECK_INTERVAL_SECS`, read where the loop decides a check is due."""
    return CHECK_INTERVAL_SECS


@dataclass(frozen=True)
class Candidate:
    """One installed app with a newer published version."""

    name: str
    installed: str
    published: str


def _eligible(app: dict[str, Any]) -> bool:
    from kiro_crew.apps.registry import is_registry_source

    return (
        app.get("lifecycle", "gateway") == "gateway"
        and isinstance(app.get("source"), str)
        and is_registry_source(app["source"])
    )


def eligible_names() -> set[str]:
    """Installed apps this module could update at all, whatever their mode.

    Blocking (reads the installed records): run it off the loop.
    """
    from kiro_crew.apps.manager import list_apps

    return {
        app["name"]
        for app in list_apps()
        if isinstance(app.get("name"), str) and app["name"] and _eligible(app)
    }


def judge(app: dict[str, Any], config: Any, index: dict[str, Any]) -> Candidate | str:
    """*app* as a candidate under *config* and the published *index*, or why it is not one.

    The one test of candidacy. :func:`find_candidates` keeps what it passes, and
    :func:`still_candidate_reason` applies it again under the lifecycle lock, so
    no fact judged when the check began can change unseen before the backend
    stops.
    """
    from kiro_crew.apps.registry_pipeline.catalog import _version_newer

    name = app.get("name")
    if not isinstance(name, str) or not name:
        return "its record has no name"
    if not _eligible(app):
        return "its source or lifecycle does not update automatically"
    if effective_mode(config, name) != "on":
        return "its update setting is not automatic"
    # Through ``publishable_version``: both versions end up in a note the
    # gateway speaks, and the installed one is manifest text too.
    installed = publishable_version(app.get("version"))
    published = publishable_version((index.get(name) or ("", None))[0])
    if not (published and installed and _version_newer(published, installed)):
        return "no newer version is published"
    return Candidate(name, installed, published)


def find_candidates(config: Any) -> list[Candidate]:
    """Apps whose effective mode is ``on`` and that have a newer published version.

    Blocking (reads the installed records and the store caches): run it off
    the loop. Reads caches only; :func:`refresh_published` is what makes them
    current.
    """
    from kiro_crew.apps.manager import list_apps
    from kiro_crew.apps.update_status import published_index

    index = published_index()
    verdicts = (judge(app, config, index) for app in list_apps())
    return [verdict for verdict in verdicts if isinstance(verdict, Candidate)]


def still_candidate_reason(candidate: Candidate) -> str:
    """Why *candidate* is not what :func:`find_candidates` would pick now, or ``""``.

    Reads the saved config, the installed record and the published index again
    and applies :func:`judge` to them. A Never or Off saved, an Update pressed
    (the installed version moved), an Update from a local checkout with a
    ``source`` override (the record names a local path, so the Update sequence
    would take its directory branch) and an uninstall are each a
    reason. A config that cannot be read is one too: an update is never applied
    on a guess. Blocking: run it off the loop.
    """
    from kiro_crew.apps.manager import list_apps
    from kiro_crew.apps.update_status import published_index
    from kiro_crew.config import KiroCrewConfig

    try:
        config = KiroCrewConfig.load()
    except Exception:
        logger.warning(
            "app auto-update: could not re-read the config for %s", candidate.name, exc_info=True
        )
        return "its update setting could not be read"
    record = next((app for app in list_apps() if app.get("name") == candidate.name), None)
    if record is None:
        return "it is no longer installed"
    installed = publishable_version(record.get("version"))
    if installed != candidate.installed:
        return f"its installed version changed to {installed or 'none'} since this check began"
    verdict = judge(record, config, published_index())
    if isinstance(verdict, str):
        return verdict
    if verdict != candidate:
        return f"its published version changed to {verdict.published} since this check began"
    return ""


async def refresh_published() -> None:
    """Bring the catalog and every registry index up to date.

    The catalog is read through ``list_catalog_apps``, which refetches once its
    cache is past ``CACHE_TTL`` (one hour, never longer than a check interval),
    exactly as the store's own listing does: a fetch that fails past the TTL
    leaves the failure marker, ``published_index`` then names no catalog app,
    and nothing is updated. Discover's refresh button also drops a cache that is
    still within its TTL; doing that here would throw away a good catalog on
    every check the network is down. Registries refetch and swap
    only on success (``refresh_registries``). Either way a check with no
    network compares against what was last seen.
    """
    from kiro_crew.apps.registry import list_catalog_apps, refresh_registries

    try:
        await list_catalog_apps()
    except Exception:
        logger.warning("app auto-update: catalog refresh failed", exc_info=True)
    try:
        await refresh_registries(None)
    except Exception:
        logger.warning("app auto-update: registry refresh failed", exc_info=True)


def busy_reason(state: Any, name: str) -> str:
    """Why *name* cannot be updated right now, or ``""`` when it is idle.

    Call ON the loop: the session half reads the session manager's turn
    leases and the hook half reads asyncio tasks, and the loop owns both. The
    cron job list comes from the job store, whose lock refuses a thread with a
    running loop, so :func:`cron_job_ids` reads it off the loop and
    :func:`cron_busy_reason` checks it against the running claims on the loop.
    :func:`job_busy_reason` runs off it too. :func:`idle_reason` runs them all.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is not None and app_turn_running(sessions, name):
        return "a session it owns is running a turn"

    from kiro_crew.apps.lifecycle import LifecycleDispatcher

    if LifecycleDispatcher.startup_hooks_running(name):
        return "its startup hook is still running"
    return ""


def app_turn_running(sessions: Any, name: str) -> bool:
    """Whether a session *name* owns holds its turn lease. On the loop.

    The owner is the provider's ``owning_app``, which the session manager
    stamps on every session an app's work runs in: a dashboard or worker slot
    the app owns, a ``ctx.turns.run`` turn, and a subagent spawned under the
    app's governance. A turn holds the lease from its claim to its end, so this
    one read sees all of them. A key still allocating has no provider yet, and
    its turn has not reached the agent.
    """
    for key in sessions.session_keys():
        if not sessions.is_busy(key):
            continue
        if getattr(sessions.get_provider(key), "owning_app", "") == name:
            return True
    return False


def cron_job_ids(state: Any, name: str) -> tuple[str, ...] | None:
    """The ids of *name*'s cron jobs, or ``None`` when they could not be read. Off the loop."""
    crons = getattr(state, "crons", None)
    if crons is None:
        return ()
    from kiro_crew.apps.cron_sdk import CronSDK

    try:
        jobs = CronSDK(name, crons).list_jobs()
    except Exception:
        logger.warning("app auto-update: could not list %s's cron jobs", name, exc_info=True)
        return None
    return tuple(getattr(job, "id", "") for job in jobs)


def cron_busy_reason(state: Any, job_ids: tuple[str, ...] | None) -> str:
    """``busy_reason``'s cron half: one of the app's cron jobs is running. On the loop.

    *job_ids* is :func:`cron_job_ids`'s answer. The cron service claims a run in
    a loop step, so the claims are read here, on the loop.
    """
    if job_ids is None:
        # Unknown is busy: an update that interrupts a job it could not see
        # is the outcome this check exists to prevent.
        return "its cron jobs could not be read"
    crons = getattr(state, "crons", None)
    if crons is not None and any(crons.is_running(job_id) for job_id in job_ids):
        return "one of its cron jobs is running"
    return ""


def job_busy_reason(name: str) -> str:
    """``busy_reason``'s Job SDK half: this process runs one of *name*'s jobs. Off the loop.

    An update that changes the app's hook code has the hook reconciler tear the
    old hooks down through ``on_app_disable``, which cancels every run the
    app's ``JobSDK`` holds live and deletes its record. ``live`` is what is
    read, not ``list_active``: a record left
    ``running`` that nothing in this process owns is not stopped by that
    cleanup, and counting it would hold the app at its version for good.
    Off the loop because ``_persist`` holds the SDK's lock across a disk write.
    """
    from kiro_crew.apps.job_sdk import get_sdk

    sdk = get_sdk(name)
    if sdk is None:
        return ""
    try:
        _cancelling, live = sdk.cancelling_and_live_ids()
    except Exception:
        logger.warning("app auto-update: could not read %s's running jobs", name, exc_info=True)
        return "its jobs could not be read"
    return "one of its jobs is running" if live else ""


async def idle_reason(state: Any, name: str) -> str:
    """Why *name* is busy now, from every half of the idle check, or ``""``.

    The two reads that run off the loop are awaited first. What the loop
    holds, the app's turns, its startup hooks and its cron claims, is read
    after them in the step that returns the answer, so work that starts on the
    loop while those reads run is still seen. A Job SDK run starts in a worker
    thread, so its read is the last one awaited.
    """
    job_ids = await asyncio.to_thread(cron_job_ids, state, name)
    jobs = await asyncio.to_thread(job_busy_reason, name)
    return jobs or busy_reason(state, name) or cron_busy_reason(state, job_ids)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    from kiro_crew.config.loader import config_dir

    return config_dir() / LEDGER_FILENAME


#: Every outcome this process recorded, kept in memory as well as on disk. A
#: ledger write that fails (a full or read-only data home) must not turn a
#: settled version back into a candidate: the next check would rebuild and
#: restart the app and post the same note again, every interval. The disk copy
#: is what survives a restart; this copy is what survives a failed write.
_RECORDED: dict[str, dict[str, str]] = {}


def read_ledger() -> dict[str, dict[str, str]]:
    """``name -> {"version", "outcome"[, "attempts"]}``, this process's records over the file's.

    An unreadable file reads as empty, which fails toward one repeated note,
    never toward a silent skip.
    """
    on_disk: dict[str, dict[str, str]] = {}
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        on_disk = {
            k: {
                "version": str(v.get("version", "")),
                "outcome": str(v.get("outcome", "")),
                **({"attempts": str(v["attempts"])} if "attempts" in v else {}),
            }
            for k, v in data.items()
            if isinstance(k, str) and isinstance(v, dict)
        }
    return {**on_disk, **{k: dict(v) for k, v in _RECORDED.items()}}


def record(name: str, version: str, outcome: str, *, attempts: int = 0) -> None:
    """Store *outcome* for *name* at *version*, in memory and on disk. Never raises.

    *attempts* counts the failed tries at *version*; it is stored only for ``failed``.
    """
    from kiro_crew.atomic_write import atomic_write

    entry = {"version": version, "outcome": outcome}
    if attempts:
        entry["attempts"] = str(attempts)
    _RECORDED[name] = entry
    ledger = read_ledger()
    try:
        atomic_write(_ledger_path(), json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    except OSError:
        logger.warning(
            "app auto-update: could not write the ledger; this run remembers %s %s",
            name,
            version,
            exc_info=True,
        )


def failed_attempts(ledger: dict[str, dict[str, str]], candidate: Candidate) -> int:
    """How many automatic updates to this published version have failed so far."""
    if not already_noted(ledger, candidate, OUTCOME_FAILED):
        return 0
    try:
        return max(1, int(ledger[candidate.name].get("attempts", "1")))
    except (TypeError, ValueError):
        return 1


def already_settled(ledger: dict[str, dict[str, str]], candidate: Candidate) -> bool:
    """Whether this published version already ended in a final outcome.

    A ``failed`` version settles after :data:`MAX_FAILED_ATTEMPTS`: a refusal
    that recurs every time (an admission denial, a withdrawn trust grant) would
    otherwise stop and restart the app on every check, with no end.
    """
    entry = ledger.get(candidate.name)
    if not entry or entry.get("version") != candidate.published:
        return False
    if entry.get("outcome") in _SETTLED:
        return True
    return failed_attempts(ledger, candidate) >= MAX_FAILED_ATTEMPTS


def already_noted(ledger: dict[str, dict[str, str]], candidate: Candidate, outcome: str) -> bool:
    entry = ledger.get(candidate.name)
    return bool(
        entry and entry.get("version") == candidate.published and entry.get("outcome") == outcome
    )


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def _detail_url(name: str) -> str:
    return f"/apps/detail/{name}"


def updated_note(candidate: Candidate, installed: str) -> dict[str, Any]:
    """*installed* is the version the app reports after the update, not the index's.

    No ``latestVersion`` in ``meta``: that key is what ``update_notify._caught_up``
    retires a note by once its version is installed, and this note's version is
    installed the moment it is sent, so the next store listing would mark it read.
    """
    return {
        "title": f"{candidate.name} updated to {installed}",
        "body": f"Updated automatically from {candidate.installed}." if candidate.installed else "",
        "meta": {
            "app": candidate.name,
            "installedVersion": candidate.installed,
            "automatic": True,
        },
        "url": _detail_url(candidate.name),
    }


def no_change_note(candidate: Candidate, installed: str) -> dict[str, Any]:
    """The note for an update that ran and left the version where it was."""
    return {
        "title": f"{candidate.name} {candidate.published} could not be installed",
        "body": (
            (
                f"Its source still has version {installed}, so "
                if installed
                else "Its source has no newer version than the one installed, so "
            )
            + "it will not be updated automatically until a newer version is published."
        ),
        "meta": {
            "app": candidate.name,
            "latestVersion": candidate.published,
            "installedVersion": installed or candidate.installed,
            "automatic": True,
            "failed": True,
        },
        "url": _detail_url(candidate.name),
    }


def _installed_version(name: str) -> str:
    """The version the installed record reports now. Blocking."""
    from kiro_crew.apps.manager import list_apps

    for app in list_apps():
        if app.get("name") == name:
            return publishable_version(app.get("version"))
    return ""


#: Why an update waits for the owner while their memory consent names the
#: installed version: the update would end that consent and ask again.
MEMORY_CONSENT_REFUSAL = "updating it would end the memory consent you gave this version"


def memory_consent_reason(name: str) -> str:
    """:data:`MEMORY_CONSENT_REFUSAL` while an update of *name* would end a memory consent.

    Blocking (reads the consent record and the installed manifest): run it off the loop.
    """
    from kiro_crew.memory_provider.consent import update_would_end_consent

    return MEMORY_CONSENT_REFUSAL if update_would_end_consent(name) else ""


def consent_note(candidate: Candidate, detail: str) -> dict[str, Any]:
    """The needs-consent note for an update held back for the owner.

    *detail* is the gateway's own refusal text: ``update_consent.consent_refusal``,
    which names only permission keys, ``UNREADABLE_BASELINE_REFUSAL`` or
    :data:`MEMORY_CONSENT_REFUSAL`. It never carries registry-authored content.
    """
    # The reason leads both lines: the feed cuts a row at its end, and the owner
    # decides on the button from what is still showing.
    from kiro_crew.apps.update_consent import consent_refusal

    prefix = consent_refusal([])
    if detail.startswith(prefix):
        title = f"{candidate.name} {candidate.published} wants new permissions"
        body = f"Adds: {detail[len(prefix):]}. Review to update."
    else:
        title = f"{candidate.name} {candidate.published} needs your review"
        body = f"{detail[:1].upper()}{detail[1:]}. Review to update."
    return {
        "title": title,
        "body": body,
        "meta": {
            "app": candidate.name,
            "latestVersion": candidate.published,
            "installedVersion": candidate.installed,
            "automatic": True,
            "needsConsent": True,
        },
        "url": _detail_url(candidate.name),
        # The button opens the app's page and starts nothing: the owner presses
        # Update there, where its consent dialog shows what the version asks for.
        "actions": [{"id": "review", "label": "Review update", "url": _detail_url(candidate.name)}],
    }


def failed_note(candidate: Candidate, error: str) -> dict[str, Any]:
    """The note for a failed automatic update. The old version is still running.

    *error* can carry text a registry or build step produced, so it is redacted
    here the way the registry install route redacts its error, before the
    delivery redaction every note gets, and cut to a length a note can show.
    """
    from kiro_crew.security import redact

    shown = " ".join(redact(error).split())[:300] or "unknown error"
    return {
        "title": f"{candidate.name} {candidate.published} could not be installed",
        "body": f"{candidate.installed or 'The installed version'} is still running. {shown}",
        "meta": {
            "app": candidate.name,
            "latestVersion": candidate.published,
            "installedVersion": candidate.installed,
            "automatic": True,
            "failed": True,
        },
        "url": _detail_url(candidate.name),
    }


def deliver(state: Any, note: dict[str, Any]) -> None:
    """Send *note* through ``state.notify``. Call on the serving loop; never raises."""
    try:
        state.notify(
            NOTE_KIND,
            note["title"],
            note["body"],
            meta=note["meta"],
            url=note["url"],
            actions=note.get("actions"),
            channel=NOTE_CHANNEL,
        )
    except Exception:
        logger.warning("app auto-update: could not deliver a note", exc_info=True)


# ---------------------------------------------------------------------------
# One check
# ---------------------------------------------------------------------------


async def update_one(state: Any, candidate: Candidate) -> str:
    """Apply *candidate* through the Update button's sequence. Returns the outcome.

    ``""`` means it was put off (busy, or a retryable refusal such as a startup
    hook) and nothing was recorded.
    """
    from kiro_crew.apps.routes import _announce_app_ui_changed_to, apply_app_update
    from kiro_crew.apps.update_consent import NEEDS_CONSENT_CODE, NOT_NEWER_CODE

    reason = stop_reason() or restart_hold_reason()
    if reason:
        logger.info("app auto-update: %s put off because %s", candidate.name, reason)
        return ""
    held = await asyncio.to_thread(memory_consent_reason, candidate.name)
    if held:
        await asyncio.to_thread(record, candidate.name, candidate.published, OUTCOME_NEEDS_CONSENT)
        deliver(state, consent_note(candidate, held))
        return OUTCOME_NEEDS_CONSENT
    reason = await idle_reason(state, candidate.name)
    if reason:
        logger.info("app auto-update: %s put off because %s", candidate.name, reason)
        return ""

    async def _still_idle() -> str:
        # Under the lifecycle lock, just before the backend stops. The candidate
        # list was chosen when the check began, and anything saved or installed
        # while an earlier app built can have changed it, so the same judge
        # runs again here. A changed answer puts the update off without
        # settling the version; the next check picks the candidates again from
        # what is saved and installed then. The stop flag and the restart hold
        # are read after the last await, and ``idle_reason`` ends with its own
        # reads of what the loop holds, so nothing awaited sits between those
        # reads and the answer. A gateway that began stopping while this app
        # cloned, built or made these reads must not take its backend down,
        # since exit would cut the swap that follows. This app is already in
        # ``_APPLYING``, so a stop or a restart that comes after the two reads
        # sees the update applying and waits for it.
        busy = (
            await asyncio.to_thread(still_candidate_reason, candidate)
            or await asyncio.to_thread(memory_consent_reason, candidate.name)
            or await idle_reason(state, candidate.name)
        )
        return busy or stop_reason() or restart_hold_reason()

    _APPLYING.add(candidate.name)
    try:
        status, body = await apply_app_update(
            candidate.name,
            cron_service=getattr(state, "crons", None),
            state=state,
            caller="app_auto_update",
            refuse_permission_widening=True,
            still_idle=_still_idle,
        )
    finally:
        _APPLYING.discard(candidate.name)
    if status < 400 and body.get("ok", True):
        from kiro_crew.apps.registry_pipeline.catalog import _version_newer

        # The files were replaced, so an open tab drops the old UI module, as it
        # does after the Update button.
        _announce_app_ui_changed_to(state, candidate.name)
        # As after every update route: a Bot chat whose app tools the new
        # version moved catches up on its next turn.
        from kiro_crew import bot_chat_app_tools

        await bot_chat_app_tools.after_app_change(state)
        # Judged by what is installed now, not by what the index advertised:
        # the clone can carry an older app.json than the index row says.
        now = await asyncio.to_thread(_installed_version, candidate.name)
        if not (now and _version_newer(now, candidate.installed)):
            await asyncio.to_thread(record, candidate.name, candidate.published, OUTCOME_NO_CHANGE)
            deliver(state, no_change_note(candidate, now))
            return OUTCOME_NO_CHANGE
        # An update that moved forward but stopped short of the advertised
        # version settles that version as ``no_change``: the next check would
        # otherwise clone, rebuild and restart the app only to be refused.
        reached = not _version_newer(candidate.published, now)
        outcome = OUTCOME_UPDATED if reached else OUTCOME_NO_CHANGE
        await asyncio.to_thread(record, candidate.name, candidate.published, outcome)
        deliver(state, updated_note(candidate, now))
        return OUTCOME_UPDATED
    if body.get("retryable"):
        logger.info(
            "app auto-update: %s put off: %s", candidate.name, body.get("error", "retryable")
        )
        return ""
    if body.get("code") == "app_not_installed":
        # Uninstalled while this check ran (the owner won the lifecycle lock):
        # nothing to update and nothing to tell anyone.
        return ""
    if body.get("code") == NOT_NEWER_CODE:
        # Refused before any file moved: the source holds this version or an
        # older one, so installing it would be a downgrade or a no-op.
        await asyncio.to_thread(record, candidate.name, candidate.published, OUTCOME_NO_CHANGE)
        deliver(state, no_change_note(candidate, ""))
        return OUTCOME_NO_CHANGE
    ledger = await asyncio.to_thread(read_ledger)
    error = str(body.get("error") or "")
    if body.get("code") == NEEDS_CONSENT_CODE:
        await asyncio.to_thread(record, candidate.name, candidate.published, OUTCOME_NEEDS_CONSENT)
        deliver(state, consent_note(candidate, error))
        return OUTCOME_NEEDS_CONSENT
    noted = already_noted(ledger, candidate, OUTCOME_FAILED)
    attempts = failed_attempts(ledger, candidate) + 1
    await asyncio.to_thread(
        record, candidate.name, candidate.published, OUTCOME_FAILED, attempts=attempts
    )
    if not noted:
        deliver(state, failed_note(candidate, error))
    logger.warning("app auto-update: %s failed: %s", candidate.name, error)
    return OUTCOME_FAILED


async def run_check(state: Any, config: Any) -> dict[str, str]:
    """Refresh, find the due updates and apply them one at a time.

    Returns ``name -> outcome`` for the apps it acted on (``""`` for put off).
    One at a time because each update stops and restarts an app's backend and
    may build it; running several at once would multiply that load for no gain.
    """
    if not checks_wanted(config):
        return {}
    await refresh_published()
    candidates = await asyncio.to_thread(find_candidates, config)
    ledger = await asyncio.to_thread(read_ledger)
    outcomes: dict[str, str] = {}
    for candidate in candidates:
        if _STOP["requested"]:
            break
        if already_settled(ledger, candidate):
            continue
        try:
            outcomes[candidate.name] = await update_one(state, candidate)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("app auto-update: %s raised", candidate.name)
            outcomes[candidate.name] = OUTCOME_FAILED
    return outcomes


async def run_forever(state: Any) -> None:
    """The gateway's app update loop: a check now and then, every interval.

    Reads the config on every pass, so turning the setting on or off takes
    effect at the next wake without a restart.
    While nothing is set to update, a pass costs one config read and no
    network.
    """
    from kiro_crew.config import KiroCrewConfig

    await _sleep(FIRST_CHECK_DELAY_SECS)
    loop = asyncio.get_running_loop()
    last_check: float | None = None
    while True:
        try:
            config = await asyncio.to_thread(KiroCrewConfig.load)
            due = last_check is None or loop.time() - last_check >= check_interval_secs()
            if _STOP["requested"]:
                return
            if checks_wanted(config) and due:
                last_check = loop.time()
                await run_check(state, config)
                if _STOP["requested"]:
                    # Shutdown drains this task; ending here lets it finish as
                    # soon as the update in flight does.
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("app auto-update check failed")
        await _sleep(POLL_SECS)
