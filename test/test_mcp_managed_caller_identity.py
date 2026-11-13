"""Pin each managed server's advertised caller-identity to what discovery reads.

:func:`managed_server_is_session_bound` answers without spawning the server, so it reads
a constant that copies the argument passed to ``run_mcp_stdio_loop``. A mismatch is
silent either way, so the ratchet drives the real serve entry point and reads it back.
"""

from __future__ import annotations

import importlib

import pytest

from kiro_crew import mcp_discovery
from kiro_crew.mcp_caller import POOLING_REQUIRES_TENANT_NONCE
from kiro_crew.mcp_discovery import (
    _MANAGED_SERVER_TOOL_MODULES,
    managed_server_is_session_bound,
)
from kiro_crew.mcp_gateway.gatewayd import REGISTERED_CAPABILITIES
from kiro_crew.mcp_gateway.stub import must_degrade_nonce_blind

#: Managed server name -> the function that starts its stdio loop. Separate from
#: the discovery map because that map points at the module whose ``_list_tools``
#: is read; this names the entry point whose argument is the fact under test.
#: ``kirocrew-core`` is the odd one out — the others all call theirs
#: ``run_mcp_server``.
_SERVE_ENTRY = {
    "kirocrew-core": "run_mcp_core_server",
    "kirocrew-cron": "run_mcp_server",
    "kirocrew-computer": "run_mcp_server",
    "kirocrew-dashboard": "run_mcp_server",
    "kirocrew-work": "run_mcp_server",
    "kirocrew-panel": "run_mcp_server",
    "kirocrew-guide": "run_mcp_server",
    "kirocrew-mate": "run_mcp_server",
}


