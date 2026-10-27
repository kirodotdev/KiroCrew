#!/usr/bin/env python3
"""A minimal stdio MCP server that records every launch.

Used by ``test_mcp_gateway_pool_integ.py`` as the REAL process gatewayd spawns
behind the pool. Recording one line per launch under the path named in
``argv[1]`` turns the question "how many backends did the pool actually create?"
into a line count -- a closed-box observation that needs no access to the pool's
private state, and therefore keeps working when that state is refactored. Every
consumer reads the lines back through :func:`recorded`, the only supported
reader: the on-disk layout is this module's own contract (see :func:`_record`),
so a test that opened the path itself would observe an empty history and read it
as a broken gateway.

Answers ``initialize``: the pool spawns a backend lazily on the first
non-register frame, so one ``initialize`` per stub is enough to force the
spawn-or-reuse decision the launch count is about.

Given an OPTIONAL second path argument, it also advertises
``kirocrew.caller-identity`` and records the ``sessionKey`` of every
``tools/call``'s caller block under that path -- turning "did gatewayd hand this
shared backend each session's own identity?" into another line read. Advertising
is bundled with recording deliberately: gatewayd injects only into a backend that
advertised, so a recorder that stayed silent would observe nothing and read as a
broken gateway. Without the argument the handshake is byte-for-byte what the
launch-counting tests already assert against.

An OPTIONAL third path argument records the per-CONNECTION ``nonce`` the same way,
which is the separate question of whether each connection got its OWN namespace --
the one that matters for a caller gatewayd cannot name, where there is no identity
to tell co-tenants apart by. Either path alone turns advertising on.

Stdlib only, and launched as ``sys.executable <this file> <log>`` -- never
through a shell and never via ``-c`` -- so no quoting or backslash assumption
travels onto Windows.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _record(log: str, line: str) -> None:
    """Append *line* to this PROCESS's own file under ``<log>.d/``.

    Never a shared append. ``open(path, "a")`` from several processes is atomic
    only on POSIX, where ``O_APPEND`` makes the seek-to-end and the write one
    step; the Windows CRT emulates append with a separate seek and write, so two
    backends writing at the same moment land at the same offset and one line is
    LOST. The observations here are exactly the clustered kind that hits: the
    storm test's backends all answer ``initialize`` after the same delay, and on
    Windows the coarse timer tick wakes them on the same clock edge, which is how
    a run counted 11 of 12 windows while all 12 stubs were answered.

    One file per pid, appended only by that pid, and :func:`recorded`
    concatenates. Closed immediately: a backend that lingers must not hold a
    handle the test reads, which on Windows would fail the read with a sharing
    violation.
    """
    directory = f"{log}.d"
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{os.getpid()}.txt"), "a", encoding="utf-8") as fh:
        fh.write(line)


def recorded(log: str | os.PathLike[str]) -> list[str]:
    """Every line recorded under *log*, concatenated across the per-pid files.

    The one supported reader of what :func:`_record` writes, and it lives beside
    it because the ``<log>.d/`` layout is this module's contract rather than any
    single test's: a consumer that opened *log* directly would find nothing there
    to read and would report a working gateway as a broken one. A missing
    directory means "no backend recorded anything yet", which is an observation
    the storm and pooling tests make on purpose, so it is the empty list and
    never an error.

    Within one pid the lines are in the order that pid wrote them. Across pids
    the order is the pid-file NAME order, which is not a launch order, so a
    caller that depends on ordering must first establish that a single backend
    wrote -- the identity tests assert a shared backend before reading identities
    for exactly that reason.
    """
    directory = f"{os.fspath(log)}.d"
    if not os.path.isdir(directory):
        return []
    out: list[str] = []
    for name in sorted(os.listdir(directory)):
        with open(os.path.join(directory, name), encoding="utf-8") as fh:
            out.extend(ln for ln in fh.read().splitlines() if ln.strip())
    return out


def _reply(msg: dict, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": result}) + "\n")
    sys.stdout.flush()


def _serve_subscription_frame(msg: dict, log: str) -> bool:
    """Answer one frame of the subscription mode; ``True`` when it was one.

    Turned on by ``FAKE_POOL_SUBSCRIBE_LOG``, which also makes the server
    advertise ``resources.subscribe`` and ``kirocrew.caller-identity``. It is the
    server-side half the resource-subscription tests observe from outside:

    * ``resources/subscribe`` and ``resources/unsubscribe`` are accepted and
      recorded as ``<method> <uri> <caller sessionKey>`` under *log*, so a test
      reads which grants reached the server and as which caller;
    * ``ping`` is answered, which is how a holder notices this process died;
    * a ``tools/call`` of ``emit_update`` with ``{"uri": ...}`` sends one
      ``notifications/resources/updated`` for that URI before its reply -- the
      scripted update. The server does not filter by subscription: routing the
      update only to subscribers is the gateway's job, which is what is tested.
    """
    method = msg.get("method")
    params = msg.get("params") or {}
    if method in ("resources/subscribe", "resources/unsubscribe"):
        block = (params.get("_meta") or {}).get("kirocrew.caller") or {}
        _record(
            log, f"{method.split('/')[1]} {params.get('uri', '')} {block.get('sessionKey', '')}\n"
        )
        _reply(msg, {})
        return True
    if method == "ping":
        _reply(msg, {})
        return True
    if method == "tools/call" and params.get("name") == "emit_update":
        uri = (params.get("arguments") or {}).get("uri", "")
        note = {
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": uri},
        }
        sys.stdout.write(json.dumps(note) + "\n")
        _reply(msg, {"content": []})
        return True
    return False


def main() -> int:
    log = sys.argv[1]
    # Optional second argument: a path to record the caller block each tools/call
    # arrived with, one line per call. Passing it also makes this server ADVERTISE
    # ``kirocrew.caller-identity`` -- gatewayd injects the block only into a
    # backend that advertised, so a recorder that did not advertise would observe
    # nothing and read as "injection is broken". Default off so the pooling tests
    # that only count launches keep the exact handshake they assert against.
    caller_log = sys.argv[2] if len(sys.argv) > 2 else ""
    # Optional third argument: a path to record the per-CONNECTION nonce each
    # tools/call arrived with, one line per call. Separate from ``caller_log`` so
    # the tests that assert on identities keep their exact line format: the nonce
    # is a namespace separator, present even when the identity is not, and the
    # question it answers ("did gatewayd give each connection its own?") is a
    # different one.
    tenant_log = sys.argv[3] if len(sys.argv) > 3 else ""

    # One line per process launch.
    _record(log, f"{os.getpid()}\n")

    # Optional slow handshake, for the admission tests: sleep this long before
    # answering ``initialize`` so spawn+initialize windows overlap and the
    # daemon's spawn gate has something to bound. ``FAKE_POOL_WINDOW_LOG`` then
    # records ``<pid> <launch_epoch> <init_answered_epoch>`` per process, from
    # which a test computes how many windows were ever open at once -- again a
    # closed-box observation, needing nothing from the pool's internals.
    # Optional subscription mode, for the resource-subscription tests: see
    # :func:`_serve_subscription_frame`. Off by default so every other test keeps
    # the exact handshake it asserts against.
    subscribe_log = os.environ.get("FAKE_POOL_SUBSCRIBE_LOG", "")
    init_delay = float(os.environ.get("FAKE_POOL_INIT_DELAY_SECS", "0") or 0)
    window_log = os.environ.get("FAKE_POOL_WINDOW_LOG", "")
    launched_at = time.time()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        if subscribe_log and _serve_subscription_frame(msg, subscribe_log):
            continue
        if method == "tools/call" and (caller_log or tenant_log):
            params = msg.get("params") or {}
            meta = params.get("_meta") or {}
            block = meta.get("kirocrew.caller") or {}
            tenant = meta.get("kirocrew.tenant") or {}
            # The empty string is a meaningful observation -- it is what a
            # backend sees when nothing injected -- so record it rather than
            # skipping the line.
            if caller_log:
                _record(caller_log, f"{block.get('sessionKey', '')}\n")
            if tenant_log:
                _record(tenant_log, f"{tenant.get('nonce', '')}\n")
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}) + "\n"
            )
            sys.stdout.flush()
            continue
        if method != "initialize":
            continue
        if init_delay > 0:
            time.sleep(init_delay)
        if window_log:
            _record(window_log, f"{os.getpid()} {launched_at:.6f} {time.time():.6f}\n")
        params = msg.get("params") or {}
        capabilities: dict = {"tools": {}}
        if caller_log or tenant_log or subscribe_log:
            capabilities["experimental"] = {"kirocrew.caller-identity": {"schemaVersion": 1}}
        if subscribe_log:
            capabilities["resources"] = {"subscribe": True}
        reply = {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": capabilities,
                "serverInfo": {"name": "fake-pool-mcp", "version": "1.0.0"},
            },
        }
        # Optional MCP ``instructions``, stamped with this process's pid so a test
        # can tell which backend produced the result a stub was replayed.
        instructions = os.environ.get("FAKE_POOL_INSTRUCTIONS", "")
        if instructions:
            reply["result"]["instructions"] = f"{instructions} pid={os.getpid()}"
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
