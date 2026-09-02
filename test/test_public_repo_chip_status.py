"""Public-repo chip-status gate: a non-owner dashboard user sees PR/MR status
for PUBLIC repos, while private/unknown repos stay owner-only and app tokens
see nothing (issue #6786).

Covers the three cooperating pieces:
  * ``source_providers`` repo-visibility cache + fetch + scheduler,
  * ``state._project_source_links`` per-link gate matrix,
  * the ``serialize_slots(dashboard_user=...)`` plumbing.
"""

import asyncio
import contextlib
import time

import pytest

import kiro_crew.dashboard.handlers.source_providers as source
from kiro_crew.dashboard.state import _project_source_links

PR_URL = "https://github.com/acme/repo/pull/7"
PR_URL_2 = "https://github.com/other/proj/pull/3"
ISSUE_URL = "https://github.com/acme/repo/issues/9"
GLAB_URL = "https://gitlab.com/grp/sub/-/merge_requests/4"


@pytest.fixture(autouse=True)
def _clear_caches():
    source._visibility_cache.clear()
    source._visibility_inflight.clear()
    source._visibility_force_gen.clear()
    source._check_cache.clear()
    yield
    source._visibility_cache.clear()
    source._visibility_inflight.clear()
    source._visibility_force_gen.clear()
    source._check_cache.clear()
    # A test that armed the debounced update (force visibility refresh /
    # status-change path) can leave a pending global TimerHandle bound to this
    # test's now-closing event loop; if it survives, a later test's callback
    # cannot arm (the handle looks live) and the stale callback set leaks across
    # tests. Cancel + reset it and clear the callback set after each test
    # (no-test-side-effects).
    handle = getattr(source, "_check_update_handle", None)
    if handle is not None:
        with contextlib.suppress(Exception):
            handle.cancel()
    source._check_update_handle = None
    source._check_update_callbacks.clear()
    # Reset the public-status-grant SEL dedup so one test's grant does not
    # suppress another's audit assertion.
    import kiro_crew.dashboard.state as _state_mod

    _state_mod._PUBLIC_STATUS_GRANT_AUDIT.clear()
    _state_mod._PUBLIC_STATUS_DENY_AUDIT.clear()


def _seed_visibility(url: str, public: bool | None) -> None:
    ref = source.parse_source_url(url)
    source._visibility_cache[source._visibility_key(ref)] = (time.monotonic(), public)


def _seed_status(url: str, status: dict) -> None:
    source._check_cache[url] = (time.monotonic(), status)


# ── is_repo_public reader ────────────────────────────────────────────────────


def test_is_repo_public_unknown_until_fetched():
    assert source.is_repo_public(PR_URL) is None


def test_is_repo_public_reads_cached_public_and_private():
    _seed_visibility(PR_URL, True)
    _seed_visibility(PR_URL_2, False)
    assert source.is_repo_public(PR_URL) is True
    assert source.is_repo_public(PR_URL_2) is False


def test_is_repo_public_none_for_unparseable_and_jira():
    assert source.is_repo_public("not a url") is None
    # Jira has no public-repo concept; visibility is never meaningful for it.
    assert source.is_repo_public("https://org.atlassian.net/browse/PROJ-1") is None


# ── _fetch_repo_visibility ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_github_visibility_maps_isprivate(monkeypatch):
    async def fake_run(*argv, **kw):
        assert argv[:3] == ("gh", "repo", "view")
        return {"isPrivate": False, "visibility": "public"}

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url(PR_URL)
    assert await source._fetch_repo_visibility(ref) is True


@pytest.mark.asyncio
async def test_fetch_github_internal_is_not_public(monkeypatch):
    # GPT #6789 round-9: isPrivate is False for internal (Enterprise) repos too,
    # but internal is NOT anonymously readable — must not classify as public.
    async def fake_run(*argv, **kw):
        return {"isPrivate": False, "visibility": "internal"}

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url(PR_URL)
    assert await source._fetch_repo_visibility(ref) is False


@pytest.mark.asyncio
async def test_fetch_github_private_is_false(monkeypatch):
    async def fake_run(*argv, **kw):
        return {"isPrivate": True, "visibility": "private"}

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url(PR_URL)
    assert await source._fetch_repo_visibility(ref) is False


