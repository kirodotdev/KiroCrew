"""Codex's spec projection: the agent spec -> a codex ``session/new`` array.

``providers/mirrors/codex.py`` reuses claude's translation and adds two rules of
its own. Both are properties of the ADAPTER rather than of Crew, so both are
measured against a real ``codex-acp`` here and asserted as unit behaviour in the
files named below:

* an ``sse`` element is dropped, because codex-acp ACCEPTS one silently and never
  wires it -- so forwarding it buys a session whose tool is missing with no error
  anywhere, and this filter is the only guard against that;
* Crew's OWN servers carry ``KIROCREW_SESSION_KEY`` on the element, because
  ``codex-rs`` launches a stdio MCP server with ``env_clear()`` plus a fixed
  allowlist and inherits nothing else -- and which servers those are is decided by
  the resolved invocation, never by the name, since that key authenticates a
  session-directive claim and the agent spec is hand-editable.

``test_real_codex_acp_accepts_the_crew_stdio_element`` is the anti-drift guard for
both: an adapter fact a projection depends on is measured against the adapter, and
this is where the measurement lives.

This file holds those measurements against an installed ``codex-acp``, and it is the
file the Real Adapter Contract Tests lane collects. The unit behaviour lives beside it:
``test_codex_session_mcp_projection.py`` holds the projection and the session array it
hands the adapter, ``test_codex_session_mcp_tool_refusal.py`` holds where a tool the
spec switched off is refused, and both build on the fixtures in
``codex_session_mcp_helpers.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from real_adapter_gate import MEASURED_CODEX_ACP_VERSION, require_real_adapter

from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.providers.mirrors.codex import (
    codex_name,
    drop_unadvertised_transports,
)

# ── the real adapter ────────────────────────────────────────────────────────

# The driver runs OUT OF PROCESS on purpose: it spawns a real Node adapter, and a
# stalled readline in the test process would surface as a pytest timeout kill
# rather than the clean assertion failures below.
_DRIVER = r"""
import json, os, queue, subprocess, sys, threading, time

root, entry, stub, node = sys.argv[1:5]
report = os.path.join(root, "report.json")
env = dict(os.environ)
env["CODEX_HOME"] = os.path.join(root, "codex_home")
env["NO_BROWSER"] = "1"


def reap(p):
    # Every exit from drive() runs this, including an exception and the outer
    # timeout's SIGTERM: a bare readline() on an adapter that has stopped writing
    # blocks forever, and an adapter left running keeps its own MCP child alive
    # after this driver is gone. Nothing else on the machine knows to kill them.
    for step in (p.terminate, p.kill):
        try:
            step()
            p.communicate(timeout=15)
            return
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            return


def pump(stream, q):
    for line in stream:
        q.put(line)
    q.put(None)


def drive(element):
    p = subprocess.Popen(
        [node, entry], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd=os.path.join(root, "work"), env=env,
        text=True, bufsize=1,
    )
    try:
        # Read on a daemon thread so the deadline below is real. The main loop
        # never blocks on the adapter, so a stalled one costs 90s, not the turn.
        q = queue.Queue()
        threading.Thread(target=pump, args=(p.stdout, q), daemon=True).start()

        def send(o):
            p.stdin.write(json.dumps(o) + "\n")
            p.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": 1, "clientCapabilities": {"fs": {}}}})
        send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
              "params": {"cwd": os.path.join(root, "work"), "mcpServers": [element]}})
        got, deadline = {}, time.time() + 90
        while 2 not in got:
            budget = deadline - time.time()
            if budget <= 0:
                break
            try:
                line = q.get(timeout=budget)
            except queue.Empty:
                break
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg.get("id"), int):
                got[msg["id"]] = msg
        if 2 in got and "result" in got[2]:
            # The child MCP server is launched and tools/list is queried after session/new answers.
            for _ in range(120):
                try:
                    with open(report) as f:
                        if "tools/list" in json.load(f).get("methods", []):
                            break
                except (OSError, ValueError):
                    pass
                time.sleep(0.25)
        return got.get(1) or {}, got.get(2) or {}
    finally:
        reap(p)


stdio_el = {
    "name": "kirocrew-core", "type": "stdio", "command": sys.executable,
    "args": [stub],
    "env": [{"name": "STUB_MCP_REPORT", "value": report},
            {"name": "KIROCREW_SESSION_KEY", "value": "probe-session-key"}],
}
init, new = drive(stdio_el)
out = {
    "mcp_capabilities": (init.get("result") or {}).get("agentCapabilities", {}).get(
        "mcpCapabilities"),
    "stdio_error": new.get("error"),
    "stdio_ok": bool(new.get("result")),
    "child": json.load(open(report)) if os.path.exists(report) else None,
}
if os.path.exists(report):
    os.unlink(report)
_, sse = drive({"name": "remote", "type": "sse", "url": "http://127.0.0.1:1/sse",
                "headers": []})
out["sse_error"] = sse.get("error")
out["sse_ok"] = bool(sse.get("result"))
# The same transport WITHOUT the schema-required `headers` array. The adapter's
# answer to an unadvertised transport depends on whether the element parses as
# that transport at all: a complete sse element is named and refused, an
# incomplete one falls to the untagged variant and is accepted unwired.
_, sse_bare = drive({"name": "remote-bare", "type": "sse", "url": "http://127.0.0.1:1/sse"})
out["sse_bare_error"] = sse_bare.get("error")
out["sse_bare_ok"] = bool(sse_bare.get("result"))
# The CONTROL for the line above: a type no schema can name. If the adapter answers
# this one normally too, it is validating the transport tag not at all, which is what
# makes client-side narrowing the only guard rather than a second opinion.
_, junk = drive({"name": "junk", "type": "nonsense-type", "url": "http://127.0.0.1:1/j"})
out["unknown_type_error"] = junk.get("error")
out["unknown_type_ok"] = bool(junk.get("result"))
_, bad = drive({"name": "no-command", "args": [], "env": []})
out["malformed_error"] = bad.get("error")
out["malformed_ok"] = bool(bad.get("result"))

# FOLD MEASUREMENT. codex registers by name with `insert`, so a collision is
# observable as a child that never launches. "probe  one" (TWO spaces) folds to
# "probe__one" under codex's per-character rule and to "probe_one" under a
# collapsing one -- so pairing it with a literal "probe__one" discriminates them:
# per-character means one slot and ONE report file, collapsing means two.
fold_a = os.path.join(root, "fold-a.json")
fold_b = os.path.join(root, "fold-b.json")
for path in (fold_a, fold_b):
    if os.path.exists(path):
        os.unlink(path)


