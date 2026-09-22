"""Tests for /api/file-grep — the side-panel Files rail's content search.

Three properties carry the endpoint, and each has its own class below:

1. **Gating.** The root goes through the same ``_validate_dashboard_path`` /
   ``is_sensitive_path`` chokepoint every other handler in ``files.py`` uses, and
   a hit is filtered by ``is_sensitive_path`` again on the way out — so a
   credential store is unreachable whichever engine ran.
2. **Engine parity.** ripgrep and the python fallback must answer the SAME tree
   the same way. They are separate implementations of one contract, and a host
   without ``rg`` must not get a different search.
3. **Budgets.** A result cap and a wall-clock deadline both report ``truncated``,
   and the document pass reports ``skipped_docs`` rather than silently omitting
   documents it never reached.

The document fixtures are built by hand (a ZIP holding the XML parts a parser
reads) so the suite needs no authoring library and no
binary fixture committed to the repo.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pdf_test_helpers import flate_bomb_pdf, text_pdf

from kiro_crew.dashboard.handlers import api_file_grep
from kiro_crew.dashboard.handlers import files as f

#: Skips the ripgrep half of a parity pair on a host without the binary.
#:
#: PURE, because a ``skipif`` condition is evaluated at COLLECTION.
#: ``_grep_rg_executable`` resolves through the provenance chokepoint, which reads
#: ``agent_writable_roots()`` and so ``workspace_root()`` -- and that does
#: ``mkdir(parents=True)``. Calling it here made merely IMPORTING this module
#: create the operator's real workspace directory. The repo's other gates
#: (``requires_git``, ``requires_symlinks`` in conftest) are pure for the same
#: reason.
requires_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


def _require_rg_engine() -> None:
    """Run-time half of :data:`requires_rg`: skip unless the endpoint MAY run it.

    ``shutil.which`` and the resolver disagree wherever the provenance chokepoint
    refuses a ripgrep that is on ``$PATH`` -- an NFS home whose parents stat as the
    overflow uid 65534, a root gateway, a world-writable parent. Without this the
    rg arm of a parity pair runs the PYTHON engine while claiming to cover
    ripgrep, and the pair asserts one engine twice while reporting two.

    Called from a test BODY, never at import, so the chokepoint's
    directory-creating lookup happens only when a test actually runs.
    """
    if f._grep_rg_executable() is None:
        pytest.skip("no ripgrep this endpoint may run (refused for its provenance)")


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/file-grep", api_file_grep)
    state = MagicMock()
    state.file_indexes.get.return_value = None
    app["state"] = state
    return app


@pytest.fixture(autouse=True)
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


async def _get(root: Path | str, query: str, **params: str) -> tuple[int, dict]:
    """One request against a fresh app. The client is built INSIDE the coroutine
    because ``TestClient`` binds to the running loop at construction."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/file-grep", json={"root": str(root), "q": query, **params})
        return resp.status, await resp.json()


async def _grep(root: Path | str, query: str, **params: str) -> dict:
    status, payload = await _get(root, query, **params)
    assert status == 200, payload
    return payload


def _files(payload: dict) -> set[str]:
    return {os.path.basename(r["file"]) for r in payload["results"]}


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A small text tree: three matches, one non-match, two ignored dirs, one
    binary."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "alpha.py").write_text("import os\nNEEDLE lives here\nafter\n", encoding="utf-8")
    (root / "notes.md").write_text("# heading\n\nprose about needle in md\n", encoding="utf-8")
    (root / "other.txt").write_text("nothing to find\n", encoding="utf-8")
    sub = root / "pkg"
    sub.mkdir()
    (sub / "deep.py").write_text("a\nb\nc\nd needle at four\n", encoding="utf-8")
    for ignored in ("node_modules", ".git"):
        d = root / ignored
        d.mkdir()
        (d / "vendored.py").write_text("needle should not be reported\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"needle\x00\x00binary\n")
    # A NUL well past any header sniff, with the query on a line AFTER it.
    # ripgrep considers the whole file binary at its first NUL wherever it
    # is, so the python engine must not answer for this file either.
    (root / "late.dat").write_bytes(b"x" * 4096 + b"\x00\nneedle after the nul\n")
    # A .gitignore and a file it covers. ripgrep honours ignore files by default
    # and the fallback's os.walk cannot, so without this the parity tests agreed
    # only because no fixture ever carried one.
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / "ignored.py").write_text("needle in an ignored file\n", encoding="utf-8")
    return root


# ── document fixtures ────────────────────────────────────────────────────────


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document)


def _write_pptx(path: Path, slides: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for number, text in enumerate(slides, 1):
            slide = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<p:sld"
                ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
                ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<a:t>{text}</a:t></p:sld>"
            )
            zf.writestr(f"ppt/slides/slide{number}.xml", slide)


@pytest.fixture()
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    _write_docx(root / "spec.docx", ["intro", "the WIDGET decision", "outro"])
    _write_pptx(root / "deck.pptx", ["cover", "agenda", "the WIDGET roadmap"])
    return root


# ── gating ───────────────────────────────────────────────────────────────────


