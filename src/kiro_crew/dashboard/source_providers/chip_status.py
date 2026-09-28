"""The sidebar chip status every surface reads, and the rules its two writers share.

A chip shows a pull request's ``{state, ci}`` plus the merge pair, from a cache
kept separate from the full payload so a slots response never waits on a
provider. Two writers fill it: the background chip read (``chip_refresh``) and the
full-payload write-through (:func:`record_full_payload_status`). Both keep a known
CI glyph and a settled merge answer when a fresh read has none, both fan a changed
status out to the owner's sinks, and both revalidate the repository's visibility
in lockstep -- the gate that lets a non-owner see status only for a repository
known to be public.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from kiro_crew.dashboard.source_providers import LOGGER_NAME, links, projection, runner
from kiro_crew.dashboard.source_providers.contract import SourceRef

logger = logging.getLogger(LOGGER_NAME)


# ── Lightweight CI check status for sidebar chips ────────────────────────────
# Separate from the full PR cache: chips poll with the slots list, so this
# path must never block a slots response. Reads are served from this cache;
# refreshes are fire-and-forget with inflight dedup and a bounded map.

_CHECK_TTL_SECS = 60
# Public alias for periodic drivers (the owner-WS refresh loop) that pace
# their wakeups to the cache TTL. Sleeping exactly one TTL between rounds
# means each round finds the previous round's entries just expired — one
# provider fetch per URL per TTL, no wasted wakeups.
CHECK_STATUS_TTL_SECS = _CHECK_TTL_SECS
# The dashboard caps live slots at 500. Keeping one tiny status entry per slot
# avoids eviction churn when a large workspace is open.
_CHECK_CACHE_MAX = 512
_CHECK_UPDATE_DEBOUNCE_SECS = 0.1
# Bound concurrent gh/glab refresh operations so a cold cache across many
# sessions can't spawn a burst of provider subprocesses at once. TTL + inflight
# dedup handle rate and duplication; this caps instantaneous concurrency.
_CHECK_CONCURRENCY = 4
# These globals are loop-affine: the dashboard creates and mutates them only
# from its single asyncio event loop. They are not thread-safe by design.
_check_semaphore = asyncio.Semaphore(_CHECK_CONCURRENCY)
_check_cache: dict[str, tuple[float, dict[str, str] | None]] = {}
# URLs whose chip read is running now (started by ``chip_refresh``).
_check_inflight: set[str] = set()
# Bumped when a mutation supersedes a URL's status. A refresh that started
# before the bump must not write its now-stale result back into the cache,
# which would otherwise restore the pre-mutation state for up to one TTL.
_check_generations: dict[str, int] = {}
_CheckUpdateCallback = Callable[[], None]
_check_update_callbacks: set[_CheckUpdateCallback] = set()
_check_update_handle: asyncio.TimerHandle | None = None
# When a turn boundary last force-read each URL (the floor is
# ``chip_refresh._CHECK_FORCE_MIN_INTERVAL_SECS``). Kept beside the cache so one
# trim bounds every per-URL chip map together.
_check_forced_at: dict[str, float] = {}
# URLs for which a turn-boundary forced refresh arrived while a (possibly
# pre-turn, now-stale) chip fetch was already in flight. The in-flight fetch is
# NOT floor-stamped (it may return pre-turn data), and when it completes
# ``_refresh_check_status`` issues exactly one follow-up forced read so the
# post-turn state is actually observed instead of waiting out the TTL.
_check_force_pending: set[str] = set()
# Status-delta sinks receive {"url", "ci"?, "state"?} whenever a URL's cached
# chip status CHANGES, so owner dashboards can invalidate the matching
# pull-request detail query immediately instead of waiting out a poll interval.
# Status is credential-backed, so sinks must be owner-scoped.
_StatusDeltaSink = Callable[[dict[str, str]], None]
_status_delta_sinks: set[_StatusDeltaSink] = set()
# Structural loop-breaker for the chip <-> full-payload mutual-invalidation
# protocol. The two caches project a provider's raw state independently and are
# kept equivalent only by convention ("keep the two in step"). Should they ever
# disagree on a URL's vocabulary (a bug), the chip refresh observes the SAME
# changed transition every TTL cycle — chip re-projects value A, the client's
# full refetch re-projects value B back into the cache — so the invalidation
# protocol would spawn a provider subprocess per URL per cycle indefinitely,
# silently. Rather than trust the two projections to stay identical as
# GitHub/GitLab vocabularies evolve, cap the blast radius: once a URL repeats an
# identical (previous -> new) changed-transition past this threshold, stop
# driving the loop for it and log loudly, so a divergence degrades to a stale
# glyph instead of an unbounded polling loop. A genuinely changing PR produces
# DISTINCT transitions, which resets the counter, so it is never damped.
_CHECK_FLAP_DAMP_THRESHOLD = 3
_check_flap: dict[str, tuple[tuple[str, str], int]] = {}
_check_flap_damped: set[str] = set()


def _status_sig(status: dict[str, str] | None) -> str:
    """Stable, order-independent signature of a chip status for flap detection."""
    if not status:
        return ""
    return "|".join(f"{key}={status[key]}" for key in sorted(status))


def _note_check_flap(url: str, previous: dict[str, str] | None, status: dict[str, str]) -> bool:
    """Track repeated identical changed-transitions; return True to damp the loop.

    See ``_check_flap`` above. The chip refresh calls this on every *changed*
    transition it is about to act on. When the same (previous -> new) transition
    recurs past ``_CHECK_FLAP_DAMP_THRESHOLD`` times in a row for one URL, the
    two projections are flapping and the caller must stop invalidating the full
    payload for it. Any different transition (a real state change) resets the
    counter and clears the damp.
    """
    transition = (_status_sig(previous), _status_sig(status))
    last, count = _check_flap.get(url, (None, 0))
    if transition == last:
        count += 1
    else:
        count = 1
        _check_flap_damped.discard(url)
    _check_flap[url] = (transition, count)
    if count >= _CHECK_FLAP_DAMP_THRESHOLD:
        if url not in _check_flap_damped:
            _check_flap_damped.add(url)
            logger.warning(
                "source-status: suppressing full-payload invalidation for %s — chip "
                "status keeps flapping (%s -> %s) every refresh, which means the chip "
                "and full-payload projections disagree on vocabulary (a bug); the chip "
                "glyph may now be stale until the two projections are reconciled",
                url,
                transition[0] or "<none>",
                transition[1] or "<none>",
            )
        return True
    return False


def _clear_check_flap(url: str) -> None:
    """Reset a URL's flap tracker after an authoritative full-payload write.

    Called by ``record_full_payload_status`` when the full fetch changes the
    cached status, so an interleaved full write is not misread as part of a
    repeating chip transition (which would otherwise falsely damp real churn).
    """
    _check_flap.pop(url, None)
    _check_flap_damped.discard(url)


# ── Repository visibility (public vs private) ────────────────────────────────
# Chip status (state / CI rollup) is credential-backed provider data, so it is
# sent only to the owner connection by default. But for a PUBLIC repository that
# same lifecycle state is world-visible on the provider's website, so withholding
# it from a legitimate authenticated dashboard user buys no confidentiality — it
# only removes the chip's most useful signal (is this PR merged / closed / green).
#
# This cache lets the status gate admit a public-repo link for a dashboard-user
# connection while keeping PRIVATE repos strictly owner-only. It is keyed by
# ``provider|host|owner|repo`` (not by PR URL): visibility is a property of the
# repository, and one repo backs many PR chips — so a per-repo entry, refreshed
# on the SAME cadence as the chip status it gates, costs at most one extra
# provider read per repo per TTL, shared across every chip on it.
#
# Fails CLOSED: until a repo is positively known public, ``is_repo_public``
# returns None and the gate treats it as owner-only. A provider read that errors
# or is unauthorized never flips a repo to public.
#
# TTL == the CHECK TTL, deliberately: the status a public flag authorizes is
# refreshed every ``_CHECK_TTL_SECS`` and ``schedule_visibility_refresh`` runs
# on the SAME calls, so visibility is never more than one refresh cycle staler
# than the status it gates. A repo that flips public->private therefore stops
# authorizing non-owner status within ~one check TTL (not an hour): the paired
# status refresh re-reads visibility, the flip is observed, and a stale entry
# fails closed the moment it crosses this TTL. This bounds the private-status
# exposure to a single short refresh window rather than a long one.
_VISIBILITY_TTL_SECS = _CHECK_TTL_SECS
_VISIBILITY_CACHE_MAX = 512
# provider|host|owner|repo -> (fetched_monotonic, is_public | None)
_visibility_cache: dict[str, tuple[float, bool | None]] = {}
_visibility_inflight: set[str] = set()
# Per-key counter bumped every time a force=True refresh fails a cached-public
# entry closed. ``_refresh_repo_visibility`` captures this at start and refuses
# to write a positive (public) result if the generation changed while it was
# fetching — i.e. a public->private force-invalidation landed mid-flight — so a
# stale in-flight read can never RESTORE public across a flip. The next refresh
# reconfirms from a fail-closed baseline.
_visibility_force_gen: dict[str, int] = {}
_VISIBILITY_TASKS: set[asyncio.Task] = set()
# Jira has no public-repo concept and its status is credential-gated regardless,
# so visibility is only meaningful for change providers.
_VISIBILITY_PROVIDERS = frozenset({"github", "gitlab"})


def _visibility_key(ref: SourceRef) -> str:
    return f"{ref.provider}|{ref.host}|{ref.owner}|{ref.repo}"


def _trim_visibility_cache() -> None:
    while len(_visibility_cache) > _VISIBILITY_CACHE_MAX:
        del _visibility_cache[min(_visibility_cache, key=lambda k: _visibility_cache[k][0])]


def is_repo_public(url: str) -> bool | None:
    """Whether the repo behind a source URL is known PUBLIC.

    Returns True (known public), False (known private), or None (not yet
    fetched / unknown / STALE / not a change provider). The status gate treats
    anything other than True as owner-only, so an unfetched, errored, or stale
    repo never leaks private status to a non-owner. Never blocks — reads the
    cache only.

    A cache entry older than ``_VISIBILITY_TTL_SECS`` is treated as STALE and
    returns None: a repo that flipped public->private while its visibility
    refresh kept failing must not keep authorizing status forever. The exposure
    is bounded to one TTL from the last SUCCESSFUL read (``_refresh_repo_visibility``
    never resets the timestamp on a failed read), after which this fails closed.
    """
    try:
        ref = links.parse_source_url(url)
    except Exception:
        return None
    if ref.provider not in _VISIBILITY_PROVIDERS:
        return None
    entry = _visibility_cache.get(_visibility_key(ref))
    if not entry:
        return None
    fetched_at, value = entry
    if time.monotonic() - fetched_at >= _VISIBILITY_TTL_SECS:
        return None
    return value


async def _fetch_repo_visibility(ref: SourceRef) -> bool | None:
    """Read a repo's public/private flag via the provider CLI. None on failure."""
    try:
        if ref.provider == "github":
            # isPrivate is False for BOTH public AND internal (GitHub Enterprise)
            # repos, but an internal repo is visible only to enterprise members —
            # NOT anonymously — so classifying it public would leak credential-
            # backed status to a non-owner. Read `visibility` and
            # require exactly "public" (mirrors the GitLab "public"-only gate);
            # "internal"/"private" → owner-only. isPrivate is kept only as a
            # belt-and-braces private check.
            data = await runner._run_json(
                "gh",
                "repo",
                "view",
                f"{ref.owner}/{ref.repo}",
                "--json",
                "isPrivate,visibility",
            )
            if not isinstance(data, dict):
                return None
            if data.get("isPrivate") is True:
                return False
            vis = data.get("visibility")
            if isinstance(vis, str):
                return vis.lower() == "public"
            return None
        if ref.provider == "gitlab":
            # GitLab exposes repository visibility as public/internal/private.
            # Only "public" is world-readable without a credential; "internal"
            # is visible to authenticated instance members, which is NOT the
            # same as anonymous-public, so it stays owner-only.
            #
            # But a PUBLIC project can still restrict individual features:
            # merge_requests_access_level / builds_access_level can be "private"
            # (members only) or "disabled" even when the project is public, so a
            # credentialed refresh would otherwise surface member-only MR/CI
            # status to a non-owner. Require the project to be public
            # AND both feature levels to be "enabled" (available at the project's
            # public visibility, i.e. anonymously readable) before treating the
            # PR/MR lifecycle + CI status as public. GitHub has no such per-
            # feature split — a public repo's PRs and checks are public.
            #
            # quote(ref.project, safe="") — NOT f"{owner}%2F{repo}": a subgroup
            # project path (group/subgroup/repo) has interior slashes that must
            # all be percent-encoded, and owner/repo drops the subgroup segment
            # entirely. Mirrors every other glab-api call site.
            project = quote(ref.project, safe="")
            data = await runner._run_json("glab", "api", f"projects/{project}", host=ref.host)
            if not isinstance(data, dict) or not isinstance(data.get("visibility"), str):
                return None
            if data["visibility"] != "public":
                return False
            # "enabled" = available at the project's (public) visibility level;
            # "private"/"disabled" restrict the feature to members. Missing keys
            # fail closed (owner-only) rather than assuming anonymous access.
            mr_level = data.get("merge_requests_access_level")
            ci_level = data.get("builds_access_level")
            # public_jobs (a.k.a. "Public pipelines") is a SEPARATE gate: when
            # False, a public project with builds_access_level "enabled" still
            # hides pipeline/job status from non-members, so a credentialed
            # refresh would leak private CI state to a non-owner.
            # Require it True (missing → fail closed) before treating CI status
            # as anonymously public.
            public_jobs = data.get("public_jobs")
            return mr_level == "enabled" and ci_level == "enabled" and public_jobs is True
    except Exception:
        return None
    return None


