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

from kiro_crew.apps.builtins.auto_triage_pipeline.backend import pipeline_fold as fold
from kiro_crew.apps.builtins.auto_triage_pipeline.backend import routes


@pytest.fixture(name="enabled")
def enabled_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the deny-by-default gate open.

    It reads installed.json, absent under a tmp home, so a real read would 403
    every request. ``test_a_disabled_app_refuses_every_route`` does NOT use this,
    so the closed path is covered too.
    """
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)


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

    def fake_fold(*, recent_hours: int):
        seen["hours"] = recent_hours
        return _Row({"steps": [{"step": "scan", "inFlight": 2}]})

    monkeypatch.setattr(fold, "fold_pipeline", fake_fold)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/overview?hours=48")
        assert resp.status == 200
        assert (await resp.json())["steps"][0]["step"] == "scan"
    assert seen["hours"] == 48


@pytest.mark.asyncio
async def test_overview_falls_back_to_the_default_window_for_junk_hours(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad ``hours`` is a DEFAULT, not a 400.

    The window only widens or narrows a throughput figure, so refusing the whole
    page over it would trade a readable default for an error screen.
    """
    seen: dict[str, Any] = {}

    def fake_fold(*, recent_hours: int):
        seen["hours"] = recent_hours
        return _Row({"steps": []})

    monkeypatch.setattr(fold, "fold_pipeline", fake_fold)
    async with client_for(make_app()) as client:
        for raw in ("-5", "abc", "", "1234567890"):
            resp = await client.get(f"{routes.PREFIX}/overview?hours={raw}")
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
        resp = await client.get(f"{routes.PREFIX}/overview")
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
        resp = await client.get(f"{routes.PREFIX}/overview")
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

    def fake_list(number):
        seen["number"] = number
        return [
            _Row({"slot": "chat:1", "credits": 17.75}),
            _Row({"slot": "chat:2", "credits": 511}),
        ]

    monkeypatch.setattr(fold, "list_item_sessions", fake_list)
    monkeypatch.setattr(fold, "populated_columns", lambda rows: ["credits", "turns"])
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/item/sessions?number=5546")
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
            resp = await client.get(f"{routes.PREFIX}/item/sessions?number={bad}")
            assert resp.status == 400, bad
            assert (await resp.json())["code"] == "number_required", bad


@pytest.mark.asyncio
async def test_item_sessions_maps_a_bad_item_to_400_and_a_read_failure_to_503(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_fold(_number):
        raise fold.FoldError("unknown item")

    monkeypatch.setattr(fold, "list_item_sessions", raise_fold)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/item/sessions?number=1")
        assert resp.status == 400
        assert (await resp.json())["code"] == "bad_item"

    def raise_os(_number):
        raise OSError(5, "I/O error", "/home/someone/usage.jsonl")

    monkeypatch.setattr(fold, "list_item_sessions", raise_os)
    async with client_for(make_app()) as client:
        resp = await client.get(f"{routes.PREFIX}/item/sessions?number=1")
        assert resp.status == 503
        body = await resp.json()
        assert body["code"] == "unreadable"
        assert "someone" not in body["error"]


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


def test_the_route_prefix_tracks_the_manifest_name() -> None:
    """The manifest's permissions.api entries and these paths must not drift."""
    assert routes.PREFIX == f"/api/apps/{routes.APP_NAME}"
