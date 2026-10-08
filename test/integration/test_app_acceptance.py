"""A real App Kit provider through install, trust, lifecycle, and work-ledger HTTP.

Unlike the focused acceptance tests, this module patches no application seam. It boots
the real gateway on an isolated home, installs a subprocess-backed app through the
owner API, and invokes the strict internal work-ledger routes with MCP credentials.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.backend import get_app_backend_port
from kiro_crew.work_vocab import canonical_json_digest

pytestmark = pytest.mark.integration

_APP = "acceptance-probe"
_KIND = f"{_APP}:release-ready"
_ENDPOINT_V1 = "acceptance/release-ready"
_ENDPOINT_V3 = "acceptance/release-ready-v3"

_SERVER = r"""
import hashlib
import hmac
import http.server
import json
import os
import time
from pathlib import Path

VERSION = __VERSION__
SECRET = os.environ.get("KIROCREW_PROXY_SECRET", "")
LOG = Path(__file__).resolve().parent / "data" / "requests.jsonl"


def _valid_proxy_header(header, method, target, body):
    try:
        stamp, supplied = header.split(":", 1)
        if abs(time.time() - int(stamp)) > 60:
            return False
    except (TypeError, ValueError):
        return False
    body_digest = hashlib.sha256(body).hexdigest()
    message = f"{stamp}:{method}:{target}:{body_digest}".encode("utf-8")
    expected = hmac.new(SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return bool(SECRET) and hmac.compare_digest(supplied, expected)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b"ok", "text/plain")
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size)
        valid = _valid_proxy_header(
            self.headers.get("X-KiroCrew-Proxy", ""), "POST", self.path, raw
        )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "path": self.path,
                        "body": payload,
                        "signature_valid": valid,
                        "version": VERSION,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        if not valid:
            self._send(403, b'{"error":"bad proxy signature"}')
            return
        if not self.path.startswith("/api/acceptance/") or not isinstance(payload, dict):
            self._send(404, b'{"error":"not found"}')
            return
        supplied = payload.get("input")
        if not isinstance(supplied, dict):
            self._send(400, b'{"error":"bad input"}')
            return
        scenario = supplied.get("scenario")
        request_id = supplied.get("request_id")
        if scenario == "malformed":
            self._send(200, b'{"verdict":"pass"')
            return
        verdict = {"pass": "pass", "pending": "pending", "fail": "fail"}.get(scenario)
        if verdict is None:
            self._send(400, b'{"error":"unknown scenario"}')
            return
        evidence = f"{VERSION}:{scenario}:{request_id}"
        body = json.dumps({"verdict": verdict, "evidence": evidence}).encode("utf-8")
        self._send(200, body)

    def log_message(self, *args):
        pass


port = int(os.environ["PORT"])
http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def _write_app_source(
    root: Path,
    *,
    version: str,
    endpoint: str | None,
) -> Path:
    source = root / f"source-{version}"
    source.mkdir()
    manifest: dict[str, Any] = {
        "name": _APP,
        "version": version,
        "displayName": "Acceptance Probe",
        "description": "Integration fixture for host-owned acceptance checks.",
        "author": "Kiro Crew tests",
        "backend": {
            "entryPoint": "server.py",
            "runtime": "python",
            "port": "auto",
            "healthCheck": "/health",
        },
    }
    if endpoint is not None:
        manifest["contributes"] = {
            "acceptanceKinds": [
                {
                    "id": "release-ready",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "scenario": {
                                "type": "string",
                                "enum": ["pass", "pending", "fail", "malformed"],
                                "maxLength": 16,
                            },
                            "request_id": {"type": "string", "maxLength": 32},
                        },
                        "required": ["scenario", "request_id"],
                        "additionalProperties": False,
                    },
                    "endpoint": endpoint,
                }
            ]
        }
    (source / "app.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (source / "server.py").write_text(
        _SERVER.replace("__VERSION__", json.dumps(version)), encoding="utf-8"
    )
    return source


