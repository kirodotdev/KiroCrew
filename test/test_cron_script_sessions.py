"""``ScriptContext`` opens and seeds dashboard sessions with the cron's own credential.

A script cron that drives the dashboard cannot hold a dashboard token:
``POST /api/token/local`` refuses a sandboxed child. These tests pin the five
methods that call the ``/api/chat`` routes the internal secret already reaches,
presenting the same ``X-Internal-Secret`` / ``X-Session-Key`` pair ``notify()``
presents plus the run's signed session token. No bearer token is minted or
handled anywhere on this path.
"""

from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from kiro_crew import cron_script
from kiro_crew.cron_script import ScriptContext
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

_JOB = "nightly-dispatch"
# Assembled at runtime so the source never holds a credential-shaped literal.
_FAKE_KEY = "AKIA" + "1234567890123456"


class _Resp:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch) -> ScriptContext:
    """A context built from the env contract ``run_script_sandboxed`` sets up."""
    monkeypatch.setenv("_KIROCREW_DIAL_PORT", "7788")
    monkeypatch.setenv("KIROCREW_INTERNAL_SECRET", "tok")
    monkeypatch.delenv("_KIROCREW_SECRET_FILE", raising=False)
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, "signed-run-token")
    return ScriptContext(job=SimpleNamespace(id=_JOB, message=""))


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch):
    """Capture every request; answer each path from a per-test table."""
    seen: list = []
    answers: dict[tuple[str, str], object] = {}

    def _urlopen(req, timeout=None):
        seen.append(req)
        key = (req.get_method(), req.full_url.split("7788", 1)[1])
        answer = answers[key]
        if isinstance(answer, Exception):
            raise answer
        return _Resp(answer)

    monkeypatch.setattr(cron_script, "loopback_urlopen", _urlopen)
    return SimpleNamespace(seen=seen, answers=answers)


def _cron_headers(req) -> dict[str, str]:
    return {
        "secret": req.get_header("X-internal-secret"),
        "key": req.get_header("X-session-key"),
        "token": req.get_header("X-session-token"),
    }


class TestListSessionFolders:
    def test_gets_the_folder_tree_as_the_cron(self, ctx, gateway):
        folders = [{"id": "f1", "name": "Ops"}, {"id": "f2", "name": "Reports"}]
        gateway.answers[("GET", "/api/chat/folders")] = folders

        assert ctx.list_session_folders() == folders

        (req,) = gateway.seen
        assert req.full_url == "http://127.0.0.1:7788/api/chat/folders"
        assert req.data is None
        assert _cron_headers(req) == {
            "secret": "tok",
            "key": f"cron:{_JOB}",
            "token": "signed-run-token",
        }

    def test_a_non_list_answer_is_an_error(self, ctx, gateway):
        gateway.answers[("GET", "/api/chat/folders")] = {"error": "Token required"}

        with pytest.raises(RuntimeError, match="Token required"):
            ctx.list_session_folders()


class TestCreateSessionFolder:
    def test_posts_the_name_and_returns_the_folder(self, ctx, gateway):
        made = {"id": "f9", "name": "Ops", "parent_id": None}
        gateway.answers[("POST", "/api/chat/folders")] = made

        assert ctx.create_session_folder("Ops") == made

        (req,) = gateway.seen
        assert req.get_method() == "POST"
        assert json.loads(req.data) == {"name": "Ops"}
        assert _cron_headers(req)["key"] == f"cron:{_JOB}"

    def test_a_refusal_raises_with_the_gateway_reason(self, ctx, gateway):
        gateway.answers[("POST", "/api/chat/folders")] = {
            "error": "folder name taken",
            "code": "folder_name_taken",
        }

        with pytest.raises(RuntimeError, match="folder name taken"):
            ctx.create_session_folder("Ops")

    def test_the_name_is_redacted_like_notify(self, ctx, gateway):
        """The name renders in the sidebar, so a credential in it is scrubbed first."""
        gateway.answers[("POST", "/api/chat/folders")] = {"id": "f9"}

        ctx.create_session_folder(f"runs for {_FAKE_KEY}")

        (req,) = gateway.seen
        sent = json.loads(req.data)["name"]
        assert _FAKE_KEY not in sent
        assert "runs for" in sent


