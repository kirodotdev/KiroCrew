"""HTTP-level tests for the Auto Triage Pipeline's three read routes.

The fold layer has its own tests; these drive the ROUTES -- the enable gate, the
query validators, the error mapping and the response envelopes -- because a
handler can be wrong in ways a fold test cannot see: a validator that accepts a
path-escaping name, a FoldError mapped to the wrong status, or a payload whose
shape the view does not expect.

The app is deny-by-default and re-checks enablement per request, so the gate is
stubbed open for the happy paths and left CLOSED in its own test.

Clients use ``TestClient(TestServer(app))``, the pattern the rest of the suite
uses, rather than aiohttp's pytest plugin.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.issue_radar.backend import pipeline_fold as fold
from kiro_crew.apps.builtins.issue_radar.backend import pipeline_routes as routes

#: The repositories these route tests name. Connected by the ``enabled`` fixture, so
#: the handlers get past the host app's authorization gate and each test can reach
#: what it is actually about. An enumerated SET rather than "connect everything",
#: because that would make the gate untestable from this fixture -- a name outside
#: this set is what ``test_every_route_refuses_an_unconnected_repository`` uses.
CONNECTED_REPOS = frozenset({("o", "r"), ("acme", "widgets")})


@pytest.fixture(name="enabled")
def enabled_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub BOTH deny-by-default gates open.

    ``is_app_enabled`` reads installed.json, absent under a tmp home, so a real
    read would 403 every request. ``store.is_repo_connected`` reads the connected
    list, empty under a tmp home, so a real read would 404 every request.

    Each closed path keeps its own coverage rather than relying on this fixture's
    absence to test it: ``test_a_disabled_app_refuses_every_route`` does not use
    this fixture at all, and ``test_every_route_refuses_an_unconnected_repository``
    reopens only the app gate so the repo gate is the one thing under test.
    """
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)
    monkeypatch.setattr(
        routes.store,
        "is_repo_connected",
        lambda owner, repo, **_kwargs: (owner, repo) in CONNECTED_REPOS,
    )


def make_app() -> web.Application:
    application = web.Application()
    routes.register_routes(application)
    return application


def client_for(app: web.Application) -> TestClient:
    return TestClient(TestServer(app))


class _Row:
    """Stand-in for a fold row: the handlers only require ``to_dict``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return self._payload


# ── the enable gate ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_disabled_app_refuses_every_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routes are mounted unconditionally, so each handler must re-check.

    Without the per-handler check a disabled app would stay fully callable, which
    is the whole reason the decorator exists.
    """
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: False)
    async with client_for(make_app()) as client:
        for path, query in (
            ("/overview", ""),
            ("/step", "?step=implement&owner=o&repo=r"),
            ("/item/sessions", "?number=1"),
        ):
            resp = await client.get(f"{routes.PREFIX}{path}{query}")
            assert resp.status == 403
            assert (await resp.json())["code"] == "app_disabled"


# ── L0: overview ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_returns_the_folded_pipeline(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_fold(*, recent_hours: int, repo: str | None = None):
        seen["hours"] = recent_hours
        seen["repo"] = repo
        return _Row({"steps": [{"step": "scan", "inFlight": 2}]})

    monkeypatch.setattr(fold, "fold_pipeline", fake_fold)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/overview?hours=48&owner=o&repo=r")
        assert resp.status == 200
        assert (await resp.json())["steps"][0]["step"] == "scan"
    assert seen["hours"] == 48
    # The handler passes the repository through as the fold's scope. It is required,
    # so there is no arm where it arrives as None.
    assert seen["repo"] == "o/r"


