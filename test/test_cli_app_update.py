"""``kirocrew app update`` sends the dashboard's Update request through a running gateway.

The verb has no file-only form: with no gateway it refuses and changes nothing.
"""

from __future__ import annotations

import argparse
import types

import pytest


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

    def test_the_parser_accepts_the_verb(self):
        from kiro_crew.cli_parser_agent import register_app_parser

        parser = argparse.ArgumentParser()
        register_app_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["app", "update", "my-app"])
        assert (args.app_action, args.name) == ("update", "my-app")
