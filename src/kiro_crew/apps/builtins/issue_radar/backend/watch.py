"""Issue Radar — in-process watcher: new issues, and the crews' unblock signals.

A single asyncio background loop, started from ``register_routes(app)`` via
``app.on_startup`` and cancelled on ``app.on_cleanup``. Every
:data:`POLL_INTERVAL_SEC` seconds it makes two passes over the connected repos:

  1. **new-issue notification** — each repo that has opted in (per-repo setting
     ``notify_on_new_issue``) is checked for issues created since the last check,
     and a Kiro Crew dashboard notification is pushed (``state.notify`` — the bell,
     persisted to the notification history) when new ones appear;
  2. **crew sweep** (:func:`crew_runtime.sweep_repo`) — every repo with a live crew
     has its crews' open work items compared against the six unblock signals, and
     the owning crew is woken when one moves.

THIS IS THE ONLY ALWAYS-ON LOOP IN THE APP, which is why the crew sweep lives here
rather than in the route layer. Everything else — including ``routes.py``'s
probe-coalescing and every cache refresh — is driven by a frontend HTTP request, so
with no dashboard tab open none of it runs and none of its caches are fresh. A crew
must keep working with the browser closed.

This is NOT a cron job: it lives entirely inside the gateway process and only
runs while KiroCrew is running (and the machine is awake). It is also
zero-LLM/zero-token — it shells out to the user's existing ``gh`` session
(``github_client.list_recent_open_issues``) and compares issue numbers against a
per-repo high-water mark stored in ``watch-state.json``.

Fail-safe / best-effort by design:
  * a disabled app is silent, crews included — revoked inside the disable request
    by ``crew_runtime.on_app_disabled``, with the ``is_app_enabled`` gate below as
    the backstop for a disable this process was never told about;
  * only repos with ``notify_on_new_issue`` set are polled for NEW ISSUES — the
    crew sweep deliberately does not inherit that gate (see :func:`_poll_once`);
  * any ``gh``/network error skips that repo for the cycle and leaves its mark
    untouched, so nothing is missed and nothing is double-reported;
  * one bad cycle never kills the loop.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime, provider, store
from kiro_crew.apps.manager import is_app_enabled

logger = logging.getLogger("kirocrew.app.issue-radar")

APP_NAME = "issue-radar"

# How often to poll. The user asked for ~1 minute; GitHub's authenticated rate
# limit (5000/hr) leaves ample headroom for a handful of opted-in repos at one
# cheap single-page call each per minute.
POLL_INTERVAL_SEC = 60

# Most-recently-created open issues pulled per poll. Issue creation almost never
# exceeds this within one interval; if it ever does, the high-water mark still
# advances past the burst (so nothing re-notifies later) — the overflow is just
# not enumerated in that one notification.
_POLL_LIMIT = 30

# How many issue titles to enumerate in a single multi-issue notification.
_NOTIFY_LIST_CAP = 4

# Module-level strong ref so the task isn't garbage-collected mid-flight.
_watch_task: asyncio.Task | None = None

# Whether the crews have already been suspended for the CURRENT disabled stretch.
# Nothing re-establishes trust or re-arms a loop while the app is off, so the
# suspension only has to happen on the enabled -> disabled edge.
_crews_suspended = False


async def start_watcher(app: web.Application) -> None:
    """``app.on_startup`` hook — launch the single background poll loop.

    Idempotent: a second call while a loop is already running is a no-op.
    """
    global _watch_task
    if _watch_task is not None and not _watch_task.done():
        return
    _watch_task = asyncio.create_task(_watch_loop(app), name="issue-radar-watch")
    logger.info("issue-radar watcher started (every %ds)", POLL_INTERVAL_SEC)


async def stop_watcher(app: web.Application) -> None:
    """``app.on_cleanup`` hook — cancel the loop on gateway shutdown."""
    global _watch_task
    task = _watch_task
    _watch_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover - defensive
        logger.debug("issue-radar watcher shutdown raised", exc_info=True)


async def _watch_loop(app: web.Application) -> None:
    # Register the crew dismissal hook BEFORE the first sleep. The watchdog also
    # installs it every cycle (idempotent, and its restart backstop), but that
    # first cycle is a full POLL_INTERVAL_SEC away, and until the hook exists a
    # deliberate ✕ on a crew's session is silently ignored and the sweep then
    # resurrects the crew — the exact bug the hook was added to prevent, live for
    # a minute after every gateway start. Registration is a dict write with no
    # subprocess, so it does not undo the startup-cost reason for the delay below.
    try:
        crew_runtime.install_slot_close_hook()
    except Exception:  # pragma: no cover - defensive; never block the loop
        logger.debug("issue-radar crew close hook install failed", exc_info=True)
    # Same timing argument, and a sharper consequence: until the disable hook is
    # registered, switching the app off only writes a flag, and the crews keep
    # their auto-approve grants until this loop next wakes. Registered here so the
    # window does not exist for the first minute of every gateway's life either.
    #
    # Announced only from here — once per process — because the watchdog
    # re-registers every cycle and a warning there would be a log flood.
    try:
        if not crew_runtime.install_app_disable_hook(app.get("state")):
            logger.warning(
                "issue-radar: this gateway has no app-disable hook registry, so "
                "disabling the app stops the crews on the next %ds sweep rather "
                "than immediately",
                POLL_INTERVAL_SEC,
            )
    except Exception:  # pragma: no cover - defensive; never block the loop
        logger.debug("issue-radar crew disable hook install failed", exc_info=True)
    # First tick waits a full interval so gateway startup isn't hit with gh
    # subprocesses the moment it comes up.
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            await _poll_once(app)
        except asyncio.CancelledError:
            break
        except Exception:  # never let one bad cycle kill the loop
            logger.warning("issue-radar watch cycle failed", exc_info=True)


async def _poll_once(app: web.Application) -> None:
    """One sweep across every connected repo.

    TWO independent passes per repo, and the difference between their gates is
    deliberate:

    * the new-issue notification, which only runs for repos that opted in via
      ``notify_on_new_issue``;
    * the crew sweep, which runs for every connected repo that has a live crew.
      ``notify_on_new_issue`` is a NOTIFICATION preference — whether the bell rings
      — not a fetch switch, and a crew that stopped reconciling its own pull
      requests because the user muted a bell would stall every open item with no
      trace of why. The app's enabled gate below is the switch that stops crews.
    """
    # A disabled app must be silent (the user turned Issue Radar off) — and that
    # includes the crews, whose whole runtime hangs off this loop. Returning here
    # is not enough on its own: a crew's auto-approval lives on its slot and its
    # nudge loop fires regardless of any app's enabled flag, so both have to be
    # taken away explicitly.
    #
    # THE BACKSTOP, not the first line. ``crew_runtime.on_app_disabled`` revokes
    # inside the disable request itself, so an operator using the dashboard or the
    # API has stopped crews by the time it returns. This branch exists for the
    # disables nothing can tell us about: ``kirocrew app disable`` in another
    # process, an ``installed.json`` edited on disk, and a gateway restart that
    # re-armed loops for an app that is off. Both paths are needed, and the
    # difference between them is a poll interval of unattended, auto-approved turns.
    #
    # In-memory only (no config read, no gh), and done once per disable transition
    # since nothing re-establishes trust or a loop while the app is off.
    global _crews_suspended
    if not await asyncio.to_thread(is_app_enabled, APP_NAME):
        if not _crews_suspended:
            _crews_suspended = True
            try:
                await crew_runtime.suspend_crews(app.get("state"))
            except Exception:
                logger.warning("issue-radar: suspending crews failed", exc_info=True)
        return
    _crews_suspended = False
    repos = await asyncio.to_thread(store.list_connected_repos)
    for entry in repos:
        owner = entry.get("owner")
        repo = entry.get("repo")
        if not owner or not repo:
            continue
        # Each entry carries its own provider+host, so a mixed GitHub/GitLab
        # install polls every repo against the right server.
        key = provider.key_from_parts(
            str(owner), str(repo), entry.get("provider"), entry.get("host")
        )
        settings = await asyncio.to_thread(
            partial(
                store.read_repo_settings, key.owner, key.repo,
                provider=key.provider, host=key.host,
            )
        )
        if settings.get("notify_on_new_issue"):
            try:
                await _poll_repo(app, key)
            except Exception:
                # Per-repo failure (CLI/network/etc.) must not stop the sweep.
                logger.warning(
                    "issue-radar watch failed for %s/%s", owner, repo, exc_info=True
                )
        try:
            await crew_runtime.sweep_repo(app, key)
        except Exception:
            # Same containment: one repo's crews failing must not stop the others,
            # and must not take the new-issue notification down with them.
            logger.warning(
                "issue-radar crew sweep failed for %s/%s", owner, repo, exc_info=True
            )


async def _poll_repo(app: web.Application, key: provider.RepoKey) -> None:
    """Detect issues opened in ``key``'s repo since the last poll and notify."""
    owner, repo = key.owner, key.repo
    scope = store.provider_root(root=None, provider=key.provider, host=key.host)
    recent = await asyncio.to_thread(
        partial(
            provider.client_for(key).list_recent_open_issues,
            owner, repo, _POLL_LIMIT, **provider.call_kwargs(key),
        )
    )
    numbers = [int(i["number"]) for i in recent if isinstance(i.get("number"), int)]
    if not numbers:
        return
    current_max = max(numbers)

    state = await asyncio.to_thread(store.read_watch_state, owner, repo, scope)
    last_seen = state.get("last_seen_number")

    if not isinstance(last_seen, int):
        # First observation for this repo: seed the high-water mark WITHOUT
        # notifying, so we don't announce the entire pre-existing backlog.
        await asyncio.to_thread(
            partial(store.write_watch_state, owner, repo, current_max, root=scope)
        )
        return

    # Issue numbers are monotonic per repo on GitHub and per project on GitLab
    # (the ``iid``), so anything above the mark was created since the last poll.
    new_issues = [i for i in recent if int(i["number"]) > last_seen]
    if not new_issues:
        return

    # Advance the mark BEFORE notifying so a delivery hiccup can't cause the same
    # issues to be re-announced next cycle.
    await asyncio.to_thread(
        partial(store.write_watch_state, owner, repo, current_max, root=scope)
    )
    _notify_new_issues(app, key, new_issues)


