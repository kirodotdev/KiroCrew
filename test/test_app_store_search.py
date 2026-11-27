"""The App Store search an agent runs: ``app_store_search`` and ``GET /api/app-store/search``.

The gateway half searches the listing the store renders (the published catalog, or the
seed offline) and answers each match's name, display copy, use cases (where the row has
them) and install state. The tool half forwards the call to that one read-only route and
nothing else.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import mcp_core, mcp_tools
from kiro_crew.apps import routes
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.mcp_tools import app_store
from kiro_crew.security import REDACTED_CREDENTIAL_TAG
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.validation import MAX_RESPONSE_LEN, ValidationError, sanitize_string

#: A store listing as ``list_catalog_apps`` dresses published-catalog rows: no
#: ``useCases`` (``official_catalog.list_catalog_rows`` maps none), install status stamped.
_CATALOG = [
    {
        "name": "pptx-maker",
        "displayName": "PPTX Maker",
        "description": "Turn notes into a slide deck.",
        "tags": ["slides"],
        "source": {"type": "git"},
        "iconUrl": "https://example.invalid/icon.png",
        "installed": True,
    },
    {
        "name": "meetings",
        "displayName": "Meetings",
        "description": "Take notes in meetings and turn them into tasks.",
        "source": {"type": "git"},
        "installed": False,
    },
    {
        "name": "papyrus",
        "displayName": "Papyrus",
        "description": "Write long documents.",
        "source": {"type": "git"},
        "installed": False,
    },
]


def _body(resp: Any) -> dict[str, Any]:
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture()
def _listing(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The catalog listing above; records which listing the search read."""
    read: list[str] = []

    async def _catalog() -> list[dict[str, Any]]:
        read.append("catalog")
        return [dict(row) for row in _CATALOG]

    async def _seed() -> list[dict[str, Any]]:
        read.append("seed")
        return []

    monkeypatch.setattr(routes, "list_catalog_apps", _catalog)
    monkeypatch.setattr(routes, "list_registry", _seed)
    monkeypatch.setattr(routes, "list_apps", lambda: [])
    return read


async def _search(query: str) -> Any:
    return await routes.handle_app_store_search(
        make_mocked_request("GET", f"/api/app-store/search?{query}")
    )


@pytest.mark.asyncio
async def test_the_search_answers_the_matching_apps_with_their_install_state(_listing) -> None:
    resp = await _search("q=notes+slides")
    assert resp.status == 200
    # pptx-maker matches both words (one in its tags), meetings one; papyrus neither.
    assert _body(resp) == {
        "apps": [
            {
                "name": "pptx-maker",
                "displayName": "PPTX Maker",
                "description": "Turn notes into a slide deck.",
                "installed": True,
            },
            {
                "name": "meetings",
                "displayName": "Meetings",
                "description": "Take notes in meetings and turn them into tasks.",
                "installed": False,
            },
        ],
        "total": 2,
        "catalogUnavailable": False,
    }
    assert _listing == ["catalog"]


@pytest.mark.asyncio
async def test_the_search_runs_off_the_event_loop(_listing, monkeypatch) -> None:
    # A padded app.json makes the word match slow; on the loop it would stall the gateway.
    real = routes.search_store_rows
    threads: list[int] = []

    def _spy(*args: Any) -> Any:
        threads.append(threading.get_ident())
        return real(*args)

    monkeypatch.setattr(routes, "search_store_rows", _spy)
    resp = await _search("q=notes")
    assert resp.status == 200
    assert len(threads) == 1
    assert threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_a_word_no_app_carries_matches_nothing(_listing) -> None:
    resp = await _search("q=teleport")
    assert _body(resp) == {"apps": [], "total": 0, "catalogUnavailable": False}


@pytest.mark.asyncio
async def test_the_limit_cuts_the_answer_and_the_total_counts_every_match(_listing) -> None:
    body = _body(await _search("q=notes&limit=1"))
    assert [app["name"] for app in body["apps"]] == ["meetings"]
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_offline_the_seed_listing_is_searched_and_said_so(monkeypatch) -> None:
    async def _no_catalog() -> list[dict[str, Any]]:
        return []

    async def _seed() -> list[dict[str, Any]]:
        # A seed row is dressed from the app's own app.json, so it can carry use cases.
        return [
            {
                "name": "papyrus",
                "description": "Write long documents.",
                "useCases": ["Draft a design doc"],
                "installed": False,
            }
        ]

    monkeypatch.setattr(routes, "list_catalog_apps", _no_catalog)
    monkeypatch.setattr(routes, "list_registry", _seed)
    monkeypatch.setattr(routes, "list_apps", lambda: [])
    body = _body(await _search("q=design"))
    assert body == {
        "apps": [
            {
                "name": "papyrus",
                "description": "Write long documents.",
                "useCases": ["Draft a design doc"],
                "installed": False,
            }
        ],
        "total": 1,
        "catalogUnavailable": True,
    }


@pytest.mark.asyncio
async def test_keywords_match_word_starts_whatever_the_punctuation(_listing) -> None:
    # "slide," still finds the "slide deck" row; "ask" sits inside "tasks" but starts no word.
    body = _body(await _search("q=slide,+ask"))
    assert [app["name"] for app in body["apps"]] == ["pptx-maker"]