async def _refresh_repo_visibility(
    ref: SourceRef,
    on_update: _CheckUpdateCallback | None = None,
    *,
    prev_public_override: bool | None = None,
) -> None:
    key = _visibility_key(ref)
    prev_entry = _visibility_cache.get(key)
    # Snapshot the force-invalidation generation at start. If a force=True
    # refresh fails this key closed WHILE we are fetching (generation bumps), our
    # read is stale w.r.t. that public->private flip, so we must NOT write back a
    # positive result that would restore ``public`` — we leave the fail-closed
    # unknown standing and let the next refresh reconfirm.
    start_gen = _visibility_force_gen.get(key, 0)
    # The RENDERED gate value before this refresh: True only if a fresh public
    # entry exists (mirrors ``is_repo_public``'s TTL check). A change in this
    # boolean is exactly when a chip appears or disappears for a non-owner.
    #
    # ``prev_public_override`` carries the rendered-public value captured BEFORE
    # a forced pre-invalidation clobbered the cache entry to unknown. Without it,
    # the force path would read its own just-written (now, None) as prev_public
    # =False, so a genuine public->private transition compares False==False and
    # fires no update — leaving connected non-owners on the stale public chip
    # indefinitely. The override restores the true baseline so the
    # hide-the-chip update is queued.
    if prev_public_override is not None:
        prev_public = prev_public_override
    else:
        prev_public = (
            bool(prev_entry[1]) and (time.monotonic() - prev_entry[0]) < _VISIBILITY_TTL_SECS
            if prev_entry
            else False
        )
    try:
        async with _check_semaphore:
            public = await _fetch_repo_visibility(ref)
    except Exception:
        public = None
    finally:
        _visibility_inflight.discard(key)
    prev = _visibility_cache.get(key)
    if public is not None and _visibility_force_gen.get(key, 0) != start_gen:
        # A force=True invalidation (public->private flip) landed while we were
        # fetching. Our positive read predates the flip, so restoring ``public``
        # here would re-open the leak the force path just closed. Discard the
        # stale positive: leave the fail-closed entry as-is (or record unknown)
        # so is_repo_public stays None until a post-flip refresh reconfirms.
        if prev is None:
            _visibility_cache[key] = (time.monotonic(), None)
    elif public is not None:
        # A positive read is authoritative: store the fresh value and reset the
        # TTL clock. This is the ONLY path that may mark a repo public.
        _visibility_cache[key] = (time.monotonic(), public)
    elif prev is not None:
        # Failed read. Keep the prior value but DO NOT reset the timestamp, so a
        # persistently-failing refresh cannot extend a stale ``public`` past its
        # TTL: it ages out from its last SUCCESSFUL read and ``is_repo_public``
        # then returns None (fail closed). This is the public->private +
        # visibility-read-fails hole — leaving the old timestamp bounds the
        # exposure to one TTL rather than forever.
        _visibility_cache[key] = (prev[0], prev[1])
    else:
        # Never successfully read: record an unknown so repeated cold failures
        # do not re-spawn a fetch every slots push (still returns None).
        _visibility_cache[key] = (time.monotonic(), None)
    _trim_visibility_cache()
    # Notify only when the RENDERED public flag flipped: a cold->public repo now
    # shows its chip status, and a public->private (or aged-out) repo hides it.
    # Without this a fresh visibility read never re-serializes the sidebar, so a
    # chip could stay bare until an unrelated push.
    new_entry = _visibility_cache.get(key)
    new_public = bool(new_entry and new_entry[1] is True)
    if on_update is not None and new_public != prev_public:
        _queue_check_update(on_update)


