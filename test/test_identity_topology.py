"""Identity-resolution topology tests (pre-work for the pid-namespace re-raise).

Background: a reverted PID-namespace sandbox change broke
subagent identity resolution in live deployments while the full unit gate
stayed green. Session hosts ran inside a PID namespace where
``os.getpid()``/``os.getppid()`` return namespace-local pids renumbered from 1,
but the on-disk ``session_pid_<pid>.txt`` files are written by the gateway and
keyed by HOST pids — so every client-side /proc ancestry walk resolved to an
empty session key. Subagents registered with no parent session: invisible in
the dashboard, completion events unroutable.

Why the gate stayed green: the ancestry walk is implemented in FOUR
independent copies (``mcp_caller.CallerContext.from_env``,
``mcp_core._resolve_session_key``, the inline walk in
``mcp_shared._resolve_tool_policy``, ``mcp_gateway/stub.py``), each tested
with hand-rolled per-file mocks that encode their author's topology
assumptions. Mocks cannot detect that the assumption itself changed.

This file provides the three unit-level defenses:

1. **ProcessTopology** — a single shared model of the real process tree
   (gateway -> session host -> kiro-cli -> MCP server), the session_pid file
   contract, and a pluggable pid *view* (``host`` vs ``pidns``). Identity
   tests consume this instead of hand-rolled ``_ppid_fn`` maps, so a future
   change to process topology is modeled once and re-checked everywhere.
2. **pid-view parametrized tests** for each of the four resolution paths.
   The ``host`` view passes today. The ``pidns`` view is ``xfail(strict)`` —
   an executable archive of the known breakage. A pid-namespace re-raise CR
   must flip these to pass (and the strict marker forces the author to
   consciously remove it).
3. **Call-site registry guard** — scans ``src/`` for ``session_pid_``
   references and fails when an unregistered file appears, so a fifth copy
   of the walk cannot land silently.

A fourth section models a SHARED RUNTIME: one kiro-cli pid hosting two ACP
sessions, each with its own per-session token. That is a different axis from
the pid view — not "can the walk match a renumbered pid" but "can it name the
right SESSION when the pid it matches hosts two" — so it carries its own
topology and tests rather than a third ``VIEWS`` value.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# The shared topology model
# ---------------------------------------------------------------------------

SESSION_KEY = "dashboard:chat-42-topofix"

#: A SECOND ACP session multiplexed over the same kiro-cli process as
#: ``SESSION_KEY``. One process hosts N sessions (a ``spawn_run`` subagent on
#: its parent's runtime, a workflow pool worker, a shared chat runtime), so a
#: pid names the PROCESS and cannot name either of these two sessions.
CO_TENANT_KEY = "subagent:chat-42-topofix-child"

#: Per-session stub tokens. These ride each session's OWN ``mcpServers``
#: elements (``mcp_gateway.claim.mint_stub_session_token`` ->
#: ``mcp_gateway.session_servers.attach_stub_session_token``), which is what
#: makes them able to tell two sessions on one pid apart.
FOUNDER_TOKEN = "founder-stub-token"
CO_TENANT_TOKEN = "co-tenant-stub-token"

#: pid views. ``host``: every process sees real host pids (status quo after
#: the pidns revert). ``pidns``: the session subtree runs inside a PID
#: namespace — processes inside see ns-local pids; session_pid files keep
#: HOST-pid keys because the gateway writes them from outside.
VIEWS = [
    "host",
    pytest.param(
        "pidns",
        marks=pytest.mark.xfail(
            strict=True,
            reason=(
                "session_pid_<pid>.txt files are keyed by HOST pids; a "
                "namespace-local pid view cannot resolve them (2026-07-18 "
                "incident, commit 24c320f6 reverted). A "
                "pid-namespace re-raise CR must make identity resolution "
                "namespace-aware and flip this to pass."
            ),
        ),
    ),
]


class ProcessTopology:
    """Single source of truth for the session process tree in identity tests.

    Models three things the four resolver copies all depend on:

    * the host-pid parent chain (``/proc``-walk semantics),
    * the ``session_pid_<host_pid>.txt`` files the gateway writes,
    * the pid *view* of a process — what ``os.getpid()/getppid()`` and a
      ``/proc`` read return from inside that process. Under ``pidns`` the
      in-namespace processes observe ns-local pids and can only see other
      in-namespace processes.

    Tests must consume this instead of hand-rolling ``_ppid_fn`` dicts: when
    the real topology changes (e.g. a sandbox adds a namespace layer), the
    change is modeled here once and every consumer test re-runs against it.
    """

    def __init__(self, cfg_dir: Path) -> None:
        self.cfg_dir = cfg_dir
        self._parent: dict[int, int] = {}  # host pid -> host ppid
        self._ns_pid: dict[int, int] = {}  # host pid -> ns-local pid

    def add(self, pid: int, ppid: int, ns_pid: Optional[int] = None) -> None:
        self._parent[pid] = ppid
        if ns_pid is not None:
            self._ns_pid[pid] = ns_pid

    def write_session_pid(
        self,
        host_pid: int,
        session_key: str = SESSION_KEY,
        tenants: Optional[list[str]] = None,
    ) -> None:
        """The gateway-side contract: files keyed by HOST pid, always.

        *tenants* is every session key the gateway saw on this pid at
        publication time. One member (or ``None``) is the 1:1 case and must
        produce the historical single-line body;
        ``test_publisher_and_topology_agree_on_the_shared_shape`` pins both
        against the publisher's own bytes, so this helper cannot drift from the
        contract it models.
        """
        body = session_key
        if tenants and len(tenants) > 1:
            body = "\n".join(
                [session_key, f"tenants={len(tenants)}"] + [f"tenant={t}" for t in tenants]
            )
        (self.cfg_dir / f"session_pid_{host_pid}.txt").write_text(body, encoding="utf-8")

    # -- what a given process observes under a view ------------------------

    def observed_ppid(self, host_pid: int, view: str) -> int:
        """What ``os.getppid()`` returns inside process *host_pid*."""
        parent = self._parent[host_pid]
        if view == "pidns" and host_pid in self._ns_pid:
            # inside the namespace, the parent is seen by its ns-local pid
            # (or is invisible if it lives outside the namespace).
            return self._ns_pid.get(parent, 0)
        return parent

    def parent_lookup(self, view: str) -> Callable[[int], int]:
        """A ``_parent_pid(pid) -> ppid`` function as seen under *view*.

        ``host``: real-pid chain (gatewayd / un-namespaced processes).
        ``pidns``: the in-namespace ``/proc`` remount — keys and values are
        ns-local pids; anything outside the namespace does not exist (0).
        """
        if view == "host":
            return lambda pid: self._parent.get(pid, 0)

        host_of_ns = {ns: host for host, ns in self._ns_pid.items()}

        def _ns_lookup(ns_pid: int) -> int:
            host = host_of_ns.get(ns_pid)
            if host is None:
                return 0
            return self._ns_pid.get(self._parent.get(host, 0), 0)

        return _ns_lookup


# Canonical subagent tree. Host pids are chosen above any real test-host
# process range concern because all lookups are routed through the fixture.
GATEWAY = 100  # writes session_pid files; outside any namespace
SESSION_HOST = 110  # ns PID 1 when the sandbox adds a pid namespace
KIRO_CLI = 120  # ns PID 2
MCP_SERVER = 130  # ns PID 3 — the process running the resolvers below


@pytest.fixture()
def topo(tmp_path: Path) -> ProcessTopology:
    t = ProcessTopology(tmp_path)
    t.add(GATEWAY, 1)
    t.add(SESSION_HOST, GATEWAY, ns_pid=1)
    t.add(KIRO_CLI, SESSION_HOST, ns_pid=2)
    t.add(MCP_SERVER, KIRO_CLI, ns_pid=3)
    # The gateway writes the mapping for the session host it spawned —
    # keyed by the HOST pid, which is the crux of the incident.
    t.write_session_pid(SESSION_HOST)
    return t


def _wire_common(monkeypatch: pytest.MonkeyPatch, topo: ProcessTopology, view: str) -> None:
    """Environment every resolver test shares: no env key, patched getppid."""
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    # A leaked KIROCREW_HOST_PID (e.g. when the test itself runs inside a
    # sandbox whose launcher exports it) would short-circuit the /proc walk
    # under test and flip the strict pidns xfails to XPASS.
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.setattr("os.getppid", lambda: topo.observed_ppid(MCP_SERVER, view))
    # Reset the fork's process-lifetime from_env cache: an already-resolved
    # identity from an earlier test (or the host-view run of this test) would
    # otherwise short-circuit the walk and XPASS the strict pidns variants.
    monkeypatch.setattr("kiro_crew.mcp_caller._FROM_ENV_CACHE", None)
    # And the once-per-pid co-tenancy report, which is process-lifetime state
    # for the same reason: a warning already emitted by an earlier test would
    # make a later one assert on a debug line it never sees.
    monkeypatch.setattr("kiro_crew.mcp_caller._reported_co_tenancy", set())


# ---------------------------------------------------------------------------
# Walk copy 1: mcp_caller.CallerContext.from_env
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("view", VIEWS)
def test_from_env_resolves_session_key(topo, monkeypatch, view) -> None:
    from kiro_crew import mcp_caller

    _wire_common(monkeypatch, topo, view)
    monkeypatch.setattr(mcp_caller, "_parent_pid", topo.parent_lookup(view))
    # from_env imports config_dir lazily from the loader module.
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)

    ctx = mcp_caller.CallerContext.from_env()
    assert ctx.session_key == SESSION_KEY
    assert ctx.session_type == "pidfile"


# ---------------------------------------------------------------------------
# Walk copy 2: mcp_core._resolve_session_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("view", VIEWS)
def test_mcp_core_resolves_session_key(topo, monkeypatch, view) -> None:
    from kiro_crew import mcp_caller, mcp_core

    _wire_common(monkeypatch, topo, view)
    monkeypatch.setattr(mcp_caller, "_parent_pid", topo.parent_lookup(view))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)

    assert mcp_core._resolve_session_key() == SESSION_KEY


# ---------------------------------------------------------------------------
# Walk copy 3: the inline walk in mcp_shared._resolve_tool_policy
# ---------------------------------------------------------------------------
# The policy session-key walk is inlined in ``_resolve_tool_policy`` and
# its deep-walk step is a nested function reading the real /proc, so it
# cannot be patched. Model the resolvable case with the file on the DIRECT
# parent (kiro-cli): under the host view the very first ancestor matches and
# the nested /proc read is never reached; under the pidns view getppid()
# yields an ns-local pid whose session_pid file does not exist and whose
# real-/proc chain terminates without a match, so the resolver fail-opens
# WITHOUT ever calling the policy endpoint.


@pytest.mark.parametrize("view", VIEWS)
def test_mcp_shared_policy_walk_reaches_gateway(topo, monkeypatch, view) -> None:
    from kiro_crew import mcp_shared

    # Reset the module-lifetime policy caches so a prior test (or the
    # host-view run) cannot leak a cached/negative-cached result in.
    monkeypatch.setattr(mcp_shared, "_excluded_tools_by_session", {})
    monkeypatch.setattr(mcp_shared, "_last_failure_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_failure_count", 0)

    monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
    monkeypatch.setattr(mcp_shared, "config_dir", lambda: topo.cfg_dir)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)
    (topo.cfg_dir / ".local_secret").write_text("s")

    topo.write_session_pid(KIRO_CLI)  # direct parent of MCP_SERVER
    _wire_common(monkeypatch, topo, view)

    response = MagicMock()
    response.read.return_value = b'{"exclude": []}'
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)

    assert mcp_shared._resolve_tool_policy().excluded == set()
    # The walk must have RESOLVED a session key and reached the gateway —
    # under pidns it resolves empty and returns unresolved without the call.
    assert urlopen.called
    request = urlopen.call_args[0][0]
    assert request.get_header("X-session-key") == SESSION_KEY


# ---------------------------------------------------------------------------
# Walk copy 4: mcp_gateway.stub — register-time caller block + ancestor chain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("view", VIEWS)
def test_stub_caller_block_carries_session_key(topo, monkeypatch, view) -> None:
    from kiro_crew import mcp_caller
    from kiro_crew.mcp_gateway import stub

    _wire_common(monkeypatch, topo, view)
    monkeypatch.setattr(mcp_caller, "_parent_pid", topo.parent_lookup(view))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)

    caller = stub._build_caller_block(None)
    assert caller["session_key"] == SESSION_KEY
    assert caller["session_type"] == "dashboard"


@pytest.mark.parametrize("view", VIEWS)
def test_stub_ancestor_chain_reaches_session_host(topo, monkeypatch, view) -> None:
    """The claim-push index keys stub connections by REAL ancestor pids; a
    claim naming the session host must find this stub's connection."""
    from kiro_crew.mcp_gateway import stub

    _wire_common(monkeypatch, topo, view)
    # stub imports _parent_pid by value — patch the stub-module binding.
    monkeypatch.setattr(stub, "_parent_pid", topo.parent_lookup(view))

    chain = stub._ancestor_pids()
    assert SESSION_HOST in chain


