"""The manager builder, fake forwarder and mint doubles the Remote Crew chaining tests share.

Each file of the family imports what it uses from here by name. ``_mgr`` builds an
``SshTunnelManager`` with ``parent_port`` 4242 over the registry file at
``tmp_path / "instances.json"``, so a second build on the same ``tmp_path`` shares it, which is
how the restart cases restart. It forwards through ``_FakeTunnel``, mints with a fake ssh mint
unless a case passes its own, makes both loopback port probes answer free (``_free_ports``),
takes its base port from ``_fake_base_port``'s per-worker band, or from the kernel when a case
asks for the real hop guard, puts an ``_InertGuard`` in place of the hop guard otherwise, and
answers the far-end identity probe as a crew that reports no id unless a case asks for the real
probe. ``_relay_mint`` is a chained mint that reports the hop, lifetime and hop identity a
parent in agreement with the row answers. ``_FakeMintSession`` records the URL each real
chained mint posts to and answers through ``_FakeMintResp`` with its ``reply`` under
``reply_status``, both read off ``type(self)`` so a subclass can stage a refusal;
``_FakeMintReader`` is the canned body reader under it, which the health-reply cases also use.
``_no_cookie_header`` is a peer cookie header carrying no credential; ``_State``, ``_FakeReq``,
``_enable`` and ``_resp_body`` drive the dashboard's instance handlers; and ``VIA_ID`` is the
parent-side id for fixtures that do not care what it is. ``_FakeTunnel.made`` and
``_FakeMintSession``'s ``posted``, ``reply`` and ``reply_status`` are class attributes, so
every file that reads them shares one record, and ``_mgr`` resets ``made`` on each build. The
family is ``test_remote_crew_chaining.py``, ``test_remote_crew_chaining_subtree_removal.py``,
``test_remote_crew_chaining_route_admission.py``,
``test_remote_crew_chaining_credential_destination.py``,
``test_remote_crew_chaining_lent_hop_lease.py`` and
``test_remote_crew_chaining_chained_refresh_and_repair.py``.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from kiro_crew.instances.registry import InstancesRegistry

# Stands in for "this crew's id on its PARENT" in fixtures that do not care what
# it is. The one test that does care asserts on its own genuinely divergent pair,
# because ids that coincide on both sides hide a mint aimed at the wrong one.
VIA_ID = "peer-id"


class _FakeTunnel:
    """Minimal stand-in for ``_SshTunnel``, recording what it was asked to forward."""

    made: list[_FakeTunnel] = []

    def __init__(
        self,
        iid,
        ssh_host,
        lp,
        rp,
        *,
        connect_timeout_secs=0,
        compression=True,
        probe_failure_threshold=0,
        on_exit=None,
        transport="ssh",
        ssm_target="",
        aws_profile="",
        aws_region="",
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self.iid = iid
        self.ssh_host = ssh_host
        self.local_port = lp
        self.remote_port = rp
        self.transport = transport
        self.pid = None
        self.stopped = False
        self.start_result = True
        self._S = TunnelState
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)
        _FakeTunnel.made.append(self)

    async def start(self):
        self.status.state = self._S.CONNECTED if self.start_result else self._S.ERROR
        if not self.start_result:
            self.status.error = "boom"
        return self.start_result

    async def stop(self):
        self.stopped = True
        self.status.state = self._S.STOPPED


def _free_ports(monkeypatch):
    """Make both loopback port probes answer "free", so these stay hermetic."""
    from kiro_crew.instances import port_allocator
    from kiro_crew.instances import ssh_tunnel_manager as stm

    monkeypatch.setattr(port_allocator, "_is_port_free", lambda *_a, **_k: True)
    monkeypatch.setattr(stm, "_is_port_free", lambda *_a, **_k: True)


async def _no_cookie_header(_pid, _url, _name):
    """Stand-in for the peer cookie header: these cases carry no credential.

    Async and three-argument because the real method is: it re-reads the credential on
    every attempt rather than being hoisted out of a retry loop, and it needs the URL to
    scope the cookie to the port it is sent to.
    """
    return {}


def _fake_base_port() -> int:
    """Worker-local fake band for cases that do not bind real hop sockets.

    Keep the band below the platform ephemeral ranges. Real ownership cases use
    a kernel-selected port hint in ``_mgr(real_hop_guard=True)``.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    index = int(worker[2:]) if worker.startswith("gw") and worker[2:].isdigit() else 0
    return 23700 + index * 40


