"""The rootdir conftest's isolation floor guards itself.

``test_host_service_guard.py`` covers the SERVICE half of that floor. This file covers
the three halves added around it, each of which protects a different piece of the
operator's machine from a test that forgot to isolate itself:

* the **data home** -- ``KIROCREW_HOME`` pinned per test, plus the ``~/.kiro`` paths
  production binds at IMPORT time, which the env var cannot reach;
* the **system temp directory** -- ``tempfile``'s base redirected per run, with residue
  reported rather than silently accumulated;
* the **worker budget** -- how many xdist workers the host can actually back.

Two jobs, the same split ``test_host_service_guard.py`` uses:

* **Behaviour** -- prove each guard is armed, catches what it claims, and stays silent
  on what it must not touch. A guard nobody exercises is a guard that stops working at
  the next refactor without anybody noticing.
* **Ratchet** -- pin the guarded set against what ``src/kiro_crew`` actually contains,
  so a NEW import-time ``Path.home()`` binding cannot land unpinned. Same shape as
  ``test_host_service_guard.py``'s ratchet and ``test_spawn_preexec_guard.py``'s
  ``_ALLOWED``.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import sys
import tempfile

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ROOT_CONFTEST = _REPO_ROOT / "conftest.py"
_SRC = _REPO_ROOT / "src" / "kiro_crew"


def _load_root_conftest():
    """Import the rootdir conftest under its own module name.

    pytest already loads it as a plugin, but reaching it through the plugin manager
    depends on the name pytest happened to register. Loading it by path is
    deterministic, and the fixtures it defines are inert in this namespace (a
    ``@pytest.fixture`` decorator only marks a function; nothing collects them here).
    """
    spec = importlib.util.spec_from_file_location("_kirocrew_isolation_conftest", _ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_root = _load_root_conftest()


#: The real directories the isolation floor exists to keep tests out of.
#:
#: Deliberately these specific roots and NOT ``Path.home()``. On POSIX "not under
#: $HOME" reads as the stronger assertion, but on Windows ``tempfile.gettempdir()`` is
#: ``%LOCALAPPDATA%\\Temp`` -- i.e. ``C:\\Users\\<user>\\AppData\\Local\\Temp`` -- which is
#: itself under the home directory. So "not under $HOME" is unconditionally FALSE there
#: for every correctly-isolated tmp path, and the assertion could never pass on the
#: Windows shards. These roots are what the fixtures actually protect, and the narrower
#: form is true on all four targets.
_GUARDED_ROOTS: tuple[pathlib.Path, ...] = (
    pathlib.Path.home() / ".kiro",
    pathlib.Path.home() / ".kirocrew",
    pathlib.Path.home() / ".claude.json",
)


def _inside_a_guarded_root(path: pathlib.Path) -> bool:
    """Whether *path* is, or is under, one of the operator's real guarded paths."""
    resolved = path.resolve()
    for root in _GUARDED_ROOTS:
        candidate = root.resolve()
        if resolved == candidate or resolved.is_relative_to(candidate):
            return True
    return False


# ── the data home ─────────────────────────────────────────────────────────


