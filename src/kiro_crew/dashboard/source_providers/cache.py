"""The coalesced provider reads behind the Changes and Issues panels, and their caches.

One provider fanout per normalized URL at a time: concurrent callers share the
in-flight task, a task reserves retained-memory room before it starts (a caller
that finds none waits, then fails as retryable), and each result is redacted,
byte-capped and cached with a lifecycle-derived TTL. A mutation bumps the URL's
generation so a read that started before it can never write the pre-mutation
payload back. An expired github.com payload is revalidated with conditional GETs
before the whole fanout runs again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Any, TypeVar
from urllib.parse import quote

from kiro_crew.dashboard.source_providers import (
    LOGGER_NAME,
    chip_status,
    github,
    gitlab,
    hosts,
    jira,
    links,
    plugins,
    projection,
    runner,
    sanitize,
)
from kiro_crew.dashboard.source_providers.contract import (
    RepoRef,
    SourceCapacityError,
    SourceChangePayload,
    SourceProviderError,
    SourceProviderNotConfigured,
    SourceRef,
)
from kiro_crew.dashboard.source_providers.runner import _ConditionalRead
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger(LOGGER_NAME)


_CACHE_TTL_SECS = 30
# A merged pull request is terminal: nothing the chip renders moves again, and
# re-reading it on the open-PR cadence for as long as its chip stays in a
# sidebar was pure provider load — one `gh` subprocess per finished PR per
# minute from the chip loop alone, forever. A CLOSED one is nearly so, but it can
# be reopened and keeps accruing discussion, so it ages on a shorter clock.
# These govern the chip cache and the full payloads that have no cheaper
# revalidation (GitLab, registered plugins); a github.com full payload is
# instead REVALIDATED with a conditional GET at the open cadence whatever its
# lifecycle (see `_revalidate_pull_request`), so post-merge comments and a
# reopen reach the panel within one TTL for one rate-limit-free request.
# Mutation invalidation still drops these entries, the explicit refresh bypasses
# them, and the turn-boundary force still re-reads a CLOSED chip, never a merged
# one.
_TERMINAL_TTL_SECS = 6 * 60 * 60
_CLOSED_TTL_SECS = 60 * 60
# Ceiling on how long conditional revalidation may keep re-stamping one full
# payload without a full read behind it. The probes rest on GitHub moving the
# `issues/{n}` ETag for every rendered field, which the API does not promise for
# every mutation class; past this age one full read runs regardless of what the
# probes say, so a coverage gap degrades to bounded staleness, never unbounded.
_REVALIDATED_MAX_AGE_SECS = _TERMINAL_TTL_SECS
_CACHE_MAX_ENTRIES = 32
_CACHE_MAX_BYTES = 48 * 1024 * 1024
# Bound direct full/check fetches by task count and retained-memory weight.
# Same-URL callers coalesce before admission; detached stale tasks keep their
# reservation until their underlying task actually completes.
_DIRECT_FETCH_PENDING_MAX = 16
_DIRECT_FETCH_MAX_RESERVED_BYTES = 128 * 1024 * 1024
# Measured worst case for one full fetch -- every command in the fanout at its
# declared output ceiling -- is a ~32MB allocation peak, and ~43MB projected if
# every ceiling is filled exactly (decode amplifies wire bytes by ~4.3x). 64MB
# is therefore a ~1.5x cover for raw bytes, decoded JSON, the normalized copy,
# and object overhead while a complete fetch remains alive. Two of these
# saturate the pool by design; a third caller WAITS for room rather than being
# refused (see _wait_for_direct_fetch_capacity), so the ceiling bounds memory
# without turning ordinary concurrent panel use into an error.
_FULL_FETCH_RESERVATION_BYTES = 64 * 1024 * 1024
_CHECKS_FETCH_RESERVATION_BYTES = 8 * 1024 * 1024
# How long a caller waits for admission room before giving up. Sized under the
# per-command timeout (runner._COMMAND_TIMEOUT_SECS) so a queued read cannot
# outlive the fetch it is queued behind by more than one command's worth of work.
_DIRECT_FETCH_WAIT_SECS = 20.0
# An issue payload is metadata plus comments -- no diffs, no check rollup -- so
# both its aggregate cache and its retained-memory lease sit well below the
# pull-request figures. TTL and entry count are shared with the PR cache.
_ISSUE_CACHE_MAX_BYTES = 16 * 1024 * 1024
_ISSUE_FETCH_RESERVATION_BYTES = 16 * 1024 * 1024
# url -> (stored_at, serialized_size_bytes, normalized_payload)
_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}
_CACHE_LOCK = LoopBoundLock()
_FULL_FETCH_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
_FULL_FETCH_TASKS: dict[str, set[asyncio.Task[dict[str, Any]]]] = {}
_FULL_FETCH_GENERATIONS: dict[str, int] = {}
_CHECKS_FETCH_INFLIGHT: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}
# Issues get their own cache and inflight map rather than sharing the
# pull-request ones: the two live at different URLs but the same normalized-URL
# key space would still be shared, and a PR mutation's cache invalidation
# (_invalidate_pull_request_cache) must not evict issue payloads it knows
# nothing about. No generation map is needed -- this phase never mutates an
# issue, so there is no post-mutation write to order against.
_ISSUE_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}
_ISSUE_CACHE_LOCK = LoopBoundLock()
_ISSUE_FETCH_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
_ISSUE_FETCH_TASKS: dict[str, set[asyncio.Task[dict[str, Any]]]] = {}
_DIRECT_FETCH_RESERVATIONS: dict[asyncio.Task[Any], int] = {}
# Futures held by callers waiting for admission room. Woken when any reservation
# is released, so a request that arrives at a full pool queues instead of
# failing (see _wait_for_direct_fetch_capacity).
_DIRECT_FETCH_WAITERS: list[asyncio.Future[None]] = []


def _lifecycle_ttl(state: str) -> float:
    """Retention for a chip-vocabulary lifecycle: merged, closed, or anything else."""
    if state == "merged":
        return _TERMINAL_TTL_SECS
    if state == "closed":
        return _CLOSED_TTL_SECS
    return _CACHE_TTL_SECS


def _full_payload_ttl(payload: dict[str, Any]) -> float:
    """How long a cached full payload stays fresh WITHOUT revalidation, by the
    lifecycle it describes.

    Decided from the payload itself (through the same ``_project_state`` the chip
    projection uses) rather than from the chip cache, so the two caches cannot
    disagree about whether a URL is finished. A github.com payload past the open
    TTL is revalidated instead of served on this clock (``fetch_pull_request``).
    """
    state = projection._project_state(
        str(payload.get("state") or ""), draft=bool(payload.get("draft"))
    )
    return _lifecycle_ttl(state or "")


_T = TypeVar("_T")


def _finish_inflight(store: dict[str, asyncio.Task[_T]], url: str, task: asyncio.Task[_T]) -> None:
    """Drop a completed shared fetch and consume orphaned exceptions."""
    if store.get(url) is task:
        store.pop(url, None)
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()


def _direct_fetch_tasks() -> set[asyncio.Task[Any]]:
    """Snapshot unique direct full/issue/check tasks, including detached stale work.

    Issue fetches are counted here so their reservations are real: the pending
    cap and the retained-byte ceiling are computed from this set, so a task
    absent from it would hold a lease nothing ever reads.
    """
    tasks: set[asyncio.Task[Any]] = set(_CHECKS_FETCH_INFLIGHT.values())
    for full_tasks in _FULL_FETCH_TASKS.values():
        tasks.update(full_tasks)
    for issue_tasks in _ISSUE_FETCH_TASKS.values():
        tasks.update(issue_tasks)
    return tasks


def _direct_fetch_capacity_free(reservation_bytes: int) -> bool:
    """Whether a lease of ``reservation_bytes`` fits under both ceilings now."""
    tasks = _direct_fetch_tasks()
    reserved = sum(
        amount
        for task, amount in _DIRECT_FETCH_RESERVATIONS.items()
        if task in tasks and not task.done()
    )
    return (
        len(tasks) < _DIRECT_FETCH_PENDING_MAX
        and reservation_bytes <= _DIRECT_FETCH_MAX_RESERVED_BYTES - reserved
    )


async def _wait_for_direct_fetch_capacity(deadline: float) -> bool:
    """Sleep until a reservation is released or ``deadline`` passes.

    Returns True if a release was observed and the caller should re-check
    capacity, False if the wait budget is spent.

    MUST NOT be awaited while holding ``_CACHE_LOCK`` or ``_ISSUE_CACHE_LOCK``:
    an in-flight fetch takes the same lock to write its result, so waiting for it
    to finish while holding that lock would deadlock. Callers therefore release
    the lock, wait here, then re-acquire and re-check.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    _DIRECT_FETCH_WAITERS.append(waiter)
    try:
        await asyncio.wait_for(waiter, timeout=remaining)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        if waiter in _DIRECT_FETCH_WAITERS:
            _DIRECT_FETCH_WAITERS.remove(waiter)