def _mgr(tmp_path, monkeypatch, *, mint=None, real_hop_guard=False, real_identity=False):
    """A manager over a fresh registry, with a fake forwarder and a fake mint.

    The far-end identity probe answers as a crew whose build reports no id
    unless *real_identity* is set: the fake forwarders bind nothing, so a real
    probe would read "no answer", which a chained connect refuses.
    """
    _free_ports(monkeypatch)
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

    _FakeTunnel.made = []
    reg = InstancesRegistry(path=tmp_path / "instances.json")

    async def ok_mint(
        host,
        *,
        remote_bin="",
        ttl="20h",
        remote_port=None,
        embed_parent_port=None,
        timeout_secs=None,
    ):
        return f"SSH_TOKEN_FOR_{host}_epp{embed_parent_port}"

    base_port = _fake_base_port()
    if real_hop_guard:
        # Real ownership cases use a kernel-selected hint, never a fixed worker band.
        # The guard must still acquire it through the production transition under test.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            base_port = probe.getsockname()[1]

    mgr = SshTunnelManager(
        reg,
        base_port=base_port,
        mint_token=mint or ok_mint,
        tunnel_factory=_FakeTunnel,
        parent_port=4242,
    )
    # Fake-port cases bind nothing; real ownership cases keep the manager's real guard.
    if not real_hop_guard:
        mgr._hop_guard = _InertGuard()
    if not real_identity:

        async def _reports_no_id(_port):
            return ""

        monkeypatch.setattr(mgr, "_peer_identity", _reports_no_id)
    return reg, mgr


class _InertGuard:
    """Records what a real guard would have been asked to own, and binds nothing."""

    def __init__(self) -> None:
        self.synced: list[tuple[dict[int, float], set[int]]] = []

    def sync(self, leases, in_use):
        self.synced.append((dict(leases), set(in_use)))
        return set()

    def hold(self, port, until):
        return True

    def release(self, port):
        return None

    def held_ports(self):
        return set()

    def close_all(self):
        return None


class _State:
    owner_id = "owner"

    def __init__(self, registry, manager=None):
        self.instances_registry = registry
        self.instances_manager = manager


class _FakeReq:
    def __init__(self, state, *, match=None, body=None, query=None, user="owner"):
        self.app = {"state": state}
        self.headers = {}
        self.match_info = match or {}
        self.query = query or {}
        self._body = body
        self._attrs = {"app": ""}
        if user is not None:
            self._attrs["user"] = user

    def get(self, key, default=None):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _enable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"instances": {"enabled": True}}))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()


def _resp_body(resp):
    return json.loads(resp.body.decode())


def _relay_mint(
    mgr, *, token="CHAINED_TOKEN", port=None, seen=None, ttl=None, hop_id=None, hop_gen=1
):
    """A stand-in for the chained mint that also reports the hop port.

    The real method reads `port` and `ttl` out of the parent's authenticated reply
    and returns all three together, publishing nothing: the hop it names is the
    very value the credential check compares against, so only the caller's fenced
    store may write it. A double that returns a bare token therefore describes a
    DIFFERENT contract, not a simpler one.

    Defaults to the row's port, which is what a parent in agreement with the row
    would answer; pass *port* to make it disagree.
    """

    async def fake(inst, _params):
        if seen is not None:
            seen.append(inst.id)
        from kiro_crew.instances.ssh_tunnel_manager import _Mint

        return _Mint(
            token=token,
            hop=inst.via_remote_port if port is None else port,
            ttl=inst.ttl if ttl is None else ttl,
            hop_id=inst.via_remote_id if hop_id is None else hop_id,
            hop_gen=hop_gen,
        )

    return fake


class _FakeMintReader:
    def __init__(self, raw: bytes):
        self._raw = raw

    async def read(self, _n: int) -> bytes:
        return self._raw


class _FakeMintResp:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.content = _FakeMintReader(json.dumps(payload).encode("utf-8"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeMintSession:
    """Records the URL each chained mint is aimed at and answers with a canned reply.

    The URL is what the assertion is really about: the path carries the crew's id
    as the PARENT knows it, and a fake that forgets the URL cannot tell the right
    id from the wrong one.
    """

    posted: list[str] = []
    reply: dict = {"token": "CHILD_TOKEN", "port": 4242, "ttl": "2h", "hop_id": "c-2", "hop_gen": 3}
    reply_status: int = 200

    def __init__(self, *_a, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def post(self, url, **_kw):
        # Read off `type(self)`, so a subclass can stage a refusal. Naming the base
        # class here meant every subclass still answered the base's 200 reply, which
        # is why no test could reach the non-2xx branches at all.
        cls = type(self)
        _FakeMintSession.posted.append(url)
        return _FakeMintResp(cls.reply_status, cls.reply)
