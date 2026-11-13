"""``kirocrew app update`` sends the dashboard's Update request through a running gateway.

The verb has no file-only form: with no gateway it refuses and changes nothing.
"""

from __future__ import annotations

import argparse
import socket
import types

import pytest


@pytest.fixture
def unix_socket_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give a runner with no ``socket.AF_UNIX`` (native Windows) one, so a test can
    drive the verb past its platform refusal. The gateway call is faked in every
    test that uses this, so no Unix socket is ever opened."""
    if not hasattr(socket, "AF_UNIX"):
        monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)


@pytest.mark.usefixtures("unix_socket_platform")
class TestCliUpdate:
    def test_it_goes_through_the_gateway(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client
        from kiro_crew.cli_commands_app import _handle_app

        sent: list[tuple[str, str]] = []

        def _toggle(name, action, *, payload=None):
            sent.append((name, action))
            return {"ok": True, "name": name, "message": "updated my-app to 1.1.0"}

        monkeypatch.setattr(client, "toggle_app", _toggle)
        _handle_app(types.SimpleNamespace(app_action="update", name="my-app"))
        assert sent == [("my-app", "update")]
        assert "updated my-app to 1.1.0" in capsys.readouterr().out

    def test_with_no_gateway_it_refuses_and_changes_nothing(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client
        import kiro_crew.cli_commands_app as cli_app

        monkeypatch.setattr(client, "toggle_app", lambda *a, **k: None)
        monkeypatch.setattr(
            cli_app, "update_app", lambda *a, **k: pytest.fail("file-only update"), raising=False
        )
        with pytest.raises(SystemExit) as exc:
            cli_app._handle_app(types.SimpleNamespace(app_action="update", name="my-app"))
        assert exc.value.code == 1
        assert "nothing was updated" in capsys.readouterr().err

    def test_a_gateway_refusal_is_reported(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client
        from kiro_crew.cli_commands_app import _handle_app

        def _refuse(*a, **k):
            raise client.AppGatewayError("app 'my-app' not installed", code="app_not_installed")

        monkeypatch.setattr(client, "toggle_app", _refuse)
        with pytest.raises(SystemExit):
            _handle_app(types.SimpleNamespace(app_action="update", name="my-app"))
        assert "not installed" in capsys.readouterr().err

    def test_a_refusal_ending_in_a_period_is_not_doubled(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client
        from kiro_crew.cli_commands_app import _handle_app

        def _refuse(*a, **k):
            raise client.AppGatewayError("app 'my-app' is already installed (v1). Uninstall first.")

        monkeypatch.setattr(client, "toggle_app", _refuse)
        with pytest.raises(SystemExit):
            _handle_app(types.SimpleNamespace(app_action="update", name="my-app"))
        err = capsys.readouterr().err
        assert "Uninstall first. " in err
        assert ".." not in err

    def test_the_parser_accepts_the_verb(self):
        from kiro_crew.cli_parser_agent import register_app_parser

        parser = argparse.ArgumentParser()
        register_app_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["app", "update", "my-app"])
        assert (args.app_action, args.name) == ("update", "my-app")

    def test_the_parser_accepts_an_optional_source(self):
        from kiro_crew.cli_parser_agent import register_app_parser

        parser = argparse.ArgumentParser()
        register_app_parser(parser.add_subparsers(dest="command"))
        assert parser.parse_args(["app", "update", "my-app"]).source is None
        args = parser.parse_args(["app", "update", "my-app", "--source", "/apps/my-app"])
        assert args.source == "/apps/my-app"


@pytest.mark.usefixtures("unix_socket_platform")
class TestCliUpdateSource:
    """``--source DIR`` updates from another directory than the recorded one."""

    @staticmethod
    def _capture(monkeypatch, answer=None):
        import kiro_crew.app_lifecycle_client as client

        sent: list[tuple[str, str, object]] = []

        def _toggle(name, action, *, payload=None):
            sent.append((name, action, payload))
            return answer or {"ok": True, "name": name, "message": f"updated {name}"}

        monkeypatch.setattr(client, "toggle_app", _toggle)
        return sent

    def test_a_relative_source_is_sent_absolute(self, monkeypatch, tmp_path):
        from kiro_crew.cli_commands_app import _handle_app

        (tmp_path / "my-app-v2").mkdir()
        monkeypatch.chdir(tmp_path)
        sent = self._capture(monkeypatch)
        # The gateway resolves paths against its own working directory, not this shell's.
        _handle_app(types.SimpleNamespace(app_action="update", name="my-app", source="my-app-v2"))
        assert sent == [("my-app", "update", {"source": str((tmp_path / "my-app-v2").resolve())})]

    def test_without_source_no_body_is_sent(self, monkeypatch):
        from kiro_crew.cli_commands_app import _handle_app

        sent = self._capture(monkeypatch)
        _handle_app(types.SimpleNamespace(app_action="update", name="my-app", source=None))
        assert sent == [("my-app", "update", None)]

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_an_empty_source_is_refused_before_any_request(self, monkeypatch, capsys, empty):
        from kiro_crew.cli_commands_app import _handle_app

        sent = self._capture(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _handle_app(types.SimpleNamespace(app_action="update", name="my-app", source=empty))
        assert exc.value.code == 1
        assert sent == []
        assert "--source is empty" in capsys.readouterr().err


class TestCliUpdateNativeWindows:
    """With no ``socket.AF_UNIX`` no gateway can take the request, so blaming one is wrong."""

    @pytest.mark.parametrize("with_source", [False, True])
    def test_it_refuses_naming_the_platform_before_any_request(
        self, monkeypatch, capsys, tmp_path, with_source
    ):
        import kiro_crew.app_lifecycle_client as client
        from kiro_crew.cli_commands_app import _handle_app

        sent: list[object] = []
        monkeypatch.setattr(client, "toggle_app", lambda *a, **k: sent.append(a))
        monkeypatch.delattr(socket, "AF_UNIX", raising=False)
        source = str(tmp_path) if with_source else None
        with pytest.raises(SystemExit) as exc:
            _handle_app(types.SimpleNamespace(app_action="update", name="my-app", source=source))
        assert exc.value.code == 1
        assert sent == []
        err = capsys.readouterr().err
        assert "not available on native Windows" in err
        assert "Update (or Sync) on the app's page" in err
        assert "Start it" not in err

    def test_help_names_the_limit(self, capsys):
        from kiro_crew.cli_parser_agent import register_app_parser

        parser = argparse.ArgumentParser()
        register_app_parser(parser.add_subparsers(dest="command"))
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["app", "update", "--help"])
        assert exc.value.code == 0
        assert "native Windows" in " ".join(capsys.readouterr().out.split())


@pytest.mark.usefixtures("unix_socket_platform")
class TestCliUpdateResult:
    """An update re-registers the new manifest, so it reports counts as enable does."""

    def test_registration_counts_are_printed(self, monkeypatch, capsys):
        import kiro_crew.app_lifecycle_client as client
        from kiro_crew.cli_commands_app import _handle_app

        answer = {
            "ok": True,
            "name": "my-app",
            "message": "updated my-app v1.0.0 -> v1.1.0",
            "registration": {"agents": ["my-agent"], "skills": [], "crons": [], "errors": []},
        }
        monkeypatch.setattr(client, "toggle_app", lambda *a, **k: answer)
        _handle_app(types.SimpleNamespace(app_action="update", name="my-app"))
        out = capsys.readouterr().out
        assert "Agents registered: 1" in out
        assert "Skills registered: 0" in out
        assert "kirocrew app enable" not in out

    def test_an_update_parked_for_consent_prints_no_counts_or_enable_hint(
        self, monkeypatch, capsys
    ):
        import kiro_crew.app_lifecycle_client as client
        from kiro_crew.cli_commands_app import _handle_app

        # The new version newly asks to control the owner's chats, so the gateway
        # leaves it off; that consent is the dashboard's alone.
        answer = {
            "ok": True,
            "name": "my-app",
            "message": "updated my-app v1 -> v2; disabled because this version newly "
            "requests session approval control",
            "notice": "session_approval_reconsent",
        }
        monkeypatch.setattr(client, "toggle_app", lambda *a, **k: answer)
        _handle_app(types.SimpleNamespace(app_action="update", name="my-app"))
        out = capsys.readouterr().out
        assert "disabled because this version newly requests session approval" in out
        assert "registered" not in out
        assert "kirocrew app enable" not in out
