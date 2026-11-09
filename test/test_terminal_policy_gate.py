"""The security policy's ``boot.allow_terminal`` gates the dashboard terminal.

A stated value that is not a JSON ``true`` refuses the terminal with the same
answer ``dashboard.terminal.enabled: false`` gives. A policy that leaves the key
out has no opinion, and the config switch alone decides.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _make_request,
)

from kiro_crew.dashboard.handlers import terminal, terminal_session
from kiro_crew.platform import context as platform_context
from kiro_crew.platform.governance import parse_policy


def _install(monkeypatch, boot: dict | None) -> None:
    """Make ``current_context().governance`` the policy with *boot*, or no policy."""
    governance = None if boot is None else parse_policy({"version": 1, "boot": boot})
    monkeypatch.setattr(
        platform_context, "current_context", lambda: SimpleNamespace(governance=governance)
    )


@pytest.mark.parametrize(
    ("boot", "enabled"),
    [
        (None, True),  # no policy at all
        ({"fail_closed": True}, True),  # key left out: no opinion
        ({"allow_terminal": True}, True),
        ({"allow_terminal": False}, False),
        ({"allow_terminal": "false"}, False),
        ({"allow_terminal": "true"}, False),
        ({"allow_terminal": None}, False),
    ],
)
def test_policy_decides_only_where_it_states_the_key(monkeypatch, boot, enabled):
    _install(monkeypatch, boot)
    assert terminal_session._terminal_enabled({"enabled": True}) is enabled


def test_boot_controls_built_without_the_key_have_no_opinion():
    from kiro_crew.platform.governance import BootControls

    assert BootControls().allow_terminal is None


def test_policy_allow_does_not_override_the_config_switch(monkeypatch):
    _install(monkeypatch, {"allow_terminal": True})
    assert terminal_session._terminal_enabled({"enabled": False}) is False


def test_a_failed_policy_read_refuses_the_terminal(monkeypatch):
    def _boom():
        raise RuntimeError("adapter down")

    monkeypatch.setattr(platform_context, "current_context", _boom)
    assert terminal_session._terminal_enabled({"enabled": True}) is False


@pytest.mark.asyncio
async def test_create_route_refused_when_policy_says_no_terminal(monkeypatch):
    _install(monkeypatch, {"fail_closed": True, "allow_terminal": False})
    monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", False)
    terminal._enabled_cache[1] = float("-inf")  # read the gate now, not the cached answer
    with (
        patch.object(terminal, "_get_config", return_value={"enabled": True}),
        patch.object(terminal, "_sel") as mock_sel,
    ):
        mock_sel.return_value.log_api_access = MagicMock()
        resp = await terminal.api_terminal_create(_make_request())
    assert resp.status == 403
    assert resp.text == "Terminal panel disabled"


def test_a_translated_enterprise_policy_leaves_the_terminal_alone(monkeypatch):
    from kiro_crew.platform.enterprise_policy_translate import neutral_document

    document = neutral_document()
    assert "allow_terminal" not in document["boot"]
    _install(monkeypatch, document["boot"])
    assert terminal_session._terminal_enabled({"enabled": True}) is True


def test_a_cached_allow_does_not_outlive_a_policy_refusal(monkeypatch):
    # _clear_enabled_cache leaves a fresh cached True; the policy still binds at once.
    _install(monkeypatch, {"allow_terminal": False})
    assert terminal._enabled_cache[0] is True
    assert terminal_session._is_enabled(_make_request()) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("boot", "ended"), [({"allow_terminal": False}, True), ({}, False)])
async def test_a_refreshed_policy_refusal_ends_open_shells(monkeypatch, boot, ended):
    import asyncio

    from kiro_crew.dashboard.handlers import terminal_lifecycle

    _install(monkeypatch, boot)
    calls: list = []

    async def _apply(registry, cfg):
        calls.append((registry, cfg))

    monkeypatch.setattr(terminal, "apply_terminal_enabled", _apply)
    registry: dict = {}
    app = {"state": SimpleNamespace(_terminal_sessions=registry)}
    loop = asyncio.get_running_loop()
    # Hooks run on the refresher thread, never on the loop.
    await asyncio.to_thread(terminal_lifecycle.end_shells_if_policy_refuses, app, loop)
    assert calls == ([(registry, {})] if ended else [])


def test_the_gateway_registers_the_shell_teardown_hook():
    """The wiring, not just the function: registered before the poller starts."""
    from pathlib import Path

    from kiro_crew.slack.gateway_runtime import service_boot

    src = Path(service_boot.__file__).read_text(encoding="utf-8")
    hook = "register_post_install_hook(lambda: end_shells_if_policy_refuses(_app, _loop))"
    assert hook in src
    assert src.index(hook) < src.index("await asyncio.to_thread(start_refresher)")


def test_a_cache_an_older_translation_wrote_is_not_reused_on_upgrade():
    """The ``t2`` build's boot block still said ``allow_terminal: false``; its cached
    version must not be sent, or ``UNCHANGED`` would keep that refusal in force."""
    from kiro_crew.platform import enterprise_policy_fetch as fetch

    assert fetch._version_for("scope:t2/v1", "scope") == ""
    assert fetch.cached_version("scope:t2/v1") == ""
    current = f"scope:{fetch.TRANSLATION_REVISION}/v1"
    assert fetch._version_for(current, "scope") == "v1"
