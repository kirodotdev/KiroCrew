"""Pin each managed server's advertised caller-identity to what discovery reads.

The shareability assessment has to answer "can one backend serve two sessions"
for Kiro Crew's own MCP servers WITHOUT spawning them: on a host where the probe
cannot run there is no ``initialize`` response to read the capability from. So
:func:`managed_server_is_session_bound` reads a module constant instead.

That makes the constant a SECOND copy of a fact whose original is the argument
passed to ``run_mcp_stdio_loop`` — and the two disagreeing is silent in both
directions. A constant that says True while the server never advertises would put
an unshareable server in the shareable set; the reverse would keep disqualifying a
server built for pooling, which is the bug this pairing was introduced to fix. So
the ratchet drives the real serve entry point and reads back what it was handed.
"""

from __future__ import annotations

import importlib

import pytest

from kiro_crew import mcp_discovery
from kiro_crew.mcp_discovery import (
    _MANAGED_SERVER_TOOL_MODULES,
    managed_server_is_session_bound,
)

#: Managed server name -> the function that starts its stdio loop. Separate from
#: the discovery map because that map points at the module whose ``_list_tools``
#: is read; this names the entry point whose argument is the fact under test.
#: ``kirocrew-core`` is the odd one out — the other three all call theirs
#: ``run_mcp_server``.
_SERVE_ENTRY = {
    "kirocrew-core": "run_mcp_core_server",
    "kirocrew-cron": "run_mcp_server",
    "kirocrew-computer": "run_mcp_server",
    "kirocrew-dashboard": "run_mcp_server",
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
def test_session_bound_is_the_inverse_of_advertising(name: str, monkeypatch) -> None:
    module_name = _MANAGED_SERVER_TOOL_MODULES[name]
    advertised = _advertised_by_serve_entry(module_name, _SERVE_ENTRY[name], monkeypatch)
    assert managed_server_is_session_bound(name) is (not advertised)


def test_the_concrete_verdicts_are_spelled_out() -> None:
    """The concrete answers the dashboard renders, spelled out.

    Kept alongside the derived checks above because those pass just as happily if
    every managed server flipped at once. ``kirocrew-core`` and ``kirocrew-cron``
    both consume the injected caller block, so neither is session-bound.
    ``kirocrew-computer`` and ``kirocrew-dashboard`` do not advertise yet -- they
    are pooled all the same (nothing declines to pool an unadvertised backend), so
    what this verdict records is our own unfinished adoption, not a property of
    those servers. It flips when they adopt it, and the reason code can be deleted
    once nothing produces it.
    """
    assert managed_server_is_session_bound("kirocrew-core") is False
    assert managed_server_is_session_bound("kirocrew-cron") is False
    assert managed_server_is_session_bound("kirocrew-computer") is True
    assert managed_server_is_session_bound("kirocrew-dashboard") is True


def test_a_third_party_server_is_not_claimed_either_way() -> None:
    """Absence from the managed map is not a claim; the pre-flight measures it."""
    assert managed_server_is_session_bound("slack-mcp") is False


def test_a_managed_server_missing_from_the_aware_set_reads_as_session_bound() -> None:
    """The conservative default, which is now a SET-membership question.

    With the classification read from a name set rather than each module's
    constant, the way a managed server gets misjudged is the set forgetting it: a
    fifth server added to ``_MANAGED_SERVER_SUBCOMMANDS`` but not to
    ``_MANAGED_SERVERS_CALLER_AWARE``. That direction must read as session-bound,
    never as shareable, so an omission costs a conservative verdict rather than
    co-tenancy. (The opposite direction -- the set claiming a server that does not
    advertise -- is what the two ratchets above catch.)
    """
    assert mcp_discovery._MANAGED_SERVERS_CALLER_AWARE <= set(_MANAGED_SERVER_TOOL_MODULES)
    for name in _MANAGED_SERVER_TOOL_MODULES:
        if name not in mcp_discovery._MANAGED_SERVERS_CALLER_AWARE:
            assert managed_server_is_session_bound(name) is True, name


def test_the_classification_reads_no_module_at_import_time(monkeypatch) -> None:
    """It must not import a managed server's module to answer.

    This is consulted once per row on every MCP-servers request. Importing there
    executes package code the gateway does not otherwise run, and the package
    directory is writable by the same uid the agent runs as -- so an editable
    checkout would make every render an execution point. The sibling in-process
    tool read accepts that only on the fallback path where a sandboxed spawn was
    impossible anyway.
    """
    calls: list[str] = []

    def _tracking_import(name: str, *args: object, **kwargs: object) -> object:
        calls.append(name)
        raise AssertionError(f"classification imported {name}")

    monkeypatch.setattr(mcp_discovery.importlib, "import_module", _tracking_import)
    for name in list(_MANAGED_SERVER_TOOL_MODULES) + ["slack-mcp"]:
        managed_server_is_session_bound(name)
    assert calls == []