def stub_named(name, report):
    return {
        "name": name, "type": "stdio", "command": sys.executable, "args": [stub],
        "env": [{"name": "STUB_MCP_REPORT", "value": report}],
    }


p_fold = subprocess.Popen(
    [node, entry], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, cwd=os.path.join(root, "work"), env=env, text=True, bufsize=1,
)
try:
    def send_fold(o):
        p_fold.stdin.write(json.dumps(o) + "\n")
        p_fold.stdin.flush()

    send_fold({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": 1, "clientCapabilities": {"fs": {}}}})
    send_fold({"jsonrpc": "2.0", "id": 2, "method": "session/new",
               "params": {"cwd": os.path.join(root, "work"), "mcpServers": [
                   stub_named("probe  one", fold_a), stub_named("probe__one", fold_b)]}})
    got_fold, deadline = {}, time.time() + 90
    q_fold = queue.Queue()
    threading.Thread(target=pump, args=(p_fold.stdout, q_fold), daemon=True).start()
    while 2 not in got_fold:
        budget = deadline - time.time()
        if budget <= 0:
            break
        try:
            line = q_fold.get(timeout=budget)
        except queue.Empty:
            break
        if line is None:
            break
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg.get("id"), int):
            got_fold[msg["id"]] = msg
    for _ in range(60):
        if os.path.exists(fold_a) or os.path.exists(fold_b):
            break
        time.sleep(0.25)
    time.sleep(4)
finally:
    reap(p_fold)
out["fold_session_ok"] = bool((got_fold.get(2) or {}).get("result"))
out["fold_children"] = sorted(
    n for n, pth in (("probe  one", fold_a), ("probe__one", fold_b)) if os.path.exists(pth)
)
print(json.dumps(out))
"""

# A stdio MCP server small enough to read: it records the environment it was
# LAUNCHED with (which is the measurement) and answers the two methods codex sends.
_STUB_MCP = r"""
import json, os, sys

REPORT = os.environ["STUB_MCP_REPORT"]
seen = []


def dump():
    tmp = REPORT + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"methods": seen, "env": dict(os.environ)}, fh)
    os.replace(tmp, REPORT)


