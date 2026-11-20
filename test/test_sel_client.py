"""The app backend's client for the gateway's ``_sel`` route, end to end.

``test_sel_routes.py`` pins the route with real app tokens. These tests put the
backend side on the other end of it: a client that holds only the per-app secret
reaches the log through the real route and the real token exchange, and no SEL key
is read or created in the backend.
"""

# flake8: noqa: F811  -- ``rec`` is a fixture imported from test_sel_routes
from __future__ import annotations

import asyncio
import http.client
import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_sel_routes import (  # noqa: F401  (``rec`` is the route tests' fixture)
    _A,
    _CLOCK,
    _SECRET_OPERATIONS,
    no_part_of,
    rec,
)

from kiro_crew.apps import protected_app_state, sel_routes
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.handlers.core import api_app_token
from kiro_crew.sel import sel as _real_sel  # the ``rec`` fixture replaces ``kiro_crew.sel.sel``
from kiro_crew.testing.wait import wait_until

_SETTLE_SECS = 30.0


def settle(backend: Any) -> None:
    """Wait, bounded, until the sender has finished every queued row; fail by name if not."""
    backend._wait_for_queue(_SETTLE_SECS)
    assert not backend._queue.unfinished_tasks, "the route client's sender did not finish its rows"


@pytest.fixture
def gateway(rec, tmp_path, monkeypatch):
    """The real route and token exchange; the owner-enable marker is faked."""
    _CLOCK.now += 1.0  # the exchange mints under ``rec``'s clock, strictly after the app record
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    protected_app_state.write_secret(_A, "per-app-secret")
    monkeypatch.setattr("kiro_crew.apps.host_exec_grants.host_exec_granted", lambda _n: True)
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    sel_routes.register_sel_routes(app)
    app.router.add_post("/api/apps/{name}/token", api_app_token)
    return app


@pytest.mark.asyncio
async def test_selclient_writes_through_the_route_with_no_key_in_the_backend(
    gateway, rec, tmp_path
) -> None:
    from kiro_crew.apps import sel_client  # imported here: the module is new in this change

    async with TestClient(TestServer(gateway)) as client:
        origin = str(client.make_url("")).rstrip("/")
        backend = sel_client.SelClient(_A, origin, "per-app-secret")
        await asyncio.to_thread(
            backend.log_api_access, operation="x.y", outcome="ok", critical=True
        )
        with pytest.raises(sel_client.SelClientError):
            bad = sel_client.SelClient(_A, origin, "wrong-secret")
            await asyncio.to_thread(bad.log_api_access, operation="x", outcome="ok", critical=True)
    ours = [kw for _n, kw in rec.app_rows() if kw["operation"] == f"{_A}.x.y"]
    assert [kw["caller"] for kw in ours] == [f"app:{_A}"]
    assert not [kw for _n, kw in rec.app_rows() if kw["operation"] == f"{_A}.x"]  # wrong secret
    assert not (tmp_path / "home" / "trust" / "sel_hmac.key").exists()
    source = Path(sel_client.__file__).read_text(encoding="utf-8")
    assert "sel_hmac" not in source and "SecurityEventLog" not in source


@pytest.mark.asyncio
async def test_a_spawned_backends_sel_write_reaches_the_log_with_no_key_in_the_process(
    gateway, rec, monkeypatch
) -> None:
    """What a builtin backend does: ``sel().log_api_access(caller=..., source=...)``."""
    key_holder_calls: list[str] = []

    class _KeyHolder:
        """Stands in for ``SecurityEventLog``: any use means the process holds the key."""

        def __init__(self) -> None:
            key_holder_calls.append("built")

        def log_api_access(self, **_kw: Any) -> None:
            key_holder_calls.append("wrote")

    monkeypatch.setattr("kiro_crew.sel.SecurityEventLog", _KeyHolder)
    async with TestClient(TestServer(gateway)) as client:
        monkeypatch.setenv("KIROCREW_SPAWNED", "1")
        monkeypatch.setenv("KIROCREW_APP_NAME", _A)
        monkeypatch.setenv("KIROCREW_GATEWAY_ORIGIN", str(client.make_url("")).rstrip("/"))
        monkeypatch.setenv("KIROCREW_PROXY_SECRET", "per-app-secret")
        await asyncio.to_thread(
            _real_sel().log_api_access,
            caller="core:somebody-else",
            source="dashboard",
            operation="x.y",
            outcome="ok",
            critical=True,
        )
    assert key_holder_calls == []
    # The token exchange writes its own app rows (first mint), so pick this write out.
    ours = [kw for _n, kw in rec.app_rows() if kw["operation"] == f"{_A}.x.y"]
    assert [(kw["caller"], kw["source"]) for kw in ours] == [(f"app:{_A}", "app")]


