"""Per-test pins of host state the app registry's git steps read.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

from conftest_guards import per_test_guard


@per_test_guard
def _no_host_ssh_agent_in_registry_git_env(_floor_monkeypatch):
    """Keep a developer's ssh-agent sockets out of every registry git environment.

    A registry git network step that keeps the ssh agent repairs a stale
    ``SSH_AUTH_SOCK`` from the agent sockets it finds on the host
    (``_git_network_step_env``), and stats a socket it finds before it uses it. On a
    developer host with a live agent the environment a test's fake spawn receives
    would then name that socket, and on CI it would not. So the scan finds nothing,
    and a socket a test's own scan names is accepted without a look at the host. A
    test of the repair sets ``resolve_ssh_auth_sock`` itself, and a test of the
    judging sets ``agent_socket_refusal``. A module without a name is left alone:
    ``prove.py`` reverts the production hunks, and this setup must not then error
    every test.
    """
    from kiro_crew.apps.registry_pipeline import subprocess_env

    pins = {"resolve_ssh_auth_sock": lambda env: None, "agent_socket_refusal": lambda path: None}
    for name, pin in pins.items():
        if hasattr(subprocess_env, name):
            _floor_monkeypatch.setattr(subprocess_env, name, pin)