def schedule_visibility_refresh(
    urls: list[str], on_update: _CheckUpdateCallback | None = None, *, force: bool = False
) -> None:
    """Kick bounded background visibility reads for repos not freshly cached.

    Fire-and-forget with inflight dedup, mirroring ``schedule_check_refresh``.
    One entry per repo (deduped by visibility key), TTL-paced, so a large
    workspace of PRs on a handful of repos costs a handful of reads per repo per
    TTL.

    ``on_update`` is invoked (debounced) whenever a repo's RENDERED public flag
    flips, so the sidebar re-serializes when a chip should appear or disappear.
    ``force`` bypasses the TTL freshness check for callers that know the repo's
    status just moved (the turn-boundary refresh), so visibility is revalidated
    in lockstep with the forced status read rather than lagging it.

    On the ``force`` path the cached PUBLIC flag is invalidated SYNCHRONOUSLY
    before the refresh task is spawned: the forced status read and the
    visibility read run as concurrent tasks, and if status finished first it
    could otherwise broadcast fresh (now-private) status against a still-cached-
    public visibility entry (a non-owner private-status leak). Dropping the entry
    to unknown up front makes ``is_repo_public`` fail closed for the whole
    in-flight window; the refresh restores ``public`` only on a positive
    reconfirmation, and its ``on_update`` re-serializes when it does.
    """
    now = time.monotonic()
    seen: set[str] = set()
    for url in dict.fromkeys(urls):
        try:
            ref = links.parse_source_url(url)
        except Exception:
            continue
        if ref.provider not in _VISIBILITY_PROVIDERS:
            continue
        key = _visibility_key(ref)
        if key in seen:
            continue
        seen.add(key)
        entry = _visibility_cache.get(key)
        if not force and entry and now - entry[0] < _VISIBILITY_TTL_SECS:
            continue
        prev_public_override: bool | None = None
        if force:
            # Bump the force generation on EVERY forced refresh, BEFORE the
            # inflight-dedup return below and regardless of the current cache
            # value. A forced refresh means "the status just moved, revalidate
            # now"; any visibility read already in flight (which may have started
            # before a public->private flip) must be treated as stale and
            # refused write-back. Gating this bump on "currently public" is a
            # hole: an entry already dropped to unknown (e.g. a first force
            # landed, then a second arrives while the pre-privacy fetch is still
            # in flight) would skip the bump, and that in-flight positive read
            # could then restore ``public``.
            _visibility_force_gen[key] = _visibility_force_gen.get(key, 0) + 1
            if entry is not None and entry[1] is True:
                # Capture the TRUE rendered-public baseline BEFORE clobbering, so
                # the refresh's on_update comparison measures the flip against
                # what non-owners currently see (public), not the unknown we are
                # about to write. Otherwise a public->private transition compares
                # False==False and never hides the chip.
                prev_public_override = (now - entry[0]) < _VISIBILITY_TTL_SECS
                # Synchronously fail the entry closed so ``is_repo_public``
                # returns None for the whole in-flight window; the refresh
                # restores True only on a positive reconfirmation. Only clobber
                # when currently public — an already-unknown entry is already
                # fail-closed, and the generation bump above covers the stale
                # in-flight read either way.
                _visibility_cache[key] = (now, None)
        if key in _visibility_inflight:
            # A refresh is already running for this repo. We have already failed
            # a cached-public entry closed above on the force path, so the
            # in-flight result can only ever restore ``public`` via a positive
            # reconfirmation (never leave a stale public flag standing); dedup
            # the redundant spawn.
            continue
        _visibility_inflight.add(key)
        running_loop = asyncio.get_running_loop()
        # A task that never finished before its loop was torn down (a caller
        # closed the loop without awaiting/cancelling the task first) never
        # runs its done-callback, so ``discard`` never fires and it lingers in
        # this module-global set forever, bound to a now-dead loop. A later
        # caller on a DIFFERENT loop that gathers the set then crashes with
        # "Future belongs to a different loop". Prune those dead-loop entries
        # before adding this task, so the set only ever holds tasks the current
        # loop can legally await.
        for stale_task in list(_VISIBILITY_TASKS):
            if stale_task.get_loop() is not running_loop:
                _VISIBILITY_TASKS.discard(stale_task)
        task = running_loop.create_task(
            _refresh_repo_visibility(ref, on_update, prev_public_override=prev_public_override)
        )
        _VISIBILITY_TASKS.add(task)
        task.add_done_callback(_VISIBILITY_TASKS.discard)


