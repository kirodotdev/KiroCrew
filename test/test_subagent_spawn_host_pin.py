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

A second ratchet below covers the other half: being pinned is not enough if the
test throws the pin away mid-body with a bare ``monkeypatch.undo()``, which
reverts the fixture's patches too because pytest hands the test and the fixture
the same ``monkeypatch`` instance.
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
        # The tests OF the two guards. Each patches ``check_memory_available``
        # and ``cached_admission_check`` in its own body, to refused AND to
        # admitted, which is the behaviour under test. Pinning the file would
        # not break them -- an inner patch lands on top -- but it would state a
        # precondition the opposite of what they exist to vary.
        "test_admission_gate.py": "drives both guards itself, to refused and to admitted",
        # A shared fake, not a collected test module: ``ManagerHarness`` pins
        # both host-memory readings itself for as long as it is open, so every
        # module that spawns through it is pinned without naming the fixture.
        "overload_fakes.py": "fake harness pins the host readings itself",
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

    pytest hands the test function and every fixture it requests the SAME
    ``monkeypatch`` instance, so ``monkeypatch.undo()`` inside a test body
    reverts ``healthy_host_memory``'s two pins along with the test's own
    patches. Everything after that line reads the runner's real free memory,
    which is the exact state the module-scope pin exists to remove -- so the
    file is pinned, looks pinned, and is not pinned where it matters.

    Measured on a macos-15 nightly backend shard reading 2.58 GB available,
    under the 4.5 GB floor: ``test_taskq_admission_integration.py``'s drain
    deferred a second time. The only failure text was
    ``assert 'queued' == 'starting'``; nothing named memory, and the whole
    nightly publish chain was skipped behind it.

    The remedy is a scoped patch -- ``with monkeypatch.context() as scoped:`` --
    which reverts only what the block set and leaves the fixture's readings in
    place.
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
