"""Test doubles shared by the ``test_instances*.py`` family.

``_patch_port_probe`` and ``_FakeTunnel`` stand in for the loopback port probe
and the tunnel a ``SshTunnelManager`` builds; ``_FakeReq``, ``_State``,
``_ConnectedMgr``, ``_fake_reconfigure``, ``_enable`` and ``_body`` drive the
owner-only instances API handlers without a running dashboard.
"""

from __future__ import annotations

import json
import types
from pathlib import Path


def _patch_port_probe(monkeypatch, *, manager_free: bool = True, allocator_free: bool = True):
    """Make loopback port probing deterministic in BOTH namespaces that probe.

    The connect path probes twice, and each site resolves ``_is_port_free`` from
    its own module, so patching one leaves the other binding real sockets:

    * ``PortAllocator.allocate`` resolves the name in ``port_allocator``. Left
      real, allocation walks upward past whatever the host happens to be
      holding, so which port an instance is handed depends on host state — and
      a test asserting an exact port passes only where the base port is free.
    * ``ssh_tunnel_manager`` holds its own re-bound reference for the advisory
      re-probe it runs on the port it just allocated.

    The two knobs are separate because the answers legitimately differ: the
    conflict case models a TOCTOU loss, where allocation succeeds and the
    re-probe then finds that port taken.
    """
    import kiro_crew.instances.port_allocator as pa
    import kiro_crew.instances.ssh_tunnel_manager as stm

    monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": manager_free)
    monkeypatch.setattr(pa, "_is_port_free", lambda port, host="127.0.0.1": allocator_free)


class _FakeTunnel:
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
        self.stopped = False
        self.start_result = True
        self._S = TunnelState
        # Mirrors _SshTunnel.pid (None when no live child). connect() persists
        # `tunnel.pid or 0` as the forwarder_pid hint; tests that assert a
        # recorded pid set this to a concrete value via a factory wrapper.
        self.pid = None
        # Recorded so transport-selection tests can assert which transport the
        # manager chose for this instance.
        self.transport = transport
        self.ssm_target = ssm_target
        self.aws_profile = aws_profile
        self.aws_region = aws_region
        self.ssh_host = ssh_host
        self.connect_timeout_secs = connect_timeout_secs
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)

    async def start(self):
        self.status.state = self._S.CONNECTED if self.start_result else self._S.ERROR
        if not self.start_result:
            self.status.error = "boom"
        return self.start_result

    async def stop(self):
        self.stopped = True
        self.status.state = self._S.STOPPED


class _FakeReq:
    def __init__(
        self,
        state,
        *,
        headers=None,
        match=None,
        body=None,
        query=None,
        user="owner",
        app_token="",
    ):
        self.app = {"state": state}
        self.headers = headers or {}
        self.match_info = match or {}
        self.query = query or {}
        self._body = body
        # Mirrors the aiohttp Request MAPPING, all three reads the owner predicate
        # in ``_guard`` performs: ``.get("user")`` for the subject, ``"app" in``
        # then ``["app"]`` for the app-token claim. A double that serves only
        # ``.get`` raises on the ``in`` test. ``app_token`` stays "" for a browser
        # session; a test wanting an app token passes it.
        self._attrs = {"app": app_token}
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


def _fake_reconfigure(mgr, keep_intent=True):
    """Mirror SshTunnelManager.reconfigure on a stub: teardown then persist.

    The real method holds the manager lock across both steps; a stub cannot model
    the lock, so it models the ORDER, which is what the handler depends on.
    """

    async def reconfigure(instance_id, apply):
        try:
            await mgr.disconnect(instance_id, keep_intent=keep_intent)
        except Exception:
            pass  # the real method logs and persists anyway
        return apply()

    return reconfigure


class _State:
    """Dashboard-state stand-in for the instances handlers.

    ``owner_id`` matches ``_FakeReq``'s default caller, because ``_guard`` demands
    the positively-identified owner: the whole control plane mints peer dashboard
    credentials with the owner's manager-held credential, so an authenticated
    non-owner must not reach it. A test wanting that caller passes a different
    ``user=``.
    """

    owner_id = "owner"

    def __init__(self, registry, manager=None):
        self.instances_registry = registry
        self.instances_manager = manager


class _ConnectedMgr:
    """Manager stub where the named instances report a live tunnel.

    Only the three members ``_status_for`` touches are implemented, which is
    what the warm-set-cap tests need: the cap is derived from how many instances
    come back ``connected``.
    """

    def __init__(self, connected):
        self._connected = set(connected)

    def status(self, instance_id):
        if instance_id not in self._connected:
            return None
        return types.SimpleNamespace(
            to_dict=lambda: {"instance_id": instance_id, "state": "connected"}
        )

    def token_ttl_remaining(self, instance_id):
        return None

    def token_ttl_total(self, instance_id):
        return None

    def last_error(self, instance_id):
        return None


def _enable(tmp_path: Path, monkeypatch, *, enabled=True, warm_set_cap=None):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    section: dict = {"enabled": enabled}
    if warm_set_cap is not None:
        section["warm_set_cap"] = warm_set_cap
    (tmp_path / "config.json").write_text(json.dumps({"instances": section}))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()


def _body(resp):
    return json.loads(resp.body.decode())
