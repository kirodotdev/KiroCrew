"""The fixture and helper shared by ``test_mcp_discovery.py`` and its six topic files.

``_clear_cache`` empties the probe cache, and the autouse ``_passthrough_sandbox`` runs each
probed command unwrapped, so the tests exercise the probe's protocol handling and process
cleanup rather than which sandbox backend the host has. Each test file imports the autouse
fixture by name, which is what registers it there.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_discovery import _probe_cache


def _clear_cache() -> None:
    _probe_cache.clear()


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse sandbox passthrough moved verbatim; no test in the test_mcp_discovery*.py modules calls monkeypatch.undo()
def _passthrough_sandbox(monkeypatch):
    """``probe_server`` routes the spawned MCP binary through
    ``sandboxed_spawn_argv`` → ``wrap_argv``, which raises when no OS-level
    sandbox backend is available (e.g. macOS without sandbox-exec). These tests
    exercise the probe's protocol handling and process cleanup, not sandbox
    availability, so run the command unwrapped in-test (preserving the caller's
    ``env=`` kwarg / falling back to os.environ)."""
    import os as _os

    def _passthrough(argv, *a, env=None, **k):
        return list(argv), dict(env if env is not None else _os.environ), None

    monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _passthrough)


@pytest.fixture(autouse=True)
def _stub_remote_probe_ssrf_vet(_floor_monkeypatch):
    """Answer ``_probe_remote``'s SSRF vet from a fixed PUBLIC address, no DNS.

    ``_probe_remote`` now routes ``server.url`` through
    ``link_unfurl.vet_unfurl_url`` and pins the connection before it POSTs, so a
    configured server at a link-local or cloud metadata address is refused.
    The probe-remote cases in these modules mock ``aiohttp.ClientSession`` and use
    public hostnames, so the vet must approve those WITHOUT a real
    ``getaddrinfo`` (determinism floor D10): the DNS seam
    (``link_unfurl._default_resolve``) is answered from one public address,
    exactly as the link-preview and OAuth-discovery suites stub the same vet. An
    IP LITERAL still reaches the real address checks (the resolver is consulted
    only for a name), so the refusal cases pass link-local and metadata literals
    through the REAL policy. ``pinned_connector`` returns None because the mocked
    session never dials; a test that wants a NAME refused overrides
    ``_default_resolve`` itself, and a live loopback case restores the real
    connector.

    Requests ``_floor_monkeypatch`` (not ``monkeypatch``) so a test calling
    ``monkeypatch.undo()`` mid-body cannot lift this stub (testing-conventions
    D11); it also keeps this autouse fixture off the shared-monkeypatch lint.
    """
    from kiro_crew import link_unfurl as lu

    _floor_monkeypatch.setattr(lu, "_default_resolve", lambda _host, _port: ["93.184.216.34"])
    _floor_monkeypatch.setattr(lu, "pinned_connector", lambda _vetted: None)
