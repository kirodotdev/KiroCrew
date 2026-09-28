"""No desktop-UI spawn takes its binary from PATH.

A gateway's PATH can lead with an agent-writable directory (a worktree venv's
``bin``, ``~/.local/bin``), so a spawn whose ``argv[0]`` is a bare name lets a
planted shim run with the gateway's environment and outside the sandbox. The
dialog and capture spawns are the shape most exposed to it: a user gesture
reaches them, they run in the gateway process, and the sandbox cannot route them
because the child's whole purpose is to draw on the real desktop session, which a
rewritten process identity would be denied.

Every binary named here ships in a fixed system directory on its platform, so
:func:`kiro_crew.platform_compat.trusted_system_bin` resolves it on a stock host
and the pin costs no function. The audit is mechanical for the reason the spawn
audit is mechanical: the rule is easy to state, and easy to forget at the next
call site.

``git`` is deliberately NOT in the set below. It is installed by a package
manager rather than shipped by the OS, so the trusted lookup answers ``None`` for
an ordinary Homebrew or version-manager install and pinning it would take the git
panel away from those hosts. That residual has its own pin at the end of this
file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from source_corpus import parsed_candidates, src_root

#: Call names that start a child process. Matched on the attribute or bare name,
#: so both ``subprocess.run(...)`` and a ``from subprocess import run`` spelling
#: are seen.
_SPAWN_CALLS = frozenset(
    {
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "create_subprocess_exec",
    }
)

#: OS-shipped binaries a desktop-UI spawn reaches for, each resolvable from the
#: fixed system directories on the platform that has it.
_DESKTOP_UI_BINARIES = frozenset(
    {
        "osascript",
        "screencapture",
        "open",
        "xdg-open",
        "explorer",
        "explorer.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
    }
)

pytestmark = pytest.mark.xdist_group(name="tree_scan_desktop_ui_trusted_bin")


def _sequence_head(node: ast.expr) -> ast.expr | None:
    """The first element of a list or tuple literal, or None.

    A starred first element (``[*_git, "show"]``) is built elsewhere, so this
    answers None and the site stays out of the audit's reach rather than passing
    it silently.
    """
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return None
    head = node.elts[0]
    return None if isinstance(head, ast.Starred) else head


def _enclosing_scope(tree: ast.Module, call: ast.Call) -> ast.AST:
    """The innermost function containing *call*, or the module itself."""
    best: ast.AST = tree
    best_line = -1
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.lineno <= call.lineno <= (node.end_lineno or node.lineno):
            if node.lineno > best_line:
                best_line = node.lineno
                best = node
    return best


def _argv_head(tree: ast.Module, call: ast.Call) -> ast.expr | None:
    """The expression that becomes ``argv[0]``, or None when it cannot be read.

    Two call shapes carry it differently: ``create_subprocess_exec`` takes the
    program as its first positional argument, while the ``subprocess`` family
    takes one sequence whose first element it is. That sequence is often bound to
    a local name first (``cmd = [...]``), so a name is followed to the last
    literal assigned to it in the same scope -- without that step the idiom hides
    the binary from this audit entirely.
    """
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, (ast.List, ast.Tuple)):
        return _sequence_head(first)
    if isinstance(first, ast.Name):
        head: ast.expr | None = None
        for node in ast.walk(_enclosing_scope(tree, call)):
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(t, ast.Name) and t.id == first.id for t in node.targets):
                head = _sequence_head(node.value) or head
        return head
    return first


def _bare_binary_name(node: ast.expr | None) -> str | None:
    """The bare binary name a literal ``argv[0]`` spells, or None.

    A literal carrying a separator is already an absolute or relative path, which
    the loader does not resolve through PATH, so it is not this audit's concern.
    """
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    value = node.value
    if "/" in value or "\\" in value:
        return None
    return value if value in _DESKTOP_UI_BINARIES else None


def _spawn_call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _violations() -> list[str]:
    """``<relpath>:<line> <binary>`` for every bare desktop-UI spawn in src."""
    needles = tuple(quote + name + quote for name in _DESKTOP_UI_BINARIES for quote in ('"', "'"))
    root = src_root()
    found: list[str] = []
    for path, _text, tree in parsed_candidates(require_any=needles):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _spawn_call_name(node) not in _SPAWN_CALLS:
                continue
            binary = _bare_binary_name(_argv_head(tree, node))
            if binary is not None:
                rel = Path(path).relative_to(root).as_posix()
                found.append(f"{rel}:{node.lineno} {binary}")
    return sorted(found)


def test_no_desktop_ui_spawn_resolves_its_binary_off_path():
    """Each of these binaries reaches the spawn as an absolute path.

    Resolve it with ``platform_compat.trusted_system_bin(name)`` and treat
    ``None`` as a refusal. A bare-name fallback reinstates the hazard, so there is
    no allowlist here to add a site to.
    """
    bare = _violations()
    assert not bare, (
        "Desktop-UI spawns taking their binary from PATH (resolve each through "
        "platform_compat.trusted_system_bin and refuse on None):\n  " + "\n  ".join(bare)
    )


def _git_panel_argv_head() -> str | None:
    """The first element of the git panel's shared argv prefix, or None.

    Read from the source rather than imported: the value lives inside a closure in
    the file-diff handler, which no caller can reach without spawning git.
    """
    handlers = src_root() / "dashboard" / "handlers" / "files.py"
    tree = ast.parse(handlers.read_text(encoding="utf-8"), str(handlers))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_git" not in names or not isinstance(node.value, ast.List):
            continue
        head = node.value.elts[0] if node.value.elts else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def test_the_git_panel_spawn_is_a_recorded_residual():
    """The file-diff panel still takes ``git`` from PATH, and that is known.

    It is left alone because the remedy the dialog spawns use does not fit: the
    trusted lookup probes the system directories only, and a package-manager git
    is not there, so pinning it would answer "unavailable" on ordinary developer
    hosts. Closing it needs a pinned PATH for the child instead, the shape
    ``git_ops.TRUSTED_PATH`` already carries.

    This test states the residual so a reader finds it written down rather than
    inferred from the audit set's silence. When the panel is hardened, delete it.
    """
    assert _git_panel_argv_head() == "git"
