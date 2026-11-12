"""The agent's pod lifecycle path: gateway routes, their guards, and the tools.

An agent session cannot reach the systemd user bus, so ``kirocrew pod up`` in one
of its shells fails with ``Permission denied``. These routes are the gateway doing
that part on the agent's behalf, and the ``pod_*`` tools Dev Fleet declares in its
manifest (``agent_tools.py``, served from the gateway through the ``agentTools``
seam) are how a session calls them. What is worth pinning is therefore not the pod mechanics --
``worktree_ops`` owns those and has its own tests -- but the seams that make the
detour safe and the answer usable:

* every route is admitted for the internal secret and nothing else,
* an app the operator never enabled is not callable,
* a refusal reaches the model as an ``Error:`` string rather than a fake handle,
* the pod token survives the trip verbatim, because a redacted token is useless.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.dev_fleet import agent_pod_api, agent_tools
from kiro_crew.apps.manifest import AppManifest

_POD_TOOLS = ("pod_up", "pod_restart", "pod_down", "pod_status", "pod_ls")


def _declared() -> dict[str, dict[str, Any]]:
    """The pod tools as the shipped ``app.json`` declares them, by name."""
    from pathlib import Path

    root = Path(agent_pod_api.__file__).parent
    manifest = AppManifest.from_json_file(root / "app.json")
    assert manifest.agentTools.validate(manifest.name) == []
    return {t.name: t.descriptor() | {"_decl": t} for t in manifest.agentTools.tools}


# Route registration + admission

_EXPECTED_ROUTES = {
    ("POST", "/api/apps/dev-fleet/pod/up"),
    ("POST", "/api/apps/dev-fleet/pod/restart"),
    ("POST", "/api/apps/dev-fleet/pod/down"),
    ("GET", "/api/apps/dev-fleet/pod/status"),
    ("GET", "/api/apps/dev-fleet/pod/list"),
}


def test_register_routes_mounts_exactly_the_agent_surface() -> None:
    """A sixth route here would be one the strict-path table has not admitted."""
    app = web.Application()
    agent_pod_api.register_routes(app)
    mounted = {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if r.method != "HEAD"  # aiohttp adds HEAD alongside every GET
    }
    assert mounted == _EXPECTED_ROUTES


def test_package_reexports_register_routes() -> None:
    """Gateway startup looks for ``register_routes`` on the PACKAGE, not a submodule.

    The package hook composes this module's agent-facing pod routes with the
    gateway-side cutover routes (``gateway_routes.py``), so what is pinned is that a
    call through the package mounts every pod route — identity with this module's
    function alone would leave the other family unmounted.
    """
    import kiro_crew.apps.builtins.dev_fleet as pkg

    app = web.Application()
    pkg.register_routes(app)
    mounted = {(r.method, r.resource.canonical) for r in app.router.routes() if r.method != "HEAD"}
    assert _EXPECTED_ROUTES <= mounted, sorted(_EXPECTED_ROUTES - mounted)


def test_every_route_is_named_in_the_strict_internal_table() -> None:
    """An unlisted route 403s for the agent; a listed prefix admits its whole subtree.

    Both halves matter, so this asserts the paths are present AND that the bare
    ``/pod`` prefix is not -- Dev Fleet's neighbourhood includes worktree prune and
    the Make Live cutover, which must never inherit an internal-secret grant.
    """
    from kiro_crew.dashboard.server import (
        _MIXED_INTERNAL_API_PATHS,
        _STRICT_INTERNAL_API_PATHS,
    )

    for _method, path in _EXPECTED_ROUTES:
        assert path in _STRICT_INTERNAL_API_PATHS, path
    assert "/api/apps/dev-fleet/pod" not in _STRICT_INTERNAL_API_PATHS
    # Strict, not mixed: no browser calls these. The dashboard's pod buttons go
    # through the app-backend reverse proxy instead.
    assert not any(p.startswith("/api/apps/dev-fleet/pod") for p in _MIXED_INTERNAL_API_PATHS)


class _Req:
    """The request attributes the guards read."""

    def __init__(
        self,
        *,
        remote: str = "127.0.0.1",
        internal_auth: bool = True,
        body: Any = None,
        query: dict[str, str] | None = None,
        unix: bool = False,
    ) -> None:
        self.remote = remote
        self._data = {"internal_auth": internal_auth}
        self._body = body
        self.can_read_body = body is not None
        self.query = query or {}
        self.path = "/api/apps/dev-fleet/pod/up"
        self.transport = object() if unix else None

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


def _payload(response: web.Response) -> dict[str, Any]:
    return json.loads(response.body.decode())


@pytest.mark.asyncio
async def test_non_git_source_install_discovers_checkout_before_pod_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Agent routes must run the executor-backed discovery tiers before the op."""
    from kiro_crew.apps.builtins.dev_fleet import repository, runtime

    installed_source = tmp_path / "installed-source"
    (installed_source / "src" / "kiro_crew").mkdir(parents=True)
    (installed_source / "pyproject.toml").write_text("[project]\nname = 'kiro-crew'\n")
    assert not (installed_source / ".git").exists()

    checkout = tmp_path / "home" / "workplace" / "kirocrew"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "src" / "kiro_crew").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname = 'kiro-crew'\n")

    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    monkeypatch.setattr(repository, "_own_source_checkout", lambda: str(installed_source))
    monkeypatch.setattr(repository, "_candidate_checkouts", lambda: [str(checkout)])
    monkeypatch.setattr(repository, "_configured_main_repo_checked", lambda failed=None: ("", True))
    monkeypatch.setattr(repository, "_resolve_primary_checkout", lambda path: path)
    monkeypatch.setattr(repository, "_repo_source_hint", lambda: "unused")
    monkeypatch.setattr(repository, "_DISCOVERY_DONE", False)
    monkeypatch.setattr(repository, "_DISCOVERY_LOCK", None)
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    monkeypatch.setattr(repository, "MAIN_REPO_INFERRED", False)
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_LATCHED_CONFIGURED", "")
    monkeypatch.setattr(runtime, "_GIT_TRUSTED_HELPERS", {})

    async def _noop() -> None:
        return None

    async def _origin() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_resolve_base_branch", _noop)
    monkeypatch.setattr(repository, "_load_fallback_repos", _noop)
    monkeypatch.setattr(repository, "_upstream_remote", _origin)

    async def _op() -> dict[str, Any]:
        return {"ok": True, "repo": repository._repo()}

    assert await agent_pod_api._run_op(_op) == {"ok": True, "repo": str(checkout)}