def _wake_direct_fetch_waiters() -> None:
    """Wake every waiter after a release; each re-checks its own ceiling.

    All waiters are woken rather than just the first: leases differ in size, so
    the head of the queue is not necessarily the one that now fits, and a
    freed lease that only satisfies a later waiter must not stall behind it.
    """
    waiters = list(_DIRECT_FETCH_WAITERS)
    _DIRECT_FETCH_WAITERS.clear()
    for waiter in waiters:
        if not waiter.done():
            waiter.set_result(None)


def _capacity_exhausted_error() -> SourceCapacityError:
    return SourceCapacityError("Too many source requests are pending; retry shortly.")


def _reserve_direct_fetch(task: asyncio.Task[Any], reservation_bytes: int) -> None:
    """Hold a conservative retained-byte lease until the task terminates."""
    _DIRECT_FETCH_RESERVATIONS[task] = reservation_bytes

    def release(done: asyncio.Task[Any]) -> None:
        _DIRECT_FETCH_RESERVATIONS.pop(done, None)
        # The lease is gone, so a queued caller may now fit. Scheduled on the loop
        # rather than woken inline: this runs during task teardown, and deferring
        # by one loop iteration means the waiter re-checks capacity after the task
        # is fully settled rather than mid-completion.
        asyncio.get_running_loop().call_soon(_wake_direct_fetch_waiters)

    task.add_done_callback(release)


