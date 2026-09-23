"""The corpus behind every AST ratchet: not stale, not empty, not narrowing.

``source_corpus`` makes the whole-tree gates cheap two ways -- it reads the tree
once and caches it, and it parses only the files whose text can possibly match
the calling gate's pattern. Both are silent failure modes: a cache that comes
back empty, or a literal filter that is not actually a necessary condition,
leaves every one of those gates green while seeing nothing. Neither shows up as a
failure anywhere else, which is why they are pinned here rather than trusted.

The cache is pinned by shape, and each filter two ways: its literals must still
appear in the very source the gate exists to reject (so a renamed chokepoint
breaks this test instead of quietly emptying that gate), and it must still
exclude a real part of the tree (so a filter that has stopped narrowing is
noticed rather than paid for).

On soundness of the filters themselves: for the identifier-based ones the
argument is textual. ``_batch_blocks`` fires only on an ``ast.Attribute`` spelled
``batched_save``, and no such node can be parsed from text that does not contain
those characters; the same holds for ``sandboxed_spawn_argv`` and for the
``redact*`` family. The keyword-based one is the exception worth stating: an
``ast.AsyncFunctionDef`` needs the ``async`` KEYWORD, but not the two-word string
``async def`` -- ``async  def f():`` parses -- which is why the blocking gate
filters on the bare keyword and why that spelling is asserted below.

The module's other half, ``repo_files``, is pinned the same way and for the same
reason, plus one more: a repo-WIDE scan written as ``rglob``/``os.walk`` sees
every gitignored tree and every nested checkout under the repo root, and it stays
GREEN while doing so. That answer was written correctly once, as a local inside
one gate, and three later gates re-derived the walk instead. So the last class
here is a ratchet: no test may enumerate the checkout from the filesystem.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import tempfile
import types
from collections.abc import Sequence
from pathlib import Path

import pytest
import source_corpus
import test_no_blocking_call_on_loop as blocking
import test_sandbox_off_loop as sandbox
import test_session_map_locking as session_map
from source_corpus import (
    candidate_sources,
    repo_files,
    repo_files_named,
    repo_root,
    source_texts,
    src_root,
    unreadable_files,
)

#: The tree is ~1250 modules. A floor well under that catches the failure that
#: matters -- a corpus that came back empty or nearly so -- without turning an
#: ordinary file addition or deletion into a test edit.
_MIN_FILES = 800

#: The census filter, which lives in ``test_security_posture`` as a literal at
#: its one call site because that gate has no class to hang it on.
_CENSUS_REQUIRE_ALL = ("redact",)

#: The nodes CPython attaches ``__doc__`` to, and so the only strings it encodes
#: on its own account rather than on the program's.
_DOCSTRING_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

#: Necessary condition for an unencodable docstring, so the gate below parses 15
#: files instead of the tree. A surrogate must have been WRITTEN as an escape:
#: no UTF-8 byte sequence decodes to one, so a literal surrogate would make the
#: file unreadable (pinned above) rather than parseable. Surrogates are
#: U+D800-U+DFFF, so every spelling is ``\u`` or ``\U0000`` followed by a ``d``,
#: and ``\N{...}`` has no name for an unpaired one.
_SURROGATE_ESCAPES = ("\\ud", "\\uD", "\\U0000d", "\\U0000D")

#: The checkout is ~10,200 files. A floor well under that catches the failure that
#: matters -- an enumeration that came back empty or nearly so -- without turning
#: an ordinary addition or deletion into a test edit.
_MIN_REPO_FILES = 5000

#: The two trees that hold this repository's repo-wide gates, and so the two the
#: ratchet below polices. Everything under them is scanned; a gate rooted at a
#: SUBDIRECTORY is not the subject (see ``_unanchored_glob``).
_GATE_TREES = ("test", "scripts")

#: The file allowed to walk: the shared enumerator's own no-git fallback, which
#: applies the nested-checkout rule itself rather than inheriting it from git.
_THE_ENUMERATOR = "test/source_corpus.py"

#: Necessary conditions for an offence, so the gate parses ~315 of the 2,500 files
#: under :data:`_GATE_TREES` instead of all of them (~4 s instead of ~50 s). Each
#: shape below needs a call name written against its ``(``, and needs the root
#: SPELLED OUT somewhere in the same module -- ``_bound_roots`` can only bind a
#: name or an accessor to a spelling it can see. ``.parent`` rather than
#: ``.parent.parent`` so a chain broken across lines is still a candidate.
_SCAN_TOKENS = ("rglob(", "walk(", "glob(")
_ROOT_TOKENS = ("parents[", ".parent", "repo_root()")

#: Recursive enumerations reached as a method on their root: ``root.rglob(...)``
#: and 3.12's ``root.walk()``.
_RECURSIVE_METHODS = frozenset({"rglob", "walk"})

#: ...and the ones that take their root as the first ARGUMENT, in every spelling
#: (``os.walk(root)``, a bare ``walk(root)`` after a ``from os import walk``).
_WALK_FUNCTIONS = frozenset({"walk", "fwalk"})

#: The shared accessor, which is the repo root by definition -- bare, or reached
#: through the corpus module. Not ``<anything>.repo_root()``: ``cloud/source.py``
#: has a function of that name whose answer is a packaging root, and its tests
#: assign it to ``root`` and then legitimately ``rglob`` a ``tmp_path`` tree.
_ROOT_ACCESSOR = "repo_root"
_CORPUS_MODULE = "source_corpus"

#: The node kinds that open a new naming scope. ``ast.Lambda`` is deliberately not
#: one: nothing is ASSIGNED inside a lambda, and treating it as a scope would only
#: matter for a lambda parameter shadowing a root name, which the tree has none of.
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _is_path_of_dunder_file(node: ast.expr) -> bool:
    """``Path(__file__)`` -- the base every spelled-out repo root is built on."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Path"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "__file__"
    )