@pytest.mark.asyncio
async def test_a_backends_tool_invocation_becomes_an_app_tool_row(gateway, rec) -> None:
    """The call the builtin backends make today; every gateway-only field is dropped."""
    from kiro_crew.apps import sel_client

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        await asyncio.to_thread(
            backend.log_tool_invocation,
            session_key="someone-elses-session",
            agent="kirocrew",
            source="dashboard",
            tool_name="Git Push!",
            tool_kind="subprocess",
            outcome="failure",
            request_id=7,
            downstream_service="x",
            error="boom",
            metadata={"rc": 1},
            critical=True,
        )
    ours = [kw for _n, kw in rec.app_rows() if kw["operation"].startswith(f"{_A}.tool.")]
    assert [
        (kw["caller"], kw["source"], kw["operation"], kw["outcome"], kw["error"]) for kw in ours
    ] == [(f"app:{_A}", "app", f"{_A}.tool.git_push_", "failure", "boom")]
    assert not [name for name, _kw in rec.rows if name != "log_api_access"]


_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop"
_PAT = (
    "github_pat_11ABCDEFG0aBcDeFgHiJkL_"
    "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcDeFgHiJkLmNoPqRsTuVw"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [_JWT, _PAT])
async def test_client_tool_name_is_redacted_before_lowercase(gateway, rec, secret) -> None:
    """Lower-casing first would turn ``eyJ`` into ``eyj`` and hide the token from redaction."""
    from kiro_crew.apps import sel_client
    from kiro_crew.testing.wait import async_wait_until

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        await asyncio.to_thread(
            backend.log_tool_invocation, tool_name=f"deploy {secret}", outcome="ok", critical=True
        )
        await async_wait_until(lambda: bool(rec.app_rows()))
    (row,) = rec.app_rows()
    stored = row[1]["operation"]
    assert stored.startswith(f"{_A}.tool.deploy_") and "\n" not in stored
    assert secret.lower() not in stored.lower()
    assert secret[:16].lower() not in stored.lower()


@pytest.mark.asyncio
async def test_a_builtins_existing_mixed_case_operation_lands_with_a_critical_write(
    gateway, rec
) -> None:
    """md-notebook's auto-sync audit row is ``critical``: refusing it would block the setting."""
    from kiro_crew.apps import sel_client

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        await asyncio.to_thread(
            backend.log_api_access,
            operation="md_notebook.settings.autoSync",
            outcome="enabled",
            critical=True,
        )
    assert [kw["operation"] for _n, kw in rec.app_rows() if "autoSync" in kw["operation"]] == [
        f"{_A}.md_notebook.settings.autoSync"
    ]


