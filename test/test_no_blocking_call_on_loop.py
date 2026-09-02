"""Build gate: no blocking syscall on the asyncio event loop (RFC Phase 1).

A static AST check that fails the build when a known-blocking syscall appears
lexically inside an ``async def`` body in ``kiro_crew`` source. This is the
deterministic enforcement the event-loop fault-isolation RFC calls for
(the ``no-blocking-call-on-event-loop`` rule in ``AUTOSDE.yaml``, whose
rationale this gate makes deterministic). It complements the judgment-based
AUTOSDE rule ``no-blocking-call-on-event-loop``: the gate hard-fails the
deterministic cases so they can never land, and the AUTOSDE rule covers the
cases that need human/LLM judgment.

Why this exists: five gateway wedges in June 2026 were one defect wearing
different masks -- a blocking syscall on the single event loop froze every
task (the user's turn, the liveness heartbeat) until the watchdog killed the
process and respawned into the same condition.

Two-tier split (deliberate):

  DETERMINISTIC tier -- this gate. Only calls with NO legitimate use on an
  event loop, so flagging them is never a false positive and a hard build
  failure is always correct:
    - subprocess.{run,call,check_call,check_output,getoutput,getstatusoutput}
    - os.{system,waitpid,wait,waitid,wait3,wait4}
    - time.sleep            (use asyncio.sleep)

  JUDGMENT tier -- the AUTOSDE rule, NOT this gate. Calls that are usually
  wrong on the loop but have a common *legitimate* on-loop form a static check
  cannot safely distinguish:
    - os.close            -- almost always closing a fresh ``tempfile.mkstemp``
                             fd (safe; close never blocks). The wedge-prone
                             form is os.close on a PTY *master* whose shell is
                             wedged. A naive "skip mkstemp fds" auto-rule is
                             unsafe: name reuse (e.g. a loop variable ``fd``
                             rebound to a master after an earlier mkstemp ``fd``)
                             makes it hide the dangerous close. Telling the two
                             apart needs judgment, so os.close is the AUTOSDE
                             rule's job, backed at runtime by the loop watchdog.
    - .communicate()      -- dominated by the legit asyncio form
                             ``await proc.communicate()`` /
                             ``await asyncio.wait_for(proc.communicate(), ...)``.
    - blocking socket ops, blocking file IO / bare ``open()``.

  (This narrows the RFC's initial banned-list, which named os.close: building
  it deterministically proved either noisy or -- worse -- unsafe, as above. The
  RFC appendix and the AUTOSDE rule carry os.close instead.)

Scope rules for the deterministic tier:
  - Flagged only inside an ``async def`` body. A nested ``def`` / ``async def``
    / ``lambda`` is a separate frame (a sync helper, a thread target, an
    offloaded callable) and is not scanned as part of the enclosing loop frame;
    a nested ``async def`` is scanned on its own by the top-level walk.
  - The offload pattern ``run_in_executor(pool, fn, *args)`` passes ``fn`` as a
    bare Name -- not a call -- so it is never flagged.
  - Import aliases are resolved per module: ``import subprocess as sp`` makes
    ``sp.run(...)`` match, and ``from time import sleep`` makes a bare
    ``sleep(...)`` match. Otherwise a single aliased import would silently defeat
    the gate. Method calls on unrelated objects (``app.run()``, ``task.wait()``)
    still never match -- their Name is not bound to a banned module/function.

Escape hatch: a flagged line that is genuinely safe on the loop may be
suppressed with a trailing ``# loop-ok: <reason>`` comment. Every suppression
must carry a reason so the exception is auditable in review and greppable
later. (In practice the deterministic tier should need none -- none of these
calls has a legitimate on-loop use.)
"""
from __future__ import annotations

import ast
import io
import pathlib
import tokenize

from source_corpus import parsed_candidates, src_root

# module name -> attribute names that block when run on the loop AND have no
# legitimate on-loop use (so a hard failure is always correct).
_BANNED: dict[str, set[str]] = {
    "subprocess": {
        "run",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
    },
    "os": {"system", "waitpid", "wait", "waitid", "wait3", "wait4"},
    "time": {"sleep"},
}

# Trailing-comment marker that suppresses a single flagged line.
_SUPPRESS = "loop-ok"

# Scopes we do NOT descend into from an async def body: a nested function or
# lambda is a different execution frame, and a nested async def is collected
# and scanned on its own by the top-level walk.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _src_root() -> pathlib.Path:
    """Locate the kiro_crew source tree (see ``source_corpus.src_root``)."""
    return src_root()


