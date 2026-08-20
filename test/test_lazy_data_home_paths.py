"""Guard: data-home paths must never be resolved at import time.

Issue #874. ``config_dir()``, ``kiro_sessions_dir()`` and friends read
``KIROCREW_HOME`` on *every* call. Binding one of them to a module-level constant
freezes whichever home happened to be active when that module was first
imported, which silently breaks:

* **pod isolation** -- a pod exports its own ``KIROCREW_HOME``;
* **the lazy default-home resolution** (``~/.kiro/crew``), which is deliberately
  resolved late and cached;
* **test isolation** -- the autouse ``_isolate_kirocrew_home`` fixture in
  ``conftest.py`` runs *after* collection has already imported the module under
  test, so it cannot reach a frozen constant. That hole is how a local test run
  wrote 2128 fixture rows and 362 fixture cron records into an operator's real
  data home.

The failure mode is silent: no error, no warning, plausible-looking behaviour,
with the damage only surfacing later as fabricated numbers in an aggregation.
A reviewer cannot be expected to catch the 17th occurrence by eye, so this is
enforced structurally instead.

The fix pattern (see ``dashboard/handlers/usage.py`` for the reference, and
``instances/registry.py`` / ``sel.py`` / ``history.py`` for prior art)::

    _SOME_DIR: Path | None = None          # explicit override hook, None = live

    def _some_dir() -> Path:
        return _SOME_DIR if _SOME_DIR is not None else config_dir() / "some"

Keeping the module-level name means existing ``monkeypatch.setattr(mod,
"_SOME_DIR", tmp)`` call sites keep working unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
PATHS_MODULE = SRC / "config" / "paths.py"


def _path_factories() -> set[str]:
    """Names of the **Path-returning** helpers declared in ``config/paths.py``.

    Derived from the module rather than hardcoded so a newly added factory is
    covered automatically -- hardcoding is how the original sweep for #874 missed
    ``kiro_sessions_dir`` and ``kiro_agents_dir`` and under-reported the scope as
    one site instead of sixteen.

    Restricted to functions whose return annotation mentions ``Path``, which is
    the precision half of the same problem: ``paths.py`` also exports helpers
    like ``_safe_dir_name() -> str`` and
    ``_is_unsafe_home() -> bool``. Since the detector matches a bare call name
    (and an attribute call of the same name), including those would make the
    guard flag unrelated module-level calls repo-wide as ``paths.py`` grows --
    and a guard that cries wolf gets weakened or deleted, which costs more than
    the bug it was written to stop.
    """
    tree = ast.parse(PATHS_MODULE.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("__") or node.returns is None:
            continue
        # covers `Path`, `Path | None`, `Optional[Path]`, `list[Path]`
        if "Path" in ast.unparse(node.returns):
            out.add(node.name)
    return out


def _transitive_path_factories() -> set[str]:
    """Transitive closure: Path-returning functions that resolve through a paths.py factory.

    Extends the root set (functions declared in ``paths.py``) with every
    ``Path``-returning function elsewhere in ``src/kiro_crew/`` that calls
    (directly or transitively) any member of the set. This closes the gap where
    accessors like ``kiro_agents_dir_path()`` or ``_subagents_dir()`` resolve
    through ``kiro_agents_dir()`` or ``data_home()`` but were not themselves in
    the forbidden set, allowing ``FROZEN = kiro_agents_dir_path()`` at module
    level without tripping the guard.

    Precision constraints (issue #1059):
    - Only ``Path``-returning functions are candidates (same restriction as the
      root set -- avoids false-positives on generically-named helpers).
    - A function must CALL a member of the growing set, not merely share a name
      with one (the match is on call-expression names within the function body).
    - Functions whose body contains no call to any set member are never added,
      even if they return a Path -- this excludes functions that merely
      manipulate a caller-supplied Path argument.
    """
    roots = _path_factories()

    # Build a map: function_name -> set of names it calls, for every
    # Path-returning function in the tree. We only need function names (not
    # qualified paths) because the guard's detector matches bare call names.
    candidates: dict[str, set[str]] = {}  # name -> called names
    for py in sorted(SRC.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("__") or node.returns is None:
                continue
            if "Path" not in ast.unparse(node.returns):
                continue
            called = _called_names(node)
            if called:
                # Merge: same-named functions across files union their callees.
                if node.name in candidates:
                    candidates[node.name] |= called
                else:
                    candidates[node.name] = called

    # Fixed-point: expand the set until stable.
    forbidden = set(roots)
    changed = True
    while changed:
        changed = False
        for name, calls in candidates.items():
            if name in forbidden:
                continue
            if calls & forbidden:
                forbidden.add(name)
                changed = True

    return forbidden


def _called_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
            if name:
                out.add(name)
    return out


def _frozen_path_constants() -> list[str]:
    """Every import-time evaluation of a path factory (transitively).

    Three shapes freeze identically at import and all three are covered:

    * a module-level assignment -- ``X = config_dir() / "a"``;
    * a **class-body** assignment -- ``class C: X = config_dir()`` (the class
      body executes when the module is imported);
    * a **function default argument** -- ``def f(d=config_dir())`` (defaults are
      evaluated once, at definition time).

    Walking only ``tree.body`` would miss the last two, which is how a guard can
    document a stronger invariant than it enforces.

    Uses the TRANSITIVE factory set so accessors that resolve through a
    paths.py factory (e.g. ``kiro_agents_dir_path()``, ``_subagents_dir()``)
    are also forbidden at module level.
    """
    factories = _transitive_path_factories()
    offenders: list[str] = []
    for py in sorted(SRC.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        # App-internal test harnesses legitimately capture the real data-home
        # path before monkeypatching (e.g. spec_builder's _REAL_STATE_DIR).
        if "tests" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - syntax is enforced elsewhere
            continue
        rel = py.relative_to(SRC)

        def _record(node: ast.AST, targets: list[str], used: set[str], kind: str) -> None:
            offenders.append(
                f"{rel}:{node.lineno}  [{kind}] {','.join(targets)} "
                f"= ...{sorted(used)[0]}()"
            )

        for node in ast.walk(tree):
            # module-level and class-body assignments
            if isinstance(node, (ast.Module, ast.ClassDef)):
                kind = "module" if isinstance(node, ast.Module) else "class-body"
                for stmt in node.body:
                    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                        continue
                    if stmt.value is None:
                        continue
                    used = _called_names(stmt.value) & factories
                    if not used:
                        continue
                    if isinstance(stmt, ast.Assign):
                        targets = [getattr(t, "id", "?") for t in stmt.targets]
                    else:
                        targets = [getattr(stmt.target, "id", "?")]
                    _record(stmt, targets, used, kind)
            # function default arguments (evaluated once, at def time)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = [d for d in node.args.defaults if d is not None]
                defaults += [d for d in node.args.kw_defaults if d is not None]
                for d in defaults:
                    used = _called_names(d) & factories
                    if used:
                        _record(d, [f"{node.name}() default"], used, "def-default")
    return offenders


class TestNoImportTimePathResolution:
    def test_no_module_level_path_constants(self) -> None:
        offenders = _frozen_path_constants()
        assert not offenders, (
            "Data-home path resolved at import time (issue #874). Convert each to "
            "an override hook + accessor -- see the module docstring of this file "
            "for the pattern:\n  " + "\n  ".join(offenders)
        )

    def test_guard_can_actually_fail(self) -> None:
        """Negative control: the detector must flag a known-bad construct.

        An assertion that cannot fail is worthless, so prove the AST walk really
        catches the shape it exists to forbid.
        """
        bad = ast.parse("X = config_dir() / 'usage'")
        node = bad.body[0]
        assert isinstance(node, ast.Assign)
        assert "config_dir" in _called_names(node.value)
        assert "config_dir" in _path_factories()

    def test_annotated_and_nested_forms_are_detected(self) -> None:
        """The regex sweep missed these shapes; the AST walk must not."""
        annotated = ast.parse("X: Path = kiro_agents_dir() / 'a'").body[0]
        nested = ast.parse("X = (kiro_sessions_dir() / 'a').resolve()").body[0]
        for node in (annotated, nested):
            assert _called_names(node.value) & _path_factories()

    def test_transitive_accessor_is_detected(self) -> None:
        """Issue #1059: a module-level call to an accessor (not just a paths.py
        factory) must be detected.

        This is the reproduction from the issue: capturing
        ``kiro_agents_dir_path()`` at module level freezes the path, and the
        guard must now catch it via the transitive closure.
        """
        factories = _transitive_path_factories()
        # Simulate the offending pattern: X = kiro_agents_dir_path()
        bad = ast.parse("X = kiro_agents_dir_path()").body[0]
        assert isinstance(bad, ast.Assign)
        called = _called_names(bad.value)
        assert called & factories, (
            "kiro_agents_dir_path should be in the transitive factory set"
        )
        # Also for _subagents_dir
        bad2 = ast.parse("X = _subagents_dir()").body[0]
        assert _called_names(bad2.value) & factories

    def test_factory_set_excludes_non_path_helpers(self) -> None:
        """Precision control: only Path-returning helpers may be factories.

        ``paths.py`` also exports generically-named helpers that return other
        types. If those entered the factory set, the detector -- which matches a
        bare call name -- would flag unrelated module-level calls anywhere in the
        tree, and a guard that cries wolf gets weakened or deleted.
        """
        factories = _path_factories()
        assert {"config_dir", "kiro_sessions_dir", "kiro_agents_dir"} <= factories
        # non-Path returners in the same module must NOT be treated as factories
        for name in (
            "_safe_dir_name",  # -> str
            "_is_unsafe_home",  # -> bool
            "_in_linked_git_worktree",  # -> bool
        ):
            assert name not in factories, name
        # a Path | None returner still counts
        assert "_valid_override_home" in factories

    def test_transitive_closure_covers_accessors(self) -> None:
        """The transitive set must include accessors that resolve through paths.py."""
        factories = _transitive_path_factories()
        # Root factories from paths.py must be present
        assert {"config_dir", "kiro_sessions_dir", "kiro_agents_dir", "data_home"} <= factories
        # Accessors that call paths.py factories transitively must also be present
        assert "kiro_agents_dir_path" in factories, "kiro_agents_dir_path calls kiro_agents_dir"
        assert "_subagents_dir" in factories, "_subagents_dir calls data_home"
        assert "_token_usage_dir" in factories, "_token_usage_dir calls data_home"
        assert "_upload_dir" in factories, "_upload_dir calls data_home"
        assert "_screenshot_dir" in factories, "_screenshot_dir calls data_home"
        # Non-Path returners must still be excluded
        for name in (
            "_safe_dir_name",
            "_is_unsafe_home",
        ):
            assert name not in factories, name

    def test_detector_covers_class_body_and_default_args(self, tmp_path, monkeypatch) -> None:
        """Negative control for the two shapes a ``tree.body``-only walk misses.

        Both freeze at import exactly like a module constant: a class body runs
        on import, and a default argument is evaluated once at ``def`` time.
        Planted in a temp tree so the real source is untouched.
        """
        import sys

        mod = sys.modules[__name__]

        fake_src = tmp_path / "kiro_crew"
        (fake_src / "config").mkdir(parents=True)
        (fake_src / "config" / "paths.py").write_text(
            "from pathlib import Path\n\n\ndef config_dir() -> Path:\n    return Path('.')\n",
            encoding="utf-8",
        )
        (fake_src / "in_class.py").write_text(
            "class C:\n    DIR = config_dir() / 'a'\n", encoding="utf-8"
        )
        (fake_src / "in_default.py").write_text(
            "def f(d=config_dir()):\n    return d\n", encoding="utf-8"
        )
        monkeypatch.setattr(mod, "SRC", fake_src)
        monkeypatch.setattr(mod, "PATHS_MODULE", fake_src / "config" / "paths.py")

        found = _frozen_path_constants()
        kinds = {f.split("[")[1].split("]")[0] for f in found if "[" in f}
        assert "class-body" in kinds, found
        assert "def-default" in kinds, found

    def test_detector_covers_transitive_accessor(self, tmp_path, monkeypatch) -> None:
        """Issue #1059: a module-level call to an accessor that transitively
        calls a paths.py factory must be flagged.
        """
        import sys

        mod = sys.modules[__name__]

        fake_src = tmp_path / "kiro_crew"
        (fake_src / "config").mkdir(parents=True)
        (fake_src / "config" / "paths.py").write_text(
            "from pathlib import Path\n\n\ndef data_home() -> Path:\n    return Path('.')\n",
            encoding="utf-8",
        )
        # An accessor that calls data_home()
        (fake_src / "accessor.py").write_text(
            "from pathlib import Path\n\n"
            "def _subagents_dir() -> Path:\n"
            "    return data_home() / 'subagents'\n",
            encoding="utf-8",
        )
        # A module that freezes the accessor at import time
        (fake_src / "bad_module.py").write_text(
            "FROZEN = _subagents_dir()\n", encoding="utf-8"
        )
        monkeypatch.setattr(mod, "SRC", fake_src)
        monkeypatch.setattr(mod, "PATHS_MODULE", fake_src / "config" / "paths.py")

        found = _frozen_path_constants()
        assert any("_subagents_dir" in f for f in found), (
            f"Transitive accessor not detected: {found}"
        )


# (module import path, override constant, accessor) for every accessor that
# resolves through ``config_dir()``. The structural test above is the exhaustive
# half; this table is the behavioural half -- it proves the accessors actually
# FOLLOW a home change rather than merely not being constants.
#
# The override constant is listed so each case can be reset to ``None`` first:
# ``conftest.py``'s autouse ``_isolate_subagents_dir`` pins
# ``_SUBAGENTS_DIR`` to a tmp dir, and an override legitimately outranks the
# environment -- without the reset this test would assert the fixture, not the
# live-resolution branch it exists to cover.
_CONFIG_DIR_ACCESSORS = [
    ("kiro_crew.dashboard.handlers.usage", "_TOKEN_USAGE_DIR", "_token_usage_dir"),
    ("kiro_crew.cron", "_DEFAULT_DIR", "_default_dir"),
    ("kiro_crew.subagent_persistence", "_SUBAGENTS_DIR", "_subagents_dir"),
    ("kiro_crew.dashboard.handlers.files", "_UPLOAD_DIR", "_upload_dir"),
    ("kiro_crew.dashboard.handlers.files", "_SCREENSHOT_DIR", "_screenshot_dir"),
    ("kiro_crew.dashboard.handlers.hooks", "_HOOK_STORE_PATH", "_hook_store_path"),
    ("kiro_crew.dashboard.handlers.mcp", "_KIROCREW_MCP_JSON", "_kirocrew_mcp_json"),
    ("kiro_crew.slack.sessions_view", "_SESSIONS_DIR", "_sessions_dir"),
    ("kiro_crew.apps.builtins.auto_research.handlers", "RESEARCH_DIR", "research_dir"),
    ("kiro_crew.apps.builtins.auto_research.handlers", "DB_PATH", "db_path"),
]


class TestAccessorsFollowTheLiveHome:
    """A post-import ``KIROCREW_HOME`` change must redirect every accessor.

    ``config_dir()`` returns a ``$KIROCREW_HOME`` override immediately, ahead of
    the cached default-home resolution branch, so the override path is live on
    every call. These modules are imported at collection time -- long before the
    env var below is set -- which is exactly the sequence that used to strand
    them on the operator's real home.
    """

    def test_every_accessor_follows_a_post_import_home_change(self, tmp_path, monkeypatch):
        import importlib

        home = tmp_path / "home-a"
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        stranded = []
        for mod_path, const, attr in _CONFIG_DIR_ACCESSORS:
            mod = importlib.import_module(mod_path)
            monkeypatch.setattr(mod, const, None)
            resolved = Path(getattr(mod, attr)())
            if not resolved.resolve().is_relative_to(home.resolve()):
                stranded.append(f"{mod_path}.{attr}() -> {resolved}")
        assert not stranded, (
            "accessor did not follow KIROCREW_HOME set after import (issue #874):\n  "
            + "\n  ".join(stranded)
        )

    def test_accessor_tracks_a_second_change(self, tmp_path, monkeypatch):
        """Not merely read-once-late: it must re-resolve on every call."""
        import importlib

        mod = importlib.import_module("kiro_crew.dashboard.handlers.usage")
        first = tmp_path / "home-1"
        monkeypatch.setenv("KIROCREW_HOME", str(first))
        one = mod._token_usage_dir()
        second = tmp_path / "home-2"
        monkeypatch.setenv("KIROCREW_HOME", str(second))
        two = mod._token_usage_dir()
        assert one != two
        assert one.resolve().is_relative_to(first.resolve())
        assert two.resolve().is_relative_to(second.resolve())

    def test_override_hook_still_wins(self, tmp_path, monkeypatch):
        """The kept module-level name must still override, so the dozens of
        existing ``monkeypatch.setattr(mod, "_X", tmp)`` call sites keep working.
        """
        import importlib

        mod = importlib.import_module("kiro_crew.dashboard.handlers.usage")
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "ignored-home"))
        pinned = tmp_path / "pinned"
        monkeypatch.setattr(mod, "_TOKEN_USAGE_DIR", pinned)
        assert mod._token_usage_dir() == pinned


class TestResolutionDoesNotRepeatStartupMaintenance:
    """``data_home()`` must not re-run start-of-process maintenance per call.

    ``config_dir()`` is resolve + maintain: it also mkdirs the home and refreshes
    the recovery breadcrumb (a stat + a read). Resolving per call (the #874 fix)
    would otherwise put that on every caller -- including request handlers, where
    the breadcrumb refresh would run on the event loop as a side effect of asking
    where a directory is.

    Each assertion is paired with its negative control: the test proves
    ``config_dir()`` DOES perform the work, so a passing ``data_home()``
    assertion cannot be explained by the maintenance simply being dead.
    """

    def _count_maintenance(self, monkeypatch):
        from kiro_crew.config import paths

        calls: list[int] = []
        monkeypatch.setattr(paths, "_write_recovery_breadcrumb", lambda d: calls.append(1))
        return calls

    def test_repeat_calls_skip_maintenance_but_config_dir_still_runs_it(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.config import paths

        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        home = tmp_path / "resolved"
        home.mkdir()
        monkeypatch.setattr(paths, "_resolved_home", home)
        calls = self._count_maintenance(monkeypatch)

        assert paths.data_home() == home
        assert paths.data_home() == home
        assert calls == [], "data_home() re-ran start-of-process maintenance"

        # negative control: the maintenance is NOT dead -- config_dir() does it
        paths.config_dir()
        assert calls, "config_dir() no longer performs maintenance; test is vacuous"

    def test_first_resolution_still_performs_maintenance(self, tmp_path, monkeypatch):
        """Per START, not per call -- the breadcrumb refresh runs once per process."""
        from kiro_crew.config import paths

        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(paths, "_resolved_home", None)
        monkeypatch.setattr(paths, "_resolve_default_home", lambda: tmp_path / "fresh")
        calls = self._count_maintenance(monkeypatch)

        paths.data_home()
        assert calls, "the first resolution in a process must still perform maintenance"

    def test_override_is_never_cached(self, tmp_path, monkeypatch):
        """A KIROCREW_HOME set after import must still be honoured (#874).

        Caching would be cheaper but would reintroduce the original bug, so the
        override branch deliberately delegates on every call.
        """
        from kiro_crew.config import paths

        monkeypatch.setattr(paths, "_resolved_home", tmp_path / "stale-default")
        first = tmp_path / "ov-1"
        monkeypatch.setenv("KIROCREW_HOME", str(first))
        assert paths.data_home().resolve() == first.resolve()
        second = tmp_path / "ov-2"
        monkeypatch.setenv("KIROCREW_HOME", str(second))
        assert paths.data_home().resolve() == second.resolve()

    def test_invalid_override_does_not_reopen_the_maintenance_path(
        self, tmp_path, monkeypatch
    ):
        """An override naming a system dir must NOT re-run maintenance per call.

        ``config_dir()`` gates the override branch on ``_valid_override_home()``
        (set AND safe); an override like ``/usr`` is rejected there and
        resolution falls through to the default home, which mkdirs and refreshes
        the breadcrumb. So ``data_home()`` must gate on the
        SAME predicate -- testing merely "is the env var set" would send every
        call down the maintenance path and put the breadcrumb refresh back on the
        request path for anyone with a bad override.
        """
        from kiro_crew.config import paths

        home = tmp_path / "resolved"
        home.mkdir()
        monkeypatch.setattr(paths, "_resolved_home", home)

        # The filesystem/drive ROOT is rejected on every platform -- do NOT
        # hardcode a POSIX system dir like "/usr": on Windows that resolves to
        # ``D:/usr``, which is a perfectly ordinary path, so the override would
        # be ACCEPTED and this test would assert nothing. (CI on Windows caught
        # exactly that.) ``tmp_path.anchor`` is "/" on POSIX and "C:\\" (or the
        # runner's drive) on Windows.
        monkeypatch.setenv("KIROCREW_HOME", tmp_path.anchor)
        assert paths._valid_override_home() is None, "precondition: override rejected"

        calls = self._count_maintenance(monkeypatch)
        assert paths.data_home() == home
        assert paths.data_home() == home
        assert calls == [], "invalid override re-ran maintenance on every call"

    def test_valid_override_still_delegates(self, tmp_path, monkeypatch):
        """Negative control for the test above: a VALID override must delegate.

        Otherwise the previous test could pass simply because the override
        branch stopped working altogether.
        """
        from kiro_crew.config import paths

        monkeypatch.setattr(paths, "_resolved_home", tmp_path / "stale-default")
        good = tmp_path / "good-override"
        monkeypatch.setenv("KIROCREW_HOME", str(good))
        assert paths._valid_override_home() is not None
        assert paths.data_home().resolve() == good.resolve()