def _trailers(node: ast.expr) -> tuple[ast.expr, list[tuple[str, object]]]:
    """Split an attribute/subscript/no-arg-call chain into ``(base, steps)``.

    Steps come back outermost-last, so ``Path(__file__).resolve().parents[1]``
    reads ``[("call", "resolve"), ("attr", "parents"), ("item", <1>)]``.
    """
    steps: list[tuple[str, object]] = []
    while True:
        if isinstance(node, ast.Attribute):
            steps.append(("attr", node.attr))
            node = node.value
        elif isinstance(node, ast.Subscript):
            steps.append(("item", node.slice))
            node = node.value
        elif (
            isinstance(node, ast.Call)
            and not node.args
            and not node.keywords
            and isinstance(node.func, ast.Attribute)
        ):
            steps.append(("call", node.func.attr))
            node = node.func.value
        else:
            return node, list(reversed(steps))


def _spelled_repo_root(node: ast.expr) -> bool:
    """True for the repo root written out from ``__file__``.

    Structurally, not by unparsing and matching text: unparsing every assigned
    value in 315 files to test it against a regex cost more than the parse did.
    ``parents[0]`` / a single ``.parent`` is the ``test/`` directory, which is a
    legitimate scan root, so the offset has to be at least one.
    """
    base, steps = _trailers(node)
    if not _is_path_of_dunder_file(base):
        return False
    if steps[:1] == [("call", "resolve")]:
        steps = steps[1:]
    if len(steps) == 2 and steps[0] == ("attr", "parents") and steps[1][0] == "item":
        index = steps[1][1]
        return isinstance(index, ast.Constant) and isinstance(index.value, int) and index.value >= 1
    return len(steps) >= 2 and all(step == ("attr", "parent") for step in steps)


def _is_repo_root(node: ast.expr, names: set[str], accessors: set[str]) -> bool:
    """True if *node* evaluates to the repo root.

    Resolved through the AST rather than by matching the source text, so a gate
    that stores the root in a constant (``REPO_ROOT``) or behind an accessor
    (``cls._repo_root()``, ``source_corpus.repo_root()``) is judged on what it
    scans, not on how it spells it.
    """
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Call) and not node.args and not node.keywords:
        func = node.func
        if isinstance(func, ast.Name):
            return func.id == _ROOT_ACCESSOR or func.id in accessors
        if isinstance(func, ast.Attribute):
            if func.attr in accessors:
                return True
            if func.attr == _ROOT_ACCESSOR:
                return isinstance(func.value, ast.Name) and func.value.id == _CORPUS_MODULE
    return _spelled_repo_root(node)


