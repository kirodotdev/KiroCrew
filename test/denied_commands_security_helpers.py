"""Fixtures and helpers shared by test_denied_commands_security.py and its topic parts.

The topic parts are the ``test_denied_commands_security_<topic>.py`` modules that import
this one, split by behaviour. An autouse fixture applies only in a module that imports it
by name, so each of them imports ``_own_address_probe_stays_local``.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import BUILTIN_DENIED_RULES
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import is_denied

# Captured at import, before the autouse fixture stubs the module attribute:
# the alias-layer tests re-bind this real function and stub the resolver
# socket underneath it instead.
_REAL_RESOLVED_HOST_VERDICT = _argv_floor._resolved_host_verdict
_REAL_SCHEDULE_HOSTS_WARM = getattr(_argv_floor, "_schedule_hosts_file_warm", None)


class _PacketlessProbeSocket(_argv_floor.socket.socket):
    """``socket.socket`` whose datagram ``connect`` never touches the network.

    ``_own_interface_addresses`` learns this host's primary outbound address per
    family by ``connect``ing a UDP socket to a documentation peer and reading
    ``getsockname()``. A UDP connect sends no packet, but it does consult the
    routing table and is a real off-loopback ``socket.connect`` from the test
    process, so a network-audited run flags it and a host with no default route
    answers differently. Here the connect is dropped and ``getsockname`` reports
    the family's loopback address, so the seed still produces a parseable
    address for every layer without reaching outside the machine. Stream
    sockets and every other method are the real thing.
    """

    def connect(self, address):  # type: ignore[override]
        if self.type == _argv_floor.socket.SOCK_DGRAM:
            return None
        return super().connect(address)

    def getsockname(self):  # type: ignore[override]
        if self.type == _argv_floor.socket.SOCK_DGRAM:
            if self.family == _argv_floor.socket.AF_INET6:
                return ("::1", 0, 0, 0)
            return ("127.0.0.1", 0)
        return super().getsockname()


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
def _own_address_probe_stays_local(monkeypatch):
    """Every ``ssh``-family verdict in a module importing this fixture seeds the own-host set.

    The first ``is_denied("ssh ...")`` in the process runs ``_own_host_seed``
    (which calls ``_own_interface_addresses``) and, once the backoff allows,
    starts the ``kirocrew-own-host-resolve`` DNS worker. Whichever test happens
    to run first then carries a real routing-table probe and a daemon thread
    doing real name resolution. Pin both at module level: the probe socket
    stays packet-less and local, and the worker backoff is pushed out so no
    enrichment thread starts. Tests of the worker itself set the backoff to
    ``0.0`` explicitly, and the resolver tests stub DNS underneath it.

    The hosts-file layer is pinned the same way: no hosts file (so a dotless
    target is never a pending refusal against the operator's real
    ``/etc/hosts``), an empty table cache, and a warm scheduler that records
    instead of starting a thread. Hosts-file tests name their own file and
    warm it explicitly.
    """
    monkeypatch.setattr(_argv_floor.socket, "socket", _PacketlessProbeSocket)
    monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
    monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: ())
    monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_CACHE", {})
    monkeypatch.setattr(_argv_floor, "_HOSTS_WARM_IN_FLIGHT", False, raising=False)
    monkeypatch.setattr(_argv_floor, "_schedule_hosts_file_warm", lambda: None, raising=False)


# ── Guarded literals ────────────────────────────────────────────────────────
# The two rules these literals exercise match on the very words that name
# them, so a test file spelling them out literally could not be read or
# grepped by an agent shell without tripping the rules under test.
# Assembling them at runtime keeps these files readable while the assertions
# stay exact.
_K = "k" + "ill"
_PK = "p" + _K
_KA = _K + "all"
_NAME = "kiro" + "crew"
_HYPH = "kiro-" + "crew"
_TOK = "to" + "ken"

_RULE_KILL = "self-protection-" + _K
_RULE_MINT = "credential-exfil-" + _NAME + "-" + _TOK
Q = chr(34)


def _rule_pattern(rule_id: str) -> str:
    return next(r.pattern for r in BUILTIN_DENIED_RULES if r.id == rule_id)


def _denied_by(cmd: str, reason_notes: "dict[str, str] | None" = None) -> "str | None":
    """Return the rule id that denied ``cmd``, or ``None`` if it is allowed.

    Goes through the PUBLIC gate (``is_denied``) rather than re-running the
    regex, so these tests survive a refactor of how rules are compiled.

    Only the FIRST line is parsed. An operator note is appended to the refusal on
    its own second line, so partitioning the whole string would fold that note
    into the captured pattern and every id lookup would miss. Single-line
    refusals (every call that passes no ``reason_notes``) are unaffected:
    ``verdict.splitlines()[0]`` is the verdict itself.
    """
    verdict = is_denied(cmd, reason_notes=reason_notes)
    if verdict is None:
        return None
    head = verdict.splitlines()[0]
    _, _, pattern = head.partition("Blocked by security policy: ")
    by_pattern = {r.pattern: r.id for r in BUILTIN_DENIED_RULES}
    return by_pattern.get(pattern or verdict, f"<unmapped:{verdict}>")