@pytest.mark.asyncio
async def test_fetch_gitlab_only_public_is_public(monkeypatch):
    seen = {}

    async def fake_run(*argv, **kw):
        seen["host"] = kw.get("host")
        return {"visibility": "internal"}  # NOT anonymous-public

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url(GLAB_URL)
    assert await source._fetch_repo_visibility(ref) is False
    assert seen["host"] == "gitlab.com"


@pytest.mark.asyncio
async def test_fetch_gitlab_public_with_public_features_is_public(monkeypatch):
    async def fake_run(*argv, **kw):
        return {
            "visibility": "public",
            "merge_requests_access_level": "enabled",
            "builds_access_level": "enabled",
            "public_jobs": True,
        }

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url(GLAB_URL)
    assert await source._fetch_repo_visibility(ref) is True


@pytest.mark.asyncio
async def test_fetch_gitlab_public_but_private_pipelines_is_not_public(monkeypatch):
    # GPT #6789 round-6: public_jobs=false hides pipeline/CI status from
    # non-members even when builds_access_level is "enabled".
    async def fake_run(*argv, **kw):
        return {
            "visibility": "public",
            "merge_requests_access_level": "enabled",
            "builds_access_level": "enabled",
            "public_jobs": False,  # private pipelines
        }

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url(GLAB_URL)
    assert await source._fetch_repo_visibility(ref) is False


@pytest.mark.asyncio
async def test_fetch_gitlab_public_but_member_only_mr_is_not_public(monkeypatch):
    # GPT #6789 round-5: a public project can restrict MR/CI to members, so
    # visibility=="public" alone must NOT authorize non-owner status.
    async def fake_run(*argv, **kw):
        return {
            "visibility": "public",
            "merge_requests_access_level": "private",  # members only
            "builds_access_level": "enabled",
        }

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url(GLAB_URL)
    assert await source._fetch_repo_visibility(ref) is False


@pytest.mark.asyncio
async def test_fetch_gitlab_public_missing_feature_levels_fails_closed(monkeypatch):
    async def fake_run(*argv, **kw):
        return {"visibility": "public"}  # no feature-access-level keys

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url(GLAB_URL)
    assert await source._fetch_repo_visibility(ref) is False