@pytest.mark.asyncio
async def test_the_search_leaves_out_what_discover_hides(monkeypatch) -> None:
    async def _catalog() -> list[dict[str, Any]]:
        return [
            # Installed, but its manifest hides it: Discover never shows it.
            {
                "name": "workflows",
                "description": "Run agent workflows.",
                "installed": True,
                "source": {"type": "builtin"},
            },
            # A built-in row nothing is installed under: Discover cannot offer it.
            {
                "name": "board",
                "description": "Agent task board.",
                "installed": False,
                "source": {"type": "builtin"},
            },
            {
                "name": "tasks",
                "description": "Agent task list.",
                "installed": True,
                "source": {"type": "builtin"},
            },
        ]

    installed = [
        {"name": "workflows", "origin": "builtin", "manifest": {"hidden": True}},
        {"name": "tasks", "origin": "builtin", "manifest": {}},
    ]
    monkeypatch.setattr(routes, "list_catalog_apps", _catalog)
    monkeypatch.setattr(routes, "list_apps", lambda: installed)
    body = _body(await _search("q=agent"))
    assert [app["name"] for app in body["apps"]] == ["tasks"]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_padded_publisher_text_is_cut_and_the_totals_come_first(monkeypatch) -> None:
    async def _catalog() -> list[dict[str, Any]]:
        return [
            {
                "name": "padded",
                "description": "notes " * 20_000,
                "installed": False,
                "useCases": ["notes " * 100] * 50,
                "source": {"type": "git"},
            },
            # Text that is not a string is not cut, so it is left out instead.
            {"name": "shaped", "description": {"x": "notes " * 20_000}, "tags": ["notes"]},
        ]

    monkeypatch.setattr(routes, "list_catalog_apps", _catalog)
    monkeypatch.setattr(routes, "list_apps", lambda: [])
    resp = await _search("q=notes")
    body = _body(resp)
    app, shaped = body["apps"]
    assert len(app["description"]) <= routes._STORE_SEARCH_TEXT_MAX + len("...")
    assert len(app["useCases"]) == routes._STORE_SEARCH_USE_CASES_MAX
    assert all(len(u) <= routes._STORE_SEARCH_TEXT_MAX + len("...") for u in app["useCases"])
    assert shaped == {"name": "shaped"}
    assert list(body) == ["total", "catalogUnavailable", "apps"]


def test_a_credential_across_the_cut_is_redacted_before_it() -> None:
    # A cut through the token leaves 39 of its characters, which the detector does not match.
    token = github_classic()
    filler = "notes " + "x" * 194 + " "
    row = {"name": "leaky", "description": filler + token, "installed": False}
    (app,), _ = routes.search_store_rows([row], "notes", 10)
    assert token[:12] not in app["description"]
    assert app["description"] == filler + REDACTED_CREDENTIAL_TAG


def test_the_answer_stays_json_under_the_response_cap(monkeypatch) -> None:
    # 50 matches at the cut length, in text that JSON escaping doubles and NFC
    # normalization (applied on the way out) triples.
    text = ('"' + "\U0001d160") * 120
    apps = [
        {"name": f"app-{i:02d}", "displayName": text, "description": text, "useCases": [text] * 5}
        for i in range(50)
    ]
    answer = {"total": 50, "catalogUnavailable": False, "apps": apps}
    monkeypatch.setattr(mcp_core, "_get", lambda *_a, **_k: json.loads(json.dumps(answer)))
    out = mcp_tools.dispatch("app_store_search", {"query": "x"})
    assert len(sanitize_string(out)) <= MAX_RESPONSE_LEN
    body = json.loads(out)
    assert body["total"] == 50
    assert 0 < len(body["apps"]) < 50


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["q=", "q=+", "q=notes&limit=two"])
async def test_a_missing_query_or_a_bad_limit_is_refused(_listing, query: str) -> None:
    resp = await _search(query)
    assert resp.status == 400
    assert _listing == []


def test_the_route_is_a_strict_internal_path() -> None:
    assert "/api/app-store/search" in _STRICT_INTERNAL_API_PATHS


def test_the_tool_is_listed_and_requires_a_query() -> None:
    assert "app_store_search" in {tool["name"] for tool in mcp_tools.build_tool_list()}
    with pytest.raises(ValidationError):
        mcp_core._validate_args("app_store_search", {})


def test_the_tool_only_reads_the_search_route(monkeypatch) -> None:
    gets: list[tuple[str, float]] = []

    def _get(path: str, session_key: str | None = None, *, timeout: float = 10) -> dict:
        gets.append((path, timeout))
        return {"apps": [{"name": "pptx-maker", "installed": True}], "total": 1}

    def _no_write(*_args: Any, **_kwargs: Any) -> dict:
        raise AssertionError("app_store_search must not write to the gateway")

    monkeypatch.setattr(mcp_core, "_get", _get)
    monkeypatch.setattr(mcp_core, "_post", _no_write)
    out = mcp_tools.dispatch("app_store_search", {"query": "slide deck", "limit": 3})
    assert json.loads(out) == {"apps": [{"name": "pptx-maker", "installed": True}], "total": 1}
    assert gets == [("/api/app-store/search?q=slide+deck&limit=3", app_store._SEARCH_TIMEOUT_S)]


def test_a_gateway_failure_reads_as_an_error(monkeypatch) -> None:
    monkeypatch.setattr(mcp_core, "_get", lambda *_a, **_k: {"error": "q is required"})
    out = mcp_tools.dispatch("app_store_search", {"query": "x"})
    assert out == "Error: app_store_search failed: q is required"
