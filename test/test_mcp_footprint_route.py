"""``GET /api/mcp-gateway/footprint``: who may read it, and what it says.

The route is registered beside the other ``/api/mcp-gateway`` routes and sits
behind the same dashboard credential gate; on top of that it is owner only,
because the reading names the owner's chats. Caller rows:

* no credential: refused by ``token_auth`` (401/403) before the handler runs;
* an app's credential: refused by ``token_auth``'s app scope (no manifest grants
  the path) and by the owner gate;
* an internal (agent-tool) caller, which ``token_auth`` leaves with no app claim:
  403 ``owner_only``;
* a non-owner dashboard subject: 403 ``owner_only``;
* the owner: 200, numbers and chat slot names only.

And the reading counts servers declared only in ``settings/mcp.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.handlers import gateway_footprint as mcp_footprint
from kiro_crew.mcp_gateway import footprint

OWNER = "owner-subject"
PATH = "/api/mcp-gateway/footprint"


def _state() -> SimpleNamespace:
    slot = SimpleNamespace(key="chat-1", linked_session_key="")
    return SimpleNamespace(
        owner_id=OWNER,
        broadcast_ws=lambda *a, **k: None,
        sessions=SimpleNamespace(runtime_pids=lambda: [{"key": "dashboard:chat-1", "pid": 4242}]),
        _slots={"chat-1": slot},
        _mcp_gateway_manager=None,
    )


def _routes(app: web.Application) -> None:
    from kiro_crew.dashboard.routes import agent_config

    agent_config.register(app)


def _claims_app(*, user: str, app_claim: str | None) -> web.Application:
    """The real route table, with the claims ``token_auth`` would publish."""

    @web.middleware
    async def _claims(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = user
        if app_claim is not None:
            request["app"] = app_claim
        return await handler(request)

    app = web.Application(middlewares=[_claims])
    app["state"] = _state()
    _routes(app)
    return app


@pytest.fixture
def reading(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fixed reading, so the owner row tests the payload, not this host."""
    seen: dict[str, Any] = {}

    def _measure(sessions: Any, specs: Any, backends: Any = ()) -> footprint.Footprint:
        seen["sessions"] = list(sessions)
        return footprint.Footprint(
            using_now_bytes=5 * 1024**3,
            with_sharing_bytes=2 * 1024**3,
            servers=3,
            stale_session_keys=["dashboard:chat-1"],
        )

    monkeypatch.setattr(footprint, "measure", _measure)
    monkeypatch.setattr(mcp_footprint, "_specs", lambda enabled, refused, routed=frozenset(): {})
    monkeypatch.setattr(mcp_footprint, "refused_servers", dict)
    return seen


async def _get(app: web.Application, query: dict[str, str] | None = None) -> tuple[int, Any]:
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(PATH, params=query or {})
        text = await resp.text()
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        return resp.status, body


@pytest.mark.asyncio
async def test_no_credential_is_refused_before_the_handler(
    reading: dict[str, Any],
) -> None:
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = _state()
    _routes(app)
    status, _ = await _get(app)
    assert status in (401, 403)
    assert "sessions" not in reading


@pytest.mark.asyncio
async def test_an_app_credential_is_refused_by_the_app_scope(
    reading: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(token_auth, "_app_api_allowlist", lambda name: ())
    assert not token_auth.app_token_path_allowed("some-app", PATH)
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = _state()
    _routes(app)
    credential = token_auth.generate_token("some-app", ttl_seconds=300, app="some-app")
    status, _ = await _get(app, {"token": credential})
    assert status == 403
    assert "sessions" not in reading


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "app_claim"),
    [
        ("channel-user", ""),  # a non-owner dashboard subject
        (OWNER, None),  # an internal caller: no app claim at all
        (OWNER, "some-app"),  # an app claim, even for the owner's own subject
    ],
    ids=["non-owner", "internal-caller", "app-claim"],
)
async def test_anyone_but_the_owner_is_refused(
    reading: dict[str, Any], user: str, app_claim: str | None
) -> None:
    status, body = await _get(_claims_app(user=user, app_claim=app_claim))
    assert status == 403
    assert body.get("code") == "owner_only"
    assert "sessions" not in reading


@pytest.mark.asyncio
async def test_the_owner_gets_numbers_and_chat_names_only(
    reading: dict[str, Any],
) -> None:
    status, body = await _get(_claims_app(user=OWNER, app_claim=""))
    assert status == 200, body
    assert set(body) == {
        "using_now_bytes",
        "with_sharing_bytes",
        "open_chats",
        "stale_chats",
    }
    assert body["using_now_bytes"] == 5 * 1024**3
    assert body["with_sharing_bytes"] == 2 * 1024**3
    assert body["open_chats"] == 1
    assert body["stale_chats"] == ["chat-1"]
    # Nothing a command line, env or path could be spelled in.
    text = json.dumps(body)
    assert "/" not in text and "=" not in text
    assert reading["sessions"] == [("dashboard:chat-1", 4242)]