def _advertised_by_serve_entry(module_name: str, entry_name: str, monkeypatch) -> bool:
    """What *module*'s serve entry actually hands the shim, without serving."""
    module = importlib.import_module(module_name)
    # No ``pytest.skip`` on a missing entry: a renamed entry point is exactly the
    # drift this file exists to catch, and skipping would report it as a pass.
    entry = getattr(module, entry_name)
    captured: dict[str, object] = {}

    def _recorder(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    # Patched on the SERVER's module, not on ``mcp_shared``: every one of these
    # imports the loop by name at import time, so replacing the shared original
    # would not be seen by the already-bound reference.
    monkeypatch.setattr(module, "run_mcp_stdio_loop", _recorder)
    entry()
    return bool(captured.get("advertise_caller_identity", False))


@pytest.mark.parametrize("name", sorted(_SERVE_ENTRY))
def test_the_constant_matches_what_the_server_advertises(name: str, monkeypatch) -> None:
    module_name = _MANAGED_SERVER_TOOL_MODULES[name]
    advertised = _advertised_by_serve_entry(module_name, _SERVE_ENTRY[name], monkeypatch)
    declared = bool(
        getattr(importlib.import_module(module_name), "ADVERTISE_CALLER_IDENTITY", False)
    )
    assert declared == advertised, (
        f"{module_name}.ADVERTISE_CALLER_IDENTITY is {declared} but its serve entry "
        f"advertises {advertised}. Discovery reads the constant, so the two drifting "
        "either hides a session-bound server in the shareable set or keeps "
        "disqualifying one that was built for pooling."
    )


@pytest.mark.parametrize("name", sorted(_SERVE_ENTRY))
def test_session_bound_follows_advertising_modulo_the_withheld_set(name: str, monkeypatch) -> None:
    """Session-bound is the inverse of advertising, EXCEPT the documented set.

    ``_MANAGED_SERVERS_ADVERTISING_BUT_WITHHELD`` servers advertise but stay session-bound,
    because their UNNAMED co-tenants are not provably separated on every gateway.
    """
    module_name = _MANAGED_SERVER_TOOL_MODULES[name]
    advertised = _advertised_by_serve_entry(module_name, _SERVE_ENTRY[name], monkeypatch)
    withheld = name in mcp_discovery._MANAGED_SERVERS_ADVERTISING_BUT_WITHHELD
    if withheld:
        assert managed_server_is_session_bound(name) is True
    else:
        assert managed_server_is_session_bound(name) is (not advertised)


def test_the_withheld_set_is_a_real_exception_not_a_stale_one(monkeypatch) -> None:
    """Each withheld name must be managed, actually advertise, and not also
    be claimed caller-aware — otherwise the exception is stale (the server
    stopped advertising, or the hazard behind it was fixed and the name was
    promoted without deleting it here) or self-contradictory.
    """
    withheld = mcp_discovery._MANAGED_SERVERS_ADVERTISING_BUT_WITHHELD
    assert withheld <= set(_MANAGED_SERVER_TOOL_MODULES)
    assert not (withheld & mcp_discovery._MANAGED_SERVERS_CALLER_AWARE)
    for name in withheld:
        module_name = _MANAGED_SERVER_TOOL_MODULES[name]
        advertised = _advertised_by_serve_entry(module_name, _SERVE_ENTRY[name], monkeypatch)
        assert advertised, (
            f"{name} is in the advertising-but-withheld set but its serve entry "
            "does not advertise — the exception no longer describes the server."
        )


def test_a_negotiating_server_keeps_what_earns_its_place() -> None:
    """A caller-aware entry resting on the nonce must keep all three of its parts.

    ``kirocrew-computer`` is shareable only because the per-connection nonce separates
    unnamed callers and a daemon minting none is refused. The checks above read only the
    advertising bit, so they cannot see a missing part.
    """
    assert "kirocrew-computer" in mcp_discovery._MANAGED_SERVERS_CALLER_AWARE
    # 1. the backend separates the callers the gateway cannot name by the nonce
    assert "kirocrew-computer" in POOLING_REQUIRES_TENANT_NONCE
    # 2. a current daemon attests that it mints one
    assert "tenant_nonce" in REGISTERED_CAPABILITIES
    # 3. a stub asked to POOL it behind a daemon that does not attest is degraded
    assert must_degrade_nonce_blind("kirocrew-computer", poolable=True, capabilities=[]) is True
    assert (
        must_degrade_nonce_blind(
            "kirocrew-computer",
            poolable=True,
            capabilities=list(REGISTERED_CAPABILITIES),
        )
        is False
    )


def test_the_concrete_verdicts_are_spelled_out() -> None:
    """The concrete answers the dashboard renders, spelled out.

    The derived checks above would pass if every server flipped at once. These servers
    refuse or namespace an unidentified caller; ``kirocrew-computer`` instead relies on
    the negotiated ``tenant_nonce`` (see ``test_a_negotiating_server_keeps_what_earns_its_place``).
    """
    assert managed_server_is_session_bound("kirocrew-core") is False
    assert managed_server_is_session_bound("kirocrew-cron") is False
    assert managed_server_is_session_bound("kirocrew-computer") is False
    assert managed_server_is_session_bound("kirocrew-dashboard") is False
    assert managed_server_is_session_bound("kirocrew-work") is False
    # ``kirocrew-debug`` gates its host-wide reads on the caller being the OWNER, which needs
    # a session the gateway named; session-bound would resolve an empty identity and
    # refuse every wide read.
    assert managed_server_is_session_bound("kirocrew-debug") is False


def test_a_third_party_server_is_not_claimed_either_way() -> None:
    """Absence from the managed map is not a claim; the pre-flight measures it."""
    assert managed_server_is_session_bound("slack-mcp") is False


def test_a_managed_server_missing_from_the_aware_set_reads_as_session_bound() -> None:
    """A managed server left out of ``_MANAGED_SERVERS_CALLER_AWARE`` reads as session-bound.

    An omission then costs a conservative verdict, not co-tenancy.
    """
    assert mcp_discovery._MANAGED_SERVERS_CALLER_AWARE <= set(_MANAGED_SERVER_TOOL_MODULES)
    for name in _MANAGED_SERVER_TOOL_MODULES:
        if name not in mcp_discovery._MANAGED_SERVERS_CALLER_AWARE:
            assert managed_server_is_session_bound(name) is True, name


def test_the_classification_reads_no_module_at_import_time(monkeypatch) -> None:
    """It must not import a managed server's module to answer.

    It runs per row on every MCP-servers request, and the package directory is writable by
    the agent's uid, so an import would make every render an execution point.
    """
    calls: list[str] = []

    def _tracking_import(name: str, *args: object, **kwargs: object) -> object:
        calls.append(name)
        raise AssertionError(f"classification imported {name}")

    monkeypatch.setattr(mcp_discovery.importlib, "import_module", _tracking_import)
    for name in list(_MANAGED_SERVER_TOOL_MODULES) + ["slack-mcp"]:
        managed_server_is_session_bound(name)
    assert calls == []