@pytest.fixture()
def granted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default posture for the tests below: the Dev Fleet app is enabled."""
    monkeypatch.setattr(
        "kiro_crew.apps.builtins.dev_fleet.agent_pod_api.host_exec_granted", lambda _n: True
    )


@pytest.mark.asyncio
async def test_the_unix_socket_transport_is_admitted(
    monkeypatch: pytest.MonkeyPatch, granted: None
) -> None:
    """mcp_core PREFERS the gateway's AF_UNIX socket, where request.remote is empty.

    Testing only the loopback half would refuse every pod tool on POSIX -- the only
    platform where systemd --user pods exist -- so this pins the union the
    middleware itself uses (`_unix_sock is not None or is_loopback(...)`).
    """
    monkeypatch.setattr("kiro_crew.dashboard.origin.request_is_unix_socket", lambda _r: True)
    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_ls": staticmethod(_ok_ls)})
    )
    resp = await agent_pod_api.handle_pod_list(_Req(remote="", unix=True))  # type: ignore[arg-type]
    assert resp.status == 200


async def _ok_ls() -> dict[str, Any]:
    return {"ok": True, "pods": []}


@pytest.mark.asyncio
async def test_a_non_local_caller_is_refused(granted: None) -> None:
    """A `local_only=False` deployment reclassifies strict paths as mixed."""
    resp = await agent_pod_api.handle_pod_list(_Req(remote="10.0.0.7"))  # type: ignore[arg-type]
    assert resp.status == 403
    assert _payload(resp)["code"] == "loopback_only"


@pytest.mark.asyncio
async def test_cookie_only_caller_is_refused(granted: None) -> None:
    """Listed-as-strict does not prove the secret was checked; the handler re-checks."""
    resp = await agent_pod_api.handle_pod_list(_Req(internal_auth=False))  # type: ignore[arg-type]
    assert resp.status == 403
    assert _payload(resp)["code"] == "internal_secret_required"


@pytest.mark.asyncio
async def test_disabled_app_is_not_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routes are registered at startup, before any app is known to be enabled."""
    monkeypatch.setattr(
        "kiro_crew.apps.builtins.dev_fleet.agent_pod_api.host_exec_granted", lambda _n: False
    )
    resp = await agent_pod_api.handle_pod_list(_Req())  # type: ignore[arg-type]
    assert resp.status == 403
    assert _payload(resp)["code"] == "app_not_enabled"


def test_the_capability_is_not_gated_on_an_operator_grant() -> None:
    """Agent pod control is an intended capability, so it carries no operator gate.

    A grant would break the unattended QA loop these routes serve, and the risk it
    seems to close is reachable by other paths anyway (security.md, "Scoped user-bus
    locator forward").
    """
    assert not hasattr(agent_pod_api, "_operator_granted")
    assert not hasattr(agent_pod_api, "_require_operator_grant")

    from kiro_crew.config.sections import AgentConfig

    assert not hasattr(AgentConfig(), "allow_agent_pod_control")


@pytest.mark.asyncio
async def test_missing_worktree_is_a_400_with_a_code(granted: None) -> None:
    resp = await agent_pod_api.handle_pod_up(_Req(body={}))  # type: ignore[arg-type]
    assert resp.status == 400
    assert _payload(resp)["code"] == "invalid_worktree"


@pytest.mark.asyncio
async def test_provisioning_is_not_reachable_from_the_agent_surface(granted: None) -> None:
    """A minutes-long blocking call dies to any timeout with no way to learn the outcome.

    The schema rejects the key, so a model that asks for it is told rather than
    silently given a 16-minute call. The dashboard's Provision button streams the
    same work under a run id, which is the shape that survives a restart.
    """
    props = _declared()["pod_up"]["inputSchema"]["properties"]
    assert list(props) == ["worktree", "memory", "cpu", "env"]
    assert "provision" not in props


