"""What the MCP servers of the open sessions cost this host now, and with sharing.

The MCP page shows two numbers: the memory the MCP servers of every open
session use NOW, and what they would use WITH SHARING. Both are read from this
host's process table. Nothing is estimated from a constant: when any process
the figure needs has no reading, the figure is ``None`` and the page shows no
number at all.

The unit is PSS (``smaps_rollup``), not RSS. RSS counts the code pages that
copies of one program share once per process, so summing it overstates what
the copies cost; PSS charges each shared page to its sharers proportionally.

What is measured, per session runtime (the pid the session manager records),
walking its process tree once:

* A **copy** is a process that runs one of a server's declared launches -- the
  basename of its ``argv[0]`` (or ``argv[1]``, for a script run by an
  interpreter) equal to the declared command's basename, followed by the
  declared args -- together with every process below it. Matching inside one
  session's own tree is what makes the basename safe: nothing outside the tree
  is ever counted. ``npx`` re-titles itself ``npm exec``; that spelling is
  matched too, ignoring the ``-y``/``--yes`` flag on either side.
* A **stub** is a ``kiro_crew.mcp_gateway.stub`` process. A stub with no child
  attaches to a shared backend, so it costs only itself. A stub WITH children
  runs its server below it inside the session (a stubbed server while sharing
  is off), so it is a copy of that server, stub included.

Shared backends are the gateway pool's own processes, measured by the pid the
pool reports.

``with_sharing`` replaces, for each server that can share, every per-session
copy by the one shared backend already running (or, with none running, by one
copy of the mean measured size), plus one stub per session that had a copy, at
the mean PSS of the stubs running now. With copies to replace and no stub
running to measure, there is no reading of a stub, so ``with_sharing`` is
``None``.

Linux only. BLOCKING (one ``/proc`` pass plus one ``smaps_rollup`` read per
counted process), so call it off the event loop.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.platform_compat_metrics import _proc_pss_kb

#: Flags ``npx`` accepts that do not change what it runs.
_NPX_NOISE = frozenset({"-y", "--yes"})


@dataclass(frozen=True)
class ServerSpec:
    """What the page knows about one server, from the agent specs."""

    launches: tuple[tuple[str, ...], ...]
    #: True when the server can run as one shared copy (stdio, not denylisted,
    #: no declared env a shared backend would withhold).
    shareable: bool
    #: True when it IS shared for new sessions now (marked, sharing on, launch
    #: approved). A session still running its own copy of it is a stale chat.
    shared_now: bool = False
    #: False when its declarations differ in what makes a pool identity beyond
    #: argv (a different declared env block): sharing it would run more than
    #: one backend, which this reading cannot size, so no forecast is made.
    one_identity: bool = True


@dataclass
class Footprint:
    """One reading of the MCP processes of every open session."""

    using_now_bytes: int | None
    with_sharing_bytes: int | None
    #: Distinct servers seen running (as copies, stubs or shared backends).
    servers: int
    #: Session keys whose tree holds its own copy of a server shared now.
    stale_session_keys: list[str] = field(default_factory=list)


def _argv(pid: int, proc_root: Path | None) -> list[str]:
    from kiro_crew.session_pid_harness import _pid_cmdline

    parts = [os.fsdecode(part) for part in _pid_cmdline(pid, proc_root).split(b"\0") if part]
    # A process that set its own title (npm does: ``npm exec @scope/pkg``)
    # leaves ONE field with spaces where argv was; split it back into words.
    if len(parts) == 1 and " " in parts[0]:
        return parts[0].split()
    return parts


def _stub_server(argv: Sequence[str]) -> str | None:
    """The ``--server`` a stub's argv names, or None when it cannot be read."""
    try:
        flags = expand_stub_flags(list(argv))
    except ValueError:
        return None
    for i, token in enumerate(flags):
        if token == "--server" and i + 1 < len(flags):
            return str(flags[i + 1])
        if isinstance(token, str) and token.startswith("--server="):
            return token.partition("=")[2]
    return None


def _strip_npx(args: Sequence[str]) -> list[str]:
    return [a for a in args if a not in _NPX_NOISE]


def _runs(argv: Sequence[str], launch: Sequence[str]) -> bool:
    """Whether *argv* runs *launch* (``command, *args``)."""
    command, *args = launch
    base = os.path.basename(command)
    for start in (0, 1):
        if len(argv) <= start:
            break
        if (
            os.path.basename(argv[start]) == base
            and list(argv[start + 1 : start + 1 + len(args)]) == args
        ):
            return True
    # ``npx`` sets its process title to ``npm exec <args>``.
    if (
        base == "npx"
        and len(argv) >= 2
        and os.path.basename(argv[0]) == "npm"
        and argv[1] == "exec"
    ):
        return _strip_npx(argv[2:])[: len(_strip_npx(args))] == _strip_npx(args)
    return False


# ``_match``'s answer for a process two servers tie on: it is an MCP copy, but
# whose is unknown, so neither figure can be honest.
_AMBIGUOUS: tuple[str, tuple[str, ...]] = ("", ())