class _Scope:
    """One naming scope: the nodes it owns, and the scopes nested directly in it.

    Names have to be resolved per scope, not per module: ``root`` is both the usual
    name for the repo root and the usual name for a fixture tree a helper takes as
    an argument, and a module-wide set of names conflates the two. Built ONCE per
    file -- deriving it per function instead cost 27 s of a 38 s run.
    """

    __slots__ = ("owner", "here", "children")

    def __init__(self, owner: ast.stmt | None) -> None:
        self.owner = owner  # None for the module itself
        self.here: list[ast.AST] = []
        self.children: list[_Scope] = []


def _build_scopes(body: list[ast.stmt], owner: ast.stmt | None = None) -> _Scope:
    """Partition *body* into scopes, in one traversal of the tree."""
    scope = _Scope(owner)
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPE_NODES):
            scope.children.append(_build_scopes(list(node.body), node))
            continue
        scope.here.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return scope


def _bound_roots(here: list[ast.AST], accessors: set[str], inherited: set[str]) -> set[str]:
    """The names standing for the repo root in one scope, *inherited* included.

    Twice over the scope, because a module-level constant is often assigned below
    the function that reads it.
    """
    names = set(inherited)
    for _pass in range(2):
        for node in here:
            if isinstance(node, ast.Assign) and _is_repo_root(node.value, names, accessors):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _root_accessors(root: _Scope) -> set[str]:
    """Every function in the module that RETURNS the repo root and nothing else.

    Twice, because one accessor can be written in terms of another.
    """
    functions: list[_Scope] = []
    pending = [root]
    while pending:
        scope = pending.pop()
        pending += scope.children
        if isinstance(scope.owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(scope)
    accessors: set[str] = set()
    for _pass in range(2):
        for scope in functions:
            returned = [n.value for n in scope.here if isinstance(n, ast.Return) and n.value]
            if returned and all(_is_repo_root(v, set(), accessors) for v in returned):
                accessors.add(scope.owner.name)  # type: ignore[union-attr]
    return accessors


def _unanchored_glob(call: ast.Call) -> bool:
    """True for a ``glob`` pattern that recurses from the receiver itself.

    ``root.glob("src/kiro_crew/**/*.py")`` is anchored below the root and several
    gates use exactly that shape on purpose, so only a LEADING ``**`` -- which is
    ``rglob`` written the long way -- counts.
    """
    first = call.args[0] if call.args else None
    return (
        isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and first.value.startswith("**")
    )


def _scan_subjects(call: ast.Call) -> list[ast.expr]:
    """The expressions *call* would enumerate a whole tree under, if any."""
    func = call.func
    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    subjects: list[ast.expr] = []
    if isinstance(func, ast.Attribute) and (
        called in _RECURSIVE_METHODS or (called == "glob" and _unanchored_glob(call))
    ):
        subjects.append(func.value)
    if called in _WALK_FUNCTIONS and call.args:
        subjects.append(call.args[0])
    return subjects


def _scan_scope(
    scope: _Scope, accessors: set[str], inherited: set[str], found: list[tuple[int, str]]
) -> None:
    """Collect this scope's offences, then recurse with its names, minus shadows."""
    names = _bound_roots(scope.here, accessors, inherited)
    for node in scope.here:
        if isinstance(node, ast.Call) and any(
            _is_repo_root(subject, names, accessors) for subject in _scan_subjects(node)
        ):
            found.append((node.lineno, ast.unparse(node)[:100]))
    for child in scope.children:
        args = getattr(child.owner, "args", None)
        shadowed = {a.arg for a in ast.walk(args) if isinstance(a, ast.arg)} if args else set()
        _scan_scope(child, accessors, names - shadowed, found)


def repo_root_scans(text: str) -> list[str]:
    """``"<line>: <call>"`` for every recursive filesystem scan rooted at the repo."""
    found: list[tuple[int, str]] = []
    root = _build_scopes(list(ast.parse(text).body))
    _scan_scope(root, _root_accessors(root), set(), found)
    return [f"{lineno}: {call}" for lineno, call in sorted(found)]


def _unencodable_docstrings(sources: Sequence[tuple[Path, str]]) -> list[tuple[Path, str, str]]:
    """``(path, owner, reason)`` for every docstring UTF-8 cannot represent.

    ``clean=False`` because de-indenting cannot change whether a character is
    encodable, and the whole point is to answer that question cheaply.
    """
    found: list[tuple[Path, str, str]] = []
    for path, text in sources:
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if not isinstance(node, _DOCSTRING_NODES):
                continue
            doc = ast.get_docstring(node, clean=False)
            if doc is None:
                continue
            try:
                doc.encode("utf-8")
            except UnicodeEncodeError as exc:
                found.append((path, getattr(node, "name", "<module>"), str(exc)))
    return found


class TestCorpusHealth:
    """A stale or empty corpus is the one failure that makes every gate green."""

    def test_the_corpus_is_not_empty_or_stale(self):
        texts = source_texts()
        assert len(texts) >= _MIN_FILES, (
            f"source_corpus returned {len(texts)} files; every whole-tree ratchet "
            "reads this, so a short corpus makes all of them pass while blind."
        )

    def test_every_file_is_python_under_the_package(self):
        root = src_root()
        assert (root / "security" / "__init__.py").is_file(), f"{root} is not the kiro_crew package"
        for path, _text in source_texts():
            assert path.suffix == ".py"
            assert path.is_relative_to(root)

    def test_no_file_was_skipped_as_unreadable(self):
        assert unreadable_files() == (), (
            "The corpus could not decode these files, so no ratchet can see them: "
            f"{[str(p) for p in unreadable_files()]}"
        )

    def test_no_docstring_holds_a_character_utf_8_cannot_encode(self):
        """An unpaired surrogate in a docstring is an unimportable module.

        The compiler de-indents docstrings and encodes ``__doc__`` as strict
        UTF-8, so from 3.13 on a docstring carrying an unpaired surrogate raises
        ``UnicodeEncodeError`` on IMPORT. Nothing else in the suite survives that:
        it lands during collection, so every gate here reports zero tests rather
        than one failure, and the corpus never gets read at all.

        Asserted rather than left to the compiler because the property has to
        hold on every interpreter the project supports -- an older one compiles
        such a docstring happily, which is exactly how one reaches a tree whose
        gates all look green.
        """
        offenders = _unencodable_docstrings(candidate_sources(require_any=_SURROGATE_ESCAPES))
        assert offenders == [], (
            "These docstrings hold a character UTF-8 cannot encode, so their "
            "modules do not import on 3.13+ -- write the escape as a literal "
            f"(``\\\\ud800``) instead: {[(str(p), owner) for p, owner, _why in offenders]}"
        )

    def test_sources_are_the_real_file_contents(self):
        """Pins the read, not just the count: a corpus of empty strings is worse."""
        by_name = {path.name: text for path, text in source_texts()}
        by_rel = {path.relative_to(src_root()).as_posix(): text for path, text in source_texts()}
        assert "def redact" in by_rel["security/__init__.py"]
        assert "def batched_save" in by_name["session_map.py"]

    def test_a_filter_returns_a_subset_and_no_filter_returns_everything(self):
        assert set(candidate_sources(blocking._REQUIRE_ALL)) <= set(source_texts())
        assert candidate_sources() == source_texts()


class TestFilterLiteralsStillMatchWhatTheGatesReject:
    """Each filter, applied to source the gate exists to fail on.

    This is the half a rename breaks: move ``batched_save`` and the gate keeps
    passing on a tree it cannot see into, unless something asserts that
    the filter admits a known violation. Each case is the same shape the gate's
    own meta-test plants.
    """

    @staticmethod
    def _admits(source: str, require_all=(), require_any=()) -> bool:
        return all(lit in source for lit in require_all) and (
            not require_any or any(lit in source for lit in require_any)
        )

    def test_a_bare_hop_survives_the_sandbox_filter(self):
        cls = sandbox.TestNoBareSandboxedSpawnArgvHops
        violating = (
            "import asyncio\n"
            "async def f(argv):\n"
            "    return await asyncio.to_thread(sandboxed_spawn_argv, argv)\n"
        )
        assert self._admits(violating, cls._REQUIRE_ALL, cls._REQUIRE_ANY)
        # ... and the indirect spelling, which is the one the gate was widened for.
        indirect = (
            "def _prepare(argv):\n"
            "    return sandboxed_spawn_argv(argv)\n"
            "async def f(loop, pool, argv):\n"
            "    return await loop.run_in_executor(pool, _prepare, argv)\n"
        )
        assert self._admits(indirect, cls._REQUIRE_ALL, cls._REQUIRE_ANY)

    def test_an_awaiting_batch_block_survives_the_session_map_filter(self):
        violating = "async def f(s):\n" "    with s.batched_save():\n" "        await s.flush()\n"
        assert self._admits(violating, session_map.TestNoAwaitInsideBatch._REQUIRE_ALL)

    def test_an_on_loop_blocking_call_survives_the_blocking_filter(self):
        violating = "import time\nasync def f():\n    time.sleep(1)\n"
        assert self._admits(violating, blocking._REQUIRE_ALL)

    def test_the_blocking_filter_admits_the_two_space_async_spelling(self):
        """``async  def`` parses, so ``"async def"`` would be an unsound filter."""
        odd = "import time\nasync  def f():\n    time.sleep(1)\n"
        assert isinstance(ast.parse(odd).body[1], ast.AsyncFunctionDef)
        assert "async def" not in odd
        assert self._admits(odd, blocking._REQUIRE_ALL)
        assert blocking.find_violations(odd), "the gate itself must flag this shape"

    def test_a_baseline_log_site_survives_the_census_filter(self):
        violating = (
            "def apply(stderr):\n"
            "    logger.error('install failed: %s', redact(stderr.decode()))\n"
        )
        assert self._admits(violating, _CENSUS_REQUIRE_ALL)

    def test_a_lone_surrogate_docstring_survives_the_encodability_filter(self):
        """The one filter whose literals are prose, so nothing else would notice."""
        violating = 'def f():\n    """A lone \\ud800 escape, quoted as prose."""\n'
        assert self._admits(violating, require_any=_SURROGATE_ESCAPES)
        assert _unencodable_docstrings([(Path("planted.py"), violating)]), "gate must flag this"


class TestFiltersStillNarrowTheTree:
    """A filter that keeps everything is cost with no benefit left in it."""

    @pytest.mark.parametrize(
        ("label", "require_all", "require_any", "ceiling"),
        [
            (
                "sandbox-bare-hop",
                sandbox.TestNoBareSandboxedSpawnArgvHops._REQUIRE_ALL,
                sandbox.TestNoBareSandboxedSpawnArgvHops._REQUIRE_ANY,
                100,
            ),
            ("session-map-batch", session_map.TestNoAwaitInsideBatch._REQUIRE_ALL, (), 100),
            ("security-census", _CENSUS_REQUIRE_ALL, (), 700),
            ("blocking-on-loop", blocking._REQUIRE_ALL, (), 900),
        ],
    )
    def test_the_filter_narrows_the_tree(self, label, require_all, require_any, ceiling):
        kept = candidate_sources(require_all, require_any)
        assert kept, f"{label}: matched nothing, so that gate now scans an empty tree"
        assert len(kept) < ceiling, (
            f"{label}: kept {len(kept)} of {len(source_texts())} files, so the filter "
            "is no longer buying anything -- either the tree or the literal moved."
        )


def _plant_nested_checkout(root: Path) -> None:
    """A miniature checkout holding a second checkout, as the harness leaves one.

    ``.git`` is written as a FILE because that is what a git *worktree* has (a
    clone has a directory), and the harness creates worktrees.
    """
    (root / "src").mkdir()
    (root / "src" / "shipped.py").write_text("", encoding="utf-8")
    nested = root / ".claude" / "worktrees" / "wf_1"
    (nested / "src").mkdir(parents=True)
    (nested / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wf_1\n", encoding="utf-8")
    (nested / "src" / "shipped.py").write_text("", encoding="utf-8")


class TestTheCheckoutEnumeration:
    """``repo_files`` answers what THIS checkout holds, and only this one.

    Both ways of being wrong are silent. A short answer leaves every repo-wide gate
    passing on a tree it cannot see. An answer that reaches into a nested checkout
    makes a reporting gate name a path its author cannot edit -- and makes an
    existential one (``any()`` over the matches, which is how the coverage omit
    contract is written) keep passing on a stale copy after the real file lost the
    property it was asserting.
    """

    def test_the_enumeration_is_not_empty_or_stale(self):
        files = repo_files()
        assert len(files) >= _MIN_REPO_FILES, (
            f"repo_files returned {len(files)} paths; the shell-script, SKILL.md and "
            "coverage-omit gates all read this, so a short answer makes them pass blind."
        )

    def test_every_entry_is_a_readable_file_inside_the_checkout(self):
        """Pins the entries git lists that a gate cannot open.

        ``--cached`` reports a file deleted in the working tree but not yet staged
        (the index entry survives) and a submodule gitlink (as a DIRECTORY path);
        ``--others`` reports a nested repository as its bare directory. Each would
        raise on the first ``read_text`` in a gate.
        """
        root = repo_root()
        for path in repo_files():
            assert path.is_file(), path
            assert path.is_relative_to(root), path

    def test_no_nested_checkout_is_visible(self):
        """A directory holding its own ``.git`` is a DIFFERENT repository.

        Stated generally rather than as ``.claude/worktrees``, which is only where
        this harness happens to put one. Live on any machine that has a nested
        checkout on disk; the fallback case below is what pins the rule where none
        does.
        """
        root = repo_root()
        # Asked once per directory rather than once per file: the same ~10k paths
        # share a few thousand parents, and every check is a `stat`.
        parents = {parent for path in repo_files() for parent in path.parents}
        offenders = sorted(
            str(parent.relative_to(root))
            for parent in parents
            if parent != root and parent.is_relative_to(root) and (parent / ".git").exists()
        )
        assert offenders == [], (
            "a file was enumerated from inside a checkout nested in this one, so every "
            f"repo-wide gate is being handed a second copy of it: {offenders}"
        )

    def test_a_suffix_filter_returns_a_subset(self):
        assert set(repo_files_named(".py")) <= set(repo_files())
        assert repo_files_named(".py"), "no Python in the checkout: the filter is broken"

    def test_the_no_git_fallback_skips_a_nested_checkout(self, tmp_path, monkeypatch):
        """The walk is where the defect grows back, and CI can never reach it.

        Only an sdist (no ``.git`` at all) takes this branch, so nothing else in
        the suite would notice it drifting back to a bare walk.
        """
        _plant_nested_checkout(tmp_path)
        monkeypatch.setattr(source_corpus, "repo_root", lambda: tmp_path)
        found = source_corpus._fallback_walk()
        assert [p.relative_to(tmp_path).as_posix() for p in found] == ["src/shipped.py"]

    def test_repo_files_takes_that_fallback_when_git_is_absent(self, tmp_path, monkeypatch):
        """...and reaches it by the route an sdist does: no ``git`` on the box."""
        _plant_nested_checkout(tmp_path)
        no_git = types.SimpleNamespace(
            run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")),
            SubprocessError=subprocess.SubprocessError,
        )
        monkeypatch.setattr(source_corpus, "repo_root", lambda: tmp_path)
        monkeypatch.setattr(source_corpus, "subprocess", no_git)
        repo_files.cache_clear()
        try:
            found = repo_files()
        finally:
            # The cache is keyed on nothing, so a tmp_path answer left in it would
            # be served to every later gate on this worker.
            repo_files.cache_clear()
        assert [p.relative_to(tmp_path).as_posix() for p in found] == ["src/shipped.py"]

    def test_a_refusing_git_in_a_real_checkout_raises_instead_of_walking(
        self, tmp_path, monkeypatch
    ):
        """The fallback must not stand in for git where git SHOULD have answered.

        A leaked ``GIT_DIR`` (three suites set one), an unreadable index or
        ``safe.directory`` makes ``ls-files`` exit non-zero in a checkout that has a
        ``.git``. Walking there answers WIDER than git -- it cannot read the ignore
        rules -- which is the exact failure the enumerator exists to stop, and no
        floor catches it: a second copy of every shipped file is a surplus, not a
        shortage. So this case is loud.
        """
        _plant_nested_checkout(tmp_path)
        (tmp_path / ".git").mkdir()
        refusing = types.SimpleNamespace(
            run=lambda *a, **k: (_ for _ in ()).throw(
                subprocess.CalledProcessError(128, "git", stderr=b"fatal: not a git repository")
            ),
            SubprocessError=subprocess.SubprocessError,
        )
        monkeypatch.setattr(source_corpus, "repo_root", lambda: tmp_path)
        monkeypatch.setattr(source_corpus, "subprocess", refusing)
        repo_files.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="not a git repository"):
                repo_files()
        finally:
            repo_files.cache_clear()

    def test_the_runs_own_temp_files_are_not_the_checkout(self, tmp_path, monkeypatch):
        """A temp root under the checkout is the HOST's layout, not the tree.

        A harness that pins ``TMPDIR`` inside the repository makes git's
        ``--others`` list every fixture the suite writes -- a copy of a shipped
        script under a fake home, a nested repository, a file another worker deletes
        a moment later -- and the gates then police the run's own scratch. On a host
        whose temp dir is elsewhere none of it is ever listed, so it is what the
        enumeration must answer identically on both. Real git, so the rule is pinned
        on the path a checkout takes and not on the walk.
        """
        _git = shutil.which("git")
        if _git is None:
            pytest.skip("git is required to build the checkout")

        def git(*args: str) -> None:
            # ``cwd`` as well as ``-C``: the child must not inherit pytest's own working
            # directory (the checkout), so a relative operand can only ever resolve inside
            # this test's tmp_path.
            subprocess.run(
                [_git, "-C", str(tmp_path), *args],
                check=True,
                capture_output=True,
                timeout=60,
                cwd=tmp_path,
            )

        git("init", "-q")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "shipped.py").write_text("", encoding="utf-8")
        git("add", "src/shipped.py")
        # An untracked source file stays in scope: that is what ``--others`` is for.
        (tmp_path / "src" / "unstaged.py").write_text("", encoding="utf-8")

        def plant(rel: str) -> None:
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text("", encoding="utf-8")

        # The platform temp dir the process started with, pinned under the checkout
        # by a harness -- another run's ``--basetemp`` lands under it too...
        plant("var/tmp/other-run/bt/test_x0/wt-feature/.git")
        plant("var/tmp/other-run/bt/test_x0/copy.py")
        monkeypatch.setattr(source_corpus, "_TEMP_ROOT_AT_IMPORT", str(tmp_path / "var" / "tmp"))
        # ...the per-run root the root conftest redirects ``tempfile`` to, which need
        # not be under the first when a test redirected it again...
        plant("run/kc-pytest-u-1-abcd/fake-home/scripts/_common.sh")
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "run" / "kc-pytest-u-1-abcd"))
        # ...and pytest's own basetemp, found by pytest's marker where neither root
        # covers it (a lazy first import after the redirect).
        plant("elsewhere/pytest-of-u/pytest-3/popen-gw0/test_x0/copy.py")
        # A directory that merely LOOKS like scratch to a human is not excluded:
        # only the process's temp dirs and pytest's marker are.
        plant("tmp/kept.py")

        monkeypatch.setattr(source_corpus, "repo_root", lambda: tmp_path)
        repo_files.cache_clear()
        try:
            found = [p.relative_to(tmp_path).as_posix() for p in repo_files()]
        finally:
            repo_files.cache_clear()
        assert found == ["src/shipped.py", "src/unstaged.py", "tmp/kept.py"]


