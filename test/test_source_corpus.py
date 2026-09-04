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
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

import pytest
import test_no_blocking_call_on_loop as blocking
import test_sandbox_off_loop as sandbox
import test_session_map_locking as session_map
from source_corpus import candidate_sources, source_texts, src_root, unreadable_files

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
        assert (root / "security.py").is_file(), f"{root} is not the kiro_crew package"
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
        assert "def redact" in by_name["security.py"]
        assert "def batched_save" in by_name["session_map.py"]

    def test_a_filter_returns_a_subset_and_no_filter_returns_everything(self):
        assert set(candidate_sources(blocking._REQUIRE_ALL)) <= set(source_texts())
        assert candidate_sources() == source_texts()


class TestFilterLiteralsStillMatchWhatTheGatesReject:
    """Each filter, applied to source the gate exists to fail on.

    This is the half a rename breaks: move ``batched_save`` and the gate keeps
    passing on a tree it can no longer see into, unless something asserts that
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