def _acceptance(scenario: str, request_id: str) -> dict[str, Any]:
    return {
        "kind": _KIND,
        "input": {"scenario": scenario, "request_id": request_id},
    }


async def _one_turn(gw: Any, slot: str) -> None:
    """Start a real fake-ACP session so work records have a crew-log unit."""
    response = await gw.post(
        "/api/chat",
        {"message": "Open the integration-test work ledger.", "slot": slot},
        timeout=45,
    )
    assert response.status == 200, await response.text()
    async for raw in response.content:
        if raw.decode("utf-8", "replace").strip() == "data: [DONE]":
            return
    pytest.fail("conductor setup turn ended without [DONE]")


async def _conductor(gw: Any) -> str:
    slot = await gw.post_json(
        "/api/chat/slots",
        {
            "name": "Acceptance integration conductor",
            "agent": "kirocrew-conductor",
            "agent_kind": "template",
        },
    )
    await _one_turn(gw, slot["key"])
    return f"dashboard:{slot['key']}"


async def _work_request(
    gw: Any,
    conductor: str,
    body: dict[str, Any],
    *,
    expect: int = 200,
) -> dict[str, Any]:
    response = await gw.post(
        "/api/work-ledger/record",
        body,
        auth=False,
        headers=gw.mcp_headers(conductor),
    )
    text = await response.text()
    assert response.status == expect, f"work_ledger_record -> {response.status}: {text}"
    return json.loads(text)


async def _create_item(gw: Any, conductor: str, acceptance: dict[str, Any]) -> str:
    result = await _work_request(
        gw,
        conductor,
        {"action": "create", "title": acceptance["input"]["request_id"], "acceptance": acceptance},
    )
    return str(result["item"]["item_id"])


async def _evaluate(gw: Any, conductor: str, item_id: str) -> dict[str, Any]:
    return await gw.post_json(
        "/api/work-ledger/evaluate",
        {"item_id": item_id},
        auth=False,
        headers=gw.mcp_headers(conductor),
    )


async def _close(
    gw: Any,
    conductor: str,
    item_id: str,
    *,
    expect: int,
) -> dict[str, Any]:
    return await _work_request(
        gw,
        conductor,
        {"action": "close", "item_id": item_id, "state": "accepted"},
        expect=expect,
    )