@pytest.mark.asyncio
async def test_the_client_does_not_use_a_system_proxy(gateway, rec, monkeypatch) -> None:
    """The secret and the token go to the gateway, never to a proxy from the environment."""
    from kiro_crew.apps import sel_client

    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:1")  # nothing listens: a proxied call fails
    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        await asyncio.to_thread(
            backend.log_api_access, operation="x.proxy", outcome="ok", critical=True
        )
    assert [kw["caller"] for _n, kw in rec.app_rows() if kw["operation"] == f"{_A}.x.proxy"] == [
        f"app:{_A}"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize("field", ["resources", "error", "outcome"])
async def test_an_oversized_field_is_redacted_then_clipped_and_the_row_is_not_dropped(
    gateway, rec, field, critical
) -> None:
    """20,000 characters would pass the route's 16 KiB body cap (a 413, row lost)."""
    from kiro_crew.apps import sel_client
    from kiro_crew.testing.wait import async_wait_until

    # A key straddling the clip point: clipping before redaction would leave its prefix.
    huge = "x" * 489 + " AKIAIOSFODNN7EXAMPLE " + "y" * 20_000
    kwargs: dict[str, Any] = {"operation": "x.big", "outcome": "ok", "critical": critical}
    kwargs[field] = huge
    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        await asyncio.to_thread(lambda: backend.log_api_access(**kwargs))
        await async_wait_until(
            lambda: any(kw["operation"] == f"{_A}.x.big" for _n, kw in rec.app_rows())
        )
        await asyncio.to_thread(settle, backend)
    (row,) = [kw for _n, kw in rec.app_rows() if kw["operation"] == f"{_A}.x.big"]
    assert len(row[field]) <= 500
    assert "AKIA" not in row[field]


@pytest.mark.asyncio
async def test_a_critical_row_lands_after_the_rows_queued_before_it(
    gateway, rec, monkeypatch
) -> None:
    """A queued row still being sent must not be overtaken by a later critical write."""
    from kiro_crew.apps import sel_client

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        real_send = backend._send
        release = threading.Event()

        def held_first(body: dict[str, Any]) -> None:
            if body["kwargs"]["operation"] == "x.first":
                release.wait(5)  # the queued row is still in flight when the critical one starts
            real_send(body)

        monkeypatch.setattr(backend, "_send", held_first)
        timer = threading.Timer(0.3, release.set)
        timer.start()
        await asyncio.to_thread(backend.log_api_access, operation="x.first", outcome="ok")
        await asyncio.to_thread(
            backend.log_api_access, operation="x.second", outcome="ok", critical=True
        )
    timer.join()
    assert [kw["operation"] for _n, kw in rec.app_rows()] == [f"{_A}.x.first", f"{_A}.x.second"]


@pytest.mark.asyncio
async def test_a_row_of_astral_characters_fits_the_routes_body_cap(gateway, rec) -> None:
    """Three 500-character fields are 18 KiB as ``\\uXXXX`` escapes but 6 KiB as UTF-8."""
    from kiro_crew.apps import sel_client

    astral = "\U0001f600" * 500
    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        # critical: a refused row (a 413 from the cap) would raise here
        await asyncio.to_thread(
            backend.log_api_access,
            operation="x.astral",
            outcome=astral,
            resources=astral,
            error=astral,
            critical=True,
        )
    (row,) = [kw for _n, kw in rec.app_rows() if kw["operation"] == f"{_A}.x.astral"]
    assert (row["outcome"], row["resources"], row["error"]) == (astral, astral, astral)


@pytest.mark.asyncio
async def test_a_lone_surrogate_in_a_field_lands_and_does_not_raise(gateway, rec) -> None:
    """A path read with surrogateescape holds one; it is replaced, never an encode error."""
    from kiro_crew.apps import sel_client

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        await asyncio.to_thread(
            backend.log_api_access,
            operation="x.surrogate",
            outcome="ok",
            resources="a\udc80b",
            critical=True,
        )
    (row,) = [kw for _n, kw in rec.app_rows() if kw["operation"] == f"{_A}.x.surrogate"]
    assert row["resources"] == "a?b"


@pytest.mark.asyncio
@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize(
    ("operation", "stored"),
    [
        ("Publish Doc", "Publish_Doc"),
        ("a\nb\x00c\x1b[31md", "a_b_c__31md"),
        ("x" * 300, "x" * 64),
        ("", "unnamed"),
    ],
)
async def test_an_operation_is_normalized_and_the_row_is_kept(
    gateway, rec, critical, operation, stored
) -> None:
    from kiro_crew.apps import sel_client
    from kiro_crew.testing.wait import async_wait_until

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        await asyncio.to_thread(
            backend.log_api_access, operation=operation, outcome="ok", critical=critical
        )
        await async_wait_until(lambda: bool(rec.app_rows()))
        await asyncio.to_thread(settle, backend)
    assert [kw["operation"] for _n, kw in rec.app_rows()] == [f"{_A}.{stored}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize(("operation", "secret"), _SECRET_OPERATIONS)
async def test_a_credential_in_the_operation_is_redacted_before_it_is_normalized(
    gateway, rec, critical, operation, secret
) -> None:
    from kiro_crew.apps import sel_client
    from kiro_crew.testing.wait import async_wait_until

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        await asyncio.to_thread(
            backend.log_api_access, operation=operation, outcome="ok", critical=critical
        )
        await async_wait_until(lambda: bool(rec.app_rows()))
        await asyncio.to_thread(settle, backend)
    (row,) = rec.app_rows()
    stored = row[1]["operation"]
    assert no_part_of(secret, stored)
    assert stored.startswith(f"{_A}.") and "\n" not in stored


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("Publish Doc", "Publish_Doc"),
        ("a\nb\tc\x7fd", "a_b_c_d"),
        ("keep.this-1_ok", "keep.this-1_ok"),
        ("y" * 65, "y" * 64),
        ("", "unnamed"),
    ],
)
def test_normalize_operation(raw, stored) -> None:
    from kiro_crew.apps import sel_client

    assert sel_client.normalize_operation(raw) == stored


