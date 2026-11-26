"""A crew whose host refuses the SSH sign-in is named as such when its token is re-minted.

The tunnel can stay up while the credential ssh signs in with stops being
accepted (an expired certificate, a removed key): the forwarder keeps its old
session, so the tunnel reads connected, and only the next token mint meets the
refusal. The connect and refresh-token answers must name that refusal, with
ssh's ``Permission denied (publickey)``, rather than send the person to check a
remote gateway that is running.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from closed_peer_helpers import refused_peer_connects  # noqa: F401
from instances_helpers import _body, _enable, _FakeReq, _FakeTunnel, _patch_port_probe, _State

pytestmark = pytest.mark.usefixtures("refused_peer_connects")

#: What OpenSSH prints when the host accepts none of the offered keys.
_PUBLICKEY_REFUSAL = "user@cdd2-host: Permission denied (publickey).\r\n"


class _Proc:
    """A finished one-shot ssh run with a fixed exit status and stderr."""

    def __init__(self, returncode: int, stderr: str):
        self.returncode = returncode
        self._stderr = stderr.encode()

    async def communicate(self):
        return b"", self._stderr


def _real_mint_exiting(monkeypatch, returncode: int, stderr: str):
    """The real SSH mint, with ssh itself answering *returncode* and *stderr*."""
    from kiro_crew.instances import token_mint

    async def spawn(argv, env, **_kw):
        return _Proc(returncode, stderr)

    monkeypatch.setattr(token_mint, "spawn_one_shot_ssh", spawn)
    monkeypatch.setattr(token_mint, "ssh_spawn_argv_env", lambda argv: (argv, {}))
    return token_mint.mint_remote_token


def _manager(tmp_path, monkeypatch, *, returncode: int = 255, stderr: str = _PUBLICKEY_REFUSAL):
    """A connected crew whose stored token fails the probe and whose re-mint fails."""
    from kiro_crew.instances.registry import InstancesRegistry
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

    _patch_port_probe(monkeypatch)
    refused = _real_mint_exiting(monkeypatch, returncode, stderr)
    calls = {"n": 0}

    async def mint(host, **kw):
        # The first mint is the one the tunnel came up with; every later one meets
        # the refusal, as after a certificate expired under a live forwarder.
        calls["n"] += 1
        if calls["n"] == 1:
            return "TOK"
        return await refused(host, **kw)

    reg = InstancesRegistry(path=tmp_path / "instances.json")
    reg.add(name="CDD2", ssh_host="cdd2-host", instance_id="cdd2")
    mgr = SshTunnelManager(reg, base_port=53900, mint_token=mint, tunnel_factory=_FakeTunnel)

    async def never_validates(local_port, token):
        return False

    monkeypatch.setattr(mgr, "token_validates", never_validates)
    return reg, mgr


def _connect_twice(tmp_path, monkeypatch, **kw):
    from kiro_crew.dashboard import handlers_instances as handlers

    _enable(tmp_path, monkeypatch)
    reg, mgr = _manager(tmp_path, monkeypatch, **kw)
    state = _State(reg, mgr)

    async def scenario():
        await mgr.connect("cdd2")
        try:
            return await handlers.api_instances_connect(_FakeReq(state, match={"id": "cdd2"}))
        finally:
            await mgr.shutdown()

    return asyncio.run(scenario())


def test_connect_names_the_refused_ssh_sign_in(tmp_path, monkeypatch):
    r = _connect_twice(tmp_path, monkeypatch)

    body = _body(r)
    assert r.status == 502
    assert "token" not in body
    assert body["code"] == "ssh_auth_refused"
    assert "refused the SSH key or certificate" in body["error"]
    assert "Permission denied (publickey)" in body["error"]
    assert "remote gateway is running" not in body["error"]


@pytest.mark.parametrize(
    ("returncode", "stderr", "shown"),
    [
        # 126 is the remote shell's "cannot execute": ssh signed in, so the key is fine.
        (126, "bash: /opt/kirocrew: Permission denied\n", "bash: /opt/kirocrew: Permission denied"),
        # A verbose ssh_config names the method that signed in; the drop came after.
        (
            255,
            'Authenticated to cdd2-host using "publickey".\nConnection reset by peer\n',
            "Connection reset by peer",
        ),
        # A ProxyCommand's own I/O error, which carries no list of auth methods.
        (
            255,
            "Error: Permission denied (os error 13)\nConnection closed by UNKNOWN port 65535\n",
            "Permission denied (os error 13)",
        ),
        # ssh's refusal wording from the REMOTE command (a nested ssh, say), not ssh's own
        # exit: only exit 255 says ssh itself failed to sign in.
        (1, _PUBLICKEY_REFUSAL, "Permission denied (publickey)"),
    ],
    ids=[
        "remote-cannot-execute",
        "verbose-log-then-reset",
        "proxy-io-error",
        "refusal-not-from-ssh",
    ],
)
def test_a_failure_after_the_sign_in_is_not_read_as_an_ssh_refusal(
    tmp_path, monkeypatch, returncode, stderr, shown
):
    r = _connect_twice(tmp_path, monkeypatch, returncode=returncode, stderr=stderr)

    body = _body(r)
    assert r.status == 502
    assert body["code"] == "instance_token_unconfirmed"
    assert "refused the SSH key" not in body["error"]
    # The mint's own reason still reaches the user instead of being dropped.
    assert shown in body["error"]


def test_the_kept_reason_is_bounded(tmp_path, monkeypatch):
    from kiro_crew.instances.ssh_tunnel_manager_tokens import _REFRESH_ERROR_MAX_CHARS

    r = _connect_twice(tmp_path, monkeypatch, stderr="x" * 50_000 + _PUBLICKEY_REFUSAL)

    error = _body(r)["error"]
    assert r.status == 502
    assert len(error) < _REFRESH_ERROR_MAX_CHARS + 300
    # ssh's own last line survives a long log ahead of it.
    assert "Permission denied (publickey)" in error


def test_refresh_token_names_the_refused_ssh_sign_in(tmp_path, monkeypatch):
    from kiro_crew.dashboard import handlers_instances as handlers

    _enable(tmp_path, monkeypatch)
    reg, mgr = _manager(tmp_path, monkeypatch)
    state = _State(reg, mgr)

    async def scenario():
        await mgr.connect("cdd2")
        try:
            return await handlers.api_instances_refresh_token(_FakeReq(state, match={"id": "cdd2"}))
        finally:
            await mgr.shutdown()

    r = asyncio.run(scenario())

    body = _body(r)
    assert r.status == 502
    assert body.get("code") == "ssh_auth_refused"
    assert "Permission denied (publickey)" in body["error"]


def test_a_request_driven_refresh_is_not_logged_as_proactive(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.instances.ssh_tunnel_manager"):
        _connect_twice(tmp_path, monkeypatch)

    lines = [r.getMessage() for r in caplog.records if "cdd2" in r.getMessage()]
    assert any("Permission denied (publickey)" in line for line in lines), lines
    assert not any(line.startswith("Proactive") for line in lines), lines