async def _wait_healthy(*, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = await asyncio.to_thread(get_app_backend_port, _APP)
        if port:
            return port
        await asyncio.sleep(0.1)
    pytest.fail(f"{_APP} did not become healthy in {timeout}s")


def _provider_requests(home: Path) -> list[dict[str, Any]]:
    path = home / "apps" / _APP / "data" / "requests.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _assert_close_refused(gw: Any, conductor: str, item_id: str) -> None:
    body = await _close(gw, conductor, item_id, expect=409)
    assert body["code"] == "provider_verdict_required", body


@pytest.mark.asyncio
async def test_real_app_acceptance_contract(
    gateway_boot: Any,
    integration_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive every authority and lifecycle edge through one real installed app."""
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    source_v1 = _write_app_source(tmp_path, version="1.0.0", endpoint=_ENDPOINT_V1)
    same_version_root = tmp_path / "same-version"
    same_version_root.mkdir()
    source_v1_replacement = _write_app_source(
        same_version_root,
        version="1.0.0",
        endpoint=_ENDPOINT_V1,
    )
    replacement_server = source_v1_replacement / "server.py"
    replacement_server.write_text(
        replacement_server.read_text(encoding="utf-8") + "\n# replacement backend bytes\n",
        encoding="utf-8",
    )
    source_v2 = _write_app_source(tmp_path, version="2.0.0", endpoint=None)
    source_v3 = _write_app_source(tmp_path, version="3.0.0", endpoint=_ENDPOINT_V3)

    async with gateway_boot() as gw:
        installed = await gw.post_json("/api/apps/install", {"source": str(source_v1)}, expect=201)
        assert installed["ok"] is True and installed["name"] == _APP

        # Blanket execution consent is deliberately insufficient machine authority.
        allow_response = await gw.put("/api/security/trusted-apps/allow-all", {"value": True})
        allow_body = await allow_response.text()
        assert allow_response.status == 200, allow_body
        allow_all = json.loads(allow_body)
        assert allow_all["allowAll"] is True
        enabled = await gw.post_json(f"/api/apps/{_APP}/enable", {})
        assert enabled["ok"] is True and enabled["backend"]["app_name"] == _APP
        await _wait_healthy()

        conductor = await _conductor(gw)
        await _work_request(
            gw,
            conductor,
            {"action": "goal", "goal": "Exercise a real acceptance app", "round": 1},
        )

        unauthorized_acceptance = _acceptance("pass", "authorization-refusal")
        unauthorized = await _create_item(gw, conductor, unauthorized_acceptance)
        refused = await _evaluate(gw, conductor, unauthorized)
        assert refused["verdict"] == "refused", refused
        assert refused["evaluation"]["authority"] == ""
        assert _provider_requests(integration_home) == [], "untrusted provider was invoked"
        direct_pass = await _work_request(
            gw,
            conductor,
            {"action": "verdict", "item_id": unauthorized, "verdict": "pass"},
            expect=409,
        )
        assert direct_pass["code"] == "provider_verdict_required"
        await _assert_close_refused(gw, conductor, unauthorized)

        trust = await gw.post_json(f"/api/security/trusted-apps/{_APP}", {})
        assert _APP in trust["apps"]
        passed = await _evaluate(gw, conductor, unauthorized)
        assert passed["verdict"] == "pass", passed
        proof_v1 = passed["evaluation"]
        assert proof_v1["provider"] == _APP
        assert proof_v1["kind"] == _KIND
        assert proof_v1["version"] == "1.0.0"
        assert proof_v1["authority"] == "trusted-app"
        assert proof_v1["endpoint"] == _ENDPOINT_V1
        assert proof_v1["acceptance_digest"] == canonical_json_digest(unauthorized_acceptance)
        assert len(proof_v1["manifest_digest"]) == 64
        assert len(proof_v1["backend_generation"]) == 64
        assert proof_v1["evaluated_at"]

        first_request = _provider_requests(integration_home)[-1]
        assert first_request == {
            "path": f"/api/{_ENDPOINT_V1}",
            "body": unauthorized_acceptance,
            "signature_valid": True,
            "version": "1.0.0",
        }

        close_item = await _create_item(gw, conductor, _acceptance("pass", "accepted-close"))
        assert (await _evaluate(gw, conductor, close_item))["verdict"] == "pass"
        closed = await _close(gw, conductor, close_item, expect=200)
        assert closed["item"]["state"] == "accepted"

        for scenario, expected in (("pending", "pending"), ("fail", "fail")):
            item_id = await _create_item(gw, conductor, _acceptance(scenario, f"{scenario}-result"))
            result = await _evaluate(gw, conductor, item_id)
            assert result["verdict"] == expected, result
            if scenario == "fail":
                assert result["fails"] == 1
            await _assert_close_refused(gw, conductor, item_id)

        malformed_id = await _create_item(
            gw, conductor, _acceptance("malformed", "malformed-result")
        )
        malformed = await _evaluate(gw, conductor, malformed_id)
        assert malformed["verdict"] == "error", malformed
        assert "valid JSON" in malformed["evaluation"]["evidence"]
        await _assert_close_refused(gw, conductor, malformed_id)

        lifecycle_acceptance = _acceptance("pass", "lifecycle-result")
        lifecycle_id = await _create_item(gw, conductor, lifecycle_acceptance)
        lifecycle_pass = await _evaluate(gw, conductor, lifecycle_id)
        assert lifecycle_pass["verdict"] == "pass"
        manifest_v1 = lifecycle_pass["evaluation"]["manifest_digest"]
        generation_v1 = lifecycle_pass["evaluation"]["backend_generation"]

        same_version_update = await gw.post_json(
            f"/api/apps/{_APP}/update",
            {"source": str(source_v1_replacement)},
        )
        assert same_version_update["ok"] is True
        await _wait_healthy()
        await _assert_close_refused(gw, conductor, lifecycle_id)
        same_version_pass = await _evaluate(gw, conductor, lifecycle_id)
        assert same_version_pass["verdict"] == "pass", same_version_pass
        same_version_proof = same_version_pass["evaluation"]
        assert same_version_proof["version"] == "1.0.0"
        assert same_version_proof["manifest_digest"] == manifest_v1
        assert same_version_proof["backend_generation"] != generation_v1

        revoked = await gw.delete(f"/api/security/trusted-apps/{_APP}")
        assert revoked.status == 200, await revoked.text()
        await _assert_close_refused(gw, conductor, lifecycle_id)
        regranted = await gw.post_json(f"/api/security/trusted-apps/{_APP}", {})
        assert _APP in regranted["apps"]

        disabled = await gw.post_json(f"/api/apps/{_APP}/disable", {})
        assert disabled["ok"] is True
        await _assert_close_refused(gw, conductor, lifecycle_id)
        disabled_result = await _evaluate(gw, conductor, lifecycle_id)
        assert disabled_result["verdict"] == "refused", disabled_result
        assert "disabled" in disabled_result["evaluation"]["evidence"]
        await _assert_close_refused(gw, conductor, lifecycle_id)

        reenabled = await gw.post_json(f"/api/apps/{_APP}/enable", {})
        assert reenabled["ok"] is True
        await _wait_healthy()
        assert (await _evaluate(gw, conductor, lifecycle_id))["verdict"] == "pass"

        updated_v2 = await gw.post_json(f"/api/apps/{_APP}/update", {"source": str(source_v2)})
        assert updated_v2["ok"] is True
        await _wait_healthy()
        await _assert_close_refused(gw, conductor, lifecycle_id)
        changed = await _evaluate(gw, conductor, lifecycle_id)
        assert changed["verdict"] == "error", changed
        assert changed["evaluation"]["version"] == "2.0.0"
        assert changed["evaluation"]["manifest_digest"] != manifest_v1
        assert "not declared exactly once" in changed["evaluation"]["evidence"]
        await _assert_close_refused(gw, conductor, lifecycle_id)

        updated_v3 = await gw.post_json(f"/api/apps/{_APP}/update", {"source": str(source_v3)})
        assert updated_v3["ok"] is True
        await _wait_healthy()
        restored = await _evaluate(gw, conductor, lifecycle_id)
        proof_v3 = restored["evaluation"]
        assert restored["verdict"] == "pass", restored
        assert proof_v3["version"] == "3.0.0"
        assert proof_v3["endpoint"] == _ENDPOINT_V3
        assert proof_v3["manifest_digest"] not in {
            manifest_v1,
            changed["evaluation"]["manifest_digest"],
        }
        assert _provider_requests(integration_home)[-1]["path"] == f"/api/{_ENDPOINT_V3}"
        assert (await _close(gw, conductor, lifecycle_id, expect=200))["item"][
            "state"
        ] == "accepted"

        board = await gw.get_json(
            "/api/work-ledger",
            auth=False,
            headers=gw.mcp_headers(conductor),
        )
        persisted = {item["item_id"]: item for item in board["items"]}
        assert persisted[lifecycle_id]["evaluation"] == proof_v3

        audit = await gw.get_json("/api/sel/events", params={"limit": "1000"})
        provider_events = [
            event
            for event in audit["events"]
            if event.get("operation") == "app_acceptance_evaluate"
        ]
        assert any(event.get("outcome") == "refused" for event in provider_events)
        pass_event = next(
            event
            for event in provider_events
            if event.get("outcome") == "pass" and "version=1.0.0" in event.get("resources", "")
        )
        resources = pass_event.get("resources", "")
        for fragment in (
            f"provider={_APP}",
            f"kind={_KIND}",
            "version=1.0.0",
            "authority=trusted-app",
        ):
            assert fragment in resources, resources
        assert "1.0.0:pass:authorization-refusal" not in json.dumps(pass_event)

        final_disable = await gw.post_json(f"/api/apps/{_APP}/disable", {})
        assert final_disable["ok"] is True