def _notify_new_issues(
    app: web.Application, key: provider.RepoKey, new_issues: list[dict[str, Any]]
) -> None:
    """Push one dashboard notification summarizing the new issues for a repo."""
    owner, repo = key.owner, key.repo
    # GitLab issue pages live under /-/issues, GitHub's under /issues, so the
    # fallback link is built from the key rather than hard-coded to github.com.
    issues_url = (
        f"{key.web_url()}/-/issues" if not key.is_github else f"{key.web_url()}/issues"
    )
    state = app.get("state")
    if state is None:  # gateway state not attached (shouldn't happen post-startup)
        return

    ordered = sorted(new_issues, key=lambda i: int(i["number"]))
    count = len(ordered)
    title = f"Issue Radar · {owner}/{repo}"

    if count == 1:
        iss = ordered[0]
        body = f"New issue #{iss['number']}: {iss.get('title') or '(no title)'}"
        url = iss.get("url") or issues_url
    else:
        shown = ordered[:_NOTIFY_LIST_CAP]
        listed = ", ".join(
            f"#{i['number']} {(i.get('title') or '').strip()}".strip() for i in shown
        )
        body = f"{count} new issues in {owner}/{repo}: {listed}"
        remaining = count - len(shown)
        if remaining > 0:
            body += f" +{remaining} more"
        url = issues_url

    try:
        # state.notify never raises (it swallows validation errors), but guard
        # anyway so notification delivery can never bubble into the poll loop.
        state.notify(
            "issue-radar",
            title,
            body,
            meta={"app": "issue-radar", "owner": owner, "repo": repo, "url": url, "count": count},
        )
        logger.info("issue-radar notified %d new issue(s) in %s/%s", count, owner, repo)
    except Exception:  # pragma: no cover - defensive
        logger.debug("issue-radar notify failed for %s/%s", owner, repo, exc_info=True)
