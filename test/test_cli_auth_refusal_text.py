"""A CLI refusal from the gateway's auth layer is explained, not printed as ``Forbidden``.

``token_auth`` refuses an internal-API request with ``{"error": "Forbidden", "code":
...}``. The MCP decoder has long replaced that ``Forbidden`` with what the code means;
``kirocrew spawn``, ``kirocrew artifact`` and doctor's adaptive-controller read printed
the bare word, which reads as a permission problem when the cause is a credential
from another instance. These drive the CLI against the real middleware holding a
different secret, so the body is the one the gateway really sends.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cli_commands_coverage_helpers import _http_error, _ns

from kiro_crew import cli_commands as cc
from kiro_crew import mcp_core
from kiro_crew.auth_refusal_text import AUTH_REFUSAL_TEXT, auth_refusal_text
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.loopback_http import loopback_urlopen

_MISMATCH = AUTH_REFUSAL_TEXT["internal_auth_mismatch"]


def _gateway_app() -> web.Application:
    """The spawn route behind the middleware the gateway runs, with its own secret."""

    async def _never(_request: web.Request) -> web.Response:
        raise AssertionError("the middleware let a stale credential through")

    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_paths=_STRICT_INTERNAL_API_PATHS,
                mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
                internal_secret="the-running-gateways-secret",
            )
        ]
    )
    app.router.add_get("/api/spawn", _never)
    app.router.add_post("/api/spawn", _never)
    return app


async def _run_spawn_against_the_gateway(**ns: Any) -> int:
    """Run ``kirocrew spawn`` with another instance's secret against the real middleware.

    The command blocks, so it runs on a worker thread while this loop serves it; the
    send goes straight to loopback, standing in for the peer-proven transport.
    """

    def _send(req: Any, port: int, timeout: float, operation: str) -> Any:
        return loopback_urlopen(req, timeout=timeout)

    async with TestServer(_gateway_app(), host="127.0.0.1") as server:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value="another-instance"),
            patch("kiro_crew.cli_commands._gateway_send", side_effect=_send),
            pytest.raises(SystemExit) as exited,
        ):
            await asyncio.to_thread(cc._spawn, _ns(port=int(server.port), **ns))
    return int(exited.value.code)


@pytest.mark.asyncio
async def test_spawn_list_explains_a_credential_from_another_instance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await _run_spawn_against_the_gateway(spawn_action="list")
    assert code == 1
    assert capsys.readouterr().out == f"Error: {_MISMATCH}\n"


@pytest.mark.asyncio
async def test_spawn_run_explains_a_credential_from_another_instance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await _run_spawn_against_the_gateway(
        spawn_action="run", task="do x", agent=None, timeout=60, no_wait=True
    )
    assert code == 1
    assert f"Error: {_MISMATCH}" in capsys.readouterr().out


def test_a_plain_forbidden_is_not_misdiagnosed(capsys: pytest.CaptureFixture[str]) -> None:
    """A genuine permission denial carries the same word and no code: it stays as sent."""
    err = _http_error(403, json.dumps({"error": "Forbidden"}).encode())
    with (
        patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
        patch("kiro_crew.cli_commands._gateway_send", side_effect=err),
        pytest.raises(SystemExit),
    ):
        cc._spawn(_ns(spawn_action="list", port=1234))
    assert capsys.readouterr().out == "Error: Forbidden\n"


def test_an_unknown_code_keeps_the_gateways_error(capsys: pytest.CaptureFixture[str]) -> None:
    err = _http_error(409, json.dumps({"error": "spawn cap reached", "code": "cap"}).encode())
    with (
        patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
        patch("kiro_crew.cli_commands._gateway_send", side_effect=err),
        pytest.raises(SystemExit),
    ):
        cc._spawn(_ns(spawn_action="list", port=1234))
    assert capsys.readouterr().out == "Error: spawn cap reached\n"


def test_artifact_list_explains_a_credential_from_another_instance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = json.dumps({"error": "Forbidden", "code": "internal_auth_mismatch"}).encode()
    with (
        patch("kiro_crew.cli_commands._client_port", return_value=1234),
        patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
        patch("kiro_crew.cli_commands._gateway_send", side_effect=_http_error(403, body)),
        pytest.raises(SystemExit),
    ):
        cc._artifact(_ns(artifact_action="list", type=None, limit=None, query=None))
    assert f"Error: {_MISMATCH}" in capsys.readouterr().err


@pytest.mark.parametrize("code", sorted(AUTH_REFUSAL_TEXT))
def test_the_mcp_decoder_reads_the_same_table(code: str) -> None:
    """One table, so a tool and the CLI say the same sentence for the same refusal."""
    body = json.dumps({"error": "Forbidden", "code": code}).encode()
    exc = urllib.error.HTTPError("http://127.0.0.1/api/x", 403, "Forbidden", {}, io.BytesIO(body))
    assert mcp_core._http_error_body(exc) == {"error": AUTH_REFUSAL_TEXT[code], "code": code}


@pytest.mark.parametrize("code", [None, 7, ["internal_auth_mismatch"], "forbidden", ""])
def test_anything_but_a_known_code_has_no_explanation(code: object) -> None:
    assert auth_refusal_text(code) is None