class TestGating:
    @pytest.mark.asyncio
    async def test_a_short_query_is_not_a_search(self, tree):
        """One character matches nearly every file, which is a whole-tree read
        rather than a result set — the same floor /api/file-search applies."""
        payload = await _grep(tree, "n")
        assert payload["results"] == []
        assert payload["root"] == ""
        # No engine ran, so none is named. Answering "rg" here would have cost a
        # $PATH walk on the event loop once per keystroke to say something the
        # caller cannot use.
        assert payload["engine"] == ""

    @pytest.mark.asyncio
    async def test_a_missing_root_is_not_a_search(self):
        assert (await _grep("", "needle"))["results"] == []

    @pytest.mark.asyncio
    async def test_an_overlong_query_is_refused(self, tree):
        payload = await _grep(tree, "x" * (f._GREP_MAX_QUERY_CHARS + 1))
        assert payload["results"] == []

    @pytest.mark.asyncio
    async def test_a_sensitive_root_is_denied_not_searched(self, tree, mock_sel):
        """403, audited as denied. Not 404: telling the caller a credential store
        is 'not found' invites it to probe for the spelling that is allowed."""
        with patch.object(f, "is_sensitive_path", lambda p: True):
            status, payload = await _get(tree, "needle")
        assert status == 403
        assert payload["error"] == "Access denied"
        # The dashboard renders `error` into a localized UI; `code` is the
        # contract the error-code ratchet guards.
        assert payload["code"] == "sensitive_path"
        assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_the_sensitive_fence_runs_off_the_event_loop(self, tree):
        """WHERE the fence runs, not just that it refuses.

        `is_sensitive_path` resolves the path and walks its ancestors -- tens of
        syscalls for one path (72 `lstat`, 8 `realpath`, 6 `readlink` for an
        ordinary one). Called from the coroutine it parks the gateway's only event
        loop on whatever mount the caller named, which is the hazard the off-loop
        ratchet records for `shutil.which`. Its name does not look like filesystem
        work, so a static name list does not catch it and this asserts the
        placement directly.

        The probe runs it in a worker thread, so the recorded thread must not be
        the loop's.
        """
        seen: list[int] = []
        real = f.is_sensitive_path

        def recording(path, *args, **kwargs):
            seen.append(threading.get_ident())
            return real(path, *args, **kwargs)

        loop_thread = threading.get_ident()
        with patch.object(f, "is_sensitive_path", recording):
            await _grep(tree, "needle")

        assert seen, "the fence was never consulted"
        # The ROOT check happens inside `_grep_resolve_root`, which the handler
        # reaches only through `_run_path_probe`.
        assert loop_thread not in seen, "is_sensitive_path ran on the event loop"

    @pytest.mark.asyncio
    async def test_a_root_the_validator_refuses_is_denied(self, tree):
        """A refused name never becomes a search root. The validator is the gate
        this whole handler family shares, so its refusal must land as 403."""
        with patch.object(f, "_validate_dashboard_path", lambda raw: None):
            status, _ = await _get(tree, "needle")
        assert status == 403

    @pytest.mark.asyncio
    async def test_a_file_root_is_a_404_not_a_search(self, tree):
        status, payload = await _get(tree / "alpha.py", "needle")
        assert status == 404
        assert payload["results"] == []
        assert payload["code"] == "not_a_directory"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_a_sensitive_hit_is_filtered_out_by_either_engine(
        self, tree, engine, monkeypatch
    ):
        """The per-hit filter is the authority. ripgrep's exclusion globs only
        keep it from reading those bytes; a hit that reaches the parser anyway
        must still be dropped, and the fallback has no globs at all."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        secret = str(tree / "alpha.py")
        real = f.is_sensitive_path
        monkeypatch.setattr(
            f, "is_sensitive_path", lambda p: os.path.realpath(p) == secret or real(p)
        )
        payload = await _grep(tree, "needle")
        assert "alpha.py" not in _files(payload)
        assert "deep.py" in _files(payload)


# ── engine parity ────────────────────────────────────────────────────────────


class TestEngines:
    @pytest.mark.asyncio
    async def test_the_python_fallback_reports_file_line_and_preview(self, tree, monkeypatch):
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        payload = await _grep(tree, "needle")
        assert payload["engine"] == "python"
        assert payload["root"] == str(tree)
        hit = next(r for r in payload["results"] if r["file"].endswith("alpha.py"))
        assert hit["line"] == 2
        assert hit["preview"] == "NEEDLE lives here"
        assert "label" not in hit  # a text hit has a line to jump to
        # No column: the rail finds the match inside the preview itself, and a
        # second answer to the same question is a second thing to get wrong.
        assert "col" not in hit
        assert "late.dat" not in _files(payload), "a NUL past the header still means binary"

    @pytest.mark.asyncio
    @requires_rg
    async def test_ripgrep_reports_the_same_shape(self, tree):
        payload = await _grep(tree, "needle")
        assert payload["engine"] == "rg"
        hit = next(r for r in payload["results"] if r["file"].endswith("alpha.py"))
        assert hit["line"] == 2
        assert hit["preview"] == "NEEDLE lives here"
        assert "col" not in hit

    @pytest.mark.asyncio
    @requires_rg
    async def test_both_engines_answer_one_tree_identically(self, tree, monkeypatch):
        """The parity that matters: same files, same lines. A host without
        ripgrep must not get a different search, so the two engines are compared
        against EACH OTHER on one tree rather than each against its own
        expectations."""
        with_rg = await _grep(tree, "needle")
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        without_rg = await _grep(tree, "needle")
        assert with_rg["engine"] == "rg"
        assert without_rg["engine"] == "python"

        def key(payload: dict) -> list[tuple[str, int]]:
            return sorted((os.path.basename(r["file"]), r["line"]) for r in payload["results"])

        assert key(with_rg) == key(without_rg)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_ignored_directories_and_binaries_are_never_reported(
        self, tree, engine, monkeypatch
    ):
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        assert _files(await _grep(tree, "needle")) == {
            "alpha.py",
            "notes.md",
            "deep.py",
            "ignored.py",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_a_multibyte_line_survives_both_engines_intact(
        self, tmp_path, engine, monkeypatch
    ):
        """``rg --json`` is UTF-8 by definition, so the preview must come back as
        the file's own characters rather than mojibake from a host-locale decode."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        root = tmp_path / "utf8"
        root.mkdir()
        (root / "wide.txt").write_text("αβγδ needle ε\n", encoding="utf-8")
        payload = await _grep(root, "needle")
        assert payload["results"][0]["preview"] == "αβγδ needle ε"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_the_query_is_a_literal_not_a_regex_on_either_engine(
        self, tmp_path, engine, monkeypatch
    ):
        """The fallback matches ``re.escape(query)`` and the document pass uses
        ``str.find``, so ripgrep must match literally too. Unfixed, ``a.c`` was a
        wildcard on an rg host and three characters on every other host."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        root = tmp_path / "regex"
        root.mkdir()
        (root / "literal.txt").write_text("a.c is here\n", encoding="utf-8")
        (root / "wildcard.txt").write_text("abc is not the same string\n", encoding="utf-8")
        assert _files(await _grep(root, "a.c")) == {"literal.txt"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_an_unbalanced_query_is_a_search_not_a_parse_error(
        self, tmp_path, engine, monkeypatch
    ):
        """``config(`` is an ordinary thing to look for in code. As a regex it does
        not compile, and ripgrep's error exit under ``--no-messages`` is an empty
        stdout — which read as an authoritative "no matches"."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        root = tmp_path / "unbalanced"
        root.mkdir()
        (root / "call.py").write_text("value = config(key)\n", encoding="utf-8")
        assert _files(await _grep(root, "config(")) == {"call.py"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_case_is_ignored_whatever_the_query_looks_like(
        self, tmp_path, engine, monkeypatch
    ):
        """Both engines fold case UNCONDITIONALLY. Under ripgrep's --smart-case a
        capital in the query silently made text files case-sensitive while the
        document pass kept folding, so one response disagreed with itself."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        root = tmp_path / "casing"
        root.mkdir()
        (root / "lower.txt").write_text("the widget ships\n", encoding="utf-8")
        (root / "upper.txt").write_text("the WIDGET ships\n", encoding="utf-8")
        assert _files(await _grep(root, "Widget")) == {"lower.txt", "upper.txt"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_a_file_over_the_size_ceiling_is_skipped_by_both_engines(
        self, tmp_path, engine, monkeypatch
    ):
        """The fallback reads a bounded prefix, so without ``--max-filesize``
        ripgrep scanned a big log to EOF and reported a match the other host never
        saw. Both now skip the file: a bounded engine that silently searched HALF
        a file would be the worse divergence."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        monkeypatch.setattr(f, "_GREP_MAX_FILE_BYTES", 64)
        root = tmp_path / "big"
        root.mkdir()
        (root / "small.txt").write_text("needle\n", encoding="utf-8")
        (root / "large.log").write_text("x" * 200 + "\nneedle at the end\n", encoding="utf-8")
        assert _files(await _grep(root, "needle")) == {"small.txt"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_an_ignore_file_does_not_change_the_answer(self, tree, engine, monkeypatch):
        """ripgrep honours .gitignore by default and the fallback's os.walk cannot,
        so the same project answered differently depending on which host ran it.
        The noisy directories are pruned by _WALK_SKIP_DIRS on both sides instead —
        the half of the ignore semantics the two engines can agree on."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        assert "ignored.py" in _files(await _grep(tree, "needle"))


# ── budgets ──────────────────────────────────────────────────────────────────


class TestBudgets:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_the_result_cap_reports_truncated(self, tmp_path, engine, monkeypatch):
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        root = tmp_path / "many"
        root.mkdir()
        for i in range(6):
            (root / f"f{i}.txt").write_text("needle\n", encoding="utf-8")
        monkeypatch.setattr(f, "_GREP_MAX_RESULTS", 3)
        payload = await _grep(root, "needle")
        assert len(payload["results"]) == 3
        assert payload["truncated"] is True

    @pytest.mark.asyncio
    async def test_a_spent_deadline_reports_truncated_rather_than_no_matches(
        self, tree, monkeypatch
    ):
        """An exhausted budget is not an empty result set. Reporting one as the
        other would tell the user the text is absent when the search stopped."""
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        monkeypatch.setattr(f, "_GREP_TIME_BUDGET_SECS", -1.0)
        assert (await _grep(tree, "needle"))["truncated"] is True

    @pytest.mark.asyncio
    async def test_a_spent_deadline_counts_documents_it_never_opened(self, docs, monkeypatch):
        """``skipped_docs`` is what keeps 'no document matched' distinguishable
        from 'the budget ran out before the documents'. It is a FLOOR: the walk
        ENDS on a spent deadline rather than counting its way through the tree,
        and ``truncated`` is what says the number is not a total."""
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        monkeypatch.setattr(f, "_GREP_TIME_BUDGET_SECS", -1.0)
        payload = await _grep(docs, "widget")
        assert payload["skipped_docs"] >= 1
        assert payload["truncated"] is True
        assert payload["results"] == []

    @pytest.mark.asyncio
    async def test_a_spent_deadline_stops_the_document_walk_rather_than_counting_on(
        self, tmp_path, monkeypatch
    ):
        """Continuing past the deadline holds a bounded transfer worker for up to
        ``_GREP_MAX_DIRS_VISITED`` directories after the budget is already gone,
        and at keystroke rate that starves the pool every other file endpoint
        shares."""
        root = tmp_path / "deep"
        root.mkdir()
        here = root
        for level in range(6):
            here = here / f"level{level}"
            here.mkdir()
        visited: list[str] = []
        real_walk = f.os.walk

        def counting_walk(top, *args, **kwargs):
            for entry in real_walk(top, *args, **kwargs):
                visited.append(entry[0])
                yield entry

        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        monkeypatch.setattr(f, "_GREP_TIME_BUDGET_SECS", -1.0)
        monkeypatch.setattr(f.os, "walk", counting_walk)
        await _grep(root, "widget")
        # Both passes may receive the root from os.walk, then must stop before
        # advancing into the six document-free child directories.
        assert len(visited) <= 2, visited

    @pytest.mark.asyncio
    async def test_an_oversized_document_is_skipped_not_parsed(self, docs, monkeypatch):
        monkeypatch.setattr(f, "_GREP_DOC_MAX_BYTES", 1)
        # Counted from the fixture, not restated: a format added to or removed from
        # the document pass changes this number, and a hardcoded one describes a
        # tree the fixture does not build.
        planted = sum(1 for p in docs.iterdir() if p.suffix.lower() in f._GREP_DOC_EXTS)
        assert (await _grep(docs, "widget"))["skipped_docs"] == planted

    @pytest.mark.asyncio
    async def test_a_busy_probe_pool_is_a_coded_503(self, tree, monkeypatch):
        async def _busy(*args, **kwargs):
            raise f._PathProbeBusy()

        monkeypatch.setattr(f, "_run_path_probe", _busy)
        status, payload = await _get(tree, "needle")
        assert status == 503
        assert payload["code"] == "path_probe_busy"


# ── the document pass ────────────────────────────────────────────────────────


class TestDocumentPass:
    @pytest.mark.asyncio
    async def test_a_word_document_reports_no_location_at_all(self, docs):
        payload = await _grep(docs, "widget")
        hit = next(r for r in payload["results"] if r["file"].endswith("spec.docx"))
        # A .docx paragraph carries no page or section a reader can navigate to, so
        # the row names nothing: no line to invent, and no tag either. "doc" beside
        # a .docx filename says nothing the filename has not, and it rendered the
        # tooltip as "Match at doc."
        assert "label" not in hit
        assert hit["line"] == 0
        assert "WIDGET" in hit["preview"]

    @pytest.mark.asyncio
    async def test_a_deck_reports_the_slide_it_matched(self, docs):
        payload = await _grep(docs, "widget")
        hit = next(r for r in payload["results"] if r["file"].endswith("deck.pptx"))
        assert hit["label"] == "slide 3"
        assert "WIDGET" in hit["preview"]

    @pytest.mark.asyncio
    async def test_a_pdf_reports_the_page_it_matched(self, tmp_path, monkeypatch):
        # The extractor child costs an interpreter start; the budget under test
        # is the ceiling, not a CI runner's spawn latency.
        monkeypatch.setattr(f, "_GREP_TIME_BUDGET_SECS", 30.0)
        root = tmp_path / "papers"
        root.mkdir()
        (root / "paper.pdf").write_bytes(text_pdf("the WIDGET plan on page one"))
        payload = await _grep(root, "widget")
        hit = next(r for r in payload["results"] if r["file"].endswith("paper.pdf"))
        assert hit["label"] == "page 1"
        assert "WIDGET" in hit["preview"]
        assert payload["truncated"] is False
        assert payload["skipped_docs"] == 0

    @pytest.mark.asyncio
    async def test_a_flate_bomb_pdf_is_skipped_and_the_search_still_answers(
        self, tmp_path, caplog, monkeypatch
    ):
        """The bound this pass depends on: one page past the ceiling is a SKIP.

        The extractor runs in a child under ``RLIMIT_AS``, so the inflate that
        would have been this process's memory is refused there and reported as a
        ``memory`` failure. The pass counts the document in ``skipped_docs`` and
        marks the answer partial -- and still answers, with the .docx beside the
        bomb found, so this cannot pass by finding nothing. The log line names
        the failure kind: ``memory``, not ``timeout``, which is what tells the
        ceiling firing apart from the deadline giving up on a child still
        inflating.
        """
        monkeypatch.setattr(f, "_GREP_TIME_BUDGET_SECS", 30.0)  # the kind, not the clock
        root = tmp_path / "mixed"
        root.mkdir()
        (root / "bomb.pdf").write_bytes(flate_bomb_pdf())
        _write_docx(root / "spec.docx", ["the WIDGET decision"])

        with caplog.at_level("WARNING", logger=f.logger.name):
            payload = await _grep(root, "WIDGET")
        assert _files(payload) == {"spec.docx"}
        assert payload["truncated"] is True
        assert payload["skipped_docs"] == 1
        assert "bomb.pdf skipped: extractor memory" in caplog.text

    @pytest.mark.asyncio
    async def test_a_pdf_the_parser_refuses_is_a_settled_answer(self, tmp_path):
        """Not a PDF under a .pdf name: no hit, no skip, no partial flag -- the
        same shape as a workbook that is not a zip."""
        root = tmp_path / "mixed"
        root.mkdir()
        (root / "paper.pdf").write_bytes(b"%PDF-1.4\n\x00 the WIDGET plan\n")
        _write_docx(root / "spec.docx", ["the WIDGET decision"])

        payload = await _grep(root, "WIDGET")
        assert _files(payload) == {"spec.docx"}
        assert payload["truncated"] is False
        assert payload["skipped_docs"] == 0

    @pytest.mark.asyncio
    async def test_a_workbook_reports_its_sheet_and_row(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        root = tmp_path / "sheets"
        root.mkdir()
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Ledger"
        sheet.append(["date", "note"])
        sheet.append(["2026-01-01", "the WIDGET invoice"])
        book.save(root / "book.xlsx")
        payload = await _grep(root, "widget")
        hit = next(r for r in payload["results"] if r["file"].endswith("book.xlsx"))
        assert hit["label"] == "Ledger · row 2"
        assert "WIDGET" in hit["preview"]

    def test_a_workbook_with_a_bloated_inventory_is_refused_before_openpyxl(
        self, tmp_path, monkeypatch
    ):
        """openpyxl opens the container itself, so without the inventory vet a
        crafted workbook reaches the XML parser on its extension alone."""
        pytest.importorskip("openpyxl")
        path = tmp_path / "bomb.xlsx"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
        monkeypatch.setattr(f, "_SHEET_MAX_MEMBERS", 0)
        parse = f._grep_xlsx_segments(path.read_bytes(), str(path), f.time.monotonic() + 30)
        assert parse == ((), True)

    @pytest.mark.asyncio
    async def test_the_text_pass_never_reports_a_document_twice(self, docs):
        """Both engines skip the document extensions, so a container is named by
        exactly one pass — the one that can say WHERE inside it the match is."""
        payload = await _grep(docs, "widget")
        assert len(payload["results"]) == len({r["file"] for r in payload["results"]})
        # Every one is a DOCUMENT hit, which is what line 0 means -- a text hit
        # always carries a line >= 1. Not every one carries a label: a Word file has
        # no location inside it to name.
        assert all(r["line"] == 0 for r in payload["results"])

    @pytest.mark.asyncio
    async def test_document_matching_uses_ignorecase_without_unicode_expansion(self, tmp_path):
        root = tmp_path / "unicode"
        root.mkdir()
        _write_docx(root / "street.docx", ["Straße"])
        assert _files(await _grep(root, "strasse")) == set()


def _rg_spawn(
    monkeypatch, *, lines=(), returncode=0, wait_raises=False, patch_wrap=True, made=None
):
    """Point `_grep_rg` at a fake streaming child.

    `_grep_rg` reads `proc.stdout` line by line, so the fake's stdout is any
    iterable of record lines -- including an endless generator, which is how the
    cap is tested without materializing anything.
    """
    if patch_wrap:
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: "/usr/bin/rg")
        monkeypatch.setattr(f, "wrap_argv", lambda cmd: (cmd, None))
        monkeypatch.setattr(f, "cgroup_scope_argv", lambda cmd: cmd)

    class _Pipe:
        """Iterable like a text pipe, and closable like one."""

        def __init__(self, source):
            self._it = iter(source)
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.closed:
                raise StopIteration
            return next(self._it)

        def close(self):
            self.closed = True

    class _Stdin:
        """Records what the pattern channel was handed.

        Not a `StringIO`: the code under test CLOSES stdin so rg sees EOF and
        starts searching, and a closed `StringIO` refuses `getvalue()`. The
        recorded text has to outlive the close for a test to read it.
        """

        def __init__(self):
            self.written = ""
            self.closed = False

        def write(self, text):
            self.written += text
            return len(text)

        def close(self):
            self.closed = True

    class _Proc:
        pid = 4242

        def __init__(self):
            self.stdout = _Pipe(lines)
            # The pattern is handed over stdin, so the fake has to accept it --
            # and record it, which is how the "never in the argv" test proves the
            # query still reaches the child.
            self.stdin = _Stdin()
            self._reaped = False

        def poll(self):
            # None means "still running", which is what makes teardown kill it.
            return None if not self._reaped else returncode

        def wait(self, timeout=None):
            if wait_raises and not self._reaped:
                self._reaped = True
                raise subprocess.TimeoutExpired(cmd="rg", timeout=timeout or 1.0)
            self._reaped = True
            return returncode

    def _spawn(argv, **kw):
        proc = _Proc()
        if made is not None:
            made.append(proc)
        return proc

    monkeypatch.setattr(f, "popen_limited", _spawn)
    # Every path that stops the child early goes through this, so the helper owns
    # it rather than each test remembering: the fake pid is not a real process.
    killed: list[int] = []
    monkeypatch.setattr(f.platform_compat, "kill_process_tree", lambda pid: killed.append(pid))
    return killed


# ── helpers ──────────────────────────────────────────────────────────────────


class TestPreviewRedaction:
    """A preview is file content on its way to a screen.

    Unlike a read, the user never asked for this particular file: they asked
    "which file says X", and the answer quotes the line back. A project source
    file with an API key pasted into it therefore puts that key in the response,
    and the sensitive-path fence does not cover it -- that guards credential
    STORES, not a secret in ordinary code.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_a_credential_on_the_matching_line_is_redacted(
        self, tmp_path, engine, monkeypatch
    ):
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        root = tmp_path / "secrets"
        root.mkdir()
        (root / "settings.py").write_text(
            'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # deploy key\n', encoding="utf-8"
        )
        payload = await _grep(root, "deploy key")
        preview = payload["results"][0]["preview"]
        assert "AKIAIOSFODNN7EXAMPLE" not in preview
        assert "deploy key" in preview  # the match itself still reads

    @pytest.mark.asyncio
    async def test_a_credential_inside_a_document_is_redacted_too(self, tmp_path):
        """The document pass and the text pass share ``_grep_hit``, which is why
        the redaction lives there rather than at each engine: one chokepoint, so
        a third pass cannot be added without it."""
        root = tmp_path / "docsecret"
        root.mkdir()
        _write_docx(
            root / "runbook.docx",
            ["the WIDGET rotation", "token AKIAIOSFODNN7EXAMPLE is the WIDGET key"],
        )
        payload = await _grep(root, "WIDGET key")
        preview = payload["results"][0]["preview"]
        assert "AKIAIOSFODNN7EXAMPLE" not in preview

    def test_a_credential_in_the_PATH_is_redacted(self):
        """A path segment can itself be credential-shaped -- a directory or filename
        holding a token -- and this row is an egress boundary for a file the user never
        asked for by name. The listings in this module already redact the paths they
        return; a search result is no different."""
        hit = f._grep_hit("/repo/AKIAIOSFODNN7EXAMPLE/notes.txt", 3, "nothing secret here")
        assert "AKIA" not in hit["file"]
        # The clean segments survive, so the row still says where it is.
        assert "notes.txt" in hit["file"]

    def test_an_ordinary_path_is_returned_byte_for_byte(self):
        """Only a credential-shaped segment is replaced. If a clean path changed, every
        row would become unopenable, which is a far worse failure than the one the
        redaction prevents."""
        clean = "/repo/src/limits.py"
        assert f._grep_hit(clean, 42, "rate limit")["file"] == clean

    def test_every_string_a_row_carries_is_redacted(self):
        """The rule, not a list of fields. Three fields in this function have been
        flagged one per round -- preview, then label, then path -- because each fix
        covered the field that was named. This asserts over the row's strings, so a
        field added later without redaction fails here rather than in a review."""
        planted = "AKIAIOSFODNN7EXAMPLE"
        hit = f._grep_hit(
            f"/repo/{planted}/notes.txt",
            0,
            f"token = {planted}",
            label=f"{planted} \u00b7 row 12",
        )
        leaked = [key for key, value in hit.items() if isinstance(value, str) and planted in value]
        assert leaked == [], f"unredacted field(s): {leaked}"

    def test_a_credential_in_a_sheet_title_is_redacted_like_a_preview(self):
        """A label is document CONTENT, not a shape this endpoint chose -- a workbook's
        is its sheet title, which an author picks. It reaches the response and the audit
        resource string, so leaving it raw published whatever the title said."""
        hit = f._grep_hit("/r/budget.xlsx", 0, "totals", label="AKIAIOSFODNN7EXAMPLE row 12")
        assert "AKIA" not in hit["label"]

    def test_a_long_sheet_title_is_cut_like_a_preview(self):
        """Author-controlled text of any length, bounded like every other string this
        row carries."""
        hit = f._grep_hit("/r/b.xlsx", 0, "totals", label="s" * 4000 + " row 1")
        assert len(hit["label"]) <= f._GREP_LABEL_CHARS

    def test_an_oversize_record_is_skipped_and_the_answer_says_it_is_short(self, monkeypatch):
        """The queue bounds how MANY records it holds, not how big they are.
        `--max-count 1` bounds records per file and `--max-filesize` bounds a file at
        2 MB, so one matching line inside a minified blob is a multi-megabyte record and
        a full queue of them is hundreds of MB. Such a record is skipped -- its 400-char
        preview carries nothing actionable -- and the answer is marked short."""
        big = (
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": "/r/bundle.min.js"},
                        "line_number": 1,
                        "lines": {"text": "needle" + "x" * (f._GREP_RG_MAX_RECORD_BYTES + 10)},
                    },
                }
            )
            + "\n"
        )
        small = (
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": "/r/a.py"},
                        "line_number": 7,
                        "lines": {"text": "needle here"},
                    },
                }
            )
            + "\n"
        )

        _rg_spawn(monkeypatch, lines=(big, small))
        hits, truncated = f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        # The ordinary record still lands ...
        assert [h["file"] for h in hits] == ["/r/a.py"]
        # ... and the oversize one is reported as incompleteness, not dropped silently.
        assert truncated is True

    def test_redaction_runs_before_truncation(self, monkeypatch):
        """A secret straddling the preview cap would be CUT IN HALF first, and
        half a token matches no pattern -- so the cut has to come second."""
        monkeypatch.setattr(f, "_GREP_PREVIEW_CHARS", 40)
        line = "x" * 30 + "AKIAIOSFODNN7EXAMPLE" + " tail"
        hit = f._grep_hit("/r/a.py", 1, line)
        assert "AKIA" not in hit["preview"]
        assert len(hit["preview"]) <= 40