def send(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    seen.append(msg.get("method") or "")
    dump()
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub", "version": "0"}}})
    elif msg.get("method") == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
            {"name": "stub_echo", "description": "echo",
             "inputSchema": {"type": "object", "properties": {}}}]}})
    else:
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
"""


def _run_driver_reaping_group(
    argv: list[str], *, timeout: float, cwd: "str | os.PathLike[str]"
) -> "subprocess.CompletedProcess[str]":
    """Run the out-of-process driver in its OWN process group and reap the group.

    ``subprocess.run(timeout=...)`` kills the driver and nothing else. The driver
    spawns codex-acp, and codex-acp spawns the MCP child the element names, so on
    the timeout path the driver's own ``finally`` never runs and two generations of
    descendants outlive the test with nothing on the machine that knows to end them
    -- a leaked Node adapter holding a port and a Python MCP server holding a temp
    directory, degrading whatever runs next.

    ``start_new_session`` makes the driver a session/group leader, so ONE tree kill
    reaches every descendant that has not left the group. The kill runs on every
    exit rather than only after a timeout: a driver that reaped cleanly leaves an
    empty group and the kill is a no-op, which is cheaper and more reliable than
    deciding case by case whether it was needed.

    Routed through ``platform_compat.kill_process_tree`` -- ``killpg`` on POSIX,
    ``taskkill /T`` on Windows, with the broadcast guard that keeps a reserved pgid
    from signalling every process this uid owns. A raw ``os.killpg`` here would be
    POSIX-only and unguarded.

    ``cwd`` is required, not defaulted: a child inherits pytest's CWD (the
    checkout) unless told otherwise, and every caller already owns a throwaway
    directory the driver tree can run from.
    """
    from kiro_crew import platform_compat

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )

    def reap() -> None:
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except Exception:
            # Already gone is the expected case on the happy path.
            pass

    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            reap()
            # Bounded: the group is SIGKILLed, so the only thing left to do is
            # drain pipes no live writer holds. A second stall would be a kernel
            # problem, and swallowing the output beats hanging the suite.
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                out, err = "", ""
    finally:
        reap()
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _codex_acp_entry() -> Path | None:
    """The installed adapter's entry script, through the SPAWN's own resolver.

    Asking ``_resolve_codex_acp_bin`` rather than ``shutil.which`` is deliberate:
    what this test must exercise is the adapter a real session would spawn, on the
    same ladder (``CODEX_ACP_BIN``, project ``node_modules``, mise, PATH).
    """
    from kiro_crew.acp.client import _resolve_codex_acp_bin

    argv, _search = _resolve_codex_acp_bin()
    if not argv:
        return None
    return Path(argv[-1])


_ENTRY = _codex_acp_entry()


def _require_codex_acp(*, with_node: bool = False) -> None:
    """Gate a live measurement on the adapter, without a skip a lane can hide behind.

    Absent locally, the test skips as a ``skipif`` did. Absent where the job
    declared the adapters must be present (``KIROCREW_E2E_REQUIRE=1``, the lane that
    installs the pinned one to run these), it FAILS -- a lane whose only guard
    skipped reports success having measured nothing. ``real_adapter_gate`` carries
    the pinned version, so the release installed and the release these assertions
    were measured against are one string.
    """
    require_real_adapter(
        _ENTRY,
        what="codex-acp",
        install=f"npm i -g @agentclientprotocol/codex-acp@{MEASURED_CODEX_ACP_VERSION}",
    )
    if with_node:
        require_real_adapter(
            shutil.which("node"), what="node", install="install Node 24 and put it on PATH"
        )


@pytest.mark.skipif(not hasattr(os, "getpgid"), reason="POSIX process groups only")
def test_the_driver_runner_reaps_descendants_on_the_timeout_path():
    """The leak the outer bound exists to prevent, driven end to end.

    ``subprocess.run(timeout=...)`` kills the driver alone. The real driver spawns
    codex-acp, which spawns the MCP child -- so on the timeout path a plain
    ``run()`` leaves a Node adapter and a Python MCP server alive with nothing on
    the machine that knows to end them. Stood in for here by a parent that spawns a
    sleeping grandchild and then hangs: the shape is the same and it costs seconds
    instead of a Node install.

    Asserted on the GRANDCHILD, because a parent-only kill is the bug -- the parent
    dies either way.

    The grandchild is known here only as a NUMBER the driver printed, and by the
    time this process reads it the driver's group has been SIGKILLed: on the
    passing path init has already collected the grandchild and the kernel is free
    to hand its number to a stranger. So the driver reports the grandchild's
    start-time identity alongside the pid, read through the repo's own helper at
    the one moment the pid is provably ours, and "gone" below means "no process
    with THAT identity" -- a reissued number is a stranger, neither a leak nor a
    kill target. The failure-path kill is pinned to the same identity.
    """
    from kiro_crew import platform_compat

    src_root = Path(platform_compat.__file__).resolve().parents[1]
    parent = (
        "import subprocess, sys, time\n"
        f"sys.path.insert(0, {str(src_root)!r})\n"
        "from kiro_crew.platform_compat import get_process_start_id\n"
        'g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])\n'
        "print(g.pid, get_process_start_id(g.pid) or '', flush=True)\n"
        "time.sleep(300)\n"
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        script = Path(w) / "parent.py"
        script.write_text(parent, encoding="utf-8")
        started = time.monotonic()
        result = _run_driver_reaping_group([sys.executable, str(script)], timeout=5, cwd=w)
        # The bound is the control here, and the reap must not add a long second wait.
        assert time.monotonic() - started < 90
        reported = (result.stdout or "").strip().splitlines()[0].split()
        grandchild = int(reported[0])
        assert len(reported) == 2, (
            f"the driver could not read the start-time identity of grandchild "
            f"{grandchild}, so neither the liveness check nor the failure-path kill "
            "below could be pinned to the process it spawned"
        )
        grandchild_identity = reported[1]

    # Liveness through the repo's own identity helper (AGENTS.md "Cross-platform"):
    # a raw ``os.kill(pid, 0)`` is a POSIX idiom that TERMINATES the target on
    # Windows, the sweep's caller filter recognises only the sanctioned helpers,
    # and a bare existence probe cannot tell our reaped grandchild's reissued
    # number from the grandchild itself.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if platform_compat.get_process_start_id(grandchild) != grandchild_identity:
            break
        time.sleep(0.2)
    else:  # pragma: no cover - the failure this test exists to catch
        try:
            platform_compat.kill_pid_pinned(
                grandchild, grandchild_identity, platform_compat.SIGKILL
            )
        except OSError:
            pass
        pytest.fail(
            f"pid {grandchild} outlived the driver's timeout: the runner killed the "
            "driver but not its process group, so a real run leaks codex-acp and "
            "the MCP child codex-acp itself spawned"
        )


@pytest.mark.real_adapter
def test_real_codex_acp_accepts_the_crew_stdio_element():
    """ANTI-DRIFT GUARD, and the measurement the old docstring lacked.

    Four facts, all of which the projection depends on and none of which is
    documented by the adapter:

    1. The element Crew already emits -- ``{"name", "command", "args", "env",
       "type": "stdio"}``, the claude shape unchanged -- is ACCEPTED. ACP v1 spells
       ``McpServer`` as ``serde(tag = "type")`` with stdio as the untagged
       fallback, so nothing guaranteed a ``"stdio"`` tag would fall through to it.
    2. The server is really LAUNCHED and its tools listed: the child answers
       ``initialize`` and then ``tools/list``.
    3. The child inherits ALMOST NOTHING. ``codex-rs`` runs ``env_clear()`` and
       re-adds an allowlist, so ``KIROCREW_SESSION_KEY`` arrives only because the
       element carried it -- which is why ``codex_elements`` carries it.
    4. The elements of the array decide whether ``session/new`` survives, and the
       adapter is not consistent about which ones. A meaningless
       ``{"type": "nonsense-type"}`` element is ACCEPTED -- an ordinary
       ``sessionId``, that server silently unwired -- and so is a MALFORMED stdio
       element, answered with the element dropped. An ``sse`` element is the one
       the adapter may instead name and refuse, failing the whole session. Both
       dispositions are measured rather than assumed, because each makes the
       client-side narrowing load-bearing on its own: an accepted element is a
       server no error reports missing, and a refused one is a whole session lost
       over one spec entry. What is NOT admitted is a third answer, where the
       adapter reports having wired a transport its own ``mcpCapabilities`` denies.

    A fabricated API key in a throwaway ``CODEX_HOME`` is what gets past the
    adapter's auth check, which fires BEFORE it looks at ``mcpServers`` (verified:
    without it every shape above answers ``-32000 Authentication required``
    identically, so the run would prove nothing). ``session/new`` performs no
    model call, so nothing is sent anywhere and the key never leaves the temp
    directory.
    """
    _require_codex_acp(with_node=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "work").mkdir()
        (root / "codex_home").mkdir()
        (root / "codex_home" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-not-a-real-key-" + "0" * 24}), encoding="utf-8"
        )
        stub = root / "stub_mcp.py"
        stub.write_text(_STUB_MCP, encoding="utf-8")
        driver = root / "drive.py"
        driver.write_text(_DRIVER, encoding="utf-8")
        result = _run_driver_reaping_group(
            [
                sys.executable,
                str(driver),
                str(root),
                str(_ENTRY),
                str(stub),
                shutil.which("node") or "node",
            ],
            # A BACKSTOP, not the control. The driver bounds each of its four
            # adapter runs itself and reaps in a finally, so its own worst case is
            # well inside this. Reaching it means the driver was killed before it
            # could reap -- which is why the runner kills the whole process group
            # rather than the driver alone.
            timeout=540,
            cwd=root / "work",
        )
        context = (
            f"driver exit: {result.returncode}\n"
            f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"
        )
        try:
            measured = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pytest.fail("the codex-acp driver produced no measurement\n" + context)

        if (measured.get("stdio_error") or {}).get("code") == -32000:
            pytest.skip("codex-acp refused the fabricated credential; nothing to measure")

        # 1 + 2: the shape is accepted and the server really runs.
        assert measured["stdio_ok"], (
            "codex-acp rejected the mcpServers element Crew emits, so the codex "
            "projection cannot be delivered in this shape at all\n" + context
        )
        child = measured.get("child")
        assert child, "the stdio MCP server was never launched\n" + context
        assert "tools/list" in child["methods"], (
            "codex-acp launched the server but never listed its tools, so the "
            "session would hold a mounted server with no usable tool\n" + context
        )

        # 3: the env allowlist, which is the whole reason for the carriage rule.
        assert child["env"].get("KIROCREW_SESSION_KEY") == "probe-session-key"
        assert "PATH" in child["env"]
        assert "STUB_MCP_REPORT" in child["env"]

        # 4: what the adapter does with a transport its own advertisement denies,
        # and it is TWO measured answers keyed on the element's shape -- each pinned
        # exactly, because a hedge admitting either would let the contract drift.
        #
        # A schema-complete sse element (with the required `headers` array, which
        # is the shape Crew's own translation emits) is named and REFUSED, and the
        # refusal fails the WHOLE session/new -- every server in the array lost over
        # one entry. That is the cost the narrowing exists to avoid on Crew's array.
        assert not measured["sse_ok"] and measured["sse_error"], (
            "codex-acp accepted a schema-complete sse element it advertises it does not "
            f"support: ok={measured['sse_ok']!r} error={measured['sse_error']!r}. "
            "Re-measure before changing this assertion.\n" + context
        )
        assert "sse" in json.dumps(measured["sse_error"]).lower(), (
            "codex-acp failed session/new over an sse element for a reason that does "
            "not name the transport, so this measures something other than the "
            f"transport check: {measured['sse_error']!r}\n" + context
        )
        # A schema-INCOMPLETE sse element (no `headers`) does not parse as sse at
        # all; it falls to the untagged variant and is ACCEPTED, the server silently
        # never wired. Nothing downstream reports it. Both dispositions make the
        # client-side narrowing load-bearing, from opposite directions.
        assert measured["sse_bare_ok"] and not measured["sse_bare_error"], (
            "codex-acp no longer accepts a headerless sse element silently: "
            f"ok={measured['sse_bare_ok']!r} error={measured['sse_bare_error']!r}. "
            "Re-measure before changing this assertion.\n" + context
        )
        assert measured["unknown_type_ok"] and not measured["unknown_type_error"], (
            "codex-acp refused a meaningless transport type. The filter keeps only "
            "POSITIVELY advertised transports because the adapter validates the tag "
            "not at all, and this is the control for the sse measurement above.\n" + context
        )
        assert measured["malformed_ok"], (
            "a malformed stdio element fails the whole session/new. The translator "
            "degrades on a bad spec entry rather than raising, so this would turn "
            "one hand-edited spec line into a dead session.\n" + context
        )
        # The advertisement the client captures and the filter consumes, so this is
        # also the assertion that the two agree on a real adapter.
        assert measured["mcp_capabilities"]["sse"] is False
        assert measured["mcp_capabilities"]["http"] is True

        # 5: the FOLD, which `codex_name` reproduces and the projection's
        # control-plane safety rests on. Two elements were sent, `"probe  one"`
        # (two spaces) and `"probe__one"`. Under codex's per-character rule both
        # register as `probe__one`, so `insert` leaves ONE slot and only one child
        # launches; under a collapsing fold they would be two distinct names and
        # both would. Exactly one child is therefore the measurement that the
        # per-character rule holds -- and that a collapsing fold (which is what
        # manufactured the control-plane collision) is NOT what the adapter does.
        assert measured["fold_session_ok"], "the fold probe's session/new failed\n" + context
        assert len(measured["fold_children"]) == 1, (
            "codex-acp no longer folds whitespace per character: two names that this "
            "rule maps together launched separately, so `codex_name` and the "
            "control-plane collision argument both need re-deriving.\n"
            f"children launched: {measured['fold_children']}\n" + context
        )
        assert codex_name("probe  one") == codex_name("probe__one") == "probe__one"
        assert (
            drop_unadvertised_transports(
                [{"name": "remote", "type": "sse", "url": "http://x/sse", "headers": []}],
                measured["mcp_capabilities"],
            )
            == []
        )


_CLOSE_DRIVER = r"""
import json, os, queue, subprocess, sys, threading, time

