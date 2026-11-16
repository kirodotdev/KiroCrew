"""Fixtures and helpers shared by test_denied_commands_security.py and its topic parts.

The topic parts are the ``test_denied_commands_security_<topic>.py`` modules that import
this one, split by behaviour. An autouse fixture applies only in a module that imports it
by name, so each of them imports ``_own_address_probe_stays_local``.
"""

from __future__ import annotations

import gc
import sys
import threading
from typing import Callable

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
def _own_address_probe_stays_local(_floor_monkeypatch):
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
    _floor_monkeypatch.setattr(_argv_floor.socket, "socket", _PacketlessProbeSocket)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
    _floor_monkeypatch.setattr(_argv_floor, "_hosts_file_paths", lambda: ())
    _floor_monkeypatch.setattr(_argv_floor, "_HOSTS_FILE_CACHE", {})
    _floor_monkeypatch.setattr(_argv_floor, "_HOSTS_WARM_IN_FLIGHT", False, raising=False)
    _floor_monkeypatch.setattr(
        _argv_floor, "_schedule_hosts_file_warm", lambda: None, raising=False
    )


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


def _interpreter_work(fn: "Callable[[], object]") -> int:
    """How many interpreter instructions this thread executes while ``fn`` runs.

    The deterministic replacement for a CPU clock around ``fn``. The count is fixed by
    what the code does, so the same code reads the same number bare, under coverage and
    on a loaded runner, where a clock reads the host. Bound its growth as a RATIO
    between input sizes. An absolute bound needs a wide margin: the number moves with
    the interpreter version and with every edit to the code under test.

    It sees every step taken in Python, whichever function takes it and however the loop
    is spelled. It cannot see work done inside one C call: a slice, ``in`` on a list,
    ``str.join`` and a regex search are each one instruction whatever their length.

    Only this thread is counted, and the cyclic collector is paused for the call, so
    neither another thread nor a finalizer the collector would run here is billed to
    ``fn``.
    """
    monitoring = sys.monitoring
    # Ids 3 and 4 are the two the interpreter reserves for no named tool.
    tool = next(i for i in (3, 4) if monitoring.get_tool(i) is None)
    this_thread = threading.get_ident()
    count = 0

    def on_instruction(_code: object, _offset: int) -> None:
        nonlocal count
        if threading.get_ident() == this_thread:
            count += 1

    monitoring.use_tool_id(tool, "interpreter-work")
    collecting = gc.isenabled()
    try:
        gc.disable()
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, on_instruction)
        monitoring.set_events(tool, monitoring.events.INSTRUCTION)
        fn()
    finally:
        monitoring.set_events(tool, monitoring.events.NO_EVENTS)
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, None)
        monitoring.free_tool_id(tool)
        if collecting:
            gc.enable()
    return count


def _linear_work(work: "list[int]") -> bool:
    """True when ``work`` at three equally spaced input sizes grows linearly.

    ``work`` is :func:`_interpreter_work` at sizes ``s``, ``2s`` and ``3s``. A linear
    cost adds the same work for each step of ``s``; a super-linear one adds more for the
    second step than for the first. The second step may cost at most a twentieth more
    than the first. The slack is not for the runner, which cannot move the count: it
    leaves room for a cost that is linear but not exactly so (a table that grows in
    chunks). Work that does not grow at all is refused too, since it means the measured
    call never reached the code under test.
    """
    small, middle, large = work
    return 0 < (middle - small) and (large - middle) * 20 <= (middle - small) * 21


def _assert_scan_is_linear(
    build: Callable[[int], str],
    sizes: "tuple[int, int, int]",
    *,
    denied: bool,
    denied_regexes: "list[str] | None" = None,
) -> None:
    """``is_denied(build(n))`` cannot stall the gate, asserted on counts, never a clock.

    A seconds budget read the runner, not the code: coverage's tracer multiplies the
    CPU of every traced line, so the same scan reads several times slower on an
    instrumented shard. The two costs that could stall the gate are asserted apart,
    since the walk is Python and the matching is the regex engine:

      1. THE ENGINE -- before any hostile input runs, every pattern a matcher hands
         the engine over the full command is free of both blow-ups: no exponential
         aws flag run (it was rewritten) and no top-level ``.*`` gap (it was split
         into fragments). Asserted first, so a regression fails here instead of
         never returning from the input below;
      2. THE WALK -- the interpreter instructions the call executes
         (``_interpreter_work``) grow linearly over three equally spaced sizes. That
         sees cost added anywhere in Python, inside or outside the matcher.

    The verdict is asserted at every size: ``denied`` when the input carries a
    needle, otherwise allowed, so the whole catalog was walked rather than cut short.
    """
    from kiro_crew.security import (
        _DANGEROUS_AWS_FLAG_RUN,
        _GIT_PUBLISH_RULE_PATTERNS,
        _deny_matcher,
        _split_deny_frags,
    )

    def verdict(command: str) -> "str | None":
        return is_denied(command, denied_regexes=denied_regexes)

    # The patterns this call hands the matchers: the built-ins, less the git-publish
    # ones ``is_denied`` decides in Python, plus any user regex.
    patterns = [
        *(r.pattern for r in BUILTIN_DENIED_RULES if r.pattern not in _GIT_PUBLISH_RULE_PATTERNS),
        *(denied_regexes or []),
    ]
    full_input = 0
    for pattern in patterns:
        matcher = _deny_matcher(pattern)
        if matcher._disabled or matcher._bounded:
            continue  # never run, or run only over a capped window
        full_input += 1
        for regex in matcher._frag_res:
            assert _DANGEROUS_AWS_FLAG_RUN not in regex.pattern, pattern
            assert _split_deny_frags(regex.pattern) == [regex.pattern], pattern
    assert full_input, "no matcher runs over the full command -- this checks nothing"
    # A benign command walks every rule, so first-call work is spent before
    # anything is measured.
    assert verdict("git status") is None

    def work_of(n: int) -> int:
        command = build(n)
        verdicts: "list[str | None]" = []
        work = _interpreter_work(lambda: verdicts.append(verdict(command)))
        assert (verdicts[0] is not None) is denied, f"{verdicts[0]!r} at size {n}"
        return work

    verdict(build(sizes[0] // 2))
    work = [work_of(n) for n in sizes]
    assert _linear_work(work), f"{work} instructions at sizes {sizes}"
