"""Identity-resolution topology tests (pre-work for the pid-namespace re-raise).

Background (2026-07-18 incident): the PID-namespace sandbox change (24c320f6,
reverted; this fork ported then reverted it in ab96394b) broke
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
``mcp_shared._resolve_excluded_tools``, ``mcp_gateway/stub.py``), each tested
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
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# The shared topology model
# ---------------------------------------------------------------------------

SESSION_KEY = "dashboard:chat-42-topofix"

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

    def write_session_pid(self, host_pid: int, session_key: str = SESSION_KEY) -> None:
        """The gateway-side contract: files keyed by HOST pid, always."""
        (self.cfg_dir / f"session_pid_{host_pid}.txt").write_text(session_key, encoding="utf-8")

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
    # Reset the fork's process-lifetime from_env cache: a previously-resolved
    # identity from an earlier test (or the host-view run of this test) would
    # otherwise short-circuit the walk and XPASS the strict pidns variants.
    monkeypatch.setattr("kiro_crew.mcp_caller._FROM_ENV_CACHE", None)


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
    from kiro_crew import mcp_core

    _wire_common(monkeypatch, topo, view)
    monkeypatch.setattr(mcp_core, "_get_ppid", topo.parent_lookup(view))
    monkeypatch.setattr(mcp_core, "config_dir", lambda: topo.cfg_dir)

    assert mcp_core._resolve_session_key() == SESSION_KEY


# ---------------------------------------------------------------------------
# Walk copy 3: the inline walk in mcp_shared._resolve_excluded_tools
# ---------------------------------------------------------------------------
# The policy session-key walk is inlined in ``_resolve_excluded_tools`` and
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

    cfg = MagicMock()
    cfg.dashboard.url = "http://localhost:5476/"
    monkeypatch.setattr(mcp_shared.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(mcp_shared, "parse_dashboard_url", lambda url: ("localhost", 5476))
    monkeypatch.setattr(mcp_shared, "config_dir", lambda: topo.cfg_dir)
    (topo.cfg_dir / ".local_secret").write_text("s")

    topo.write_session_pid(KIRO_CLI)  # direct parent of MCP_SERVER
    _wire_common(monkeypatch, topo, view)

    response = MagicMock()
    response.read.return_value = b'{"exclude": []}'
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)

    assert mcp_shared._resolve_excluded_tools() == set()
    # The walk must have RESOLVED a session key and reached the gateway —
    # under pidns it resolves empty and fail-opens without the call.
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
    from kiro_crew import mcp_core

    _wire_common(monkeypatch, topo, view)
    monkeypatch.setattr(mcp_core, "_get_ppid", topo.parent_lookup(view))
    monkeypatch.setattr(mcp_core, "config_dir", lambda: topo.cfg_dir)
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

    cfg = MagicMock()
    cfg.dashboard.url = "http://localhost:5476/"
    monkeypatch.setattr(mcp_shared.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(mcp_shared, "parse_dashboard_url", lambda url: ("localhost", 5476))
    monkeypatch.setattr(mcp_shared, "config_dir", lambda: topo.cfg_dir)
    (topo.cfg_dir / ".local_secret").write_text("s")

    # Symlink at the DIRECT parent's path (where the resolvable-case test
    # plants a real file) plus one at the fixture-written SESSION_HOST path,
    # so no ancestor resolves via a symlink.
    _plant_symlink(topo, SESSION_HOST)
    _plant_symlink(topo, KIRO_CLI)
    _wire_common(monkeypatch, topo, "host")

    urlopen = MagicMock()
    monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)

    # No key resolvable -> startup-race fail-open WITHOUT a policy call and,
    # crucially, WITHOUT the stolen key ever being read through the symlink.
    assert mcp_shared._resolve_excluded_tools() == set()
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
        "reader: client-side /proc ancestry walk — assumes HOST pids; .txt "
        "reads via session_pid_sig.read_session_pid_txt (hardened, unsigned)"
    ),
    "mcp_core.py": (
        "reader: lenient /proc ancestry walk + stale-file cleanup glob "
        "(assumes HOST pids), .txt reads via "
        "session_pid_sig.read_session_pid_txt (hardened, unsigned); STRICT "
        "path delegates to session_pid_sig.verify_session_pid "
        "(HMAC-verified, direct KIROCREW_HOST_PID lookup, no walk)"
    ),
    "mcp_shared.py": (
        "reader: policy session-key /proc ancestry walk inline in "
        "_resolve_excluded_tools — assumes HOST pids; .txt reads via "
        "session_pid_sig.read_session_pid_txt (hardened, unsigned)"
    ),
    "mcp_gateway/stub.py": "reader via CallerContext.from_env; register-time caller block — assumes HOST pids",
    "dashboard/handlers/messaging.py": (
        "reader (STRICT): api_browser_frame resolves the browse-mirror session "
        "key from the posting Playwright proxy's host_pid by walking process "
        "ancestry and calling session_pid_sig.verify_session_pid (HMAC-verified) "
        "on each ancestor — HOST-pid-keyed, no unsigned .txt read"
    ),
    "mcp_gateway/gatewayd.py": (
        "(_resolve_peer_identity) — runs in gatewayd's own (host) pid namespace, "
        "so it is immune to client-side namespace divergence; also indexes the "
        "host ancestor chain for claim-push matching; .txt reads via "
        "session_pid_sig.read_session_pid_txt (hardened, unsigned)"
    ),
    "sandbox.py": (
        "writer-adjacent: launcher exports KIROCREW_HOST_PID (its own HOST pid — "
        "the exact pid the gateway keys the file by) before fork/namespace work, "
        "so in-namespace readers can look the file up directly without a /proc walk"
    ),
    "mcp_gateway/claim.py": "docstring reference to the contract (no code reads)",
    "session_pid.py": "stale-file cleanup: globs session_pid_*.txt (+ .sig sidecars) for dead processes",
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
# Class-level publisher guard (#232)
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
