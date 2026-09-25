"""Every mutating dashboard route reaches the owner gate, or is named on purpose.

The route list comes from the REAL registrars ``start_dashboard`` calls, never from
a list of modules. A route added anywhere later is walked here without anyone
editing this file.

Each POST, PUT, PATCH and DELETE route is driven once as an allow-listed channel
user who is NOT the owner: ``request["user"]`` is a channel id and
``request["app"]`` is ``""``. That is the session ``slack/allowlist.py`` mints
with ``generate_token(user_id)``. A route counts as gated only when both hold:

* ``source_providers.is_owner_dashboard_request`` ran during the request. Every
  owner helper funnels into it: ``require_owner_dashboard_request``, the
  per-module ``_require_owner`` wrappers, ``_require_monitor_owner``, and the
  ``_guard`` functions of the cloud, instances and tailnet modules.
* The answer was 401 or 403. A handler that asks the question and then ignores
  the answer is not gated.

A route that fails both is allowed only when ``owner_gate_route_exemptions.json``
names it. ``exempt`` rows are routes that must not carry the gate, each with its
reason. An exempt route must still refuse the non-owner: 401 or 403, or the exact
status and error code its row pins under ``expect`` (a fail-closed answer given
before any mutation). ``known_ungated`` rows are counted debt: they may only be
removed. A row whose route is now gated, or is not registered at all, fails the
test until the row is deleted.

The identity comes from a test middleware, not the real ``token_auth_middleware``.
The real one admits a loopback request carrying a valid dashboard cookie and
publishes the same two claims, so the handler sees an identical request. The
middleware also adds ``X-Forwarded-For``, so the request looks like a channel user
arriving through the product's tunnel rather than a direct local caller.

The walk runs real handler bodies, so it is sealed. HOME and the data home point
at a temporary directory, every process-creating and signal call raises, sockets
may reach only the test server, writes outside the temporary roots are refused,
and ``mcp_discovery.probe_all`` is replaced so no MCP server can start.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew

_DATA_FILE = Path(__file__).with_name("owner_gate_route_exemptions.json")

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_OWNER = "U0OWNER"
_NON_OWNER = "U0NONOWNER"
_REFUSED = frozenset({401, 403})

#: A channel user reaches the gateway through the product's tunnel or reverse
#: proxy, which attach forwarding headers. Without one the loopback test client
#: would pass every "direct local caller" check a remote user cannot.
_REMOTE_PEER = "203.0.113.7"
_REQUEST_TIMEOUT_S = 5.0

#: Routes the walk must reach, so a registrar refactor cannot make it vacuous.
_MUST_BE_WALKED = frozenset(
    {
        ("POST", "/api/hooks"),
        ("POST", "/api/spawn"),
        ("POST", "/api/crons"),
        ("POST", "/api/autonudge"),
        ("PUT", "/api/taskrunner/{task_id}/plan"),
        ("POST", "/api/taskrunner/{task_id}/execute"),
        ("PUT", "/api/computer-use/config"),
        ("PUT", "/api/memory/preferences"),
        ("POST", "/api/update"),
        ("POST", "/api/chat/slots/{slot}/side/turn"),
    }
)

#: The owner predicates in ``handlers/source_providers``. Every owner helper in the
#: dashboard funnels into the first; the second is the source-provider routes' own
#: exact-owner check, which compares ``owner_id`` itself.
_OWNER_GATES = ("is_owner_dashboard_request", "_authorize_owner_request")

#: Every call in ``start_dashboard`` that mounts routes, in its order. The census
#: calls each one; ``test_registrars_match_start_dashboard`` fails when the two drift.
_ROUTE_REGISTRARS = (
    "_register_mcp_routes",
    "register_all",
    "_register_deploy_routes",
    "setup_knowledge_routes",
    "setup_weixin_routes",
    "setup_feedback_routes",
    "setup_secrets_routes",
    "setup_whatsapp_routes",
    "setup_link_meta_routes",
    "contribute_routes",
)

#: Route-mounting calls the census leaves out, each with why it holds no mutating route.
_REGISTRARS_NOT_WALKED = {
    "_register_dist_static_routes": "serves the built SPA: GET and HEAD only",
}

#: The number of ``known_ungated`` rows. It may only go down: deleting a row means
#: lowering this number in the same change, and adding a row means raising it,
#: which a reviewer sees.
_KNOWN_UNGATED_CEILING = 439

#: The walk must see at least this many mutating routes.
_MIN_MUTATING_ROUTES = 400


class _SealBreach(RuntimeError):
    """Raised by every sealed call, so a handler cannot leave the sandbox."""


@dataclass
class _Probe:
    method: str
    path: str
    handler: str
    status: int = 0
    body: str = ""
    gate_calls: int = 0
    breaches: list[str] = field(default_factory=list)
    timed_out: bool = False

    @property
    def gated(self) -> bool:
        return self.gate_calls > 0 and self.status in _REFUSED

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)


def _load_lists() -> tuple[dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], str]]:
    data = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    exempt = {(row["method"], row["path"]): row for row in data["exempt"]}
    for row in data["exempt"]:
        assert row.get("reason"), f"exempt row without a reason: {row}"
    debt = {(row["method"], row["path"]): row["module"] for row in data["known_ungated"]}
    assert len(exempt) == len(data["exempt"]), "duplicate exempt row"
    assert len(debt) == len(data["known_ungated"]), "duplicate known_ungated row"
    return exempt, debt


def _build_app() -> web.Application:
    """The dashboard's route table on a bare app, plus the non-owner identity."""
    from kiro_crew.dashboard import server
    from kiro_crew.platform.context import current_context

    @web.middleware
    async def non_owner(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = _NON_OWNER
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[non_owner])
    state = MagicMock(name="DashboardState")
    state.owner_id = _OWNER
    app["state"] = state
    for name in _ROUTE_REGISTRARS:
        if name == "contribute_routes":
            current_context().dashboard.contribute_routes(app)
        else:
            getattr(server, name)(app)
    return app