def get_cached_check_status(url: str) -> dict[str, str] | None:
    """Cached status for a PR url: {"ci": ..., "state": ..., "mergeable": ...}.

    Every key is present only when known. ``mergeable``/``mergeStateStatus`` are
    omitted while the provider is still computing mergeability, so a client must
    treat their absence as "no news" rather than "nothing blocks the merge".
    Returns None until the first background refresh completes.
    """
    entry = _check_cache.get(url)
    return entry[1] if entry else None


def _trim_check_cache() -> None:
    while len(_check_cache) > _CHECK_CACHE_MAX:
        del _check_cache[min(_check_cache, key=lambda key: _check_cache[key][0])]
    if len(_check_generations) > _CHECK_CACHE_MAX:
        for url in [
            url
            for url in _check_generations
            if url not in _check_cache and url not in _check_inflight
        ]:
            del _check_generations[url]
    while len(_check_forced_at) > _CHECK_CACHE_MAX:
        del _check_forced_at[min(_check_forced_at, key=lambda key: _check_forced_at[key])]
    # Flap-tracking state is only meaningful while a URL is live in the cache;
    # drop entries for evicted URLs so these maps cannot outgrow the cache.
    if len(_check_flap) > _CHECK_CACHE_MAX:
        for stale in [key for key in _check_flap if key not in _check_cache]:
            _check_flap.pop(stale, None)
            _check_flap_damped.discard(stale)
    # Follow-up-force intent only matters while a fetch is actually in flight;
    # drop any stragglers whose fetch has finished so the set stays bounded.
    if len(_check_force_pending) > _CHECK_CACHE_MAX:
        for stale in [key for key in _check_force_pending if key not in _check_inflight]:
            _check_force_pending.discard(stale)