@pytest.mark.asyncio
async def test_overview_falls_back_to_the_default_window_for_junk_hours(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad ``hours`` is a DEFAULT, not a 400.

    The window only widens or narrows a throughput figure, so refusing the whole
    page over it would trade a readable default for an error screen.
    """
    seen: dict[str, Any] = {}

    def fake_fold(*, recent_hours: int, repo: str | None = None):
        seen["hours"] = recent_hours
        return _Row({"steps": []})

    monkeypatch.setattr(fold, "fold_pipeline", fake_fold)
    async with client_for(make_app()) as client:
        for raw in ("-5", "abc", "", "1234567890"):
            resp = await client.get(f"{routes.PREFIX}/overview?hours={raw}&owner=o&repo=r")
            assert resp.status == 200
            assert seen["hours"] == fold.DEFAULT_RECENT_HOURS, raw


@pytest.mark.asyncio
async def test_overview_reports_an_unreadable_source_as_503_not_500(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_fold(**_kwargs):
        raise fold.FoldError("the audit log could not be read")

    monkeypatch.setattr(fold, "fold_pipeline", raise_fold)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/overview?owner=o&repo=r")
        assert resp.status == 503
        body = await resp.json()
        assert body["code"] == "unreadable"
        # The fold layer authors this message, and it must not name a local path.
        assert ":\\" not in body["error"] and not body["error"].startswith("/")


@pytest.mark.asyncio
async def test_overview_maps_an_os_error_to_503_without_leaking_it(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError's own text carries the offending PATH, so it is not echoed."""

    def raise_os(**_kwargs):
        raise PermissionError(13, "Permission denied", "/home/someone/secret/audit.jsonl")

    monkeypatch.setattr(fold, "fold_pipeline", raise_os)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/overview?owner=o&repo=r")
        assert resp.status == 503
        body = await resp.json()
        assert body["code"] == "unreadable"
        assert "secret" not in body["error"]


# ── L1: step items ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_returns_the_items_and_their_count(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_list(step, *, owner, repo, limit):
        seen.update(step=step, owner=owner, repo=repo, limit=limit)
        return [_Row({"number": 4624}), _Row({"number": 5546})]

    monkeypatch.setattr(fold, "list_step_items", fake_list)
    async with client_for(make_app()) as client:
        resp = await client.get(
            f"{routes.PREFIX}/step?step=implement&owner=acme&repo=widgets&limit=7"
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["step"] == "implement"
        assert body["count"] == 2
        assert [i["number"] for i in body["items"]] == [4624, 5546]
    assert seen == {"step": "implement", "owner": "acme", "repo": "widgets", "limit": 7}


@pytest.mark.asyncio
async def test_step_requires_owner_repo_and_step(enabled: None) -> None:
    async with client_for(make_app()) as client:
        for query, code in (
            ("?step=implement", "repo_required"),
            ("?step=implement&owner=o", "repo_required"),
            ("?owner=o&repo=r", "step_required"),
            ("?step=%20%20&owner=o&repo=r", "step_required"),
        ):
            resp = await client.get(f"{routes.PREFIX}/step{query}")
            assert resp.status == 400, query
            assert (await resp.json())["code"] == code, query


@pytest.mark.asyncio
async def test_step_refuses_a_name_that_is_not_simply_a_name(enabled: None) -> None:
    """Both values become path segments when the issue cache is read.

    ``D:foo`` is the case a deny-list missed: on Windows it is drive-RELATIVE and
    resolves against that drive's current directory, escaping the cache root even
    though it contains no slash.
    """
    async with client_for(make_app()) as client:
        for bad in ("../etc", "a/b", "a\\b", ".hidden", "D:foo", "x" * 101, "na me"):
            resp = await client.get(f"{routes.PREFIX}/step?step=implement&owner={bad}&repo=r")
            assert resp.status == 400, bad
            assert (await resp.json())["code"] == "repo_invalid", bad


@pytest.mark.asyncio
async def test_every_repo_scoped_route_refuses_a_foreign_forge(enabled: None) -> None:
    """A repository on another forge is refused, not silently read as GitHub's.

    The pipeline's data is keyed on ``owner/repo`` alone -- the jobs that write the
    trail and the queue shard are GitHub-only and stamp no forge -- so honouring a
    GitLab request would return the PUBLIC GITHUB repository of the same slug: its
    items, its sessions, its credit costs, under the other repository's heading.

    Asserted on all three repo-scoped routes together, because the refusal lives in
    the two shared resolvers and a fourth route added later must inherit it rather
    than re-derive it.
    """
    foreign = (
        "provider=gitlab",
        "provider=azure",
        "provider=GitLab",  # case must not smuggle it through
        "provider=github&host=ghe.internal",  # self-hosted GitHub is not public GitHub
        "host=gitlab.example.com",  # a host alone still names another forge
    )
    async with client_for(make_app()) as client:
        for scope in foreign:
            for path in (
                f"{routes.PREFIX}/overview?owner=o&repo=r&{scope}",
                f"{routes.PREFIX}/step?step=implement&owner=o&repo=r&{scope}",
                f"{routes.PREFIX}/item/sessions?number=1&owner=o&repo=r&{scope}",
            ):
                resp = await client.get(path)
                assert resp.status == 400, path
                assert (await resp.json())["code"] == "repo_provider_unsupported", path


@pytest.mark.asyncio
async def test_a_nested_namespace_is_refused_as_a_forge_not_as_a_bad_name(
    enabled: None,
) -> None:
    """The name rule is GITHUB's shape, so it must not answer first.

    `_REPO_NAME_RE` allows no slash, while GitLab nests a group path and Azure
    ALWAYS carries "{organization}/{project}" -- so validating before the forge
    guard answered `repo_invalid` for every Azure repository and every nested
    GitLab group. That code is generic and retryable, and the client offers a Retry
    that can never succeed; `repo_provider_unsupported` is the one the client
    explains and stops polling on. The cases above all pass GitHub-shaped names, so
    they cannot catch this -- which is why it reached review.
    """
    nested = (
        ("group/sub", "thing", "provider=gitlab"),
        ("myorg/myproject", "thing", "provider=azure"),
        ("group/sub/deeper", "thing", "host=gitlab.example.com"),
    )
    async with client_for(make_app()) as client:
        for owner, repo, scope in nested:
            for path in (
                f"{routes.PREFIX}/overview?owner={owner}&repo={repo}&{scope}",
                f"{routes.PREFIX}/step?step=implement&owner={owner}&repo={repo}&{scope}",
                f"{routes.PREFIX}/item/sessions?number=1&owner={owner}&repo={repo}&{scope}",
            ):
                resp = await client.get(path)
                body = await resp.json()
                assert body["code"] == "repo_provider_unsupported", f"{path} -> {body}"


@pytest.mark.asyncio
async def test_a_bad_name_on_github_is_still_a_bad_name(enabled: None) -> None:
    """Reordering must not stop the name rule applying where it belongs.

    Nothing above says a slash is acceptable -- only that a non-GitHub request is
    refused for the right reason first. A GitHub request still has to carry a
    GitHub-shaped name, because both halves become path segments when the issue
    cache is read.
    """
    async with client_for(make_app()) as client:
        for bad in ("a/b", "../etc", "-lead", "sp ace"):
            resp = await client.get(f"{routes.PREFIX}/overview?owner={bad}&repo=r")
            assert resp.status == 400, bad
            assert (await resp.json())["code"] == "repo_invalid", bad


@pytest.mark.asyncio
async def test_public_github_is_accepted_however_it_is_spelled(enabled: None) -> None:
    """The refusal must not catch the repositories the board is FOR.

    An absent provider means public GitHub across the rest of Issue Radar, so a
    request that names none is the common case and must pass -- that is what keeps
    every pre-existing caller working. The explicit spellings are here so the guard
    cannot be tightened into rejecting them by accident.
    """
    async with client_for(make_app()) as client:
        for scope in (
            "",
            "provider=github",
            "provider=github&host=github.com",
            "provider=GitHub&host=GitHub.com",
            "host=www.github.com",
        ):
            sep = "&" if scope else ""
            resp = await client.get(f"{routes.PREFIX}/overview?owner=o&repo=r{sep}{scope}")
            assert resp.status == 200, scope


@pytest.mark.asyncio
async def test_every_route_refuses_an_unconnected_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name that parses is not permission to read that repository's data.

    This is the HOST APP's authorization gate: ``routes.py`` enforces
    ``store.is_repo_connected`` at every repo-scoped handler it owns, and these three
    read the same per-repository data -- the issue cache under that repo's own
    directory, and its queue shard. Disconnecting does not erase any of it, so a
    name-only check would keep serving a removed repository's titles, assignees,
    slots and credit costs.

    Only the APP gate is opened here, not the repo gate, so the refusal under test is
    the one this test is named for. Asserted on all three routes together because the
    gate is per-handler, and a fourth added later must be given it explicitly.
    """
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)
    monkeypatch.setattr(routes.store, "is_repo_connected", lambda *_a, **_k: False)

    called: list[str] = []
    monkeypatch.setattr(fold, "fold_pipeline", lambda **_k: called.append("fold"))
    monkeypatch.setattr(fold, "list_step_items", lambda *_a, **_k: called.append("step"))
    monkeypatch.setattr(fold, "list_item_sessions", lambda *_a, **_k: called.append("sessions"))

    async with client_for(make_app()) as client:
        for path in (
            f"{routes.PREFIX}/overview?owner=someone&repo=else",
            f"{routes.PREFIX}/step?step=implement&owner=someone&repo=else",
            f"{routes.PREFIX}/item/sessions?number=1&owner=someone&repo=else",
        ):
            resp = await client.get(path)
            assert resp.status == 404, path
            assert (await resp.json())["code"] == "repo_not_connected", path

    assert called == [], "a refused request must not read any repository data"


@pytest.mark.asyncio
async def test_step_maps_an_unknown_step_to_400_and_a_read_failure_to_503(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad step name is the CALLER's error; an unreadable source is ours."""

    def raise_fold(*_args, **_kwargs):
        raise fold.FoldError("no such step")

    monkeypatch.setattr(fold, "list_step_items", raise_fold)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/step?step=nope&owner=o&repo=r")
        assert resp.status == 400
        assert (await resp.json())["code"] == "bad_step"

    def raise_os(*_args, **_kwargs):
        raise OSError(5, "I/O error", "/home/someone/queue.json")

    monkeypatch.setattr(fold, "list_step_items", raise_os)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/step?step=implement&owner=o&repo=r")
        assert resp.status == 503
        body = await resp.json()
        assert body["code"] == "unreadable"
        assert "someone" not in body["error"]


# ── L2: item sessions ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_item_sessions_returns_rows_and_the_populated_columns(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The column list is what lets the table omit structurally-zero columns."""
    seen: dict[str, Any] = {}

    def fake_list(number, *, repo=None):
        seen["number"] = number
        seen["repo"] = repo
        return [
            _Row({"slot": "chat:1", "credits": 17.75}),
            _Row({"slot": "chat:2", "credits": 511}),
        ]

    monkeypatch.setattr(fold, "list_item_sessions", fake_list)
    monkeypatch.setattr(fold, "populated_columns", lambda rows: ["credits", "turns"])
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/item/sessions?number=5546&owner=o&repo=r")
        assert resp.status == 200
        body = await resp.json()
        assert body["number"] == 5546
        assert body["count"] == 2
        assert body["populatedColumns"] == ["credits", "turns"]
        # Across-retries summing is the point of this level: both slots are here.
        assert [s["slot"] for s in body["sessions"]] == ["chat:1", "chat:2"]
    # Passed through as an int, not the raw string.
    assert seen["number"] == 5546


@pytest.mark.asyncio
async def test_item_sessions_requires_a_plain_number(enabled: None) -> None:
    """``isdecimal`` refuses the signs, spaces and oversized values a cast accepts."""
    async with client_for(make_app()) as client:
        # "%2B1" is a literal plus. A bare "+1" would arrive as " 1", because "+"
        # in a query string IS an encoded space -- which strips to a valid "1".
        for bad in ("", "  ", "abc", "-1", "%2B1", "1.5", "1e3", "0x10", "1234567890"):
            resp = await client.get(f"{routes.PREFIX}/item/sessions?number={bad}&owner=o&repo=r")
            assert resp.status == 400, bad
            assert (await resp.json())["code"] == "number_required", bad


@pytest.mark.asyncio
async def test_item_sessions_maps_a_bad_item_to_400_and_a_read_failure_to_503(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_fold(_number, *, repo=None):
        raise fold.FoldError("unknown item")

    monkeypatch.setattr(fold, "list_item_sessions", raise_fold)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/item/sessions?number=1&owner=o&repo=r")
        assert resp.status == 400
        assert (await resp.json())["code"] == "bad_item"

    def raise_os(_number, *, repo=None):
        raise OSError(5, "I/O error", "/home/someone/usage.jsonl")

    monkeypatch.setattr(fold, "list_item_sessions", raise_os)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/item/sessions?number=1&owner=o&repo=r")
        assert resp.status == 503
        body = await resp.json()
        assert body["code"] == "unreadable"
        assert "someone" not in body["error"]


@pytest.mark.asyncio
async def test_an_unmigrated_queue_is_not_blamed_on_the_request(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both queue-reading handlers must name the real cause.

    ``QueueMigrationPending`` subclasses ``FoldError``, so without its own arm each
    of these would answer 400 ``bad_step`` / ``bad_item`` -- telling the operator
    their step or item number is wrong when in fact the queue-sharding deploy has
    not run. 503 also says "retry later", which is true here and false of a bad
    number.
    """

    def raise_pending(*_args, **_kwargs):
        raise fold.QueueMigrationPending("queue not sharded yet")

    monkeypatch.setattr(fold, "list_step_items", raise_pending)
    monkeypatch.setattr(fold, "list_item_sessions", raise_pending)
    async with client_for(make_app()) as client:
        for path in (
            f"{routes.PREFIX}/step?step=scan&owner=o&repo=r",
            f"{routes.PREFIX}/item/sessions?number=1&owner=o&repo=r",
        ):
            resp = await client.get(path)
            assert resp.status == 503, path
            assert (await resp.json())["code"] == "queue_migration_pending", path


# ── the shape of the surface itself ───────────────────────────────────────────


def test_only_read_routes_are_mounted() -> None:
    """The app is a WINDOW. A write route appearing here is a design regression.

    Asserted on the router rather than in prose so the guarantee is enforced.
    """
    app = make_app()
    methods = {
        resource.method
        for resource in app.router.routes()
        if resource.method != "HEAD"  # aiohttp pairs a HEAD with every GET
    }
    assert methods == {"GET"}
    paths = sorted(
        route.resource.canonical
        for route in app.router.routes()
        if route.method == "GET" and route.resource is not None
    )
    assert paths == [
        f"{routes.PREFIX}/item/sessions",
        f"{routes.PREFIX}/overview",
        f"{routes.PREFIX}/step",
    ]


def test_the_route_prefix_is_derived_from_the_host_app() -> None:
    """The prefix must be built from Issue Radar's own app name, never re-typed.

    These routes' clients are forward-tolerant: a non-2xx renders as "nothing
    here yet" rather than as an error. So a prefix that drifted from the host
    app's real name would not fail loudly -- it would render a pipeline that has
    never run. Deriving it from one constant is what makes that drift impossible;
    this test is what stops someone replacing the derivation with a literal.
    """
    from kiro_crew.apps.builtins.issue_radar.backend import store

    assert routes.APP_NAME == store.APP_NAME
    assert routes.PREFIX == f"/api/apps/{store.APP_NAME}/pipeline"


@pytest.mark.asyncio
async def test_overview_REQUIRES_a_repository_and_never_folds_a_refused_request(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This route names its repository or it answers nothing.

    It went through two rounds to get here, and both are worth recording. It first
    had no repository dimension at all, because the dispatch queue was one file
    keyed on the issue number alone and a narrowed fold would have served one
    repository's sessions and costs under another's name. The queue became one file
    per repository, so the dimension arrived -- as OPTIONAL, on the reasoning that
    an absent pair should stay compatible with what the route answered before.

    That reasoning was wrong, and this is the correction: there is no "before". This
    route is new, the retired app's routes are deleted, so no client existed to keep
    compatible. Nor did the arm earn its keep on merit -- the census (`repos`,
    `unattributedEvents`, `totalEvents`) is whole-trail even on a SCOPED call, so
    there is nothing an unscoped request could tell a caller that a scoped one does
    not. What it did carry was a hazard: a fold with no repository had to pick a
    queue shard, and it picked one named by a module constant.

    Three properties:
      * a full pair reaches the fold as "owner/repo";
      * an absent or PARTIAL pair is a 400 -- ?owner=acme alone names nothing;
      * a refused request never reaches the fold at all.
    """
    calls: list[dict[str, Any]] = []

    def fake_fold(**kwargs):
        calls.append(kwargs)
        return _Row({"steps": []})

    monkeypatch.setattr(fold, "fold_pipeline", fake_fold)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/overview?owner=acme&repo=widgets")
        assert resp.status == 200
        assert calls[-1]["repo"] == "acme/widgets"

        # Absent and partial are the same refusal, and neither folds.
        before = len(calls)
        for query in ("", "?owner=acme", "?repo=widgets"):
            resp = await client.get(f"{routes.PREFIX}/overview{query}")
            assert resp.status == 400, query
            assert (await resp.json())["code"] == "repo_required", query
        assert len(calls) == before, "a refused request must not reach the fold"

        # And a malformed name is refused on its own code, still without folding.
        resp = await client.get(f"{routes.PREFIX}/overview?owner=acme&repo=a/b")
        assert resp.status == 400
        assert (await resp.json())["code"] == "repo_invalid"
        assert len(calls) == before
