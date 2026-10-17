"""Issue Radar — CI-failure alerts for the signed-in user's own pull requests.

One pass of the in-process watcher (``watch._poll_once``), run only for the
connected GitHub repos whose per-repo setting ``notify_on_own_pr_ci_failure`` is
on. With no repo opted in it returns before touching ``gh``, so the watcher's
calls and notifications are those of the new-issue pass and the crew sweep alone.

Each cycle:

  1. one GraphQL search (per few repos, read page by page) lists the viewer's
     open PRs in the opted-in repos, with each head commit
     (``search_viewer_open_pull_heads``); repos whose search is cut off at its
     page cap are skipped for the cycle, with a warning;
  2. for each head commit, the Actions runs on it are read with the same client
     the PR pane's run actions use (``list_pr_workflow_runs``);
  3. a run that concluded ``failure`` or ``timed_out`` at an attempt not yet
     reported produces one dashboard notification per PR, on this app's
     ``ci-failure`` channel, so the channel's mute and priority settings in
     Settings → Notifications apply to it.

Reported attempts are kept per repo in ``ci-watch-state.json`` (see
``store.read_ci_watch_state``). The first successful read of a repo seeds that
record without notifying, so turning the setting on does not announce the red
runs already present. A head commit whose runs had all completed at its last read
is re-read every :data:`SETTLED_RECHECK_SEC` rather than every cycle; that keeps
a user with many quiet open PRs from spending a request per PR per minute, and
still catches a re-run within a few minutes.

Read-only and GitHub-only: it uses the user's existing ``gh`` session and never
writes to the provider. Any ``gh`` error skips the affected repo for the cycle
and leaves its record untouched, so nothing is lost or reported twice. A note
the rate limiter refuses, or that the notification history fails to persist, is
left unrecorded, so the next cycle offers it again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import github_client, provider, store
from kiro_crew.apps.manager import app_lifecycle_lock
from kiro_crew.dashboard.handlers import notifications_push
from kiro_crew.notifications.bus import NotificationPayload, NotificationValidationError

logger = logging.getLogger("kirocrew.app.issue-radar")

APP_NAME = "issue-radar"

#: Channel id, mirroring ``app.json``'s ``notifications.channels``. The bus
#: namespaces it as ``issue-radar.ci-failure``.
CHANNEL_CI_FAILURE = "ci-failure"

#: The dashboard page the notification opens. Path-only: the bus refuses any
#: external link, so the run and PR URLs travel in the note's metadata.
_APP_URL = "/issue-radar"

#: Run conclusions that count as a failure.
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})

#: Seconds between re-reads of a head commit whose runs had all completed.
SETTLED_RECHECK_SEC = 300

#: Failed workflow names listed in one notification body before "+N more".
_NOTIFY_LIST_CAP = 3

_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 1000

#: Outcomes of :func:`push_ci_failure`.
DELIVERED = "delivered"
DROPPED = "dropped"
DEFERRED = "deferred"

# ``owner/repo@sha`` (lower-cased) -> monotonic time of the read that found every
# run on that commit completed. In memory only: a gateway restart re-reads each
# head once, which costs one request and cannot repeat a notification.
_settled: dict[str, float] = {}

# ``owner/repo@sha`` (lower-cased) -> ids of the failed runs on that commit at its
# last read. The record keeps only these, so its cap is spent on live failures.
_head_failed: dict[str, set[str]] = {}


def opted_in(settings: dict[str, Any], key: provider.RepoKey) -> bool:
    """Whether the CI-failure pass covers this repo."""
    return key.provider == provider.GITHUB and bool(settings.get("notify_on_own_pr_ci_failure"))


async def poll_ci_failures(app: web.Application, keys: list[provider.RepoKey]) -> None:
    """Run one CI-failure pass over the opted-in repos in ``keys``."""
    keys = [k for k in keys if k.provider == provider.GITHUB]
    if not keys:
        return
    try:
        found = await asyncio.to_thread(
            github_client.search_viewer_open_pull_heads, [(k.owner, k.repo) for k in keys]
        )
    except Exception:
        logger.warning("issue-radar CI watch: own-PR search failed", exc_info=True)
        return
    # A repo whose search had more open PRs than it reads is skipped for the
    # cycle: seeding or checking it from a partial list would announce failures
    # that were already there once a hidden PR comes into view.
    truncated = {f"{o}/{r}".lower() for o, r in found["truncated"]}
    if truncated:
        logger.warning(
            "issue-radar CI watch: skipped %d repo(s) with more than %d open own PRs per search: %s",
            len(truncated),
            github_client.CI_SEARCH_PAGE_SIZE * github_client.CI_SEARCH_MAX_PAGES,
            ", ".join(sorted(truncated)),
        )
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for pull in found["pulls"]:
        by_repo.setdefault(f"{pull['owner']}/{pull['repo']}".lower(), []).append(pull)
    live: set[str] = set()
    for key in keys:
        slug = f"{key.owner}/{key.repo}".lower()
        if slug in truncated:
            live.update(t for t in (*_settled, *_head_failed) if t.startswith(f"{slug}@"))
            continue
        pulls = by_repo.get(slug, [])
        live.update(_head_tag(key, p["head_sha"]) for p in pulls)
        try:
            await _check_repo(app, key, pulls)
        except Exception:
            logger.warning(
                "issue-radar CI watch failed for %s/%s", key.owner, key.repo, exc_info=True
            )
    for tag in [t for t in _settled if t not in live]:
        _settled.pop(tag, None)
    for tag in [t for t in _head_failed if t not in live]:
        _head_failed.pop(tag, None)


def _head_tag(key: provider.RepoKey, sha: str) -> str:
    return f"{key.owner}/{key.repo}@{sha}".lower()


async def _check_repo(
    app: web.Application, key: provider.RepoKey, pulls: list[dict[str, Any]]
) -> None:
    """Read the runs on each own PR's head in one repo and report new failures."""
    owner, repo = key.owner, key.repo
    scope = store.provider_root(root=None, provider=key.provider, host=key.host)
    # Read with the record, under the reset's lock: once turning the setting on
    # resets the record, this pass neither notifies nor writes, so it cannot
    # announce or restore what the reset cleared.
    record, generation = await asyncio.to_thread(store.read_ci_watch_record, owner, repo, scope)

    def current() -> bool:
        return store.ci_watch_generation(owner, repo, scope) == generation

    notified: dict[str, int] = dict(record["notified"])
    failures: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    changed = False
    for pull in pulls:
        sha = pull["head_sha"]
        tag = _head_tag(key, sha)
        last = _settled.get(tag)
        # Until the repo is seeded every head is read, so a cycle cut short by a
        # gh error cannot leave a settled head's failures out of the seed.
        if (
            record["seeded"]
            and last is not None
            and tag in _head_failed
            and time.monotonic() - last < SETTLED_RECHECK_SEC
        ):
            continue
        runs = await asyncio.to_thread(
            partial(github_client.list_pr_workflow_runs, owner, repo, sha)
        )
        new_failed: list[dict[str, Any]] = []
        failed_ids: set[str] = set()
        for run in runs:
            if run.get("status") != "completed":
                continue
            if run.get("conclusion") not in FAILED_CONCLUSIONS:
                continue
            attempt = _attempt(run)
            run_id = str(run.get("id"))
            failed_ids.add(run_id)
            if notified.get(run_id, 0) >= attempt:
                continue
            notified.pop(run_id, None)
            notified[run_id] = attempt
            changed = True
            new_failed.append(run)
        _head_failed[tag] = failed_ids
        if runs and all(r.get("status") == "completed" for r in runs):
            _settled[tag] = time.monotonic()
        else:
            _settled.pop(tag, None)
        if new_failed:
            failures.append((pull, new_failed))

    # Keep only the attempts on the user's open PR heads; any other run cannot
    # come back, so its entry only takes room under the record's cap.
    live_ids: set[str] = set()
    for pull in pulls:
        live_ids |= _head_failed.get(_head_tag(key, pull["head_sha"]), set())
    notified = {run_id: n for run_id, n in notified.items() if run_id in live_ids}
    if len(notified) > store.CI_WATCH_MAX_RUNS:
        # Capping would forget live failures and announce them again later.
        logger.warning(
            "issue-radar CI watch: skipped %s/%s, %d failed runs on open own PRs exceed the %d"
            " the record keeps",
            owner,
            repo,
            len(notified),
            store.CI_WATCH_MAX_RUNS,
        )
        return

    if not record["seeded"]:
        await asyncio.to_thread(
            partial(
                store.write_ci_watch_state, owner, repo, notified, root=scope, generation=generation
            )
        )
        return
    before: dict[str, int] = record["notified"]
    if not changed and notified == before:
        return
    # Notify first, then record: a note the rate limiter refuses or whose push
    # raises stays out of the record, so the next cycle offers it again.
    state = app.get("state")
    for pull, runs in failures:
        if not current():
            return
        if await push_ci_failure(state, key, pull, runs, still_current=current) != DEFERRED:
            continue
        # Re-read this head next cycle rather than after the settled interval.
        _settled.pop(_head_tag(key, pull["head_sha"]), None)
        for run in runs:
            run_id = str(run.get("id"))
            if run_id in before:
                notified[run_id] = before[run_id]
            else:
                notified.pop(run_id, None)
    if notified != before:
        await asyncio.to_thread(
            partial(
                store.write_ci_watch_state, owner, repo, notified, root=scope, generation=generation
            )
        )