def test_an_unsupported_method_warns_once_and_does_not_raise(caplog) -> None:
    """A gateway-only writer (not decision/degraded): say so, never an AttributeError."""
    from kiro_crew.apps import sel_client

    backend = sel_client.SelClient(_A, "http://127.0.0.1:1", "per-app-secret")
    for _ in range(3):
        backend.log_sel_rotation(segment="a")
    backend.log_something_else(reason="y")
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 2
    assert "log_sel_rotation" in warned[0] and "log_something_else" in warned[1]
    with pytest.raises(AttributeError):
        backend._private  # noqa: B018 -- only public writer names are swallowed


@pytest.mark.asyncio
@pytest.mark.parametrize("critical", [False, True])
async def test_a_governance_decision_becomes_an_app_attributed_api_access_row(
    gateway, rec, critical
) -> None:
    """The boot permission decisions of Dev Fleet / File Explorer / Workflows leave a row."""
    from kiro_crew.apps import sel_client
    from kiro_crew.testing.wait import async_wait_until

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        await asyncio.to_thread(
            lambda: backend.log_governance_decision(
                session_key="someone-elses",
                agent="kirocrew",
                tool_name="app_select",
                scope="app",
                item="/x/ AKIAIOSFODNN7EXAMPLE ",
                outcome="denied",
                rule="r1",
                layer="L2",
                reason="not selectable",
                critical=critical,
            )
        )
        await async_wait_until(
            lambda: any(kw["operation"] == f"{_A}.governance.decision" for _n, kw in rec.app_rows())
        )
        await asyncio.to_thread(settle, backend)
    name, row = [
        (n, kw) for n, kw in rec.app_rows() if kw["operation"] == f"{_A}.governance.decision"
    ][0]
    assert name == "log_api_access" and row["caller"] == f"app:{_A}" and row["outcome"] == "denied"
    assert all(part in row["resources"] for part in ("app_select", "r1", "L2", "not selectable"))
    assert "AKIA" not in row["resources"]  # the details go through the writer's redactor
    assert row.get("critical", False) is critical
    assert not [n for n, _kw in rec.rows if n != "log_api_access"]  # no new event kind


@pytest.mark.asyncio
async def test_a_governance_degraded_becomes_an_app_attributed_api_access_row(gateway, rec) -> None:
    from kiro_crew.apps import sel_client
    from kiro_crew.testing.wait import async_wait_until

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        await asyncio.to_thread(
            lambda: backend.log_governance_degraded(
                session_key="s", chokepoint="spawn_gate", scope="app", reason="boom"
            )
        )
        await async_wait_until(
            lambda: any(kw["operation"] == f"{_A}.governance.degraded" for _n, kw in rec.app_rows())
        )
        await asyncio.to_thread(settle, backend)
    (row,) = [kw for _n, kw in rec.app_rows() if kw["operation"] == f"{_A}.governance.degraded"]
    assert row["outcome"] == "failed_open" and "spawn_gate" in row["resources"]