def _import_bindings(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve how a module names the banned stdlib modules/functions.

    Returns ``(module_aliases, func_aliases)`` where:
      * ``module_aliases`` maps a local Name bound to a banned MODULE to its real
        name -- e.g. ``import subprocess as sp`` -> ``{"sp": "subprocess"}`` (so
        ``sp.run(...)`` resolves), plus the identity ``{"subprocess": "subprocess"}``.
      * ``func_aliases`` maps a local Name bound via ``from``-import to its
        ``"module.func"`` -- e.g. ``from time import sleep as nap`` ->
        ``{"nap": "time.sleep"}`` (so a bare ``nap(...)`` resolves).
    Without this, ``import subprocess as sp`` / ``from os import system`` defeat
    the gate -- a one-import-away hole that would let a hard-blocking syscall
    land on the loop undetected.
    """
    module_aliases: dict[str, str] = {}
    func_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _BANNED:
                    module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            banned_funcs = _BANNED.get(node.module or "")
            if banned_funcs is None:
                continue
            for alias in node.names:
                if alias.name in banned_funcs:
                    func_aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return module_aliases, func_aliases


def _dotted_if_banned(
    call: ast.Call,
    module_aliases: dict[str, str],
    func_aliases: dict[str, str],
) -> str | None:
    """Return ``"module.func"`` if this call matches a banned pattern, else None.

    Matches both the ``module.func(...)`` attribute shape (resolving import
    aliases via ``module_aliases``) and the bare ``func(...)`` shape bound by a
    ``from``-import (via ``func_aliases``). Method calls on unrelated objects
    (``app.run()``, ``task.wait()``) never match: their Name is not a known
    banned-module binding.
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        value = func.value
        if not isinstance(value, ast.Name):
            return None
        real_module = module_aliases.get(value.id)
        if real_module is not None and func.attr in _BANNED.get(real_module, ()):
            return f"{real_module}.{func.attr}"
        return None
    if isinstance(func, ast.Name):
        return func_aliases.get(func.id)
    return None


def _scope_calls(node: ast.AST):
    """Yield Call nodes reachable from ``node`` without crossing into a nested
    function/lambda scope."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _NESTED_SCOPES):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from _scope_calls(child)


def _suppressed_lines(source: str) -> set[int]:
    """Line numbers carrying a genuine ``# loop-ok`` COMMENT (not a substring).

    Tokenizing (rather than substring-scanning the raw text) means a ``loop-ok``
    appearing inside a string literal -- e.g. ``subprocess.run(["echo", "loop-ok"])``
    -- does NOT suppress, while a real trailing ``# loop-ok: <reason>`` comment
    does. A flagged call is suppressed when ANY line it spans is so marked, so the
    comment may sit on the last line of a multi-line call (the idiomatic spot),
    not only the call's first line.
    """
    out: set[int] = set()
    if _SUPPRESS not in source:
        # No comment token can carry the marker if the text does not, and
        # re-lexing every async module to learn that costs 3.5s across the tree.
        return out
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and _SUPPRESS in tok.string:
                out.add(tok.start[0])
    except (tokenize.TokenError, IndentationError):
        # Malformed source: fall back to no suppressions; ast.parse in the
        # caller will surface the real error.
        pass
    return out


def find_violations(
    source: str, path: str = "<source>", *, tree: ast.Module | None = None
) -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, dotted_name)`` for banned calls in async bodies.

    ``tree`` lets a caller that already parsed *source* hand the tree over; it
    must be the parse of *source*, since the suppression scan re-tokenizes the
    text.
    """
    if tree is None:
        tree = ast.parse(source)
    module_aliases, func_aliases = _import_bindings(tree)
    suppressed = _suppressed_lines(source)
    out: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, _NESTED_SCOPES):
                # A nested def/async def/lambda directly in the body is a
                # separate execution frame (a sync helper, a thread target, an
                # offloaded callable) -- not this loop. Skip it; a nested async
                # def is collected and scanned on its own by ast.walk.
                continue
            for call in _scope_calls(stmt):
                dotted = _dotted_if_banned(call, module_aliases, func_aliases)
                if dotted is None:
                    continue
                # Suppressed if a `# loop-ok` comment sits on any line the call
                # spans (first line .. end_lineno, which covers multi-line calls).
                span = range(call.lineno, (call.end_lineno or call.lineno) + 1)
                if any(ln in suppressed for ln in span):
                    continue
                out.append((path, call.lineno, dotted))
    return out