def _invalidate_check_status(url: str) -> None:
    """Drop a URL's cached chip status and supersede any in-flight refresh.

    The sidebar/source-strip chips read a separate, shorter-lived cache from the
    full pull-request payload, so a mutation that only busts the full cache
    would leave the chips showing pre-mutation state until their TTL expired.
    """
    _check_cache.pop(url, None)
    _check_generations[url] = _check_generations.get(url, 0) + 1
    _trim_check_cache()


def register_status_delta_sink(sink: _StatusDeltaSink) -> None:
    """Receive ``{"url", "ci"?, "state"?}`` whenever a chip status changes.

    Idempotent: registering the same bound method twice keeps one sink. Sinks
    must be owner-scoped — chip status is credential-backed provider data.
    """
    _status_delta_sinks.add(sink)


def unregister_status_delta_sink(sink: _StatusDeltaSink) -> None:
    _status_delta_sinks.discard(sink)


def _emit_status_delta(url: str, status: dict[str, str], origin: str) -> None:
    """Fan a changed status out to every registered sink, best-effort.

    ``origin`` records where the change was observed — ``"chip"`` (the
    lightweight refresh path) or ``"detail"`` (a full fetch's write-through) —
    and is **diagnostic only**: the client invalidates the detail payload for
    EVERY changed delta regardless of origin. It must, because a ``"detail"``
    delta is produced by the single window whose full fetch ran; only that
    window received the fresh HTTP payload, so the other owner windows (whose
    detail query is ``staleTime: Infinity``) would otherwise keep rendering the
    pre-change lifecycle. The initiating window's resulting refetch is harmless:
    ``record_full_payload_status`` only runs in the *uncached* fetch path, so
    the refetch hits the warm 30s cache and emits no further delta (no loop).
    The field is retained on the wire for diagnostics and possible future
    requester-aware routing; no consumer branches on it today.
    """
    if not _status_delta_sinks:
        return
    delta = {"url": url, "origin": origin, **status}
    for sink in tuple(_status_delta_sinks):
        with contextlib.suppress(Exception):
            sink(delta)