@pytest.mark.asyncio
async def test_pod_up_returns_the_handle(monkeypatch: pytest.MonkeyPatch, granted: None) -> None:
    seen: dict[str, Any] = {}

    async def _fake_up(name: str) -> dict[str, Any]:
        seen["name"] = name
        return {
            "ok": True,
            "name": name,
            "status": "up",
            "port": 7913,
            "base_url": "http://127.0.0.1:7913",
            "token": "tok-abc",
            "ttl": "2h",
        }

    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_up": staticmethod(_fake_up)})
    )
    resp = await agent_pod_api.handle_pod_up(
        _Req(body={"worktree": "kc-wt-1"})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert seen == {"name": "kc-wt-1"}
    assert _payload(resp)["token"] == "tok-abc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "req"),
    [
        ("handle_pod_up", {"body": {"worktree": "w"}}),
        ("handle_pod_restart", {"body": {"worktree": "w"}}),
        ("handle_pod_down", {"body": {"worktree": "w"}}),
        ("handle_pod_status", {"query": {"worktree": "w"}}),
        ("handle_pod_list", {}),
    ],
)
async def test_every_verb_answers_an_unusable_checkout(
    monkeypatch: pytest.MonkeyPatch, handler: str, req: dict[str, Any], granted: None
) -> None:
    """``repository._repo()`` RAISES, and it sits on more than one branch per verb.

    It is reached through the subprocess cwd and through the discovery chain inside
    ``_pod_checkout_guard``, so this asserts on every verb rather than on raise sites.
    """
    monkeypatch.setattr(agent_pod_api.platform_compat, "IS_WINDOWS", False)
    from kiro_crew.apps.builtins.dev_fleet import repository

    async def _aboom(*_a: Any, **_k: Any) -> Any:
        raise repository.RepoNotConfigured("no Kiro Crew checkout found to manage")

    class _Ops:
        _pod_up = staticmethod(_aboom)
        _pod_restart = staticmethod(_aboom)
        _pod_down = staticmethod(_aboom)
        _pod_status = staticmethod(_aboom)
        _pod_ls = staticmethod(_aboom)

    monkeypatch.setattr(agent_pod_api, "_ops", lambda: _Ops)
    resp = await getattr(agent_pod_api, handler)(_Req(**req))  # type: ignore[arg-type]
    assert resp.status == 409, handler
    payload = _payload(resp)
    assert payload["code"] == "repo_unavailable"
    assert "checkout" in payload["error"]


@pytest.mark.asyncio
async def test_a_refused_pod_op_is_409_not_500(
    monkeypatch: pytest.MonkeyPatch, granted: None
) -> None:
    """The request was well-formed and authorized; the HOST could not carry it out."""

    async def _fake_down(name: str) -> dict[str, Any]:
        return {"ok": False, "error": "unknown worktree: 'nope'"}

    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_down": staticmethod(_fake_down)})
    )
    resp = await agent_pod_api.handle_pod_down(_Req(body={"worktree": "nope"}))  # type: ignore[arg-type]
    assert resp.status == 409
    assert _payload(resp)["code"] == "pod_down_failed"


@pytest.mark.asyncio
async def test_an_overlong_worktree_name_is_refused(granted: None) -> None:
    """The name reaches worktree matching and a filesystem path before validate_name."""
    resp = await agent_pod_api.handle_pod_up(
        _Req(body={"worktree": "x" * 5000})  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert _payload(resp)["code"] == "invalid_worktree"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["{not json", "[1, 2]"])
async def test_a_non_object_body_is_refused(body: str, granted: None) -> None:
    resp = await agent_pod_api.handle_pod_down(_Req(body=body))  # type: ignore[arg-type]
    assert resp.status == 400
    assert _payload(resp)["code"] == "invalid_body"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "op", "code", "req"),
    [
        ("handle_pod_up", "_pod_up", "pod_up_failed", {"body": {"worktree": "w"}}),
        ("handle_pod_status", "_pod_status", "pod_status_failed", {"query": {"worktree": "w"}}),
        ("handle_pod_list", "_pod_ls", "pod_list_failed", {}),
    ],
)
async def test_each_verb_reports_its_own_refusal_code(
    monkeypatch: pytest.MonkeyPatch,
    handler: str,
    op: str,
    code: str,
    req: dict[str, Any],
    granted: None,
) -> None:
    """One code per verb: an agent branching on the answer must know which call failed."""

    async def _fake(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"ok": False}

    monkeypatch.setattr(agent_pod_api, "_ops", lambda: type("Ops", (), {op: staticmethod(_fake)}))
    resp = await getattr(agent_pod_api, handler)(_Req(**req))  # type: ignore[arg-type]
    assert resp.status == 409
    payload = _payload(resp)
    assert payload["code"] == code
    # An empty in-band error must not become an empty sentence.
    assert payload["error"]


def test_ops_resolves_the_real_worktree_ops_module() -> None:
    """The lazy import is the seam every handler depends on; a typo there is silent."""
    from kiro_crew.apps.builtins.dev_fleet import worktree_ops

    assert agent_pod_api._ops() is worktree_ops
    assert callable(worktree_ops._pod_status)
    assert callable(worktree_ops._pod_ls)


# The declared tools (agent_tools.py, through the manifest agentTools seam)


def _verb(
    monkeypatch: pytest.MonkeyPatch,
    attr: str,
    payload: dict[str, Any],
    *,
    status: int = 200,
    calls: list[tuple[Any, ...]] | None = None,
) -> None:
    """Stand in for one ``agent_pod_api.pod_*_response`` verb."""

    async def _fake(*args: Any) -> web.Response:
        if calls is not None:
            calls.append(args)
        return web.json_response(payload, status=status)

    monkeypatch.setattr(agent_pod_api, attr, _fake)