async def _fetch_pull_request_checks_uncached(ref: SourceRef) -> list[dict[str, Any]]:
    plugin = plugins._plugin_for_change(ref)
    if plugin is not None:
        try:
            with plugins._plugin_errors(plugin.id):
                fetched = await plugin.fetch_checks(ref)
        except SourceProviderNotConfigured as exc:
            raise plugins._plugin_setup_error(plugin, exc) from exc
    else:
        fetched = await (
            github._fetch_github_checks(ref)
            if ref.provider == "github"
            else gitlab._fetch_gitlab_checks(ref)
        )
    checks = sanitize._redact_provider_data(fetched)
    if not isinstance(checks, list):
        raise SourceProviderError("provider returned an invalid checks payload")
    payload = {"checks": checks}
    if sanitize._payload_size_bytes(payload) > sanitize._MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider checks payload was too large")
    return checks


async def fetch_pull_request_checks(raw_url: str) -> list[dict[str, Any]]:
    """Fetch current CI checks, coalescing concurrent requests for one URL."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    deadline = time.monotonic() + _DIRECT_FETCH_WAIT_SECS
    while True:
        task = _CHECKS_FETCH_INFLIGHT.get(ref.url)
        if task is not None:
            break
        if _direct_fetch_capacity_free(_CHECKS_FETCH_RESERVATION_BYTES):
            task = asyncio.create_task(_fetch_pull_request_checks_uncached(ref))
            _CHECKS_FETCH_INFLIGHT[ref.url] = task
            _reserve_direct_fetch(task, _CHECKS_FETCH_RESERVATION_BYTES)

            def finish_checks(done: asyncio.Task[list[dict[str, Any]]]) -> None:
                _finish_inflight(_CHECKS_FETCH_INFLIGHT, ref.url, done)

            task.add_done_callback(finish_checks)
            break
        if not await _wait_for_direct_fetch_capacity(deadline):
            raise _capacity_exhausted_error()
    return await asyncio.shield(task)


async def _fetch_pull_request_uncached(
    ref: SourceRef, generation: int, *, refresh: bool = False
) -> dict[str, Any]:
    # A registered plugin is dispatched from HERE, inside the shared layer, so it
    # inherits redaction, the byte cap, the cache write, the generation guard and
    # the chip-status projection without being able to opt out of any of them.
    plugin = plugins._plugin_for_change(ref)
    fetched: SourceChangePayload | dict[str, Any]
    if plugin is not None:
        try:
            with plugins._plugin_errors(plugin.id):
                fetched = await plugin.fetch_full(ref, refresh=refresh)
        except SourceProviderNotConfigured as exc:
            raise plugins._plugin_setup_error(plugin, exc) from exc
    elif ref.provider == "github":
        fetched = await github._fetch_github(ref)
    else:
        fetched = await gitlab._fetch_gitlab(ref)
    data = sanitize._redact_provider_data(fetched)
    if not isinstance(data, dict):
        raise SourceProviderError("provider returned an invalid pull-request payload")
    payload_size = sanitize._payload_size_bytes(data)
    if payload_size > sanitize._MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider pull-request payload was too large")

    async with _CACHE_LOCK:
        if _FULL_FETCH_GENERATIONS.get(ref.url, 0) != generation:
            # A successful mutation invalidated this generation while provider
            # I/O was running. Return its result to existing waiters, but never
            # let pre-mutation data overwrite the post-mutation cache.
            return data
        now = time.monotonic()
        # Sweep expired entries on write, then cap by both recency count and
        # aggregate serialized weight. A PR combines several provider commands,
        # so per-command pipe limits alone do not bound retained cache memory.
        # Each entry ages by its own lifecycle-derived TTL (`_full_payload_ttl`).
        for key in [
            key
            for key, (stored_at, _, payload) in _CACHE.items()
            if now - stored_at >= _full_payload_ttl(payload)
        ]:
            del _CACHE[key]
        _CACHE[ref.url] = (now, payload_size, data)
        while (
            len(_CACHE) > _CACHE_MAX_ENTRIES
            or sum(entry[1] for entry in _CACHE.values()) > _CACHE_MAX_BYTES
        ):
            del _CACHE[min(_CACHE, key=lambda key: _CACHE[key][0])]
        # One provider read, both surfaces: project this payload onto the chip
        # cache so the sidebar cannot keep rendering an older lifecycle than the
        # detail panel it was just fetched for. Kept INSIDE the lock, in the same
        # transaction as the generation check and the `_CACHE` write, so a
        # provider mutation cannot land between the passing generation check and
        # this projection and republish pre-mutation status into the chip cache
        # (which would emit a stale `source_status` delta the full-cache
        # invalidation cannot undo). `record_full_payload_status` is synchronous
        # and never re-acquires `_CACHE_LOCK`, so running it here cannot deadlock.
        chip_status.record_full_payload_status(ref.url, data)
    return data


# ── Conditional revalidation of an expired GitHub payload ────────────────────
# An expired full payload would otherwise mean the whole provider fanout again
# (the core `gh pr view`, the files, review-comment and rollup reads, and the
# merge-state re-reads) even when nothing about the pull request has moved.
# GitHub's REST
# API honours `If-None-Match`, and an authenticated `304 Not Modified` is free on
# the primary rate limit, so an expired github.com payload is first REVALIDATED
# with small conditional GETs; only when one reports a change does the fanout
# run. Together the probes cover what the panel renders:
#   * `issues/{n}` -- its ETag moves with the pull request's `updated_at`, i.e.
#     title/body/labels/lifecycle (a reopen included), a push (synchronize),
#     reviews, and comments. `pulls/{n}` is deliberately NOT the probe: it
#     embeds the base/head repository objects whose live counters (open issues,
#     stars, pushed_at) change its ETag on a busy repository without the pull
#     request changing.
#   * `commits/{head_sha}/check-runs` and `commits/{head_sha}/status` -- CI
#     hangs off the commit, not the pull request, so `updated_at` never moves
#     for it; these two ETags do. Both are needed: check runs and legacy commit
#     statuses are separate resources, and `statusCheckRollup` renders both.
#     Skipped for a merged or closed pull request, whose CI the panel and the
#     chip stop tracking.
# The merge pair (`mergeable`/`mergeStateStatus`) is recomputed lazily by GitHub
# and moves no validator; it is carried by the chip refresh, which drops the
# full payload on a changed merge pair (see the chip <-> full protocol).
# GitHub's GraphQL API -- what `gh pr view` speaks -- has no conditional
# requests at all, which is why the probes are REST.
#
# Strictly 304-only: a probe that answers 200 (including the first probe of a
# URL, which has no validator to send and only LEARNS the ETags) or that fails
# is "unknown", and unknown runs the fanout. Nothing is ever judged unchanged by
# comparing bodies. An explicit refresh, a mutation invalidation and any
# non-GitHub provider (GitLab, a registered plugin) skip the probes entirely.


@dataclass(frozen=True)
class _Revalidator:
    """The validators learned for one pull request URL. ``head_sha`` scopes the
    two commit-level ETags: a push moves the head, and the old commit's
    check-runs ETag would still answer 304 for a commit nobody looks at any
    more."""

    issue_etag: str
    checks_etag: str
    status_etag: str
    head_sha: str
    learned_at: float
    # When the payload these validators vouch for was last read in full. An
    # all-304 carries it forward unchanged; only a full read resets it, and
    # `_revalidate_pull_request` forces one once it is `_REVALIDATED_MAX_AGE_SECS`
    # old. ``None`` (validators built without a read behind them) is never
    # aged out.
    read_at: float | None = None


_REVALIDATORS: dict[str, _Revalidator] = {}
_REVALIDATORS_MAX = 512
# Check-runs are paged; the ETag is per exact URL, so the page size is part of
# the key and must not drift between probes.
_REVALIDATE_CHECK_RUNS_PAGE = 100


def _trim_revalidators() -> None:
    while len(_REVALIDATORS) > _REVALIDATORS_MAX:
        del _REVALIDATORS[min(_REVALIDATORS, key=lambda key: _REVALIDATORS[key].learned_at)]


async def _gh_conditional_get(
    path: str, etag: str, *, max_output_bytes: int = runner._METADATA_OUTPUT_BYTES
) -> _ConditionalRead:
    """One `gh api` GET carrying ``If-None-Match`` when a validator is known."""
    argv = ["gh", "api", path, "-i"]
    if etag:
        argv += ["-H", f"If-None-Match: {etag}"]
    return await runner._run_provider(
        *argv, max_output_bytes=max_output_bytes, parse=runner._parse_conditional_get("gh")
    )


def _payload_is_terminal(payload: dict[str, Any]) -> bool:
    state = projection._project_state(
        str(payload.get("state") or ""), draft=bool(payload.get("draft"))
    )
    return state in projection._TERMINAL_CHIP_STATES


@dataclass(frozen=True)
class _ProbeOutcome:
    """``unchanged`` when every probe answered 304. ``learned`` carries the
    validators the probes returned, or ``None`` when a probe failed or the
    payload had no head to probe. On an all-304 they are already committed;
    on a 200 they describe a payload the cache does NOT hold yet, so the
    caller commits them only once the full read that follows has succeeded
    -- committed early, a fanout that then failed would leave the pre-change
    payload paired with post-change validators, and every later probe would
    answer 304 against it and re-stamp the stale payload as current."""

    unchanged: bool
    learned: _Revalidator | None


_PROBE_UNKNOWN = _ProbeOutcome(False, None)


def _commit_revalidator(url: str, learned: _Revalidator | None) -> None:
    if learned is None:
        return
    _REVALIDATORS[url] = learned
    _trim_revalidators()


async def _probe_github_payload(ref: SourceRef, payload: dict[str, Any]) -> _ProbeOutcome:
    """Ask GitHub whether the pull request ``payload`` describes has moved.

    Any probe failure is an "unknown" (not unchanged, nothing learned): the
    fanout that follows is the same read that would have run without probing,
    so a failing probe costs at most the probes themselves and can never hide
    a change.
    """
    head_sha = str(payload.get("headSha") or "")
    if not head_sha:
        return _PROBE_UNKNOWN
    known = _REVALIDATORS.get(ref.url)
    same_head = known is not None and known.head_sha == head_sha
    issue_etag = known.issue_etag if known else ""
    checks_etag = known.checks_etag if known and same_head else ""
    status_etag = known.status_etag if known and same_head else ""
    repo_api = f"repos/{quote(ref.owner)}/{quote(ref.repo)}"
    commit_api = f"{repo_api}/commits/{quote(head_sha)}"
    probes = [_gh_conditional_get(f"{repo_api}/issues/{ref.number}", issue_etag)]
    if not _payload_is_terminal(payload):
        probes.append(
            _gh_conditional_get(
                f"{commit_api}/check-runs?per_page={_REVALIDATE_CHECK_RUNS_PAGE}",
                checks_etag,
                max_output_bytes=runner._CHECKS_OUTPUT_BYTES,
            )
        )
        probes.append(
            _gh_conditional_get(
                f"{commit_api}/status", status_etag, max_output_bytes=runner._CHECKS_OUTPUT_BYTES
            )
        )
    try:
        reads = await asyncio.gather(*probes)
    except SourceProviderError as exc:
        logger.info("source revalidation probe failed; reading in full: %s", exc)
        return _PROBE_UNKNOWN
    issue = reads[0]
    checks = reads[1] if len(reads) > 1 else None
    status = reads[2] if len(reads) > 2 else None
    learned = _Revalidator(
        issue_etag=issue.etag or issue_etag,
        checks_etag=(checks.etag if checks else "") or checks_etag,
        status_etag=(status.etag if status else "") or status_etag,
        head_sha=head_sha,
        learned_at=time.monotonic(),
        read_at=known.read_at if known else None,
    )
    unchanged = all(read.status == 304 for read in reads)
    if unchanged:
        # A 304 vouches for the payload the cache already holds, so the
        # (re-confirmed) validators are safe to keep right away.
        _commit_revalidator(ref.url, learned)
    return _ProbeOutcome(unchanged, learned)


def _revalidation_applies(ref: SourceRef) -> bool:
    """Only a github.com pull request served by the built-in fetcher is probed;
    GitLab and registered plugins have no conditional read here."""
    return ref.provider == "github" and plugins._plugin_for_change(ref) is None


async def _revalidate_pull_request(
    ref: SourceRef, generation: int, cached: tuple[float, int, dict[str, Any]]
) -> dict[str, Any]:
    """Serve the expired ``cached`` payload if the probes say it is current,
    else fall through to the full read. Runs under the same inflight slot and
    memory reservation as a full fetch, because it may become one."""
    _, size, payload = cached
    known = _REVALIDATORS.get(ref.url)
    if (
        known is not None
        and known.read_at is not None
        and time.monotonic() - known.read_at >= _REVALIDATED_MAX_AGE_SECS
    ):
        # Ceiling reached: read in full without asking, and forget the
        # validators so the next cycle learns a fresh set against this read
        # (kept, they would pin `read_at` and force every later cycle too).
        _REVALIDATORS.pop(ref.url, None)
        return await _fetch_pull_request_uncached(ref, generation)
    outcome = await _probe_github_payload(ref, payload)
    if outcome.unchanged:
        async with _CACHE_LOCK:
            # Re-stamp only the entry the probes vouched for: a mutation that
            # landed meanwhile has advanced the generation and dropped it, and a
            # concurrent write may have replaced it. Either way the caller still
            # gets the payload the probes confirmed current.
            if (
                _FULL_FETCH_GENERATIONS.get(ref.url, 0) == generation
                and _CACHE.get(ref.url) is cached
            ):
                _CACHE[ref.url] = (time.monotonic(), size, payload)
        return payload
    fresh = await _fetch_pull_request_uncached(ref, generation)
    # Only now does the cache hold a payload at least as new as the validators
    # describe (see _ProbeOutcome); a fanout that raised leaves the old ones.
    if outcome.learned is not None:
        _commit_revalidator(ref.url, replace(outcome.learned, read_at=time.monotonic()))
    return fresh


async def fetch_pull_request(raw_url: str, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch a PR/MR, sharing one provider fanout per normalized URL."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links._require_change_ref(links.parse_source_url(raw_url))
    now = time.monotonic()
    deadline = now + _DIRECT_FETCH_WAIT_SECS
    while True:
        async with _CACHE_LOCK:
            cached = _CACHE.get(ref.url)
            revalidate = cached is not None and not refresh and _revalidation_applies(ref)
            if cached and not refresh:
                age = time.monotonic() - cached[0]
                # Inside the open TTL every provider serves the entry as is. Past
                # it, a github.com entry is REVALIDATED below whatever its
                # lifecycle (the probe is one rate-limit-free request); a
                # provider with no conditional read keeps serving a finished
                # payload on its lifecycle clock instead.
                if age < _CACHE_TTL_SECS or (not revalidate and age < _full_payload_ttl(cached[2])):
                    return cached[2]
            task = _FULL_FETCH_INFLIGHT.get(ref.url)
            if task is not None:
                break
            if _direct_fetch_capacity_free(_FULL_FETCH_RESERVATION_BYTES):
                generation = _FULL_FETCH_GENERATIONS.get(ref.url, 0)
                if revalidate and cached is not None:
                    task = asyncio.create_task(_revalidate_pull_request(ref, generation, cached))
                else:
                    task = asyncio.create_task(
                        _fetch_pull_request_uncached(ref, generation, refresh=refresh)
                    )
                _FULL_FETCH_INFLIGHT[ref.url] = task
                _FULL_FETCH_TASKS.setdefault(ref.url, set()).add(task)
                _reserve_direct_fetch(task, _FULL_FETCH_RESERVATION_BYTES)

                def finish_full_fetch(done: asyncio.Task[dict[str, Any]]) -> None:
                    _finish_inflight(_FULL_FETCH_INFLIGHT, ref.url, done)
                    active = _FULL_FETCH_TASKS.get(ref.url)
                    if active is None:
                        return
                    active.discard(done)
                    if not active:
                        _FULL_FETCH_TASKS.pop(ref.url, None)
                        _FULL_FETCH_GENERATIONS.pop(ref.url, None)

                task.add_done_callback(finish_full_fetch)
                break
        # Outside the lock on purpose: the fetches being waited on take
        # _CACHE_LOCK themselves to write their result, so waiting under it would
        # deadlock. Re-checks the cache and the inflight map on wake, since
        # either may have been satisfied by whoever just finished.
        if not await _wait_for_direct_fetch_capacity(deadline):
            raise _capacity_exhausted_error()
    # Shield the shared fetch so one disconnected browser cannot cancel work
    # still awaited by another request for the same URL.
    return await asyncio.shield(task)


async def _fetch_issue_uncached(ref: SourceRef) -> dict[str, Any]:
    if ref.provider == "github":
        fetched = await github._fetch_github_issue(ref)
    elif ref.provider == "gitlab":
        fetched = await gitlab._fetch_gitlab_issue(ref)
    elif ref.provider == "jira":
        fetched = await jira._fetch_jira_issue(ref)
    else:
        raise SourceProviderError(f"unsupported issue provider: {ref.provider}")
    data = sanitize._redact_provider_data(fetched)
    if not isinstance(data, dict):
        raise SourceProviderError("provider returned an invalid issue payload")
    payload_size = sanitize._payload_size_bytes(data)
    if payload_size > sanitize._MAX_PAYLOAD_BYTES:
        raise SourceProviderError("provider issue payload was too large")

    async with _ISSUE_CACHE_LOCK:
        now = time.monotonic()
        # Sweep expired entries on write, then cap by both recency count and
        # aggregate serialized weight -- an issue combines several provider
        # commands, so per-command pipe limits alone do not bound retained
        # cache memory. Deliberately NOT paired with `record_full_payload_status`:
        # an issue has no chip status, so projecting one would publish a
        # meaningless {ci, state} for a URL the sidebar never asks about.
        for key in [
            key
            for key, (stored_at, _, _) in _ISSUE_CACHE.items()
            if now - stored_at >= _CACHE_TTL_SECS
        ]:
            del _ISSUE_CACHE[key]
        _ISSUE_CACHE[ref.url] = (now, payload_size, data)
        while (
            len(_ISSUE_CACHE) > _CACHE_MAX_ENTRIES
            or sum(entry[1] for entry in _ISSUE_CACHE.values()) > _ISSUE_CACHE_MAX_BYTES
        ):
            del _ISSUE_CACHE[min(_ISSUE_CACHE, key=lambda key: _ISSUE_CACHE[key][0])]
    return data


async def fetch_issue(raw_url: str, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch an issue, sharing one provider fanout per normalized URL."""
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links.parse_source_url(raw_url)
    if ref.kind != "issue":
        raise ValueError("This URL points at a pull request or merge request, not an issue.")
    # Jira issues require configured credentials. When none are available, the
    # ValueError propagates to the frontend which shows the "Open in Jira"
    # link-out fallback (the same behaviour as a zero-config install).
    now = time.monotonic()
    deadline = now + _DIRECT_FETCH_WAIT_SECS
    while True:
        async with _ISSUE_CACHE_LOCK:
            cached = _ISSUE_CACHE.get(ref.url)
            if not refresh and cached and time.monotonic() - cached[0] < _CACHE_TTL_SECS:
                return cached[2]
            task = _ISSUE_FETCH_INFLIGHT.get(ref.url)
            if task is not None:
                break
            if _direct_fetch_capacity_free(_ISSUE_FETCH_RESERVATION_BYTES):
                task = asyncio.create_task(_fetch_issue_uncached(ref))
                _ISSUE_FETCH_INFLIGHT[ref.url] = task
                _ISSUE_FETCH_TASKS.setdefault(ref.url, set()).add(task)
                _reserve_direct_fetch(task, _ISSUE_FETCH_RESERVATION_BYTES)

                def finish_issue_fetch(done: asyncio.Task[dict[str, Any]]) -> None:
                    _finish_inflight(_ISSUE_FETCH_INFLIGHT, ref.url, done)
                    active = _ISSUE_FETCH_TASKS.get(ref.url)
                    if active is None:
                        return
                    active.discard(done)
                    if not active:
                        _ISSUE_FETCH_TASKS.pop(ref.url, None)

                task.add_done_callback(finish_issue_fetch)
                break
        # Outside the lock: see the matching note in fetch_pull_request.
        if not await _wait_for_direct_fetch_capacity(deadline):
            raise _capacity_exhausted_error()
    # Shield the shared fetch so one disconnected browser cannot cancel work
    # still awaited by another request for the same URL.
    return await asyncio.shield(task)


