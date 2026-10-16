"""``POST /api/crons`` reads the crew roster off the event loop.

A scripted POST names a crew member in ``agent`` with no ``member_id``. The
handler classifies that name with ``split_cron_agent_member``, which loads the
configuration from disk on every call. Run directly in the ``async`` handler,
that read holds the gateway's only event loop for as long as the disk takes,
and every other session's turn waits behind it.

Two rules, each with its own cases:

* The member split runs on a worker thread, and its answer is the one the job
  is stored with
  (``test_create_naming_a_member_reads_the_configuration_off_the_loop_thread``).
* No cron route calls the configuration loader, or the helper that loads it,
  from its own frame (``test_no_cron_route_reads_the_configuration_on_the_loop``).
  The next handler to do it is caught without a new harness, and
  ``test_the_rule_tells_a_direct_read_from_a_handed_off_one`` pins what the rule
  matches, so a rule that stopped matching would fail there.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import textwrap
import threading
from collections.abc import Iterable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import _facade_mirror
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, config_dir
from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers.cron import api_crons_create
from kiro_crew.memory_stores import provision_member_memory

_CRON_ROUTE_PREFIX = "api_cron"
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


@pytest.fixture
def member_home(tmp_path, monkeypatch):
    """A data home holding one crew member whose name is not a provider template."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (config_dir() / "crons").mkdir(parents=True)
    cfg = KiroCrewConfig.load()
    cfg.agents["SampleMemberAgent"] = KiroCrewAgentConfig(
        kiro_agent="sample-template-agent", triggers="sample"
    )
    provision_member_memory(cfg, "SampleMemberAgent")
    cfg.save()


def _dashboard_app(add_job_async: Any) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(add_job_async=add_job_async),
        push_refresh=MagicMock(),
    )
    app.router.add_route("*", "/api/crons", api_crons_create)
    return app


# ── The member split runs on a worker thread ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_naming_a_member_reads_the_configuration_off_the_loop_thread(
    member_home, monkeypatch
):
    app = _dashboard_app(CronService(base_dir=config_dir()).add_job_async)
    loop_thread = threading.current_thread()
    readers: list[threading.Thread] = []
    real_load = KiroCrewConfig.load.__func__

    def recording_load(cls: type[KiroCrewConfig]) -> KiroCrewConfig:
        readers.append(threading.current_thread())
        return real_load(cls)

    with monkeypatch.context() as scoped:
        scoped.setattr(KiroCrewConfig, "load", classmethod(recording_load))
        async with TestClient(TestServer(app)) as client:
            resp = await asyncio.wait_for(
                client.post(
                    "/api/crons",
                    json={
                        "name": "daily",
                        "message": "task",
                        "every": 60,
                        "agent": "SampleMemberAgent",
                    },
                ),
                timeout=30,
            )

    assert resp.status == 200
    assert readers, "the create never read the configuration, so the lookup under test did not run"
    assert loop_thread not in readers, "POST /api/crons read the configuration on the loop thread"
    job = CronService(base_dir=config_dir()).list_jobs(include_disabled=True)[0]
    # The worker's answer is what the store receives: member in, template out.
    assert (job.member_id, job.agent_id) == ("samplememberagent", "")


# ── No cron route reads the configuration from its own frame ─────────────────


def _is_config_read(func: ast.expr) -> bool:
    """A call to ``KiroCrewConfig.load`` or to the member split that calls it."""
    if isinstance(func, ast.Name):
        return func.id == "split_cron_agent_member"
    if isinstance(func, ast.Attribute):
        if func.attr == "split_cron_agent_member":
            return True
        return (
            func.attr == "load"
            and isinstance(func.value, ast.Name)
            and func.value.id == "KiroCrewConfig"
        )
    return False


def _own_frame_calls(statements: Iterable[ast.stmt]) -> Iterator[ast.Call]:
    """Calls made by the frame itself, not by a nested def, lambda or class."""
    pending: list[ast.AST] = list(statements)
    while pending:
        node = pending.pop()
        if isinstance(node, _NESTED_SCOPES):
            continue
        if isinstance(node, ast.Call):
            yield node
        pending.extend(ast.iter_child_nodes(node))


def cron_routes(source: str) -> list[ast.AsyncFunctionDef]:
    """Every ``async def api_cron*`` in *source*: the cron routes' coroutines."""
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith(_CRON_ROUTE_PREFIX)
    ]


def config_reads_on_the_loop(source: str) -> list[str]:
    """``<route>:<line>`` for each direct config read in an ``async def api_cron*``.

    A function handed to ``asyncio.to_thread`` is a reference, not a call, so
    ``await asyncio.to_thread(KiroCrewConfig.load)`` is not a finding.
    """
    found: list[tuple[int, str]] = []
    for route in cron_routes(source):
        for call in _own_frame_calls(route.body):
            if _is_config_read(call.func):
                found.append((call.lineno, f"{route.name}:{call.lineno}"))
    return [label for _lineno, label in sorted(found)]


def test_no_cron_route_reads_the_configuration_on_the_loop():
    facade = sys.modules["kiro_crew.dashboard.handlers.cron"]
    family = (facade.__name__, *vars(facade)[_facade_mirror.INDEX_ATTRIBUTE].owners)
    source = "\n".join(inspect.getsource(sys.modules[name]) for name in family)
    routes = {route.name for route in cron_routes(source)}
    assert api_crons_create.__name__ in routes, "the rule no longer finds the cron routes"
    assert config_reads_on_the_loop(source) == []


def test_the_rule_tells_a_direct_read_from_a_handed_off_one():
    def reads(body: str) -> list[str]:
        route = "async def api_cron_probe(request):\n" + textwrap.indent(body, "    ")
        return config_reads_on_the_loop(route)

    assert reads("cfg = KiroCrewConfig.load()\n") == ["api_cron_probe:2"]
    assert reads("agent, member = split_cron_agent_member(agent, member)\n") == ["api_cron_probe:2"]
    assert reads("agent, member = identity.split_cron_agent_member(agent, member)\n") == [
        "api_cron_probe:2"
    ]
    assert reads("cfg = await asyncio.to_thread(KiroCrewConfig.load)\n") == []
    assert reads("agent, member = await asyncio.to_thread(split_cron_agent_member, a, m)\n") == []
    # A nested def or lambda is another frame, run wherever it is handed.
    assert reads("def worker():\n    return KiroCrewConfig.load()\n") == []
    assert reads("cfg = await asyncio.to_thread(lambda: KiroCrewConfig.load())\n") == []