class TestTrustedRipgrep:
    """The gateway's ``$PATH`` can reach trees the agent writes. An ``rg`` planted
    there would run on the user's next keystroke with the gateway's own
    environment, so the resolved binary goes through the repo's
    executable-provenance chokepoint before it is ever spawned.

    What belongs here is the DELEGATION -- that the chokepoint is consulted and
    that its refusal degrades to the python engine. Which binaries it clears is
    its own contract, tested against real security descriptors and real mode
    bits in ``test_source_providers.py`` and ``test_windows_acl.py``; asserting
    that here a second time is what produced a subset that disagreed with it.
    """

    def test_the_chokepoint_decides_and_its_canonical_path_is_what_runs(self, monkeypatch):
        """The candidate goes in as ``which`` found it, and what comes back out is
        the chokepoint's own canonical answer -- not the pre-resolution spelling --
        so the spawn runs the file that was checked."""
        seen: list[str] = []

        def _validate(candidate: str) -> str:
            seen.append(candidate)
            return "/usr/bin/rg"

        monkeypatch.setattr(f.shutil, "which", lambda name: "/usr/local/bin/../../usr/bin/rg")
        monkeypatch.setattr(f, "validate_provider_executable", _validate)
        assert f._grep_rg_executable() == "/usr/bin/rg"
        assert seen == ["/usr/local/bin/../../usr/bin/rg"]

    def test_a_refusal_takes_the_python_engine_rather_than_failing_the_search(self, monkeypatch):
        """Every refusal reason the chokepoint has -- containment, a third owner, a
        root gateway, an unreadable ACL -- arrives here as one ``ValueError``, and
        none of them is an error for the user: the python engine answers the same
        question."""

        def _refuse(candidate: str) -> str:
            raise ValueError("executable is inside the agent-writable tree /w/proj")

        monkeypatch.setattr(f.shutil, "which", lambda name: "/w/proj/node_modules/.bin/rg")
        monkeypatch.setattr(f, "validate_provider_executable", _refuse)
        assert f._grep_rg_executable() is None

    def test_the_relaxed_policy_is_what_rg_asks_for(self, monkeypatch):
        """``require_protected`` escalates to the root-owned-only rule, and it is set
        by callers that hand the child provider CREDENTIALS. ``rg`` is handed none,
        so asking for it would refuse every ordinary Homebrew or ``~/.local/bin``
        install and buy nothing. Asserted here because it is the one policy choice
        this module makes; every other question is the chokepoint's."""
        seen: list[dict] = []

        def _validate(candidate: str, **kwargs: object) -> str:
            seen.append(dict(kwargs))
            return candidate

        monkeypatch.setattr(f.shutil, "which", lambda name: "/usr/bin/rg")
        monkeypatch.setattr(f, "validate_provider_executable", _validate)
        assert f._grep_rg_executable() == "/usr/bin/rg"
        assert seen == [{}], "rg must take the default relaxed policy"

    def test_the_real_policy_refuses_an_open_parent_and_keeps_a_plain_install(
        self, tmp_path, monkeypatch
    ):
        """The regression this delegation exists for, run against the REAL chokepoint
        rather than a stub, as a control and a variant differing in one bit.

        The control is a 0755 ``rg`` in a private directory -- the ordinary install
        the relaxed policy must keep. The variant is the same file whose parent is
        0777 and not sticky, so anyone on the host can swap the binary between the
        check and the spawn. A world-writable test applied to the binary alone
        cleared the variant; the chokepoint's walk over every parent is what
        refuses it.

        Skipped where the control cannot clear, which is not a pass: an NFS home
        (parents owned by the overflow uid 65534) or a root gateway refuses every
        candidate for reasons unrelated to the parent bits, and asserting the
        refusal there would hold no matter what this resolver did.
        """
        control_dir = tmp_path / "private"
        control_dir.mkdir(mode=0o755)
        control = control_dir / "rg"
        control.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        control.chmod(0o755)
        monkeypatch.setattr(f.shutil, "which", lambda name: str(control))
        if f._grep_rg_executable() is None:
            pytest.skip("this host's own path topology cannot clear the chokepoint")

        # ... and the SAME binary is refused once its directory is world-writable.
        control_dir.chmod(0o777)
        try:
            assert f._grep_rg_executable() is None
        finally:
            control_dir.chmod(0o755)

    def test_no_rg_means_the_python_engine(self, monkeypatch):
        """No candidate, no provenance question -- and the chokepoint is not called
        with ``None``, which would raise rather than fall back."""
        called: list[str] = []
        monkeypatch.setattr(f.shutil, "which", lambda name: None)
        monkeypatch.setattr(f, "validate_provider_executable", lambda c: called.append(c) or c)
        assert f._grep_rg_executable() is None
        assert called == []