root, entry, node = sys.argv[1:4]
work = os.path.join(root, "work")
env = dict(os.environ)
env["CODEX_HOME"] = os.path.join(root, "codex_home")
env["NO_BROWSER"] = "1"


def reap(p):
    for step in (p.terminate, p.kill):
        try:
            step()
            p.communicate(timeout=15)
            return
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            return


def pump(stream, q):
    for line in stream:
        q.put(line)
    q.put(None)


p = subprocess.Popen(
    [node, entry], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, cwd=work, env=env, text=True, bufsize=1,
)
q = queue.Queue()
threading.Thread(target=pump, args=(p.stdout, q), daemon=True).start()
next_id = [0]


def send(method, params, notification=False):
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    if not notification:
        next_id[0] += 1
        msg["id"] = next_id[0]
    p.stdin.write(json.dumps(msg) + "\n")
    p.stdin.flush()
    return None if notification else next_id[0]


def wait(rid, timeout=60):
    deadline = time.time() + timeout
    while True:
        budget = deadline - time.time()
        if budget <= 0:
            return {"_timeout": True}
        try:
            line = q.get(timeout=budget)
        except queue.Empty:
            return {"_timeout": True}
        if line is None:
            return {"_eof": True}
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == rid:
            return msg


def call(method, params):
    return wait(send(method, params))


def alive(sid):
    # The liveness oracle: a live session answers with its refreshed configOptions;
    # an evicted one answers an error frame.
    r = call("session/set_config_option",
             {"sessionId": sid, "configId": "mode", "value": "read-only"})
    return {"ok": "result" in r, "error": r.get("error")}


out = {}
try:
    call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {}}})
    new1 = call("session/new", {"cwd": work, "mcpServers": []})
    out["new_error"] = new1.get("error")
    s1 = (new1.get("result") or {}).get("sessionId")
    if s1:
        out["before_close"] = alive(s1)
        close = call("session/close", {"sessionId": s1})
        out["close_reply"] = {"result": close.get("result"), "error": close.get("error")}
        out["after_close"] = alive(s1)
        out["close_again"] = {"result": call("session/close", {"sessionId": s1}).get("result")}
        # The notification form, on a fresh session: must NOT evict, or the
        # TeardownPolicy's notification=False stops being load-bearing.
        s2 = (call("session/new", {"cwd": work, "mcpServers": []}).get("result") or {}).get("sessionId")
        send("session/close", {"sessionId": s2}, notification=True)
        time.sleep(1.0)
        out["after_close_notification"] = alive(s2)
        # The process is unharmed by the closes.
        out["fresh_after"] = "result" in call("session/new", {"cwd": work, "mcpServers": []})
finally:
    reap(p)
