"""Issue Radar's GitLab groups: one group path, its review requests across subgroups.

A GitLab group is stored as ``{host, path}`` only, and its review requests are one
paginated ``groups/:id/merge_requests`` search scoped to the group tree. These
tests pin the properties that design depends on:

  * the query is built from the encoded group path with ``include_subgroups=true``,
    the reviewer filter and the open state, through the existing username check;
  * pagination stops at the pages the search cap needs, never the generic cap,
    and every field a row keeps is bounded;
  * a malformed or unknown path is refused, never widened into a broader query;
  * with no group followed nothing new happens: no ``glab`` call, no config change,
    and the connected-repo list is exactly what it was.

``glab`` is replaced by a path-table stub at ``gitlab_client._glab_run``, so no
subprocess runs and no network is touched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import NoConfiguredOwner

from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl
from kiro_crew.apps.builtins.issue_radar.backend import routes, store
from kiro_crew.apps.builtins.issue_radar.backend.errors import RepoUrlError

BASE = "/api/apps/issue-radar"

MR = {
    "iid": 12,
    "title": "Add widget",
    "web_url": "https://gitlab.com/acme/web/api/-/merge_requests/12",
    "references": {"full": "acme/web/api!12"},
    "state": "opened",
    "draft": False,
    "labels": ["bug"],
    "author": {"username": "alice"},
    "updated_at": "2026-07-02T00:00:00Z",
    "created_at": "2026-07-01T00:00:00Z",
    "reviewers": [{"username": "me"}],
    "description": "why " * 10_000,
}

ROW = {
    "number": 12,
    "title": "Add widget",
    "url": "https://gitlab.com/acme/web/api/-/merge_requests/12",
    "project": "acme/web/api",
    "author": "alice",
    "updated_at": "2026-07-02T00:00:00Z",
    "truncated_fields": [],
}

NOT_FOUND = (1, "glab: 404 Not Found (HTTP 404)")


class Router:
    """Stub for ``_glab_run``: answers from an ordered path table, records calls."""

    def __init__(self, table):
        self.table = list(table)
        self.calls: list[str] = []

    def __call__(self, argv, *, host, timeout, input_text=None):
        target = argv[2] if len(argv) > 2 else ""
        self.calls.append(target)
        for pattern, answer in self.table:
            if pattern in target:
                if isinstance(answer, tuple):
                    return SimpleNamespace(
                        stdout="", stderr=answer[1], returncode=answer[0], args=[]
                    )
                return SimpleNamespace(stdout=json.dumps(answer), stderr="", returncode=0, args=[])
        return SimpleNamespace(stdout="{}", stderr="", returncode=0, args=[])


@pytest.fixture(autouse=True)
def _isolate(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr(gl, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: None))
    _floor_monkeypatch.setattr(gl, "allowed_hosts", lambda: frozenset({"gitlab.acme.example"}))
    _floor_monkeypatch.setattr(store, "app_data_dir", lambda _name: tmp_path / "data")


@pytest.fixture
def route(monkeypatch):
    def _install(table):
        router = Router(table)
        monkeypatch.setattr(gl, "_glab_run", router)
        return router

    return _install


# ── query builder ────────────────────────────────────────────────────────────


def test_group_query_carries_include_subgroups_and_the_reviewer():
    params = gl.build_group_pr_search_query("acme/web", reviewer="me").split("&")
    assert "include_subgroups=true" in params
    assert "reviewer_username=me" in params
    assert "state=opened" in params
    assert "scope=all" in params


def test_group_query_requires_a_reviewer():
    with pytest.raises(gl.PrSearchError):
        gl.build_group_pr_search_query("acme", reviewer="")


def test_group_query_refuses_a_hostile_username():
    with pytest.raises(gl.PrSearchError):
        gl.build_group_pr_search_query("acme", reviewer="me&include_subgroups=false")


def test_group_search_is_one_call_to_the_encoded_group(route):
    router = route([("groups/", [MR])])
    rows = gl.search_group_reviews("acme/web", host="gitlab.com", reviewer="me")
    assert len(router.calls) == 1
    target = router.calls[0]
    assert target.startswith("groups/acme%2Fweb/merge_requests?")
    assert "include_subgroups=true" in target
    assert "reviewer_username=me" in target
    assert rows == [ROW]


def test_project_path_falls_back_to_the_web_url(route):
    route([("groups/", [{**MR, "references": None}])])
    out = gl.search_group_reviews("acme", host="gitlab.com", reviewer="me")
    assert out[0]["project"] == "acme/web/api"


def test_every_field_a_row_keeps_is_bounded(route):
    long_url = "https://gitlab.com/acme/x/-/merge_requests/1?" + "a" * gl.GROUP_ROW_URL_MAX
    raw = {
        **MR,
        "title": "t" * (gl.GROUP_ROW_TITLE_MAX + 50),
        "web_url": long_url,
        "references": {"full": "p" * (gl.GROUP_ROW_PATH_MAX + 1) + "!1"},
        "author": {"username": "u" * (gl.GROUP_ROW_NAME_MAX + 1)},
        "updated_at": "9" * (gl.GROUP_ROW_TIMESTAMP_MAX + 1),
    }
    route([("groups/", [raw])])
    row = gl.search_group_reviews("acme", host="gitlab.com", reviewer="me")[0]
    assert len(row["title"]) == gl.GROUP_ROW_TITLE_MAX
    assert row["url"] == ""
    assert len(row["project"]) == gl.GROUP_ROW_PATH_MAX
    assert len(row["author"]) == gl.GROUP_ROW_NAME_MAX
    assert len(row["updated_at"]) == gl.GROUP_ROW_TIMESTAMP_MAX
    assert row["truncated_fields"] == ["title", "url", "project", "author", "updated_at"]
    assert "body" not in row and "labels" not in row


def test_group_search_pagination_is_capped(route):
    page = [{**MR, "iid": n} for n in range(gl._PAGE_SIZE)]
    router = route([("groups/", page)])
    rows = gl.search_group_reviews(
        "acme", host="gitlab.com", reviewer="me", limit=gl.PR_SEARCH_MAX + 1
    )
    assert len(router.calls) == gl.GROUP_SEARCH_MAX_PAGES
    assert gl.GROUP_SEARCH_MAX_PAGES * gl._PAGE_SIZE >= gl.PR_SEARCH_MAX + 1
    assert len(rows) == gl.PR_SEARCH_MAX + 1


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "acme//web",
        "../acme",
        "acme/../other",
        "acme web",
        "acme%2Fweb",
        "acme?scope=all",
        "acme/-/web",
        "groups/acme",
        "api",
        "/".join(["g"] * 30),
    ],
)
def test_a_bad_group_path_is_refused_before_any_call(route, path):
    router = route([("groups/", [MR])])
    with pytest.raises(RepoUrlError):
        gl.search_group_reviews(path, host="gitlab.com", reviewer="me")
    assert router.calls == []


# ── parsing what the user types ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        ("acme", ("gitlab.com", "acme")),
        ("acme/web/api-team", ("gitlab.com", "acme/web/api-team")),
        ("/acme/web/", ("gitlab.com", "acme/web")),
        ("https://gitlab.com/acme/web", ("gitlab.com", "acme/web")),
        ("https://gitlab.com/groups/acme/web/-/merge_requests", ("gitlab.com", "acme/web")),
        ("https://www.gitlab.com/acme/", ("gitlab.com", "acme")),
    ],
)
def test_parse_gitlab_group_accepts_links_and_paths(value, expected):
    assert gl.parse_gitlab_group(value) == expected


def test_a_self_managed_group_comes_in_through_its_link():
    allowed = frozenset({"gitlab.acme.example"})
    assert gl.parse_gitlab_group("https://gitlab.acme.example/acme/web", allowed_hosts=allowed) == (
        "gitlab.acme.example",
        "acme/web",
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/acme",
        "http://gitlab.com/acme",
        "https://user:pw@gitlab.com/acme",
        "https://gitlab.com/groups",
        "   ",
    ],
)
def test_parse_gitlab_group_refuses_unusable_input(value):
    with pytest.raises(RepoUrlError):
        gl.parse_gitlab_group(value, allowed_hosts=frozenset({"gitlab.acme.example"}))


def test_verify_group_access_reads_the_group_once(route):
    router = route([("groups/", {"full_path": "acme/web"})])
    gl.verify_group_access("acme/web", host="gitlab.com")
    assert router.calls == ["groups/acme%2Fweb?with_projects=false"]


def test_verify_group_access_raises_not_found_for_an_unknown_group(route):
    route([("groups/", NOT_FOUND)])
    with pytest.raises(gl.ProviderNotFoundError):
        gl.verify_group_access("acme/nope", host="gitlab.com")


def test_a_404_in_the_path_is_not_read_as_not_found(route):
    route([("groups/", (1, "GET groups/team404: HTTP 500 Internal Server Error"))])
    with pytest.raises(gl.ProviderCliError) as caught:
        gl.verify_group_access("team404", host="gitlab.com")
    assert not isinstance(caught.value, gl.ProviderNotFoundError)


# ── store ────────────────────────────────────────────────────────────────────


def test_no_group_leaves_the_config_byte_for_byte(tmp_path):
    root = tmp_path / "cfg"
    store.add_connected_repo("acme", "api", provider="gitlab", host="gitlab.com", root=root)
    before = store.config_path(root).read_bytes()
    repos_before = store.list_connected_repos(root)

    assert store.list_gitlab_groups(root) == []
    assert not store.is_gitlab_group_added("gitlab.com", "acme", root)
    assert store.remove_gitlab_group("gitlab.com", "acme", root) is False
    assert store.config_path(root).read_bytes() == before

    assert store.add_gitlab_group("gitlab.com", "acme/web", root) is True
    assert store.list_connected_repos(root) == repos_before
    assert store.remove_gitlab_group("gitlab.com", "acme/web", root) is True
    assert store.config_path(root).read_bytes() == before


def test_a_group_is_stored_as_host_and_path_only(tmp_path):
    root = tmp_path / "cfg"
    assert store.add_gitlab_group("gitlab.com", "acme/web", root) is True
    assert store.add_gitlab_group("gitlab.com", "acme/web", root) is False
    store.add_gitlab_group("gitlab.acme.example", "acme/web", root)
    raw = json.loads(store.config_path(root).read_text(encoding="utf-8"))
    assert raw[store.GITLAB_GROUPS_KEY] == [
        {"host": "gitlab.com", "path": "acme/web"},
        {"host": "gitlab.acme.example", "path": "acme/web"},
    ]
    assert store.is_gitlab_group_added("gitlab.com", "acme/web", root)
    assert not store.is_gitlab_group_added("gitlab.com", "Acme/Web", root)
    assert not store.is_gitlab_group_added("gitlab.com", "acme", root)


def test_malformed_group_rows_are_ignored(tmp_path):
    root = tmp_path / "cfg"
    rows = [
        {"host": "gitlab.com"},
        "x",
        {"host": 1, "path": "a"},
        {"host": "gitlab.com", "path": "ok"},
    ]
    store.write_config({"repos": [], store.GITLAB_GROUPS_KEY: rows}, root)
    assert store.list_gitlab_groups(root) == [{"host": "gitlab.com", "path": "ok"}]


# ── routes ───────────────────────────────────────────────────────────────────


def _get(path: str, query: dict) -> web.Request:
    return make_mocked_request("GET", f"{BASE}/{path}?{urlencode(query)}")


def _owner_request(
    method: str, path: str, *, query: dict | None = None, body: object = None
) -> web.Request:
    full = f"{BASE}/{path}" + (f"?{urlencode(query)}" if query else "")
    req = make_mocked_request(method, full)
    req.app["state"] = NoConfiguredOwner()
    req["user"] = "local-app"
    req["app"] = ""
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


async def _reviews(host: str, path: str) -> web.Response:
    return await routes._handle_gitlab_group_reviews(
        _get("gitlab-groups/review-requests", {"host": host, "path": path})
    )


@pytest.mark.asyncio
async def test_no_group_followed_means_no_glab_call(route):
    router = route([("", [MR])])
    resp = await _reviews("gitlab.com", "acme")
    assert resp.status == 404
    assert _body(resp)["code"] == "group_not_added"
    assert router.calls == []
    listed = _body(await routes._handle_gitlab_groups(_get("gitlab-groups", {})))
    assert listed == {"groups": []}


@pytest.mark.asyncio
async def test_a_bad_path_is_a_400_with_no_glab_call(route):
    router = route([("", [MR])])
    store.add_gitlab_group("gitlab.com", "acme")
    resp = await _reviews("gitlab.com", "acme/../x")
    assert resp.status == 400
    assert _body(resp)["code"] == "invalid_group"
    assert router.calls == []


@pytest.mark.asyncio
async def test_review_requests_for_a_followed_group(route):
    router = route([("groups/", [MR]), ("user", {"username": "me"})])
    store.add_gitlab_group("gitlab.com", "acme/web")
    resp = await _reviews("gitlab.com", "acme/web")
    assert resp.status == 200
    assert _body(resp) == {
        "host": "gitlab.com",
        "path": "acme/web",
        "login": "me",
        "pulls": [ROW],
        "truncated": False,
        "limit": gl.PR_SEARCH_MAX,
    }
    searches = [c for c in router.calls if c.startswith("groups/")]
    assert len(searches) == 1 and "reviewer_username=me" in searches[0]


@pytest.mark.asyncio
async def test_review_requests_report_the_cap(route, monkeypatch):
    monkeypatch.setattr(gl, "PR_SEARCH_MAX", 2)
    route([("groups/", [{**MR, "iid": n} for n in range(5)]), ("user", {"username": "me"})])
    store.add_gitlab_group("gitlab.com", "acme")
    body = _body(await _reviews("gitlab.com", "acme"))
    assert body["truncated"] is True
    assert len(body["pulls"]) == 2


@pytest.mark.asyncio
async def test_an_unknown_group_on_read_is_a_per_group_404(route):
    route([("groups/", NOT_FOUND), ("user", {"username": "me"})])
    store.add_gitlab_group("gitlab.com", "acme")
    resp = await _reviews("gitlab.com", "acme")
    assert resp.status == 404
    assert _body(resp)["code"] == "group_not_found"


@pytest.mark.asyncio
async def test_an_outage_on_a_group_named_with_404_is_a_502(route):
    route([("groups/", (1, "HTTP 500 Internal Server Error")), ("user", {"username": "dev404"})])
    store.add_gitlab_group("gitlab.com", "team404")
    resp = await _reviews("gitlab.com", "team404")
    assert resp.status == 502
    assert _body(resp)["code"] == "provider_error"


@pytest.mark.asyncio
async def test_a_glab_auth_failure_asks_for_setup(route):
    route([("user", (1, "glab auth login required"))])
    store.add_gitlab_group("gitlab.com", "acme")
    resp = await _reviews("gitlab.com", "acme")
    assert resp.status == 502
    assert _body(resp)["code"] == "setup_required"


@pytest.mark.asyncio
async def test_no_username_is_its_own_error(route):
    route([("user", {})])
    store.add_gitlab_group("gitlab.com", "acme")
    resp = await _reviews("gitlab.com", "acme")
    assert resp.status == 502
    assert _body(resp)["code"] == "no_login"


@pytest.mark.asyncio
async def test_follow_stores_a_verified_group(route):
    router = route([("groups/", {"full_path": "acme/web"})])
    resp = await routes._handle_add_gitlab_group(
        _owner_request("POST", "gitlab-groups", body={"group": "https://gitlab.com/acme/web"})
    )
    assert resp.status == 200
    assert _body(resp) == {"host": "gitlab.com", "path": "acme/web"}
    assert store.list_gitlab_groups() == [{"host": "gitlab.com", "path": "acme/web"}]
    assert router.calls == ["groups/acme%2Fweb?with_projects=false"]


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["acme/../x", "groups/acme", "", 7])
async def test_follow_refuses_a_bad_path_and_stores_nothing(route, group):
    router = route([("", {"full_path": "x"})])
    resp = await routes._handle_add_gitlab_group(
        _owner_request("POST", "gitlab-groups", body={"group": group})
    )
    assert resp.status == 400
    assert router.calls == []
    assert store.list_gitlab_groups() == []


@pytest.mark.asyncio
async def test_follow_refuses_an_unknown_group_and_stores_nothing(route):
    route([("groups/", NOT_FOUND)])
    resp = await routes._handle_add_gitlab_group(
        _owner_request("POST", "gitlab-groups", body={"group": "acme/nope"})
    )
    assert resp.status == 404
    assert _body(resp)["code"] == "group_not_found"
    assert store.list_gitlab_groups() == []


@pytest.mark.asyncio
async def test_unfollow_drops_only_the_named_group():
    store.add_gitlab_group("gitlab.com", "acme")
    store.add_gitlab_group("gitlab.com", "other")
    missing = await routes._handle_remove_gitlab_group(
        _owner_request("DELETE", "gitlab-groups", query={"host": "gitlab.com", "path": "nope"})
    )
    assert missing.status == 404
    resp = await routes._handle_remove_gitlab_group(
        _owner_request("DELETE", "gitlab-groups", query={"host": "gitlab.com", "path": "acme"})
    )
    assert resp.status == 200
    assert store.list_gitlab_groups() == [{"host": "gitlab.com", "path": "other"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_a_non_owner_cannot_follow_or_unfollow(route, method):
    router = route([("", {"full_path": "acme"})])
    store.add_gitlab_group("gitlab.com", "acme")
    req = make_mocked_request(
        method, f"{BASE}/gitlab-groups?{urlencode({'host': 'gitlab.com', 'path': 'acme'})}"
    )
    req.app["state"] = SimpleNamespace(owner_id="owner-user")
    req["user"] = "someone-else"
    req["app"] = ""
    req.json = AsyncMock(return_value={"group": "other"})  # type: ignore[method-assign]
    handler = (
        routes._handle_add_gitlab_group if method == "POST" else routes._handle_remove_gitlab_group
    )
    resp = await handler(req)
    assert resp.status == 403
    assert _body(resp)["code"] == "owner_only"
    assert router.calls == []
    assert store.list_gitlab_groups() == [{"host": "gitlab.com", "path": "acme"}]
