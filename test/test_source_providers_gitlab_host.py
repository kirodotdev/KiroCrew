"""GitLab reads and the host they address: checks and chip status from pipelines
and jobs, the self-hosted host allowlist, and the host and environment every ``glab``
call carries."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401

from kiro_crew import github_runner
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.testing.wait import default_timeout


@pytest.mark.asyncio
async def test_fetch_gitlab_checks_uses_at_most_two_calls(monkeypatch) -> None:
    run = AsyncMock(
        side_effect=[
            [{"id": 91, "status": "running", "web_url": "https://gitlab.com/p/91"}],
            [
                {
                    "name": "test",
                    "stage": "verify",
                    "status": "running",
                    "web_url": "https://gitlab.com/j/7",
                }
            ],
        ]
    )
    monkeypatch.setattr(source, "_run_json", run)

    checks = await source.fetch_pull_request_checks(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42"
    )

    assert run.await_count == 2
    assert (
        "projects/acme%2Fplatform%2Fservice/merge_requests/42/pipelines?per_page=1"
        in run.await_args_list[0].args
    )
    assert (
        "projects/acme%2Fplatform%2Fservice/pipelines/91/jobs?per_page=100"
        in run.await_args_list[1].args
    )
    # host must be forwarded on every glab call or _run_json's guard refuses it.
    assert [call.kwargs.get("host") for call in run.await_args_list] == ["gitlab.com"] * 2
    assert checks[0]["name"] == "test"
    assert checks[0]["bucket"] == "pending"


@pytest.mark.asyncio
async def test_fetch_gitlab_checks_falls_back_to_pipeline_without_jobs(monkeypatch) -> None:
    run = AsyncMock(
        side_effect=[
            [{"id": 91, "status": "success", "web_url": "https://gitlab.com/p/91"}],
            [],
        ]
    )
    monkeypatch.setattr(source, "_run_json", run)

    checks = await source.fetch_pull_request_checks(
        "https://gitlab.com/acme/repo/-/merge_requests/42"
    )

    assert run.await_count == 2
    assert [call.kwargs.get("host") for call in run.await_args_list] == ["gitlab.com"] * 2
    assert checks[0]["name"] == "Pipeline"
    assert checks[0]["bucket"] == "passed"


@pytest.mark.asyncio
async def test_gitlab_chip_status_uses_head_pipeline_without_second_call(monkeypatch) -> None:
    run = AsyncMock(
        return_value={
            "state": "opened",
            "draft": False,
            "head_pipeline": {"status": "running"},
        }
    )
    monkeypatch.setattr(source, "_run_json", run)

    status = await source._fetch_check_status(
        "https://gitlab.com/acme/platform/service/-/merge_requests/42"
    )

    assert run.await_count == 1
    assert run.await_args_list[0].kwargs.get("host") == "gitlab.com"
    assert status == {"state": "open", "ci": "running"}


@pytest.mark.asyncio
async def test_gitlab_chip_status_falls_back_to_pipelines_list(monkeypatch) -> None:
    run = AsyncMock(
        side_effect=[
            {"state": "opened", "draft": True},
            [{"status": "failed"}],
        ]
    )
    monkeypatch.setattr(source, "_run_json", run)

    status = await source._fetch_check_status("https://gitlab.com/acme/repo/-/merge_requests/42")

    assert run.await_count == 2
    assert "merge_requests/42/pipelines?per_page=1" in run.await_args_list[1].args[2]
    assert [call.kwargs.get("host") for call in run.await_args_list] == ["gitlab.com"] * 2
    assert status == {"state": "draft", "ci": "failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "draft", "expected"),
    [
        ("opened", True, "draft"),
        ("opened", False, "open"),
        # Draft must not outrank a terminal state: GitLab keeps `draft` set on a
        # merge request closed while still a draft.
        ("closed", True, "closed"),
        ("merged", False, "merged"),
        ("locked", False, None),
    ],
)
async def test_gitlab_chip_state_precedence(monkeypatch, state, draft, expected) -> None:
    run = AsyncMock(return_value={"state": state, "draft": draft, "head_pipeline": {}})
    monkeypatch.setattr(source, "_run_json", run)

    status = await source._fetch_check_status("https://gitlab.com/acme/repo/-/merge_requests/42")

    assert (status or {}).get("state") == expected


@pytest.mark.asyncio
async def test_gitlab_allowlist_never_reads_config_on_the_event_loop(monkeypatch) -> None:
    """KiroCrewConfig.load() stats/reads/parses config files, so it must only run
    in a worker thread; the sync accessor every URL parse uses is cache-only."""
    calls: list[str] = []

    def fake_load() -> (
        tuple[frozenset[str], frozenset[str], bool, frozenset[str], frozenset[str], frozenset[str]]
    ):
        calls.append("load")
        return (
            frozenset({"gitlab.acme.internal"}),
            frozenset(),
            True,
            frozenset(),
            frozenset(),
            frozenset(),
        )

    monkeypatch.setattr(source, "_load_source_link_settings", fake_load)
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 0.0)
    to_thread_calls: list[object] = []
    real_to_thread = source.asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        to_thread_calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(source.asyncio, "to_thread", spy_to_thread)

    # Cold cache fails closed rather than blocking to load.
    assert source._allowed_gitlab_hosts() == frozenset()
    assert calls == []

    assert await source.ensure_gitlab_hosts_loaded() == frozenset({"gitlab.acme.internal"})
    assert to_thread_calls == [fake_load]
    assert source._allowed_gitlab_hosts() == frozenset({"gitlab.acme.internal"})

    # Within the TTL a second call is a pure cache read - no further thread hop.
    assert await source.ensure_gitlab_hosts_loaded() == frozenset({"gitlab.acme.internal"})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_async_entry_points_refresh_the_allowlist_before_parsing(monkeypatch) -> None:
    order: list[str] = []

    async def fake_ensure() -> frozenset[str]:
        order.append("ensure")
        return frozenset()

    def fake_parse(url: str):
        order.append("parse")
        raise ValueError("stop here")

    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", fake_ensure)
    monkeypatch.setattr(source, "parse_source_url", fake_parse)

    for coro in (
        source.fetch_pull_request("https://x/y"),
        source.fetch_pull_request_checks("https://x/y"),
        source.resolve_pull_request_thread("https://x/y", "abc"),
        source._fetch_check_status("https://x/y"),
        # Mutation entry points: a cold snapshot here would reject an authorized
        # self-managed URL as an unsupported host (400) before any dispatch.
        source.enable_pull_request_auto_merge("https://x/y"),
        source.mark_pull_request_ready("https://x/y"),
    ):
        with pytest.raises(ValueError):
            await coro

    assert order == ["ensure", "parse"] * 6


def test_self_hosted_gitlab_accepts_absolute_fqdn_url(monkeypatch) -> None:
    """A trailing dot is the absolute-FQDN form of the same host.

    The allowlist is dot-normalized by the config loader, so the URL side must
    normalize too or the two can never agree and the link is rejected. Existing
    tests cover dotted config ENTRIES; this covers a dotted URL.
    """
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    ref = source.parse_source_url("https://gitlab.acme.internal./team/api/-/merge_requests/7")
    assert ref.host == "gitlab.acme.internal"
    assert ref.url == "https://gitlab.acme.internal/team/api/-/merge_requests/7"


def test_self_hosted_gitlab_treats_default_https_port_as_absent(monkeypatch) -> None:
    """The browser URL API drops :443, so the backend must too or an explicit
    :443 URL builds a Changes tab the backend then refuses to load."""
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    ref = source.parse_source_url("https://gitlab.acme.internal:443/a/b/-/merge_requests/1")
    assert ref.host == "gitlab.acme.internal"
    assert ref.url == "https://gitlab.acme.internal/a/b/-/merge_requests/1"


def test_manual_pipeline_fallback_is_pending_not_skipped() -> None:
    """A pipeline standing in for its jobs keeps PIPELINE-level semantics.

    The synthesized record carries no ``allow_failure``, so the job-level reading
    would call a `manual` pipeline skipped and the frontend would roll that up as
    passed -- contradicting the chip, which reports a blocked pipeline as running.
    """
    manual = source._gitlab_pipeline_as_check({"status": "manual", "web_url": "https://x/1"})
    assert manual["bucket"] == "pending"
    # A terminal pipeline is unaffected.
    assert source._gitlab_pipeline_as_check({"status": "success"})["bucket"] == "passed"
    assert source._gitlab_pipeline_as_check({"status": "skipped"})["bucket"] == "skipped"


def test_job_level_manual_stays_skipped_while_pipeline_level_blocks() -> None:
    """One optional manual job among finished ones is not a blocked build, but a
    pipeline whose own status is `manual` is waiting on a required job."""
    assert source._gitlab_check({"name": "deploy", "status": "manual"})["bucket"] == "skipped"
    assert source._gitlab_aggregate_ci("manual") == "running"


def test_required_manual_job_is_pending_not_skipped() -> None:
    """allow_failure=False makes a manual job a gate: rolling it up as skipped
    would let the Checks tab read green while the build waits on a human."""
    required = source._gitlab_check({"name": "deploy", "status": "manual", "allow_failure": False})
    optional = source._gitlab_check({"name": "deploy", "status": "manual", "allow_failure": True})
    assert required["bucket"] == "pending"
    assert optional["bucket"] == "skipped"


@pytest.mark.asyncio
async def test_github_payload_identity_ignores_provider_supplied_url(monkeypatch) -> None:
    """Same hardening as the GitLab case, asserted on the GitHub path.

    Both providers echo an identity (`url`/`number`) that the browser later
    submits back for refresh and thread resolution. Reverting either provider's
    pin lets a mismatched payload steer an owner-authenticated call at a
    different pull request, so each needs its own regression test.
    """

    async def fake_run(*argv: str, **kwargs):
        if "--json" in argv:
            return {
                "number": 999,
                "url": "https://github.com/victim/repo/pull/1",
                "title": "t",
                "state": "OPEN",
            }
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://github.com/acme/widgets/pull/12")

    data = await source._fetch_github(ref)

    assert data["url"] == "https://github.com/acme/widgets/pull/12"
    assert data["number"] == 12


@pytest.mark.asyncio
async def test_payload_identity_ignores_provider_supplied_url(monkeypatch) -> None:
    """The browser submits the payload url back for refresh/resolve, so a
    compromised or hostile instance echoing someone else's web_url must not be
    able to steer an owner-authenticated call at an unrelated merge request."""
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )

    async def fake_run(*argv: str, **kwargs):
        if argv[-1].endswith("merge_requests/7") or kwargs.get("host"):
            if "merge_requests/7" in " ".join(argv):
                return {
                    "iid": 999,
                    "title": "t",
                    "state": "opened",
                    "web_url": "https://gitlab.com/victim/repo/-/merge_requests/1",
                }
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://gitlab.acme.internal/team/api/-/merge_requests/7")

    data = await source._fetch_gitlab(ref)

    assert data["url"] == "https://gitlab.acme.internal/team/api/-/merge_requests/7"
    assert data["number"] == 7


@pytest.mark.asyncio
async def test_glab_does_not_forward_ambient_token_to_a_self_managed_host(monkeypatch) -> None:
    """GITLAB_TOKEN has no host binding, so forwarding it while GITLAB_HOST points
    at a self-managed instance would hand a gitlab.com PAT to that server."""

    class FakeProcess:
        returncode = 0

    sandbox = MagicMock(
        return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/glab"], {"SAFE": "1"}, None)
    )
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-" + "a" * 20)
    monkeypatch.setenv("GLAB_CONFIG_DIR", "/home/user/.config/glab-cli")
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/glab")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))

    await source._run_json("glab", "api", "projects/1", host="gitlab.acme.internal")
    env = sandbox.call_args.kwargs["env"]
    assert "GITLAB_TOKEN" not in env
    # The per-host credential store is still reachable -- that is how a
    # self-managed instance is expected to authenticate.
    assert env["GLAB_CONFIG_DIR"] == "/home/user/.config/glab-cli"
    assert env["GITLAB_HOST"] == "gitlab.acme.internal"

    # gitlab.com keeps the ambient token: it is the host the token belongs to.
    sandbox.reset_mock()
    await source._run_json("glab", "api", "projects/1", host="gitlab.com")
    assert sandbox.call_args.kwargs["env"]["GITLAB_TOKEN"].startswith("glpat-")


@pytest.mark.asyncio
async def test_glab_chip_refresh_hands_the_cli_an_explicit_session_bus_address(monkeypatch) -> None:
    """The sidebar PR/MR chip refresh runs one ``glab api`` per open MR on a timer.

    glab asks the keyring over D-Bus, and a child left with no ``DBUS_SESSION_BUS_ADDRESS``
    searches for the bus itself and, where none exists, autolaunches a private
    ``dbus-daemon`` plus ``gnome-keyring-daemon`` that outlive the call, one pair per
    probe. The strict allowlist never carries the key, so the runner pins the value the
    parent resolved once: the user bus by its standard path when it exists, an inert
    address otherwise, and never the gateway's own ambient value.
    """

    class FakeProcess:
        returncode = 0

    sandbox = MagicMock(
        return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/glab"], {"SAFE": "1"}, None)
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/ambient/forwarded/bus")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-" + "a" * 20)
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/glab")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))

    for resolved in (github_runner.INERT_SESSION_BUS_ADDRESS, "unix:path=/run/user/4242/bus"):
        monkeypatch.setattr(
            github_runner, "session_bus_address", lambda resolved=resolved: resolved
        )
        sandbox.reset_mock()
        await source._run_json("glab", "api", "projects/1", host="gitlab.com")
        env = sandbox.call_args.kwargs["env"]
        assert env["DBUS_SESSION_BUS_ADDRESS"] == resolved
        assert env["DBUS_SESSION_BUS_ADDRESS"] != os.environ["DBUS_SESSION_BUS_ADDRESS"]


@pytest.mark.asyncio
async def test_gitlab_mutations_forward_host_to_the_run_json_guard(monkeypatch) -> None:
    """Regression: the auto-merge and mark-ready mutation call sites must thread
    ``host=ref.host`` into :func:`_run_json`.

    ``host`` is REQUIRED for glab, so a call site that drops it is refused at the
    guard (``host_not_specified``) and the mutation dies for every GitLab host,
    including gitlab.com. Every existing mutation test monkeypatches ``_run_json``
    wholesale, which makes exactly that omission invisible. This drives both entry
    points through the REAL ``_run_json`` -- mocking only the pre-mutation read and
    the sandbox -- and asserts the self-managed host reaches ``GITLAB_HOST``. Drop
    the ``host=ref.host`` argument at either site and the guard raises before the
    sandbox is reached, failing this test.
    """

    class FakeProcess:
        returncode = 0

    sandbox = MagicMock(
        return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/glab"], {"SAFE": "1"}, None)
    )
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/glab")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))
    monkeypatch.setattr(source, "_invalidate_pull_request_cache", AsyncMock())

    url = "https://gitlab.acme.internal/team/api/-/merge_requests/7"

    # enable_pull_request_auto_merge: a running pipeline genuinely gates the
    # merge, so no immediate-merge confirmation is required and the PUT dispatches.
    async def _read_armable(_ref):
        return {"merge_when_pipeline_succeeds": False, "head_pipeline": {"status": "running"}}

    monkeypatch.setattr(source, "_gitlab_merge_request", _read_armable)
    sandbox.reset_mock()
    assert await source.enable_pull_request_auto_merge(url) == "pipeline"
    assert sandbox.call_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.acme.internal"

    # mark_pull_request_ready: the MR is a draft, so the set-draft mutation runs.
    async def _read_draft(_ref):
        return {"draft": True}

    monkeypatch.setattr(source, "_gitlab_merge_request", _read_draft)
    sandbox.reset_mock()
    await source.mark_pull_request_ready(url)
    assert sandbox.call_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.acme.internal"


@pytest.mark.asyncio
async def test_gitlab_merge_request_read_forwards_host(monkeypatch) -> None:
    """The pre-mutation read is the third glab call site.

    Kept separate rather than folded into the dispatch test above: that test has
    to stub ``_gitlab_merge_request`` to reach the dispatches, and undoing the
    stub mid-test would also drop this module's autouse fixture patches, running
    the real ``_run_json`` without the suite's normal isolation and audit stub.
    """

    class FakeProcess:
        returncode = 0

    sandbox = MagicMock(
        return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/glab"], {"SAFE": "1"}, None)
    )
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/glab")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(
        source, "_collect_process_output", AsyncMock(return_value=(b'{"draft": false}', b""))
    )

    ref = source.parse_source_url("https://gitlab.acme.internal/team/api/-/merge_requests/7")
    assert await source._gitlab_merge_request(ref) == {"draft": False}
    assert sandbox.call_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.acme.internal"


@pytest.mark.asyncio
async def test_concurrent_allowlist_refresh_cannot_restore_a_revoked_host(monkeypatch) -> None:
    """Without serialization, a loader holding the PRE-revocation config could
    install its snapshot after the post-revocation one and re-admit the removed
    host for another full TTL."""
    started = source.asyncio.Event()
    release = source.asyncio.Event()
    loads = {"n": 0}

    def slow_stale_load() -> (
        tuple[frozenset[str], frozenset[str], bool, frozenset[str], frozenset[str], frozenset[str]]
    ):
        loads["n"] += 1
        # asyncio.Event is not thread-safe: this runs in a worker thread, so the
        # set() must be marshalled back onto the loop.
        loop.call_soon_threadsafe(started.set)
        # Block inside the worker thread so a second waiter queues on the lock. The
        # hold ends when the test sets ``release``; the timeout is a lost-run bound.
        source.asyncio.run_coroutine_threadsafe(_noop(), loop).result(timeout=default_timeout())
        return (
            frozenset({"gitlab.acme.internal"}),
            frozenset(),
            True,
            frozenset(),
            frozenset(),
            frozenset(),
        )

    async def _noop() -> None:
        # flake-ok: pre-existing unbounded hold in the stub until the test releases it, moved verbatim; tracked for a de-flake pass
        await release.wait()

    loop = source.asyncio.get_running_loop()
    monkeypatch.setattr(source, "_load_source_link_settings", slow_stale_load)
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 0.0)
    monkeypatch.setattr(source, "_gitlab_hosts_lock", source.asyncio.Lock())

    first = loop.create_task(source.ensure_gitlab_hosts_loaded())
    # flake-ok: pre-existing unbounded wait for the stub to signal it started, moved verbatim; tracked for a de-flake pass
    await started.wait()
    second = loop.create_task(source.ensure_gitlab_hosts_loaded())
    await source.asyncio.sleep(0)
    release.set()
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    await first
    # flake-ok: pre-existing unbounded await of a task the test has already released, moved verbatim; tracked for a de-flake pass
    await second

    # The second caller reused the fresh snapshot instead of running its own load.
    assert loads["n"] == 1


@pytest.mark.asyncio
async def test_allowlist_generation_bumps_only_on_content_change(monkeypatch) -> None:
    """Per-slot sidebar caches key off this, so it must change when (and only
    when) the host set actually changes."""
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 0.0)
    monkeypatch.setattr(source, "_gitlab_hosts_generation", 0)

    source._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
    first = source.gitlab_hosts_generation()
    assert first == 1

    source._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
    assert source.gitlab_hosts_generation() == first

    source._publish_provider_hosts(frozenset(), frozenset())
    assert source.gitlab_hosts_generation() == first + 1


def test_self_hosted_gitlab_rejected_when_allowlist_empty(monkeypatch) -> None:
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset())
    with pytest.raises(ValueError, match="dashboard.gitlab_hosts"):
        source.parse_source_url("https://gitlab.acme.internal/team/api/-/merge_requests/7")


def test_self_hosted_gitlab_accepted_when_allowlisted(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    ref = source.parse_source_url(
        "https://gitlab.acme.internal/team/platform/api/-/merge_requests/7"
    )
    assert ref.provider == "gitlab"
    assert ref.host == "gitlab.acme.internal"
    assert ref.project == "team/platform/api"
    assert ref.repo == "api"
    assert ref.number == 7
    # The normalized URL keeps the self-managed host so cache keys, the panel's
    # external link, and the CLI host pin all agree.
    assert ref.url == "https://gitlab.acme.internal/team/platform/api/-/merge_requests/7"


@pytest.mark.parametrize(
    ("allowlist", "url", "allowed"),
    [
        # An entry without a port does not authorize an arbitrary port.
        (
            {"gitlab.acme.internal"},
            "https://gitlab.acme.internal:8443/a/b/-/merge_requests/1",
            False,
        ),
        (
            {"gitlab.acme.internal:8443"},
            "https://gitlab.acme.internal:8443/a/b/-/merge_requests/1",
            True,
        ),
        # Exact match only: no suffix or lookalike widening.
        (
            {"gitlab.acme.internal"},
            "https://evil-gitlab.acme.internal/a/b/-/merge_requests/1",
            False,
        ),
        (
            {"gitlab.acme.internal"},
            "https://gitlab.acme.internal.evil.test/a/b/-/merge_requests/1",
            False,
        ),
        ({"acme.internal"}, "https://gitlab.acme.internal/a/b/-/merge_requests/1", False),
        # www is not stripped for a self-managed host, unlike gitlab.com.
        (
            {"gitlab.acme.internal"},
            "https://www.gitlab.acme.internal/a/b/-/merge_requests/1",
            False,
        ),
    ],
)
def test_self_hosted_gitlab_matches_host_exactly(monkeypatch, allowlist, url, allowed) -> None:
    monkeypatch.setattr(source, "_allowed_gitlab_hosts", lambda: frozenset(allowlist))
    if allowed:
        assert source.parse_source_url(url).provider == "gitlab"
    else:
        with pytest.raises(ValueError):
            source.parse_source_url(url)


def test_self_hosted_gitlab_still_requires_https_and_mr_path(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    with pytest.raises(ValueError, match="HTTPS"):
        source.parse_source_url("http://gitlab.acme.internal/a/b/-/merge_requests/1")
    with pytest.raises(ValueError, match="HTTPS"):
        source.parse_source_url("https://user:pw@gitlab.acme.internal/a/b/-/merge_requests/1")
    # An issue path is now a supported shape (kind="issue"), so the rejection
    # case is a path that is neither: a self-managed host does not widen which
    # OBJECTS are readable.
    with pytest.raises(ValueError, match="Expected a GitLab URL"):
        source.parse_source_url("https://gitlab.acme.internal/a/b/-/tree/main")
    with pytest.raises(ValueError, match="Expected a GitLab URL"):
        source.parse_source_url("https://gitlab.acme.internal/a/b/-/snippets/1")


@pytest.mark.asyncio
async def test_run_json_pins_glab_to_the_allowlisted_host(monkeypatch) -> None:
    class FakeProcess:
        returncode = 0

    sandbox = MagicMock(
        return_value=(["/usr/bin/sandbox-launcher", "/usr/bin/glab"], {"SAFE": "1"}, None)
    )
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    monkeypatch.setattr(source, "_resolve_provider_executable", lambda _name: "/usr/bin/glab")
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(
        source.asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess())
    )
    monkeypatch.setattr(source, "_collect_process_output", AsyncMock(return_value=(b"{}", b"")))

    assert await source._run_json("glab", "api", "projects/1", host="gitlab.acme.internal") == {}
    assert sandbox.call_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.acme.internal"

    sandbox.reset_mock()
    assert await source._run_json("glab", "api", "projects/1", host="gitlab.com") == {}
    assert sandbox.call_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.com"


@pytest.mark.asyncio
async def test_run_json_refuses_glab_without_an_explicit_host(monkeypatch) -> None:
    """A call site that forgets `host` must fail loudly, not silently target
    gitlab.com: an allowlisted self-managed MR could otherwise be read -- or
    mutated -- on the PUBLIC instance at the same project/IID."""
    resolver = MagicMock()
    sandbox = MagicMock()
    monkeypatch.setattr(source, "_resolve_provider_executable", resolver)
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)

    with pytest.raises(source.SourceProviderError, match="host is required"):
        await source._run_json("glab", "api", "projects/1")

    resolver.assert_not_called()
    sandbox.assert_not_called()


@pytest.mark.asyncio
async def test_run_json_refuses_glab_host_outside_allowlist(monkeypatch) -> None:
    """Defense in depth: a caller cannot reach an unauthorized instance even if a
    future code path skips parse_source_url."""
    resolver = MagicMock()
    sandbox = MagicMock()
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    monkeypatch.setattr(source, "_resolve_provider_executable", resolver)
    monkeypatch.setattr(source, "sandboxed_spawn_argv", sandbox)

    with pytest.raises(source.SourceProviderError, match="not allowlisted"):
        await source._run_json("glab", "api", "projects/1", host="gitlab.evil.test")

    resolver.assert_not_called()
    sandbox.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_gitlab_threads_self_hosted_host_through_every_call(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "_allowed_gitlab_hosts", lambda: frozenset({"gitlab.acme.internal"})
    )
    hosts: list[str] = []

    async def fake_run(*_argv: str, **kwargs):
        hosts.append(kwargs.get("host", ""))
        command = " ".join(_argv)
        if command.endswith("merge_requests/7"):
            return {"iid": 7, "title": "t", "state": "opened", "web_url": "https://x/1"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://gitlab.acme.internal/team/api/-/merge_requests/7")

    await source._fetch_gitlab(ref)

    assert hosts and set(hosts) == {"gitlab.acme.internal"}


@pytest.mark.asyncio
async def test_fetch_gitlab_flattens_discussions_with_resolve_fields(monkeypatch) -> None:
    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            return {
                "iid": 42,
                "title": "Fix pipeline",
                "description": "",
                "state": "opened",
                "web_url": "https://gitlab.com/acme/repo/-/merge_requests/42",
                "source_branch": "fix",
                "target_branch": "main",
                "sha": "def456",
                "changes_count": "1",
                "author": {"username": "dev"},
            }
        if "/discussions?" in command:
            return [
                {
                    "id": "a1b2c3",
                    "notes": [
                        {
                            "id": 7,
                            "author": {"username": "reviewer"},
                            "body": "Please fix",
                            "resolvable": True,
                            "resolved": False,
                        },
                        {"id": 8, "system": True, "body": "changed the description"},
                    ],
                }
            ]
        if "/commits?" in command or "/pipelines?" in command:
            return []
        if "/changes" in command:
            return {"changes": []}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert data["provider"] == "gitlab"
    assert data["partialSections"] == ["files"]
    assert len(data["comments"]) == 1  # system note filtered out
    comment = data["comments"][0]
    assert comment["threadId"] == "a1b2c3"
    assert comment["resolvable"] is True
    assert comment["resolved"] is False