class TestOpenSession:
    def test_creates_a_slot_in_the_folder_and_returns_its_key(self, ctx, gateway):
        gateway.answers[("POST", "/api/chat/slots")] = {"key": "nightly-dispatch-1", "name": "x"}

        key = ctx.open_session("Nightly", folder_id="f9", agent="worker", model="sonnet")

        assert key == "nightly-dispatch-1"
        (req,) = gateway.seen
        assert req.full_url == "http://127.0.0.1:7788/api/chat/slots"
        assert json.loads(req.data) == {
            "name": "Nightly",
            "folder_id": "f9",
            "agent": "worker",
            "model": "sonnet",
        }

    def test_omitted_fields_are_left_to_the_gateway(self, ctx, gateway):
        """An absent field takes the gateway default; an empty string must not pin one."""
        gateway.answers[("POST", "/api/chat/slots")] = {"key": "chat-77"}

        assert ctx.open_session() == "chat-77"

        (req,) = gateway.seen
        assert json.loads(req.data) == {}

    def test_an_answer_without_a_key_is_an_error(self, ctx, gateway):
        gateway.answers[("POST", "/api/chat/slots")] = {"error": "folder not found"}

        with pytest.raises(RuntimeError, match="folder not found"):
            ctx.open_session(folder_id="missing")

    def test_the_name_is_redacted_like_notify(self, ctx, gateway):
        gateway.answers[("POST", "/api/chat/slots")] = {"key": "chat-78"}

        ctx.open_session(f"key {_FAKE_KEY} run")

        (req,) = gateway.seen
        sent = json.loads(req.data)["name"]
        assert _FAKE_KEY not in sent
        assert sent.endswith(" run")


class TestSendToSession:
    def test_queues_a_turn_on_the_slot_without_streaming(self, ctx, gateway):
        receipt = {"ok": True, "slot": "chat-77"}
        gateway.answers[("POST", "/api/chat?ws=1")] = receipt

        assert ctx.send_to_session("chat-77", "triage the queue") == receipt

        (req,) = gateway.seen
        assert req.full_url == "http://127.0.0.1:7788/api/chat?ws=1"
        assert json.loads(req.data) == {"slot": "chat-77", "message": "triage the queue"}

    def test_the_message_is_redacted_like_notify(self, ctx, gateway):
        gateway.answers[("POST", "/api/chat?ws=1")] = {"ok": True, "slot": "chat-77"}

        ctx.send_to_session("chat-77", f"use key {_FAKE_KEY} for the run")

        (req,) = gateway.seen
        sent = json.loads(req.data)["message"]
        assert _FAKE_KEY not in sent
        assert "for the run" in sent

    def test_a_refusal_raises(self, ctx, gateway):
        gateway.answers[("POST", "/api/chat?ws=1")] = {"error": "slot is busy", "code": "busy"}

        with pytest.raises(RuntimeError, match="slot is busy"):
            ctx.send_to_session("chat-77", "hello")


class TestSetSessionMode:
    """``set_session_mode`` asks the gateway for a slot-scoped approval mode.

    The mode is sent as the script gave it. The gateway owns the rule that a
    cron sets only ``trust`` or ``trust_reads`` on a slot it created, and it
    audits each refusal, so no allowlist is duplicated here.
    """

    @pytest.mark.parametrize("mode", ["trust", "trust_reads"])
    def test_posts_the_slot_and_mode_and_returns_the_receipt(self, ctx, gateway, mode):
        receipt = {"ok": True, "mode": mode}
        gateway.answers[("POST", "/api/chat/mode")] = receipt

        assert ctx.set_session_mode("chat-77", mode) == receipt

        (req,) = gateway.seen
        assert req.full_url == "http://127.0.0.1:7788/api/chat/mode"
        assert req.get_method() == "POST"
        assert json.loads(req.data) == {"slot": "chat-77", "mode": mode}
        assert _cron_headers(req) == {
            "secret": "tok",
            "key": f"cron:{_JOB}",
            "token": "signed-run-token",
        }

    def test_a_mode_the_gateway_refuses_raises_with_its_code(self, ctx, gateway):
        """``yolo`` reaches the gateway as sent; the gateway refuses and audits it."""
        body = json.dumps(
            {
                "ok": False,
                "error": "a scheduled run can set only trust or trust_reads on a session it created",
                "code": "mode_not_allowed",
            }
        ).encode()
        gateway.answers[("POST", "/api/chat/mode")] = urllib.error.HTTPError(
            "http://127.0.0.1:7788/api/chat/mode", 403, "Forbidden", {}, io.BytesIO(body)
        )

        with pytest.raises(RuntimeError, match="mode_not_allowed"):
            ctx.set_session_mode("chat-77", "yolo")

        (req,) = gateway.seen
        assert json.loads(req.data) == {"slot": "chat-77", "mode": "yolo"}

    def test_a_slot_the_cron_did_not_create_raises_with_the_gateway_reason(self, ctx, gateway):
        body = json.dumps(
            {
                "error": "a scheduled run can only control sessions it created itself",
                "code": "not_creator",
            }
        ).encode()
        gateway.answers[("POST", "/api/chat/mode")] = urllib.error.HTTPError(
            "http://127.0.0.1:7788/api/chat/mode", 403, "Forbidden", {}, io.BytesIO(body)
        )

        with pytest.raises(RuntimeError, match="not_creator"):
            ctx.set_session_mode("owner-tab", "trust")

    def test_a_non_dict_answer_is_an_error(self, ctx, gateway):
        gateway.answers[("POST", "/api/chat/mode")] = ["unexpected"]

        with pytest.raises(RuntimeError, match="unexpected response"):
            ctx.set_session_mode("chat-77", "trust")


