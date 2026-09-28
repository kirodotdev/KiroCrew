"""M3 backend slice — the Workflows builtin-app manifest + HTTP handlers.

Covers the backend half of the M3 UI app (the dashboard tab + WS progress are the
E1–E4 Playwright gates against the dev instance). Asserts:
  * app.json parses as a valid AppManifest with the expected tab/route/permissions,
  * handle_validate / handle_run / handle_examples behave per contract,
  * the run handler drives the real WorkflowRunner and returns the event stream.

All pure-function tests — no socket bound, no real agent (the runner's injected
stub / a test agent_fn is used).
"""

from __future__ import annotations

import json
import os

import pytest

from kiro_crew.apps.builtins.workflows.server import (
    handle_examples,
    handle_run,
    handle_validate,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.workflows.runner import WorkflowRunner

_APP_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "kiro_crew",
    "apps",
    "builtins",
    "workflows",
    "app.json",
)

GOOD_SCRIPT = (
    'META = {"name": "demo", "description": "d"}\n'
    "async def workflow(ctx):\n"
    "    ctx.log('hi')\n"
    "    return {'ok': True}\n"
)


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def _load_manifest() -> AppManifest:
    with open(_APP_JSON, encoding="utf-8") as fh:
        return AppManifest.from_dict(json.load(fh))


def test_app_json_is_valid_manifest() -> None:
    m = _load_manifest()
    assert m.validate() == [], m.validate()


def test_manifest_exposes_workflows_tab() -> None:
    m = _load_manifest()
    routes = [p.route for p in m.ui.pages]
    assert "/workflows" in routes
    assert any(p.label == "Workflows" for p in m.ui.pages)


def test_manifest_scopes_api_and_declares_run_event() -> None:
    m = _load_manifest()
    # API scoped to its own prefix; declares the run-event WS type the run view needs
    assert any("workflows" in route for route in m.permissions.api)
    assert "workflow_run_event" in m.permissions.events


def test_app_json_ships_opt_in() -> None:
    # AppManifest does not model ``defaultEnabled`` (the builtin registry tracks it),
    # so assert opt-in at the raw-json level — builtins must not auto-enable.
    with open(_APP_JSON, encoding="utf-8") as fh:
        raw = json.load(fh)
    assert raw.get("defaultEnabled") is False


# --------------------------------------------------------------------------- #
# handle_validate
# --------------------------------------------------------------------------- #


def test_handle_validate_good() -> None:
    out = handle_validate({"source": GOOD_SCRIPT})
    assert out["ok"] is True
    assert out["errors"] == []
    assert out["meta"]["name"] == "demo"


def test_handle_validate_rejects_bad_script() -> None:
    out = handle_validate({"source": "import os\n" + GOOD_SCRIPT})
    assert out["ok"] is False
    assert out["errors"]


def test_handle_validate_non_string() -> None:
    out = handle_validate({"source": 123})
    assert out["ok"] is False


# --------------------------------------------------------------------------- #
# handle_run
# --------------------------------------------------------------------------- #


def test_handle_run_happy_path_returns_event_stream() -> None:
    out = handle_run({"source": GOOD_SCRIPT, "run_id": "wf_t", "now": "2026-06-18T00:00:00Z"})
    assert out["ok"] is True
    assert out["result"] == {"ok": True}
    types = [e["type"] for e in out["events"]]
    assert types[0] == "run_started" and types[-1] == "run_finished"
    assert "log" in types


def test_handle_run_invalid_script_fails_at_validate() -> None:
    out = handle_run({"source": "import os\n" + GOOD_SCRIPT})
    assert out["ok"] is False
    assert out["events"][-1]["type"] == "run_failed"
    assert out["events"][-1]["data"]["where"] == "validate"


def test_handle_run_missing_source() -> None:
    out = handle_run({})
    assert out["ok"] is False
    assert out["events"] == []


