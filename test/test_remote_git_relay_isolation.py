"""The guest relay is reached only through the git config the proxy hands out.

A workspace's own git and network settings (an HTTP proxy, a credential helper)
must neither carry the relay's traffic nor be asked about the relay's URL, and a
credential lookup on the gateway that overruns takes its whole process tree.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys

import pytest
from test_remote_git_proxy import upstream_repo  # noqa: F401 - fixture
from test_remote_git_proxy import (
    _POSIX_GUEST,
    HttpBackendUpstream,
    _client_env,
    _git,
    _in_thread,
    _run_channel,
    _stop,
)

from kiro_crew.remote_session import git_gateway
from kiro_crew.testing.wait import async_wait_until


@_POSIX_GUEST
@pytest.mark.asyncio
async def test_a_workspace_http_proxy_never_carries_the_relay_traffic(
    tmp_path, upstream_repo  # noqa: F811 - fixture
):
    """A workspace that sets an HTTP proxy with no loopback exclusion still clones
    through the relay: the rewritten URL is never sent to that proxy, which could
    not reach the guest's loopback and must not see the relay's bearer secret."""
    root, _bares = upstream_repo
    upstream = HttpBackendUpstream(root)
    process, serve, port_file, _link = await _run_channel(tmp_path, upstream)
    seen: list[bytes] = []
    with socket.socket() as trap:
        # A listening "corporate proxy" that records anything sent to it.
        trap.bind(("127.0.0.1", 0))
        trap.listen()
        trap.settimeout(0.2)
        trap_url = f"http://127.0.0.1:{trap.getsockname()[1]}"
        try:
            env = _client_env(tmp_path, port_file)
            for name in ("NO_PROXY", "no_proxy"):
                env.pop(name, None)
            for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy"):
                env[name] = trap_url
            clone = tmp_path / "clone"
            result = await _in_thread(
                _git,
                "clone",
                "https://github.com/acme/widgets.git",
                str(clone),
                cwd=tmp_path,
                env=env,
                check=False,
            )
            try:
                conn, _addr = trap.accept()
                with conn:
                    conn.settimeout(0.2)
                    seen.append(conn.recv(4096))
            except OSError:
                pass
            assert result.returncode == 0, result.stderr
            assert (clone / "README").read_text() == "hello\n"
            assert not seen, seen
            assert upstream.calls
        finally:
            await _stop(process, serve)


@_POSIX_GUEST
@pytest.mark.asyncio
async def test_a_workspace_credential_helper_is_never_asked_about_the_relay(
    tmp_path, upstream_repo  # noqa: F811 - fixture
):
    """A routed request that ends in a 401 does not reach the workspace's own
    general credential helper (Git Credential Manager would open a prompt): the
    relay's URL gets an empty helper list, then the quit helper."""
    root, _bares = upstream_repo
    process, serve, port_file, _link = await _run_channel(
        tmp_path, HttpBackendUpstream(root, status=401)
    )
    asked = tmp_path / "helper-ran"
    helper = tmp_path / "helper.sh"
    helper.write_text(f'#!/bin/sh\necho "$1" >> {asked}\n')
    helper.chmod(0o755)
    try:
        env = _client_env(tmp_path, port_file)
        # The workspace's own, general helper, in its global config.
        (tmp_path / "client-home" / ".gitconfig").write_text(f"[credential]\n\thelper = {helper}\n")
        result = await _in_thread(
            _git,
            "clone",
            "https://gitlab.com/group/sub/proj.git",
            str(tmp_path / "c"),
            cwd=tmp_path,
            env=env,
            check=False,
        )
        assert result.returncode != 0
        assert not asked.exists(), asked.read_text()
    finally:
        await _stop(process, serve)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_an_overrun_lookup_reaps_a_descendant_holding_its_stdout(tmp_path, monkeypatch):
    """``git credential fill`` runs its helpers as children: when the lookup
    overruns, a helper that outlives git and keeps its stdout must not hold the
    lookup past its deadline, and must not survive it."""
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(git_gateway, "_GH_TIMEOUT_SECS", 1.0)
    # The grandchild sleeps 60 s holding the pipe: a reap that waited on it
    # would still be pending when this bound expires.
    with pytest.raises(git_gateway._NoCredential, match="timed out"):
        await asyncio.wait_for(
            git_gateway._run_quiet([sys.executable, "-c", script], env=dict(os.environ)), 30
        )
    grandchild = int(pid_file.read_text())

    def gone() -> bool:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return True
        return False

    await async_wait_until(gone, timeout=10, describe=lambda: "the lookup's grandchild exits")