print(json.dumps(out))
"""


@pytest.mark.real_adapter
def test_real_codex_acp_session_close_evicts():
    """ANTI-DRIFT GUARD for ``CodexHarness.teardown`` and codex's membership in
    ``ACP_BACKENDS_SESSION_EVICTION``.

    Both rest on one measured fact about the installed adapter: ``session/close``,
    sent as a REQUEST, makes the sessionId stop answering. If a codex-acp release
    changed that -- close becoming a no-op, or silently turning into a notification
    -- every path that creates and destroys codex sessions on the shared runtime
    (background handles, warm pooled reuse, the entitlement probe) would leak one
    resident session per call, with nothing red to say so. This is where it goes red.

    Three claims, each one the harness docstring makes:

    1. Before close the session is live; after close, as a request, it is not.
       The oracle is ``session/set_config_option`` on the same id, which answers
       ``configOptions`` on a live session and an error frame on an evicted one.
    2. Close as a NOTIFICATION does not evict. This is why ``notification=False``
       is load-bearing rather than a delivery detail: the wrong form fails silent.
    3. Close is idempotent and leaves the process able to open fresh sessions, so
       a double-terminate is safe and a teardown never costs the shared process.

    Same credential arrangement as the sibling live test: a fabricated key in a
    throwaway ``CODEX_HOME`` gets past the auth check that fires before
    ``session/new``; nothing here performs a model call, so nothing leaves the box.
    """
    _require_codex_acp(with_node=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "work").mkdir()
        (root / "codex_home").mkdir()
        (root / "codex_home" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-not-a-real-key-" + "0" * 24}), encoding="utf-8"
        )
        driver = root / "drive_close.py"
        driver.write_text(_CLOSE_DRIVER, encoding="utf-8")
        result = _run_driver_reaping_group(
            [sys.executable, str(driver), str(root), str(_ENTRY), shutil.which("node") or "node"],
            timeout=300,
            cwd=root / "work",
        )
        context = (
            f"driver exit: {result.returncode}\n"
            f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"
        )
        try:
            m = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pytest.fail("the codex-acp close driver produced no measurement\n" + context)

        if (m.get("new_error") or {}).get("code") == -32000:
            pytest.skip("codex-acp refused the fabricated credential; nothing to measure")
        assert "before_close" in m, "session/new failed, so nothing was measured\n" + context

        # 1. eviction.
        assert m["before_close"]["ok"], "baseline session was not live\n" + context
        assert m["close_reply"]["error"] is None, (
            "session/close was refused; the harness's teardown verb is wrong for this "
            f"adapter: {m['close_reply']['error']!r}\n" + context
        )
        assert not m["after_close"]["ok"], (
            "session/close did NOT evict: the sessionId still answers, so codex must "
            "leave ACP_BACKENDS_SESSION_EVICTION until a verb that evicts is found\n" + context
        )

        # 2. the notification form is the leak.
        assert m["after_close_notification"]["ok"], (
            "close as a notification now evicts too; TeardownPolicy.notification=False "
            "is no longer what makes eviction happen -- re-measure before changing it\n" + context
        )

        # 3. idempotent, and the process survives.
        assert m["close_again"]["result"] == {}, (
            "a second close on the same id errored; a double-terminate would fault\n" + context
        )
        assert m["fresh_after"], "session/new failed after the closes\n" + context


_DEPTH_DRIVER = r"""
import json, os, queue, subprocess, sys, threading, time

root, entry, node, src_root, max_depth = sys.argv[1:6]
max_depth = int(max_depth)
sys.path.insert(0, src_root)
from kiro_crew.acp.runtime import _iter_descendant_pids

work = os.path.join(root, "work")
env = dict(os.environ)
env["CODEX_HOME"] = os.path.join(root, "codex_home")
env["NO_BROWSER"] = "1"


def reap(p):
    for step in (p.terminate, p.kill):
        try:
            step()
            p.communicate(timeout=15)
            return
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            return


def pump(stream, q):
    for line in stream:
        q.put(line)
    q.put(None)


def cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return [tok.decode("utf-8", "replace") for tok in raw.split(b"\0") if tok]


p = subprocess.Popen(
    [node, entry], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, cwd=work, env=env, text=True, bufsize=1,
)
q = queue.Queue()
threading.Thread(target=pump, args=(p.stdout, q), daemon=True).start()
next_id = [0]


def call(method, params):
    next_id[0] += 1
    rid = next_id[0]
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
    p.stdin.flush()
    deadline = time.time() + 60
    while True:
        budget = deadline - time.time()
        if budget <= 0:
            return {"_timeout": True}
        try:
            line = q.get(timeout=budget)
        except queue.Empty:
            return {"_timeout": True}
        if line is None:
            return {"_eof": True}
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == rid:
            return msg


out = {}
try:
    call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {}}})
    new = call("session/new", {"cwd": work, "mcpServers": []})
    out["new_error"] = new.get("error")
    # The runtime's own bounded walk, one generation at a time: a pid's depth is the
    # first bound it appears under, so the generation the constant names is measured
    # by the same reader the recycle guard uses, not by a second tree walker.
    depth_of = {}
    for depth in range(max_depth + 1):
        for pid in _iter_descendant_pids(p.pid, depth):
            depth_of.setdefault(pid, depth)
    cmdlines = {str(pid): cmdline(pid) for pid in depth_of}
    hits = [pid for pid, argv in ((int(k), v) for k, v in cmdlines.items()) if argv and "app-server" in argv]
    out["depth_pids"] = sorted(depth_of, key=depth_of.get)
    out["app_server_found"] = bool(hits)
    out["app_server_depth"] = depth_of[hits[0]] if hits else None
    out["cmdlines"] = cmdlines
finally:
    reap(p)