class TestDocumentExclusionIsCaseInsensitive:
    """rg and the document pass must not both claim the same file.

    The document pass owns `.docx`/`.pptx`/`.xlsx` and matches them by
    `splitext(name)[1].lower()`, so it claims `REPORT.DOCX`. ripgrep's globs are
    case-SENSITIVE, so `!**/*.docx` would not exclude that spelling -- rg reads it as
    a binary blob and the response carried the file twice, positionless from rg
    and with a page from the document pass.
    """

    def test_document_extensions_are_excluded_case_insensitively(self):
        argv = f._grep_rg_argv("/r")
        for ext in f._GREP_DOC_EXTS:
            assert ["--iglob", f"!**/*{ext}"] == [
                argv[argv.index(f"!**/*{ext}") - 1],
                f"!**/*{ext}",
            ], f"{ext} must be excluded with --iglob, not --glob"

    def test_no_document_extension_is_left_on_a_case_sensitive_glob(self):
        """The rule as a rule: asserted over the whole argv so an extension added
        to `_GREP_DOC_EXTS` later cannot arrive on a `--glob`."""
        argv = f._grep_rg_argv("/r")
        case_sensitive = {argv[index + 1] for index, token in enumerate(argv) if token == "--glob"}
        for ext in f._GREP_DOC_EXTS:
            assert f"!**/*{ext}" not in case_sensitive

    def test_the_walk_skip_dirs_stay_case_sensitive(self):
        """The deliberate asymmetry, pinned so it is not "tidied" into --iglob.

        The python walk screens directories by EXACT name, so folding case here
        would make ripgrep skip a `Node_Modules` the fallback still descends
        into -- the engines answering different questions, which is what the rest
        of this argv exists to prevent."""
        argv = f._grep_rg_argv("/r")
        case_insensitive = {
            argv[index + 1] for index, token in enumerate(argv) if token == "--iglob"
        }
        for skipped in f._WALK_SKIP_DIRS:
            assert f"!**/{skipped}" not in case_insensitive

    def test_every_glob_is_still_negated(self):
        """Unchanged property, re-asserted because this round touched the flag
        name: ONE non-negated glob flips ripgrep's whole set into allowlist mode
        and would silently exclude every file the set does not name."""
        argv = f._grep_rg_argv("/r")
        for index, token in enumerate(argv):
            if token in {"--glob", "--iglob"}:
                assert argv[index + 1].startswith("!"), argv[index + 1]