def _match(
    argv: Sequence[str], specs: Mapping[str, ServerSpec]
) -> tuple[str, tuple[str, ...]] | None:
    """The server and the declared launch *argv* runs, or None.

    One launch's args may be a prefix of another's, within one server or
    across two, so the longest matching launch over every server wins. Two
    servers tying on it return ``_AMBIGUOUS``, never None: the process is an
    MCP copy, and treating it as a non-MCP process would undercount.
    """
    if not argv or any("kiro_crew.mcp_gateway" in part for part in argv):
        return None
    best: list[tuple[str, tuple[str, ...]]] = []
    for name, spec in specs.items():
        for launch in spec.launches:
            if not launch or not _runs(argv, launch):
                continue
            if not best or len(launch) > len(best[0][1]):
                best = [(name, tuple(launch))]
            elif len(launch) == len(best[0][1]):
                best.append((name, tuple(launch)))
    if not best:
        return None
    if len({name for name, _ in best}) > 1:
        return _AMBIGUOUS
    return best[0]


def measure(
    sessions: Iterable[tuple[str, int]],
    specs: Mapping[str, ServerSpec],
    backends: Iterable[tuple[str, int]] | None = (),
    *,
    proc_root: Path | None = None,
) -> Footprint | None:
    """Measure the MCP processes of *sessions* (``(session_key, runtime_pid)``).

    Several keys may name one runtime pid (a multiplexed runtime); its tree is
    walked once and every key on it is marked stale together. *backends* is
    ``(server, pid)`` per shared backend the pool reports, or ``None`` when the
    pool could not be asked: the shared backends are then unread, so both byte
    figures are ``None``. Returns ``None`` when the process table cannot be
    read at all (off Linux, ``/proc`` unlistable).
    """
    children = platform_compat.proc_child_map(proc_root=proc_root)
    if children is None:
        return None
    # One name space: a stub's ``--server`` and the pool's backend rows carry
    # the slash-free alias, so every key here is read through it too.
    specs = {mcp_server_alias(name): spec for name, spec in specs.items()}

    keys_by_pid: dict[int, list[str]] = {}
    for key, pid in sessions:
        keys_by_pid.setdefault(int(pid), []).append(key)

    def tree(root: int) -> list[int]:
        out, stack, seen = [], [root], set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            out.append(pid)
            stack.extend(children.get(pid, ()))
        return out

    readable = True

    def pss(pids: Iterable[int]) -> int:
        nonlocal readable
        total = 0
        for pid in pids:
            kb = _proc_pss_kb(pid, proc_root=proc_root)
            if kb < 0:
                readable = False
                continue
            total += kb * 1024
        return total

    # server -> [(keys, bytes)] for per-session copies; stubs attached to a
    # shared backend; shared backends.
    copies: dict[str, list[tuple[list[str], int]]] = {}
    # The distinct declared launches each server's copies run. Two launches
    # of one name pool as two backends (the pool key hashes the args), so a
    # forecast that budgets one copy per NAME would be too low.
    launches_seen: dict[str, set[tuple[str, ...]]] = {}
    stub_bytes: list[int] = []
    seen_servers: set[str] = set()

    for root, keys in keys_by_pid.items():
        stack = list(children.get(root, ()))
        visited: set[int] = set()
        while stack:
            pid = stack.pop()
            if pid in visited:
                continue
            visited.add(pid)
            argv = _argv(pid, proc_root)
            if any(STUB_MODULE in part for part in argv):
                server = _stub_server(argv)
                server = mcp_server_alias(server) if server is not None else None
                below = tree(pid)
                if server is not None:
                    seen_servers.add(server)
                if len(below) > 1 and server is not None:
                    copies.setdefault(server, []).append((keys, pss(below)))
                else:
                    stub_bytes.append(pss([pid]))
                continue
            hit = _match(argv, specs)
            if hit is _AMBIGUOUS:
                # An MCP copy no one server owns: no figure, no stale chat.
                readable = False
                continue
            if hit is not None:
                server, launch = hit
                launches_seen.setdefault(server, set()).add(launch)
                seen_servers.add(server)
                copies.setdefault(server, []).append((keys, pss(tree(pid))))
                continue
            stack.extend(children.get(pid, ()))

    backend_bytes: dict[str, int] = {}
    if backends is None:
        readable = False
        backends = ()
    for server, pid in backends:
        if pid is None:
            continue
        server = mcp_server_alias(server)
        seen_servers.add(server)
        backend_bytes[server] = backend_bytes.get(server, 0) + pss(tree(int(pid)))

    stale: list[str] = []
    for server, rows in copies.items():
        spec = specs.get(server)
        if spec is not None and spec.shared_now:
            for keys, _ in rows:
                stale.extend(k for k in keys if k not in stale)

    if not readable:
        return Footprint(None, None, len(seen_servers), stale)

    stubs_now = sum(stub_bytes)
    backends_now = sum(backend_bytes.values())
    copies_now = sum(b for rows in copies.values() for _, b in rows)
    using_now = copies_now + stubs_now + backends_now

    mean_stub = stubs_now // len(stub_bytes) if stub_bytes else None
    with_sharing = stubs_now + backends_now
    for server, rows in copies.items():
        spec = specs.get(server)
        if spec is None or not spec.shareable:
            with_sharing += sum(b for _, b in rows)
            continue
        # More than one DECLARED launch pools as more than one backend, and a
        # backend row names only its server, so it cannot be matched to the
        # launch it serves: no forecast for that server.
        if mean_stub is None or len(spec.launches) > 1 or not spec.one_identity:
            # No stub to measure, or copies of more than one launch whose
            # backends this reading cannot tell apart: no forecast.
            return Footprint(using_now, None, len(seen_servers), stale)
        if server not in backend_bytes:
            with_sharing += sum(b for _, b in rows) // len(rows)
        with_sharing += len(rows) * mean_stub
    return Footprint(using_now, with_sharing, len(seen_servers), stale)