def _route_path(route: web.AbstractRoute) -> str:
    info = route.resource.get_info() if route.resource is not None else {}
    return str(info.get("path") or info.get("formatter") or "")


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _handler_name(handler: Any) -> str:
    return f"{getattr(handler, '__module__', '?')}.{getattr(handler, '__qualname__', '?')}"


class _Seal:
    """Every exit from the sandbox, patched for the duration of the walk."""

    def __init__(self, mp: pytest.MonkeyPatch, roots: tuple[str, ...]) -> None:
        self.mp = mp
        self.roots = roots
        self.port = 0
        self.current: _Probe | None = None
        self.gate_calls = 0

    def _breach(self, what: str) -> None:
        if self.current is not None:
            self.current.breaches.append(what)
        raise _SealBreach(f"sealed in the owner-gate census: {what}")

    def _inside(self, target: Any) -> bool:
        try:
            resolved = os.path.realpath(os.fspath(target))
        except TypeError:
            return True  # an fd, not a path
        if resolved == os.devnull:
            return True
        return any(resolved == r or resolved.startswith(r + os.sep) for r in self.roots)

    def install(self) -> None:
        mp = self.mp
        breach = self._breach

        def refuse(name: str, real: Any = None):
            def _refused(*a: Any, **k: Any) -> Any:
                # pytest-timeout's watchdog thread ends a hung run with os._exit.
                # It is not handler code, so it keeps the real call.
                if real is not None and threading.current_thread().name.startswith(
                    "pytest_timeout"
                ):
                    return real(*a, **k)
                breach(name)

            return _refused

        mp.setattr(subprocess.Popen, "_execute_child", refuse("subprocess"))
        for name in (
            "system",
            "fork",
            "forkpty",
            "posix_spawn",
            "posix_spawnp",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
            "kill",
            "killpg",
            "_exit",
            "abort",
        ):
            if hasattr(os, name):
                keep = getattr(os, name) if name in ("_exit", "abort") else None
                mp.setattr(os, name, refuse(f"os.{name}", keep))
        if hasattr(signal, "raise_signal"):
            mp.setattr(signal, "raise_signal", refuse("signal.raise_signal"))
        mp.setattr(sys, "exit", refuse("sys.exit"))

        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex
        seal = self

        def _allowed(sock: socket.socket, address: Any) -> bool:
            if sock.family not in (socket.AF_INET, socket.AF_INET6):
                return False
            host, port = address[0], address[1]
            return host in ("127.0.0.1", "::1", "localhost") and port == seal.port

        def connect(sock: socket.socket, address: Any) -> Any:
            if not _allowed(sock, address):
                breach(f"socket.connect {address!r}")
            return real_connect(sock, address)

        def connect_ex(sock: socket.socket, address: Any) -> Any:
            if not _allowed(sock, address):
                breach(f"socket.connect_ex {address!r}")
            return real_connect_ex(sock, address)

        mp.setattr(socket.socket, "connect", connect)

        real_getaddrinfo = socket.getaddrinfo

        def getaddrinfo(host: Any, *a: Any, **k: Any) -> Any:
            if host not in (None, "127.0.0.1", "::1", "localhost"):
                breach(f"socket.getaddrinfo {host!r}")
            return real_getaddrinfo(host, *a, **k)

        mp.setattr(socket, "getaddrinfo", getaddrinfo)
        mp.setattr(socket.socket, "connect_ex", connect_ex)

        real_open = builtins.open
        real_os_open = os.open

        def sealed_open(file: Any, mode: str = "r", *a: Any, **k: Any) -> Any:
            if any(c in mode for c in "wax+") and not seal._inside(file):
                breach(f"open({file!r}, {mode!r})")
            return real_open(file, mode, *a, **k)

        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

        def sealed_os_open(path: Any, flags: int, *a: Any, **k: Any) -> Any:
            if flags & write_flags and not seal._inside(path):
                breach(f"os.open({path!r})")
            return real_os_open(path, flags, *a, **k)

        mp.setattr(builtins, "open", sealed_open)
        mp.setattr(os, "open", sealed_os_open)

        for name in ("replace", "rename", "remove", "unlink", "rmdir", "mkdir", "symlink", "link"):
            real = getattr(os, name)

            def make(real_fn: Any, fn_name: str) -> Any:
                def sealed(*a: Any, **k: Any) -> Any:
                    for target in a[:2]:
                        if isinstance(target, (str, bytes, os.PathLike)) and not seal._inside(
                            target
                        ):
                            breach(f"os.{fn_name}({target!r})")
                    return real_fn(*a, **k)

                return sealed

            mp.setattr(os, name, make(real, name))

        from kiro_crew import mcp_discovery

        mp.setattr(mcp_discovery, "probe_all", refuse("mcp_discovery.probe_all"))

        import kiro_crew.sel as sel_mod

        audit = MagicMock(name="sel")
        self._rebind(sel_mod.sel, lambda *a, **k: audit)

    def _rebind(self, original: Any, replacement: Any) -> int:
        """Replace *original* under every name a ``kiro_crew`` module binds it to."""
        bound = 0
        for module in list(sys.modules.values()):
            name = getattr(module, "__name__", "") or ""
            if not name.startswith("kiro_crew"):
                continue
            for attr, value in list(vars(module).items()):
                if value is original:
                    self.mp.setattr(module, attr, replacement)
                    bound += 1
        return bound

    def install_gate_spy(self) -> int:
        """Count every call of the owner predicate, under every name it is bound to.

        Every builtin app also reads as enabled. A disabled app refuses everyone
        with ``app_disabled``, which says nothing about who may call it once the
        owner turns it on.
        """
        from kiro_crew.apps import manager
        from kiro_crew.dashboard.handlers import source_providers

        seal = self
        self._rebind(manager.is_app_enabled, lambda _name: True)
        bound = 0
        for name in _OWNER_GATES:
            original = getattr(source_providers, name)

            def spy(*a: Any, _original: Any = original, **k: Any) -> Any:
                seal.gate_calls += 1
                return _original(*a, **k)

            bound += self._rebind(original, spy)
        return bound