class TestTheCronCredential:
    def test_an_http_refusal_carries_status_and_body(self, ctx, gateway):
        """A 403 body names the remedy; ``HTTP Error 403: Forbidden`` alone does not."""
        body = json.dumps({"error": "Token required", "code": "token_required"}).encode()
        gateway.answers[("GET", "/api/chat/folders")] = urllib.error.HTTPError(
            "http://127.0.0.1:7788/api/chat/folders", 403, "Forbidden", {}, io.BytesIO(body)
        )

        with pytest.raises(RuntimeError, match="403.*Token required"):
            ctx.list_session_folders()

    @pytest.mark.parametrize(
        "method,path,call",
        [
            ("POST", "/api/chat/slots", lambda c: c.open_session("Nightly", folder_id="f9")),
            ("POST", "/api/chat?ws=1", lambda c: c.send_to_session("chat-77", "triage")),
            ("POST", "/api/chat/mode", lambda c: c.set_session_mode("chat-77", "trust")),
        ],
        ids=["open_session", "send_to_session", "set_session_mode"],
    )
    def test_a_switched_off_gateway_refusal_names_its_code(self, ctx, gateway, method, path, call):
        """The gateway owns the switch. The method surfaces its refusal code."""
        body = json.dumps(
            {
                "error": "session control is disabled in config (agent.session_control)",
                "code": "session_control_disabled",
            }
        ).encode()
        gateway.answers[(method, path)] = urllib.error.HTTPError(
            f"http://127.0.0.1:7788{path}", 403, "Forbidden", {}, io.BytesIO(body)
        )

        with pytest.raises(RuntimeError, match="session_control_disabled"):
            call(ctx)

        (req,) = gateway.seen
        assert req.get_header("X-session-key") == f"cron:{_JOB}"

    def test_a_transport_failure_raises(self, ctx, gateway):
        gateway.answers[("POST", "/api/chat/slots")] = OSError("connection refused")

        with pytest.raises(RuntimeError, match="connection refused"):
            ctx.open_session("x")

    def test_no_run_token_means_no_token_header(self, monkeypatch, gateway):
        """A directly constructed context outside a run presents only the secret and key."""
        monkeypatch.setenv("_KIROCREW_DIAL_PORT", "7788")
        monkeypatch.setenv("KIROCREW_INTERNAL_SECRET", "tok")
        monkeypatch.delenv("_KIROCREW_SECRET_FILE", raising=False)
        monkeypatch.delenv(STUB_SESSION_TOKEN_ENV, raising=False)
        gateway.answers[("GET", "/api/chat/folders")] = []

        ScriptContext(job=SimpleNamespace(id=_JOB, message="")).list_session_folders()

        (req,) = gateway.seen
        assert req.get_header("X-session-token") is None
        assert req.get_header("X-session-key") == f"cron:{_JOB}"

    def test_the_run_token_stays_in_the_env_for_mcp_children(self, ctx):
        """``call_tool`` servers inherit the token; the context reads it, never pops it."""
        import os

        assert os.environ[STUB_SESSION_TOKEN_ENV] == "signed-run-token"

    def test_notify_still_sends_the_same_credential(self, ctx, gateway):
        gateway.answers[("POST", "/api/send-message")] = {"ok": True}

        ctx.notify("done")

        (req,) = gateway.seen
        assert _cron_headers(req) == {
            "secret": "tok",
            "key": f"cron:{_JOB}",
            "token": "signed-run-token",
        }
