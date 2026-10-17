"""The lock-refusal diagnostics ``cli_gateway_boot`` gives the gateway branch of ``main``.

A second gateway refused by the home's lock names the port and address it was about to
use, so the message can say whether the holder answers there. ``_diagnostic_port`` and
``_diagnostic_bind_address`` resolve them the way the gateway itself does, and answer
``None`` -- never an exception -- when there is no single answer or the resolver cannot be
reached, because they only decorate a refusal.
"""

from __future__ import annotations

import pytest

from kiro_crew import cli, cli_gateway_boot
from kiro_crew.dashboard import urls


class _Config:
    """A loaded config whose only read is ``dashboard.url``."""

    def __init__(self, url: str) -> None:
        self.dashboard = type("Dashboard", (), {"url": url})()


@pytest.mark.parametrize(
    ("gw_kwargs", "expected"),
    [
        ({"no_dashboard": True, "port_override": "7000"}, None),
        ({"no_dashboard": False, "port_override": "auto"}, None),
        ({"no_dashboard": False, "port_override": "AUTO"}, None),
        ({"no_dashboard": False, "port_override": "7123"}, 7123),
    ],
    ids=["slack-only-binds-nothing", "auto", "auto-any-case", "explicit-port"],
)
def test_the_port_comes_from_the_arguments_when_they_decide_it(
    gw_kwargs: dict, expected: int | None
) -> None:
    assert cli._diagnostic_port(gw_kwargs) == expected


def test_without_an_override_the_port_is_the_configured_dashboard_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read from the config the facade's ``KiroCrewConfig`` loads, through the gateway's
    own URL parser: a patch of ``cli.KiroCrewConfig`` reaches the owner that reads it."""
    config = _Config("http://127.0.0.1:7345")
    monkeypatch.setattr(
        cli, "KiroCrewConfig", type("Cfg", (), {"load": staticmethod(lambda: config)})
    )
    assert urls.parse_dashboard_url(config.dashboard.url)[1] == 7345
    assert cli._diagnostic_port({"no_dashboard": False, "port_override": None}) == 7345


def test_a_config_that_cannot_be_read_names_no_port(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable() -> object:
        raise ValueError("corrupt config.json")

    monkeypatch.setattr(cli, "KiroCrewConfig", type("Cfg", (), {"load": staticmethod(unreadable)}))
    assert cli._diagnostic_port({"no_dashboard": False, "port_override": None}) is None


def test_the_bind_address_is_none_when_its_resolver_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(local_only: bool) -> str:
        raise OSError("no interfaces")

    monkeypatch.setattr(urls, "bind_address_for", broken)
    assert cli._diagnostic_bind_address() is None


def test_the_facade_and_the_owner_hold_one_object_for_each_helper() -> None:
    for name in ("_diagnostic_port", "_diagnostic_bind_address", "_resolve_gateway_args"):
        assert getattr(cli, name) is getattr(cli_gateway_boot, name)
