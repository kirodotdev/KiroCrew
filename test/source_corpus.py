"""One cached read of the ``kiro_crew`` source tree, shared by every AST ratchet.

Several gates in this suite pin a structural property of the whole package by
walking ``src/kiro_crew/**/*.py`` and ``ast.parse``-ing each file. Independently
they each paid the same costs, and the suite paid them once per gate:

* the ``rglob`` walk and the read of ~41 MB of source (0.32 s), and
* a full ``ast.parse`` of all 1254 modules (2.8 s), even though a violation of
  any one gate can only exist in a file whose TEXT already contains the token
  that gate matches on.

So the corpus is read once here and cached, and each gate declares the literals
its pattern requires. :func:`parsed_candidates` parses only the files that can
possibly match, which is 10-22 files for the narrow gates and about a third of
the tree for the broad ones.

Two things this module deliberately does NOT do.

It does not cache parsed trees. Retaining all 1254 modules' ASTs measures at
~900 MB RSS and 3.2 M live nodes, which is per xdist worker, and it makes the
parse itself 4x slower (11.1 s vs 2.3 s) because every later generational
collection then traverses that live set -- a tax the whole rest of the session
pays. Trees are therefore yielded and dropped.

And it never narrows a gate. A literal is only accepted as a filter when the
gate's AST pattern cannot match without that literal appearing in the source
text, so filtering removes files that were always going to be non-matches.
Exclusions (``_vendor``, ``testing/``) stay in the calling gate, because which
files a gate polices is that gate's contract, not this module's.

The second half of the module answers the wider question -- *what files does the
CHECKOUT hold* -- for the gates that police more than the package (shell
scripts, ``SKILL.md``, the coverage omit list). :func:`repo_files` is the one
answer, because the obvious one is wrong in a way that stays green: see its
docstring.
"""

from __future__ import annotations

import ast
import functools
import os
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterator, Sequence
from pathlib import Path


def src_root() -> Path:
    """Locate the ``kiro_crew`` package.

    Prefer the importable package (correct regardless of CWD / install layout);
    fall back to the in-repo path so a gate also runs standalone under a bare
    ``python3`` with no deps installed.
    """
    try:
        import kiro_crew  # noqa: PLC0415

        return Path(kiro_crew.__file__).resolve().parent
    except Exception:
        return Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


@functools.lru_cache(maxsize=1)
def _read_tree() -> tuple[tuple[tuple[Path, str], ...], tuple[Path, ...]]:
    """``((path, text), ...), (unreadable, ...)`` for the whole package, once."""
    readable: list[tuple[Path, str]] = []
    unreadable: list[Path] = []
    for path in sorted(src_root().rglob("*.py")):
        try:
            readable.append((path, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            # Recorded rather than swallowed: a file no gate can read is a file
            # no gate can see, and `test_source_corpus.py` fails on a non-empty
            # list so going blind cannot look like a green run.
            unreadable.append(path)
    return tuple(readable), tuple(unreadable)


def source_texts() -> tuple[tuple[Path, str], ...]:
    """Every readable ``*.py`` under the package, as ``(path, text)`` pairs."""
    return _read_tree()[0]


def unreadable_files() -> tuple[Path, ...]:
    """Files the corpus could not decode. Expected empty; pinned by a test."""
    return _read_tree()[1]


def _nfkc(text: str) -> str:
    """NFKC-normalise, matching how CPython folds identifiers at parse time."""
    return unicodedata.normalize("NFKC", text)


@functools.lru_cache(maxsize=1)
def _normalized_texts() -> tuple[str, ...]:
    """NFKC-normalised copy of every file's text, in ``source_texts`` order.

    A gate matches on a bare identifier, and CPython NFKC-folds identifiers at
    parse time -- so a call written with a Unicode compatibility homoglyph of a
    guarded name (``delete_items_b\uff41tch``) is that ASCII name in the AST but
    NOT in the raw bytes. Filtering on raw text would skip the file and let the
    offender through green. Normalising the haystack (here) and the needle (in
    ``candidate_sources``) the same way closes that hole while keeping the
    narrowing: NFKC is a fixpoint on ASCII, so every raw ASCII match is
    preserved and only homoglyph spellings are newly caught. Computed once over
    the whole tree (~0.3s) and cached, like the read itself. ``source_texts``
    still returns the RAW text, which gates that scan comments or string
    literals (a ``# render-ok`` marker, an import alias) depend on.
    """
    return tuple(_nfkc(text) for _path, text in source_texts())


def candidate_sources(
    require_all: Sequence[str] = (),
    require_any: Sequence[str] = (),
) -> tuple[tuple[Path, str], ...]:
    """Files whose text holds every ``require_all`` and one of ``require_any``.

    An empty ``require_any`` imposes no alternation, so passing neither argument
    returns the whole corpus.
    """
    # Match on the NFKC-normalised text with NFKC-normalised needles, so a call
    # whose identifier is a Unicode compatibility homoglyph of a literal (which
    # CPython folds to that literal at parse time, making it a real AST match) is
    # not skipped by a raw-byte pre-filter. The yielded ``text`` stays RAW.
    all_n = tuple(_nfkc(lit) for lit in require_all)
    any_n = tuple(_nfkc(lit) for lit in require_any)
    texts = source_texts()
    norm = _normalized_texts()
    return tuple(
        (path, text)
        for (path, text), ntext in zip(texts, norm)
        if all(lit in ntext for lit in all_n) and (not any_n or any(lit in ntext for lit in any_n))
    )


def parsed_candidates(
    require_all: Sequence[str] = (),
    require_any: Sequence[str] = (),
    *,
    skip_syntax_errors: bool = True,
) -> Iterator[tuple[Path, str, ast.Module]]:
    """Yield ``(path, text, tree)`` for each candidate file, one tree at a time.

    Trees are not retained between iterations, so a gate over a third of the
    tree costs one parse and no lasting heap. With ``skip_syntax_errors`` false
    the ``SyntaxError`` propagates, for a gate that treats an unparseable module
    as a hole in its own coverage rather than as the compiler's problem.
    """
    for path, text in candidate_sources(require_all, require_any):
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            if skip_syntax_errors:
                continue
            raise
        yield path, text, tree


def repo_root() -> Path:
    """The checkout root -- the directory holding ``src/``, ``test/`` and ``.git``."""
    return Path(__file__).resolve().parent.parent


#: Directory names the no-git fallback below never descends. Each is a tree the
#: repo never commits (``.git`` itself, dependency and virtualenv trees, byte
#: caches), so pruning them only makes that walk cheaper -- it cannot make it
#: narrower than the answer git gives.
_UNTRACKABLE_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})