def _record_merge_state(result: dict[str, str], mergeable: str, merge_state: str) -> None:
    """Add the merge fields to a chip-status entry, each only once it is real.

    An unanswered field is left out entirely rather than written as ``unknown``:
    the chip cache is a short-TTL hint the client compares against its loaded
    pull-request payload, and "still computing" must not read as a disagreement
    with a real answer the payload already has. The two fields are recorded
    independently because GitLab settles `need_rebase` and its branch-protection
    gates in the detail field while ``mergeable`` stays ``unknown`` — dropping the
    detail because its sibling is unknown would leave exactly those banners
    invisible to the poll.
    """
    if projection._merge_state_real(mergeable):
        result["mergeable"] = mergeable
    if projection._merge_state_real(merge_state):
        result["mergeStateStatus"] = merge_state


_MERGE_STATE_FIELDS = ("mergeable", "mergeStateStatus")
# Lifecycle states for which a merge answer is still meaningful. Once a source is
# merged or closed the providers stop answering the merge pair at all, so a
# carried-forward value could never be cleared again.
_MERGE_STATE_LIVE_STATES = frozenset({"open", "draft"})


def _keep_known_merge_state(
    status: dict[str, str], previous: dict[str, str] | None
) -> dict[str, str]:
    """Carry a settled merge field forward when a fresh read has no answer yet.

    ``_record_merge_state`` omits a field the provider has not settled, on the
    principle that "still computing" must never be published as a real answer.
    That is necessary but not sufficient: every writer replaces the chip entry
    WHOLESALE, so an omitted field does not read as "no news" downstream — it
    erases whatever the previous entry had settled.

    That matters because an unsettled read is the COMMON case, not a rare one:
    both providers compute mergeability lazily, so a poll that arrives after the
    provider's evaluation lapsed returns ``unknown`` for a source whose conflict
    is already known. Without this carry-forward, such a poll drops the merge
    pair, which (a) removes it from the owner-gated sidebar payload that spreads
    the entry whole, and (b) reads as a CHANGED chip status, dropping the full
    payload and emitting a delta — whose refetch re-projects the real answer
    straight back into the cache. That is the repeating chip<->full transition
    ``_CHECK_FLAP_DAMP_THRESHOLD`` exists to contain, so the banner would survive
    only until the damper tripped and then go stale.

    Mirrors the same keep-known rule already applied to the ``ci`` glyph. A real
    answer always wins, including one that CHANGES the value, so this only ever
    fills a gap and cannot pin a stale verdict. Carry-forward stops once the
    source leaves an open state, where the pair is both meaningless and
    permanently unanswered.
    """
    if not previous:
        return status
    if status.get("state", "open") not in _MERGE_STATE_LIVE_STATES:
        return status
    carried = {
        field: previous[field]
        for field in _MERGE_STATE_FIELDS
        if field not in status and field in previous
    }
    return {**status, **carried} if carried else status