#: A banned call only counts inside an ``ast.AsyncFunctionDef``, which cannot be
#: parsed from text lacking the ``async`` keyword -- so a module without it holds
#: no violation and is not worth parsing. The literal is the bare keyword and NOT
#: ``"async def"``: ``async  def f():`` (two spaces) is valid Python, so the
#: two-word spelling is not a necessary condition and would skip such a file.
#:
#: The banned names are deliberately not filtered on either. They resolve through
#: import aliases (``import subprocess as sp``), so the literal at the call site
#: is not predictable from ``_BANNED``.
_REQUIRE_ALL = ("async",)


def collect_repo_violations() -> list[tuple[str, int, str]]:
    """Scan every ``kiro_crew/**/*.py`` for on-loop blocking calls."""
    base = _src_root().parent
    out: list[tuple[str, int, str]] = []
    for py, src, tree in parsed_candidates(_REQUIRE_ALL):
        # A syntactically-invalid module is the compiler's problem, not this
        # gate's, so `parsed_candidates` skips it rather than masking the real
        # build error.
        try:
            rel = str(py.relative_to(base))
        except ValueError:
            rel = str(py)
        out.extend(find_violations(src, rel, tree=tree))
    return out


# --------------------------------------------------------------------------- #
# The gate                                                                     #
# --------------------------------------------------------------------------- #
def test_no_blocking_call_on_event_loop() -> None:
    violations = collect_repo_violations()
    if violations:
        detail = "\n".join(
            f"  {path}:{lineno}  {name}(...) runs on the event loop"
            for path, lineno, name in violations
        )
        raise AssertionError(
            "Blocking syscall(s) found inside an `async def` body "
            "(event-loop fault-isolation RFC, Phase 1 gate).\n"
            "These have no legitimate use on the loop. Offload via "
            "run_in_executor(<pool>, fn, *args) using a pool from "
            "kiro_crew.executors, or switch to an async API (e.g. "
            "asyncio.sleep, asyncio.create_subprocess_exec). If a line is "
            "genuinely safe on the loop, add a trailing '# loop-ok: <reason>' "
            "comment.\n"
            f"{detail}"
        )


# --------------------------------------------------------------------------- #
# Meta-tests: prove the gate detects/allows the right things                   #
# --------------------------------------------------------------------------- #
def test_flags_inline_subprocess_run() -> None:
    src = "import subprocess\nasync def f():\n    subprocess.run(['x'])\n"
    assert [v[2] for v in find_violations(src)] == ["subprocess.run"]


def test_flags_inline_time_sleep() -> None:
    src = "import time\nasync def f():\n    time.sleep(1)\n"
    assert [v[2] for v in find_violations(src)] == ["time.sleep"]


def test_flags_inline_os_waitpid() -> None:
    src = "import os\nasync def f(pid):\n    os.waitpid(pid, 0)\n"
    assert [v[2] for v in find_violations(src)] == ["os.waitpid"]


def test_allows_run_in_executor_bare_callable() -> None:
    # The codebase's offload form: the blocking fn is passed as a Name, not
    # called inline, so it must NOT be flagged.
    src = (
        "import subprocess\n"
        "async def f(loop):\n"
        "    await loop.run_in_executor(None, subprocess.run, ['x'])\n"
    )
    assert find_violations(src) == []


def test_allows_blocking_in_nested_sync_def() -> None:
    # A sync helper defined inside an async def is a separate scope (and the
    # established pattern for offloaded work); not flagged.
    src = (
        "import subprocess\n"
        "async def f():\n"
        "    def helper():\n"
        "        return subprocess.run(['x'])\n"
        "    return helper\n"
    )
    assert find_violations(src) == []


def test_subprocess_run_in_to_thread_helper_is_skipped() -> None:
    # Regression for the nested-scope root bug: subprocess.run inside a sync
    # `def _run()` offloaded via asyncio.to_thread must NOT be flagged even
    # when the helper is a direct statement of the async body.
    src = (
        "import subprocess, asyncio\n"
        "async def f():\n"
        "    def _run():\n"
        "        return subprocess.run(['x'])\n"
        "    return await asyncio.to_thread(_run)\n"
    )
    assert find_violations(src) == []


def test_allows_sync_def_blocking() -> None:
    # Plain sync functions are never on the loop.
    src = "import subprocess\ndef f():\n    subprocess.run(['x'])\n"
    assert find_violations(src) == []


def test_respects_loop_ok_suppression() -> None:
    src = (
        "import subprocess\n"
        "async def f():\n"
        "    subprocess.run(['x'])  # loop-ok: pre-loop startup, fixed argv\n"
    )
    assert find_violations(src) == []