def _fallback_walk() -> list[Path]:
    """Approximate :func:`repo_files` by walking, for a checkout with no git.

    Prunes any directory that holds a ``.git`` entry of its own -- a nested
    checkout or worktree is a DIFFERENT repository, and its copy of every shipped
    file is not ours. That is the general rule, not a skip of one path: git draws
    the same line by refusing to descend into a nested repository, so this branch
    and the git branch agree on it. ``.git`` is a FILE in a worktree and a
    directory in a clone, hence ``exists()``.

    Honest about the rest: with no git there is no way to read the ignore rules,
    so a gitignored file that survives the prune above is in scope here and would
    not be under git. Which is why :func:`repo_files` reaches this branch ONLY with
    no ``.git`` at all (an sdist, where nothing generated those trees to begin
    with) or no git binary to ask -- never for a checkout whose git call merely
    failed.
    """
    root = repo_root()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames if d not in _UNTRACKABLE_DIRS and not (here / d / ".git").exists()
        ]
        found += [here / name for name in filenames]
    return found


@functools.lru_cache(maxsize=1)
def repo_files() -> tuple[Path, ...]:
    """Every file the checkout CONTAINS, enumerated the way git sees it.

    Not ``rglob``/``os.walk``, which is the answer a repo-wide gate reaches for
    and the one that fails while staying green. Both descend gitignored trees and
    nested checkouts, so a worktree under ``.claude/worktrees/`` (the Claude Code
    harness creates them there), a local ``.kirocrew-dev/`` data home or any
    scratch clone puts a SECOND copy of every shipped file in front of the gate.
    Where the gate reports offenders that is a false positive naming a path the
    author cannot edit; where it asserts ``any(...)`` over the matches it is
    worse -- a stale copy keeps satisfying the assertion after the real file lost
    the property. Git excludes both kinds: an ignored path by its ignore rules, a
    nested repository by not descending into one at all.

    ``--others --exclude-standard`` keeps a file that is not yet ``git add``ed in
    scope, so a newly written file is policed on the machine that wrote it and
    not only in CI, where the checkout is fully tracked.

    Scope exclusions (``_vendor``, ``test/``, generated trees) stay in the
    CALLING gate: this says what the checkout holds, never what a gate polices.
    """
    root = repo_root()
    argv = ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    try:
        out = subprocess.run(argv, capture_output=True, check=True, timeout=60, cwd=str(root))
    except OSError:
        # No git binary to ask. The walk is the only enumeration left.
        paths = _fallback_walk()
    except subprocess.SubprocessError as exc:
        # git ran and refused. With no ``.git`` that is an sdist and the walk is
        # right; WITH one it is an anomaly -- a leaked ``GIT_DIR``, an unreadable
        # index, ``safe.directory`` -- and falling back would answer WIDER than git
        # does, because the walk cannot read the ignore rules. Wider is the failure
        # this module exists to stop, and no floor can catch it: a second copy of
        # every shipped file is a surplus, not a shortage. So fail loudly instead.
        if (root / ".git").exists():
            stderr = getattr(exc, "stderr", b"") or b""
            raise RuntimeError(
                f"git could not enumerate {root}, which has a .git: "
                f"{stderr.decode('utf-8', 'replace').strip() or exc}. Every repo-wide "
                "gate reads this; walking instead would silently hand them the "
                "gitignored trees git excludes."
            ) from exc
        paths = _fallback_walk()
    else:
        # Bytes and an explicit decode rather than ``text=True``: a filename that
        # is not valid UTF-8 must not take every gate down with it. ``-z``
        # because git otherwise QUOTES such a path instead of emitting it.
        names = out.stdout.decode("utf-8", "surrogateescape").split("\0")
        paths = [root / name for name in names if name]
    # ``--cached`` also lists what cannot be read -- a file deleted in the working
    # tree but not yet staged (the index entry survives, so git still names it),
    # and a submodule gitlink, which arrives as a DIRECTORY path -- and ``--others``
    # lists a nested repository as its bare directory. ``is_file`` drops all three.
    scratch = _run_scratch_roots(root)
    return tuple(
        sorted(
            path for path in paths if not _is_run_scratch(path, root, scratch) and path.is_file()
        )
    )