def status_from_full_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    """Derive the lightweight chip status from a FULL pull-request payload.

    The sidebar chips and the detail panel must not read two independent caches
    with different TTLs, or they could each be "fresh" and still disagree. The
    full fetch is strictly richer than the chip fetch, so it write-throughs into
    the chip cache via this projection — one provider read, one truth, both
    surfaces. Shares the SAME projection helpers as ``_fetch_check_status``:
    ``_project_state`` for lifecycle, and — for CI — GitLab's authoritative
    ``ciStatus`` aggregate (stamped by ``_fetch_gitlab`` via
    ``_gitlab_aggregate_ci``, the same value the chip path reads) or, for GitHub,
    ``_rollup_ci`` over the ``statusCheckRollup`` buckets. Because both paths
    resolve the CI glyph from the identical aggregate, they cannot drift.
    """
    if not isinstance(payload, dict):
        return None
    result: dict[str, str] = {}
    # GitLab stamps an authoritative aggregate CI (`ciStatus`) — the same value
    # the chip path reads straight from the pipeline aggregate — so prefer it and
    # never roll up GitLab's faithful per-job buckets (which would count an
    # allow_failure red job, or miss a truncated one, and diverge from the chip).
    # GitHub has no separate aggregate: its `statusCheckRollup` buckets ARE the
    # aggregate, so fall back to rolling them up.
    ci_status = payload.get("ciStatus")
    if isinstance(ci_status, str) and ci_status:
        result["ci"] = ci_status
    else:
        buckets = [
            str(check.get("bucket") or "") for check in projection._as_list(payload.get("checks"))
        ]
        ci = projection._rollup_ci(buckets)
        if ci is not None:
            result["ci"] = ci
    state = projection._project_state(
        str(payload.get("state") or ""), draft=bool(payload.get("draft"))
    )
    if state is not None:
        result["state"] = state
    # The merge pair must be projected here too, not just by the chip read. If the
    # write-through omitted it, every full fetch would rewrite the chip entry
    # WITHOUT the fields the chip read had recorded, so the next chip refresh
    # would see a "change" and drop the full payload, which would write-through
    # and strip them again — the exact repeating chip↔full transition the flap
    # damper below exists to contain, spun by nothing but a projection gap.
    _record_merge_state(
        result,
        str(payload.get("mergeable") or ""),
        str(payload.get("mergeStateStatus") or ""),
    )
    return result or None