def test_pod_tools_are_declared_with_a_verb_each() -> None:
    """A declaration with no verb advertises a tool that cannot run."""
    declared = _declared()
    assert set(declared) == set(_POD_TOOLS) == set(agent_tools.TOOLS)
    for name in _POD_TOOLS:
        decl = declared[name]["_decl"]
        assert decl.result == "text", name
        assert decl.max_result_chars == 60_000, name


def test_pod_tools_keep_the_core_validation_bounds() -> None:
    """The bounds the core tool held are the declaration's, so the same calls are refused."""
    from kiro_crew.apps import app_agent_tools
    from kiro_crew.validation_parts.field_engine import ValidationError, validate_tool_args

    for name in _POD_TOOLS:
        decl = _declared()[name]["_decl"]
        schema = app_agent_tools.build_tool_schema(
            app_agent_tools.DeclaredTool(app="dev-fleet", route="agent-tools", decl=decl)
        )
        if name == "pod_ls":
            with pytest.raises(ValidationError):
                validate_tool_args({"worktree": "w"}, schema)
            continue
        with pytest.raises(ValidationError):
            validate_tool_args({"worktree": "w" * 201}, schema)
        assert validate_tool_args({"worktree": "w" * 200}, schema) == {"worktree": "w" * 200}
        if name in ("pod_up", "pod_restart"):
            with pytest.raises(ValidationError):
                validate_tool_args({"worktree": "w", "memory": "1" * 17}, schema)


def test_pod_tools_wait_as_long_as_the_core_tools_did() -> None:
    """A pod boot is bounded at 180s and a pod down at 90s in the gateway; the caller waits past both.

    A read spawns a python subprocess in the gateway, so 10s cannot cover it either.
    """
    waits = {n: d["_decl"].timeout_seconds for n, d in _declared().items()}
    assert waits == {
        "pod_up": 210,
        "pod_restart": 210,
        "pod_down": 120,
        "pod_status": 45,
        "pod_ls": 45,
    }


@pytest.mark.asyncio
async def test_pod_up_tool_returns_the_token_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token IS the deliverable -- a redacted one is a handle that cannot be used."""
    calls: list[tuple[Any, ...]] = []
    _verb(
        monkeypatch,
        "pod_up_response",
        {
            "ok": True,
            "name": "kc-wt-1",
            "status": "up",
            "port": 7913,
            "base_url": "http://127.0.0.1:7913",
            "token": "tok-abc123",
            "ttl": "2h",
        },
        calls=calls,
    )
    out = _text(await agent_tools.pod_up({"worktree": "kc-wt-1"}))
    assert "tok-abc123" in out
    assert "http://127.0.0.1:7913" in out
    assert "open: http://127.0.0.1:7913/?token=tok-abc123" in out.splitlines()
    assert calls == [({"worktree": "kc-wt-1"},)]


def _mint_link_token() -> str:
    """A link token minted the way a pod mints one: a 2h session."""
    from kiro_crew.dashboard.token_auth import generate_token

    return generate_token("local-app", ttl_seconds=7200, register_nonce=False)


def _text(answer: dict[str, Any]) -> str:
    """The text a tool's answer carries, its credential put back where it stands."""
    text = answer["text"]
    return answer["credential"].join(text) if "credential" in answer else text


def _token_line(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("token:"))


@pytest.mark.asyncio
async def test_a_pod_start_names_its_token_as_the_answers_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token is cut out of the text, so the core redacts every other character."""
    token = "tok-" + "a" * 40
    _verb(monkeypatch, "pod_up_response", {"ok": True, "port": 7913, "token": token})
    answer = await agent_tools.pod_up({"worktree": "kc-wt-1"})
    assert answer["credential"] == token
    assert len(answer["text"]) == 3
    assert not any(token in piece for piece in answer["text"])


def test_the_core_relays_a_pod_up_reply_byte_for_byte() -> None:
    """Through the core's real redaction, the agent reads what the core tool returned.

    A real link token is what the redactor would otherwise rewrite (``token=...`` and
    the token itself), so the reply is built around a minted one.
    """
    from kiro_crew.mcp_tools import app_tools

    token = _mint_link_token()
    resp = {
        "ok": True,
        "port": 7913,
        "base_url": "http://127.0.0.1:7913",
        "checkout": "/home/u/clone-a/kc-wt-1",
        "memory": "4G",
        "cpu": "200%",
        "token": token,
        "link_ttl_secs": 300,
        "session_ttl_secs": 7200,
    }
    answer = agent_tools._pod_start_reply({"worktree": "kc-wt-1"}, resp, "up")
    assert app_tools._text_answer("pod_up", answer) == (
        "Pod `kc-wt-1` is up on port 7913.\n"
        "base_url: http://127.0.0.1:7913\n"
        "checkout: /home/u/clone-a/kc-wt-1\n"
        "limits: memory=4G cpu=200%\n"
        f"token: {token}  (use within 5 min; the session it opens lasts 2h)\n"
        f"open: http://127.0.0.1:7913/?token={token}\n"
        "Past its window the pod refuses the token as expired; call pod_up "
        "again (the pod keeps running) for a fresh one.\n"
        "Stop it with pod_down when you are done: kc-wt-1"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Pod `kc-wt-1`: up port=7913 health=200 memory=4G cpu=200%",
        "2 pod(s) active:\n  kc-wt-1  port=7913 health=200\n  kc-wt-2  port=7940 health=0",
        "Pod `kc-wt-1` is stopped and its data directory reclaimed.",
        "Error: pod up 'kc-wt-1' failed [pod_up_failed]: no built dist",
    ],
)
def test_the_core_relays_every_other_pod_reply_unchanged(text: str) -> None:
    from kiro_crew.mcp_tools import app_tools

    assert app_tools._text_answer("pod_ls", {"text": text}) == text


@pytest.mark.asyncio
async def test_pod_up_tool_names_both_token_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The link window and the session lifetime are both named, from the route's numbers.

    Naming only the session lifetime tells the agent its token lasts hours when
    the pod refuses it as expired minutes later.
    """
    _verb(
        monkeypatch,
        "pod_up_response",
        {
            "ok": True,
            "port": 7913,
            "base_url": "http://127.0.0.1:7913",
            "token": "tok-abc123",
            "ttl": "2h",
            "link_ttl_secs": 300,
            "session_ttl_secs": 7200,
        },
    )
    out = _text(await agent_tools.pod_up({"worktree": "kc-wt-1"}))
    line = _token_line(out)
    assert "tok-abc123" in line
    assert "use within 5 min" in line
    assert "the session it opens lasts 2h" in line
    assert "ttl 2h" not in out
    assert "call pod_up again" in out


