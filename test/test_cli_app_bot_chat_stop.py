"""``kirocrew app disable|uninstall`` when a Bot chat running the app's tools did not stop.

The gateway answers 200 with ``bot_chat_stop_failed`` (the action landed) or 409
``bot_chat_stop_incomplete`` (the app was already gone). The CLI names the Bots,
says how to stop them, and exits non-zero. Each test names the change that turns
it red:

* a 200 carrying the field -- drop the ``bot_chat_stop_lines`` call from
  ``print_result``, or the ``sys.exit(1)`` on its False in ``_apply_through_gateway``;
* a ``bot_chat_stop_incomplete`` 409 -- drop its branch in ``_apply_through_gateway``.
"""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import patch

import pytest

import kiro_crew.app_lifecycle_client as client
from kiro_crew import cli_commands as cc


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _run(action: str, answer: Any) -> None:
    kwargs: dict[str, Any] = {"app_action": action, "name": "demo"}
    if action == "uninstall":
        kwargs["purge_data"] = False
    with patch("kiro_crew.app_lifecycle_client.toggle_app", side_effect=answer):
        cc._handle_app(_ns(**kwargs))


@pytest.mark.parametrize("action", ["disable", "uninstall"])
def test_a_landed_action_whose_bot_chat_did_not_stop_exits_non_zero(
    action: str, capsys: pytest.CaptureFixture[str]
) -> None:
    landed = {"ok": True, "name": "demo", "bot_chat_stop_failed": ["atlas"]}
    with pytest.raises(SystemExit) as stopped:
        _run(action, lambda *a, **k: landed)
    assert stopped.value.code == 1
    err = capsys.readouterr().err
    assert "atlas" in err
    assert f"kirocrew app {action} demo" in err
    assert "kirocrew restart" in err


def test_a_stop_incomplete_refusal_names_the_bots_and_exits_non_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _refuse(*a: Any, **k: Any) -> None:
        raise client.AppGatewayError(
            "app 'demo' is not installed, but its Bot chats could not be stopped",
            code="bot_chat_stop_incomplete",
            bots=("atlas",),
        )

    with pytest.raises(SystemExit) as stopped:
        _run("uninstall", _refuse)
    assert stopped.value.code == 1
    err = capsys.readouterr().err
    assert "atlas" in err and "kirocrew app uninstall demo" in err
    assert "gateway refused" not in err


def test_a_clean_disable_still_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    _run("disable", lambda *a, **k: {"ok": True, "name": "demo"})
    assert "Bot chats" not in capsys.readouterr().err


def test_the_refusal_body_carries_its_bots() -> None:
    body = {"code": "bot_chat_stop_incomplete", "bot_chat_stop_failed": ["atlas"]}
    assert client.stop_failed_bots(body) == ("atlas",)
    assert client.stop_failed_bots({"code": "bot_chat_stop_incomplete", "bots": ["nova"]}) == (
        "nova",
    )