# ---------------------------------------------------------------------------
# The SHARED-RUNTIME view: one pid, two sessions, one token each
# ---------------------------------------------------------------------------
# A different axis from the pid VIEW above, which asks "does this walk survive
# a different pid NUMBERING". This asks "does it name the right SESSION when
# the pid it walks to hosts two of them" — one kiro-cli process serving a
# parent slot and a ``spawn_run`` subagent, a workflow pool worker, or two
# chats on a shared runtime.
#
# ``session_pid_<pid>.txt`` holds ONE key per pid, so on a shared pid the walk
# does not fail: it SUCCEEDS with whichever co-tenant published last. That is a
# positive misattribution, and every consumer inherits it — callback routing,
# memory keys, audit rows, and the tool-policy cache that
# ``mcp_shared._policy_session_key`` keys on the answer.
#
# Two sub-cases, and they are separate because the remedies differ:
#
# * WITH the per-session token — the element carries its own name, so the
#   resolver must answer the session it belongs to, not the pid's founder.
#   Already correct: the token is read above every pid source. These are the
#   positive controls.
# * WITHOUT it — the degrade paths where no token reaches the element
#   (``mcp_gateway.stub.fallback_exec`` strips it before exec'ing a third-party
#   backend, and an explicit caller-supplied ``mcpServers`` array is not
#   re-keyed). Nothing can name the session, so the only correct answer is a
#   REFUSAL. Handing out the founder's key instead is the bug.
#
# The refusal needs evidence, and the evidence is the publisher's recorded
# tenant set: absence of a tenant section means UNKNOWN, never "not shared",
# the same asymmetry ``session_pid_sig._pid_recycled`` applies to a start token.
# Recording the KEYS rather than just a count is what lets the server side
# VERIFY a co-tenant's declared identity instead of degrading to no check.


@pytest.fixture()
def shared_topo(tmp_path: Path) -> ProcessTopology:
    """KIRO_CLI hosts SESSION_KEY and CO_TENANT_KEY; MCP_SERVER serves the latter."""
    t = ProcessTopology(tmp_path)
    t.add(GATEWAY, 1)
    t.add(SESSION_HOST, GATEWAY)
    t.add(KIRO_CLI, SESSION_HOST)
    t.add(MCP_SERVER, KIRO_CLI)
    # The founder published; the co-tenant did not (a shared subagent session
    # deliberately does not overwrite the mapping — subagent_manager/run.py
    # guards the publish on ``not use_session_sharing``). So line 1 stays the
    # founder's key for every reader that predates the tenant section, and the
    # section is what records the second session.
    t.write_session_pid(KIRO_CLI, SESSION_KEY, tenants=[SESSION_KEY, CO_TENANT_KEY])
    return t