@pytest.mark.asyncio
async def test_pod_up_tool_reads_the_numbers_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """The windows come from the route response, not from constants in the tool."""
    _verb(
        monkeypatch,
        "pod_up_response",
        {
            "ok": True,
            "port": 7913,
            "token": "tok-abc123",
            "link_ttl_secs": 120,
            "session_ttl_secs": 1800,
        },
    )
    line = _token_line(_text(await agent_tools.pod_up({"worktree": "kc-wt-1"})))
    assert "use within 2 min" in line
    assert "lasts 30 min" in line


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"ttl": "2h"},
        {"link_ttl_secs": 0, "session_ttl_secs": 7200},
        {"link_ttl_secs": True, "session_ttl_secs": 7200},
        {"link_ttl_secs": "300", "session_ttl_secs": 7200},
    ],
)
async def test_pod_up_tool_names_no_duration_it_was_not_given(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]
) -> None:
    """Without usable windows the line names no duration; a guessed one is the defect."""
    _verb(
        monkeypatch, "pod_up_response", {"ok": True, "port": 7913, "token": "tok-abc123", **extra}
    )
    line = _token_line(_text(await agent_tools.pod_up({"worktree": "kc-wt-1"})))
    assert "tok-abc123" in line
    assert "2h" not in line
    assert "straight away" in line


def test_pod_up_description_names_the_link_window() -> None:
    """The description is what an agent plans from, so it states both limits."""
    desc = _declared()["pod_up"]["description"]
    assert "2h credential" not in desc
    assert "5 minutes" in desc
    assert "session" in desc and "2h" in desc
    assert "fresh token" in desc


@pytest.mark.asyncio
async def test_pod_up_route_reports_the_windows_of_the_minted_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route reads both windows from the token's own claims.

    A real link token is minted the way the pod mints one (a 2h session), so the
    numbers pinned here are the ones a pod hands out, not ones this test chose.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.apps.builtins.dev_fleet import repository, runtime, worktree_ops
    from kiro_crew.dashboard.token_auth import LINK_WINDOW_SECS

    token = _mint_link_token()
    handle = {"name": "kc-wt-1", "status": "up", "port": 7913, "token": token, "ttl": "2h"}
    monkeypatch.setattr(worktree_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(worktree_ops, "_pod_env", lambda: {})
    monkeypatch.setattr(runtime, "_warm_build_path", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_load_cfg", lambda: None)
    monkeypatch.setattr(runtime, "_find_cli", lambda: ["kirocrew"])
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", False)
    monkeypatch.setattr(repository, "_repo", lambda: "/nonexistent-repo")
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, json.dumps(handle), "")))
    result = await worktree_ops._pod_up_locked("kc-wt-1")
    assert result["ok"] is True
    assert result["token"] == token
    assert result["link_ttl_secs"] == LINK_WINDOW_SECS == 300
    assert result["session_ttl_secs"] == 7200


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "!!!.sig",
        "bnVsbA.sig",  # JSON null
        "WzEsMiwzXQ.sig",  # a JSON list
        "eyJpYXQiOjEwMCwiZXhwIjoxMDB9.sig",  # no session_exp, zero link window
        "eyJpYXQiOjEwMCwiZXhwIjp0cnVlLCJzZXNzaW9uX2V4cCI6MjAwfQ.sig",  # bool exp
    ],
)
def test_token_windows_reports_nothing_for_a_malformed_token(token: str) -> None:
    """A token the route cannot read yields no window rather than a wrong one."""
    from kiro_crew.apps.builtins.dev_fleet import worktree_ops

    assert worktree_ops._token_windows(token) == {}


@pytest.mark.asyncio
async def test_pod_up_tool_says_so_when_the_cli_withheld_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty token is a deliberate refusal to certify the port, not an oversight."""
    _verb(
        monkeypatch, "pod_up_response", {"ok": True, "name": "kc-wt-1", "port": 7913, "token": ""}
    )
    out = _text(await agent_tools.pod_up({"worktree": "kc-wt-1"}))
    assert "withheld" in out
    assert not out.startswith("Error:")


@pytest.mark.asyncio
async def test_pod_up_tool_prints_the_checkout_a_handle_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pod-e2e handle is built from this reply, so the clone it serves must be on it."""
    _verb(
        monkeypatch,
        "pod_up_response",
        {
            "ok": True,
            "port": 7913,
            "base_url": "http://127.0.0.1:7913",
            "token": "tok-abc123",
            "checkout": "/home/u/clone-a/kc-wt-1",
        },
    )
    out = _text(await agent_tools.pod_up({"worktree": "kc-wt-1"}))
    assert "checkout: /home/u/clone-a/kc-wt-1" in out.splitlines()