def test_the_sender_registers_an_exit_drain_once(monkeypatch) -> None:
    from kiro_crew.apps import sel_client

    registered: list[Any] = []
    monkeypatch.setattr(sel_client.atexit, "register", registered.append)
    backend = sel_client.SelClient(_A, "http://127.0.0.1:1", "per-app-secret")
    backend._send = lambda _body: None  # type: ignore[method-assign]  # nothing is sent
    backend._enqueue({"method": "log_api_access", "kwargs": {"operation": "x", "outcome": "ok"}})
    backend._enqueue({"method": "log_api_access", "kwargs": {"operation": "y", "outcome": "ok"}})
    assert registered == [backend.flush_at_exit]
    backend._queue.join()  # settle: no worker work outlives the test


@pytest.mark.asyncio
async def test_the_exit_drain_sends_what_is_queued(gateway, rec, caplog) -> None:
    from kiro_crew.apps import sel_client

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        backend.log_api_access(operation="x.queued", outcome="ok")  # queued, not critical
        await asyncio.to_thread(backend.flush_at_exit, 5.0)
    assert [kw["operation"] for _n, kw in rec.app_rows() if kw["operation"].endswith("queued")] == [
        f"{_A}.x.queued"
    ]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_exit_drain_is_bounded_and_counts_what_it_could_not_send(caplog) -> None:
    from kiro_crew.apps import sel_client

    backend = sel_client.SelClient(_A, "http://127.0.0.1:1", "per-app-secret")
    gate = threading.Event()
    backend._send = lambda _body: wait_until(gate.is_set)  # type: ignore[method-assign]
    for i in range(3):  # the worker holds the first inside _send, two wait: exactly 3 pending
        backend.log_api_access(operation=f"x.lost{i}", outcome="ok")
    backend.flush_at_exit(0.3)
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    gate.set()
    backend._queue.join()  # settle: the worker finishes before the test (and its caplog) ends
    assert [m for m in warned if "not sent at exit" in m] == [
        f"app {_A}: 3 audit row(s) not sent at exit"
    ]


@pytest.mark.asyncio
async def test_a_truncated_token_response_does_not_kill_the_sender(gateway, rec, monkeypatch):
    """``IncompleteRead`` is an ``HTTPException``, not an ``OSError``: it must be a retry."""
    from kiro_crew.apps import sel_client
    from kiro_crew.testing.wait import async_wait_until

    real, calls = sel_client.loopback_urlopen, [0]

    class _Cut:
        status = 200

        def __enter__(self) -> _Cut:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def read(self) -> bytes:
            raise http.client.IncompleteRead(b"par")

    def _flaky(req: Any, timeout: float) -> Any:
        calls[0] += 1
        return _Cut() if calls[0] == 1 else real(req, timeout=timeout)

    monkeypatch.setattr(sel_client, "loopback_urlopen", _flaky)
    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        backend.log_api_access(operation="x.first", outcome="ok")
        await async_wait_until(
            lambda: any(kw["operation"] == f"{_A}.x.first" for _n, kw in rec.app_rows())
        )
        backend.log_api_access(operation="x.later", outcome="ok")  # the sender is still alive
        await async_wait_until(
            lambda: any(kw["operation"] == f"{_A}.x.later" for _n, kw in rec.app_rows())
        )
        await asyncio.to_thread(settle, backend)  # the sender is done before the server closes


@pytest.mark.asyncio
async def test_an_unexpected_error_on_one_row_does_not_end_the_sender(gateway, rec, caplog):
    from kiro_crew.apps import sel_client
    from kiro_crew.testing.wait import async_wait_until

    async with TestClient(TestServer(gateway)) as client:
        backend = sel_client.SelClient(_A, str(client.make_url("")).rstrip("/"), "per-app-secret")
        real, first = backend._send, [True]

        def _send(body: dict[str, object]) -> None:
            if first:
                first.pop()
                raise RuntimeError("not a transport error")
            real(body)

        backend._send = _send  # type: ignore[method-assign]
        backend.log_api_access(operation="x.bad", outcome="ok")
        backend.log_api_access(operation="x.good", outcome="ok")
        await async_wait_until(
            lambda: any(kw["operation"] == f"{_A}.x.good" for _n, kw in rec.app_rows())
        )
        await asyncio.to_thread(settle, backend)  # the sender is done before the server closes
    assert any(r.levelno >= logging.WARNING and _A in r.getMessage() for r in caplog.records)