def _wire_shared(
    monkeypatch: pytest.MonkeyPatch, topo: ProcessTopology, *, token: str = ""
) -> None:
    """Run as the CO-TENANT's MCP server on the shared pid, with or without a token."""
    from kiro_crew import session_token_sig
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.setattr("os.getppid", lambda: KIRO_CLI)
    monkeypatch.setattr("kiro_crew.mcp_caller._FROM_ENV_CACHE", None)
    monkeypatch.setattr("kiro_crew.mcp_caller._reported_co_tenancy", set())
    # One patch covers all three client resolvers: each reads the token through
    # the single shared reader ``session_token_sig.session_key_from_env_token``,
    # which dispatches to ``verify_session_token`` by module-level name.
    mapping = {FOUNDER_TOKEN: SESSION_KEY, CO_TENANT_TOKEN: CO_TENANT_KEY}
    monkeypatch.setattr(session_token_sig, "verify_session_token", lambda tok: mapping.get(tok, ""))
    if token:
        monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, token)
    else:
        monkeypatch.delenv(STUB_SESSION_TOKEN_ENV, raising=False)


def _wire_shared_caller(monkeypatch, topo: ProcessTopology, *, token: str = "") -> None:
    from kiro_crew import mcp_caller

    _wire_shared(monkeypatch, topo, token=token)
    monkeypatch.setattr(mcp_caller, "_parent_pid", topo.parent_lookup("host"))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)


def _published(tmp_path: Path, monkeypatch, *, co_tenants) -> str:
    """Bytes ``publish_session_pid`` writes for *co_tenants*, start token absent."""
    from kiro_crew import session_pid_sig

    monkeypatch.setattr(session_pid_sig, "config_dir", lambda: tmp_path)
    # No live process behind a synthetic pid, so no start token is recorded.
    monkeypatch.setattr(session_pid_sig.platform_compat, "get_process_start_id", lambda pid: None)
    session_pid_sig.publish_session_pid(KIRO_CLI, SESSION_KEY, co_tenants=co_tenants)
    return (tmp_path / f"session_pid_{KIRO_CLI}.txt").read_text(encoding="utf-8")


def test_publisher_and_topology_agree_on_the_shared_shape(tmp_path, monkeypatch) -> None:
    """The bytes this file's model writes are the bytes the publisher writes.

    Without this the model could drift into asserting a shape production never
    produces, and every refusal test below would pass against a fiction.
    """
    tenants = [SESSION_KEY, CO_TENANT_KEY]
    published = _published(tmp_path, monkeypatch, co_tenants=tenants)

    model = ProcessTopology(tmp_path / "model")
    model.cfg_dir.mkdir()
    model.write_session_pid(KIRO_CLI, SESSION_KEY, tenants=tenants)
    assert published == (model.cfg_dir / f"session_pid_{KIRO_CLI}.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("co_tenants", [None, [], [SESSION_KEY]])
def test_a_sole_tenant_publishes_the_historical_bytes(tmp_path, monkeypatch, co_tenants) -> None:
    """The 1:1 case gains no tenant section — cap-1 bytes are unchanged."""
    assert _published(tmp_path, monkeypatch, co_tenants=co_tenants) == SESSION_KEY


def test_a_truncated_membership_still_states_the_true_count(tmp_path, monkeypatch) -> None:
    """The mapping is size-bounded; the count must not shrink with the set.

    A count narrowed to what fitted makes a PARTIAL set look complete, and a
    consumer reading absence as decisive then denies every session the file had
    no room to name. Membership and count are separate facts for that reason.

    The bound is asserted on the file's own worst-case size rather than on the
    body's length, because that is the figure every reader weighs: a platform
    that stores a two-byte line separator would otherwise overflow the bound
    and have the whole mapping refused, which reads as no tenancy at all.
    """
    from kiro_crew import session_pid_sig

    # More sessions than the mapping's byte bound can enumerate.
    many = [f"dashboard:chat-{i}-{'k' * 40}" for i in range(200)]
    body = _published(tmp_path, monkeypatch, co_tenants=many)
    assert session_pid_sig._on_disk_bytes(body) <= session_pid_sig._MAX_MAPPING_FILE_BYTES

    mapping = session_pid_sig.read_session_pid_mapping(KIRO_CLI, tmp_path)
    assert mapping.tenant_count == len(many)
    assert 0 < len(mapping.tenants) < len(many)
    assert not mapping.membership_complete
    # Enumerated members are still admitted; the ones that did not fit are not
    # denied, because the caller must consult membership_complete first.
    assert mapping.admits(mapping.tenants[0])
    assert not mapping.admits(many[-1])


def test_a_translated_line_separator_still_reads_back(tmp_path, monkeypatch) -> None:
    """The record survives being stored with a two-byte line separator.

    Only Windows writes the mapping that way, so a size budget taken over the
    body alone passes on every other platform and fails there -- and it fails
    as a REFUSED file, i.e. as "this pid hosts one session", which is the
    misattribution the tenant section exists to prevent. Rewriting the bytes
    here puts that platform's outcome under the same assertion everywhere.
    """
    from kiro_crew import session_pid_sig

    many = [f"dashboard:chat-{i}-{'k' * 40}" for i in range(200)]
    body = _published(tmp_path, monkeypatch, co_tenants=many)
    assert "\n" in body, "a 200-tenant record must be multi-line for this to mean anything"

    txt = tmp_path / f"session_pid_{KIRO_CLI}.txt"
    txt.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    assert txt.stat().st_size > len(body.encode("utf-8"))
    assert txt.stat().st_size <= session_pid_sig._MAX_MAPPING_FILE_BYTES

    mapping = session_pid_sig.read_session_pid_mapping(KIRO_CLI, tmp_path)
    assert mapping.tenant_count == len(many)
    assert mapping.tenants and mapping.admits(mapping.tenants[0])


# -- walk copy 1: mcp_caller.CallerContext.from_env -------------------------


def test_from_env_refuses_a_co_tenants_key(shared_topo, monkeypatch, caplog) -> None:
    from kiro_crew import mcp_caller

    _wire_shared_caller(monkeypatch, shared_topo)
    with caplog.at_level(logging.WARNING):
        assert mcp_caller.CallerContext.from_env().session_key == ""
    # The REASON matters as much as the outcome: a refusal attributed to a pid
    # recycle or an unparseable file would send an operator after the wrong
    # thing, and is what an unextended reader produces for these same bytes.
    assert "co-tenant" in caplog.text


def test_from_env_names_the_co_tenant_from_its_own_token(shared_topo, monkeypatch) -> None:
    from kiro_crew import mcp_caller

    _wire_shared_caller(monkeypatch, shared_topo, token=CO_TENANT_TOKEN)
    ctx = mcp_caller.CallerContext.from_env()
    assert (ctx.session_key, ctx.session_type) == (CO_TENANT_KEY, "token")


# -- walk copy 2: mcp_core._resolve_session_key -----------------------------


def test_mcp_core_refuses_a_co_tenants_key(shared_topo, monkeypatch, caplog) -> None:
    from kiro_crew import mcp_caller, mcp_core

    _wire_shared(monkeypatch, shared_topo)
    monkeypatch.setattr(mcp_caller, "_parent_pid", shared_topo.parent_lookup("host"))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: shared_topo.cfg_dir)
    with caplog.at_level(logging.WARNING):
        assert mcp_core._resolve_session_key() == ""
    assert "co-tenant" in caplog.text


def test_mcp_core_names_the_co_tenant_from_its_own_token(shared_topo, monkeypatch) -> None:
    from kiro_crew import mcp_caller, mcp_core

    _wire_shared(monkeypatch, shared_topo, token=CO_TENANT_TOKEN)
    monkeypatch.setattr(mcp_caller, "_parent_pid", shared_topo.parent_lookup("host"))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: shared_topo.cfg_dir)
    assert mcp_core._resolve_session_key() == CO_TENANT_KEY


# -- walk copy 3: the policy walk in mcp_shared -----------------------------