@pytest.mark.asyncio
async def test_a_route_refusal_reaches_the_model_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Error:`` is load-bearing: call_tool_with_logging derives the outcome from it."""
    _verb(
        monkeypatch,
        "pod_up_response",
        {"ok": False, "code": "pod_up_failed", "error": "no built dist"},
        status=409,
    )
    out = _text(await agent_tools.pod_up({"worktree": "kc-wt-1"}))
    assert out == "Error: pod up 'kc-wt-1' failed [pod_up_failed]: no built dist"


@pytest.mark.asyncio
async def test_a_refusal_with_no_json_body_is_not_reported_as_a_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _plain(*_a: Any) -> web.Response:
        return web.Response(status=500, text="boom")

    monkeypatch.setattr(agent_pod_api, "pod_down_response", _plain)
    assert _text(await agent_tools.pod_down({"worktree": "kc-wt-1"})).startswith("Error:")


@pytest.mark.asyncio
async def test_pod_status_hands_the_worktree_to_the_verb_as_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No query string is built any more, so the name cannot carry a second parameter."""
    calls: list[tuple[Any, ...]] = []
    _verb(
        monkeypatch,
        "pod_status_response",
        {"ok": True, "name": "a b", "status": "down", "port": 7999, "health": 0},
        calls=calls,
    )
    out = _text(await agent_tools.pod_status({"worktree": "a b&x=1"}))
    assert calls == [("a b&x=1",)]
    assert out == "Pod `a b`: down port=7999 health=0"


@pytest.mark.asyncio
async def test_pod_ls_reports_an_empty_host_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    _verb(monkeypatch, "pod_list_response", {"ok": True, "pods": []})
    assert _text(await agent_tools.pod_ls({})) == "No pods are active on this host."


@pytest.mark.asyncio
async def test_pod_ls_lists_each_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    _verb(
        monkeypatch,
        "pod_list_response",
        {
            "ok": True,
            "pods": [
                {"name": "kc-wt-1", "port": 7913, "health": 200},
                {"name": "kc-wt-2", "port": 7940, "health": 0},
            ],
        },
    )
    out = _text(await agent_tools.pod_ls({}))
    assert "kc-wt-1" in out
    assert "kc-wt-2" in out
    assert "2 pod(s) active" in out


class _ToolReq(_Req):
    """A ``POST /api/app-tools/call`` request as the registry hands it to the route."""

    def __init__(self, body: Any, **kw: Any) -> None:
        super().__init__(body=body, **kw)
        self.path = "/api/apps/dev-fleet/agent-tools"


@pytest.mark.asyncio
async def test_the_tool_route_answers_each_tool_as_text(
    monkeypatch: pytest.MonkeyPatch, granted: None
) -> None:
    _verb(monkeypatch, "pod_down_response", {"ok": True})
    resp = await agent_tools.handle_agent_tool(
        _ToolReq({"tool": "pod_down", "arguments": {"worktree": "w"}})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert _payload(resp) == {"text": "Pod `w` is stopped and its data directory reclaimed."}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "req",
    [
        {"remote": "10.0.0.7"},
        {"internal_auth": False},
    ],
)
async def test_the_tool_route_holds_the_pod_routes_caller_gate(
    monkeypatch: pytest.MonkeyPatch, granted: None, req: dict[str, Any]
) -> None:
    """A dashboard cookie that reaches the route directly is not the gateway's own call."""
    monkeypatch.setattr(agent_pod_api, "pod_list_response", lambda: pytest.fail("no pod work"))
    resp = await agent_tools.handle_agent_tool(
        _ToolReq({"tool": "pod_ls", "arguments": {}}, **req)  # type: ignore[arg-type]
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_the_tool_route_refuses_without_the_grant_in_the_tools_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal reads exactly as the core tool rendered the route's 403."""
    monkeypatch.setattr(
        "kiro_crew.apps.builtins.dev_fleet.agent_pod_api.host_exec_granted", lambda _n: False
    )
    monkeypatch.setattr(agent_pod_api, "pod_up_response", lambda _b: pytest.fail("no pod work"))
    resp = await agent_tools.handle_agent_tool(
        _ToolReq({"tool": "pod_up", "arguments": {"worktree": "w"}})  # type: ignore[arg-type]
    )
    text = _payload(resp)["text"]
    assert text.startswith("Error: pod up 'w' failed [app_not_enabled]: dev-fleet is not enabled.")


@pytest.mark.asyncio
async def test_the_tool_route_knows_only_the_declared_tools(granted: None) -> None:
    resp = await agent_tools.handle_agent_tool(
        _ToolReq({"tool": "pod_provision", "arguments": {}})  # type: ignore[arg-type]
    )
    assert resp.status == 404


def test_the_route_hook_registers_exactly_the_declared_route() -> None:
    from pathlib import Path

    manifest = json.loads((Path(agent_pod_api.__file__).parent / "app.json").read_text())
    assert manifest["backend"]["hooks"] == {"routes": "agent_tools:register_routes"}
    (route,) = agent_tools.register_routes(None)
    assert (route.method, route.path) == ("POST", "/" + manifest["agentTools"]["route"])


# Ceilings and the home-keeping restart


@pytest.mark.asyncio
async def test_pod_up_forwards_the_ceilings(monkeypatch: pytest.MonkeyPatch, granted: None) -> None:
    seen: dict[str, Any] = {}

    async def _fake_up(name: str, **limits: str) -> dict[str, Any]:
        seen.update(name=name, **limits)
        return {"ok": True, "port": 7913}

    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_up": staticmethod(_fake_up)})
    )
    resp = await agent_pod_api.handle_pod_up(
        _Req(body={"worktree": "w", "memory": "16G", "cpu": "800%"})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert seen == {"name": "w", "memory": "16G", "cpu": "800%"}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [16, "--cpu", " --json", "x" * 17])
