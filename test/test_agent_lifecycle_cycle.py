"""``session_pid`` must not depend on the ACP layer at import time.

``session_pid`` is the one agent-lifecycle concern the ACP layer still owns:
``acp/runtime.py`` and ``acp/client.py`` drive every track/untrack transition.
That direction is fine.  The reverse direction is not, and until recently
``session_pid.py`` imported ``kiro_crew.providers.base`` at module scope for a
single parameter annotation, which closed a real cycle::

    session_pid -> providers.base -> acp.types -> acp/__init__
                -> acp.runtime -> session_pid

The cycle was not theoretical.  Importing ``kiro_crew.session_pid`` as the
first ``kiro_crew`` module raised ``ImportError: cannot import name
'_track_pid' from partially initialized module``, so the module was only ever
importable because something else had already imported the ACP package first.
Other leaves paid for it with ``LLMProvider = Any`` runtime stubs and
function-local imports carrying ``# circular import`` comments.

Note what the fix is NOT.  Moving the import under ``if TYPE_CHECKING:`` closes
the cycle but is refused by ``scripts/check_agent_sdk_boundary.py``, which counts
a type-only import as boundary knowledge by design and offers no opt-out marker.
The annotation was simply wrong: ``_sync_kill_provider`` reads three private
attributes through ``getattr(..., None)`` that the provider ABC does not declare,
so the parameter is ``object`` and the edge is gone rather than exempted.

These tests pin that, and they are the criterion for the cycle being closed.  A
module-scope import of ``kiro_crew.providers`` or ``kiro_crew.acp`` added back to
``session_pid.py`` fails them -- including one added lazily, since the second test
checks what the import actually loaded rather than what the source says.

Both run in a fresh interpreter on purpose.  Inside the pytest process the ACP
package is already in ``sys.modules``, so the cycle cannot be observed and an
in-process assertion would pass vacuously.  The child inserts this repository's
``src/`` at the front of ``sys.path`` rather than trusting ``PYTHONPATH``: an
editable install may point at a different checkout, and this test must measure
the tree it ships in.

See docs/request-for-change/rfc-crew-agent-sdk-boundary.md, PR 4.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from kiro_crew.subprocess_utf8 import UTF8_TEXT

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"

_FORBIDDEN_ROOTS = ("kiro_crew.acp", "kiro_crew.providers")

# The child prefixes its answer, because anything ELSE that reaches stdout --
# an import that prints, a library banner -- would otherwise read as a leaked
# module name and fail the suite for the wrong reason.
_ANSWER = "ANSWER:"
_PRELUDE = f"import sys; sys.path.insert(0, {str(_SRC)!r})\n"

# -I isolates the child (no PYTHONPATH, no user site) so it measures this tree
# and not an editable install's.  -B is separate and NOT implied by it: without
# it the child compiles the kiro_crew modules it imports and writes __pycache__
# into src/, so a read-only assertion would leave artifacts in the checkout it
# was inspecting.  -I also implies -E, so PYTHONDONTWRITEBYTECODE cannot do this
# job -- the flag is the only lever.  Keep both;
# test_the_child_refuses_to_write_bytecode pins the second.
_CHILD_FLAGS = ("-I", "-B")


def _run(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter pinned to this repository's ``src/``."""
    return subprocess.run(
        [sys.executable, *_CHILD_FLAGS, "-c", _PRELUDE + code],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        timeout=120,
        **UTF8_TEXT,
    )


def _answers(proc: subprocess.CompletedProcess[str]) -> list[str]:
    """The child's sentinel-prefixed lines only, with the sentinel stripped."""
    return [
        line[len(_ANSWER) :].strip()
        for line in proc.stdout.splitlines()
        if line.startswith(_ANSWER)
    ]


def test_session_pid_imports_standalone() -> None:
    """Importing it first must work, not just importing it after the ACP layer."""
    proc = _run("import kiro_crew.session_pid")
    assert proc.returncode == 0, (
        "importing kiro_crew.session_pid as the first kiro_crew module failed, which "
        "means a module-scope import from it re-enters a package that imports it back. "
        "The historical offender was `from kiro_crew.providers.base import LLMProvider`, "
        "carried for one annotation that the ABC did not actually describe. Drop the "
        "import rather than deferring it under `if TYPE_CHECKING:` -- the boundary gate "
        "counts a type-only import too, and has no opt-out marker.\n"
        f"stderr:\n{proc.stderr}"
    )


def test_session_pid_pulls_in_no_agent_layer_module() -> None:
    """It must load without the ACP or provider packages coming with it."""
    proc = _run(
        "import kiro_crew.session_pid; "
        f"print({_ANSWER!r} + ' '.join(sorted(m for m in sys.modules "
        "if m.startswith('kiro_crew.acp') or m.startswith('kiro_crew.providers'))))"
    )
    assert proc.returncode == 0, proc.stderr
    answers = _answers(proc)
    assert len(answers) == 1, f"expected one sentinel line, got {answers!r} from {proc.stdout!r}"
    leaked = answers[0].split()
    assert not leaked, (
        "importing kiro_crew.session_pid dragged in agent-layer modules: "
        f"{leaked}. session_pid is a leaf the ACP layer drives, not the other way "
        "round; it should name no type from there at all."
    )


def test_the_child_measures_this_checkout() -> None:
    """Guard the two tests above against silently measuring another tree.

    An editable install can put a different checkout's ``src/`` on the path, and
    that is exactly how a first draft of this file reported a failure from a
    sibling worktree.
    """
    proc = _run(f"import kiro_crew.session_pid as m; print({_ANSWER!r} + m.__file__)")
    assert proc.returncode == 0, proc.stderr
    assert _answers(proc) == [str(_SRC / "kiro_crew" / "session_pid.py")], proc.stdout


def test_the_child_refuses_to_write_bytecode() -> None:
    """A read-only assertion must not write into the tree it inspects.

    ``-I`` does not imply ``-B``, so an earlier draft compiled every module the
    child imported straight into ``src/**/__pycache__``.

    This asks the child what it will DO rather than diffing the directory
    afterwards, and the difference matters: pytest imports most of ``kiro_crew``
    during collection, so by the time this test runs the ``.pyc`` files already
    exist and a child writing bytecode adds no new file. A mtime-and-filename
    diff therefore passes with ``-B`` removed -- measured, not assumed -- which
    makes it exactly the vacuous test this suite's other self-checks exist to
    prevent.
    """
    proc = _run(f"print({_ANSWER!r} + str(sys.dont_write_bytecode))")
    assert proc.returncode == 0, proc.stderr
    assert _answers(proc) == ["True"], (
        "the child interpreter will write bytecode into the checkout it is only "
        f"meant to read (sys.dont_write_bytecode={_answers(proc)}). Keep -B in "
        "_CHILD_FLAGS -- -I implies -E, so PYTHONDONTWRITEBYTECODE cannot substitute."
    )


def test_the_guard_names_roots_that_exist() -> None:
    """A typo in the forbidden prefixes would make both tests above vacuous."""
    for root in _FORBIDDEN_ROOTS:
        pkg = _SRC / Path(*root.split("."))
        assert (pkg / "__init__.py").is_file(), f"{root} is not a package under src/"