def _wire_shared_policy(monkeypatch, topo: ProcessTopology, *, token: str = "") -> MagicMock:
    from kiro_crew import mcp_shared

    monkeypatch.setattr(mcp_shared, "_excluded_tools_by_session", {})
    monkeypatch.setattr(mcp_shared, "_last_failure_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_failure_count", 0)
    monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
    monkeypatch.setattr(mcp_shared, "config_dir", lambda: topo.cfg_dir)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)
    (topo.cfg_dir / ".local_secret").write_text("s")
    _wire_shared(monkeypatch, topo, token=token)
    response = MagicMock()
    response.read.return_value = b'{"exclude": []}'
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)
    return urlopen


def test_mcp_shared_policy_refuses_a_co_tenants_key(shared_topo, monkeypatch, caplog) -> None:
    """A misresolved policy key applies one session's tool exclusions to another.

    So the refusal has to be a class that actually REFUSES. The empty exclusion
    set this path returns is not a permission, and what stops it being read as
    one is ``unresolved`` naming a class in ``_UNRESOLVED_REFUSES_CALL``. Pinning
    membership rather than the string is the point: a rename that dropped the
    class from that set would leave the label looking right while every excluded
    tool became callable on this topology.
    """
    from kiro_crew import mcp_shared

    urlopen = _wire_shared_policy(monkeypatch, shared_topo)
    with caplog.at_level(logging.WARNING):
        policy = mcp_shared._resolve_tool_policy()
    assert policy.excluded == frozenset()
    assert policy.unresolved in mcp_shared._UNRESOLVED_REFUSES_CALL
    assert policy.unresolved == "resolution_failed"
    assert not urlopen.called
    assert "co-tenant" in caplog.text


def test_mcp_shared_policy_still_proceeds_when_nothing_has_named_the_session(
    shared_topo, monkeypatch, tmp_path
) -> None:
    """The other empty key must NOT start refusing, or warm-pool startup breaks.

    A co-tenant refusal and a not-yet-claimed session are both empty keys, and
    only the first is unusable. A warm-pool process resolves before its mapping
    exists, and ``tools/list`` is called once and cached for the session, so
    hardening this arm would hide every tool for as long as that session lives.

    The walk runs for real against a directory holding NO mapping, rather than
    against a stubbed rung: stubbing the rung is what lets a mutation inside it
    survive, since the pin would then never execute the line it is meant to hold.
    """
    from kiro_crew import mcp_shared

    urlopen = _wire_shared_policy(monkeypatch, shared_topo)
    # Point every config-dir seam at an EMPTY directory: same walk, no answers.
    empty = tmp_path / "no-mappings"
    empty.mkdir()
    (empty / ".local_secret").write_text("s")
    monkeypatch.setattr(mcp_shared, "config_dir", lambda: empty)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: empty)

    policy = mcp_shared._resolve_tool_policy()
    assert policy.unresolved == "no_session_key"
    assert policy.unresolved not in mcp_shared._UNRESOLVED_REFUSES_CALL
    assert not urlopen.called


def test_mcp_shared_policy_names_the_co_tenant_from_its_own_token(shared_topo, monkeypatch) -> None:
    from kiro_crew import mcp_shared

    urlopen = _wire_shared_policy(monkeypatch, shared_topo, token=CO_TENANT_TOKEN)
    assert mcp_shared._resolve_tool_policy().excluded == set()
    assert urlopen.call_args[0][0].get_header("X-session-key") == CO_TENANT_KEY


# -- walk copy 4: the stub's register-time caller block ---------------------


def test_stub_caller_block_refuses_a_co_tenants_key(shared_topo, monkeypatch, caplog) -> None:
    from kiro_crew.mcp_gateway import stub

    _wire_shared_caller(monkeypatch, shared_topo)
    with caplog.at_level(logging.WARNING):
        assert stub._build_caller_block(None)["session_key"] == ""
    assert "co-tenant" in caplog.text


def test_stub_caller_block_names_the_co_tenant_from_its_own_token(shared_topo, monkeypatch) -> None:
    from kiro_crew.mcp_gateway import stub

    _wire_shared_caller(monkeypatch, shared_topo, token=CO_TENANT_TOKEN)
    assert stub._build_caller_block(None)["session_key"] == CO_TENANT_KEY


# -- the server-side walk: gatewayd / dashboard peer verification -----------


def test_peer_identity_refuses_to_name_a_shared_pids_founder(shared_topo, monkeypatch) -> None:
    """The server-side walk has NO token channel, so a shared pid is all it sees.

    It must decline to NAME a session rather than attribute the peer to the
    founder: ``dashboard.token_auth`` turns that name into a 403 for a
    legitimate co-tenant, and gatewayd turns it into a claim on the wrong
    session. The ancestor chain stays complete — claim-push indexing needs it
    whether or not a key resolved.
    """
    from kiro_crew.mcp_gateway import gatewayd as gw

    monkeypatch.setattr(gw, "_config_dir", lambda: shared_topo.cfg_dir)
    monkeypatch.setattr(gw, "_ppid_fn", shared_topo.parent_lookup("host"))

    key, chain = gw._resolve_peer_identity(MCP_SERVER)
    assert key == ""
    assert chain == [MCP_SERVER, KIRO_CLI, SESSION_HOST, GATEWAY]


def test_peer_identity_admits_a_recorded_co_tenant(shared_topo, monkeypatch) -> None:
    """Declining to NAME one session is not the same as knowing nothing.

    The recorded membership is what lets ``token_auth`` verify a co-tenant's
    declared key instead of 403-ing it, so the walk has to surface the set.
    """
    from kiro_crew.peer_resolve import resolve_peer_tenancy

    tenancy = resolve_peer_tenancy(
        MCP_SERVER,
        config_dir_fn=lambda: shared_topo.cfg_dir,
        ppid_fn=shared_topo.parent_lookup("host"),
    )
    assert tenancy.session_key == ""
    assert tenancy.admits(CO_TENANT_KEY)
    assert tenancy.admits(SESSION_KEY)
    assert not tenancy.admits("dashboard:chat-99-elsewhere")
    assert tenancy.unverifiable is False


def test_peer_tenancy_carries_that_it_passed_an_unusable_mapping(shared_topo) -> None:
    """The walk skips a mapping it cannot use so it can reach the ancestor
    holding the real one -- but the skip is EVIDENCE, and an authorization
    consumer owes a different answer to "a record here was unreadable" than to
    "there was no record".

    The state built here is the republication window's own: BOTH files present,
    with a ``.sig`` that does not match the ``.txt`` beside it. That is what a
    reader sees between the two ``os.replace`` calls of a body-changing publish,
    and it is indistinguishable from a forgery -- which is why it must not read
    as "this pid hosts one session".
    """
    from kiro_crew.peer_resolve import resolve_peer_tenancy

    # A non-empty signature that cannot verify against the body: the window's
    # shape. Missing the `.sig` entirely would be REFUSAL_ABSENT instead, which
    # is the case the next test covers.
    (shared_topo.cfg_dir / f"session_pid_{KIRO_CLI}.sig").write_text("00" * 32, encoding="utf-8")

    tenancy = resolve_peer_tenancy(
        MCP_SERVER,
        config_dir_fn=lambda: shared_topo.cfg_dir,
        ppid_fn=shared_topo.parent_lookup("host"),
        signed_only=True,
    )
    assert tenancy.session_key == ""
    assert tenancy.tenant_count == 0
    assert tenancy.shared is False
    assert tenancy.membership_complete is False
    assert tenancy.unverifiable is True


def test_peer_tenancy_reports_a_genuine_absence_as_absent(shared_topo) -> None:
    """The converse, and the reason the flag is keyed on a record being PRESENT:
    a pid with no mapping anywhere in its ancestry must stay the degrade arm --
    warm-pool runtimes before claim, cron scripts, pooled MCP backends."""
    from kiro_crew.peer_resolve import resolve_peer_tenancy

    for leftover in shared_topo.cfg_dir.glob("session_pid_*"):
        leftover.unlink()

    tenancy = resolve_peer_tenancy(
        MCP_SERVER,
        config_dir_fn=lambda: shared_topo.cfg_dir,
        ppid_fn=shared_topo.parent_lookup("host"),
        signed_only=True,
    )
    assert tenancy.session_key == ""
    assert tenancy.unverifiable is False