print(json.dumps(out))
"""


@pytest.mark.real_adapter
def test_real_codex_acp_app_server_sits_at_core_rss_depth():
    """ANTI-DRIFT GUARD for ``CodexHarness.CORE_RSS_DEPTH``.

    The core-scope recycle guard measures RSS over ``CORE_RSS_DEPTH`` generations
    below the adapter and holds that against ``CORE_RSS_CEILING_MB``. The scope
    rests on one structural fact about the installed adapter: ``codex app-server``
    is a DIRECT child of the Node process, one generation down, before any sandbox
    wrapper. Every existing pin on the constant is static -- the contract test checks
    it travels with its ceiling, the harness test checks the wrapper arithmetic, the
    runtime test checks the depth is consumed -- so an adapter release that inserts a
    launcher generation would move app-server OUT of the measured scope with nothing
    red to say so: the probe would sum a flat ~100 MB of adapter forever, the
    ceiling would never trip, and a genuine app-server leak would be invisible.
    This is where that goes red.

    Two claims:

    1. ``codex app-server`` is reachable within ``CORE_RSS_DEPTH`` of the spawned
       pid, walked by ``_iter_descendant_pids`` -- the reader the recycle guard
       itself uses, so the test measures the guard's own scope and not a proxy.
       That reader is the kernel's ``/proc`` child lists, so the pin is a Linux
       fact and is gated on Linux the way the adapter itself is gated: through
       ``require_real_adapter``, which skips a local run elsewhere and FAILS a
       lane that declared the pin must run (``KIROCREW_E2E_REQUIRE=1``). A
       ``skipif`` would restore the silence that gate exists to remove.
    2. It sits at exactly ``CORE_RSS_DEPTH``. A plain spawn has
       ``wrapper_generations=0``, so ``rss_depth == CORE_RSS_DEPTH`` and the constant
       IS the generation; a refused ``session/new`` fails rather than skips, because
       a silent skip is how a ratchet goes quiet.

    The constant is asserted, not resolved from the live tree: a wrong constant must
    fail here, not be quietly replaced by a runtime observation of a third-party
    process tree. ``session/new`` is sent so the walk happens after app-server has
    answered a request, not merely been forked. Same credential arrangement as the
    sibling live tests: a fabricated key in a throwaway ``CODEX_HOME`` gets past the
    auth check that fires before ``session/new``; nothing performs a model call.
    """
    _require_codex_acp(with_node=True)
    require_real_adapter(
        sys.platform == "linux",
        what="the Linux /proc process tree the recycle guard's reader walks",
        install="run the real-adapter lane on a Linux host",
    )
    src_root = Path(acp_runtime.__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "work").mkdir()
        (root / "codex_home").mkdir()
        (root / "codex_home" / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-not-a-real-key-" + "0" * 24}), encoding="utf-8"
        )
        driver = root / "drive_depth.py"
        driver.write_text(_DEPTH_DRIVER, encoding="utf-8")
        result = _run_driver_reaping_group(
            [
                sys.executable,
                str(driver),
                str(root),
                str(_ENTRY),
                shutil.which("node") or "node",
                str(src_root),
                str(CodexHarness.CORE_RSS_DEPTH),
            ],
            timeout=300,
            cwd=root / "work",
        )
        context = (
            f"driver exit: {result.returncode}\n"
            f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"
        )
        try:
            m = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pytest.fail("the codex-acp depth driver produced no measurement\n" + context)

        # A refused credential is adapter drift, not "nothing to measure": the
        # fabricated key exists to get past the auth check, and a ratchet that
        # skipped here would go quiet on exactly the release this guard is for.
        assert m.get("new_error") is None, (
            f"session/new was refused ({m.get('new_error')!r}); the fabricated-credential "
            "arrangement no longer reaches app-server -- re-measure before trusting the "
            "depth pin\n" + context
        )
        assert "depth_pids" in m, "the process tree was not walked\n" + context

        # 1. within scope.
        assert m["app_server_found"] is True, (
            f"no `codex app-server` within CORE_RSS_DEPTH={CodexHarness.CORE_RSS_DEPTH} of "
            "the adapter; the core-scope recycle guard is measuring the wrong processes\n" + context
        )
        # 2. exactly the generation the constant names, compared against the
        # constant itself so an edit to CORE_RSS_DEPTH that the tree does not
        # justify goes red here.
        assert m["app_server_depth"] == CodexHarness.CORE_RSS_DEPTH, (
            f"`codex app-server` sits at depth {m['app_server_depth']}, not "
            f"CORE_RSS_DEPTH={CodexHarness.CORE_RSS_DEPTH}; re-measure the constant against "
            "this adapter release\n" + context
        )


@pytest.mark.real_adapter
def test_the_installed_adapter_still_builds_the_frames_the_refusal_reads():
    """The frame VOCABULARY the deny channel keys on, pinned against the adapter.

    Three things identify a codex MCP call to Crew: the ``tool_call`` frame's
    ``rawInput = {server, tool, arguments}`` (``mcpInput``), its
    ``_meta.is_mcp_tool_call`` marker, and the approval request's
    ``_meta.is_mcp_tool_approval`` marker with ``cancel`` as its reject option
    (``buildMcpPermissionRequest`` / ``McpApprovalOptionId``). All three come from
    one builder in codex-acp, so a release that reshapes them blinds the per-call
    refusal, the unidentified-approval refusal and the tripwire together -- the
    coordinated drift the design review names. Observing them on the wire needs a
    model to call a tool; observing them in the adapter's own shipped source does
    not, and the entry the spawn resolves IS that source. Gated like its siblings:
    a skip where the adapter is absent, a failure where a lane requires it, and a
    red where it is present and has drifted.
    """
    _require_codex_acp()
    assert _ENTRY is not None
    source = _ENTRY.read_text(encoding="utf-8", errors="replace")
    for needle in (
        "return { server: item.server, tool: item.tool, arguments: item.arguments };",
        "is_mcp_tool_call: true",
        "is_mcp_tool_approval: true",
        'Cancel: "cancel"',
    ):
        assert needle in source, (
            f"codex-acp at {_ENTRY} no longer contains {needle!r}: the frame shape the "
            "spec-restriction refusal identifies an MCP call by has drifted"
        )


def test_the_real_adapter_guard_is_reachable_at_all(monkeypatch):
    """A skip-only guard is a guard nobody notices has stopped running.

    This does not assert the adapter is installed -- most runners have none, and
    the lane that installs it enforces presence with ``KIROCREW_E2E_REQUIRE``
    instead. It asserts the RESOLVER the guard
    reads is the spawn's own, so a rename there cannot turn the guard permanently
    green without anyone seeing it.

    The ladder's one spawn is ``mise which <adapter>`` through ``_mise_which``. That
    seam is pinned to a recording fake: a real ``mise`` is a version manager that
    may fetch toolchains, and it is a host program this test is not about. What the
    fake records -- that the ladder asked mise for THIS adapter's binary -- is the
    "spawn's own resolver" fact the test exists to pin.
    """
    from kiro_crew.acp import client as client_mod
    from kiro_crew.acp.client import CODEX_ACP_BIN, _resolve_codex_acp_bin

    asked: list[str] = []

    def fake_mise_which(tool: str) -> str | None:
        asked.append(tool)
        return None

    monkeypatch.setattr(client_mod, "_mise_which", fake_mise_which)

    argv, search = _resolve_codex_acp_bin()
    assert argv is None or isinstance(argv, list)
    assert isinstance(search, str)
    assert asked == [CODEX_ACP_BIN]
    assert os.environ.get("CODEX_ACP_BIN") is None or _ENTRY is not None


_LOAD_DRIVER = r"""
import json, os, queue, subprocess, sys, threading, time

root, entry, node = sys.argv[1:4]
work = os.path.join(root, "work")
env = dict(os.environ)
env["CODEX_HOME"] = os.path.join(root, "codex_home")
env["AWS_CONFIG_FILE"] = os.path.join(root, "aws", "config")
env["AWS_SHARED_CREDENTIALS_FILE"] = os.path.join(root, "aws", "creds")
env["NO_BROWSER"] = "1"
# A wrapped codex build writes telemetry into TMPDIR. Point it inside the temp
# root so the measurement leaves nothing behind for the residue reporter to find.
env["TMPDIR"] = os.path.join(root, "tmp")
os.makedirs(env["TMPDIR"], exist_ok=True)
SECRET = "quibbleflum"