_KIRO_CREW_ROOT = os.path.dirname(os.path.realpath(kiro_crew.__file__ or ""))


def _started_by_kiro_crew(task: asyncio.Task[Any]) -> bool:
    code = getattr(task.get_coro(), "cr_code", None)
    filename = os.path.realpath(getattr(code, "co_filename", "") or "")
    return filename.startswith(_KIRO_CREW_ROOT + os.sep)


async def _walk(seal: _Seal) -> list[_Probe]:
    app = _build_app()
    bound = seal.install_gate_spy()
    assert bound >= 1, "the owner predicate was not found under any name"
    probes: list[_Probe] = []
    for route in app.router.routes():
        if route.method not in _MUTATING:
            continue
        probes.append(_Probe(route.method, _route_path(route), _handler_name(route.handler)))

    server = TestServer(app)
    async with TestClient(server) as client:
        seal.port = server.port or 0
        seal.install()
        for probe in probes:
            seal.current = probe
            seal.gate_calls = 0
            before = asyncio.all_tasks()
            # A fresh connection per probe: a handler that answers without reading the
            # body leaves a kept-alive connection the next request would stall on.
            kwargs: dict[str, Any] = {
                "headers": {
                    "X-Session-Key": "dashboard:ui",
                    "Connection": "close",
                    "X-Forwarded-For": _REMOTE_PEER,
                }
            }
            if probe.method != "DELETE":
                kwargs["json"] = {}
            try:
                async with asyncio.timeout(_REQUEST_TIMEOUT_S):
                    resp = await client.request(probe.method, _concrete(probe.path), **kwargs)
                    probe.status = resp.status
                    probe.body = (await resp.text(errors="replace"))[:300]
            except TimeoutError:
                probe.timed_out = True
            except Exception as exc:  # the transport broke; the route is not gated
                probe.breaches.append(f"client error {type(exc).__name__}")
            probe.gate_calls = seal.gate_calls
            # Cancel only what the handler started. The event loop and aiohttp start
            # tasks of their own during a request: Windows' proactor loop arms the
            # next accept as a task, and cancelling it stops the server listening.
            for task in asyncio.all_tasks() - before:
                if task is not asyncio.current_task() and _started_by_kiro_crew(task):
                    task.cancel()
            seal.current = None
    return probes