def test_a_proven_recycle_is_not_unverifiable(shared_topo, monkeypatch) -> None:
    """HEADLINE: a signed recycle must stay on the degrade arm, not demand a token.

    It is reported only AFTER the MAC verifies, so it is the publisher's own
    attested statement that the pid moved on -- knowledge about a DIFFERENT
    process, not ambiguity about this one, and an agent cannot plant a signed
    one. The orphan sweep leaves such a file for every live recycled pid, so
    counting it would 403 the tokenless callers the degrade arm exists for
    (a cron script, a pooled MCP backend) for as long as that file sits in
    their ancestry -- callers that work on main today.
    """
    from kiro_crew import platform_compat, session_pid_sig
    from kiro_crew.peer_resolve import resolve_peer_tenancy

    # Publish a SIGNED 1:1 mapping, then make the live start token disagree with
    # the recorded one: REFUSAL_RECYCLED, reached only past a verifying MAC.
    monkeypatch.setattr(session_pid_sig, "config_dir", lambda: shared_topo.cfg_dir)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "recorded-token")
    session_pid_sig.publish_session_pid(KIRO_CLI, SESSION_KEY)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "live-token-differs")

    mapping = session_pid_sig.verify_session_pid_mapping(KIRO_CLI, shared_topo.cfg_dir)
    assert mapping.refusal == session_pid_sig.REFUSAL_RECYCLED, mapping

    tenancy = resolve_peer_tenancy(
        MCP_SERVER,
        config_dir_fn=lambda: shared_topo.cfg_dir,
        ppid_fn=shared_topo.parent_lookup("host"),
        signed_only=True,
    )
    assert tenancy.session_key == ""
    assert tenancy.unverifiable is False


def test_single_tenant_peer_identity_still_resolves(topo, monkeypatch) -> None:
    """Control: the 1:1 mapping the previous tests' shape differs from."""
    from kiro_crew.mcp_gateway import gatewayd as gw

    monkeypatch.setattr(gw, "_config_dir", lambda: topo.cfg_dir)
    monkeypatch.setattr(gw, "_ppid_fn", topo.parent_lookup("host"))
    assert gw._resolve_peer_identity(MCP_SERVER)[0] == SESSION_KEY


# ---------------------------------------------------------------------------
# Token above pid: the rungs disagree after a warm-pool rekey
# ---------------------------------------------------------------------------
# The token's position in the ladder is load-bearing on a 1:1 pid too, and this
# is the case that shows it. A warm-pool process is re-keyed to a new session
# while the mapping a reader finds may still name the PREVIOUS one, so where the
# two disagree the pid is the stale answer and the token is the current one.
# Without this, a ladder that consulted the pid first would look correct
# everywhere the two agree, which is everywhere else in this file.


@pytest.mark.parametrize(
    "resolver",
    ["from_env", "mcp_core", "mcp_shared_policy"],
)
def test_the_token_outranks_a_stale_pid_mapping(topo, monkeypatch, resolver) -> None:
    from kiro_crew import mcp_caller, mcp_core, mcp_shared, session_token_sig
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

    # The mapping on the direct parent names the session this process was
    # spawned under; the token on its own element names the one it serves now.
    topo.write_session_pid(KIRO_CLI, SESSION_KEY)
    _wire_common(monkeypatch, topo, "host")
    monkeypatch.setattr(mcp_caller, "_parent_pid", topo.parent_lookup("host"))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)
    monkeypatch.setattr(
        session_token_sig,
        "verify_session_token",
        lambda tok: CO_TENANT_KEY if tok == CO_TENANT_TOKEN else "",
    )
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, CO_TENANT_TOKEN)

    if resolver == "from_env":
        assert mcp_caller.CallerContext.from_env().session_key == CO_TENANT_KEY
    elif resolver == "mcp_core":
        assert mcp_core._resolve_session_key() == CO_TENANT_KEY
    else:
        monkeypatch.setattr(mcp_shared, "_excluded_tools_by_session", {})
        assert mcp_shared._policy_session_key() == CO_TENANT_KEY


# ---------------------------------------------------------------------------
# Rung 1: the protected member binding, above every other source
# ---------------------------------------------------------------------------
# The binding gates a PRIVATE memory store, and every rung below it is writable
# by the same uid it exists to fence, so its three answers must not be
# collapsed: a key binds, an EMPTY string is a record that exists and is
# invalid (a refusal, which must not fall through), and None is no binding at
# all. A probe that RAISES is the machinery breaking, which is a fourth answer
# and also must not fall through -- the record it could not read may be the
# refusal.


def _wire_protected(monkeypatch, topo, answer, *, raises: bool = False) -> None:
    import kiro_crew.member_memory_auth as mma
    from kiro_crew import mcp_caller

    _wire_common(monkeypatch, topo, "host")
    monkeypatch.setattr(mcp_caller, "_parent_pid", topo.parent_lookup("host"))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)

    def probe(pid):
        if raises:
            raise RuntimeError("member record unreadable")
        return answer

    monkeypatch.setattr(mma, "protected_member_session_for_pid", probe)


def test_a_protected_binding_outranks_the_pid_mapping(topo, monkeypatch) -> None:
    from kiro_crew import mcp_caller

    _wire_protected(monkeypatch, topo, "dashboard:member-alice")
    identity = mcp_caller.resolve_own_identity()
    assert (identity.session_key, identity.source) == ("dashboard:member-alice", "protected-pid")


def test_an_invalid_protected_record_refuses_without_falling_through(topo, monkeypatch) -> None:
    """An empty binding is a refusal, and the pid mapping must not answer past it."""
    from kiro_crew import mcp_caller

    _wire_protected(monkeypatch, topo, "")
    identity = mcp_caller.resolve_own_identity()
    assert (identity.session_key, identity.source) == ("", "protected-pid")
    assert not identity.failed


def test_a_raising_protected_probe_reports_a_broken_resolution(topo, monkeypatch) -> None:
    from kiro_crew import mcp_caller, mcp_shared

    _wire_protected(monkeypatch, topo, None, raises=True)
    identity = mcp_caller.resolve_own_identity()
    assert identity.session_key == "" and identity.failed
    # The policy lookup turns that into its own third outcome, distinct from
    # "no identity yet", so a broken host is not reported as a benign race.
    monkeypatch.setattr(mcp_shared, "_excluded_tools_by_session", {})
    assert mcp_shared._policy_session_key() is None


def test_no_protected_binding_falls_through_to_the_pid_mapping(topo, monkeypatch) -> None:
    from kiro_crew import mcp_caller

    _wire_protected(monkeypatch, topo, None)
    identity = mcp_caller.resolve_own_identity()
    assert (identity.session_key, identity.source) == (SESSION_KEY, "pidfile")


def test_mcp_cores_lenient_resolver_does_not_consult_the_binding(topo, monkeypatch) -> None:
    """The binding gates memory, and this resolver's consumers are attribution."""
    from kiro_crew import mcp_core

    _wire_protected(monkeypatch, topo, "dashboard:member-alice")
    assert mcp_core._resolve_session_key() == SESSION_KEY


def test_the_co_tenancy_warning_is_reported_once_per_pid(shared_topo, monkeypatch, caplog) -> None:
    """The recaller poll does not terminate while a key is unresolved.

    An unthrottled line there floods the log at the one moment an operator
    needs to read it, so the operator-facing message fires once per pid and
    repeats land at debug.
    """
    from kiro_crew import mcp_caller

    _wire_shared_caller(monkeypatch, shared_topo)
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            monkeypatch.setattr("kiro_crew.mcp_caller._FROM_ENV_CACHE", None)
            assert mcp_caller.resolve_own_identity().session_key == ""
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    repeats = [r for r in caplog.records if "still names" in r.getMessage()]
    assert len(warnings) == 1
    assert len(repeats) == 2