def reap(p):
    for step in (p.terminate, p.kill):
        try:
            step()
            p.communicate(timeout=15)
            return
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            return


class Conn:
    def __init__(self):
        self.p = subprocess.Popen(
            [node, entry], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=work, env=env, text=True, bufsize=1,
        )
        self.q = queue.Queue()
        self.notes = []
        self.n = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.p.stdout:
            self.q.put(line)
        self.q.put(None)

    def send(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        return self.n

    def wait(self, rid, timeout=180):
        deadline = time.time() + timeout
        while True:
            budget = deadline - time.time()
            if budget <= 0:
                return {"_timeout": True}
            try:
                line = self.q.get(timeout=budget)
            except queue.Empty:
                return {"_timeout": True}
            if line is None:
                return {"_eof": True}
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == rid:
                return msg
            if msg.get("method"):
                if "id" in msg:
                    # A request from the agent: answer fail-closed so nothing hangs.
                    self.p.stdin.write(json.dumps({
                        "jsonrpc": "2.0", "id": msg["id"],
                        "result": {"outcome": {"outcome": "cancelled"}}}) + "\n")
                    self.p.stdin.flush()
                else:
                    self.notes.append(msg)

    def call(self, method, params, timeout=180):
        return self.wait(self.send(method, params), timeout=timeout)

    def alive(self, sid):
        r = self.call("session/set_config_option",
                      {"sessionId": sid, "configId": "mode", "value": "read-only"}, 60)
        return "result" in r

    def replay(self):
        out = []
        for note in self.notes:
            upd = (note.get("params") or {}).get("update") or {}
            if upd.get("sessionUpdate") in ("agent_message_chunk", "user_message_chunk"):
                c = upd.get("content") or {}
                if isinstance(c, dict) and c.get("type") == "text":
                    out.append(c.get("text") or "")
        return "".join(out)

    def prompt(self, sid, text):
        self.notes = []
        r = self.call("session/prompt",
                      {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}, 300)
        res = r.get("result") or {}
        return {"stop": res.get("stopReason"), "error": r.get("error"),
                "usage": res.get("usage"), "text": self.replay()[:200]}


out = {}
sid = None
c1 = Conn()
try:
    c1.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {}}}, 120)
    new = c1.call("session/new", {"cwd": work, "mcpServers": []})
    out["new_error"] = new.get("error")
    sid = (new.get("result") or {}).get("sessionId")
    if sid:
        out["plant"] = c1.prompt(
            sid, "Remember this word for later: %s. Reply with exactly OK." % SECRET)
        out["close_error"] = c1.call("session/close", {"sessionId": sid}, 60).get("error")
        out["alive_after_close"] = c1.alive(sid)
        c1.notes = []
        load = c1.call("session/load", {"sessionId": sid, "cwd": work, "mcpServers": []})
        out["same_load_ok"] = "result" in load
        out["same_load_error"] = load.get("error")
        if "result" in load:
            out["same_recall"] = c1.prompt(
                sid, "What word did I ask you to remember? Reply with only that word.")
finally:
    reap(c1.p)

if sid:
    c2 = Conn()
    try:
        c2.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {}}}, 120)
        c2.notes = []
        load = c2.call("session/load", {"sessionId": sid, "cwd": work, "mcpServers": []})
        out["fresh_load_ok"] = "result" in load
        out["fresh_load_error"] = load.get("error")
        if "result" in load:
            out["fresh_recall"] = c2.prompt(
                sid, "What word did I ask you to remember? Reply with only that word.")
        out["delete_error"] = c2.call("session/delete", {"sessionId": sid}, 120).get("error")
        after = c2.call("session/load", {"sessionId": sid, "cwd": work, "mcpServers": []})
        out["load_after_delete_ok"] = "result" in after
        out["load_after_delete_error"] = after.get("error")
    finally:
        reap(c2.p)

print(json.dumps(out))
"""


def _provider_config_only(config_toml: str) -> str:
    """Keep only provider settings needed by the live Codex test.

    The allowlist keeps selected top-level scalar keys and whole
    ``[model_providers...]`` tables. A denylist has an open spelling axis: each new
    TOML spelling can reopen it and let an operator-configured process start.
    """
    allowed_top_level_keys = {
        "model_provider",
        "forced_login_method",
        "model",
        "model_reasoning_effort",
        "chatgpt_base_url",
        "check_for_update_on_startup",
    }
    out: list[str] = []
    at_top_level = True
    in_provider_table = False
    for line in config_toml.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("["):
            at_top_level = False
            if stripped.startswith("[["):
                header = None
            else:
                end = stripped.find("]", 1)
                header = stripped[1:end].strip() if end != -1 else None
            in_provider_table = header == "model_providers" or (
                header is not None and header.startswith("model_providers.")
            )
            if in_provider_table:
                out.append(line)
            continue
        if in_provider_table:
            out.append(line)
            continue
        if at_top_level:
            key, separator, _value = stripped.partition("=")
            if separator and key.strip() in allowed_top_level_keys:
                out.append(line)
    return "".join(out)


def test_provider_config_only_excludes_process_configuration():
    config = """\
model_provider = "custom"
forced_login_method = "chatgpt"
model = "example-model"
model_reasoning_effort = "low"
chatgpt_base_url = "https://example.invalid"
check_for_update_on_startup = false
mcp_servers = { evil = { command = "/bin/sh" } }
mcp_servers.evil2 = { command = "/bin/sh" }
notify = ["/bin/sh"]

[model_providers.custom]
name = "Custom"
wire_api = "responses"

[mcp_servers.builder]
command = "/bin/sh"

