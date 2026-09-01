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
"""

from __future__ import annotations

import ast
import functools
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


def candidate_sources(
    require_all: Sequence[str] = (),
    require_any: Sequence[str] = (),
) -> tuple[tuple[Path, str], ...]:
    """Files whose text holds every ``require_all`` and one of ``require_any``.

    An empty ``require_any`` imposes no alternation, so passing neither argument
    returns the whole corpus.
    """
    return tuple(
        (path, text)
        for path, text in source_texts()
        if all(lit in text for lit in require_all)
        and (not require_any or any(lit in text for lit in require_any))
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
