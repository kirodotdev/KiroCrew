"""Windows liveness-probe audit — ``os.kill(pid, 0)`` must never reach Windows.

On POSIX, ``os.kill(pid, 0)`` is the idiomatic "does this pid exist?" probe:
signal 0 performs the permission/existence check without delivering anything.
On Windows CPython maps ``os.kill(pid, sig)`` onto ``TerminateProcess(handle,
sig)`` for every signal except ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT`` — so the
same expression **terminates the process it is asking about** (with exit code 0)
and then reports it alive. It is not a portability wart; it is a silent
process-killer, and the damage lands on whatever now owns that pid.

``platform_compat.pid_exists`` / ``pid_liveness`` exist precisely for this
(``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` on Windows) and preserve the
POSIX semantics callers rely on, including "EPERM means alive, not gone".

This is a regression tripwire, in the spirit of ``test_spawn_audit.py``: a NEW
raw signal-0 probe fails this test until the author either routes it through the
shim or adds a ``file::function`` key to :data:`GATED_PROBES` with a
justification for why that site can never execute on Windows.

Scope is deliberately narrow — only the signal-0 *probe* form, not raw POSIX
calls in general. The tree carries many raw POSIX call sites (``fcntl``,
``resource``, ``os.killpg``, ``pty``, ``termios``, …) and the overwhelming
majority are legitimately POSIX-gated implementation detail; auditing them all
here would bury this signal in noise. General portability is governed by the
``platform_compat`` shim table in ``AGENTS.md``, the ``cross-platform.yml``
added-line gate, and review. What makes signal-0 special is that getting it
wrong is *destructive* rather than merely unavailable — and the CI gate only
inspects lines a PR adds, so a probe that arrives by a file move or a rebase is
invisible to it. This test reads the whole tree on every run.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

# ``file::function`` sites allowed to keep a raw signal-0 probe, with the reason
# it can never run on Windows. Keep this list SHORT and each entry justified.
GATED_PROBES: dict[str, str] = {
    # POSIX-only sweep of children that reparented out of the killed process
    # group. Guarded by an `if platform_compat.IS_WINDOWS: return` on the first
    # line of the body, and moot there anyway: kill_process_tree already uses
    # `taskkill /T` to walk the whole child tree, so nothing is left to sweep.
    # The function docstring states this.
    "acp/client.py::_kill_escaped_children": (
        "explicit `if platform_compat.IS_WINDOWS: return` early-out; "
        "process groups do not exist on Windows"
    ),
    # platform_compat IS the shim — its POSIX branch is the real implementation
    # that every other caller is supposed to route through, and it is reached
    # only under `if IS_POSIX`.
    "platform_compat.py::pid_exists": "the shim's own POSIX branch (under IS_POSIX)",
    "platform_compat.py::pid_liveness": "the shim's own POSIX branch (under IS_POSIX)",
}


def _is_signal_zero_probe(node: ast.Call) -> bool:
    """True when *node* is ``os.kill(<anything>, 0)``.

    Matches the literal ``0`` second positional argument only — that is the
    probe form. A real signal (``signal.SIGTERM``, a variable) is an intentional
    kill and out of scope for this audit.
    """
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "kill"):
        return False
    if not (isinstance(func.value, ast.Name) and func.value.id == "os"):
        return False
    if len(node.args) < 2:
        return False
    sig = node.args[1]
    # ``sig.value is not False`` because ``False == 0`` in Python, and
    # ``os.kill(pid, False)`` is not the idiom this audit is about.
    return isinstance(sig, ast.Constant) and sig.value == 0 and sig.value is not False


def _enclosing_functions(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    out: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node))
    return out


def _find_raw_probes() -> dict[str, list[int]]:
    """Map ``file::function`` -> line numbers of raw signal-0 probes."""
    found: dict[str, list[int]] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if rel.startswith("_vendor/"):
            continue  # vendored third-party code is excluded from all linters
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        funcs = _enclosing_functions(tree)
        for name, fnode in funcs:
            for sub in ast.walk(fnode):
                if isinstance(sub, ast.Call) and _is_signal_zero_probe(sub):
                    found.setdefault(f"{rel}::{name}", []).append(sub.lineno)
        # Module-level probes (outside any function) would escape the loop
        # above; catch them under a synthetic "<module>" key so they cannot
        # slip through unaudited.
        func_line_spans = [
            range(f.lineno, (getattr(f, "end_lineno", f.lineno) or f.lineno) + 1) for _, f in funcs
        ]
        for sub in ast.walk(tree):
            if isinstance(sub, ast.Call) and _is_signal_zero_probe(sub):
                if not any(sub.lineno in span for span in func_line_spans):
                    found.setdefault(f"{rel}::<module>", []).append(sub.lineno)
    return found


def test_no_ungated_signal_zero_probe() -> None:
    """No raw ``os.kill(pid, 0)`` outside the justified allowlist."""
    found = _find_raw_probes()
    offenders = {k: v for k, v in found.items() if k not in GATED_PROBES}
    assert not offenders, (
        "Raw `os.kill(pid, 0)` liveness probe(s) found. On Windows this "
        "TERMINATES the probed process instead of testing it. Use "
        "`platform_compat.pid_exists(pid)` (or `pid_liveness` when you must "
        "distinguish EPERM/unsignalable), or add the site to GATED_PROBES with "
        "a justification for why it cannot run on Windows:\n"
        + "\n".join(f"  {k} at line(s) {v}" for k, v in sorted(offenders.items()))
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Every GATED_PROBES entry still names a real probe.

    Keeps the allowlist honest: once a site is fixed or deleted, its exemption
    must go too, so the list can never quietly grant cover to a future probe
    that reuses the name.
    """
    found = _find_raw_probes()
    stale = sorted(set(GATED_PROBES) - set(found))
    assert not stale, (
        "GATED_PROBES lists sites that no longer contain a raw signal-0 probe — "
        f"remove them: {stale}"
    )


def test_gated_sites_carry_a_written_justification() -> None:
    """An exemption with an empty reason is not an exemption.

    The allowlist is only trustworthy if each entry says why the site cannot
    execute on Windows; a bare key would let a probe in on a shrug.
    """
    unjustified = sorted(k for k, v in GATED_PROBES.items() if not v.strip())
    assert not unjustified, f"GATED_PROBES entries need a justification: {unjustified}"


def test_app_backend_pid_alive_uses_the_shim() -> None:
    """Regression pin for a site that was previously a raw probe.

    ``apps/backend.py::_pid_alive`` is called from the stale-reap escalation
    loop, which is otherwise fully shim-routed
    (``platform_compat.kill_process_tree``) and therefore clearly intended to run
    on Windows. A raw probe there was only ever *latently* dangerous because
    ``_proc_start_time`` shells ``ps`` and returns ``None`` on Windows, so the
    PID-reuse guard declined to reap before reaching the probe — an incidental
    mask that a Windows ``_proc_start_time`` would remove.
    """
    from kiro_crew.apps import backend

    src = Path(backend.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_pid_alive"
    )
    assert not any(
        isinstance(s, ast.Call) and _is_signal_zero_probe(s) for s in ast.walk(fn)
    ), "_pid_alive regressed to a raw os.kill(pid, 0) probe"
    segment = ast.get_source_segment(src, fn) or ""
    assert (
        "pid_exists" in segment
    ), "_pid_alive must route its liveness probe through platform_compat.pid_exists"