def _attempt(run: dict[str, Any]) -> int:
    value = run.get("attempt")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def push_ci_failure(
    state: Any,
    key: provider.RepoKey,
    pull: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    still_current: Callable[[], bool] | None = None,
) -> str:
    """Push one CI-failure note for a PR; returns DELIVERED, DROPPED or DEFERRED.

    Goes through the bus on the ``ci-failure`` channel the installed, enabled
    app declares, registering it on first use with the manifest's default
    priority (a later registration would overwrite a runtime priority), and
    draws from the same per-app rate limiter as an HTTP push. A note counts as
    delivered only once the notification history has persisted it. DEFERRED
    (the limiter refused, the push raised, or persistence failed) means a later
    cycle may retry; DROPPED means retrying cannot help. Never raises.
    """
    bus = getattr(state, "notification_bus", None) if state is not None else None
    if bus is None or not runs:
        return DROPPED
    channel = f"{APP_NAME}.{CHANNEL_CI_FAILURE}"
    try:
        # Same order as the HTTP push route: hold the app's lifecycle lock so a
        # disable cannot land between the enablement read and the registration,
        # and read the manifest off the event loop.
        async with app_lifecycle_lock(APP_NAME):
            declared = (
                await asyncio.to_thread(notifications_push._resolve_app_channels, APP_NAME) or {}
            )
            if CHANNEL_CI_FAILURE not in declared:
                logger.warning("issue-radar: the %r channel is not available", CHANNEL_CI_FAILURE)
                return DROPPED
            if not bus.is_registered(channel):
                bus.register_channel(channel, declared[CHANNEL_CI_FAILURE])
    except Exception as exc:  # noqa: BLE001 — never fatal to a watch cycle
        logger.warning("issue-radar: could not register the CI-failure channel: %s", exc)
        return DROPPED
    try:
        owner, repo, number = key.owner, key.repo, int(pull["number"])
        names = [str(r.get("name") or "workflow") for r in runs]
        listed = ", ".join(names[:_NOTIFY_LIST_CAP])
        if len(names) > _NOTIFY_LIST_CAP:
            listed += f" +{len(names) - _NOTIFY_LIST_CAP} more"
        pr_title = str(pull.get("title") or "").strip() or "(no title)"
        payload = NotificationPayload(
            source=f"app:{APP_NAME}",
            channel=channel,
            title=_clip(f"CI failed · {owner}/{repo}#{number}", _MAX_TITLE_CHARS),
            body=_clip(f"{listed} failed on {pr_title}", _MAX_BODY_CHARS),
            group_key=f"ci-failure:{owner}/{repo}#{number}".lower(),
            url=_APP_URL,
            meta={
                "app": APP_NAME,
                "owner": owner,
                "repo": repo,
                "number": number,
                "pr_url": str(pull.get("url") or ""),
                "run_url": str(runs[0].get("url") or ""),
                "count": len(runs),
            },
        )
        try:
            payload.validate()
        except NotificationValidationError as exc:
            logger.warning("issue-radar: dropped an invalid CI-failure note: %s", exc)
            return DROPPED
    except Exception as exc:  # noqa: BLE001 — never fatal to a watch cycle
        logger.warning("issue-radar: could not build a CI-failure note: %s", exc)
        return DROPPED
    limiter = getattr(state, "notification_rate_limiter", None)
    # Checked with no await before the push, so a reset of the repo's record
    # that landed while this note was being prepared cannot be outrun.
    if still_current is not None and not still_current():
        return DROPPED
    if limiter is not None and not limiter.allow(APP_NAME):
        logger.info("issue-radar: notification rate limit reached, deferring a CI note")
        return DEFERRED
    try:
        bus.push(payload)
    except Exception as exc:  # noqa: BLE001 — never fatal to a watch cycle
        logger.warning("issue-radar: CI-failure notification failed: %s", exc)
        return DEFERRED
    # No await ran since the push, so the stashed future belongs to this note.
    persist = getattr(state, "last_notification_persist", None)
    if persist is not None:
        try:
            persisted = await persist
        except Exception as exc:  # noqa: BLE001 — never fatal to a watch cycle
            logger.warning("issue-radar: CI-failure note persistence raised: %s", exc)
            return DEFERRED
        if not persisted:
            logger.warning("issue-radar: CI-failure note was not persisted, deferring")
            return DEFERRED
    logger.info("issue-radar notified CI failure on %s/%s#%s", key.owner, key.repo, pull["number"])
    return DELIVERED