def test_handle_run_uses_injected_runner_and_agent() -> None:
    async def agent_fn(prompt: str, opts: dict):
        return "INJECTED"

    script = (
        'META = {"name": "x"}\n' "async def workflow(ctx):\n" "    return await ctx.agent('go')\n"
    )
    out = handle_run({"source": script}, runner=WorkflowRunner(agent_fn=agent_fn))
    assert out["ok"] is True
    assert out["result"] == "INJECTED"


# --------------------------------------------------------------------------- #
# handle_examples
# --------------------------------------------------------------------------- #


def test_handle_examples_lists_shipped_dsl_examples() -> None:
    examples = handle_examples()
    # Assert non-empty rather than guarding on it: `_examples_dir()` returns "" when
    # it cannot resolve the directory, so a guarded body passes vacuously and a moved
    # examples tree silently empties the app's example picker. Tests only run from a
    # source checkout, where the directory is always present.
    assert examples, "handle_examples() found no shipped examples — did the docs tree move?"
    for ex in examples:
        assert ex["name"] and isinstance(ex["source"], str)
        # every shipped example must itself be a VALID workflow script
        assert handle_validate({"source": ex["source"]})["ok"] is True, ex["name"]


# --------------------------------------------------------------------------- #
# Output redaction (security-controls): workflow result/events are LLM-derived
# and must be scrubbed of credentials + exfiltration URLs before leaving this
# backend, mirroring the gateway handlers (dashboard/handlers/workflows.py).
# --------------------------------------------------------------------------- #

# A planted AWS access key id (matches kiro_crew.security._CREDENTIAL_PATTERNS)
# and a planted exfiltration URL (long query on a non-allowlisted domain).
_PLANTED_CRED = "AKIAIOSFODNN7EXAMPLE"
_PLANTED_URL = "https://evil.example.com/collect?data=" + "a" * 36


def test_redact_obj_scrubs_credentials_and_exfil_urls_in_run_payload() -> None:
    from kiro_crew.apps.builtins.workflows.server import _redact_obj

    # Shape mirrors a real run result: nested dict + events list with LLM text.
    payload = {
        "run_id": "wf_x",
        "result": f"agent leaked key {_PLANTED_CRED} in output",
        "events": [
            {"type": "log", "data": {"text": f"fetched {_PLANTED_URL} ok"}},
        ],
    }
    out = _redact_obj(payload)

    # Credential is gone, replaced with the security module's marker.
    assert _PLANTED_CRED not in out["result"]
    assert "[REDACTED: credential]" in out["result"]
    # Exfiltration URL is gone (scheme + query stripped), marker present.
    ev_text = out["events"][0]["data"]["text"]
    assert _PLANTED_URL not in ev_text
    assert "?data=" not in ev_text
    assert "[REDACTED: suspicious URL" in ev_text
    # Non-sensitive fields and structure are preserved verbatim.
    assert out["run_id"] == "wf_x"
    assert out["events"][0]["type"] == "log"


def test_redact_obj_routes_every_string_through_platform_context(monkeypatch) -> None:
    """Responses must use the composed credential policy, including mapping keys."""
    from kiro_crew.apps.builtins.workflows import server

    seen: list[str] = []

    def _redact(value: str) -> str:
        seen.append(value)
        return f"context:{value}"

    monkeypatch.setattr(server, "redact_via_context", _redact)

    assert server._redact_obj({"key": ["value"]}) == {"context:key": ["context:value"]}
    assert seen == ["key", "value"]