class TestFinishedSearchIsNotReportedPartial:
    """The reader announces end-of-stream with `put_nowait(None)`, which is
    non-blocking by necessity -- a blocking put on a full queue wedges the thread.
    So a full queue at that instant DROPS the sentinel, and nothing retries it."""

    def test_a_dropped_sentinel_ends_the_search_instead_of_costing_the_deadline(self, monkeypatch):
        """A search whose sentinel is dropped must still end when it finishes.

        Blocking for the whole remaining budget on the first `Empty` and then
        reporting `truncated` would call a FINISHED search partial and hold the
        user for the rest of the budget to say so, which is what the elapsed-time
        assertion below fences.

        The sentinel is dropped deterministically rather than by racing a real full
        queue: `put_nowait` always refuses, which is exactly the state the reader's
        `finally` hits when the queue is full.
        """
        real_queue_cls = f.queue.Queue

        class _DropsSentinel(real_queue_cls):  # type: ignore[valid-type,misc]
            def put_nowait(self, item):
                raise f.queue.Full

        monkeypatch.setattr(f.queue, "Queue", _DropsSentinel)
        record = (
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": "/r/a.py"},
                        "line_number": 7,
                        "lines": {"text": "needle here"},
                    },
                }
            )
            + "\n"
        )
        _rg_spawn(monkeypatch, lines=(record,))

        started = f.time.monotonic()
        hits, truncated = f._grep_rg("/r", "needle", started + 30)
        elapsed = f.time.monotonic() - started

        # The record still lands ...
        assert [h["file"] for h in hits] == ["/r/a.py"]
        # ... the answer is COMPLETE ...
        assert truncated is False
        # ... and it did not wait out the 30s budget to say so.
        assert elapsed < 5, f"took {elapsed:.1f}s; the deadline should not be the exit"

    def test_a_reader_still_running_does_not_end_the_search_early(self, monkeypatch):
        """The other half of the same branch: an empty queue with a LIVE reader is
        rg still traversing and has to keep waiting, or a slow root would report
        "no matches" the moment it paused. Here the deadline is already spent, so
        the loop caps rather than claiming a complete empty answer."""
        _rg_spawn(monkeypatch, lines=())
        hits, truncated = f._grep_rg("/r", "needle", f.time.monotonic() - 1)
        assert hits == []
        assert truncated is True


class TestTheQueryNeverReachesTheCommandLine:
    """A child's arguments are readable by other accounts on the host through
    `/proc/<pid>/cmdline` and `ps`.

    This handler already treats the query as secret-class -- it redacts it before
    every SEL write -- because "which file holds this key" is an ordinary reason to
    type a credential into a search box. Putting it in the argv published it to the
    whole host for the life of the process, contradicting the redaction two
    functions away.
    """

    def test_no_part_of_the_argv_carries_the_query(self):
        """The rule over the whole argv, not a check of the last three tokens: the
        query must not appear as an operand, inside a glob, or anywhere else."""
        argv = f._grep_rg_argv("/r")
        assert "needle" not in argv
        assert not any("needle" in token for token in argv)

    def test_the_pattern_channel_is_stdin(self):
        argv = f._grep_rg_argv("/r")
        assert argv[argv.index("--file") + 1] == "-"

    def test_the_query_still_reaches_the_child_over_that_channel(self, monkeypatch):
        """Keeping it off the command line is only correct if the search still runs,
        so the fake records what was written and this asserts the pattern arrived --
        newline-terminated, because `--file` is line-delimited."""
        made: list = []
        _rg_spawn(monkeypatch, lines=(), made=made)
        # A future deadline: `_grep_rg` returns before spawning anything when the
        # budget is already spent, and then this would assert on an empty list.
        f._grep_rg("/r", "AKIAIOSFODNN7EXAMPLE", f.time.monotonic() + 5)
        assert made, "no child was spawned"
        assert made[0].stdin.written == "AKIAIOSFODNN7EXAMPLE\n"
        # Closed too, or rg would wait for EOF that never comes.
        assert made[0].stdin.closed is True

    def test_a_child_that_exits_before_reading_the_pattern_does_not_raise(self, monkeypatch):
        """rg can be gone before the write lands -- a rejected flag, a refused root.
        A BrokenPipeError there would surface as a 500 on an ordinary search, so it
        takes the same fallback an error exit does."""
        made: list = []
        _rg_spawn(monkeypatch, lines=(), made=made)

        class _Broken:
            def write(self, _text):
                raise BrokenPipeError(32, "Broken pipe")

            def close(self):
                pass

        monkeypatch.setattr(
            f, "popen_limited", lambda argv, **kw: _closed_stdin_proc(made, _Broken())
        )
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) is not None


def _closed_stdin_proc(made, stdin):
    """A child whose stdin refuses the write, otherwise inert."""

    class _Pipe:
        def __iter__(self):
            return iter(())

        def close(self):
            pass

    class _Proc:
        pid = 4243

        def __init__(self):
            self.stdin = stdin
            self.stdout = _Pipe()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    proc = _Proc()
    made.append(proc)
    return proc


class TestTheQueryTravelsInTheBody:
    """A user searching for a token VALUE types the token, and a request URL is
    what proxies and access logs retain -- so the query is never in one."""

    @pytest.mark.asyncio
    async def test_a_get_with_the_query_in_the_url_is_not_a_route(self, tree):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-grep?root={tree}&q=needle")
            assert resp.status == 405

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["[1, 2]", '"needle"', "not json"])
    async def test_a_body_that_is_not_an_object_is_refused_not_a_500(self, tree, raw):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/file-grep", data=raw, headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400
            assert "code" in await resp.json()

    @pytest.mark.asyncio
    async def test_non_string_fields_are_treated_as_absent(self, tree):
        """`str(None or "")` rather than `.get("q", "")`: a JSON `null` is a value,
        and `.strip()` on it would be a 500."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/file-grep", json={"root": None, "q": None})
            assert resp.status == 200
            assert (await resp.json())["engine"] == ""


class TestANewlineQueryIsAnsweredEmpty:
    """`--file` is line-delimited, so a two-line pattern would be two patterns
    OR-ed together while the python pass matches one literal. Both engines are
    line-oriented, so a pattern spanning a newline matches nothing either way --
    which makes the empty answer the true one rather than a restriction."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["nee\ndle", "nee\rdle", "nee\r\ndle"])
    async def test_a_query_containing_a_line_break_returns_the_empty_answer(self, tmp_path, query):
        """A JSON body carries the control character verbatim, so this is the
        exact string the handler sees.

        The file below contains BOTH halves on separate lines, so an `--file`
        pattern list split into two OR-ed patterns would match it -- the exact
        divergence this guard prevents.
        """
        root = tmp_path / "tree"
        root.mkdir()
        (root / "a.py").write_text("needle here\ndle also\n", encoding="utf-8")
        payload = await _grep(root, query)
        assert payload["results"] == []
        assert payload["engine"] == ""
        assert payload["truncated"] is False

    @pytest.mark.asyncio
    async def test_trailing_whitespace_is_still_only_trimmed(self, tmp_path):
        """The guard must not swallow the ordinary case: a query the user typed with
        a trailing newline is TRIMMED by the handler's `.strip()` and still searches,
        because nothing about it spans two lines."""
        root = tmp_path / "tree"
        root.mkdir()
        (root / "a.py").write_text("needle here\n", encoding="utf-8")
        payload = await _grep(root, "needle\n")
        # `_files` uses os.path.basename: splitting on '/' is not a basename on
        # Windows, where the separator is a backslash.
        assert _files(payload) == {"a.py"}