@pytest.fixture(scope="module")
def census(tmp_path_factory: pytest.TempPathFactory) -> list[_Probe]:
    home = tmp_path_factory.mktemp("owner-gate-census-home")
    roots = (os.path.realpath(home),)
    with pytest.MonkeyPatch.context() as mp:
        for var in ("HOME", "USERPROFILE"):
            mp.setenv(var, str(home))
        mp.setenv("KIROCREW_HOME", str(home / ".kiro" / "crew"))
        for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            mp.setenv(var, str(home / var.lower()))
        (home / "tmp").mkdir()
        mp.setenv("TMPDIR", str(home / "tmp"))
        mp.setattr(tempfile, "tempdir", str(home / "tmp"))
        mp.chdir(home)
        seal = _Seal(mp, roots)
        return asyncio.run(_walk(seal))


def _start_dashboard_route_calls() -> list[str]:
    """The route-mounting calls in ``start_dashboard``'s own body, in order."""
    import ast
    import inspect
    import textwrap

    from kiro_crew.dashboard import server

    tree = ast.parse(textwrap.dedent(inspect.getsource(server.start_dashboard)))
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "register_all" or name.endswith("_routes"):
            names.append(name)
    return names


def test_registrars_match_start_dashboard() -> None:
    """The census walks every route registrar the real gateway calls."""
    called = set(_start_dashboard_route_calls())
    walked = set(_ROUTE_REGISTRARS) | set(_REGISTRARS_NOT_WALKED)
    assert called == walked, (
        f"start_dashboard mounts routes through {sorted(called - walked)} which the "
        f"census does not call; add them to _ROUTE_REGISTRARS. Stale entries: "
        f"{sorted(walked - called)}"
    )


def test_walk_is_not_vacuous(census: list[_Probe]) -> None:
    walked = {p.key for p in census}
    assert len(walked) >= _MIN_MUTATING_ROUTES, f"only {len(walked)} mutating routes walked"
    missing = _MUST_BE_WALKED - walked
    assert not missing, f"the registrars no longer register {sorted(missing)}"