# ---------------------------------------------------------------------------
# Resolution path 5: KIROCREW_HOST_PID env shortcut.
# The sandbox launcher exports its own HOST pid before any fork/namespace
# work, so every resolver can look the session_pid file up DIRECTLY —
# this is the namespace-aware path the pidns xfails above point at.
# ---------------------------------------------------------------------------

#: Unlike VIEWS, no xfail: the env shortcut must resolve under BOTH views.
VIEWS_ALL_PASS = ["host", "pidns"]


@pytest.mark.parametrize("view", VIEWS_ALL_PASS)
def test_from_env_host_pid_env_resolves_in_any_view(topo, monkeypatch, view) -> None:
    """KIROCREW_HOST_PID resolution must succeed under BOTH pid views — it
    bypasses the /proc walk entirely, which is its whole point."""
    from kiro_crew import mcp_caller

    _wire_common(monkeypatch, topo, view)
    monkeypatch.setattr(mcp_caller, "_parent_pid", topo.parent_lookup(view))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)
    # The launcher (session host) exported its HOST pid before unshare.
    monkeypatch.setenv("KIROCREW_HOST_PID", str(SESSION_HOST))

    ctx = mcp_caller.CallerContext.from_env()
    assert ctx.session_key == SESSION_KEY
    assert ctx.session_type == "pidfile"


@pytest.mark.parametrize("view", VIEWS_ALL_PASS)
def test_mcp_core_host_pid_env_resolves_in_any_view(topo, monkeypatch, view) -> None:
    from kiro_crew import mcp_caller, mcp_core

    _wire_common(monkeypatch, topo, view)
    monkeypatch.setattr(mcp_caller, "_parent_pid", topo.parent_lookup(view))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)
    monkeypatch.setenv("KIROCREW_HOST_PID", str(SESSION_HOST))

    assert mcp_core._resolve_session_key() == SESSION_KEY


# ---------------------------------------------------------------------------
# Resolution path 6: gatewayd server-side peer-identity walk.
# Runs in gatewayd's OWN pid namespace (host pids via SO_PEERCRED), so it is
# immune to the client's view by construction — the "view" axis does not
# apply; what matters is that a host-pid walk from the peer resolves the key
# and returns the full host chain for claim indexing.
# ---------------------------------------------------------------------------


def test_gatewayd_peer_identity_resolves_via_host_walk(topo, monkeypatch) -> None:
    from kiro_crew.mcp_gateway import gatewayd as gw

    monkeypatch.setattr(gw, "_config_dir", lambda: topo.cfg_dir)
    monkeypatch.setattr(gw, "_ppid_fn", topo.parent_lookup("host"))

    key, chain = gw._resolve_peer_identity(MCP_SERVER)
    assert key == SESSION_KEY
    assert chain == [MCP_SERVER, KIRO_CLI, SESSION_HOST, GATEWAY]


# ---------------------------------------------------------------------------
# Hardened-reader wiring: every registered .txt reader must refuse a planted
# symlink at the predictable session_pid_<pid>.txt path (same-uid agent
# symlink-planting — the surface session_pid_sig.read_session_pid_txt
# closes). mcp_core's refusal is locked in test_resolve_session_key.py;
# these lock the remaining three readers.
# ---------------------------------------------------------------------------


def _plant_symlink(topo, host_pid: int) -> None:
    """Replace the mapping file for *host_pid* with a symlink to a secret."""
    secret = topo.cfg_dir / "victim-secret"
    secret.write_text("dashboard:chat-stolen", encoding="utf-8")
    txt = topo.cfg_dir / f"session_pid_{host_pid}.txt"
    txt.unlink(missing_ok=True)
    txt.symlink_to(secret)


def test_from_env_refuses_symlinked_pid_file(topo, monkeypatch) -> None:
    from kiro_crew import mcp_caller

    _plant_symlink(topo, SESSION_HOST)
    _wire_common(monkeypatch, topo, "host")
    monkeypatch.setattr(mcp_caller, "_parent_pid", topo.parent_lookup("host"))
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)

    ctx = mcp_caller.CallerContext.from_env()
    assert ctx.session_key == ""


def test_mcp_shared_refuses_symlinked_pid_file(topo, monkeypatch) -> None:
    from kiro_crew import mcp_shared

    monkeypatch.setattr(mcp_shared, "_excluded_tools_by_session", {})
    monkeypatch.setattr(mcp_shared, "_last_failure_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_failure_count", 0)

    monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
    monkeypatch.setattr(mcp_shared, "config_dir", lambda: topo.cfg_dir)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: topo.cfg_dir)
    (topo.cfg_dir / ".local_secret").write_text("s")

    # Symlink at the DIRECT parent's path (where the resolvable-case test
    # plants a real file) plus one at the fixture-written SESSION_HOST path,
    # so no ancestor resolves via a symlink.
    _plant_symlink(topo, SESSION_HOST)
    _plant_symlink(topo, KIRO_CLI)
    _wire_common(monkeypatch, topo, "host")

    urlopen = MagicMock()
    monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)

    # No key resolvable -> startup-race refusal WITHOUT a policy call and,
    # crucially, WITHOUT the stolen key ever being read through the symlink.
    policy = mcp_shared._resolve_tool_policy()
    assert policy.excluded == set()
    # Refusing to follow the symlink must not be reported as an empty
    # exclusion list, or a stolen-key attempt would silently widen the deny.
    assert policy.unresolved == "no_session_key"
    assert not urlopen.called


def test_gatewayd_peer_identity_refuses_symlinked_pid_file(topo, monkeypatch) -> None:
    from kiro_crew.mcp_gateway import gatewayd as gw

    _plant_symlink(topo, SESSION_HOST)
    monkeypatch.setattr(gw, "_config_dir", lambda: topo.cfg_dir)
    monkeypatch.setattr(gw, "_ppid_fn", topo.parent_lookup("host"))

    key, chain = gw._resolve_peer_identity(MCP_SERVER)
    assert key == ""
    # The chain must remain complete for claim indexing even when the key
    # is refused — identity repair happens later via claim-push.
    assert chain == [MCP_SERVER, KIRO_CLI, SESSION_HOST, GATEWAY]


# ---------------------------------------------------------------------------
# Call-site registry guard
# ---------------------------------------------------------------------------
# Every file referencing the session_pid_<pid>.txt contract must be listed
# here with its declared role and namespace assumption. If this test fails
# on your change: prefer REUSING one of the registered resolvers over adding
# a new copy of the /proc walk; if a new reference is genuinely needed, add
# it here with its role, and add pid-view parametrized tests above for any
# new resolution path.

_SESSION_PID_TOKEN = re.compile(r"session_pid_(\{|\*|<)")