class TestNoGateEnumeratesTheCheckoutByWalking:
    """The ratchet: a repo-wide scan asks git, never the filesystem.

    This is the third time the class has been fixed one site at a time. The right
    answer was written inside one gate, with its own docstring naming the failure,
    and three later gates re-derived the walk anyway -- two of them latent, one
    fail-OPEN. A convention nothing pins does not propagate, so it is pinned here:
    a recursive scan whose root is the REPO ROOT is the offence. A scan rooted at a
    subdirectory is not, because ``src/kiro_crew`` and ``docs`` hold no second
    checkout and no ignored tree.
    """

    def test_no_gate_scans_the_checkout_from_the_filesystem(self):
        root = repo_root()
        offenders: list[str] = []
        for path in repo_files_named(".py"):
            rel = path.relative_to(root).as_posix()
            if rel.split("/")[0] not in _GATE_TREES or rel == _THE_ENUMERATOR:
                continue
            text = path.read_text(encoding="utf-8")
            if not (
                any(tok in text for tok in _SCAN_TOKENS)
                and any(tok in text for tok in _ROOT_TOKENS)
            ):
                continue
            offenders += [f"{rel}:{hit}" for hit in repo_root_scans(text)]
        assert offenders == [], (
            "these scans enumerate the checkout from the filesystem, which also "
            "descends every gitignored tree and any checkout nested under the repo "
            "root -- so the gate reports that copy as the offender, or keeps passing "
            "on it. Use source_corpus.repo_files()/repo_files_named() and keep the "
            f"scope filter in the gate: {offenders}"
        )

    def test_the_detector_flags_every_shape_it_claims_to(self):
        """Without this the gate above is a green line that checks nothing.

        Each case is asserted against the text prefilter too: a filter that is not
        a necessary condition of the AST match would skip the offending file, which
        looks exactly like having no offenders.
        """
        for label, source in {
            "constant-rglob": "REPO = Path(__file__).resolve().parents[1]\nREPO.rglob('*.py')\n",
            "parent-chain": "R = Path(__file__).resolve().parent.parent\nR.rglob('*.sh')\n",
            "os-walk": "REPO = Path(__file__).resolve().parents[1]\nos.walk(REPO)\n",
            "path-walk": "REPO = Path(__file__).resolve().parents[1]\nREPO.walk()\n",
            "bare-walk": "REPO = Path(__file__).resolve().parents[1]\nwalk(REPO)\n",
            "leading-star-glob": "R = Path(__file__).resolve().parents[1]\nR.glob('**/*.md')\n",
            "accessor": (
                "class C:\n"
                "    @staticmethod\n"
                "    def _repo_root():\n"
                "        return Path(__file__).resolve().parent.parent\n"
                "    def files(self):\n"
                "        return self._repo_root().rglob('SKILL.md')\n"
            ),
            "shared-accessor": "import source_corpus\nsource_corpus.repo_root().rglob('*.py')\n",
            "assigned-below-its-reader": (
                "def files():\n    return REPO.rglob('*.py')\n"
                "REPO = Path(__file__).resolve().parents[1]\n"
            ),
        }.items():
            assert any(tok in source for tok in _SCAN_TOKENS), f"{label}: prefilter skips it"
            assert any(tok in source for tok in _ROOT_TOKENS), f"{label}: prefilter skips it"
            assert repo_root_scans(source), label

    def test_the_detector_leaves_the_legitimate_shapes_alone(self):
        """A false positive here costs a gate its scan, so the misses are named."""
        for label, source in {
            # The receiver is the test/ directory, not the repo root.
            "test-dir": "HERE = Path(__file__).resolve().parent\nHERE.rglob('*.py')\n",
            # Anchored below the root: no ignored tree and no nested checkout there.
            "anchored-glob": (
                "REPO = Path(__file__).resolve().parents[1]\n"
                "REPO.glob('src/kiro_crew/**/*.py')\n"
            ),
            "subtree-rglob": "REPO = Path(__file__).resolve().parents[1]\n(REPO / 'docs').rglob('*.md')\n",
            # The overwhelmingly common `walk` in this suite, and not a filesystem one.
            "ast-walk": "for node in ast.walk(tree):\n    pass\n",
            # A fixture tree is the caller's own, and holds nothing but what it wrote.
            "tmp-path": "def t(tmp_path):\n    return list(tmp_path.rglob('*'))\n",
        }.items():
            assert repo_root_scans(source) == [], label
