"""``kirocrew memory forget``: delete one semantic key through the gateway's owner gate.

Three layers, each pinned in both directions:

* **The CLI verb** refuses from an agent session's shell and without a terminal,
  before any store opens; otherwise it shows the value, asks the person to type the
  key again, and only then calls the gateway, passing the shown value for a
  compare-and-delete. The gateway is replaced by a fake that applies the delete to
  the real store, so the store-side effects are real.
* **The client** sends the delete only through the owner-only socket with a
  credential the gateway minted, and turns every refusal (no gateway, a non-owner
  403, a changed value) into "nothing was forgotten".
* **The route** keeps its owner gate in front of the new optional body, and the
  body's two fields do what they say.
"""

from __future__ import annotations

import argparse
import io
import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew import mcp_caller
from kiro_crew import memory_forget_client as client
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import config_dir
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
from kiro_crew.vector_memory import VectorMemoryStore

# Each CLI test writes ``config.json`` into its own data home and drops the
# process-wide config cache, so two workers racing that global is a flake.
pytestmark = pytest.mark.xdist_group("memory_forget_cli")

_KEY = "pref.editor"
_WINNER = "pref.editor_choice"


# ── CLI verb ──────────────────────────────────────────────────────────────


@pytest.fixture
def store_path(monkeypatch: pytest.MonkeyPatch):
    """The default store's file, seeded with two live keys and then closed."""
    payload = {
        "memory_stores": {DEFAULT_MEMORY_STORE: {}},
        "default_memory_store": DEFAULT_MEMORY_STORE,
    }
    (config_dir() / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    loader_mod._invalidate_config_cache()
    path = resolve_store_path(DEFAULT_MEMORY_STORE)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = VectorMemoryStore(db_path=path)
    store.init()
    try:
        store.set_semantic(_KEY, "vim", 0.9, "test")
        store.set_semantic(_WINNER, "vim, with relative numbers", 0.9, "test")
    finally:
        store.close()
    return path


def _read(path, key: str) -> dict | None:
    store = VectorMemoryStore(db_path=path)
    store.init()
    try:
        return store.get_semantic(key)
    finally:
        store.close()


@pytest.fixture
def gateway(store_path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A fake gateway that applies the route's delete to the real store."""
    calls: list[dict[str, Any]] = []

    def fake_forget(key: str, *, expect_value_json: str, superseded_by: str | None) -> None:
        calls.append(
            {"key": key, "expect_value_json": expect_value_json, "superseded_by": superseded_by}
        )
        store = VectorMemoryStore(db_path=store_path)
        store.init()
        try:
            ok = store.delete_semantic(
                key,
                source="user_explicit",
                expect_value_json=expect_value_json,
                superseded_by=superseded_by,
            )
        finally:
            store.close()
        if not ok:
            raise client.AlreadyAbsent("already absent")

    monkeypatch.setattr(client, "forget_via_gateway", fake_forget)
    return calls


def _owner_terminal(monkeypatch: pytest.MonkeyPatch, typed: str = _KEY) -> None:
    """No agent session names this process, a person is at the terminal, and types *typed*."""
    monkeypatch.setattr(mcp_caller, "resolve_own_identity", lambda **_: mcp_caller.OwnIdentity())
    monkeypatch.setattr(cc, "_interactive_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": typed)


def _forget(key: str = _KEY, *, superseded_by: str | None = None) -> None:
    cc._memory_cmd(argparse.Namespace(mem_action="forget", key=key, superseded_by=superseded_by))


class TestOwnerTerminal:
    def test_confirmed_forget_deletes_and_passes_the_shown_value(
        self, store_path, gateway, monkeypatch, capsys
    ):
        _owner_terminal(monkeypatch)
        _forget()
        out = capsys.readouterr().out
        assert f"{_KEY}: vim" in out
        assert "Forgotten." in out
        assert gateway == [
            {"key": _KEY, "expect_value_json": json.dumps("vim"), "superseded_by": None}
        ]
        assert _read(store_path, _KEY) is None
        assert _read(store_path, _WINNER) is not None

    def test_superseded_by_reaches_the_gateway(self, store_path, gateway, monkeypatch, capsys):
        _owner_terminal(monkeypatch)
        _forget(superseded_by=_WINNER)
        assert f"superseded by {_WINNER}" in capsys.readouterr().out
        assert gateway[0]["superseded_by"] == _WINNER
        assert _read(store_path, _KEY) is None

    def test_wrong_confirmation_deletes_nothing(self, store_path, gateway, monkeypatch, capsys):
        _owner_terminal(monkeypatch, typed="pref.other")
        with pytest.raises(SystemExit) as exc:
            _forget()
        assert exc.value.code == 1
        assert "Not confirmed" in capsys.readouterr().err
        assert gateway == []
        assert _read(store_path, _KEY) is not None

    def test_eof_at_the_prompt_deletes_nothing(self, store_path, gateway, monkeypatch, capsys):
        _owner_terminal(monkeypatch)

        def eof(_prompt: str = "") -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        with pytest.raises(SystemExit):
            _forget()
        assert gateway == []
        assert _read(store_path, _KEY) is not None

    def test_absent_key_exits_1_without_asking(self, store_path, gateway, monkeypatch, capsys):
        _owner_terminal(monkeypatch)
        monkeypatch.setattr("builtins.input", MagicMock(side_effect=AssertionError("asked")))
        with pytest.raises(SystemExit) as exc:
            _forget("pref.missing")
        assert exc.value.code == 1
        assert "Already absent: pref.missing" in capsys.readouterr().err
        assert gateway == []

    def test_second_run_reports_absent(self, store_path, gateway, monkeypatch, capsys):
        _owner_terminal(monkeypatch)
        _forget()
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            _forget()
        assert exc.value.code == 1
        assert f"Already absent: {_KEY}" in capsys.readouterr().err

    @pytest.mark.parametrize("winner", [_KEY, "pref.missing"])
    def test_bad_winner_is_refused_before_asking(
        self, store_path, gateway, monkeypatch, capsys, winner
    ):
        _owner_terminal(monkeypatch)
        monkeypatch.setattr("builtins.input", MagicMock(side_effect=AssertionError("asked")))
        with pytest.raises(SystemExit) as exc:
            _forget(superseded_by=winner)
        assert exc.value.code == 1
        assert "nothing was forgotten" in capsys.readouterr().err
        assert gateway == []
        assert _read(store_path, _KEY) is not None


def test_the_parser_routes_forget_with_its_flag(monkeypatch, tmp_path):
    """``kirocrew memory forget <key> --superseded-by <key>`` reaches the verb."""
    seen: list[argparse.Namespace] = []
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(cc, "_memory_verb", seen.append)
    monkeypatch.setattr(
        "sys.argv", ["kirocrew", "memory", "forget", _KEY, "--superseded-by", _WINNER]
    )
    from kiro_crew.cli import main

    try:
        main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert [(a.mem_action, a.key, a.superseded_by) for a in seen] == [("forget", _KEY, _WINNER)]


class TestRefusedBeforeAnyStoreOpens:
    """Every refusal here must leave the store unopened and the gateway uncalled."""

    @pytest.fixture(autouse=True)
    def _no_store_no_gateway(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        # The independent patch stack, so a test calling monkeypatch.undo() cannot
        # lift these guards.
        _floor_monkeypatch.setattr(
            cc, "_admitted_store_path", MagicMock(side_effect=AssertionError("store opened"))
        )
        _floor_monkeypatch.setattr(
            client, "forget_via_gateway", MagicMock(side_effect=AssertionError("gateway called"))
        )
        _floor_monkeypatch.setattr("builtins.input", MagicMock(side_effect=AssertionError("asked")))

    def _assert_refused(self, capsys, needle: str) -> None:
        with pytest.raises(SystemExit) as exc:
            _forget()
        assert exc.value.code == 1
        assert needle in capsys.readouterr().err

    def test_named_agent_session_is_refused(self, monkeypatch, capsys):
        monkeypatch.setattr(
            mcp_caller,
            "resolve_own_identity",
            lambda **_: mcp_caller.OwnIdentity(session_key="dashboard:chat-1"),
        )
        monkeypatch.setattr(cc, "_interactive_terminal", lambda: True)
        self._assert_refused(capsys, "agent session's shell")

    def test_unsettled_identity_is_refused(self, monkeypatch, capsys):
        monkeypatch.setattr(
            mcp_caller, "resolve_own_identity", lambda **_: mcp_caller.OwnIdentity(failed=True)
        )
        monkeypatch.setattr(cc, "_interactive_terminal", lambda: True)
        self._assert_refused(capsys, "agent session's shell")

    def test_session_env_is_refused_through_the_real_ladder(self, monkeypatch, capsys):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-2")
        monkeypatch.setattr(cc, "_interactive_terminal", lambda: True)
        self._assert_refused(capsys, "agent session's shell")

    def test_no_terminal_is_refused_through_the_real_check(self, monkeypatch, capsys):
        monkeypatch.setattr(
            mcp_caller, "resolve_own_identity", lambda **_: mcp_caller.OwnIdentity()
        )
        monkeypatch.setattr(cc.sys, "stdin", io.StringIO(_KEY + "\n"))
        self._assert_refused(capsys, "interactive terminal")


# ── client ────────────────────────────────────────────────────────────────


class _Resp(io.BytesIO):
    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _http_error(url: str, status: int, body: dict[str, str]) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url, status, "refused", {}, io.BytesIO(json.dumps(body).encode())  # type: ignore[arg-type]
    )


_GATEWAY_PID = 4321


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Socket transport stand-in: records each request and replays scripted answers.

    Each fake connection runs the client's ``verify_peer`` first, as the real
    opener does, against a peer pid the test controls; a refusal there sends
    nothing.
    """
    script: dict[str, Any] = {
        "sent": [],
        "delete": {"ok": True},
        "secret": "s3cret",
        "peer": _GATEWAY_PID,
        "record": (_GATEWAY_PID, "start-1"),
        "live_start": "start-1",
    }
    monkeypatch.setattr(client, "resolve_client_port_ex", lambda _p: (4242, True))
    monkeypatch.setattr(client, "read_local_secret", lambda _port: script["secret"])
    monkeypatch.setattr(client, "dashboard_socket_path", lambda _port: "/run/dash.sock")
    # The peer proof is the shared one in ``cli_server_peer``: the record and its
    # start token are read through ``run_marker``, and the kernel peer pid through
    # the ``kiro_crew.cli_server`` facade, which reaches that owner.
    from kiro_crew.instances import run_marker

    monkeypatch.setattr(run_marker, "read_pid_record_path", lambda _p: script["record"])
    monkeypatch.setattr(run_marker, "pid_start_token", lambda _pid: script["live_start"])
    monkeypatch.setattr("kiro_crew.cli_server.get_peer_pid", lambda _sock: script["peer"])

    def fake_open(req, timeout, *, socket_path, verify_peer):
        assert socket_path == "/run/dash.sock"
        verify_peer(object())
        script["sent"].append(req)
        if "/api/token/local" in req.full_url:
            mint = script.get("mint", {"token": "tok"})
            if isinstance(mint, BaseException):
                raise mint
            return _Resp(json.dumps(mint).encode())
        answer = script["delete"]
        if isinstance(answer, BaseException):
            raise answer
        return _Resp(json.dumps(answer).encode())

    monkeypatch.setattr(client, "unix_socket_urlopen", fake_open)
    return script


class TestClient:
    def test_delete_carries_the_minted_credential_and_body(self, transport):
        client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=_WINNER)
        mint, delete = transport["sent"]
        assert mint.get_header("X-local-secret") == "s3cret"
        assert delete.get_method() == "DELETE"
        assert delete.full_url.startswith("http://127.0.0.1:4242/api/memory/semantic/pref.editor?")
        assert "token=tok" in delete.full_url
        assert json.loads(delete.data) == {
            "expect_value_json": '"vim"',
            "superseded_by": _WINNER,
        }

    def test_a_rebound_socket_receives_nothing(self, transport):
        transport["peer"] = _GATEWAY_PID + 1
        with pytest.raises(client.ForgetRefused, match="not answered by the recorded gateway"):
            client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=None)
        assert transport["sent"] == []

    def test_a_recycled_gateway_pid_receives_nothing(self, transport):
        # A start token that differs from the live pid's fails the record's
        # freshness proof, so nothing is sent.
        transport["live_start"] = "start-2"
        with pytest.raises(client.ForgetRefused, match="pid record|recorded gateway"):
            client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=None)
        assert transport["sent"] == []

    @pytest.mark.parametrize("record", [None, (_GATEWAY_PID, "")])
    def test_an_unproven_pid_record_sends_nothing(self, transport, record):
        transport["record"] = record
        with pytest.raises(client.ForgetRefused, match="pid record"):
            client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=None)
        assert transport["sent"] == []

    @pytest.mark.asyncio
    async def test_the_clients_own_delete_passes_the_real_route_gate(self, transport):
        """The request the client builds, headers and body as sent, clears the route."""
        client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=_WINNER)
        delete = transport["sent"][-1]
        store = _route_store()
        req = _route_request(
            store,
            body=json.loads(delete.data),
            headers={k.title(): v for k, v in delete.header_items()},
        )
        status, _ = await _delete(req)
        assert status == 200
        store.delete_semantic.assert_called_once_with(
            _KEY, source="user_explicit", expect_value_json='"vim"', superseded_by=_WINNER
        )

    def test_no_gateway_sends_nothing(self, transport):
        transport["secret"] = ""
        with pytest.raises(client.ForgetRefused, match="Nothing was forgotten"):
            client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=None)
        assert transport["sent"] == []

    def test_non_owner_refusal_sends_no_delete(self, transport):
        transport["mint"] = _http_error("u", 403, {"error": "not the owner"})
        with pytest.raises(client.ForgetRefused, match="Nothing was forgotten"):
            client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=None)
        assert len(transport["sent"]) == 1

    def test_owner_gate_refusal_on_the_route_is_reported(self, transport):
        transport["delete"] = _http_error("u", 403, {"error": "x", "code": "owner_only"})
        with pytest.raises(client.ForgetRefused, match="only from the owner"):
            client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=None)

    def test_changed_value_is_reported(self, transport):
        transport["delete"] = _http_error("u", 409, {"error": "x", "code": "value_changed"})
        with pytest.raises(client.ForgetRefused, match="changed after it was shown"):
            client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=None)

    def test_404_is_already_absent(self, transport):
        transport["delete"] = _http_error("u", 404, {"error": "not found"})
        with pytest.raises(client.AlreadyAbsent):
            client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=None)

    def test_missing_socket_is_refused(self, transport):
        transport["mint"] = urllib.error.URLError(FileNotFoundError("gone"))
        with pytest.raises(client.ForgetRefused, match="dashboard Memory page"):
            client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=None)

    def test_unconfirmed_answer_is_refused(self, transport):
        transport["delete"] = {"ok": False}
        with pytest.raises(client.ForgetRefused, match="did not confirm"):
            client.forget_via_gateway(_KEY, expect_value_json='"vim"', superseded_by=None)


# ── route ─────────────────────────────────────────────────────────────────


def _route_request(
    store: Any,
    *,
    body: Any = None,
    user: str = "local-app",
    headers: dict[str, str] | None = None,
) -> Any:
    """A DELETE request carrying *user*'s dashboard claims and an optional JSON body.

    The state mirrors ``handlers_memory_coverage_helpers._make_state``: a readable,
    unbound Global V1 session whose vector store is *store*.
    """
    state = MagicMock()
    state.conversation_log.get_metadata_status.return_value = ({}, True)
    state.context_builder = MagicMock(memory=MagicMock(vector_store=store))
    state.consolidator = None
    state._slots = {}
    state._restricted_keys = set()
    state.owner_id = ""
    raw = b"" if body is None else json.dumps(body).encode()

    async def chunks(_size: int):
        if raw:
            yield raw

    req = MagicMock()
    req.app = {"state": state}
    req.method = "DELETE"
    req.query = {}
    req.match_info = {"key": _KEY}
    req.headers = {"X-Session-Key": "dashboard:ui"} if headers is None else headers
    req.can_read_body = bool(raw)
    req.content_length = len(raw)
    req.content_type = "application/json"
    req.charset = None
    req.content.iter_chunked = chunks
    claims = {"app": "", "user": user}
    req.__contains__.side_effect = lambda key: key in claims
    req.__getitem__.side_effect = lambda key: claims[key]
    req.get.side_effect = lambda key, *default: claims.get(key, *default)
    return req


def _route_store(**attrs: Any) -> Any:
    store = MagicMock()
    store.delete_semantic.return_value = True
    store.get_semantic.return_value = {"key": _WINNER, "value_json": '"x"'}
    for name, value in attrs.items():
        setattr(store, name, value)
    return store


async def _delete(req: Any) -> tuple[int, Any]:
    import kiro_crew.dashboard.handlers.memory as mem_mod

    resp = await mem_mod.api_memory_semantic_delete(req)
    return resp.status, json.loads(resp.text or "null")


class TestRoute:
    @pytest.mark.asyncio
    async def test_non_owner_with_a_body_is_refused_before_any_delete(self):
        store = _route_store()
        status, body = await _delete(
            _route_request(store, body={"expect_value_json": '"vim"'}, user="someone-else")
        )
        assert status == 403
        assert body["code"] == "owner_only"
        store.delete_semantic.assert_not_called()

    @pytest.mark.asyncio
    async def test_body_fields_reach_delete_semantic(self):
        store = _route_store()
        status, _ = await _delete(
            _route_request(store, body={"expect_value_json": '"vim"', "superseded_by": _WINNER})
        )
        assert status == 200
        store.delete_semantic.assert_called_once_with(
            _KEY, source="user_explicit", expect_value_json='"vim"', superseded_by=_WINNER
        )

    @pytest.mark.asyncio
    async def test_bodiless_delete_is_unchanged(self):
        store = _route_store()
        status, _ = await _delete(_route_request(store))
        assert status == 200
        store.delete_semantic.assert_called_once_with(_KEY, source="user_explicit")

    @pytest.mark.asyncio
    async def test_changed_value_is_409(self):
        store = _route_store(delete_semantic=MagicMock(return_value=False))
        status, body = await _delete(_route_request(store, body={"expect_value_json": '"old"'}))
        assert (status, body["code"]) == (409, "value_changed")

    @pytest.mark.asyncio
    async def test_absent_key_with_expect_is_404(self):
        store = _route_store(
            delete_semantic=MagicMock(return_value=False), get_semantic=MagicMock(return_value=None)
        )
        status, _ = await _delete(_route_request(store, body={"expect_value_json": '"old"'}))
        assert status == 404

    @pytest.mark.asyncio
    async def test_winner_equal_to_key_is_400(self):
        store = _route_store()
        status, body = await _delete(_route_request(store, body={"superseded_by": _KEY}))
        assert (status, body["code"]) == (400, "superseded_by_self")
        store.delete_semantic.assert_not_called()

    @pytest.mark.asyncio
    async def test_absent_winner_is_409(self):
        store = _route_store(get_semantic=MagicMock(return_value=None))
        status, body = await _delete(_route_request(store, body={"superseded_by": "pref.gone"}))
        assert (status, body["code"]) == (409, "superseded_by_absent")
        store.delete_semantic.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad", [{"reason": "x"}, {"expect_value_json": 3}, {"superseded_by": None}]
    )
    async def test_unknown_or_non_string_fields_are_400(self, bad):
        store = _route_store()
        status, body = await _delete(_route_request(store, body=bad))
        assert (status, body["code"]) == (400, "invalid_forget_body")
        store.delete_semantic.assert_not_called()
