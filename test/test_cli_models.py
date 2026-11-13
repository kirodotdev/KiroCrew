"""``kirocrew models`` lists, and checks, the names ``--model`` accepts.

The list is the gateway's own ``GET /api/models`` answer, read through the
owner-verified secret send; these tests stand in for that send and pin what the
command prints and the exit code a script reads.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import cli_models
from kiro_crew.cli_server_peer import _GatewayPeerRefused

_ROWS = [
    {"model_name": "auto", "display_name": "Auto", "description": "Backend default"},
    {"model_name": "claude-sonnet-4.5", "display_name": "Claude Sonnet 4.5", "description": ""},
    {"model_name": "claude-haiku-4.5", "description": "fast"},
    {"display_name": "no id"},
]


class _Resp:
    def __init__(self, payload: Any) -> None:
        self._raw = json.dumps(payload).encode()

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._raw


def _run(argv: list[str], answer: Any, default: str = "auto") -> tuple[int, str, str, list]:
    """Run ``kirocrew models <argv>`` against a stand-in gateway answer."""
    sent: list[Any] = []

    def send(req: Any, port: int, timeout: float, operation: str) -> Any:
        sent.append((req.full_url, operation))
        if isinstance(answer, BaseException):
            raise answer
        return _Resp(answer)

    out, err = io.StringIO(), io.StringIO()
    with (
        patch.object(sys, "argv", ["kirocrew", "models", *argv]),
        patch.object(cli_models, "_gateway_send", send),
        patch.object(cli_models, "_internal_secret", return_value=""),
        patch.object(cli_models, "_client_port", return_value=5476),
        patch.object(cli_models, "configured_default", return_value=default),
        patch.object(sys, "stdout", out),
        patch.object(sys, "stderr", err),
        pytest.raises(SystemExit) as exc,
    ):
        from kiro_crew.cli import main

        main()
    return int(exc.value.code or 0), out.getvalue(), err.getvalue(), sent


def test_lists_each_name_with_the_default_marked() -> None:
    code, out, _err, sent = _run([], _ROWS, default="claude-sonnet-4.5")
    assert code == 0
    assert sent == [("http://127.0.0.1:5476/api/models", "cli_models_list")]
    lines = out.splitlines()
    assert lines[0].split() == ["auto", "Auto", "(Backend", "default)"]
    assert lines[1].split()[:2] == ["*", "claude-sonnet-4.5"]
    assert lines[2].split() == ["claude-haiku-4.5", "(fast)"]
    assert "no id" not in out
    assert "* configured default" in out


def test_says_when_the_configured_default_is_not_listed() -> None:
    _code, out, _err, _sent = _run([], _ROWS, default="gone-model")
    assert "Configured default gone-model is not in this list." in out


def test_json_lists_the_same_rows() -> None:
    code, out, _err, _sent = _run(["--json"], _ROWS)
    data = json.loads(out)
    assert code == 0 and data["default"] == "auto"
    assert [m["name"] for m in data["models"]] == ["auto", "claude-sonnet-4.5", "claude-haiku-4.5"]
    assert data["models"][0] == {
        "name": "auto",
        "label": "Auto",
        "description": "Backend default",
        "default": True,
    }


def test_a_listed_name_is_valid() -> None:
    code, out, _err, _sent = _run(["claude-haiku-4.5"], _ROWS)
    assert code == 0 and "claude-haiku-4.5 is a valid --model name" in out


def test_an_unknown_name_exits_1_with_the_closest_names() -> None:
    code, out, _err, _sent = _run(["Claude-Sonet-4.5", "--json"], _ROWS)
    data = json.loads(out)
    assert code == 1 and data["valid"] is False
    assert data["closest"][0] == "claude-sonnet-4.5"


def test_auto_is_always_valid() -> None:
    code, _out, _err, _sent = _run(["auto"], [{"model_name": "only-one"}])
    assert code == 0


def _refusal(code: str) -> urllib.error.HTTPError:
    body = io.BytesIO(json.dumps({"error": "sign in", "code": code}).encode())
    return urllib.error.HTTPError("u", 503, "Service Unavailable", {}, body)  # type: ignore[arg-type]


@pytest.mark.parametrize("code", ["kiro_prerequisite_required", "kas_bundle_sign_in_required"])
def test_a_signed_out_backend_is_reported_and_exits_1(code: str) -> None:
    exit_code, out, err, _sent = _run([], _refusal(code))
    assert exit_code == 1 and out == ""
    assert err.strip() == "Error: the backend is signed out: sign in"


def test_only_a_retryable_answer_invites_a_retry() -> None:
    _code, _out, err, _sent = _run([], _refusal("model_list_unavailable"))
    assert "try again" in err
    _code, _out, err, _sent = _run([], _refusal("kas_bundle_not_installed"))
    assert "try again" not in err


def test_an_unproven_gateway_is_refused() -> None:
    code, _out, err, _sent = _run([], _GatewayPeerRefused("not the recorded gateway"))
    assert code == 1 and "gateway ownership not verified" in err


@pytest.mark.asyncio
async def test_the_gateway_admits_the_cli_request_and_still_refuses_a_bare_one() -> None:
    """Drive the REAL token middleware with the CLI's own request shape.

    The CLI sends only ``X-Internal-Secret`` from loopback; ``/api/models`` must
    take it as the cookie-authed picker's twin, and refuse a wrong secret.
    """
    from aiohttp import web

    from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
    from kiro_crew.dashboard.token_auth import token_auth_middleware

    mw = token_auth_middleware(
        internal_paths=_STRICT_INTERNAL_API_PATHS,
        mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
        internal_secret="s3cret",
    )
    reached: list[str] = []

    async def _handler(request: web.Request) -> web.Response:
        reached.append(request.path)
        return web.json_response([])

    def _req(headers: dict[str, str]) -> Any:
        req = MagicMock(spec=web.Request)
        req.path, req.query, req.cookies = "/api/models", {}, {}
        req.remote, req.headers, req.method = "127.0.0.1", headers, "GET"
        return req

    assert (await mw(_req({"X-Internal-Secret": "s3cret"}), _handler)).status == 200
    assert (await mw(_req({"X-Internal-Secret": "wrong"}), _handler)).status in (401, 403)
    assert (await mw(_req({}), _handler)).status in (401, 403)
    assert reached == ["/api/models"]


def test_no_gateway_is_reported() -> None:
    code, _out, err, _sent = _run([], ConnectionRefusedError())
    assert code == 1 and "gateway not running" in err