class TestTheDataHomeIsPinnedForEveryTestpath:
    """``KIROCREW_HOME`` must be a tmp dir here, and it must be the SAME one the
    package resolves.

    These assertions run against the LIVE fixtures rather than a reconstruction,
    because the thing worth pinning is that the autouse chain actually fired. The
    stakes are specific: ``config_dir()`` is not a read -- it CREATES the home and its
    marker on first use and can run the one-time ``~/.kirocrew`` -> ``~/.kiro/crew``
    migration as a side effect. A test that resolves it unpinned mutates the
    operator's live install.
    """

    def test_kirocrew_home_is_not_the_operators_real_home(self) -> None:
        home = pathlib.Path(os.environ["KIROCREW_HOME"]).resolve()

        assert not _inside_a_guarded_root(home), f"KIROCREW_HOME is a real home path: {home}"

    def test_config_dir_resolves_to_that_same_pinned_home(self) -> None:
        """The env var is only worth pinning if the package actually follows it.

        ``config_dir()`` memoises its answer in a module global for the process
        lifetime, so this also proves the per-test reset of ``_resolved_home`` works --
        without it a home cached by an earlier test on this xdist worker would win.
        """
        from kiro_crew.config.paths import config_dir

        assert config_dir().resolve() == pathlib.Path(os.environ["KIROCREW_HOME"]).resolve()

    def test_a_test_can_still_override_the_home_itself(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor is a safety net, not a cage: a test that isolates itself wins."""
        from kiro_crew.config.paths import config_dir

        mine = tmp_path / "my-own-home"
        mine.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(mine))
        monkeypatch.setattr("kiro_crew.config.paths._resolved_home", None)

        assert config_dir().resolve() == mine.resolve()


class TestTheSharedKiroPathsArePinned:
    """``~/.kiro`` is kiro-cli's own home -- machine-wide, shared with the real agent.

    A test that writes ``~/.kiro/settings/mcp.json`` edits the MCP servers of the
    operator's LIVE agent, and ``KIROCREW_HOME`` does not help: these paths are bound
    at import time from ``Path.home()``, before any test could set an env var.
    """

    @pytest.mark.parametrize(
        ("module", "attr"),
        [(module, attr) for module, attr, _ in _root._SHARED_KIRO_PATHS],
    )
    def test_each_pinned_path_is_outside_the_real_home(self, module: str, attr: str) -> None:
        importlib.import_module(module)
        value = pathlib.Path(getattr(sys.modules[module], attr))

        assert not _inside_a_guarded_root(value), (
            f"{module}.{attr} still resolves inside the operator's real "
            f"kiro-cli/data home: {value.resolve()}"
        )

    def test_the_mcp_lock_stays_a_sibling_of_the_mcp_json(self) -> None:
        """A derived pair must be redirected together, or it is worse than neither.

        ``_McpFileLockSync.__enter__`` creates ``_GLOBAL_MCP_JSON.parent`` and then
        touches ``_MCP_LOCK_PATH``. Redirect only the json and the code creates a tmp
        directory, then touches a lock in the REAL one whose parent nothing created --
        ``FileNotFoundError`` on any host where ``~/.kiro/settings`` does not already
        exist. Pinning both is not enough on its own: they must land in the SAME
        directory, which is what this asserts.
        """
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        assert mcp_mod._MCP_LOCK_PATH.parent == mcp_mod._GLOBAL_MCP_JSON.parent

    def test_the_table_names_a_real_attribute_on_a_real_module(self) -> None:
        """A renamed constant would make its entry a silent no-op.

        The fixture patches with ``raising=False`` so a partial checkout cannot break
        collection, which is right for the fixture and exactly why the assertion has to
        live here instead.
        """
        for module, attr, _relative in _root._SHARED_KIRO_PATHS:
            imported = importlib.import_module(module)
            assert hasattr(imported, attr), f"{module} has no attribute {attr!r}"


class TestTheSharedKiroPathRatchet:
    """A NEW import-time ``Path.home()`` binding must not land unpinned.

    The fixture can only redirect what its table names, so the table is the guarded
    set and this is what stops it from silently falling behind ``src/``. Deliberately
    WIDER than the fixture acts on: an entry has to be either pinned or explicitly
    excluded with a reason, so adding one forces a decision rather than an omission.
    """

    #: Import-time ``Path.home()`` bindings that deliberately need no redirect.
    #: Each entry states why, in the same spirit as
    #: ``test_spawn_preexec_guard.py``'s ``_ALLOWED``.
    #:
    #: Note the two shapes here are excluded for OPPOSITE reasons: the launchd paths
    #: are already redirected somewhere else, while the security anchors must NOT be
    #: redirected at all.
    _EXCLUDED: dict[tuple[str, str], str] = {
        # Already redirected, by the rootdir conftest's own ``_isolate_launchd_paths``
        # fixture. It has to move the whole macOS launchd set together (PLIST_DIR,
        # PLIST_PATH, LOG_DIR, STDOUT_LOG, STDERR_LOG, LIVE_PROGRAM) because both
        # consumers import them by value.
        ("kiro_crew/service/macos.py", "PLIST_DIR"): "covered by _isolate_launchd_paths",
        ("kiro_crew/service/macos.py", "LOG_DIR"): "covered by _isolate_launchd_paths",
        # NOT a data path -- a security MATCHER compiled from the real home. It exists
        # to refuse `tar -C ~/.kiro/crew`, which can drop a `security_policy.json` or a
        # `profiles/` entry into the governance trust root. Pointing it at a tmp dir
        # would make every test that exercises it assert against a pattern that no
        # longer matches the thing it protects -- weakening the guard to satisfy an
        # isolation ratchet, which is backwards.
        ("kiro_crew/security.py", "_EXTRACT_INTO_TRUST_ROOT_RE"): "security anchor: must name the REAL home",
        # Also not redirectable: the home-anchoring IS the security property. These name
        # OTHER products' credential stores (kiro-cli, amazon-q), and the module's own
        # comment records that an entry either equals the home-anchored path inside
        # `_SENSITIVE_HOME_DIRS`' fence or falls outside it and must not be trusted --
        # so a redirected value would manufacture a forgeable "trusted" path. They are
        # only ever READ, and `test_kiro_usage_api.py` stubs the tuples per test, which
        # is the right seam: stub the READER, never move the anchor.
        ("kiro_crew/dashboard/handlers/kiro_usage_api.py", "_CLI_SQLITE_DBS"): "security anchor: must name the REAL home",
        ("kiro_crew/dashboard/handlers/kiro_usage_api.py", "_OTHER_SQLITE_DBS"): "security anchor: must name the REAL home",
        # An ALLOW-LIST root, so the same rule applies from the other direction: the
        # file browser's first permitted root is the operator's real home BY DESIGN,
        # since that is the directory the user is entitled to browse. Redirecting it
        # would make every containment test assert against a root that does not ship.
        # Nothing here writes: the module reads the value to bound path resolution.
        ("kiro_crew/apps/builtins/file_explorer/server.py", "_HOME"): "security anchor: the browsing allow-list root",
    }

    @staticmethod
    def _home_bindings() -> dict[tuple[str, str], int]:
        """Every module-level assignment whose value calls ``Path.home()``.

        Parsed rather than grepped so a multi-line or parenthesised expression is
        found too, and so a ``Path.home()`` inside a FUNCTION -- which is re-evaluated
        per call and therefore already follows a patched home -- is correctly ignored.

        Deliberately catches only a DIRECT call. A binding derived from another
        (``PLIST_PATH = PLIST_DIR / "x.plist"``) is invisible here, so this is a
        tripwire for the common shape rather than a completeness proof: pinning the
        root of such a chain does not pin the leaves, which is why
        ``_isolate_launchd_paths`` enumerates its whole set by hand.
        """
        found: dict[tuple[str, str], int] = {}
        for path in sorted(_SRC.rglob("*.py")):
            if "_vendor" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):  # pragma: no cover - unreadable source
                continue
            # Module SCOPE, not just ``tree.body``. The real cut is a FUNCTION or CLASS
            # body, which is re-evaluated per call and so is not a value frozen at
            # import. Note that is NOT the same as "already isolated": the floor pins
            # neither ``Path.home()`` nor ``$HOME``, so a LAZY resolver
            # (``config.paths.kiro_home()`` and its callers) still names the operator's
            # real ``~/.kiro`` and this ratchet does not cover it. What this guards is
            # precisely the import-time shape. Every other nested statement -- a
            # module-level ``try:`` or a platform ``if:``, which is the normal shape for
            # cross-platform code here -- still runs exactly once at import, so the
            # "re-evaluated per call" reason for skipping it does not apply. Excluding
            # the two scope-opening node kinds rather than enumerating control-flow
            # kinds means ``match``, ``with`` and anything a later Python adds are
            # covered without another edit.
            pending: list[ast.stmt] = list(tree.body)
            while pending:
                node = pending.pop(0)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                pending.extend(
                    child for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt)
                )
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                # A bare ``x: Path`` annotation binds nothing, and walking None raises.
                if node.value is None:
                    continue
                calls_home = any(
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "home"
                    for inner in ast.walk(node.value)
                )
                if not calls_home:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        rel = path.relative_to(_REPO_ROOT / "src").as_posix()
                        found[(rel, target.id)] = node.lineno
        return found

    def test_every_import_time_home_binding_is_pinned_or_excluded(self) -> None:
        pinned = {
            (module.replace(".", "/") + ".py", attr)
            for module, attr, _relative in _root._SHARED_KIRO_PATHS
        }
        unhandled = {
            key: line
            for key, line in self._home_bindings().items()
            if key not in pinned and key not in self._EXCLUDED
        }

        assert not unhandled, (
            "these module-level Path.home() bindings are neither pinned by the rootdir "
            "conftest's _SHARED_KIRO_PATHS nor excluded with a reason in _EXCLUDED:\n"
            + "\n".join(f"    {mod}:{line} {attr}" for (mod, attr), line in sorted(unhandled.items()))
            + "\nA test that reaches one of these writes the operator's real home. Pin it, "
            "or exclude it and say why."
        )

    def test_the_exclusion_list_has_not_gone_stale(self) -> None:
        """An exclusion for a binding that no longer exists hides the next one."""
        bindings = self._home_bindings()
        stale = [key for key in self._EXCLUDED if key not in bindings]

        assert not stale, f"_EXCLUDED names bindings that no longer exist: {stale}"


# ── the system temp directory ─────────────────────────────────────────────


class TestTheTempBaseIsRedirected:
    """``tempfile``'s base must be a per-run directory, not the shared temp root.

    The point is not tidiness. A bare ``mkdtemp()`` whose cleanup is missing or skipped
    used to leave its directory in the platform temp root forever, and MEASURED on the
    hosts this was written against, ``/tmp`` is a tmpfs with a hard 1,048,576-INODE cap
    that returns ENOSPC to unrelated processes while 90% of the BYTES are still free.
    """

    @pytest.mark.skipif(
        bool(os.environ.get("KIROCREW_TMP_PER_TEST")),
        reason="per-test diagnostic mode nests the base one level deeper on purpose",
    )
    def test_gettempdir_is_this_runs_own_root(self) -> None:
        base = pathlib.Path(tempfile.gettempdir())

        assert base.name.startswith(_root._TMP_ROOT_PREFIX), (
            f"tempfile base is {base}, not a {_root._TMP_ROOT_PREFIX}* root -- the "
            "redirect did not take effect"
        )
        assert base.is_dir()

    def test_pytests_own_basetemp_is_not_inside_the_redirect(
        self, tmp_path: pathlib.Path
    ) -> None:
        """pytest resolves basetemp lazily from ``gettempdir()``, so ORDER decides this.

        If the redirect wins the race, pytest's whole basetemp lands inside the run's
        temp root -- which the session teardown deletes, taking every failed test's
        retained ``tmp_path`` with it, and adding ~25 characters to every temp path in
        the suite. ``_isolate_tempfile_base`` forces ``getbasetemp()`` before
        redirecting to make that impossible; this is what keeps it that way.
        """
        assert not tmp_path.resolve().is_relative_to(
            pathlib.Path(tempfile.gettempdir()).resolve()
        ), f"pytest basetemp {tmp_path} is inside the redirected temp root"

    def test_a_mkdtemp_with_no_dir_argument_lands_inside_it(self) -> None:
        made = pathlib.Path(tempfile.mkdtemp())
        try:
            assert made.parent == pathlib.Path(tempfile.gettempdir())
        finally:
            made.rmdir()

    def test_the_env_vars_carry_the_redirect_to_child_processes(self) -> None:
        """A child re-derives its own temp dir, so the global alone is not enough.

        All three names are set because the platforms disagree on which is real:
        ``TMPDIR`` on POSIX, ``TEMP``/``TMP`` on Windows.
        """
        base = tempfile.gettempdir()

        for name in _root._TMP_ENV_VARS:
            assert os.environ.get(name) == base, f"{name} does not carry the redirect"

    def test_the_root_name_carries_the_account_and_the_pid(self) -> None:
        """A bare pid collides across accounts: POSIX shares one temp root.

        Two accounts can hold the same pid simultaneously, so the account segment is what
        keeps one run's root distinct from another's. The pid is carried for a HUMAN
        reading a stray directory -- nothing parses it, and no code reclaims a root it did
        not create.
        """
        prefix = _root._tmp_root_prefix_for_run()

        assert prefix.startswith(_root._TMP_ROOT_PREFIX)
        assert prefix.endswith(f"-{os.getpid()}-")
        # the account segment sits between the two, and is non-empty
        assert len(prefix) > len(_root._TMP_ROOT_PREFIX) + len(str(os.getpid())) + 2

    def test_the_root_is_created_atomically_and_owner_only(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A predictable name in a world-writable temp root is a hijack.

        Another local account can pre-create the exact pid-derived name as a SYMLINK to a
        directory it controls; ``mkdir(exist_ok=True)`` adopts it, the redirect follows it,
        and every temp write in the run lands somewhere that account chose and can read.
        ``mkdtemp`` closes it three ways: a random component nobody can guess ahead of
        time, ``O_EXCL`` so nothing existing is adopted, and mode 0o700.
        """
        made = _root._create_tmp_root(tmp_path)

        assert made.is_dir() and not made.is_symlink()
        assert made.parent == tmp_path
        assert made.name.startswith(_root._tmp_root_prefix_for_run())
        # A random component after the pid is what makes the name unguessable.
        assert made.name != _root._tmp_root_prefix_for_run().rstrip("-")
        if os.name != "nt":  # POSIX mode bits; Windows uses ACLs
            assert (made.stat().st_mode & 0o777) == 0o700

    def test_two_roots_in_the_same_process_never_collide(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Which also proves nothing pre-existing is ever adopted."""
        first = _root._create_tmp_root(tmp_path)
        second = _root._create_tmp_root(tmp_path)

        assert first != second


class TestTheTempResidueReport:
    """Residue must be REPORTED, not just relocated somewhere pytest prunes."""

    def test_it_names_what_was_left_behind(self, tmp_path: pathlib.Path) -> None:
        message = _root._tmp_residue_report(tmp_path, ["tmpleaked"], per_test=False)

        assert "tmpleaked" in message
        # The fix belongs in the message: an rmtree in tearDown is the shape that
        # leaks, because unittest skips tearDown entirely when setUp raises.
        assert "addCleanup" in message
        assert _root._TMP_PER_TEST_ENV in message

    def test_a_third_party_or_by_design_entry_is_not_residue(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A guard that cries wolf gets deleted, and then it protects nothing.

        Redirecting `tempfile`'s base also redirects every CHILD's, so the browser and
        its driver put their scratch here too, and production's deliberately-persistent
        screenshot spool lands here rather than in the real temp root. None of that is a
        test forgetting to clean up.
        """
        for name in ("kirocrew-computer-shots", "playwright-transform-cache-1001",
                     ".org.chromium.Chromium.AHpK6x"):
            (tmp_path / name).mkdir()

        assert _root._tmp_residue(tmp_path, per_test=False) == []

    def test_it_stays_silent_when_nothing_was_left(self, tmp_path: pathlib.Path) -> None:
        assert _root._tmp_residue(tmp_path, per_test=False) == []

    def test_a_nested_pytests_basetemp_is_not_residue(self, tmp_path: pathlib.Path) -> None:
        """Several tests spawn a nested pytest, which computes its own basetemp inside
        ours because it resolves ``gettempdir()`` after the redirect. That is a child
        runner's bookkeeping, with its own retention, not residue this suite dropped."""
        (tmp_path / "pytest-of-someone").mkdir()

        assert _root._tmp_residue(tmp_path, per_test=False) == []

    def test_per_test_mode_reports_the_leaf_not_the_test_directory(
        self, tmp_path: pathlib.Path
    ) -> None:
        """In per-test mode the immediate children are bases the fixture itself made.

        Reporting those would name every test in the run as its own leak and answer
        nothing -- the whole point of the mode is that the leaf's PARENT is the test id.
        """
        (tmp_path / "test_guilty").mkdir()
        (tmp_path / "test_guilty" / "tmpleaked").mkdir()
        (tmp_path / "test_innocent").mkdir()

        assert _root._tmp_residue(tmp_path, per_test=True) == ["test_guilty/tmpleaked"]

    def test_an_unreadable_base_is_not_reported_as_residue(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A guard that reds the suite on an unanswerable question gets deleted, and
        then it protects nothing."""
        assert _root._tmp_residue(tmp_path / "does-not-exist", per_test=False) == []


# ── the process working directory ──────────────────────────────────────────


#: Written by one test and asserted by the next: a failure raised inside a finalizer
#: is reported as a teardown error against an innocent test id.
_CWD_ORDER: dict[str, str] = {}


class TestTheWorkingDirectoryIsRestored:
    """The CWD is per-PROCESS, so one test's ``os.chdir`` is every later test's start.

    Survivable only while the directory outlived the run. With
    ``tmp_path_retention_policy = failed`` a passing test's ``tmp_path`` is removed at that
    test's own teardown, so a leaked CWD leaves the worker in a DELETED directory and
    ``Path.cwd()`` then raises ``FileNotFoundError`` in every later test that reaches it --
    including inside production code (``TaskRunner.__init__`` does ``work_dir or Path.cwd()``).
    """

    def test_this_test_starts_somewhere_real(self) -> None:
        """Which is only true if no earlier test on this worker leaked its directory."""
        assert pathlib.Path.cwd().is_dir()

    def test_a_chdir_is_undone_before_fixture_finalizers_run(
        self, tmp_path: pathlib.Path, request: pytest.FixtureRequest
    ) -> None:
        """ORDER, not just eventual restoration -- and it is load-bearing on Windows.

        An outer autouse FIXTURE would tear down LAST, after ``tmp_path`` cleanup had
        already tried to remove a directory the process was still sitting in; Windows
        refuses to delete its own working directory, so that cleanup fails there. A
        ``tryfirst`` ``pytest_runtest_teardown`` hookimpl runs before the default one,
        which is what performs fixture finalization -- so a finalizer registered here
        observes the CWD already restored.

        The observation is asserted by the NEXT test rather than in a finalizer, because a
        failure raised inside a finalizer is reported as a teardown error against an
        innocent-looking test id.
        """
        _CWD_ORDER["expected"] = os.getcwd()
        request.addfinalizer(lambda: _CWD_ORDER.__setitem__("at_finalizer", os.getcwd()))

        os.chdir(tmp_path)
        assert pathlib.Path.cwd().samefile(tmp_path)

    def test_the_finalizer_saw_the_cwd_already_restored(self) -> None:
        """Reads what the previous test recorded. Ordered by definition order in the file."""
        assert _CWD_ORDER.get("at_finalizer") == _CWD_ORDER.get("expected"), (
            f"CWD at fixture-finalizer time was {_CWD_ORDER.get('at_finalizer')!r}, "
            f"expected {_CWD_ORDER.get('expected')!r} -- the restore ran too late, so "
            "tmp_path cleanup would be asked to delete the process's own directory"
        )


# ── the worker budget ─────────────────────────────────────────────────────


class TestTheWorkerBudgetIsMemoryBounded:
    """How many xdist workers the host can actually back, not just how many cores.

    Cores alone oversubscribe: two worktrees each taking 10 workers on a 10-core box
    once produced a load average of ~590 with zero tests completing in 21 minutes. But
    TOTAL RAM alone is also the wrong number -- it is the MACHINE's, so it over-reports
    inside a memory-capped container and says nothing about a host already using most
    of its memory for something else.

    The failure mode that matters most here is the inverted one: a reading that comes
    back wrongly SMALL collapses the whole run to one worker, which looks like a hang
    rather than a bug. So each reading must degrade to "skip this bound", never to zero.
    """

    @pytest.fixture
    def slot_dir_env(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
        """Point the host-global slot directory at a tmp dir, and release what is taken.

        Mirrors ``test_xdist_host_budget.py``'s ``slot_dir``, and both halves matter. The
        real slot directory is under ``~/.cache``, shared with every other run on the
        machine, so a test that claims slots must not compete there. And a claim HOLDS an
        open file descriptor for the process lifetime by design -- that is how the kernel
        owns the lease -- so the descriptors have to be closed here or the test keeps real
        capacity for the rest of the session, and on Windows the open handle also blocks
        ``tmp_path`` cleanup and fails teardown.

        ``_held_slots`` is REPLACED rather than cleared, so the suite's own list is never
        touched and ``monkeypatch`` restores it even if the test fails.
        """
        import conftest as suite_conftest

        monkeypatch.setenv(suite_conftest._SLOT_DIR_ENV, str(tmp_path / "slots"))
        held: list[int] = []
        monkeypatch.setattr(suite_conftest, "_held_slots", held)
        yield tmp_path
        for fd in held:
            try:
                os.close(fd)
            except OSError:
                pass

    def test_every_reading_is_optional_and_the_tightest_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import conftest as suite_conftest

        monkeypatch.setattr(suite_conftest, "_host_total_gib", lambda: 64)
        monkeypatch.setattr(suite_conftest, "_cgroup_limit_mib", lambda: 8 * 1024)
        monkeypatch.setattr(suite_conftest, "_host_available_mib", lambda: 0)

        # 8 GiB ceiling at 2 GiB/worker is the tightest real reading, and the
        # unavailable one (0) is skipped rather than read as "no memory".
        assert suite_conftest._static_memory_bounded_capacity(32) == 4

    def test_a_starved_host_is_bounded_rather_than_read_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inversion this unit choice exists to prevent.

        In whole GiB, 860 MiB free truncates to 0 -- indistinguishable from "could not
        determine", which is SKIPPED. The live bound would therefore drop out on exactly
        the starved host it protects, leaving the static total-RAM term to allow 16
        workers on under a gigabyte of free memory.
        """
        import conftest as suite_conftest

        monkeypatch.setattr(suite_conftest, "_host_available_mib", lambda: 860)

        assert suite_conftest._live_memory_bounded_cap(32) == 1

    def test_a_small_container_ceiling_is_not_read_as_no_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same inversion, reached through the cgroup reading instead."""
        import conftest as suite_conftest

        monkeypatch.setattr(suite_conftest, "_host_total_gib", lambda: 256)
        monkeypatch.setattr(suite_conftest, "_cgroup_limit_mib", lambda: 512)
        monkeypatch.setattr(suite_conftest, "_host_available_mib", lambda: 0)

        assert suite_conftest._static_memory_bounded_capacity(32) == 1

    def test_the_static_bound_shapes_the_shared_range_not_just_this_run(
        self, monkeypatch: pytest.MonkeyPatch, slot_dir_env: pathlib.Path
    ) -> None:
        """The memory budget is SHARED between concurrent runs, not granted to each.

        This is the property that decides where each bound goes. A 64-core / 32 GiB host
        can back 16 workers, so there must be 16 SLOTS in total -- a first run takes them
        all and a second gets its floor. Put the static bound only on the per-run cap and
        both runs take 16 each: 32 workers against a 16-worker budget, which is the
        swapping incident the budget exists to prevent, reached from the other end.
        """
        import conftest as suite_conftest

        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(suite_conftest, "_host_total_gib", lambda: 32)
        monkeypatch.setattr(suite_conftest, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(suite_conftest, "_host_available_mib", lambda: 0)
        monkeypatch.delenv(suite_conftest._MAX_WORKERS_ENV, raising=False)

        first = suite_conftest.pytest_xdist_auto_num_workers(None)
        # A second run in this same process cannot re-lock what it already holds, so the
        # slot RANGE is what the assertion has to pin: 16, never 64.
        assert first == 16
        assert suite_conftest._static_memory_bounded_capacity(64) == 16

    def test_the_live_bound_does_not_shrink_the_shared_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half: a transient reading must not reshape the namespace.

        Slots fill from index 0, so a range shortened by a momentary dip excludes exactly
        the slots an earlier run left free -- collapsing the later run while the machine
        idles.
        """
        import conftest as suite_conftest

        monkeypatch.setattr(suite_conftest, "_host_total_gib", lambda: 128)
        monkeypatch.setattr(suite_conftest, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(suite_conftest, "_host_available_mib", lambda: 2048)

        assert suite_conftest._static_memory_bounded_capacity(32) == 32

    def test_an_unavailable_reading_never_collapses_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """macOS has no /proc/meminfo and no /sys/fs/cgroup; Windows raises on
        ``os.sysconf``. All three readings returning 0 must leave the core count
        standing."""
        import conftest as suite_conftest

        monkeypatch.setattr(suite_conftest, "_host_total_gib", lambda: 0)
        monkeypatch.setattr(suite_conftest, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(suite_conftest, "_host_available_mib", lambda: 0)

        assert suite_conftest._static_memory_bounded_capacity(12) == 12

    def test_a_tiny_host_still_gets_one_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slow beats stalled: the floor is one worker, never zero."""
        import conftest as suite_conftest

        monkeypatch.setattr(suite_conftest, "_host_total_gib", lambda: 1)
        monkeypatch.setattr(suite_conftest, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(suite_conftest, "_host_available_mib", lambda: 1024)

        assert suite_conftest._static_memory_bounded_capacity(8) == 1

    @pytest.mark.parametrize(
        ("kb", "expected"),
        [
            (99_328_704, 97_000),  # a large host, in whole MiB
            (8_388_608, 8192),
            (1_048_576, 1024),  # exactly 1 GiB
            (900_000, 878),  # under 1 GiB: a REAL reading, not the unknown sentinel
            (500, 0),  # half a MiB: genuinely below the resolution, reads as unknown
        ],
    )
    def test_meminfo_is_parsed_into_whole_mib(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, kb: int, expected: int
    ) -> None:
        """Parsing is asserted against a FIXTURE, never against the live host.

        ``assert available > 0`` on the real reading looks like a smoke test and is
        actually the wall-clock-race flake class applied to memory: the value truncates
        to whole GiB, so any host with under 1 GiB free returns 0 -- which is the
        function's own "could not determine" sentinel, i.e. a CORRECT return that the
        assertion would call a failure. Small CI containers are the most exposed.
        """
        import conftest as suite_conftest

        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            f"MemTotal:       131549320 kB\nMemAvailable:   {kb} kB\nBuffers: 1 kB\n",
            encoding="utf-8",
        )
        real_open = open

        def _fake(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                return real_open(meminfo, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _fake)

        assert suite_conftest._host_available_mib() == expected

    @pytest.mark.skipif(
        not pathlib.Path("/proc/meminfo").exists(), reason="Linux-only reading"
    )
    def test_available_never_exceeds_total_on_a_real_host(self) -> None:
        """The one invariant that holds at ANY memory level, so it cannot flake."""
        import conftest as suite_conftest

        assert suite_conftest._host_available_mib() <= suite_conftest._host_total_gib() * 1024

    def test_a_missing_meminfo_is_reported_as_unknown_not_as_zero_memory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Stands in for macOS and Windows, where the file does not exist at all."""
        import conftest as suite_conftest

        real_open = open

        def _no_meminfo(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _no_meminfo)

        assert suite_conftest._host_available_mib() == 0

    def test_an_unlimited_cgroup_is_not_read_as_a_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """cgroup v2 spells "no limit" as the literal ``max``.

        v1 instead uses a huge sentinel (0x7FFFFFFFFFFFF000), which needs no special
        case: divided into GiB it is a number no ``min()`` will ever pick. Both are
        exercised here because the two files are read in the same loop.
        """
        import conftest as suite_conftest

        limit_file = tmp_path / "memory.max"
        limit_file.write_text("max\n", encoding="utf-8")
        v1_file = tmp_path / "memory.limit_in_bytes"
        v1_file.write_text(f"{0x7FFFFFFFFFFFF000}\n", encoding="utf-8")

        real_open = open

        def _fake_cgroup(path, *args, **kwargs):
            if str(path) == "/sys/fs/cgroup/memory.max":
                return real_open(limit_file, *args, **kwargs)
            if str(path) == "/sys/fs/cgroup/memory/memory.limit_in_bytes":
                return real_open(v1_file, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _fake_cgroup)

        # "max" is skipped outright; the v1 sentinel converts to a ceiling far above
        # any real core count, so neither can bind.
        assert suite_conftest._static_memory_bounded_capacity(8) <= 8
        assert suite_conftest._cgroup_limit_mib() >= 8 * 1024

    def test_a_real_cgroup_ceiling_binds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """The container case: 8 GiB inside a cgroup on a 256 GiB machine."""
        import conftest as suite_conftest

        limit_file = tmp_path / "memory.max"
        limit_file.write_text(str(8 * 1024**3), encoding="utf-8")
        real_open = open

        # Falls THROUGH for every other path rather than raising. `builtins.open` is
        # shared by every thread in this worker, and the SEL writer is a session-lived
        # daemon thread that opens a file on each flush -- a blanket raise would hand it
        # FileNotFoundError for a path that exists, and could kill the writer so that a
        # later, unrelated SEL test on this worker fails for a reason it cannot see.
        def _fake_cgroup(path, *args, **kwargs):
            if str(path) == "/sys/fs/cgroup/memory.max":
                return real_open(limit_file, *args, **kwargs)
            if str(path) == "/sys/fs/cgroup/memory/memory.limit_in_bytes":
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _fake_cgroup)

        assert suite_conftest._cgroup_limit_mib() == 8 * 1024
