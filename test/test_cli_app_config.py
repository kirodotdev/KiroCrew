"""``kirocrew app config NAME [get | set KEY VALUE]`` reads and sets an app's config.

It goes through the running gateway only. ``set`` writes only where the GET answer
carries the host handler's ``ETag``, and reports success only when the PUT's answer
says ``ok: true``.
"""

from __future__ import annotations

import argparse
import io
import json
import socket
import types
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

NAME = "demo"


@pytest.fixture
def unix_socket_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give a runner with no ``socket.AF_UNIX`` (native Windows) one, so a test can
    drive the verb past its platform refusal. The gateway call is faked in every
    test that uses this, so no Unix socket is ever opened."""
    if not hasattr(socket, "AF_UNIX"):
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)


class _Gateway:
    """A fake ``app_config_request``: answers each method from a script and records calls."""

    def __init__(self, get: Any = None, put: Any = None) -> None:
        self.answers = {"GET": get, "PUT": put}
        self.calls: list[tuple[str, str, object, str]] = []

    def __call__(
        self, app_name: str, method: str, *, config: object = None, if_match: str = ""
    ) -> Any:
        self.calls.append((app_name, method, config, if_match))
        answer = self.answers[method]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def puts(self) -> list[object]:
        return [config for _name, method, config, _tag in self.calls if method == "PUT"]


def _install(monkeypatch: pytest.MonkeyPatch, gateway: _Gateway) -> None:
    import kiro_crew.app_lifecycle_client as client

    monkeypatch.setattr(client, "app_config_request", gateway)


def _run(**kwargs: Any) -> None:
    from kiro_crew.cli_commands_app import _handle_app

    _handle_app(types.SimpleNamespace(app_action="config", name=NAME, **kwargs))


def _set(key: str, value: str) -> None:
    _run(config_action="set", key=key, value=value)


def _exit_code(call: Any, *args: Any) -> object:
    with pytest.raises(SystemExit) as exc:
        call(*args)
    return exc.value.code


_TAG = '"tag-of-the-read"'
_OK = ({"ok": True, "name": NAME}, "")
_CHECK = f"Run `kirocrew app config {NAME} get` to see the current value."


@pytest.mark.usefixtures("unix_socket_platform")
class TestGet:
    def test_it_prints_the_gateways_object_as_indented_json(self, monkeypatch, capsys):
        gateway = _Gateway(get=({"root": "/x", "port": 8080}, _TAG))
        _install(monkeypatch, gateway)
        _run(config_action="get")
        assert capsys.readouterr().out == json.dumps({"root": "/x", "port": 8080}, indent=2) + "\n"
        assert gateway.calls == [(NAME, "GET", None, "")]

    def test_no_action_is_get_and_any_json_prints(self, monkeypatch, capsys):
        _install(monkeypatch, _Gateway(get=(None, "")))
        _run(config_action=None)
        assert capsys.readouterr().out == "null\n"

    def test_a_control_character_in_a_value_is_escaped(self, monkeypatch, capsys):
        _install(monkeypatch, _Gateway(get=({"name": "a\x1b[2Jb"}, _TAG)))
        _run(config_action="get")
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\\u001b[2Jb" in out

    def test_a_timeout_prints_its_own_line(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client

        _install(
            monkeypatch,
            _Gateway(get=client.AppGatewayTimeout("the gateway did not answer within 30 s")),
        )
        assert _exit_code(lambda: _run(config_action="get")) == 1
        err = capsys.readouterr().err
        assert "did not answer within 30 s" in err
        assert "kirocrew app list" not in err
        assert "300 s" not in err


@pytest.mark.usefixtures("unix_socket_platform")
class TestSet:
    def test_it_merges_one_key_and_reports_success(self, monkeypatch, capsys):
        gateway = _Gateway(get=({"a": 1}, _TAG), put=_OK)
        _install(monkeypatch, gateway)
        _set("root", "/x")
        assert gateway.puts == [{"a": 1, "root": "/x"}]
        assert capsys.readouterr().out.strip() == f'✅ {NAME}: root = "/x"'

    @pytest.mark.parametrize(
        ("key", "raw", "stored"),
        [
            ("n", "5", 5),
            ("flag", "true", True),
            ("obj", '{"x":1}', {"x": 1}),
            ("a.b", "1", 1),
            ("code", '"0123"', "0123"),
        ],
        ids=["int", "bool", "object", "literal-dotted-key", "json-string"],
    )
    def test_the_value_is_typed_as_config_set_types_it(self, monkeypatch, key, raw, stored):
        gateway = _Gateway(get=({}, _TAG), put=_OK)
        _install(monkeypatch, gateway)
        _set(key, raw)
        assert gateway.puts == [{key: stored}]

    def test_the_put_sends_the_gets_tag_as_if_match(self, monkeypatch):
        gateway = _Gateway(get=({"a": 1}, _TAG), put=_OK)
        _install(monkeypatch, gateway)
        _set("root", "/x")
        assert [tag for _n, method, _c, tag in gateway.calls if method == "PUT"] == [_TAG]

    def test_an_answer_without_an_etag_is_refused_before_any_put(self, monkeypatch, capsys):
        gateway = _Gateway(get=({"config": {"a": 1}, "providers": []}, ""), put=_OK)
        _install(monkeypatch, gateway)
        assert _exit_code(_set, "root", "/x") == 1
        assert gateway.puts == []
        err = capsys.readouterr().err
        assert "not answered by the host's app config store" in err
        assert "Nothing was set" in err

    def test_a_non_object_is_refused_before_any_put(self, monkeypatch, capsys):
        gateway = _Gateway(get=([1], _TAG), put=_OK)
        _install(monkeypatch, gateway)
        assert _exit_code(_set, "root", "/x") == 1
        assert gateway.puts == []
        assert "not a JSON object" in capsys.readouterr().err

    def test_a_non_finite_number_is_refused_before_any_put(self, monkeypatch, capsys):
        gateway = _Gateway(get=({}, _TAG), put=_OK)
        _install(monkeypatch, gateway)
        assert _exit_code(_set, "x", "nan") == 1
        assert gateway.puts == []
        assert "non-finite number" in capsys.readouterr().err

    def test_an_empty_key_is_refused_before_any_call(self, monkeypatch, capsys):
        gateway = _Gateway(get=({}, _TAG), put=_OK)
        _install(monkeypatch, gateway)
        assert _exit_code(_set, "", "1") == 1
        assert gateway.calls == []

    def test_a_put_answer_without_ok_true_is_not_success(self, monkeypatch, capsys):
        _install(monkeypatch, _Gateway(get=({}, _TAG), put=({"name": NAME}, "")))
        assert _exit_code(_set, "root", "/x") == 1
        captured = capsys.readouterr()
        assert "✅" not in captured.out
        assert "did not confirm the change" in captured.err
        assert _CHECK in captured.err

    def test_a_get_phase_timeout_prints_its_own_line(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client

        gateway = _Gateway(get=client.AppGatewayTimeout("the gateway did not answer within 30 s"))
        _install(monkeypatch, gateway)
        assert _exit_code(_set, "root", "/x") == 1
        err = capsys.readouterr().err
        assert "did not answer within 30 s" in err
        assert "Nothing was set" in err
        assert "kirocrew app list" not in err
        assert gateway.puts == []

    def test_a_put_phase_timeout_says_the_outcome_is_unknown(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client

        timeout = client.AppGatewayTimeout("the gateway did not answer within 30 s")
        _install(monkeypatch, _Gateway(get=({}, _TAG), put=timeout))
        assert _exit_code(_set, "root", "/x") == 1
        err = capsys.readouterr().err
        assert "may or may not have been set" in err
        assert "kirocrew app list" not in err
        assert _CHECK in err

    def test_an_http_error_answer_to_the_put_claims_no_write(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client

        refusal = client.AppGatewayError("failed to write config: disk full")
        _install(monkeypatch, _Gateway(get=({}, _TAG), put=refusal))
        assert _exit_code(_set, "root", "/x") == 1
        err = capsys.readouterr().err
        assert "failed to write config: disk full" in err
        assert "nothing was set" not in err.lower()
        assert _CHECK in err

    def test_a_gateway_refusal_is_reported(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client

        refusal = client.AppGatewayError("only the owner may do this", code="owner_only")
        _install(monkeypatch, _Gateway(get=({}, _TAG), put=refusal))
        assert _exit_code(_set, "root", "/x") == 1
        assert "only the owner may do this" in capsys.readouterr().err

    def test_with_no_gateway_nothing_is_sent_or_written(self, monkeypatch, capsys, tmp_path):
        home = tmp_path / "crew"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        gateway = _Gateway(get=None, put=_OK)
        _install(monkeypatch, gateway)
        assert _exit_code(_set, "root", "/x") == 1
        assert gateway.puts == []
        err = capsys.readouterr().err
        assert "nothing was set" in err
        assert "kirocrew gateway" in err
        assert list(home.rglob("*")) == []


class TestRefusedBeforeTheGateway:
    def test_native_windows_is_refused_before_the_secret_is_read(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client

        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
        monkeypatch.setattr(client, "read_local_secret", lambda *a, **k: pytest.fail("secret read"))
        assert _exit_code(_set, "root", "/x") == 1
        err = capsys.readouterr().err
        assert "not available on native Windows" in err
        assert "data/config.json" in err

    @pytest.mark.usefixtures("unix_socket_platform")
    def test_an_unsafe_name_is_refused_before_any_client_call(self, monkeypatch, capsys):
        from kiro_crew.cli_commands_app import _handle_app

        gateway = _Gateway(get=({}, _TAG), put=_OK)
        _install(monkeypatch, gateway)
        args = types.SimpleNamespace(
            app_action="config", name="..", config_action="set", key="k", value="1"
        )
        assert _exit_code(_handle_app, args) == 1
        assert gateway.calls == []
        assert "unsafe app name" in capsys.readouterr().err


class TestParser:
    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        from kiro_crew.cli_parser_agent import register_app_parser

        parser = argparse.ArgumentParser()
        register_app_parser(parser.add_subparsers(dest="command"))
        return parser

    def test_set_without_a_value_exits_2(self, capsys):
        assert _exit_code(self._parser().parse_args, ["app", "config", NAME, "set", "k"]) == 2
        assert "required: value" in capsys.readouterr().err

    def test_it_parses_get_set_and_the_bare_form(self):
        parser = self._parser()
        assert parser.parse_args(["app", "config", NAME]).config_action is None
        assert parser.parse_args(["app", "config", NAME, "get"]).config_action == "get"
        args = parser.parse_args(["app", "config", NAME, "set", "a.b", "1"])
        assert (args.app_action, args.name, args.config_action, args.key, args.value) == (
            "config",
            NAME,
            "set",
            "a.b",
            "1",
        )


class _Response:
    """A context-manager stand-in for ``unix_socket_urlopen``'s answer, with headers."""

    def __init__(self, payload: Any, headers: dict[str, str] | None = None) -> None:
        self._raw = json.dumps(payload).encode()
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


