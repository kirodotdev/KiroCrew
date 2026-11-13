"""Coverage for the workflows backend: redaction, examples listing, the stub
agent and the HTTP routes, exercised on a loopback listener."""

import http.client
import json
import logging
import threading

import pytest

from kiro_crew.apps.builtins.workflows import server


@pytest.fixture
def listener(monkeypatch):
    monkeypatch.setattr(server, "verify_proxy_request", lambda *a, **k: True)
    httpd = server._Server(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _call(port, path, method="GET", data=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=data)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"null")
    finally:
        conn.close()


def test_redaction_keeps_non_string_scalars_and_walks_containers(monkeypatch):
    monkeypatch.setattr(server, "redact_via_context", lambda text: f"<{text}>")
    assert server._redact_obj(3) == 3
    assert server._redact_obj(None) is None
    assert server._redact_obj(["a", {"k": ["v", 3, None]}]) == ["<a>", {"<k>": ["<v>", 3, None]}]


@pytest.mark.asyncio
async def test_stub_agent_echoes_a_truncated_prompt():
    head = "x" * 80
    reply = await server._stub_agent(head + "tail", {})
    assert reply == f"[stub agent reply to: {head}]"


def test_examples_missing_directory_logs_and_returns_empty(monkeypatch, caplog):
    monkeypatch.setattr(server, "_examples_dir", lambda: "")
    with caplog.at_level(logging.WARNING, logger="kirocrew.app.workflows"):
        assert server.handle_examples() == []
    assert "no examples directory found" in caplog.text


def test_examples_unlistable_directory_logs_and_returns_empty(tmp_path, monkeypatch, caplog):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    monkeypatch.setattr(server, "_examples_dir", lambda: str(not_a_dir))
    with caplog.at_level(logging.WARNING, logger="kirocrew.app.workflows"):
        assert server.handle_examples() == []
    assert "cannot list examples" in caplog.text


def test_examples_skip_non_python_and_unreadable_entries(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("not an example", encoding="utf-8")
    (tmp_path / "broken.py").mkdir()  # open() on a directory raises OSError
    (tmp_path / "plain.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(server, "_examples_dir", lambda: str(tmp_path))
    out = server.handle_examples()
    assert [item["name"] for item in out] == ["plain"]
    assert out[0]["source"] == "x = 1\n" and out[0]["description"] == ""


def test_log_message_routes_to_debug_logger(caplog):
    with caplog.at_level(logging.DEBUG, logger="kirocrew.app.workflows"):
        object.__new__(server._Handler).log_message("GET %s", "/x")
    assert "workflows-app: GET /x" in caplog.text


def test_health_needs_no_signature(listener, monkeypatch):
    monkeypatch.setattr(server, "verify_proxy_request", lambda *a, **k: False)
    assert _call(listener, "/health") == (200, {"status": "ok"})


def test_unsigned_request_is_rejected(listener, monkeypatch):
    monkeypatch.setattr(server, "verify_proxy_request", lambda *a, **k: False)
    assert _call(listener, "/examples") == (401, {"error": "unauthorized"})


def test_unknown_routes_return_404(listener):
    assert _call(listener, "/nope") == (404, {"error": "not found"})
    assert _call(listener, "/nope", "POST", b"{}") == (404, {"error": "not found"})


def test_examples_route_serves_the_list(listener):
    status, payload = _call(listener, "/api/examples")
    assert status == 200 and isinstance(payload, list)


def test_validate_route_reports_source_type_error(listener):
    body = json.dumps({"source": 5}).encode()
    status, payload = _call(listener, "/validate", "POST", body)
    assert status == 200
    assert payload == {"ok": False, "errors": ["'source' must be a string"], "meta": None}


def test_run_route_rejects_missing_source_and_bad_json(listener):
    status, payload = _call(listener, "/run", "POST", json.dumps({"source": " "}).encode())
    assert status == 200 and payload["error"] == "missing 'source'"
    status, payload = _call(listener, "/run", "POST", b"{not json")
    assert status == 200 and payload["ok"] is False