# A GitHub username/org login: alphanumeric or single hyphens, 1-39 chars. Used
# to gate the per-login profile lookup so a malformed login from provider data
# can never widen the `gh api users/<login>` path (traversal / query injection).
_GH_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}")


_CONTRIBUTORS_TTL_SECS = 6 * 60 * 60
_CONTRIBUTORS_MAX = 6
_contributors_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_contributors_lock = LoopBoundLock()
_contributors_inflight: dict[str, asyncio.Task[list[dict[str, str]]]] = {}


async def _fetch_github_contributors(ref: RepoRef, key: str) -> list[dict[str, str]]:
    raw = await runner._run_json(
        "gh",
        "api",
        f"repos/{ref.owner}/{ref.repo}/contributors?per_page={_CONTRIBUTORS_MAX}&anon=false",
    )
    contributors: list[dict[str, str]] = []
    for row in projection._as_list(raw)[:_CONTRIBUTORS_MAX]:
        login = str(row.get("login") or "").strip()
        if not login:
            continue
        # The display name needs a second lookup, but only when the login is a
        # safe GitHub handle -- an unexpected value never reaches the API path.
        name = login
        if _GH_LOGIN_RE.fullmatch(login):
            profile = projection._as_dict(await runner._run_json("gh", "api", f"users/{login}"))
            name = str(profile.get("name") or "").strip() or login
        contributors.append(
            {
                "login": login,
                "name": name,
                "avatarUrl": str(row.get("avatar_url") or ""),
                "profileUrl": f"https://github.com/{login}",
            }
        )
    # Names and avatar URLs are provider-controlled: redact secrets/exfil URLs
    # before they are cached or returned. The client renders them as text/<img>.
    contributors = sanitize._redact_provider_data(contributors)
    async with _contributors_lock:
        now = time.monotonic()
        stale_keys = [
            k for k, (at, _) in _contributors_cache.items() if now - at >= _CONTRIBUTORS_TTL_SECS
        ]
        for stale in stale_keys:
            del _contributors_cache[stale]
        _contributors_cache[key] = (now, contributors)
        while len(_contributors_cache) > _CACHE_MAX_ENTRIES:
            oldest = min(_contributors_cache, key=lambda k: _contributors_cache[k][0])
            del _contributors_cache[oldest]
    return contributors