class TestClientTransport:
    """The real client helper, over a fake socket opener."""

    @pytest.fixture
    def opener(self, monkeypatch: pytest.MonkeyPatch) -> list[urllib.request.Request]:
        import kiro_crew.app_lifecycle_client as client

        requests: list[urllib.request.Request] = []

        def _open(request, *, timeout, socket_path, verify_peer):
            requests.append(request)
            if request.full_url.endswith("/api/token/local?ttl=2m"):
                return _Response({"token": "dashboard-credential"})
            if "/config?" in request.full_url:
                answer = {"a": 1} if request.get_method() == "GET" else {"ok": True}
                return _Response(answer, {"ETag": _TAG})
            return _Response({"ok": True, "message": "enabled demo"})

        monkeypatch.setattr(client, "resolve_client_port_ex", lambda _port: (8123, True))
        monkeypatch.setattr(client, "read_local_secret", lambda _port: "local-secret")
        monkeypatch.setattr(client, "unix_socket_urlopen", _open)
        return requests

    def test_get_sends_get_and_reads_the_etag(self, opener):
        import kiro_crew.app_lifecycle_client as client

        assert client.app_config_request(NAME, "GET") == ({"a": 1}, _TAG)
        assert opener[1].get_method() == "GET"
        assert opener[1].data is None
        assert f"/api/apps/{NAME}/config?" in opener[1].full_url

    @pytest.mark.usefixtures("unix_socket_platform")
    def test_set_puts_the_merged_object(self, opener, capsys):
        _set("root", "/x")
        put = opener[-1]
        assert put.get_method() == "PUT"
        assert f"/api/apps/{NAME}/config?" in put.full_url
        assert json.loads(put.data) == {"a": 1, "root": "/x"}
        assert put.get_header("If-match") == _TAG
        assert "✅" in capsys.readouterr().out

    def test_lifecycle_calls_still_post(self, opener, capsys):
        import kiro_crew.app_lifecycle_client as client

        assert client.toggle_app(NAME, "enable") == {"ok": True, "message": "enabled demo"}
        assert opener[-1].get_method() == "POST"
        assert "may take up to 5 minutes" in capsys.readouterr().err

    def test_the_credential_mint_waits_as_long_as_kirocrew_token(self, monkeypatch, opener):
        """A busy gateway can take seconds to answer the mint; a shorter wait fails the verb."""
        import kiro_crew.app_lifecycle_client as client
        from kiro_crew.cli_server_requests import _TOKEN_REQUEST_TIMEOUT

        answer = client.unix_socket_urlopen
        mint_waits: list[float] = []

        def _timed(request, *, timeout, **kwargs):
            if request.full_url.endswith("/api/token/local?ttl=2m"):
                mint_waits.append(timeout)
            return answer(request, timeout=timeout, **kwargs)

        monkeypatch.setattr(client, "unix_socket_urlopen", _timed)
        client.app_config_request(NAME, "GET")
        assert len(mint_waits) == 1
        assert mint_waits[0] >= _TOKEN_REQUEST_TIMEOUT

    def test_an_answer_without_headers_has_no_etag(self, monkeypatch, opener):
        import kiro_crew.app_lifecycle_client as client

        def _plain(request, *, timeout, socket_path, verify_peer):
            if request.full_url.endswith("/api/token/local?ttl=2m"):
                return _Response({"token": "dashboard-credential"})
            return _Response({"config": {}, "providers": []})

        monkeypatch.setattr(client, "unix_socket_urlopen", _plain)
        assert client.app_config_request(NAME, "GET") == ({"config": {}, "providers": []}, "")

    def test_an_http_error_answer_carries_the_gateways_text(self, monkeypatch, opener):
        import kiro_crew.app_lifecycle_client as client

        def _refuse(request, *, timeout, socket_path, verify_peer):
            if request.full_url.endswith("/api/token/local?ttl=2m"):
                return _Response({"token": "dashboard-credential"})
            body = io.BytesIO(json.dumps({"error": "app 'nosuch' not installed"}).encode())
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", Message(), body)

        monkeypatch.setattr(client, "unix_socket_urlopen", _refuse)
        with pytest.raises(client.AppGatewayError, match="not installed"):
            client.app_config_request("nosuch", "GET")