def test_handler_send_redacts_before_writing_to_the_wire() -> None:
    """The fix is wired centrally in _Handler._send, so EVERY response surface is
    redacted. Drive _send with fakes (no socket bound) and assert the bytes
    written to the wire carry no planted secret."""
    from kiro_crew.apps.builtins.workflows.server import _Handler

    handler = _Handler.__new__(_Handler)  # skip socket-binding __init__
    written: list[bytes] = []

    class _FakeWFile:
        def write(self, b: bytes) -> None:
            written.append(b)

    handler.wfile = _FakeWFile()
    handler.send_response = lambda code: None  # type: ignore[method-assign]
    handler.send_header = lambda *a, **k: None  # type: ignore[method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]

    handler._send(
        200,
        {"result": f"key {_PLANTED_CRED}", "events": [f"see {_PLANTED_URL}"]},
    )

    wire = b"".join(written).decode("utf-8")
    assert _PLANTED_CRED not in wire
    assert _PLANTED_URL not in wire
    assert "[REDACTED: credential]" in wire
    assert "[REDACTED: suspicious URL" in wire


# --------------------------------------------------------------------------- #
# Process entrypoint
# --------------------------------------------------------------------------- #


def _forbid_real_bind(monkeypatch) -> None:
    """Name-independent anti-hang guard for the entrypoint tests.

    Patching ``server._Server`` intercepts what ``main()`` constructs *today*; a
    patch on any name ``main()`` does not construct (an inlined
    ``ThreadingHTTPServer(...)``, a ``_make_server()`` factory) still succeeds
    but goes unused, and the test then binds a real socket and hangs the CI
    shard. Failing at the bind itself turns that silent hang into a loud,
    diagnosable failure whatever ``main()`` happens to name.
    """
    import socketserver

    monkeypatch.setattr(
        socketserver.TCPServer,
        "server_bind",
        lambda self: pytest.fail("main() bound a real socket"),
    )


def test_main_boots_platform_before_binding_server(monkeypatch) -> None:
    """The app process must compose security before accepting workflow requests."""
    from types import SimpleNamespace

    from kiro_crew.apps.builtins.workflows import server

    calls: list[str] = []

    def _boot(_cfg) -> None:
        calls.append("boot")

    class _FakeServer:
        def __init__(self, *args, **kwargs) -> None:
            calls.append("bind")

        def serve_forever(self) -> None:
            calls.append("serve")

    monkeypatch.setattr(server, "boot_platform", _boot)
    monkeypatch.setattr(server.KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace()))
    # Patch _Server, the class main() actually constructs: its ThreadingHTTPServer
    # base was bound at class-definition time, so patching the base name never
    # engages and the test would bind a REAL listener and block in serve_forever().
    # setattr raises on a missing attribute, so a rename of _Server fails fast here.
    monkeypatch.setattr(server, "_Server", _FakeServer)
    _forbid_real_bind(monkeypatch)

    assert server.main() is None
    assert calls == ["boot", "bind", "serve"]


def test_main_fails_closed_when_platform_cannot_compose(monkeypatch) -> None:
    """A failed composition must not expose the backend with baseline policy."""
    from kiro_crew.apps.builtins.workflows import server
    from kiro_crew.platform.context import PlatformCompositionError

    def _boom(_cfg) -> None:
        raise PlatformCompositionError("companion missing")

    served: list[str] = []

    # Class-shaped fake (not a bare lambda): if the boot/bind ordering ever
    # regresses, main() goes on to call .serve_forever() on the constructed
    # object, and a lambda returning None would surface an unrelated
    # AttributeError instead of the ordering assertion below.
    class _FakeServer:
        def __init__(self, *args, **kwargs) -> None:
            served.append("bind")

        def serve_forever(self) -> None:
            served.append("serve")

    monkeypatch.setattr(server, "boot_platform", _boom)
    # Same substitution as the boot-ordering test: main() constructs _Server, so a
    # patch on the ThreadingHTTPServer base name is dead — masked here only because
    # the boot stub raises before the bind, and it would bind a real socket the
    # moment that ordering changed.
    monkeypatch.setattr(server, "_Server", _FakeServer)
    _forbid_real_bind(monkeypatch)

    with pytest.raises(PlatformCompositionError):
        server.main()
    assert served == []