#: file (relative to src/kiro_crew) -> declared role / namespace assumption
_REGISTERED_CALL_SITES: dict[str, str] = {
    "messaging/identity.py": (
        "SOLE per-turn writer — publish_turn_identity() calls "
        "session_pid_sig.publish_session_pid to key the session_pid_<pid>.txt "
        "file (plus HMAC sidecar) by the spawned session's HOST pid. Every "
        "turn-running surface (dashboard, native Slack, and each channel "
        "transport_dispatch) calls this one helper instead of copy-pasting the "
        "publish block — the per-surface duplication that caused #232"
    ),
    "session_pid_sig.py": (
        "canonical owner of the file contract — writer, strict verifier, "
        "and lenient hardened reader: publishes session_pid_<pid>.txt with "
        "an HMAC-SHA256 sidecar (session_pid_<pid>.sig, keyed by the "
        "SEL trust root sel_hmac.key), verifies it for strict resolvers "
        "(pid bound into the MAC), and exposes read_session_pid_txt "
        "(no-follow, regular-file, size-bounded; unsigned) for lenient "
        "readers — HOST-pid keyed"
    ),
    "mcp_caller.py": (
        "SOLE client-side resolver — resolve_own_identity() owns the one /proc "
        "ancestry walk every client-side consumer shares (mcp_core's lenient "
        "path, the managed-tool-policy lookup in mcp_shared, and the stub's "
        "register block), replacing the four independent copies whose "
        "per-file mocks each encoded their author's topology assumptions. "
        "Assumes HOST pids; .txt reads via "
        "session_pid_sig.read_session_pid_mapping (hardened, unsigned), which "
        "reports WHY it declined so a co-tenant refusal is not logged as a "
        "missing file. Consulted only BELOW the protected member binding and "
        "the per-SESSION token (session_token_sig.session_key_from_env_token), "
        "which from_env reads above its own cache so a warm-pool rekey stays "
        "visible"
    ),
    "mcp_core.py": (
        "stale-file cleanup glob only (assumes HOST pids) — its lenient "
        "resolver no longer carries a walk of its own and delegates to "
        "mcp_caller.resolve_own_identity; the STRICT path delegates to "
        "session_pid_sig.verify_session_pid (HMAC-verified, direct "
        "KIROCREW_HOST_PID lookup, no walk)"
    ),
    "mcp_gateway/stub.py": "reader via CallerContext.from_env; register-time caller block — assumes HOST pids",
    "peer_resolve.py": (
        "reader: the SERVER-side /proc ancestry walk (extracted from "
        "mcp_gateway/gatewayd._resolve_peer_identity, which now delegates "
        "here) — runs in the server's own (host) pid namespace, so it is "
        "immune to client-side namespace divergence; returns the session key "
        "plus the host ancestor chain (gatewayd indexes the chain for "
        "claim-push matching); .txt reads via "
        "session_pid_sig.read_session_pid_txt (hardened, unsigned). Consumed "
        "by gatewayd (stub register) and dashboard/token_auth (unix-socket "
        "peer verification)"
    ),
    "dashboard/token_auth.py": (
        "reader (via peer_resolve.resolve_peer_identity, no inline walk): "
        "kernel-attests internal-API requests arriving on the dashboard's "
        "AF_UNIX socket — SO_PEERCRED peer pid → host-namespace ancestry walk "
        "→ session_pid_<pid>.txt; denies when the resolved key differs from "
        "the client-declared X-Session-Key header, degrades to status quo "
        "when unresolvable"
    ),
    "sandbox_launcher.py": (
        "writer-adjacent: launcher exports KIROCREW_HOST_PID (its own HOST pid — "
        "the exact pid the gateway keys the file by) before fork/namespace work, "
        "so in-namespace readers can look the file up directly without a /proc walk"
    ),
    "mcp_gateway/claim.py": "docstring reference to the contract (no code reads)",
    "config/paths.py": (
        "docstring reference only (no code reads): shared_kiro_agents_writable "
        "explains the #9690 failure shape — a foreign KIROCREW_HOME pinned into "
        "the shared agent specs makes stubs search session_pid_<pid> mappings "
        "in a home the real gateway never writes"
    ),
    "session_pid.py": (
        "stale-file cleanup: globs session_pid_*.txt (+ .sig sidecars) for dead "
        "processes, and (age-bounded) session_token_*.sig mappings, whose "
        "token-hash filenames name no pid to probe"
    ),
    "session_cleanup.py": (
        "SCHEDULER, not a reader or a resolver: the periodic cleanup tick calls "
        "session_pid._prune_stale_session_pid_files on the maintenance executor "
        "so the pass above runs more often than gateway startup and shutdown. It "
        "reads no mapping, resolves no session key, and walks no /proc — the pid "
        "view it would need is the one the pass it delegates to already uses, so "
        "the pid-view parametrization this file requires of a new resolution path "
        "has no new path to cover. It appears in this scan only because its "
        "docstring names the file family it schedules the prune of, and says why "
        "the prune is scheduled here rather than on a provider teardown, which "
        "is synchronous on the gateway event loop"
    ),
    "session_token_sig.py": (
        "SIBLING contract, NOT a session_pid reader: owns the per-SESSION "
        "token -> session-key mapping (session_token_<sha256(token)>.sig, one "
        "file holding MAC + body, signed with a subkey derived from the same SEL "
        "trust root under a DIFFERENT domain label so the two sidecars cannot be "
        "cross-replayed). It appears in this scan only because it IMPORTS "
        "session_pid_sig's hardened reader and key loader rather than copying "
        "them, and because its docstring contrasts the two contracts. It reads "
        "and writes no session_pid file and does no /proc walk — deliberately: "
        "a pid names a PROCESS, and one kiro-cli process hosts many ACP "
        "sessions, so pid-keyed identity answers with the parent for a "
        "spawn_run subagent. Being pid-FREE is the property that makes it "
        "namespace-insensitive, so the pid-view parametrization this file "
        "requires of a new resolution path has nothing to vary"
    ),
    "mcp_computer.py": (
        "comment reference only (no code reads): the computer-use stdio shim "
        "explains why it resolves identity with mcp_core._resolve_session_key_strict "
        "(HMAC-verified, direct KIROCREW_HOST_PID lookup) rather than the lenient "
        "walk — an unresolved key is treated as an unattended surface and refused "
        "before anything reaches the wire"
    ),
}


def _src_root() -> Path:
    return Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


def test_session_pid_call_sites_are_registered() -> None:
    src = _src_root()
    found = {
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if _SESSION_PID_TOKEN.search(p.read_text(encoding="utf-8", errors="replace"))
    }
    registered = set(_REGISTERED_CALL_SITES)

    unregistered = found - registered
    stale = registered - found
    assert not unregistered, (
        "New session_pid_<pid>.txt call site(s) detected: "
        f"{sorted(unregistered)}.\n"
        "The session_pid contract is HOST-pid-keyed and namespace-sensitive "
        "(see the 2026-07-18 pid-namespace incident). Prefer reusing an "
        "existing registered resolver over adding another copy of the /proc "
        "walk. If the reference is intentional, register it in "
        "_REGISTERED_CALL_SITES with its role, and add pid-view parametrized "
        "tests in this file for any new resolution path."
    )
    assert not stale, (
        f"Registered session_pid call site(s) no longer reference the token: "
        f"{sorted(stale)}. Remove them from _REGISTERED_CALL_SITES."
    )


# ---------------------------------------------------------------------------
# Reflexive-tool strict-gate ratchet
# ---------------------------------------------------------------------------
# A REFLEXIVE MCP tool is one whose semantics embed "my session": ledger
# writes, monitor loops, session-scoped control, attributed channel sends. The
# invariant is that every one of them resolves the caller through the single
# fail-closed gate ``mcp_core.require_strict_session_key`` — never the lenient
# resolver (whose /proc ancestor walk hands a subagent its PARENT slot's
# identity) and never a private copy of the strict check. Before the gate
# existed the fail-closed handling was re-derived by hand at every call site,
# and nothing stopped the NEXT reflexive tool from calling the lenient
# resolver and silently writing the parent slot's state from a subagent.
# Same shape as the session_pid registry guard above: scan the source, fail on
# an unregistered file, so the invariant is enforced rather than remembered.

#: Call-form tokens. Prose references (docstrings without parens, ``#``
#: comments, which are stripped per line below) do not count as calls.
_STRICT_RESOLVER_CALL = re.compile(r"_resolve_session_key_strict\s*\(")
_REFLEXIVE_GATE_CALL = re.compile(r"require_strict_session_key\s*\(")


