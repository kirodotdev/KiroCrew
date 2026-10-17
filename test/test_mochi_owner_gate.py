"""Owner gate on mochi's mutating routes.

Every POST/DELETE handler in ``mochi/backend/routes.py`` refuses a non-owner
dashboard subject with the shared 403 ``owner_only`` before it reads the body
or touches the runtime. The owner and mochi's own app token reach the handler.
GET routes are not gated.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import NoConfiguredOwner

from kiro_crew.apps.builtins.mochi import agent_policy, hooks
from kiro_crew.apps.builtins.mochi.backend import routes

OWNER = "owner-1"
BASE = "/api/apps/mochi"
_MISSING = object()


class _ConfiguredOwner:
    owner_id = OWNER


def _req(
    method: str,
    path: str,
    payload: object = None,
    *,
    user: str,
    app: object = "",
    state: object = None,
    match_info: dict[str, str] | None = None,
):
    request = make_mocked_request(method, path, match_info=match_info or {})
    request.app["state"] = state if state is not None else _ConfiguredOwner()
    request["user"] = user
    if app is not _MISSING:
        request["app"] = app

    async def _json(*_a: object, **_k: object) -> object:
        return payload

    request.json = _json  # type: ignore[method-assign]
    return request


def _refused(resp: Any) -> bool:
    return resp.status == 403 and json.loads(resp.text or "{}").get("code") == "owner_only"


@pytest.fixture(autouse=True)
def _enabled():
    with (
        mock.patch.object(routes, "is_app_enabled", return_value=True),
        mock.patch("kiro_crew.sel.sel"),
        # No test here may reach petdex.dev: the owner rows pass the gate and
        # would otherwise run the real outbound fetch.
        mock.patch.object(
            routes, "fetch_pet", mock.AsyncMock(side_effect=routes.PetdexError("offline"))
        ),
        # Nor may one read the host's MCP config or spawn a configured server.
        mock.patch.object(routes, "list_servers", lambda: []),
        mock.patch.object(routes, "probe_server", mock.AsyncMock()),
    ):
        yield


class _Recorder:
    """Stand-in runtime: any attribute read means the handler ran past the gate."""

    def __init__(self, log: list[str], name: str = "rt") -> None:
        self._log = log
        self._name = name

    def __getattr__(self, attr: str) -> "_Recorder":
        self._log.append(f"{self._name}.{attr}")
        return _Recorder(self._log, f"{self._name}.{attr}")

    def __call__(self, *_a: object, **_k: object) -> "_Recorder":
        self._log.append(f"{self._name}()")
        return self

    def __truediv__(self, other: object) -> "_Recorder":
        self._log.append(f"{self._name}/{other}")
        return self


#: (id, handler name, method, path, body, match_info) for every mutating route.
_MUTATING = [
    (
        "watchlist/update",
        "_handle_watchlist_update",
        "POST",
        "/watchlist/update",
        {"add": []},
        None,
    ),
    (
        "watchlist/clear-completed",
        "_handle_watchlist_clear_completed",
        "POST",
        "/watchlist/clear-completed",
        {},
        None,
    ),
    ("reset", "_handle_reset", "POST", "/reset", {}, None),
    ("packs save", "_handle_pack_save", "POST", "/packs", {"id": "p"}, None),
    ("packs/content", "_handle_pack_save_content", "POST", "/packs/content", {}, None),
    ("packs/import", "_handle_pack_import", "POST", "/packs/import", None, None),
    ("packs delete", "_handle_pack_delete", "DELETE", "/packs/p", None, {"pack_id": "p"}),
    ("pinned/unpin", "_handle_pinned_unpin", "POST", "/pinned/unpin", {"path": "/x"}, None),
    (
        "pinned/mark-seen",
        "_handle_pinned_mark_seen",
        "POST",
        "/pinned/mark-seen",
        {"path": "/x"},
        None,
    ),
    ("petdex/import", "_handle_petdex_import", "POST", "/petdex/import", {"slug": "x"}, None),
    ("presence", "_handle_presence", "POST", "/presence", {"visible": True}, None),
    ("quiet", "_handle_quiet", "POST", "/quiet", {"minutes": 1440}, None),
    ("pet-event", "_handle_pet_event", "POST", "/pet-event", {"event": "error"}, None),
    ("walk-done", "_handle_walk_done", "POST", "/walk-done", {}, None),
    ("walk-distance", "_handle_walk_distance", "POST", "/walk-distance", {"pixels": 64}, None),
    ("peeking", "_handle_peeking", "POST", "/peeking", {"peeking": True}, None),
    ("stat", "_handle_stat", "POST", "/stat", {"kind": "drag"}, None),
    ("displays", "_handle_displays", "POST", "/displays", {"displays": []}, None),
    ("settings", "_handle_settings_update", "POST", "/settings", {}, None),
    ("mcp-tools", "_handle_mcp_tools_probe", "POST", "/mcp-tools/srv", None, {"name": "srv"}),
]


async def _drive(row, **claims) -> tuple[Any, list[str]]:
    _rid, name, method, path, body, mi = row
    log: list[str] = []
    handler = routes._require_enabled(getattr(routes, name))
    with mock.patch.object(hooks, "_runtime", _Recorder(log)):
        try:
            resp = await handler(_req(method, BASE + path, body, match_info=mi, **claims))
        except Exception:  # noqa: BLE001 - the stand-in runtime is not a real one
            resp = None
    return resp, log


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _MUTATING, ids=[r[0] for r in _MUTATING])
async def test_non_owner_dashboard_subject_is_refused_before_any_work(row):
    resp, log = await _drive(row, user="mallory")
    assert resp is not None and _refused(resp), resp and resp.status
    assert log == []


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _MUTATING, ids=[r[0] for r in _MUTATING])
async def test_missing_app_claim_is_refused(row):
    resp, log = await _drive(row, user=OWNER, app=_MISSING)
    assert resp is not None and _refused(resp), resp and resp.status
    assert log == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"user": OWNER}, id="configured-owner"),
        pytest.param({"user": "local-app", "state": NoConfiguredOwner()}, id="no-configured-owner"),
        pytest.param({"user": "mochi", "app": "mochi"}, id="own-app-token"),
    ],
)
@pytest.mark.parametrize("row", _MUTATING, ids=[r[0] for r in _MUTATING])
async def test_owner_and_own_app_token_pass_the_gate(row, claims):
    resp, log = await _drive(row, **claims)
    assert resp is None or not _refused(resp)
    # The handler went on to use the runtime or answered from its own validation.
    assert log or resp is not None


@pytest.mark.asyncio
async def test_bootstrap_subject_is_refused_once_an_owner_is_configured():
    resp, log = await _drive(_MUTATING[0], user="local-app")
    assert resp is not None and resp.status in (401, 403)
    assert log == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"user": OWNER}, id="owner"),
        pytest.param({"user": "mochi", "app": "mochi"}, id="own-app-token"),
    ],
)
async def test_settings_grant_written_for_owner_and_own_app(tmp_path, monkeypatch, claims):
    monkeypatch.setattr(agent_policy, "_ambient_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.apps.bridges.refresh_app_agents", lambda _n: [])
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.get_app_manifest", lambda _n: SimpleNamespace(agents=[])
    )
    monkeypatch.setattr(hooks, "_runtime", SimpleNamespace(data_dir=tmp_path))
    body = {"extraMcpServers": [{"name": "kirocrew-core", "agents": ["bg"]}]}

    resp = await routes._handle_settings_update(_req("POST", f"{BASE}/settings", body, **claims))

    assert resp.status == 200, resp.text
    assert (tmp_path / agent_policy.POLICY_FILENAME).exists()


@pytest.mark.asyncio
async def test_settings_grant_not_written_for_non_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_runtime", SimpleNamespace(data_dir=tmp_path))
    body = {"extraMcpServers": [{"name": "kirocrew-core", "agents": ["bg"]}]}

    resp = await routes._handle_settings_update(
        _req("POST", f"{BASE}/settings", body, user="mallory")
    )

    assert _refused(resp)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims,spawned",
    [
        pytest.param({"user": "mallory"}, 0, id="non-owner"),
        pytest.param({"user": OWNER, "app": _MISSING}, 0, id="missing-app-claim"),
        pytest.param({"user": OWNER}, 1, id="owner"),
        pytest.param({"user": "mochi", "app": "mochi"}, 1, id="own-app-token"),
    ],
)
async def test_mcp_probe_spawns_only_for_owner_or_own_app(tmp_path, monkeypatch, claims, spawned):
    server = SimpleNamespace(name="srv", disabled=False, tools=[])
    probe = mock.AsyncMock(return_value=SimpleNamespace(name="srv", tools=["t"], status="ok"))
    monkeypatch.setattr(routes, "list_servers", lambda: [server])
    monkeypatch.setattr(routes, "_mcp_effectively_disabled", lambda _n, _s: False)
    monkeypatch.setattr(routes, "probe_server", probe)
    monkeypatch.setattr(hooks, "_runtime", SimpleNamespace(data_dir=tmp_path))

    resp = await routes._require_enabled(routes._handle_mcp_tools_probe)(
        _req("POST", f"{BASE}/mcp-tools/srv", match_info={"name": "srv"}, **claims)
    )

    assert probe.await_count == spawned
    assert resp.status == (200 if spawned else 403)


@pytest.mark.asyncio
async def test_watchlist_add_starts_no_turn_for_non_owner():
    rt = mock.MagicMock()
    with mock.patch.object(hooks, "_runtime", rt):
        resp = await routes._require_enabled(routes._handle_watchlist_update)(
            _req(
                "POST",
                f"{BASE}/watchlist/update",
                {"add": [{"label": "x", "kind": "url", "target": "https://example.invalid"}]},
                user="mallory",
            )
        )
    assert _refused(resp)
    assert rt.mock_calls == []


@pytest.mark.asyncio
async def test_get_routes_stay_open_to_a_non_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_runtime", SimpleNamespace(data_dir=tmp_path))
    resp = await routes._handle_settings_get(_req("GET", f"{BASE}/settings", user="mallory"))
    assert resp.status == 200
