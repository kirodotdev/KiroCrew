"""A NEW test that spawns a subagent must not read the host's free memory.

``SubagentManager.spawn`` queues when the host is below ``agent.spawn_min_memory_gb``, and
``healthy_host_memory`` pins that reading only for files that ask. A ratchet, because a
queued spawn is still a ``SubagentInfo``: the test dies a line later on a bare ``KeyError``
with nothing naming memory.

Any module naming ``SubagentManager`` and calling ``.spawn(`` must be pinned or excluded
with a reason (``AcpRuntime.spawn`` has no memory gate). A second ratchet forbids a bare
``monkeypatch.undo()``, which reverts the fixture's pins too.
"""

from __future__ import annotations

import ast
import functools
import pathlib

import pytest

_TEST_DIR = pathlib.Path(__file__).resolve().parent

#: The fixture in ``test/conftest.py`` that pins the floor reading. A rename that
#: misses this file turns every module below into an unpinned one, so the
#: ratchet goes red rather than quietly stopping.
_FIXTURE = "healthy_host_memory"


@functools.lru_cache(maxsize=1)
def _spawning_modules() -> tuple[tuple[str, bool], ...]:
    """Every ``test/`` module that reaches ``spawn``, paired with whether it is pinned.

    Pinned means the fixture appears in a ``usefixtures(...)`` call at any scope. Cached:
    parsing ``test/``'s ~1,500 modules costs ~5s and both tests below need it.
    """
    found: list[tuple[str, bool]] = []
    for path in sorted(_TEST_DIR.glob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # A qualifying module must spell both names textually -- an AST node
        # named `SubagentManager` or a `.spawn(` call cannot be parsed from
        # source that lacks those characters -- so this is a necessary, not
        # sufficient, precondition and skips straight past every file that
        # cannot possibly match before paying for its parse.
        if "SubagentManager" not in text or "spawn" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
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
        # The tests OF the guard. Each patches ``check_memory_available`` in its
        # own body, to low AND to healthy, which is the behaviour under test.
        # Pinning the file would not break them -- an inner patch lands on top
        # -- but it would state a precondition the opposite of what they exist
        # to vary.
        "test_admission_gate.py": "drives the floor itself, to deferred and to admitted",
        # Runs the REAL memory check over fabricated /proc and cgroup files; the
        # pin would replace the check under test.
        "test_spawn_memory_cause.py": "feeds the real memory check fabricated kernel files",
        # A shared fake, not a collected test module: ``ManagerHarness`` pins
        # the host-memory reading itself for as long as it is open, so every
        # module that spawns through it is pinned without naming the fixture.
        "overload_fakes.py": "fake harness pins the host readings itself",
        # A shared run harness, not a collected test module: a module-level
        # ``pytestmark`` here would mark no test. Its one importer,
        # ``test_subagent_stop_reason_consistency.py``, carries the pin.
        "subagent_stop_reason_helpers.py": "harness; its importer pins the host readings",
        # A shared harness, not a collected test module: a module-level
        # ``pytestmark`` here would mark no test. Its importers, the five
        # ``test_queue_depth_reconcile*.py`` modules, each carry the pin.
        "queue_depth_reconcile_helpers.py": "harness; its importers pin the host readings",
        # A shared harness, not a collected test module: a module-level
        # ``pytestmark`` here would mark no test. Its importers, the three
        # ``test_subagent_turn_resilience*.py`` modules, each carry the pin.
        "subagent_turn_resilience_helpers.py": "harness; its importers pin the host readings",
        # A shared harness, not a collected test module: the helper methods the
        # ``test_admission_gate*.py`` spawn-gate classes share. Like
        # ``test_admission_gate.py``, its importers drive the floor themselves.
        "admission_gate_helpers.py": "harness; its importers drive the floor themselves",
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
            "spawn is queued on a memory-pressured runner and the test fails as a "
            "bare KeyError on the following line."
        )

    def test_the_exclusion_list_has_not_gone_stale(self) -> None:
        """An exclusion for a module that does not spawn hides the next one."""
        reached = {name for name, _pinned in _spawning_modules()}
        stale = sorted(name for name in self._EXCLUDED if name not in reached)

        assert not stale, f"_EXCLUDED names modules that no longer reach spawn: {stale}"


@functools.lru_cache(maxsize=1)
def _pinned_modules_that_undo() -> tuple[str, ...]:
    """Pinned spawning modules that also call a bare ``monkeypatch.undo()``.

    Textual precheck first, then an AST walk for a ``.undo()`` call taking no
    arguments, so a helper of the same name on some other object still counts --
    the hazard is the call shape, not which object it is spelled on.
    """
    pinned = {name for name, is_pinned in _spawning_modules() if is_pinned}
    offenders: list[str] = []
    for path in sorted(_TEST_DIR.glob("*.py")):
        if path.name not in pinned:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if ".undo()" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "undo"
            and not node.args
            and not node.keywords
            for node in ast.walk(tree)
        ):
            offenders.append(path.name)
    return tuple(offenders)


class TestTheHostPinSurvivesTheTestsOwnPatches:
    """A pinned module must not throw the pin away half way through a test.

    The test and its fixtures share one ``monkeypatch``, so ``monkeypatch.undo()`` reverts
    ``healthy_host_memory``'s pins and the rest reads real free memory. Use
    ``with monkeypatch.context() as scoped:`` instead.
    """

    def test_no_pinned_spawning_module_reverts_the_pin(self) -> None:
        offenders = _pinned_modules_that_undo()

        assert not offenders, (
            "these modules pin the host-memory reading and then throw it away "
            "with a bare monkeypatch.undo():\n    " + "\n    ".join(offenders) + "\n"
            "Wrap the patches the test wants reverted in "
            "`with monkeypatch.context() as scoped:` and set them on `scoped` "
            f'instead, so leaving the block restores "{_FIXTURE}"\'s readings '
            "rather than the runner's."
        )


@pytest.mark.usefixtures(_FIXTURE)
class TestThePinnedHostIsHealthyNotInfinite:
    """The pin answers as a real host with that much free memory would.

    A pin that admits whatever floor is asked hides every test whose reserve has
    outgrown the host: a wave of starts the operator's 8 GB machine would queue
    reads as admitted here. So the floor is still compared against the pinned
    figure, and only the reading itself is fixed.
    """

    def test_a_floor_above_the_pinned_host_is_refused(self) -> None:
        import kiro_crew.subagent as subagent

        assert subagent.check_memory_available(min_gb=9.0) == (False, 8.0)

    def test_a_floor_the_pinned_host_clears_is_admitted(self) -> None:
        import kiro_crew.subagent as subagent

        assert subagent.check_memory_available(min_gb=8.0) == (True, 8.0)
        assert subagent.check_memory_available() == (True, 8.0)