#: pytest's per-user temp root. ``_pytest/tmpdir.py`` creates every ``tmp_path``
#: under ``<platform temp dir>/pytest-of-<user>/pytest-<n>/``; the name is pytest's
#: own marker, the same one the root conftest recognises in its residue scan.
_PYTEST_TEMP_ROOT_PREFIX = "pytest-of-"

#: The temp dir as this process FIRST saw it: the platform root every temp tree on
#: the host descends from. Read at import because nothing can recover it later --
#: the root conftest redirects ``tempfile`` to a per-run root UNDER it before the
#: first test runs, and a module is imported at collection, before that fixture.
#: Never resolved at import: that would ``stat`` on every import of a gate helper.
_TEMP_ROOT_AT_IMPORT = tempfile.gettempdir()


def _run_scratch_roots(root: Path) -> tuple[Path, ...]:
    """The temporary-file roots this process knows of that lie inside the checkout.

    Where the platform keeps temporary files is a property of the host, not of the
    checkout. A harness that pins ``TMPDIR`` under the repository (so a ``/tmp``
    reaper cannot delete a run's scratch mid-flight) puts every fixture the suite
    writes in front of ``git ls-files --others``: copies of shipped scripts under
    fake homes, nested repositories, and files another worker deletes between the
    enumeration and a gate's ``read_text``. None of that is what the checkout
    holds, and on a host whose temp dir is outside the checkout none of it is ever
    listed -- so a gate that reads it asserts the host's temp layout, not the tree.

    Two roots are named: the platform temp dir as it was at import
    (:data:`_TEMP_ROOT_AT_IMPORT`, the ancestor of every run's scratch on the host,
    including a concurrent run's ``--basetemp``) and the process's CURRENT
    ``tempfile`` dir, which the root conftest has redirected by the time a gate
    runs. pytest's ``basetemp`` is a child of the first; when a lazy import misses
    that, :func:`_is_run_scratch` still recognises it by pytest's own marker.
    Empty when the temp dir is not inside the checkout, which is every CI runner.
    """
    roots: list[Path] = []
    for candidate in (_TEMP_ROOT_AT_IMPORT, tempfile.gettempdir()):
        try:
            resolved = Path(candidate).resolve()
        except OSError:
            continue
        if resolved != root and resolved.is_relative_to(root) and resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _is_run_scratch(path: Path, root: Path, scratch: tuple[Path, ...]) -> bool:
    """Whether *path* is one of this run's temporary files rather than the checkout's.

    True under a root from :func:`_run_scratch_roots`, or under a directory pytest
    names as its temp root (:data:`_PYTEST_TEMP_ROOT_PREFIX`) anywhere strictly
    below the checkout root. A shipped directory carrying that name would be
    hidden from every gate, which is why the marker is pytest's own and not a
    generic ``tmp``.
    """
    if any(path.is_relative_to(candidate) for candidate in scratch):
        return True
    try:
        parents = path.relative_to(root).parts[:-1]
    except ValueError:
        return False
    return any(part.startswith(_PYTEST_TEMP_ROOT_PREFIX) for part in parents)


def repo_files_named(*suffixes: str) -> tuple[Path, ...]:
    """The :func:`repo_files` entries whose file name ends with one of *suffixes*."""
    return tuple(path for path in repo_files() if path.name.endswith(suffixes))


def _clear_caches() -> None:
    """Drop the cached raw and NFKC-normalised corpus text.

    ``_read_tree`` and ``_normalized_texts`` are each an ``lru_cache(maxsize=1)``
    over the whole ``src/`` tree (~80 MB raw text + ~80 MB of its NFKC copy), and
    once any gate in a worker calls either one, that ~160 MB sits on the heap for
    the rest of that worker's life -- it is never large enough to trigger a GC
    that would reclaim it, so it is pure retained RSS on every later test the
    worker runs. A caller that is done with the corpus for now (a module-scoped
    fixture at teardown) can drop it here; the next gate that needs it just pays
    the read again, which is the same one-time cost every gate already paid
    before this module existed to share it.
    """
    _read_tree.cache_clear()
    _normalized_texts.cache_clear()