class TestAParseIsBoundedByTheDeadline:
    """The character cap bounds TEXT, not work.

    A workbook of millions of empty rows yields no text at all -- so
    `_GREP_DOC_MAX_CHARS` never trips and the loop runs to the end of the sheet
    however long that takes. The row loop watches the shared deadline for exactly
    the rows the cap cannot see.
    """

    def test_a_workbook_of_empty_rows_stops_at_the_deadline(self, tmp_path, monkeypatch):
        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "blank.xlsx"
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Wide"
        # Empty rows produce no text at all, so nothing here moves the char cap.
        for _ in range(4000):
            sheet.append([None])
        sheet.append(["the WIDGET total"])
        book.save(path)
        monkeypatch.setattr(f, "_GREP_ROW_DEADLINE_STRIDE", 1)

        segments, whole = f._grep_xlsx_segments(
            path.read_bytes(), str(path), f.time.monotonic() - 1
        )
        # Cut short, and SAYING so: the answer must not claim it saw the sheet.
        assert whole is False
        assert segments == ()

    def test_a_workbook_within_the_deadline_reports_complete(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "ok.xlsx"
        book = openpyxl.Workbook()
        book.active.title = "Ledger"
        book.active.append(["the WIDGET invoice"])
        book.save(path)
        segments, whole = f._grep_xlsx_segments(
            path.read_bytes(), str(path), f.time.monotonic() + 30
        )
        assert whole is True
        assert any("WIDGET" in text for _label, text in segments)


class TestSymlinkedFilesAreSkipped:
    """ripgrep does not follow symlinks while traversing, so a link to a matching
    file is invisible to that engine. Reading it in the python walk makes one tree
    answer differently depending on whether the host has ripgrep -- and a link can
    point outside the root the caller named, so the walk would read content from a
    tree it was never asked to search."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_a_symlinked_file_is_not_reported(self, tmp_path, engine, monkeypatch):
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        else:
            _require_rg_engine()
        root = tmp_path / "tree"
        root.mkdir()
        # The real file lives OUTSIDE the searched tree, which is the case worth
        # failing on: following the link reads bytes from a tree the caller did
        # not name.
        outside = tmp_path / "outside"
        outside.mkdir()
        real = outside / "notes.txt"
        real.write_text("the WIDGET plan\n", encoding="utf-8")
        os.symlink(real, root / "linked.txt")
        # A control, so the test cannot pass by finding nothing at all.
        (root / "plain.txt").write_text("the WIDGET plan\n", encoding="utf-8")

        payload = await _grep(root, "WIDGET")
        names = {os.path.basename(r["file"]) for r in payload["results"]}
        assert names == {"plain.txt"}, f"engine {engine} reported {names}"

    @pytest.mark.asyncio
    async def test_a_symlink_inside_the_tree_is_skipped_not_deduplicated(
        self, tmp_path, monkeypatch
    ):
        """Even when the target is inside the root, the link is skipped rather than
        reported as a second path for the same bytes -- one row per file, and a link
        is not the file."""
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        root = tmp_path / "tree"
        root.mkdir()
        target = root / "real.txt"
        target.write_text("the WIDGET plan\n", encoding="utf-8")
        os.symlink(target, root / "alias.txt")

        payload = await _grep(root, "WIDGET")
        names = {os.path.basename(r["file"]) for r in payload["results"]}
        assert names == {"real.txt"}

    @pytest.mark.asyncio
    async def test_a_symlinked_document_is_not_reported(self, tmp_path):
        """The document pass reads every extension the text pass hands over, so it
        needs the guard in its own right: `safe_read_prefix` re-resolves the link and
        refuses credential STORES, but an ordinary document outside the root is not
        sensitive, and reading it labels out-of-root content with an in-root path.

        No engine parametrization: documents are python-walked on every host,
        because neither engine can read a zip container.
        """
        root = tmp_path / "tree"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        _write_docx(outside / "secret.docx", ["the WIDGET plan"])
        os.symlink(outside / "secret.docx", root / "linked.docx")
        # A control, so the test cannot pass by finding nothing at all.
        _write_docx(root / "plain.docx", ["the WIDGET plan"])

        payload = await _grep(root, "WIDGET")
        names = {os.path.basename(r["file"]) for r in payload["results"]}
        assert names == {"plain.docx"}


class TestPartialParsesAreHonest:
    """A parse that did not see all of a document's text must say so: a hidden
    match is the one thing a search result cannot afford to be quiet about."""

    def test_a_mid_read_failure_is_not_whole(self, tmp_path, monkeypatch):
        """Rows already read are real and are kept; the reader did not see the
        rest, so the answer is short."""
        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "breaks.xlsx"
        book = openpyxl.Workbook()
        book.active.title = "Ledger"
        book.active.append(["the WIDGET invoice"])
        book.save(path)

        class _Boom:
            """A sheet that yields one row and then fails."""

            title = "Ledger"

            def iter_rows(self, **_kw):
                yield ("the WIDGET invoice",)
                raise RuntimeError("the workbook broke here")

        class _BreakingBook:
            """Stands in for the whole workbook.

            NOT a patched real `Workbook`: assigning `type(loaded).worksheets`
            rewrites openpyxl's CLASS, which monkeypatch does not undo, so the
            stand-in sheet leaks into every later test in the process. Only
            `worksheets` and `close` are used by the code under test.
            """

            worksheets = (_Boom(),)

            def close(self):
                pass

        monkeypatch.setattr(openpyxl, "load_workbook", lambda *a, **kw: _BreakingBook())
        segments, whole = f._grep_xlsx_segments(
            path.read_bytes(), str(path), f.time.monotonic() + 30
        )

        # What it DID read survives -- discarding it would lose a real hit ...
        assert any("WIDGET" in text for _label, text in segments)
        # ... and the answer says it is short.
        assert whole is False

    def test_the_character_cap_is_not_whole(self, tmp_path, monkeypatch):
        """A match past the cap is invisible, so the answer must say it is partial."""
        openpyxl = pytest.importorskip("openpyxl")
        monkeypatch.setattr(f, "_GREP_DOC_MAX_CHARS", 20)
        path = tmp_path / "long.xlsx"
        book = openpyxl.Workbook()
        book.active.title = "Ledger"
        for _ in range(50):
            book.active.append(["the WIDGET invoice row"])
        book.save(path)

        _segments, whole = f._grep_xlsx_segments(
            path.read_bytes(), str(path), f.time.monotonic() + 30
        )
        assert whole is False, "a cap that hides matches must not claim whole"

    def test_a_wide_row_is_built_inside_the_budget_not_joined_then_cut(self, tmp_path, monkeypatch):
        """openpyxl returns the SAME shared string for every cell that references
        it, so a wide row is N references until a join makes it N copies -- and the
        zip expansion gate counts the string once. The row must be assembled inside
        the remaining budget, so no string longer than the cap is ever built."""
        openpyxl = pytest.importorskip("openpyxl")
        monkeypatch.setattr(f, "_GREP_DOC_MAX_CHARS", 100)
        path = tmp_path / "wide.xlsx"
        book = openpyxl.Workbook()
        book.active.title = "Wide"
        big = "the WIDGET " * 8  # 88 chars, shared by every cell
        book.active.append([big] * 200)  # ~17.6k chars if the row were joined whole
        book.save(path)

        segments, whole = f._grep_xlsx_segments(
            path.read_bytes(), str(path), f.time.monotonic() + 30
        )
        assert whole is False
        assert sum(len(text) for _label, text in segments) <= 100
        # The cut is at the cap, not a refusal of the row: the match inside the
        # budget is still found.
        assert any("WIDGET" in text for _label, text in segments)

    @pytest.mark.asyncio
    async def test_a_capped_document_marks_the_answer_partial(self, tmp_path, monkeypatch):
        """End to end: the flag has to reach the response, or the user is told a short
        answer is complete. Covers the docx/pptx path, whose truncation is detectable
        only because `extract_text` is asked for one character past the cap."""
        monkeypatch.setattr(f, "_GREP_DOC_MAX_CHARS", 20)
        root = tmp_path / "docs"
        root.mkdir()
        _write_docx(root / "spec.docx", ["the WIDGET decision " * 40])

        payload = await _grep(root, "WIDGET")
        assert payload["truncated"] is True


class TestAuditRedaction:
    @pytest.mark.asyncio
    async def test_a_secret_typed_as_the_query_never_reaches_the_audit_log(
        self, tree, mock_sel, monkeypatch
    ):
        """Previews are redacted for the pasted-secret case; a user grepping for a
        token VALUE types the token, which is the same class of text."""
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        await _grep(tree, "AKIAIOSFODNN7EXAMPLE")
        for call in mock_sel.log_api_access.call_args_list:
            assert "AKIAIOSFODNN7EXAMPLE" not in str(call.kwargs.get("resources", ""))


class TestWorkbookExpansion:
    def test_a_workbook_that_expands_past_the_cap_never_reaches_openpyxl(
        self, tmp_path, monkeypatch
    ):
        """The shared inventory vet bounds member COUNT and central-directory
        size, not expansion, so a single hugely-compressed member passed it and
        reached openpyxl's XML parser."""
        pytest.importorskip("openpyxl")
        path = tmp_path / "bomb.xlsx"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
            zf.writestr("xl/big.bin", b"\0" * (2 * 1024 * 1024))
        opened: list[str] = []
        monkeypatch.setattr(f, "_SHEET_MAX_EXPANDED_BYTES", 1024)

        real_import = __import__

        def tracking_import(name, *args, **kwargs):
            if name == "openpyxl":
                opened.append(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", tracking_import)
        parse = f._grep_xlsx_segments(path.read_bytes(), str(path), f.time.monotonic() + 30)
        assert parse == ((), True)
        # The refusal is BEFORE the parser: reaching openpyxl and bailing out
        # later would already have paid the expansion this cap exists to refuse.
        assert opened == []

    def test_a_workbook_within_the_cap_is_still_read(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "small.xlsx"
        book = openpyxl.Workbook()
        book.active.title = "Ledger"
        book.active.append(["note"])
        book.active.append(["the WIDGET invoice"])
        book.save(path)
        segments, whole = f._grep_xlsx_segments(
            path.read_bytes(), str(path), f.time.monotonic() + 30
        )
        assert whole is True
        assert any("WIDGET" in text for _label, text in segments)


class TestHelpers:
    def test_the_sensitive_globs_are_derived_from_the_shared_fence(self):
        """Derived, never a second hand-kept list: a copy here would be the thing
        that silently stops matching what ``is_sensitive_path`` blocks.

        Asserted with HOME as the root, because that is the only root the stores
        are actually under -- which is the whole point of the anchoring below."""
        from kiro_crew.security import sensitive_home_dirs

        home = os.path.expanduser("~")
        args = f._grep_sensitive_globs(home)
        assert args[0::2] == ["--iglob"] * (len(args) // 2)
        patterns = {p.casefold() for p in args[1::2]}
        for entry in sensitive_home_dirs():
            relative = entry.strip("/")
            if relative:
                # CASE is folded, separators are NOT. The emitted spelling is the
                # ON-DISK one: ``sandbox_credential_targets`` realpaths every target,
                # and on Windows that folds a leaf to the real directory name, so the
                # ``AppData/Local/kiro-cli`` fence entry surfaces as
                # ``!/AppData/Local/Kiro-Cli``. Case cannot cost coverage -- every
                # pattern is emitted under ``--iglob``, which ripgrep matches
                # case-insensitively -- so byte-identical case is a stricter claim
                # than the contract makes, and one only a case-insensitive volume can
                # break.
                #
                # Separators carry a real claim and stay compared exactly: ripgrep
                # reads only ``/`` as a separator, which is why the emitting side runs
                # each path through ``PurePath.as_posix``, so a pattern carrying
                # Windows backslashes matches nothing and the store gets read.
                # ``os.path.normcase`` would hide that regression, because on Windows
                # it rewrites ``/`` to ``\`` on both sides of this comparison;
                # ``casefold`` drops the case claim alone. The sibling anchor test
                # checks the leading ``!/`` only, so it is not a net for an
                # internal-separator slip.
                assert f"!/{relative}".casefold() in patterns

    def test_every_exclusion_is_anchored_to_the_root(self):
        """The leading slash is the anchor, and without it the exclusion is not
        root-relative at all.

        ripgrep follows gitignore semantics, under which a pattern containing NO
        slash matches a BASENAME at any depth. So a bare `!.ssh` hides every `.ssh`
        in the tree -- the unanchored behaviour this function exists to remove --
        and it does it silently, because the docstring says "root-relative" either
        way. Asserted over every emitted pattern rather than for one entry, since a
        single-component leaf is the only shape that loses its anchor.
        """
        home = os.path.expanduser("~")
        for pattern in f._grep_sensitive_globs(home)[1::2]:
            assert pattern.startswith("!/"), pattern

    def test_a_project_root_outside_home_gets_no_exclusions(self, tmp_path):
        """The divergence this anchoring removes. `is_sensitive_path` is
        HOME-anchored, so a project's own `.npmrc` is an ordinary file and the
        python walk searches it. An unanchored `!**/.npmrc` excluded it at any
        depth, so the same query answered differently depending on whether the host
        had ripgrep -- and the rg host silently returned less."""
        project = tmp_path / "proj"
        project.mkdir()
        assert f._grep_sensitive_globs(str(project)) == []

    def test_a_store_that_really_is_under_the_root_is_still_excluded(self, tmp_path):
        """The optimisation is kept where it is real: searching a tree that DOES
        contain a credential store still tells ripgrep not to read those bytes."""
        home = os.path.expanduser("~")
        patterns = set(f._grep_sensitive_globs(home)[1::2])
        assert "!/.aws" in patterns
        # Neither unanchored spelling: `!**/.aws` matches at any depth explicitly,
        # and a slash-less `!.aws` matches at any depth under gitignore semantics.
        assert "!**/.aws" not in patterns
        assert "!.aws" not in patterns

    def test_the_two_engines_agree_about_a_project_local_credential_name(self):
        """The parity claim stated over the pair rather than over one engine: no
        exclusion the rg argv carries may hide a name the python walk would search
        under the same root."""
        from kiro_crew.security import is_sensitive_path

        project = "/srv/checkout"
        argv = f._grep_rg_argv(project)
        excluded = {argv[i + 1].lstrip("!") for i, tok in enumerate(argv) if tok == "--iglob"}
        for name in (".npmrc", ".netrc", ".pypirc", ".git-credentials"):
            if not is_sensitive_path(f"{project}/{name}"):
                assert name not in excluded, f"rg would skip {name} that python searches"

    def test_the_argv_carries_every_flag_the_parity_claim_rests_on(self):
        """Each flag closes a divergence the fallback cannot match without it.

        Asserted on the argv rather than through behaviour so the check runs on a
        host with no ripgrep installed."""
        argv = f._grep_rg_argv("/r")
        assert "--fixed-strings" in argv  # the fallback matches a literal
        assert "--ignore-case" in argv  # the fallback folds case unconditionally
        assert "--smart-case" not in argv  # ... which --smart-case does not
        assert "--no-ignore" in argv  # os.walk cannot honour ignore files
        assert argv[argv.index("--max-count") + 1] == "1"  # one hit per file
        # The fallback skips a file over the ceiling; rg must skip it too.
        assert argv[argv.index("--max-filesize") + 1] == str(f._GREP_MAX_FILE_BYTES)
        for ext in f._GREP_DOC_EXTS:
            assert f"!**/*{ext}" in argv  # the document pass owns these

    def test_the_argv_refuses_project_controlled_config(self):
        """rg reads flags from the file `RIPGREP_CONFIG_PATH` names, and one flag it
        accepts is `--pre=<binary>`, which runs an arbitrary executable -- so without
        this a search executes whatever that file says. The child inherits this
        process's environment and the sandbox does not scrub the variable, so the flag
        is the fence. It also keeps a config file from injecting `--smart-case` or a
        glob, which would make rg answer a different question than the fallback."""
        assert "--no-config" in f._grep_rg_argv("/r")

    def test_the_argv_runs_the_vetted_absolute_path_not_a_name(self):
        """The spawn must run the file that was checked. A bare ``rg`` would be
        re-resolved through $PATH at exec time -- after the check."""
        argv = f._grep_rg_argv("/r", "/opt/tools/bin/rg")
        assert argv[0] == "/opt/tools/bin/rg"

    def test_every_glob_in_the_argv_is_negated(self):
        """One non-negated glob flips ripgrep's whole glob set into ALLOWLIST mode,
        which would silently exclude every file the set does not name."""
        argv = f._grep_rg_argv("/r")
        globs = [argv[i + 1] for i, tok in enumerate(argv) if tok in ("--glob", "--iglob")]
        assert globs, "the argv should carry exclusions"
        assert all(g.startswith("!") for g in globs), [g for g in globs if not g.startswith("!")]

    def test_the_root_is_the_only_operand_after_the_terminator(self):
        """The query is not on the command line, so the terminator guards the root
        alone. A query starting with `-` cannot be read as a flag when it is not
        there to be read."""
        assert f._grep_rg_argv("/root")[-2:] == ["--", "/root"]

    def test_an_error_exit_asks_for_the_fallback_rather_than_reporting_no_matches(
        self, monkeypatch
    ):
        """rg exits 0 with matches, 1 with none and >1 on an error. Only the last
        is 'no verdict' — and under ``--no-messages`` an error's stdout is empty,
        which read as an authoritative empty result set."""
        _rg_spawn(monkeypatch, lines=(), returncode=2)
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) is None

        # Exit 1 is a real answer: the tree genuinely holds no match.
        _rg_spawn(monkeypatch, lines=(), returncode=1)
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) == ([], False)

    def test_an_error_exit_after_hits_keeps_them_as_a_partial_answer(self, monkeypatch):
        """ripgrep exits 2 for an unreadable directory even under ``--no-messages``,
        AFTER printing every match it did reach. Discarding those for the python
        engine hands it the same spent deadline and answers "fewer" or "none" for
        a search that found things; the hits are real and come back marked short."""
        record = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": "/r/a.py"},
                    "line_number": 4,
                    "lines": {"text": "needle here"},
                },
            }
        )
        _rg_spawn(monkeypatch, lines=(record + "\n",), returncode=2)
        answer = f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        assert answer is not None, "hits were discarded for the fallback"
        hits, truncated = answer
        assert truncated is True
        assert [(h["file"], h["line"]) for h in hits] == [("/r/a.py", 4)]

    def test_a_timeout_reports_its_partial_hits_instead_of_an_empty_fallback(self, monkeypatch):
        """The deadline is SHARED, so handing a timeout to the python pass gives it
        nothing left and it returns an empty list at its first check. The matches
        ripgrep already printed are real; they come back marked truncated."""
        record = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": "/r/a.py"},
                    "line_number": 4,
                    "lines": {"text": "needle here"},
                },
            }
        )

        # The records are read as they arrive, so a timeout on `wait()` finds them
        # already parsed. No bytes-vs-str decode of TimeoutExpired.stdout exists
        # to get wrong any more -- the partial answer falls out of the loop.
        _rg_spawn(monkeypatch, lines=(record + "\n",), wait_raises=True)
        hits, truncated = f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        assert truncated is True
        assert [(h["file"], h["line"]) for h in hits] == [("/r/a.py", 4)]

    def test_a_fail_closed_sandbox_refusal_takes_the_python_engine_not_a_500(self, monkeypatch):
        """`SandboxUnavailableError` is a RuntimeError, raised when the host has no
        sandbox backend. Catching it is what keeps an ordinary Linux box without
        bubblewrap searching through the python engine instead of answering every
        keystroke with an HTTP 500."""
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: "/usr/bin/rg")
        monkeypatch.setattr(f, "cgroup_scope_argv", lambda cmd: cmd)

        def refuses(cmd):
            raise RuntimeError("no sandbox backend and unsandboxed exec is not allowed")

        monkeypatch.setattr(f, "wrap_argv", refuses)
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) is None

    def test_the_launcher_temp_file_is_unlinked_on_every_path(self, monkeypatch, tmp_path):
        """`wrap_argv` returns (argv, cleanup_path) and the caller owns the temp.
        Discarding it leaked one launcher file per search."""
        unlinked: list[str] = []
        monkeypatch.setattr(f.os, "unlink", lambda p: unlinked.append(p))
        monkeypatch.setattr(f, "cgroup_scope_argv", lambda cmd: cmd)
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: "/usr/bin/rg")
        monkeypatch.setattr(f, "wrap_argv", lambda cmd: (cmd, "/tmp/launcher-xyz"))

        # Success path.
        _rg_spawn(monkeypatch, lines=(), returncode=0, patch_wrap=False)
        f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        assert unlinked == ["/tmp/launcher-xyz"]

        # Fallback path: still unlinked.
        unlinked.clear()
        _rg_spawn(monkeypatch, lines=(), returncode=2, patch_wrap=False)
        f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        assert unlinked == ["/tmp/launcher-xyz"]

    def test_the_reader_stops_at_the_cap_instead_of_draining_the_tree(self, monkeypatch):
        """`--max-count 1` bounds records per FILE and `--max-filesize` bounds one
        record, but neither bounds the record COUNT -- that is the number of matching
        files. Buffering all of them to keep the first 200 is the growth path; the
        reader stops at the cap and kills the child instead."""

        def record_for(i):
            return (
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": f"/r/f{i}.py"},
                            "line_number": 1,
                            "lines": {"text": "needle"},
                        },
                    }
                )
                + "\n"
            )

        served: list[int] = []

        def endless():
            i = 0
            while True:
                served.append(i)
                yield record_for(i)
                i += 1

        killed = _rg_spawn(monkeypatch, lines=endless())
        hits, truncated = f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        assert truncated is True
        assert len(hits) == f._GREP_MAX_RESULTS
        # Bounded, not unbounded: the reader thread may run ahead of the parse loop
        # by at most the hand-off queue, and BLOCKS once it is full -- so the tree
        # is never drained to find the 200 hits that are kept.
        assert len(served) <= f._GREP_MAX_RESULTS + f._GREP_RG_QUEUE_LINES + 2
        assert killed, "the child was left running after the cap"

    def test_a_capped_search_reaps_the_child_and_lets_the_reader_exit(self, monkeypatch):
        """One search runs per keystroke, so anything a capped search leaves behind
        accumulates. Killing without reaping leaves a zombie; and the reader blocks on
        a full queue once this side stops consuming -- including on the sentinel put in
        its own `finally` -- so it wedges instead of exiting."""
        import threading as _threading

        record = (
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": "/r/a.py"},
                        "line_number": 1,
                        "lines": {"text": "needle"},
                    },
                }
            )
            + "\n"
        )

        def endless():
            while True:
                yield record

        before = {t.name for t in _threading.enumerate()}
        made: list = []
        killed = _rg_spawn(monkeypatch, lines=endless(), made=made)
        hits, truncated = f._grep_rg("/r", "needle", f.time.monotonic() + 5)

        assert truncated is True
        assert len(hits) == f._GREP_MAX_RESULTS
        assert killed, "the child was not stopped"
        assert made, "no child was spawned"
        # Reaped, not merely signalled: an unwaited kill is a zombie per search.
        assert made[0]._reaped is True
        # And the pipe was closed, which is what ends the reader's iteration.
        assert made[0].stdout.closed is True

        deadline = f.time.monotonic() + 3
        while f.time.monotonic() < deadline:
            if "file-grep-rg" not in {t.name for t in _threading.enumerate()} - before:
                break
            f.time.sleep(0.05)
        leaked = {t.name for t in _threading.enumerate()} - before
        assert "file-grep-rg" not in leaked, f"reader thread survived: {leaked}"

    def test_a_query_that_prints_nothing_still_answers_inside_the_budget(self, monkeypatch):
        """rg emits no record for a non-matching file, so a rare-or-absent query over
        a large tree prints NOTHING while it traverses. Reading the pipe inline could
        only notice the deadline when a line arrived, so this case ran to rg's own
        completion and then reported `truncated: False` -- a budget overrun presented
        as a complete answer, with a transfer-pool worker held for the whole walk."""
        import threading as _threading

        release = _threading.Event()

        class _Silent:
            """A pipe that never yields a line until the test lets it go."""

            def __iter__(self):
                return self

            def __next__(self):
                release.wait(30)
                raise StopIteration

        killed = _rg_spawn(monkeypatch, lines=_Silent())
        started = f.time.monotonic()
        hits, truncated = f._grep_rg("/r", "needle", started + 0.3)
        elapsed = f.time.monotonic() - started
        release.set()
        assert hits == []
        # Short, and SAYS it is short -- the two facts the budget contract rests on.
        assert truncated is True
        assert elapsed < 5, f"the read was not deadline-bounded ({elapsed:.1f}s)"
        assert killed, "the child was left traversing after the deadline"

    def test_a_failed_ripgrep_asks_for_the_fallback_rather_than_claiming_no_matches(
        self, monkeypatch
    ):
        """None, not an empty list: a missing binary and a failed spawn are both
        'no verdict', and reporting them as an empty result set would say 'no
        matches' about a search that never ran. A TIMEOUT is not in that class —
        it has partial hits, and the test above pins them."""

        def boom(argv, **kwargs):
            raise OSError("no rg")

        monkeypatch.setattr(f, "_grep_rg_executable", lambda: "/usr/bin/rg")
        monkeypatch.setattr(f, "wrap_argv", lambda cmd: (cmd, None))
        monkeypatch.setattr(f, "cgroup_scope_argv", lambda cmd: cmd)
        monkeypatch.setattr(f, "popen_limited", boom)
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) is None

        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) is None


class TestAudit:
    @pytest.mark.asyncio
    async def test_an_allowed_search_is_audited_with_its_engine_and_counts(self, tree, mock_sel):
        await _grep(tree, "needle")
        kwargs = mock_sel.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "file_grep"
        assert kwargs["outcome"] == "allowed"
        assert "engine=" in kwargs["resources"]
        assert "results=" in kwargs["resources"]