def record_full_payload_status(url: str, payload: dict[str, Any]) -> None:
    """Publish a full fetch's lifecycle/CI projection into the chip cache.

    Keeps the sidebar chips in lockstep with whatever the detail panel just
    rendered, and emits a delta so other owner windows converge too. Never
    invalidates the full cache — the caller just stored it.
    """
    status = status_from_full_payload(payload)
    if status is None:
        return
    previous = _check_cache.get(url)
    # A degraded full payload (a provider's secondary pipelines/jobs call failed,
    # so ``checks`` came back empty and is flagged in ``partialSections``) omits
    # the ``ci`` projection. Mirror ``_refresh_check_status``'s keep-known-status
    # rule: never let a transient partial fetch erase a CI glyph the chip cache
    # already knows, or the write-through would recreate the very chip/panel
    # divergence this projection exists to prevent. Only carry the field over
    # when ``checks`` is explicitly partial — a genuinely empty checks section
    # (no CI configured) must still be allowed to clear a stale glyph.
    if (
        "ci" not in status
        and previous
        and previous[1]
        and "ci" in previous[1]
        and "checks" in (payload.get("partialSections") or [])
    ):
        status = {**status, "ci": previous[1]["ci"]}
    # Never let a lazily-unsettled merge read erase a settled one. A first full
    # fetch commonly returns `unknown` (that is the bug the provider merge-state
    # re-reads address), so without this the write-through would strip a conflict
    # the chip cache already knew.
    status = _keep_known_merge_state(status, previous[1] if previous else None)
    _check_cache[url] = (time.monotonic(), status)
    _trim_check_cache()
    if previous is None or previous[1] != status:
        # A full-payload write is an independent, authoritative status change —
        # NOT another instance of the chip re-projecting the same value. Reset
        # this URL's flap tracker so the chip refresh's consecutive-transition
        # counter does not mistake "chip A→B, full B→C, chip C→B ..." for a
        # single repeating A→B loop and falsely damp legitimate CI churn (e.g.
        # three real re-runs of the same job).
        _clear_check_flap(url)
        # Lockstep visibility revalidation: the full-payload writer
        # is a SECOND authoritative status writer alongside _refresh_check_status.
        # A public->private change whose owner detail fetch refreshes status here
        # would otherwise be served to a non-owner against a still-cached-public
        # visibility flag. force=True bypasses the visibility TTL and synchronously
        # fails a cached-public entry closed for the in-flight window (restoring
        # public only on positive reconfirmation), closing the same window the
        # chip-refresh path already guards. Bounded to real status transitions.
        with contextlib.suppress(Exception):
            schedule_visibility_refresh([url], force=True)
        _emit_status_delta(url, status, "detail")


def _flush_check_updates() -> None:
    """Coalesce completed refreshes into one slots broadcast per event-loop tick."""
    global _check_update_handle
    callbacks = tuple(_check_update_callbacks)
    _check_update_callbacks.clear()
    _check_update_handle = None
    for callback in callbacks:
        with contextlib.suppress(Exception):
            callback()


def _queue_check_update(callback: _CheckUpdateCallback) -> None:
    global _check_update_handle
    _check_update_callbacks.add(callback)
    if _check_update_handle is None:
        _check_update_handle = asyncio.get_running_loop().call_later(
            _CHECK_UPDATE_DEBOUNCE_SECS, _flush_check_updates
        )
