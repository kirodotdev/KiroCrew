"""The port `kirocrew doctor` prints, in the order docs/guides/remote-crew-on-ec2.md states.

The guide's "Port/tunnel mismatch" section says the doctor's ``dashboard:`` line
takes the shell's ``KIROCREW_PORT`` first, then a port written in ``dashboard.url``,
then 5476. This drives the doctor's own Configuration section so the guide and the
code cannot drift apart.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.doctor_checks import services


def _printed_dashboard_line(monkeypatch, capsys, url: str) -> str:
    cfg = SimpleNamespace(
        agent=SimpleNamespace(provider="kiro", model="auto", approval_mode="auto"),
        dashboard=SimpleNamespace(url=url),
        load_credentials=lambda: {},
    )
    services._doctor_configuration(cfg, [])
    lines = [line for line in capsys.readouterr().out.splitlines() if "dashboard:" in line]
    assert lines, "doctor printed no dashboard line"
    return lines[0]


@pytest.mark.parametrize(
    "env_port,url,expected",
    [
        (None, "", ":5476"),
        (None, "http://localhost:8080", ":8080"),
        ("7777", "", ":7777"),
        ("7777", "http://localhost:8080", ":7777"),
    ],
)
def test_doctor_dashboard_port_follows_shell_env_then_url_then_default(
    monkeypatch, capsys, env_port, url, expected
):
    if env_port is None:
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
    else:
        monkeypatch.setenv("KIROCREW_PORT", env_port)

    assert _printed_dashboard_line(monkeypatch, capsys, url).endswith(expected)