def test_every_probe_got_an_answer(census: list[_Probe]) -> None:
    """The harness reached every route. A probe with no HTTP answer proves nothing."""
    silent = sorted(
        f"{p.method} {p.path}: {'timed out' if p.timed_out else p.breaches or 'no status'}"
        for p in census
        if p.status == 0
    )
    assert not silent, f"{len(silent)} probe(s) got no HTTP answer:\n" + "\n".join(silent)


def test_the_gate_spy_sees_a_gated_route(census: list[_Probe]) -> None:
    """A control: a route known to call the shared gate reads as gated."""
    by_key = {p.key: p for p in census}
    for key in (("POST", "/api/hooks"), ("POST", "/api/spawn"), ("POST", "/api/crons")):
        probe = by_key[key]
        assert (
            probe.gated
        ), f"{key} should read as gated: status={probe.status} calls={probe.gate_calls}"


def test_every_mutating_route_is_gated_or_named(census: list[_Probe]) -> None:
    exempt, debt = _load_lists()
    failures = []
    for probe in census:
        if probe.gated or probe.key in exempt or probe.key in debt:
            continue
        observed = "timed out" if probe.timed_out else f"answered {probe.status}"
        failures.append(
            f"{probe.method} {probe.path} ({probe.handler}) {observed} to a non-owner "
            "without reaching is_owner_dashboard_request. Fix: call "
            "require_owner_dashboard_request(request, op) first, or add an EXEMPT row "
            "with a reason."
        )
    assert not failures, f"{len(failures)} ungated mutating route(s):\n" + "\n".join(failures)


def _refused_as_declared(probe: _Probe, row: dict[str, Any]) -> bool:
    expect = row.get("expect")
    if expect is None:
        return probe.status in _REFUSED
    try:
        code = json.loads(probe.body).get("code")
    except (ValueError, AttributeError):
        code = None
    return probe.status == expect["status"] and code == expect["code"]


def test_exempt_routes_refuse_the_non_owner(census: list[_Probe]) -> None:
    """An exempt row names a route that refuses by another credential, never an open one."""
    exempt, _debt = _load_lists()
    open_exempt = sorted(
        f"{p.method} {p.path} answered {p.status}"
        for p in census
        if p.key in exempt and not _refused_as_declared(p, exempt[p.key])
    )
    assert not open_exempt, (
        "these exempt routes did not refuse a non-owner; an exemption may only name a "
        f"route another credential check closes: {open_exempt}"
    )


def test_named_routes_still_exist(census: list[_Probe]) -> None:
    exempt, debt = _load_lists()
    walked = {p.key for p in census}
    stale = sorted((set(exempt) | set(debt)) - walked)
    assert not stale, f"these rows name no registered route; delete them: {stale}"


def test_known_ungated_only_shrinks(census: list[_Probe]) -> None:
    _exempt, debt = _load_lists()
    now_gated = sorted(p.key for p in census if p.key in debt and p.gated)
    assert (
        not now_gated
    ), f"these routes are gated now; delete their known_ungated rows: {now_gated}"


def test_known_ungated_matches_its_ceiling() -> None:
    _exempt, debt = _load_lists()
    assert len(debt) <= _KNOWN_UNGATED_CEILING, (
        f"known_ungated grew to {len(debt)} rows (ceiling {_KNOWN_UNGATED_CEILING}); "
        "gate the new route instead of listing it"
    )
    assert len(debt) >= _KNOWN_UNGATED_CEILING, (
        f"known_ungated shrank to {len(debt)} rows; lower _KNOWN_UNGATED_CEILING to "
        f"{len(debt)} so it cannot grow back"
    )


def test_debt_count_is_reported(census: list[_Probe], capsys: pytest.CaptureFixture[str]) -> None:
    exempt, debt = _load_lists()
    with capsys.disabled():
        print(
            f"\nowner-gate census: {len(census)} mutating routes, "
            f"{sum(p.gated for p in census)} gated, {len(exempt)} exempt, "
            f"{len(debt)} known ungated"
        )
    assert exempt.keys().isdisjoint(debt.keys()), "a route is both exempt and known ungated"