@pytest.mark.asyncio
async def test_fetch_visibility_fails_closed_on_error(monkeypatch):
    async def boom(*argv, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(source, "_run_json", boom)
    ref = source.parse_source_url(PR_URL)
    assert await source._fetch_repo_visibility(ref) is None


@pytest.mark.asyncio
async def test_refresh_keeps_prior_known_value_on_failure(monkeypatch):
    _seed_visibility(PR_URL, True)
    ref = source.parse_source_url(PR_URL)

    async def boom(*argv, **kw):
        raise RuntimeError("transient")

    monkeypatch.setattr(source, "_run_json", boom)
    source._visibility_inflight.add(source._visibility_key(ref))
    await source._refresh_repo_visibility(ref)
    # A failed read must not erase a previously-known public flag (within TTL).
    assert source.is_repo_public(PR_URL) is True


def test_stale_public_entry_reads_as_unknown(monkeypatch):
    # GPT-blocked hole: a repo cached public must NOT authorize status forever.
    # An entry older than the TTL reads as None (fail closed) even if its stored
    # value is True.
    ref = source.parse_source_url(PR_URL)
    stale_at = time.monotonic() - source._VISIBILITY_TTL_SECS - 1
    source._visibility_cache[source._visibility_key(ref)] = (stale_at, True)
    assert source.is_repo_public(PR_URL) is None


@pytest.mark.asyncio
async def test_failed_refresh_does_not_extend_stale_public(monkeypatch):
    # public -> private transition where the visibility read keeps failing: the
    # failed refresh must NOT reset the timestamp, so the stale public ages out
    # from its last SUCCESSFUL read and is_repo_public fails closed within one TTL.
    ref = source.parse_source_url(PR_URL)
    key = source._visibility_key(ref)
    original_at = time.monotonic() - source._VISIBILITY_TTL_SECS + 5  # nearly stale
    source._visibility_cache[key] = (original_at, True)

    async def boom(*argv, **kw):
        raise RuntimeError("visibility read fails while repo went private")

    monkeypatch.setattr(source, "_run_json", boom)
    source._visibility_inflight.add(key)
    await source._refresh_repo_visibility(ref)
    # Value kept but timestamp NOT reset — so the entry is still anchored to the
    # original (nearly-stale) fetch time, not refreshed to now.
    assert source._visibility_cache[key][0] == original_at
    # And once that original time crosses the TTL, it reads unknown (fail closed).
    source._visibility_cache[key] = (
        time.monotonic() - source._VISIBILITY_TTL_SECS - 1,
        True,
    )
    assert source.is_repo_public(PR_URL) is None


@pytest.mark.asyncio
async def test_cold_failed_refresh_records_unknown(monkeypatch):
    ref = source.parse_source_url(PR_URL)
    key = source._visibility_key(ref)

    async def boom(*argv, **kw):
        raise RuntimeError("cold miss, provider down")

    monkeypatch.setattr(source, "_run_json", boom)
    source._visibility_inflight.add(key)
    await source._refresh_repo_visibility(ref)
    # A never-known repo whose first read failed records an unknown (None), so
    # the scheduler does not respawn a fetch on every slots push.
    assert key in source._visibility_cache
    assert source._visibility_cache[key][1] is None
    assert source.is_repo_public(PR_URL) is None


# ── schedule_visibility_refresh ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_dedups_by_repo_and_skips_issue_and_jira(monkeypatch):
    calls: list[str] = []

    async def fake_refresh(ref, on_update=None, *, prev_public_override=None):
        calls.append(source._visibility_key(ref))
        source._visibility_inflight.discard(source._visibility_key(ref))

    monkeypatch.setattr(source, "_refresh_repo_visibility", fake_refresh)
    # Two PRs on the SAME repo + an issue on it + a jira: one repo key scheduled.
    source.schedule_visibility_refresh(
        [PR_URL, ISSUE_URL, "https://github.com/acme/repo/pull/99", "not-a-url"]
    )
    # Let the created tasks run.
    await asyncio.gather(*list(source._VISIBILITY_TASKS), return_exceptions=True)
    assert calls == [source._visibility_key(source.parse_source_url(PR_URL))]


@pytest.mark.asyncio
async def test_schedule_respects_fresh_ttl(monkeypatch):
    _seed_visibility(PR_URL, True)  # fresh entry
    called = False

    async def fake_refresh(ref, on_update=None, *, prev_public_override=None):
        nonlocal called
        called = True
        source._visibility_inflight.discard(source._visibility_key(ref))

    monkeypatch.setattr(source, "_refresh_repo_visibility", fake_refresh)
    source.schedule_visibility_refresh([PR_URL])
    await asyncio.gather(*list(source._VISIBILITY_TASKS), return_exceptions=True)
    assert called is False


@pytest.mark.asyncio
async def test_force_synchronously_invalidates_public_before_refresh(monkeypatch):
    # GPT #6789 round-4 race: a forced refresh runs concurrently with the forced
    # status read; if status finishes first it must NOT see a still-cached-public
    # visibility. force=True drops a public entry to unknown SYNCHRONOUSLY (before
    # the task is spawned), so is_repo_public fails closed for the in-flight window.
    _seed_visibility(PR_URL, True)
    assert source.is_repo_public(PR_URL) is True

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_refresh(ref, on_update=None, *, prev_public_override=None):
        started.set()
        await release.wait()  # hold the refresh open to model the in-flight window
        source._visibility_inflight.discard(source._visibility_key(ref))

    monkeypatch.setattr(source, "_refresh_repo_visibility", slow_refresh)
    source.schedule_visibility_refresh([PR_URL], force=True)
    # BEFORE the refresh completes, the public flag is already gone (fail closed).
    assert source.is_repo_public(PR_URL) is None
    await started.wait()
    assert source.is_repo_public(PR_URL) is None
    release.set()
    await asyncio.gather(*list(source._VISIBILITY_TASKS), return_exceptions=True)


@pytest.mark.asyncio
async def test_force_invalidates_public_even_when_refresh_already_inflight(monkeypatch):
    # GPT #6789 round-14: the force path must fail a cached-public entry closed
    # BEFORE the inflight-dedup return — otherwise a force=True call that arrives
    # while a (pre-flip) refresh is already running would `continue` without
    # invalidating, and the in-flight positive read could restore public after a
    # public->private flip.
    source._visibility_cache.clear()
    source._visibility_inflight.clear()
    source._visibility_force_gen.clear()
    key = source._visibility_key(source.parse_source_url(PR_URL))
    _seed_visibility(PR_URL, True)
    # Model a refresh already in flight for this key (spawned before the flip).
    source._visibility_inflight.add(key)

    # A forced refresh now arrives (status just detected the repo went private).
    # It must synchronously invalidate the cached-public entry despite the
    # in-flight dedup, so is_repo_public fails closed immediately.
    source.schedule_visibility_refresh([PR_URL], force=True)
    assert source.is_repo_public(PR_URL) is None
    # The force generation bumped, so a stale in-flight positive read is refused.
    assert source._visibility_force_gen.get(key, 0) >= 1
    source._visibility_inflight.discard(key)


@pytest.mark.asyncio
async def test_stale_inflight_positive_cannot_restore_public_across_force(monkeypatch):
    # GPT #6789 round-14: a visibility refresh whose positive read predates a
    # force-invalidation (public->private flip) must NOT write ``public`` back —
    # doing so would re-open the leak the force path just closed. The generation
    # guard makes it record unknown instead, so is_repo_public stays None.
    source._visibility_cache.clear()
    source._visibility_inflight.clear()
    source._visibility_force_gen.clear()
    key = source._visibility_key(source.parse_source_url(PR_URL))

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_public_fetch(ref):
        entered.set()
        await release.wait()  # hold open so a force-invalidation can land mid-flight
        return True  # positive read — but it predates the flip below

    monkeypatch.setattr(source, "_fetch_repo_visibility", slow_public_fetch)

    # Start a refresh that will positively read "public" (pre-flip).
    source._visibility_inflight.add(key)
    ref = source.parse_source_url(PR_URL)
    task = asyncio.get_running_loop().create_task(source._refresh_repo_visibility(ref))
    await entered.wait()
    # Mid-flight, the repo flips private: a force-invalidation bumps the gen.
    _seed_visibility(PR_URL, True)  # simulate the cached-public the force sees
    source.schedule_visibility_refresh([PR_URL], force=True)
    # Let the stale positive read complete.
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.gather(*list(source._VISIBILITY_TASKS), return_exceptions=True)
    # The stale positive did NOT restore public — is_repo_public stays fail-closed.
    assert source.is_repo_public(PR_URL) is None


@pytest.mark.asyncio
async def test_force_bumps_generation_even_when_entry_already_unknown(monkeypatch):
    # GPT #6789 round-15: the generation bump must fire on EVERY forced refresh
    # (before inflight dedup), not only when the cached entry is currently
    # public. Otherwise a second force arriving while the entry is already
    # unknown (first force landed, pre-privacy fetch still in flight) would skip
    # the bump and let that stale in-flight positive read restore public.
    source._visibility_cache.clear()
    source._visibility_inflight.clear()
    source._visibility_force_gen.clear()
    key = source._visibility_key(source.parse_source_url(PR_URL))
    # Entry is already UNKNOWN (not public) and a refresh is in flight.
    source._visibility_cache[key] = (0.0, None)
    source._visibility_inflight.add(key)
    gen_before = source._visibility_force_gen.get(key, 0)

    source.schedule_visibility_refresh([PR_URL], force=True)

    assert source._visibility_force_gen.get(key, 0) == gen_before + 1
    source._visibility_inflight.discard(key)


@pytest.mark.asyncio
async def test_force_public_to_private_transition_queues_hide_update(monkeypatch):
    # GPT #6789 round-7: a forced refresh pre-invalidates a cached-public entry
    # to unknown before spawning the refresh. If the repo genuinely went private,
    # the refresh must STILL queue an on_update so connected non-owners hide the
    # now-stale public chip — the comparison must measure the flip against the
    # TRUE prior rendered value (public), not the pre-invalidated unknown.
    source._check_update_callbacks.clear()
    _seed_visibility(PR_URL, True)  # was rendered public

    async def fetch_private(ref):
        return False  # repo is now private

    monkeypatch.setattr(source, "_fetch_repo_visibility", fetch_private)

    fired = {"n": 0}

    def on_update():
        fired["n"] += 1

    source.schedule_visibility_refresh([PR_URL], on_update=on_update, force=True)
    await asyncio.gather(*list(source._VISIBILITY_TASKS), return_exceptions=True)
    # The hide-the-chip update was queued (public -> private is a rendered flip).
    assert on_update in source._check_update_callbacks or fired["n"] >= 1
    assert source.is_repo_public(PR_URL) is False


@pytest.mark.asyncio
async def test_status_change_forces_visibility_revalidation(monkeypatch):
    # GPT #6789 round-8: a status refresh can land a freshly-fetched status while
    # this URL's visibility entry is still within its TTL, so a plain (non-force)
    # visibility schedule would SKIP the read and is_repo_public would authorize
    # the new status against a stale-fresh public flag. A confirmed status change
    # must force-revalidate visibility for that URL in lockstep.
    source._check_cache.clear()
    _seed_status(PR_URL, {"state": "open"})  # prior status

    async def fetch_changed(url):
        return {"state": "merged"}  # status changed

    monkeypatch.setattr(source, "_fetch_check_status", fetch_changed)

    forced: list[tuple[list[str], bool]] = []
    real_schedule = source.schedule_visibility_refresh

    def spy_schedule(urls, on_update=None, *, force=False):
        forced.append((list(urls), force))
        # Do not spawn real tasks in this unit test.

    monkeypatch.setattr(source, "schedule_visibility_refresh", spy_schedule)
    monkeypatch.setattr(source, "_emit_status_delta", lambda *a, **k: None)

    source._check_inflight.add(PR_URL)  # _refresh_check_status discards it
    await source._refresh_check_status(PR_URL, on_update=None)

    # Visibility was force-revalidated for exactly this URL in lockstep.
    assert ([PR_URL], True) in forced
    assert real_schedule is not None


def test_full_payload_status_change_forces_visibility_revalidation(monkeypatch):
    # GPT #6789 round-9: record_full_payload_status is a SECOND authoritative
    # status writer (the detail panel). A public->private change refreshed here
    # must also force-revalidate visibility, or a non-owner is served the new
    # status against a stale-fresh public flag. Fix the writer, do NOT drop the
    # non-owner feature.
    source._check_cache.clear()
    _seed_status(PR_URL, {"state": "open"})  # prior status

    monkeypatch.setattr(source, "status_from_full_payload", lambda payload: {"state": "merged"})
    monkeypatch.setattr(source, "_emit_status_delta", lambda *a, **k: None)

    forced: list[tuple[list[str], bool]] = []

    def spy_schedule(urls, on_update=None, *, force=False):
        forced.append((list(urls), force))

    monkeypatch.setattr(source, "schedule_visibility_refresh", spy_schedule)

    source.record_full_payload_status(PR_URL, {"state": "merged"})

    assert ([PR_URL], True) in forced


@pytest.mark.asyncio
async def test_status_change_forces_visibility_before_flap_and_await(monkeypatch):
    # GPT #6789 round-11 finding 2: the force visibility invalidation must run
    # IMMEDIATELY after 'changed' is detected — before flap handling (which can
    # early-return) and before the first await (_invalidate_full_payload_cache).
    # Otherwise a public->private repo whose transition is flapping would return
    # without ever invalidating visibility, or a concurrent slots push during the
    # await could observe the newly-cached private status against a stale-fresh
    # public flag.
    source._check_cache.clear()
    _seed_status(PR_URL, {"state": "open"})

    async def fetch_changed(url):
        return {"state": "merged"}

    monkeypatch.setattr(source, "_fetch_check_status", fetch_changed)

    order: list[str] = []

    def spy_schedule(urls, on_update=None, *, force=False):
        order.append(f"visibility_force={force}")

    async def spy_invalidate_full(url):
        order.append("full_payload_await")

    monkeypatch.setattr(source, "schedule_visibility_refresh", spy_schedule)
    monkeypatch.setattr(source, "_invalidate_full_payload_cache", spy_invalidate_full)
    monkeypatch.setattr(source, "_emit_status_delta", lambda *a, **k: None)

    # Case A: force the flap path (early return). Visibility must STILL have been
    # invalidated first.
    monkeypatch.setattr(source, "_note_check_flap", lambda *a, **k: True)
    source._check_inflight.add(PR_URL)
    await source._refresh_check_status(PR_URL, on_update=None)
    assert order and order[0] == "visibility_force=True"
    assert "full_payload_await" not in order  # flap path returned before the await

    # Case B: normal path — visibility force precedes the full-payload await.
    order.clear()
    source._check_cache.clear()
    _seed_status(PR_URL, {"state": "open"})
    monkeypatch.setattr(source, "_note_check_flap", lambda *a, **k: False)
    source._check_inflight.add(PR_URL)
    await source._refresh_check_status(PR_URL, on_update=None)
    assert order == ["visibility_force=True", "full_payload_await"]


# ── _project_source_links gate matrix ────────────────────────────────────────


def _link(url: str, kind: str = "change") -> dict:
    return {"provider": "github", "number": 7, "url": url, "kind": kind}


def _has_status(projected: dict) -> bool:
    return "state" in projected or "ci" in projected


def test_owner_sees_status_for_private_repo():
    _seed_status(PR_URL, {"state": "merged", "ci": "passed"})
    _seed_visibility(PR_URL, False)  # private
    out = _project_source_links([_link(PR_URL)], True, dashboard_user=False)
    assert _has_status(out[0])  # owner gate wins regardless of visibility


def test_dashboard_user_sees_status_for_public_repo():
    _seed_status(PR_URL, {"state": "merged", "ci": "passed"})
    _seed_visibility(PR_URL, True)
    out = _project_source_links([_link(PR_URL)], False, dashboard_user=True)
    assert out[0].get("state") == "merged"
    assert out[0].get("ci") == "passed"


def test_dashboard_user_gets_bare_chip_for_private_repo():
    _seed_status(PR_URL, {"state": "merged", "ci": "passed"})
    _seed_visibility(PR_URL, False)
    out = _project_source_links([_link(PR_URL)], False, dashboard_user=True)
    assert not _has_status(out[0])


def test_dashboard_user_fails_closed_when_visibility_unknown():
    _seed_status(PR_URL, {"state": "merged", "ci": "passed"})
    # No visibility seeded -> unknown -> owner-only.
    out = _project_source_links([_link(PR_URL)], False, dashboard_user=True)
    assert not _has_status(out[0])


def test_app_token_gets_bare_chip_even_on_public_repo():
    _seed_status(PR_URL, {"state": "merged", "ci": "passed"})
    _seed_visibility(PR_URL, True)
    # Neither owner nor dashboard-user -> app token -> no status.
    out = _project_source_links([_link(PR_URL)], False, dashboard_user=False)
    assert not _has_status(out[0])


def test_issue_link_never_gets_status_even_for_owner():
    _seed_status(ISSUE_URL, {"state": "closed"})
    _seed_visibility(ISSUE_URL, True)
    out = _project_source_links([_link(ISSUE_URL, kind="issue")], True, dashboard_user=True)
    assert not _has_status(out[0])


def test_non_owner_public_status_grant_is_sel_audited(monkeypatch):
    # GPT #6789 round-12: granting a non-owner status on a confirmed-public repo
    # is an access-control decision and MUST leave an SEL allow event
    # (AUTOSDE backend-security-controls: grants, not only denials). Owner and
    # app-token paths do NOT go through this grant, so they emit nothing here.
    import kiro_crew.dashboard.state as state_mod

    state_mod._PUBLIC_STATUS_GRANT_AUDIT.clear()
    state_mod._PUBLIC_STATUS_DENY_AUDIT.clear()
    events: list[tuple] = []

    class _FakeSel:
        def log_api_access(self, **kw):
            events.append((kw.get("operation"), kw.get("outcome"), kw.get("resources")))

    monkeypatch.setattr(state_mod, "sel", lambda: _FakeSel())

    _seed_status(PR_URL, {"state": "merged", "ci": "passed"})
    _seed_visibility(PR_URL, True)

    # Non-owner + public -> grant -> one allow event.
    _project_source_links([_link(PR_URL)], False, dashboard_user=True)
    assert events == [("source_link_public_status", "allowed", PR_URL)]

    # Deduplicated within the window: a second projection emits nothing new.
    _project_source_links([_link(PR_URL)], False, dashboard_user=True)
    assert len(events) == 1

    # Owner path does not go through the non-owner grant -> no new event.
    events.clear()
    _seed_visibility(PR_URL_2, False)
    _seed_status(PR_URL_2, {"state": "open"})
    _project_source_links([_link(PR_URL_2)], True, dashboard_user=False)
    assert events == []

    # DENY decision (GPT #6789 round-15): a non-owner on a NON-public repo is
    # denied status and that denial must ALSO be SEL-audited (deduped per url).
    events.clear()
    _seed_status(PR_URL_2, {"state": "open", "ci": "passed"})
    _seed_visibility(PR_URL_2, False)  # private -> denied for a non-owner
    _project_source_links([_link(PR_URL_2)], False, dashboard_user=True)
    assert events == [("source_link_public_status", "denied", PR_URL_2)]
    # Deduped within the window.
    _project_source_links([_link(PR_URL_2)], False, dashboard_user=True)
    assert len(events) == 1
