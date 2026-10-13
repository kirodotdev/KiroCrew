"""Issue Radar — CI-failure alerts for the signed-in user's own pull requests.

One pass of the in-process watcher (``watch._poll_once``), run only for the
connected GitHub and GitLab repos whose per-repo setting
``notify_on_own_pr_ci_failure`` is on. With no repo opted in it returns before
touching ``gh`` or ``glab``, so the watcher's calls and notifications are those
of the new-issue pass and the crew sweep alone.

Each cycle, per forge:

  1. the viewer's open PRs in the opted-in repos are listed with each head
     commit. On GitHub that is one GraphQL search per few repos, read page by
     page (``search_viewer_open_pull_heads``); on GitLab it is one per-project
     MR listing scoped to the token's user (``search_viewer_open_mr_heads``).
     Repos whose listing is cut off at its page cap are skipped for the cycle,
     with a warning;
  2. for each head commit, the runs on it are read with the same client the PR
     pane's run actions use (``list_pr_workflow_runs``: Actions runs on GitHub,
     pipelines on GitLab, where the pipeline id is the run id);
  3. a run that concluded ``failure`` or ``timed_out`` at an attempt not yet
     reported produces one dashboard notification per PR, on this app's
     ``ci-failure`` channel, so the channel's mute and priority settings in
     Settings → Notifications apply to it.

Reported attempts are kept per repo in ``ci-watch-state.json`` (see
``store.read_ci_watch_state``), under the repo's own provider subtree; the record
schema is the same on every forge. The first successful read of a repo seeds
that record without notifying, so turning the setting on does not announce the
red runs already present. A head commit whose runs had all completed at its last
read is re-read every :data:`SETTLED_RECHECK_SEC` rather than every cycle; that
keeps a user with many quiet open PRs from spending a request per PR per minute,
and still catches a re-run within a few minutes.

A re-run is announced again on GitHub because a workflow run carries an ``attempt``
number that climbs on each re-run. GitLab's "Retry" reuses the pipeline id and
its rows carry no attempt, so an in-place retry that fails again is NOT
re-announced; a new commit is, because its pipelines are new ids.

Read-only: it uses the user's existing ``gh`` / ``glab`` session and never writes
to the provider. Azure repos are not covered (:data:`CI_WATCH_PROVIDERS`). Any
provider error skips the affected repo for the cycle and leaves its record
untouched, so nothing is lost or reported twice. A note the rate limiter
refuses, or that the notification history fails to persist, is left unrecorded,
so the next cycle offers it again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import (
    github_client,
    gitlab_client,
    gitlab_client_ci,
    provider,
    store,
)
from kiro_crew.apps.manager import app_lifecycle_lock
from kiro_crew.dashboard.handlers import notifications_push
from kiro_crew.notifications.bus import NotificationPayload, NotificationValidationError

logger = logging.getLogger("kirocrew.app.issue-radar")

APP_NAME = "issue-radar"

#: The forges this pass covers. Membership here is the whole opt-in: a provider
#: outside it is never searched, whatever its repos' settings say.
CI_WATCH_PROVIDERS = frozenset({provider.GITHUB, provider.GITLAB})

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
# last read, plus reported runs that were running again. The record keeps only
# these, so its cap is spent on live failures.
_head_failed: dict[str, set[str]] = {}


def opted_in(settings: dict[str, Any], key: provider.RepoKey) -> bool:
    """Whether the CI-failure pass covers this repo."""
    return key.provider in CI_WATCH_PROVIDERS and bool(settings.get("notify_on_own_pr_ci_failure"))


async def poll_ci_failures(app: web.Application, keys: list[provider.RepoKey]) -> None:
    """Run one CI-failure pass over the opted-in repos in ``keys``.

    GitHub repos share one batched search; GitLab projects are listed one by one,
    each on its own host. A forge whose listing fails is skipped for the cycle
    without touching the other forge's repos.
    """
    github_keys = [k for k in keys if k.provider == provider.GITHUB]
    gitlab_keys = [k for k in keys if k.provider == provider.GITLAB]
    if not github_keys and not gitlab_keys:
        return
    live: set[str] = set()
    if github_keys:
        await _poll_github(app, github_keys, live)
    # One ``GET /user`` per GitLab host per cycle, not per project: the login is the
    # same for every project on a host, and a host whose user cannot be read skips
    # all of its projects for the cycle (their tracked heads stay tracked).
    logins: dict[str, str | None] = {}
    for key in gitlab_keys:
        if key.host not in logins:
            logins[key.host] = await _gitlab_login(key.host)
        await _poll_gitlab(app, key, live, login=logins[key.host])
    for tag in [t for t in _settled if t not in live]:
        _settled.pop(tag, None)
    for tag in [t for t in _head_failed if t not in live]:
        _head_failed.pop(tag, None)


async def _poll_github(app: web.Application, keys: list[provider.RepoKey], live: set[str]) -> None:
    try:
        found = await asyncio.to_thread(
            github_client.search_viewer_open_pull_heads, [(k.owner, k.repo) for k in keys]
        )
    except Exception:
        logger.warning("issue-radar CI watch: own-PR search failed", exc_info=True)
        # Nothing was read, so every head this forge tracked stays tracked.
        live.update(t for t in (*_settled, *_head_failed) if t.startswith(f"{provider.GITHUB}/"))
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
    for key in keys:
        slug = f"{key.owner}/{key.repo}".lower()
        if slug in truncated:
            live.update(_tracked_heads(key))
            continue
        await _check_found(app, key, by_repo.get(slug, []), live)


async def _gitlab_login(host: str) -> str | None:
    """The signed-in ``glab`` user on ``host``, or ``None`` when it cannot be read."""
    try:
        login = await asyncio.to_thread(partial(gitlab_client.get_current_login, host=host))
    except Exception:
        logger.warning(
            "issue-radar CI watch: could not read the glab user on %s", host, exc_info=True
        )
        return None
    if not login:
        logger.warning("issue-radar CI watch: glab did not report the signed-in user on %s", host)
    return login or None


async def _poll_gitlab(
    app: web.Application, key: provider.RepoKey, live: set[str], *, login: str | None
) -> None:
    if login is None:
        live.update(_tracked_heads(key))
        return
    try:
        found = await asyncio.to_thread(
            partial(
                gitlab_client.search_viewer_open_mr_heads,
                key.owner,
                key.repo,
                host=key.host,
                login=login,
            )
        )
    except Exception:
        logger.warning(
            "issue-radar CI watch: own-MR listing failed for %s/%s",
            key.owner,
            key.repo,
            exc_info=True,
        )
        live.update(_tracked_heads(key))
        return
    if found["truncated"]:
        logger.warning(
            "issue-radar CI watch: skipped %s/%s with more than %d open own MRs per listing",
            key.owner,
            key.repo,
            gitlab_client_ci.CI_SEARCH_PAGE_SIZE * gitlab_client_ci.CI_SEARCH_MAX_PAGES,
        )
        live.update(_tracked_heads(key))
        return
    await _check_found(app, key, list(found["pulls"]), live)


async def _check_found(
    app: web.Application, key: provider.RepoKey, pulls: list[dict[str, Any]], live: set[str]
) -> None:
    live.update(_head_tag(key, p["head_sha"]) for p in pulls)
    try:
        await _check_repo(app, key, pulls)
    except Exception:
        logger.warning("issue-radar CI watch failed for %s/%s", key.owner, key.repo, exc_info=True)


def _repo_tag(key: provider.RepoKey) -> str:
    # Provider and host are part of the tag: a GitHub repo and a GitLab project
    # sharing a slug must not share settled or failed-run memory.
    return f"{key.provider}/{key.host}/{key.owner}/{key.repo}".lower()


def _head_tag(key: provider.RepoKey, sha: str) -> str:
    return f"{_repo_tag(key)}@{sha}".lower()


def _tracked_heads(key: provider.RepoKey) -> set[str]:
    prefix = f"{_repo_tag(key)}@"
    return {t for t in (*_settled, *_head_failed) if t.startswith(prefix)}


def _note_scope(key: provider.RepoKey) -> str:
    """The repo part of a note's ``group_key``.

    GitHub keeps the ``owner/repo`` spelling its notes have always grouped under, so
    a note from before this pass covered GitLab still stacks with the next one; any
    other forge carries its provider and host so a same-slug project cannot stack
    onto a GitHub repo's thread.
    """
    if key.provider == provider.GITHUB:
        return f"{key.owner}/{key.repo}".lower()
    return _repo_tag(key)


def _list_runs(key: provider.RepoKey, sha: str) -> list[dict[str, Any]]:
    """The runs on one head commit, from the key's own forge client."""
    if key.provider == provider.GITLAB:
        return gitlab_client.list_pr_workflow_runs(key.owner, key.repo, sha, host=key.host)
    return github_client.list_pr_workflow_runs(key.owner, key.repo, sha)


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
        runs = await asyncio.to_thread(partial(_list_runs, key, sha))
        new_failed: list[dict[str, Any]] = []
        failed_ids: set[str] = set()
        for run in runs:
            if run.get("status") != "completed":
                # A reported run that is running again keeps its record entry: a
                # GitLab Retry reuses the id with no attempt, so dropping it here
                # would announce the retry's failure as new.
                if str(run.get("id")) in notified:
                    failed_ids.add(str(run.get("id")))
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
        # "#7" on GitHub, "!7" on GitLab: the forge's own spelling of the ref.
        sigil = provider.terms(key)["change_request_sigil"]
        names = [str(r.get("name") or "workflow") for r in runs]
        listed = ", ".join(names[:_NOTIFY_LIST_CAP])
        if len(names) > _NOTIFY_LIST_CAP:
            listed += f" +{len(names) - _NOTIFY_LIST_CAP} more"
        pr_title = str(pull.get("title") or "").strip() or "(no title)"
        payload = NotificationPayload(
            source=f"app:{APP_NAME}",
            channel=channel,
            title=_clip(f"CI failed · {owner}/{repo}{sigil}{number}", _MAX_TITLE_CHARS),
            body=_clip(f"{listed} failed on {pr_title}", _MAX_BODY_CHARS),
            group_key=f"ci-failure:{_note_scope(key)}{sigil}{number}",
            url=_APP_URL,
            meta={
                "app": APP_NAME,
                "provider": key.provider,
                "host": key.host,
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