def _code_text(path: Path) -> str:
    """File text with per-line ``#`` comments stripped (heuristic, no parser).

    Good enough for these tokens: neither contains ``#``, and a call is never
    legitimately split across a comment boundary.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(line.split("#", 1)[0] for line in lines)


def test_reflexive_tools_route_through_the_strict_gate() -> None:
    from kiro_crew.mcp_core import REFLEXIVE_TOOL_MODULES

    src = _src_root()
    direct: set[str] = set()
    gated: set[str] = set()
    for p in src.rglob("*.py"):
        rel = p.relative_to(src).as_posix()
        text = _code_text(p)
        if _STRICT_RESOLVER_CALL.search(text):
            direct.add(rel)
        if _REFLEXIVE_GATE_CALL.search(text):
            gated.add(rel)

    # 1. The gate is the ONLY caller of the strict resolver. mcp_core.py hosts
    #    both, plus the resolver's own internal consumers (diagnosis, the
    #    Slack identity classifier), so it is the single permitted file.
    direct -= {"mcp_core.py"}
    assert not direct, (
        f"Direct _resolve_session_key_strict() call(s) outside mcp_core: "
        f"{sorted(direct)}.\n"
        "Reflexive tools must resolve identity through "
        "mcp_core.require_strict_session_key so the fail-closed handling "
        "stays in one place. Route the call through the gate and register "
        "the module in mcp_core.REFLEXIVE_TOOL_MODULES."
    )

    # 2. Every module calling the gate is registered as reflexive, and every
    #    registered module still calls it — the set is data, not lore.
    gated -= {"mcp_core.py"}
    registered = set(REFLEXIVE_TOOL_MODULES)
    unregistered = gated - registered
    stale = registered - gated
    assert not unregistered, (
        f"New reflexive-tool module(s) call require_strict_session_key but are "
        f"not registered: {sorted(unregistered)}.\n"
        "Add them to mcp_core.REFLEXIVE_TOOL_MODULES so the reflexive surface "
        "stays enumerable."
    )
    assert not stale, (
        f"Registered reflexive module(s) no longer call the gate: "
        f"{sorted(stale)}. Either the tool regressed to a private identity "
        "check (fix the tool) or it is gone (remove it from "
        "mcp_core.REFLEXIVE_TOOL_MODULES)."
    )


@pytest.mark.parametrize("identified", [True, False])
@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("kiro_cli_logs", {}),
        ("search_chat_history", {"query": "sharedmarker"}),
        ("get_chat_session", {"session_key": "dashboard:target"}),
        ("list_sessions", {"summarize": True}),
    ],
)
def test_session_reads_require_and_reuse_strict_identity(
    tmp_path, monkeypatch, identified, tool_name, args
):
    from kiro_crew import mcp_core
    from kiro_crew.history import ConversationLog
    from kiro_crew.mcp_tools import logs, sessions

    caller_key = "dashboard:child"
    refusal = "Error: session identity unavailable. Fixture diagnosis."
    gate = MagicMock(return_value=(caller_key, "") if identified else ("", refusal))
    monkeypatch.setattr(mcp_core, "require_strict_session_key", gate)
    monkeypatch.setattr(
        mcp_core,
        "_resolve_session_key",
        MagicMock(side_effect=AssertionError("must not fall back to parent identity")),
    )
    audit = MagicMock()
    monkeypatch.setattr(mcp_core, "sel", lambda: audit)
    gateway = MagicMock(return_value={"summaries": {}})
    monkeypatch.setattr(mcp_core, "_post", gateway)

    history = ConversationLog(base_dir=tmp_path / "sessions")
    history.update_metadata(caller_key, {"workspace": "child"})
    for key, workspace, body in (
        ("dashboard:target", "child", "CHILD-HISTORY sharedmarker"),
        ("dashboard:parent", "parent", "PARENT-HISTORY sharedmarker"),
    ):
        history.append(key, "user", body)
        history.update_metadata(key, {"workspace": workspace, "title": body})
    history_reader = MagicMock(return_value=history)
    monkeypatch.setattr(sessions, "ConversationLog", history_reader)
    log_reader = MagicMock(return_value="READABLE PROTOCOL")
    monkeypatch.setattr(logs.diagnostics, "read_kiro_cli_logs", log_reader)

    handler = logs.kiro_cli_logs if tool_name == "kiro_cli_logs" else getattr(sessions, tool_name)
    result = handler(tool_name, args)

    gate.assert_called_once()
    if not identified:
        assert result == refusal
        history_reader.assert_not_called()
        log_reader.assert_not_called()
        gateway.assert_not_called()
        return
    assert ("READABLE PROTOCOL" if tool_name == "kiro_cli_logs" else "CHILD-HISTORY") in result
    assert "PARENT-HISTORY" not in result
    assert audit.log_tool_invocation.call_args.kwargs["session_key"] == caller_key
    if tool_name == "list_sessions":
        gateway.assert_called_once()
        assert gateway.call_args.args[0] == "/api/sessions/summarize"
        assert set(gateway.call_args.args[1]["keys"]) == {"dashboard_target", "dashboard_child"}
        assert gateway.call_args.kwargs == {"timeout": 120, "session_key": caller_key}


@pytest.mark.parametrize("identified", [True, False])
def test_memory_recall_uses_the_shared_gate_identity_once(monkeypatch, identified):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import learn

    key = "subagent:memory-recall" if identified else ""
    refusal = "Error: unresolved session. Fixture installation diagnosis."
    gate = MagicMock(return_value=(key, "" if identified else refusal))
    gateway = MagicMock(return_value={"store": "member-alice"})
    monkeypatch.setattr(mcp_core, "require_strict_session_key", gate)
    monkeypatch.setattr(mcp_core, "_get", gateway)

    result = learn.memory_recall("memory_recall", {"query": "database"})

    gate.assert_called_once_with("Error: memory recall requires an established session")
    if identified:
        gateway.assert_called_once_with("/api/memory/recall?q=database", session_key=key)
        assert "member-alice" in result
    else:
        gateway.assert_not_called()
        assert result == refusal


# ---------------------------------------------------------------------------
# Class-level publisher guard
# ---------------------------------------------------------------------------
# The "missing X-Session-Key" HTTP 400 was a channel-turn *publisher* gap: a
# surface that runs an agent turn but never publishes the session_pid mapping
# leaves managed MCP tools (learn_add, cron management, ...) unable to resolve
# the caller's session identity. Telegram was the reported case; discord,
# slack, webex and wecom transport dispatch shared the exact same gap. The fix
# centralizes publication in messaging.identity.publish_turn_identity so every
# turn-running surface shares one writer. This guard DYNAMICALLY discovers all
# channel transport-dispatch surfaces (glob, not a hard-coded list) and fails
# if any of them — including a newly added channel — does not call the shared
# helper, so the class-level fix cannot silently regress one surface at a time.


def test_every_channel_transport_dispatch_publishes_identity() -> None:
    src = _src_root()
    dispatchers = sorted(src.glob("*/transport_dispatch.py"))
    assert dispatchers, (
        "no */transport_dispatch.py surfaces discovered — the channel dispatch "
        "layout changed; update this guard so it keeps covering every surface."
    )
    # A surface satisfies the contract either by calling the shared publisher
    # directly, or by delegating its turn to the shared pipeline
    # (messaging.dispatch.drive_turn), which publishes on the channel's behalf.
    # The delegation branch is only sound while the pipeline itself publishes,
    # so that is asserted first — otherwise "calls drive_turn" would become a
    # loophole that silently reintroduces the #232 gap for every adopter at once.
    pipeline = src / "messaging" / "dispatch.py"
    assert "publish_turn_identity" in pipeline.read_text(encoding="utf-8"), (
        "messaging/dispatch.py no longer publishes per-turn session identity. "
        "Every channel delegating to drive_turn depends on it, so removing the "
        "call reintroduces the #232 'missing X-Session-Key' gap for ALL of them."
    )
    missing = []
    for p in dispatchers:
        text = p.read_text(encoding="utf-8")
        if "publish_turn_identity" in text or "drive_turn" in text:
            continue
        missing.append(str(p.relative_to(src)))
    assert not missing, (
        "channel transport-dispatch surface(s) run a turn without publishing "
        f"per-turn session identity: {missing}. Every channel turn must call "
        "messaging.identity.publish_turn_identity — directly, or by delegating "
        "to messaging.dispatch.drive_turn — so managed MCP tools resolve "
        "X-Session-Key; otherwise they fail with HTTP 400 'missing "
        "X-Session-Key' from that channel (#232)."
    )