async def fetch_app_contributors(url: str, *, refresh: bool = False) -> list[dict[str, str]]:
    """Return an app source repo's top contributors (GitHub only, v1).

    Each entry is ``{login, name, avatarUrl, profileUrl}`` -- ``name`` falls back
    to the login when the GitHub profile has no display name. Capped at six by
    commit count (the provider's default ordering). A non-github host returns
    ``[]`` (not an error) so the caller can simply hide the row; an unparseable
    or non-allowlisted URL raises ``ValueError`` (mapped to 400).
    """
    # Refresh the self-managed GitLab allowlist off the loop before parse_repo_url
    # reads the cached snapshot, mirroring fetch_pull_request.
    await hosts.ensure_gitlab_hosts_loaded()
    ref = links.parse_repo_url(url)
    if ref.provider != "github":
        return []
    key = f"{ref.host}/{ref.owner}/{ref.repo}"
    async with _contributors_lock:
        cached = _contributors_cache.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < _CONTRIBUTORS_TTL_SECS:
            return cached[1]
        task = _contributors_inflight.get(key)
        if task is None:
            task = asyncio.create_task(_fetch_github_contributors(ref, key))
            _contributors_inflight[key] = task
            task.add_done_callback(lambda done: _finish_inflight(_contributors_inflight, key, done))
    # Shield the shared fetch so one disconnected browser cannot cancel work
    # another concurrent view is still awaiting for the same repo.
    return await asyncio.shield(task)


async def _invalidate_full_payload_cache(url: str) -> None:
    """Supersede the FULL pull-request payload cache and its in-flight fetch.

    Deliberately does NOT touch the lightweight chip-status cache. A caller that
    has just written a fresh chip status (the changed-status path in
    ``_refresh_check_status``) must drop only the now-stale full payload behind
    the detail panel, not the chip entry it just produced — invalidating the
    chip here would pop that entry and bump its generation, spuriously
    re-judging the next refresh as "changed" and spinning the very
    mutual-invalidation loop this projection exists to avoid.
    """
    async with _CACHE_LOCK:
        _CACHE.pop(url, None)
        if _FULL_FETCH_TASKS.get(url):
            _FULL_FETCH_GENERATIONS[url] = _FULL_FETCH_GENERATIONS.get(url, 0) + 1
        else:
            _FULL_FETCH_GENERATIONS.pop(url, None)
        _FULL_FETCH_INFLIGHT.pop(url, None)


async def _invalidate_pull_request_cache(url: str) -> None:
    """Supersede cached and in-flight data before a provider mutation."""
    await _invalidate_full_payload_cache(url)
    chip_status._invalidate_check_status(url)
