"""``kirocrew update`` says when the running gateway is older than what is installed.

An update can be on disk without being served: the managed venv prepares the new
tree on a gateway start and the next start applies it. "Already on the latest
version!" alone then reads as nothing to do while the dashboard still shows the old
version, so the up-to-date verdict names the running version and the restart.
"""

from __future__ import annotations

import io
import json

import pytest

from kiro_crew import cli_server
from kiro_crew.doctor_checks import services
from kiro_crew.platform.update_layout import InstallLayout
from kiro_crew.service import live_target

_HINT = "kirocrew restart"


@pytest.fixture
def versions(monkeypatch):
    """Pin the client port and return a setter for (running, on disk)."""
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda cli_port: 7777)

    def _set(running: str | None, on_disk: str | None) -> None:
        monkeypatch.setattr(services, "_running_gateway_version", lambda port: running)
        monkeypatch.setattr(live_target, "selected_version", lambda: (on_disk, None))

    return _set


def test_an_older_running_gateway_names_its_version_and_the_restart(versions, capsys) -> None:
    versions("0.11.2", "0.12.1")

    cli_server._report_pending_restart()

    out = capsys.readouterr().out
    assert "still on 0.11.2; 0.12.1 is installed" in out
    assert _HINT in out


@pytest.mark.parametrize(
    ("running", "on_disk"),
    [(None, "0.12.1"), ("0.12.1", "0.12.1"), ("0.12.1", None), ("0.13.0", "0.12.1")],
    ids=["no-gateway", "same-version", "on-disk-unknown", "gateway-newer"],
)
def test_nothing_is_printed_unless_the_gateway_is_older(versions, capsys, running, on_disk) -> None:
    versions(running, on_disk)

    cli_server._report_pending_restart()

    assert capsys.readouterr().out == ""


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_wheel_up_to_date_verdict_carries_the_hint(versions, monkeypatch, capsys) -> None:
    from kiro_crew import __version__ as local_version
    from kiro_crew.platform import wheel_apply

    versions("0.0.1", local_version)
    monkeypatch.setattr(
        wheel_apply,
        "preflight_bases",
        lambda: ("https://cdn.example.com", "https://cdn.example.com"),
    )
    monkeypatch.setattr("kiro_crew.platform.update_layout.release_channel", lambda: "stable")
    feed = {
        "schema": "kirocrew-cli-artifact-manifest-v1",
        "channel": "stable",
        "version": local_version,
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(json.dumps(feed).encode()))
    layout = InstallLayout(
        kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
    )

    cli_server._update_wheel(layout)

    out = capsys.readouterr().out
    assert "Already on the latest version" in out
    assert f"still on 0.0.1; {local_version} is installed" in out
    assert _HINT in out