def test_unrelated_run_method_not_flagged() -> None:
    # `.run` / `.wait` on something that is not the subprocess/os module must
    # not be flagged (avoids false positives on app.run(), task.wait()).
    src = (
        "async def f(app, task):\n"
        "    app.run()\n"
        "    task.wait()\n"
    )
    assert find_violations(src) == []


def test_os_close_is_judgment_tier_not_flagged() -> None:
    # os.close is intentionally NOT in the deterministic tier: its dominant use
    # is closing a fresh mkstemp fd (safe), and a naive auto-skip can hide the
    # wedge-prone case (os.close on a PTY master), so it is enforced by the
    # judgment-based AUTOSDE rule + the runtime watchdog instead. Pin that
    # decision here so it is not silently reverted.
    src = "import os\nasync def f(fd):\n    os.close(fd)\n"
    assert find_violations(src) == []


def test_flags_aliased_module_import() -> None:
    # `import subprocess as sp; sp.run(...)` must be flagged — a single aliased
    # import would otherwise silently defeat the gate.
    src = "import subprocess as sp\nasync def f():\n    sp.check_output(['x'])\n"
    assert [v[2] for v in find_violations(src)] == ["subprocess.check_output"]


def test_flags_aliased_time_import() -> None:
    src = "import time as _time\nasync def f():\n    _time.sleep(1)\n"
    assert [v[2] for v in find_violations(src)] == ["time.sleep"]


def test_flags_from_import_bare_call() -> None:
    # `from os import system; system(...)` — bare-Name call bound by a from-import.
    src = "from os import system\nasync def f():\n    system('x')\n"
    assert [v[2] for v in find_violations(src)] == ["os.system"]


def test_flags_from_import_aliased_bare_call() -> None:
    src = "from time import sleep as nap\nasync def f():\n    nap(5)\n"
    assert [v[2] for v in find_violations(src)] == ["time.sleep"]


def test_from_import_does_not_flag_unrelated_name() -> None:
    # A local `run` that is NOT the from-imported banned func must not be flagged.
    src = "async def f(run):\n    run('x')\n"
    assert find_violations(src) == []


def test_loop_ok_substring_in_string_does_not_suppress() -> None:
    # `loop-ok` inside a string literal is NOT a comment and must NOT suppress.
    src = (
        "import subprocess\n"
        "async def f():\n"
        "    subprocess.run(['echo', 'loop-ok'])\n"
    )
    assert [v[2] for v in find_violations(src)] == ["subprocess.run"]


def test_loop_ok_on_last_line_of_multiline_call_suppresses() -> None:
    # The comment on the LAST line of a multi-line call (idiomatic placement)
    # must suppress — the call's lineno is its first line.
    src = (
        "import subprocess\n"
        "async def f():\n"
        "    subprocess.check_output(\n"
        "        ['x'],\n"
        "        timeout=2,\n"
        "    )  # loop-ok: pre-loop startup\n"
    )
    assert find_violations(src) == []


def test_heartbeat_beat_has_no_inline_blocking_calls() -> None:
    """Pin that heartbeat._beat() offloads rebuild_index/prune_history/prune.

    A focused guard: if someone adds a bare blocking call in _beat() without
    asyncio.to_thread wrapping, this fails before the broad repo gate to give
    a specific, actionable message.
    """
    src_path = _src_root() / "heartbeat.py"
    source = src_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Find the _beat async def
    beat_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_beat":
            beat_fn = node
            break
    assert beat_fn is not None, "Could not find async def _beat in heartbeat.py"

    # Collect all direct method calls in _beat's immediate scope (not nested defs)
    # that call rebuild_index, prune_history, or prune WITHOUT asyncio.to_thread wrapping
    banned_methods = {"rebuild_index", "prune_history", "prune"}
    violations = []
    for call in _scope_calls(beat_fn):
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr in banned_methods:
            # Check it's not inside asyncio.to_thread(...)
            violations.append(f"line {call.lineno}: bare .{func.attr}() call")

    assert violations == [], (
        "heartbeat._beat() contains inline blocking calls that must be wrapped "
        "in asyncio.to_thread():\n  " + "\n  ".join(violations)
    )


if __name__ == "__main__":  # standalone triage: `python3 test/test_no_blocking_call_on_loop.py`
    import sys

    found = collect_repo_violations()
    for _path, _lineno, _name in found:
        print(f"{_path}:{_lineno}: {_name}")
    print(f"\n{len(found)} violation(s)")
    sys.exit(1 if found else 0)