[hooks]
command = "/bin/sh"
"""

    filtered = _provider_config_only(config)

    assert "mcp_servers" not in filtered
    assert "hooks" not in filtered
    assert "notify" not in filtered
    for provider_setting in (
        'model_provider = "custom"',
        'forced_login_method = "chatgpt"',
        'model = "example-model"',
        'model_reasoning_effort = "low"',
        'chatgpt_base_url = "https://example.invalid"',
        "check_for_update_on_startup = false",
        "[model_providers.custom]",
        'name = "Custom"',
        'wire_api = "responses"',
    ):
        assert provider_setting in filtered


@pytest.mark.skipif(
    os.environ.get("KIROCREW_LIVE_CODEX_PROMPT_TESTS") != "1",
    reason=("spends real model tokens; set KIROCREW_LIVE_CODEX_PROMPT_TESTS=1 to opt in"),
)
def test_real_codex_acp_load_after_close_restores():
    """ANTI-DRIFT GUARD for codex's membership in ``ACP_BACKENDS_SESSION_SHARING``.

    The sibling above measures that ``session/close`` makes the sessionId stop
    answering. That alone would argue codex OUT of sharing, and for a while it did.
    This measures the other half of the same verb: the Codex thread's own record
    SURVIVES the close, so ``session/load`` restores the conversation and a shared
    subagent stays continuable after the runtime that served it is gone.

    Three claims, each one the sharing set's comment makes:

    1. After a close, ``session/load`` on the SAME id succeeds and the session then
       answers a question only the first turn could have taught it.
    2. The same load succeeds from a RESTARTED adapter process over the same
       ``CODEX_HOME``. This is the shape ``spawn_continue`` actually takes -- the
       parent's runtime is usually dead by then -- and it is the claim that cannot be
       inferred from the first, because a same-process load could have been served
       from adapter memory.
    3. ``session/delete`` ARCHIVES the thread and a load afterwards refuses. Release
       therefore has a verb that genuinely disposes, and ``close`` is demonstrably
       not it.

    Unlike its sibling this test PROMPTS, so it needs a codex build whose credential
    this host actually holds. The spend opt-in below is the ONE gate that decides
    whether it runs; every other precondition -- codex-acp, node, ``CODEX_PATH``, a
    resolvable provider, a prompt that completes -- is a FAILURE past that gate,
    because the opt-in is a declaration that this host can run it. None of them may
    be a ``skipif``: ``test_real_adapter_gate.py`` forbids one on ``_ENTRY`` in a
    guarded file, on the ground that a skipif cannot fail and so restores exactly
    the silence the lane removes.

    It deliberately does NOT carry ``@pytest.mark.real_adapter``, and adding one would
    turn the contract lane red rather than widen its coverage. That lane installs
    adapters from a pinned manifest and then asserts on its own junit report that no
    SELECTED case was skipped; it has no model credential and does not set the spend
    opt-in below, so a marked case here would be selected and then skip, which is the
    exact shape that assertion exists to refuse. The lane's floor counts the contracts
    it can actually measure, and this one is not among them. What covers this file in
    CI is ``test_provider_config_only_excludes_process_configuration``, which carries
    no marker and no skip.

    ``CODEX_PATH`` is configuration, not consent to spend model tokens, so prompting
    also requires the dedicated opt-in. This is the only test in this file that sends
    ``session/prompt``; the other two live tests handshake and inspect session state
    without a model call, so they carry no such gate.
    """
    if _ENTRY is None:
        pytest.fail(
            "codex-acp is not installed, and KIROCREW_LIVE_CODEX_PROMPT_TESTS=1: the "
            "opt-in declares this host can prompt a real codex. Install it with: "
            f"npm i -g @agentclientprotocol/codex-acp@{MEASURED_CODEX_ACP_VERSION}"
        )
    if shutil.which("node") is None:
        pytest.fail("node is not on PATH, so the codex-acp entrypoint cannot run")
    if not os.environ.get("CODEX_PATH"):
        pytest.fail(
            "CODEX_PATH is unset, so there is no codex build to prompt; unset "
            "KIROCREW_LIVE_CODEX_PROMPT_TESTS on a host that cannot prompt one"
        )

    ambient = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "config.toml"
    if not ambient.is_file():
        pytest.fail(
            f"no codex config.toml at {ambient} to resolve a provider from, so this "
            "opt-in test cannot prompt; unset KIROCREW_LIVE_CODEX_PROMPT_TESTS on a "
            "host without a configured codex"
        )

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "work").mkdir()
        (root / "codex_home").mkdir()
        (root / "aws").mkdir()
        (root / "codex_home" / "config.toml").write_text(
            _provider_config_only(ambient.read_text(encoding="utf-8")), encoding="utf-8"
        )
        driver = root / "drive_load.py"
        driver.write_text(_LOAD_DRIVER, encoding="utf-8")
        result = _run_driver_reaping_group(
            [sys.executable, str(driver), str(root), str(_ENTRY), shutil.which("node") or "node"],
            timeout=900,
            cwd=root / "work",
        )
        context = (
            f"driver exit: {result.returncode}\n"
            f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"
        )
        try:
            m = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pytest.fail("the codex-acp load driver produced no measurement\n" + context)

        # ``KIROCREW_LIVE_CODEX_PROMPT_TESTS=1`` is a DECLARATION that this host has
        # a working credentialled codex, the way ``KIROCREW_E2E_REQUIRE=1`` is used in
        # ``real_adapter_gate``. Under a declaration an absent precondition is a
        # failure rather than a skip: a host that cannot meet it unsets the opt-in,
        # and a run that reports success having measured nothing is what the
        # no-silent-skip rule exists to refuse.
        if m.get("new_error"):
            pytest.fail(
                f"codex-acp refused session/new on this host: {m['new_error']!r}\n{context}"
            )
        plant = m.get("plant") or {}
        if plant.get("error") or plant.get("stop") != "end_turn":
            pytest.fail(f"the planting prompt did not complete on this host: {plant!r}\n{context}")

        # The close still evicts -- restated here so this test fails rather than
        # silently measuring a load on a session that never left.
        assert m["close_error"] is None, f"session/close was refused\n{context}"
        assert not m["alive_after_close"], (
            "session/close no longer evicts, so this test is measuring a live session "
            "rather than a restored one\n" + context
        )

        # 1. the record survived the close, on the same process.
        assert m["same_load_ok"], (
            "session/load on a CLOSED sessionId failed, so codex's close disposes the "
            "record as well as the session -- codex must leave "
            f"ACP_BACKENDS_SESSION_SHARING: {m.get('same_load_error')!r}\n" + context
        )
        same = m.get("same_recall") or {}
        assert "quibbleflum" in (same.get("text") or "").lower(), (
            "the restored session could not recall the first turn, so the load "
            f"returned a session without its context: {same!r}\n" + context
        )

        # 2. and across an adapter RESTART, which is the continuation's real shape.
        assert m["fresh_load_ok"], (
            "session/load from a FRESH adapter process failed, so a codex subagent "
            "stops being continuable once its runtime is recycled -- which is the "
            f"ordinary case: {m.get('fresh_load_error')!r}\n" + context
        )
        fresh = m.get("fresh_recall") or {}
        assert "quibbleflum" in (fresh.get("text") or "").lower(), (
            f"the reopened thread lost its context across the restart: {fresh!r}\n" + context
        )

        # 3. delete is the disposing verb, so release has one and close is not it.
        assert m["delete_error"] is None, f"session/delete was refused\n{context}"
        assert not m["load_after_delete_ok"], (
            "session/load succeeded after session/delete, so delete no longer disposes "
            "the thread and release has no verb that does\n" + context
        )