async def test_a_malformed_ceiling_is_a_400_before_any_pod_work(
    monkeypatch: pytest.MonkeyPatch, granted: None, bad: object
) -> None:
    """The CLI validates the value; this only keeps argv flags and junk out of it."""
    monkeypatch.setattr(agent_pod_api, "_ops", lambda: pytest.fail("no pod work for a bad body"))
    resp = await agent_pod_api.handle_pod_restart(
        _Req(body={"worktree": "w", "memory": bad})  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert _payload(resp)["code"] == "invalid_limit"


@pytest.mark.asyncio
async def test_pod_restart_route_returns_the_handle(
    monkeypatch: pytest.MonkeyPatch, granted: None
) -> None:
    monkeypatch.setattr(agent_pod_api.platform_compat, "IS_WINDOWS", False)
    seen: dict[str, Any] = {}

    async def _fake_restart(name: str, **limits: str) -> dict[str, Any]:
        seen.update(name=name, **limits)
        return {"ok": True, "port": 7913, "home_kept": True, "token": "tok-r"}

    monkeypatch.setattr(
        agent_pod_api,
        "_ops",
        lambda: type("Ops", (), {"_pod_restart": staticmethod(_fake_restart)}),
    )
    resp = await agent_pod_api.handle_pod_restart(
        _Req(body={"worktree": "w", "cpu": "400%"})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert seen == {"name": "w", "cpu": "400%"}
    assert _payload(resp)["home_kept"] is True


@pytest.mark.asyncio
async def test_pod_restart_route_reports_a_refusal_as_409(
    monkeypatch: pytest.MonkeyPatch, granted: None
) -> None:
    monkeypatch.setattr(agent_pod_api.platform_compat, "IS_WINDOWS", False)

    async def _refuse(name: str, **_l: str) -> dict[str, Any]:
        return {"ok": False, "error": "nothing to restart"}

    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_restart": staticmethod(_refuse)})
    )
    resp = await agent_pod_api.handle_pod_restart(_Req(body={"worktree": "w"}))  # type: ignore[arg-type]
    assert resp.status == 409
    assert _payload(resp)["code"] == "pod_restart_failed"


@pytest.mark.asyncio
async def test_pod_restart_tool_hands_the_ceilings_to_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    _verb(
        monkeypatch,
        "pod_restart_response",
        {
            "ok": True,
            "port": 7913,
            "base_url": "http://127.0.0.1:7913",
            "token": "tok-r",
            "memory": "16G",
            "cpu": "200%",
        },
        calls=calls,
    )
    out = _text(await agent_tools.pod_restart({"worktree": "w", "memory": "16G", "cpu": ""}))
    assert calls == [({"worktree": "w", "memory": "16G"},)]
    assert "restarted with its data kept" in out
    assert "memory=16G cpu=200%" in out


@pytest.mark.asyncio
async def test_pod_restart_route_refuses_on_windows_before_any_pod_work(
    monkeypatch: pytest.MonkeyPatch, granted: None
) -> None:
    """The only Windows restart deletes the home, which this tool promises to keep."""
    monkeypatch.setattr(agent_pod_api.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(agent_pod_api, "_ops", lambda: pytest.fail("no pod work on Windows"))
    resp = await agent_pod_api.handle_pod_restart(
        _Req(body={"worktree": "w", "memory": "256M"})  # type: ignore[arg-type]
    )
    assert resp.status == 409
    assert _payload(resp)["code"] == "pod_restart_unsupported"


# The seam end to end: listing, the deny universe, and the gateway route


def test_the_shipped_declaration_is_read_as_first_party() -> None:
    """The reader takes the block from the shipped ``app.json``, for a first-party slot only."""
    from kiro_crew.apps import app_agent_tools

    rows = ("apps", ("dev-fleet", 0, 0))
    tools = app_agent_tools._scan(rows, frozenset({"dev-fleet"}))
    assert [(t.app, t.route, t.name) for t in tools] == [
        ("dev-fleet", "agent-tools", name) for name in _POD_TOOLS
    ]
    assert app_agent_tools._scan(rows, frozenset()) == ()


def test_the_pod_tools_keep_their_names_in_the_core_listing() -> None:
    """The names an agent, a skill or a policy knows stay ``pod_*``, under ``kirocrew-core``."""
    from kiro_crew.apps import app_agent_tools
    from kiro_crew.mcp_tools import app_tools, build_tool_names

    shipped = {t.name for t in app_agent_tools.shipped_tools.__wrapped__() if t.app == "dev-fleet"}
    assert shipped == set(_POD_TOOLS)
    assert set(_POD_TOOLS) <= set(build_tool_names())
    assert set(_POD_TOOLS) <= set(app_tools.HANDLERS)
    # No core domain declares them any more, so nothing in core keys a tool on this app.
    assert not set(_POD_TOOLS) & app_tools._core_tool_names()


def test_adoption_no_longer_refuses_dev_fleet_on_a_core_tool_table() -> None:
    """E10 refused a pin for an app a core table keys a capability on."""
    from kiro_crew.apps.builtin_adoption_checks import core_named_apps

    assert "dev-fleet" not in core_named_apps()


@pytest.mark.asyncio
async def test_a_pod_tool_call_is_served_by_the_gateway_route_not_the_backend(
    monkeypatch: pytest.MonkeyPatch, granted: None
) -> None:
    """Dev Fleet has a sandboxed backend, so the call must reach its GATEWAY route.

    The real route hook is loaded from the shipped tree into a real route registry, and
    the real ``/api/app-tools/call`` handler dispatches the call: the backend proxy must
    not be reached, and the answer is the tool's text.
    """
    import types
    from pathlib import Path

    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.apps import app_agent_tools, hooks_integration
    from kiro_crew.apps import routes as app_routes
    from kiro_crew.apps.route_registry import RouteRegistry
    from kiro_crew.dashboard.handlers import app_agent_tools as handler_mod

    root = Path(agent_pod_api.__file__).parent
    manifest = json.loads((root / "app.json").read_text(encoding="utf-8"))
    registry = RouteRegistry(web.Application())
    registered = await registry.register_app_routes(
        "dev-fleet",
        root,
        manifest["backend"]["hooks"]["routes"],
        types.SimpleNamespace(health=None),  # type: ignore[arg-type]
    )
    try:
        assert registered == ["POST /api/apps/dev-fleet/agent-tools"]
        decl = _declared()["pod_ls"]["_decl"]
        tool = app_agent_tools.DeclaredTool(app="dev-fleet", route="agent-tools", decl=decl)
        monkeypatch.setattr(handler_mod, "_resolve", lambda _n: (tool, True, True))
        monkeypatch.setattr(hooks_integration, "get_route_registry", lambda: registry)
        monkeypatch.setattr(
            app_routes, "handle_app_api_proxy", lambda *_a, **_k: pytest.fail("proxied")
        )
        monkeypatch.setattr("kiro_crew.dashboard.origin.request_is_unix_socket", lambda _r: True)
        _verb(monkeypatch, "pod_list_response", {"ok": True, "pods": []})
        req = make_mocked_request("POST", "/api/app-tools/call")
        req["internal_auth"] = True
        req._read_bytes = json.dumps({"tool": "pod_ls", "arguments": {}}).encode()
        resp = await handler_mod.api_app_agent_tool_call(req)
        assert resp.status == 200
        assert _payload(resp) == {"text": "No pods are active on this host."}
    finally:
        registry.deregister_app_routes("dev-fleet")


# `env`: allowlisted test-double overrides, checked before argv


@pytest.mark.asyncio
async def test_pod_up_forwards_a_checked_env_override(
    monkeypatch: pytest.MonkeyPatch, granted: None, tmp_path
) -> None:
    fake = tmp_path / "fake-kiro-cli.py"
    fake.write_text("#!/bin/sh\nexit 2\n")
    fake.chmod(0o755)
    seen: dict[str, Any] = {}

    async def _fake_up(name: str, **kw: Any) -> dict[str, Any]:
        seen.update(kw, name=name)
        return {"ok": True, "name": name, "port": 7913, "token": "t"}

    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_up": staticmethod(_fake_up)})
    )
    resp = await agent_pod_api.handle_pod_up(
        _Req(body={"worktree": "w", "env": [f"KIROCREW_KIRO_BIN={fake}"]})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert seen == {"name": "w", "env": {"KIROCREW_KIRO_BIN": str(fake)}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [["AWS_SECRET_ACCESS_KEY=abc"], ["KIROCREW_KIRO_BIN=relative/kiro"], "KIROCREW_KIRO_BIN=/x"],
)
async def test_pod_up_refuses_an_env_override_before_any_pod_work(
    monkeypatch: pytest.MonkeyPatch, granted: None, env: Any
) -> None:
    async def _never(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise AssertionError("a refused env override must not reach the pod start")

    monkeypatch.setattr(
        agent_pod_api, "_ops", lambda: type("Ops", (), {"_pod_up": staticmethod(_never)})
    )
    resp = await agent_pod_api.handle_pod_up(
        _Req(body={"worktree": "w", "env": env})  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert _payload(resp)["code"] == "invalid_env"


@pytest.mark.asyncio
async def test_each_env_override_is_one_argv_element(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--env=K=V`` in one element, so a value can never be read as another flag."""
    from kiro_crew.apps.builtins.dev_fleet import worktree_ops_pods

    seen: list[list[str]] = []

    async def _start(name: str, verb: str, extra: list[str]) -> dict[str, Any]:
        seen.append(extra)
        return {"ok": True}

    monkeypatch.setattr(worktree_ops_pods, "_pod_start", _start)
    await worktree_ops_pods._pod_up("w", "16G", "", env={"KIROCREW_KIRO_BIN": "/abs/--seed x"})
    assert seen == [["--memory", "16G", "--env=KIROCREW_KIRO_BIN=/abs/--seed x"]]
