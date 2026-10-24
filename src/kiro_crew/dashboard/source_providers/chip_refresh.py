"""The background chip reads: when a sidebar chip is re-read from its provider, and how.

Refreshes are fire-and-forget, deduplicated per URL, bounded in flight and in
concurrency, and paced by the chip TTL -- except at an agent turn boundary, which
forces one read per URL per floor interval. A changed result is written to
``chip_status``, drops the now-stale full payload from ``cache`` (the chip half of
the mutual-invalidation protocol), and is broadcast to the owner's sinks.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from urllib.parse import quote

from kiro_crew.dashboard.source_providers import (
    cache,
    chip_status,
    github,
    gitlab,
    hosts,
    links,
    plugins,
    projection,
    runner,
    sanitize,
)
from kiro_crew.dashboard.source_providers.chip_status import _CheckUpdateCallback
from kiro_crew.dashboard.source_providers.contract import SourceProviderNotConfigured

# Bound both running and semaphore-waiting tasks. A paced overflow URL is
# re-stamped `(now, cached status)` instead of creating a new task on every slots
# request, so it waits out its own lifecycle clock again (60 s open, 900 s open on
# the idle tier, 1 h closed, 6 h merged). A forced overflow URL is not re-stamped.
_CHECK_PENDING_MAX = 16
# Public alias for periodic drivers that need to know the per-round admission
# cap so they can rotate which URLs they submit first across rounds (fair
# scheduling when the number of stale chips exceeds the cap).
CHECK_STATUS_PENDING_MAX = _CHECK_PENDING_MAX
_CHECK_TASKS: set[asyncio.Task] = set()  # keep strong refs until done
# An agent turn that touched a pull request (opened it, pushed, merged, drove a
# round of review) is the highest-signal moment to re-read it, so the turn-boundary
# hook bypasses the TTL instead of waiting out the periodic rotation. The floor
# bounds a rapid multi-turn session to one forced provider read per URL per
# interval; every other caller stays on plain TTL pacing.
_CHECK_FORCE_MIN_INTERVAL_SECS = 10.0
# The idle tier: chips on slots nobody is using (no turn running, and no turn
# ended and no owner opened the slot recently -- every tab restored at boot starts
# here). An open pull request on such a chip is re-read on this slower clock, and
# at most ``_IDLE_CHECK_PER_ROUND`` of those re-reads start per chip TTL across
# the whole gateway, oldest read first. So the provider cost of a long sidebar
# history is a small constant instead of a burst that grows with every PR link
# the dashboard ever showed. A chip never read since boot is not on this budget:
# it gets its first read at the normal admission pace, so chips still fill in
# after a restart.
_IDLE_CHECK_TTL_SECS = 15 * 60
_IDLE_CHECK_PER_ROUND = 2
# [window start, idle re-reads started in it]. One window for every owner
# socket's driver, so a second dashboard window does not double the idle cost.
_idle_check_window: list[float] = [float("-inf"), 0.0]


def _chip_refresh_due(
    entry: tuple[float, dict[str, str] | None] | None,
    now: float,
    *,
    force: bool,
    open_ttl: float | None = None,
) -> bool:
    """Whether a chip-cache entry has aged out of its TTL.

    ``force`` (a turn boundary) bypasses the TTL for every lifecycle except
    ``merged``: a PR the agent may have just reopened is worth a forced read, a
    merged one cannot change and would only spend a provider subprocess per turn
    for as long as its chip stays in the sidebar. A merged chip still keeps its
    own clock under force: once that clock has run out, a forced call reads it.

    ``open_ttl`` replaces the open-PR TTL (the idle tier passes
    ``_IDLE_CHECK_TTL_SECS``); merged and closed chips keep their own clocks.
    """
    if entry is None:
        return True
    stamped_at, status = entry
    state = projection._chip_state(status)
    if state == "merged":
        return now - stamped_at >= cache._TERMINAL_TTL_SECS
    if force:
        return True
    return now - stamped_at >= _chip_ttl(state, open_ttl)


def _chip_ttl(state: str, open_ttl: float | None = None) -> float:
    """The clock a paced ``_chip_refresh_due`` applies to a chip state."""
    if state in projection._TERMINAL_CHIP_STATES:
        return cache._lifecycle_ttl(state)
    if open_ttl is None:
        return chip_status._CHECK_TTL_SECS
    return open_ttl


def schedule_check_refresh(
    urls: list[str], on_update: _CheckUpdateCallback | None = None, *, force: bool = False
) -> list[str]:
    """Kick bounded background refreshes for stale URLs without blocking.

    Returns the URLs whose value is expected to change shortly — the ones this
    call started plus the due ones a prior call already has in flight (an
    in-flight URL that is not due is not returned). Callers that
    serve the cache to a client (the source-strip status endpoint) use it to tell
    the client "poll again soon" instead of leaving it on TTL pacing, which would
    surface a just-refreshed state up to one extra TTL late. URLs deferred by the
    pending-work cap are deliberately excluded: nothing is coming sooner. A paced
    deferred URL is re-stamped with its cached status, so it waits out its own
    lifecycle clock again; a forced one is not re-stamped.

    ``force`` skips the TTL check for event-driven callers that know the remote
    state just moved (see ``request_check_refresh_now``). The pending cap and
    inflight dedup still apply, so a forced round can never outgrow a paced one.
    A finished PR ages on its own clock instead of the open-PR TTL -- a merged one
    by ``_TERMINAL_TTL_SECS``, a closed one by ``_CLOSED_TTL_SECS`` -- and force
    does not bypass a MERGED one's clock (``_chip_refresh_due``).
    """
    now = time.monotonic()
    refreshing: list[str] = []
    for url in dict.fromkeys(urls):
        entry = chip_status._check_cache.get(url)
        if not _chip_refresh_due(entry, now, force=force):
            continue
        if url in chip_status._check_inflight:
            refreshing.append(url)
            continue
        if len(chip_status._check_inflight) >= _CHECK_PENDING_MAX:
            # A paced (TTL) caller over the cap renews its timestamp with its
            # cached status, to stop the slots endpoint re-attempting every poll;
            # it then waits out its own lifecycle clock again (60 s open, 900 s
            # open on the idle tier, 1 h closed, 6 h merged). A forced caller
            # (turn boundary) must instead stay eligible: renewing the
            # timestamp here would push the periodic sweep out by a
            # TTL and make the chip STALER in exactly the contention case force
            # exists for (more PR-linked slots than the pending cap).
            if not force:
                chip_status._check_cache[url] = (now, entry[1] if entry else None)
                chip_status._trim_check_cache()
            continue
        chip_status._check_inflight.add(url)
        refreshing.append(url)
        if force:
            # Every forced read that STARTS burns the URL's floor allowance, here
            # and only here, whichever caller forced it. A URL already in flight
            # or deferred by the cap above is not stamped: its read is pre-turn or
            # never ran, and stamping it would lock out the corrective read.
            chip_status._check_forced_at[url] = now
        task = asyncio.get_running_loop().create_task(_refresh_check_status(url, on_update))
        _CHECK_TASKS.add(task)
        task.add_done_callback(_CHECK_TASKS.discard)
    return refreshing


def schedule_idle_check_refresh(
    urls: list[str], on_update: _CheckUpdateCallback | None = None
) -> list[str]:
    """The idle tier of the chip sweep: chips on slots nobody is using.

    Same fire-and-forget contract and return value as ``schedule_check_refresh``,
    with two differences that bound the cost of a long sidebar history:

    * a chip that has been read is due again only after ``_IDLE_CHECK_TTL_SECS``
      (merged and closed chips keep their longer lifecycle clocks), and at most
      ``_IDLE_CHECK_PER_ROUND`` such re-reads start per chip TTL, gateway-wide,
      the longest-unread first so none is starved;
    * a chip never read since boot is read now, but only up to the free pending
      capacity. Submitting more would make the pending cap stamp the overflow as
      "backed off" with no status, and it would then wait out the chip TTL with
      no status at all. A chip that has never yielded a status (its first read
      failed, or the pending cap backed it off) is retried on that same path
      once the chip TTL has passed, so a boot hiccup does not leave it blank for the slow clock.
    """
    now = time.monotonic()
    unread: list[str] = []
    due: list[tuple[float, str]] = []
    for url in dict.fromkeys(urls):
        if url in chip_status._check_inflight:
            continue
        entry = chip_status._check_cache.get(url)
        if entry is None:
            unread.append(url)
        elif entry[1] is None:
            if now - entry[0] >= chip_status._CHECK_TTL_SECS:
                unread.append(url)
        elif _chip_refresh_due(entry, now, force=False, open_ttl=_IDLE_CHECK_TTL_SECS):
            due.append((entry[0], url))
    free = max(_CHECK_PENDING_MAX - len(chip_status._check_inflight), 0)
    refreshing = schedule_check_refresh(unread[:free], on_update) if unread[:free] else []
    window_start, started = _idle_check_window
    if now - window_start >= chip_status._CHECK_TTL_SECS:
        window_start, started = now, 0.0
    free = max(_CHECK_PENDING_MAX - len(chip_status._check_inflight), 0)
    budget = min(max(_IDLE_CHECK_PER_ROUND - int(started), 0), free)
    picked = [url for _, url in sorted(due)[:budget]]
    if picked:
        reread = schedule_check_refresh(picked, on_update)
        started += len(reread)
        refreshing.extend(reread)
    _idle_check_window[:] = [window_start, started]
    return refreshing


def schedule_tiered_check_refresh(
    urls: list[str], idle: set[str], on_update: _CheckUpdateCallback | None = None
) -> None:
    """Split one chip sweep between the two tiers, keeping its order.

    ``idle`` is the set of URLs that only idle slots link
    (``DashboardState.idle_source_link_urls``): those go to
    :func:`schedule_idle_check_refresh`, the rest to the chip TTL. Every
    periodic or list-driven sweep goes through here, so none can skip the tier.
    """
    fast_urls = [url for url in urls if url not in idle]
    idle_urls = [url for url in urls if url in idle]
    if fast_urls:
        schedule_check_refresh(fast_urls, on_update)
    if idle_urls:
        schedule_idle_check_refresh(idle_urls, on_update)


def request_check_refresh_now(
    urls: list[str], on_update: _CheckUpdateCallback | None = None
) -> list[str]:
    """TTL-bypassing refresh for event-driven callers (agent turn boundaries).

    A finished agent turn is the moment a PR most likely changed, so waiting out
    the 60s chip TTL — or the periodic loop's rotation, which with more PR-linked
    slots than ``CHECK_STATUS_PENDING_MAX`` can take minutes — leaves both the
    chips and the detail panel visibly behind reality. Each URL is floored to one
    forced read per ``_CHECK_FORCE_MIN_INTERVAL_SECS`` so a burst of short turns
    cannot turn into a burst of provider subprocesses; URLs inside the floor fall
    back to normal TTL pacing rather than being dropped.
    """
    now = time.monotonic()
    eligible: list[str] = []
    paced: list[str] = []
    for url in dict.fromkeys(urls):
        last = chip_status._check_forced_at.get(url)
        if last is not None and now - last < _CHECK_FORCE_MIN_INTERVAL_SECS:
            paced.append(url)
            continue
        eligible.append(url)
    inflight_before = set(chip_status._check_inflight)
    # `schedule_check_refresh` stamps the floor for every forced read it starts.
    refreshing = schedule_check_refresh(eligible, on_update, force=True)
    # Of the URLs it returns, the ones a prior fetch already had in flight did
    # not do a fresh post-turn read: their fetch may predate this turn's push.
    already = [url for url in refreshing if url in inflight_before]
    # For URLs whose in-flight fetch predates this turn boundary, request exactly
    # one follow-up forced read on completion (see `_refresh_check_status`), so a
    # stale pre-turn result cannot pin the chip for a full TTL.
    for url in already:
        chip_status._check_force_pending.add(url)
    chip_status._trim_check_cache()
    if paced:
        refreshing.extend(schedule_check_refresh(paced, on_update))
    return refreshing


# Internal marker `_fetch_check_status` sets when the core chip read succeeded
# but the isolated rollup read alone failed (or described a different head).
# `_refresh_check_status` — the sole consumer — POPS it before the status is
# cached or compared, so only the documented chip keys
# ({state, ci, mergeable, mergeStateStatus}) ever reach slot serialization.
# It exists because "rollup unavailable" and "rollup empty" are otherwise the
# same absent `ci` key, and only the former may keep a glyph an earlier read knew.
_CHIP_CI_UNAVAILABLE = "ciUnavailable"


async def _refresh_check_status(url: str, on_update: _CheckUpdateCallback | None = None) -> None:
    previous = chip_status._check_cache.get(url)
    generation = chip_status._check_generations.get(url, 0)
    # Set when a queued follow-up forced read was due but did not start (the
    # pending cap refused it); the result below is then cached as already due.
    followup_deferred = False
    try:
        async with chip_status._check_semaphore:
            status = await _fetch_check_status(url)
    except Exception:
        status = None
    finally:
        chip_status._check_inflight.discard(url)
        if url in chip_status._check_force_pending:
            # A turn-boundary force arrived while THIS (possibly pre-turn) fetch
            # was in flight. Its result may predate the turn's final push, so
            # issue exactly one follow-up forced read now that the URL is free.
            # The follow-up starts fresh (not in flight), so it is not re-queued
            # here — at most one follow-up per in-flight collision, bounded.
            chip_status._check_force_pending.discard(url)
            # A follow-up that starts is floor-stamped by `schedule_check_refresh`
            # itself, so a turn boundary inside the floor is paced instead of
            # queueing yet another follow-up.
            followup: list[str] = []
            with contextlib.suppress(Exception):
                followup = schedule_check_refresh([url], on_update, force=True)
            # The URL left `_check_inflight` above, so a due follow-up that did
            # not start was refused by the pending cap.
            if url not in followup and _chip_refresh_due(
                chip_status._check_cache.get(url), time.monotonic(), force=True
            ):
                followup_deferred = True
    if chip_status._check_generations.get(url, 0) != generation:
        # A mutation superseded this URL while the fetch was in flight, so the
        # result describes the pre-mutation state. Drop it rather than let it
        # overwrite the invalidated entry.
        return
    ci_unavailable = False
    if status is not None:
        # Strip the internal marker BEFORE the status is cached or compared:
        # cache entries feed owner slot serialization, which must only ever
        # carry the documented chip keys.
        ci_unavailable = bool(status.pop(_CHIP_CI_UNAVAILABLE, None))
        if not status:
            status = None
    # A transient provider failure must not erase a known status. It still
    # refreshes the timestamp so repeated slots requests respect the TTL.
    if status is None and previous:
        status = previous[1]
    elif (
        ci_unavailable
        and status is not None
        and "ci" not in status
        and previous
        and previous[1]
        and "ci" in previous[1]
    ):
        # The CI portion ALONE was unavailable this round (rollup read failed
        # or straddled a push) while the authorized core fields survived.
        # Mirror `record_full_payload_status`'s keep-known rule for a partial
        # `checks` section: a degraded read must not erase a glyph the cache
        # already knows — but a SUCCESSFUL rollup with zero checks (no marker)
        # must still be allowed to clear a stale one.
        status = chip_status.carry_known_ci(status, previous[1])
    # Re-read the cache AFTER the provider await. The turn-boundary design makes
    # a concurrent full fetch the COMMON case: on `chat_done` the client
    # invalidates the detail payload (starting a full fetch) at the same moment
    # `refresh_slot_source_status` forces a chip refresh for the same URL. If the
    # full fetch resolved first it already wrote the fresh projection into
    # `_check_cache` via `record_full_payload_status`. Comparing against the
    # stale pre-await `previous` would then let this (possibly older) chip read
    # overwrite the newer value, spuriously judge "changed", drop the just-stored
    # full payload, and emit a redundant delta — roughly doubling provider cost
    # on every status-changing turn boundary and briefly broadcasting the wrong
    # status. `_check_inflight` dedups concurrent chip refreshes, so the only
    # writer that can land here is `record_full_payload_status`; when it did,
    # defer to it entirely rather than clobber the richer full-payload projection.
    latest = chip_status._check_cache.get(url)
    if latest is not previous:
        return
    if status is not None:
        # Same keep-known rule as the full-payload writer: an unsettled merge read
        # must not erase a settled one, or this refresh would strip the pair,
        # judge itself "changed", and drive the invalidation loop the flap damper
        # below contains.
        status = chip_status._keep_known_merge_state(status, previous[1] if previous else None)
    stamped_at = time.monotonic()
    if followup_deferred:
        # The pending cap deferred the follow-up forced read, so this possibly
        # pre-turn result must not be cached as fresh for a full TTL: stamp it
        # already due so the next paced sweep re-reads it.
        stamped_at -= _chip_ttl(projection._chip_state(status))
    chip_status._check_cache[url] = (stamped_at, status)
    chip_status._trim_check_cache()
    changed = status is not None and (previous is None or previous[1] != status)
    if not changed:
        return
    assert status is not None  # narrowed by `changed`
    # Lockstep visibility revalidation — FIRST, before flap handling
    # and before the first ``await``. A status refresh can land a freshly-fetched
    # (possibly now-private) status while this URL's visibility entry is still
    # within its TTL, so ``is_repo_public`` would authorize the new status
    # against a stale-fresh public flag. ``schedule_visibility_refresh(force=True)``
    # SYNCHRONOUSLY fails a cached-public entry closed for the in-flight window
    # (it pre-invalidates before spawning the refresh task), so it must run
    # before any ``await`` yields the event loop and before the flap path's
    # early return — otherwise a concurrent slots push (or the flap path, which
    # returns before a later call site would run) could observe the newly-cached
    # private status against an un-invalidated public flag. Bounded to real
    # status transitions only.
    with contextlib.suppress(Exception):
        chip_status.schedule_visibility_refresh([url], on_update, force=True)
    # Structural loop-breaker: if this URL keeps repeating the identical chip
    # transition every refresh, the chip and full-payload projections disagree
    # on vocabulary and the mutual-invalidation protocol below would spin a
    # provider-polling loop forever. Cap the blast radius — still update the chip
    # cache and re-serialize the sidebar, but stop driving the full-payload
    # invalidation + delta that closes the loop, so the divergence degrades to a
    # stale glyph instead of an unbounded loop.
    if chip_status._note_check_flap(url, previous[1] if previous else None, status):
        if on_update:
            chip_status._queue_check_update(on_update)
        return
    # The chip cache just learned the PR moved, so the full payload behind the
    # detail panel is known-stale. Drop ONLY the full payload (rather than let it
    # live out its own TTL) and tell owner dashboards, so the panel and the chip
    # can never render two different lifecycles for the same PR. We must not
    # invalidate the chip cache here: this refresh just wrote the fresh chip
    # entry, and clearing it (as the mutation-path `_invalidate_pull_request_cache`
    # does) would bump the chip generation and re-judge the next refresh as
    # "changed", spinning the mutual-invalidation loop.
    with contextlib.suppress(Exception):
        await cache._invalidate_full_payload_cache(url)
    chip_status._emit_status_delta(url, status, "chip")
    if on_update:
        chip_status._queue_check_update(on_update)


def _normalized_merge_field(value: object) -> str:
    """A plugin-supplied merge field in the shared vocabulary, or ``""`` when not a short string.

    The vocabulary is open (GitLab's ``detailed_merge_status`` values pass
    through unmapped), so the discipline is the one every chip field gets: a
    short string, redacted like the rest. ``_record_merge_state`` then keeps
    only a settled answer.
    """
    if not isinstance(value, str) or len(value) > 32:
        return ""
    return str(sanitize._redact_provider_data(value))


async def _fetch_check_status(url: str) -> dict[str, str] | None:
    # Refresh the self-managed GitLab allowlist off the event loop before any
    # URL validation reads the cached snapshot.
    await hosts.ensure_gitlab_hosts_loaded()
    # Belt-and-braces: the callers that feed this path already drop issue links
    # (see DashboardState.source_link_urls), but a chip refresh is what reaches
    # `gh pr view`, so refuse an issue URL here too rather than rely on every
    # future scheduling site remembering to filter.
    ref = links._require_change_ref(links.parse_source_url(url))
    result: dict[str, str] = {}
    # A registered provider projects its own chip status. Optional: a plugin
    # without the hook simply contributes no {ci, state} glyph, which renders as a
    # plain chip -- strictly better than falling into the GitLab branch and
    # running `glab` against a host it knows nothing about.
    plugin = plugins._plugin_for_change(ref)
    if plugin is not None:
        hook = getattr(plugin, "fetch_check_status", None)
        if not callable(hook):
            return None
        try:
            with plugins._plugin_errors(plugin.id):
                status = await hook(ref)
        except SourceProviderNotConfigured as exc:
            raise plugins._plugin_setup_error(plugin, exc) from exc
        if not isinstance(status, dict):
            return None
        # Same redaction and key discipline as a built-in read: only the fields
        # the chip renders survive, and each must be a short string.
        # ``ciPending`` survives only for a provider in ``CI_PENDING_PROVIDERS``,
        # the providers ``status_from_full_payload`` projects it for, so the
        # chip read and the write-through agree; for any other plugin it is
        # dropped. The merge pair passes through as the built-in reads record
        # it: a provider whose full payload carries the pair writes it into the
        # chip cache, and the keep-known rule below carries a recorded pair
        # forward whenever a refresh omits it, so a chip read that could not
        # answer the pair would pin the full fetch's verdict until the next
        # full fetch. Only the normalized vocabulary is admitted.
        projected = sanitize._redact_provider_data(
            {
                key: value
                for key, value in status.items()
                if key in {"ci", "state", projection.CI_PENDING_KEY}
                and isinstance(value, str)
                and len(value) <= 32
            }
        )
        projection._record_merge_state(
            projected,
            _normalized_merge_field(status.get("mergeable")),
            _normalized_merge_field(status.get("mergeStateStatus")),
        )
        # The marker only means something beside a failed rollup.
        if (
            plugin.id not in projection.CI_PENDING_PROVIDERS
            or projected.get("ci") != "failed"
            or projected.get(projection.CI_PENDING_KEY) != "1"
        ):
            projected.pop(projection.CI_PENDING_KEY, None)
        return projected or None
    if ref.provider == "github":
        # The rollup is read separately from the core fields: `gh` resolves a
        # `--json` field set atomically, so bundling `statusCheckRollup` here
        # would make a token without Checks read access lose the state/draft/merge
        # data it IS authorized to read. The two reads run concurrently; only the
        # core read is load-bearing.
        data_raw, rollup_raw = await asyncio.gather(
            runner._run_json(
                "gh",
                "pr",
                "view",
                ref.url,
                "--json",
                "state,isDraft,mergeable,mergeStateStatus,headRefOid",
                host=ref.host,
            ),
            github._github_rollup_read(ref),
            return_exceptions=True,
        )
        if isinstance(data_raw, BaseException):
            raise data_raw
        data = data_raw
        if not isinstance(data, dict):
            return None
        if isinstance(rollup_raw, BaseException):
            # Core data survives; flag the CI portion unavailable so the cache
            # writer can keep the glyph an earlier read knew instead of erasing it.
            result[_CHIP_CI_UNAVAILABLE] = "1"
        else:
            checks, rollup_head = rollup_raw
            head_oid = str(data.get("headRefOid") or "")
            # A missing sha on either side deliberately fails open, same as
            # the full-payload guard: unverifiable must not mean unavailable.
            if head_oid and rollup_head and rollup_head != head_oid:
                # The two reads straddled a push — this rollup describes a
                # different commit. Treat it as unavailable rather than paint
                # another head's CI on this one; the next refresh re-pairs.
                result[_CHIP_CI_UNAVAILABLE] = "1"
            else:
                # Same projection AND the same latest-run collapsing as the
                # full payload (`_github_checks`, applied inside
                # `_github_rollup_read`), so the chip glyph cannot disagree
                # with the panel's own rollup — a superseded CANCELLED row must
                # not paint either red.
                projection._record_ci(result, checks)
        raw_state = str(data.get("state") or "").upper()
        state = projection._project_state(raw_state, draft=bool(data.get("isDraft")))
        if state is not None:
            result["state"] = state
        projection._record_merge_state(result, *github._github_merge_state(data))
        return result or None
    project = quote(ref.project, safe="")
    details = await runner._run_json(
        "glab", "api", f"projects/{project}/merge_requests/{ref.number}", host=ref.host
    )
    head_status = ""
    if isinstance(details, dict):
        # Same {state} vocabulary as the full-payload path via `_project_state`:
        # GitLab keeps `draft: true` on an MR closed while still in draft, so the
        # draft mapping is gated on the open state and `locked` folds into
        # `closed`. A mismatch here would ping-pong under the mutual
        # invalidation (chip "draft" ≠ cached "closed", drop, refetch, repeat).
        state = projection._project_state(
            str(details.get("state") or ""),
            draft=bool(details.get("draft") or details.get("work_in_progress")),
        )
        if state is not None:
            result["state"] = state
        projection._record_merge_state(result, *gitlab._gitlab_merge_state(details))
        # head_pipeline is the MR's own HEAD pipeline and ships with this same
        # payload, so the common case needs one provider call like GitHub does.
        head = details.get("head_pipeline")
        if isinstance(head, dict):
            head_status = str(head.get("status") or "").lower()
    if head_status:
        status = head_status
    else:
        pipelines = await runner._run_json(
            "glab",
            "api",
            f"projects/{project}/merge_requests/{ref.number}/pipelines?per_page=1",
            host=ref.host,
        )
        rows = projection._as_list(pipelines)
        status = str(rows[0].get("status") or "").lower() if rows else ""
    if status:
        # Project the pipeline AGGREGATE through the SAME helper the full-payload
        # path uses (`_gitlab_aggregate_ci`, consumed there via `ciStatus`), so
        # the chip glyph and the panel glyph cannot drift. The aggregate already
        # folds allow_failure into `success` and marks a blocking manual gate, so
        # it is the authoritative, lossless source for the single CI glyph.
        ci = gitlab._gitlab_aggregate_ci(status)
        if ci is not None:
            result["ci"] = ci
    return result or None