def test_settings_servers_are_read_with_the_rewriters_filter(tmp_path: Path) -> None:
    settings = tmp_path / "mcp.json"
    settings.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local-creds-agent-mcp": {
                        "command": "aim",
                        "args": ["mcp", "start-server", "local-creds-agent-mcp"],
                    },
                    "muted-one": {"command": "/opt/m", "disabled": True},
                    "remote-one": {"url": "https://example.invalid/mcp"},
                    "env-one": {
                        "command": "/opt/e",
                        "env": {"API_KEY": "secret-value"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    rows = mcp_footprint._settings_rows(settings)
    assert sorted(rows) == ["env-one", "local-creds-agent-mcp"]
    assert rows["local-creds-agent-mcp"]["launches"] == {
        ("aim", "mcp", "start-server", "local-creds-agent-mcp")
    }
    # Env NAMES only: the value never leaves the file.
    assert rows["env-one"]["env_names"] == {"API_KEY"}
    assert "secret-value" not in repr(rows)


def test_a_settings_only_server_counts_on_both_sides_when_it_cannot_share(
    tmp_path: Path,
) -> None:
    """The undercount fix: a settings-only, unshareable server adds the same
    bytes to now and to with-sharing, so the saving shown does not move."""

    def proc(pid: int, ppid: int, argv: list[str], pss_kb: int) -> None:
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_bytes(f"{pid} (x) S {ppid} 0 0 0".encode())
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "smaps_rollup").write_text(f"Pss: {pss_kb} kB\n", encoding="ascii")

    proc(100, 1, ["kiro-cli", "acp"], 50)
    proc(101, 100, ["aim", "mcp", "start-server", "local-creds-agent-mcp"], 30)
    proc(102, 101, ["/home/x/aim", "mcp", "start-server", "local-creds-agent-mcp"], 70)
    spec = footprint.ServerSpec(
        launches=(("aim", "mcp", "start-server", "local-creds-agent-mcp"),),
        shareable=False,
    )

    without = footprint.measure([("a", 100)], {}, proc_root=tmp_path)
    with_it = footprint.measure([("a", 100)], {"local-creds-agent-mcp": spec}, proc_root=tmp_path)
    assert without is not None and with_it is not None
    assert without.using_now_bytes == 0
    assert with_it.using_now_bytes == 100 * 1024
    assert with_it.with_sharing_bytes == 100 * 1024


@pytest.mark.asyncio
async def test_unanswered_pool_stats_null_both_figures(
    reading: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running daemon that does not answer stats: its backends are unread, so
    the handler passes ``None`` and no figure is drawn from half the processes."""
    seen: dict[str, Any] = {}

    def _measure(sessions: Any, specs: Any, backends: Any = ()) -> footprint.Footprint:
        seen["backends"] = backends
        return footprint.Footprint(None, None, 0, [])

    monkeypatch.setattr(footprint, "measure", _measure)

    class _Manager:
        is_running = True
        _spec = SimpleNamespace(mcp_target_env={})

        async def stats(self) -> dict:
            return {}

    app = _claims_app(user=OWNER, app_claim="")
    app["state"]._mcp_gateway_manager = _Manager()
    status, body = await _get(app)
    assert status == 200
    assert seen["backends"] is None
    assert body["using_now_bytes"] is None and body["with_sharing_bytes"] is None


@pytest.mark.asyncio
async def test_shared_now_reads_the_running_routing_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server enabled since the daemon started is saved intent only: it is
    not shared now, so no chat is offered a reload for it."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(mcp_footprint, "refused_servers", dict)
    monkeypatch.setattr(
        mcp_footprint,
        "_specs",
        lambda enabled, refused, routed=frozenset(): (captured.setdefault("routed", routed) and {}),
    )
    monkeypatch.setattr(footprint, "measure", lambda *a, **k: footprint.Footprint(0, 0, 0, []))

    class _Manager:
        is_running = True
        _spec = SimpleNamespace(
            mcp_target_env={
                "KIROCREW_MCP_TARGET_DOCS_MCP": "x",
                "KIROCREW_MCP_TARGET_SLACK_MCP__ab12": "y",
            }
        )

        async def stats(self) -> dict:
            return {"backends": []}

    app = _claims_app(user=OWNER, app_claim="")
    app["state"]._mcp_gateway_manager = _Manager()
    status, _ = await _get(app)
    assert status == 200
    assert captured["routed"] == frozenset({"DOCS_MCP", "SLACK_MCP"})


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["exclusive", "draining"])
async def test_unlisted_private_or_draining_backends_null_both_figures(
    reading: dict[str, Any], monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """The pool snapshot lists pooled backends only; a private or draining one
    is running but unlisted, so the reading would be short."""
    seen: dict[str, Any] = {}

    def _measure(sessions: Any, specs: Any, backends: Any = ()) -> footprint.Footprint:
        seen["backends"] = backends
        return footprint.Footprint(None, None, 0, [])

    monkeypatch.setattr(footprint, "measure", _measure)

    class _Manager:
        is_running = True
        _spec = SimpleNamespace(mcp_target_env={})

        async def stats(self) -> dict:
            return {"backends": [], field: 1}

    app = _claims_app(user=OWNER, app_claim="")
    app["state"]._mcp_gateway_manager = _Manager()
    status, _ = await _get(app)
    assert status == 200
    assert seen["backends"] is None


def test_a_settings_launch_is_kept_beside_an_agent_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One agent overrides a server; the others inherit the settings launch, so
    the reading must recognise both."""
    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    settings = tmp_path / "settings" / "mcp.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"mcpServers": {"docs": {"command": "/opt/docs", "args": ["serve"]}}})
    )
    monkeypatch.setattr(
        mcp_handlers,
        "_collect_server_rows",
        lambda: {
            "docs": {
                "transport": "stdio",
                "env_names": set(),
                "launches": {("/opt/docs", "serve", "--ro")},
                "env_ids": {"x"},
            }
        },
    )
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path / "agents")
    specs = mcp_footprint._specs(False, {})
    assert set(specs["docs"].launches) == {("/opt/docs", "serve"), ("/opt/docs", "serve", "--ro")}
    # Two env blocks (the override's and the settings one): no forecast.
    assert specs["docs"].one_identity is False
