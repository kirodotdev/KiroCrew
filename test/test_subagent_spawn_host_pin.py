"""A NEW test that spawns a subagent must not read the host's free memory.

``SubagentManager.spawn`` refuses -- returning before it registers anything in
``_tasks`` -- while the machine looks short of memory, and it does so twice: an
absolute floor (``check_memory_available`` against ``agent.spawn_min_memory_gb``)
and the posture tier (``cached_admission_check``, refusing while the
cgroup-clamped reading is CRITICAL). ``test/conftest.py``'s
``healthy_host_memory`` pins both, but only for a file that asks for it -- so
this is what stops the pinned set falling behind ``test/``.

A ratchet rather than a convention, because of how the failure reads: a refusal
IS a ``SubagentInfo`` -- a done one carrying ``error`` -- so
``assert info is not None`` still passes and the test dies on the NEXT line with
a bare ``KeyError`` naming an id nothing else mentions. Nothing in the traceback
says "memory"; the only evidence is a WARNING in the captured log. Measured on a
CI runner with ~0.5 GB free, on a PR that touched none of this.

Deliberately WIDER than the fixture acts on, the same way
``test_host_isolation_floor.py``'s ratchet is: a module that names
``SubagentManager`` AND calls ``.spawn(`` must be either pinned or excluded with
a reason, so adding one forces a decision instead of an omission. Files whose
``spawn`` is ``AcpRuntime.spawn`` never name ``SubagentManager`` and so are not
swept up -- that method has no memory gate.
"""

from __future__ import annotations

import ast
import functools
import pathlib

_TEST_DIR = pathlib.Path(__file__).resolve().parent

#: The fixture in ``test/conftest.py`` that pins both guards. A rename that
#: misses this file turns every module below into an unpinned one, so the
#: ratchet goes red rather than quietly stopping.
_FIXTURE = "healthy_host_memory"


@functools.lru_cache(maxsize=1)
def _spawning_modules() -> tuple[tuple[str, bool], ...]:
    """Every ``test/`` module that reaches ``spawn``, paired with whether it is pinned.

    A module qualifies when it names ``SubagentManager`` anywhere and calls some
    ``.spawn(``. Pinned means the fixture appears inside a ``usefixtures(...)``
    call -- at module, class, or test scope, so a file that mixes guard tests
    with spawning ones can opt in narrowly.

    Cached and returned as a tuple: parsing ``test/``'s ~1,500 modules costs
    ~5s, and both tests below need the same answer.
    """
    found: list[tuple[str, bool]] = []
    for path in sorted(_TEST_DIR.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {a.attr for a in ast.walk(tree) if isinstance(a, ast.Attribute)}
        if "SubagentManager" not in names:
            continue
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        spawns = any(
            isinstance(node.func, ast.Attribute) and node.func.attr == "spawn" for node in calls
        )
        if not spawns:
            continue
        pinned = any(
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "usefixtures"
            and any(isinstance(arg, ast.Constant) and arg.value == _FIXTURE for arg in node.args)
            for node in calls
        )
        found.append((path.name, pinned))
    return tuple(found)


class TestTheSpawnHostMemoryPinRatchet:
    """A new ``SubagentManager.spawn`` caller must not land unpinned."""

    #: Modules that reach ``spawn`` and deliberately need no pin. Each states
    #: why, in the same spirit as ``test_host_isolation_floor.py``'s
    #: ``_EXCLUDED``.
    _EXCLUDED: dict[str, str] = {
        # The tests OF the two guards. Each patches ``check_memory_available``
        # and ``cached_admission_check`` in its own body, to refused AND to
        # admitted, which is the behaviour under test. Pinning the file would
        # not break them -- an inner patch lands on top -- but it would state a
        # precondition the opposite of what they exist to vary.
        "test_admission_gate.py": "drives both guards itself, to refused and to admitted",
    }

    def test_every_spawning_module_is_pinned_or_excluded(self) -> None:
        modules = _spawning_modules()
        assert modules, "the scan found no spawning modules — it has stopped matching"

        unhandled = sorted(
            name for name, pinned in modules if not pinned and name not in self._EXCLUDED
        )

        assert not unhandled, (
            "these test modules drive SubagentManager.spawn without pinning the "
            "host-memory reading:\n    " + "\n    ".join(unhandled) + "\n"
            f'Add `pytestmark = pytest.mark.usefixtures("{_FIXTURE}")` at module '
            "scope, or exclude the file in _EXCLUDED and say why. Unpinned, the "
            "spawn is refused on a memory-pressured runner and the test fails as a "
            "bare KeyError on the following line."
        )

    def test_the_exclusion_list_has_not_gone_stale(self) -> None:
        """An exclusion for a module that no longer spawns hides the next one."""
        reached = {name for name, _pinned in _spawning_modules()}
        stale = sorted(name for name in self._EXCLUDED if name not in reached)

        assert not stale, f"_EXCLUDED names modules that no longer reach spawn: {stale}"
