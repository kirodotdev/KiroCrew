"""Off-loop filesystem discipline for the auto_research backend.

``test_auto_research_onloop_db.py`` pins this module's *database* discipline
with a static ratchet: no ``async def`` in ``handlers.py`` may reach
``_get_db`` on the event loop. This module is the same guard for its
filesystem calls.

The blocking condition is not hypothetical. ``FINDINGS.md`` is the campaign
report an LLM research loop produces, so its size is bounded by the model's
output rather than by anything this code controls, and ``_handle_action``'s
fork path reads one and writes it straight back out.
``RESEARCH_DIR`` lives under the Kiro Crew home, which is
operator-configurable and may be a synced or network volume.
``_poll_workflow_campaign`` is a polling adapter, so its reads and writes recur
for the life of a campaign. AUTOSDE ``no-blocking-call-on-event-loop`` — which
this module already cites by name — lists "large synchronous file IO" among the
calls that must not run on the gateway loop.

Two guards, mirroring the DB module:

1. a static AST ratchet, so a new inline ``read_text`` / ``write_text`` /
   ``mkdir`` / ``unlink`` in an ``async def`` fails here rather than in
   production;
2. behavioural proof on the thread the IO ACTUALLY ran on, for the read path a
   user hits most (the grill tree) and for the write path that leaves a file
   behind (the knowledge-library export).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import threading
from pathlib import Path
from typing import Any, NoReturn
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.auto_research import handlers as h

BASE = "/api/apps/auto-research"

#: ``pathlib.Path`` methods that hit the filesystem. Deliberately a name-based
#: set, like ``_DB_TOUCHING_FNS`` next door: it over-matches a same-named method
#: on some other object, which fails safe (a spurious offload), and it is the
#: only shape a static check can see without type inference.
_FS_METHODS = frozenset(
    {
        "exists",
        "is_file",
        "is_dir",
        "is_symlink",
        "stat",
        "lstat",
        "samefile",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "mkdir",
        "rmdir",
        "unlink",
        "touch",
        "chmod",
        "iterdir",
        "glob",
        "rglob",
    }
)

#: Module-level functions in ``os``/``shutil`` that do the same.
_FS_FUNCS = frozenset(
    {"listdir", "scandir", "walk", "makedirs", "copyfile", "copytree", "rmtree", "which"}
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    with (
        mock.patch.object(h, "DB_PATH", tmp_path / "t.db"),
        mock.patch.object(h, "RESEARCH_DIR", tmp_path / "r"),
    ):
        yield tmp_path


@pytest.fixture(autouse=True)
def _no_autonudge(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(h, "_autonudge_instance", lambda: None)


def _app(**keys: Any) -> web.Application:
    app = web.Application()
    for k, v in keys.items():
        app[k] = v
    return app


def _mk(path: str, *, app: web.Application, match: dict) -> web.Request:
    req = make_mocked_request("GET", f"{BASE}/{path}", app=app, match_info=match)
    req["user"] = "test-user"
    return req


def _body(resp: web.Response) -> Any:
    return json.loads(resp.text or "")


def _spy(monkeypatch: pytest.MonkeyPatch, method: str, target: Path, sink: list[str]) -> None:
    """Record the thread name of a real ``Path`` call against *target*."""
    original = getattr(Path, method)

    def _wrapper(self: Path, *args: object, **kwargs: object) -> object:
        if self == target:
            sink.append(threading.current_thread().name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, _wrapper)


# --- 1. static ratchet -------------------------------------------------------


def _module_tree() -> ast.Module:
    src = Path(inspect.getsourcefile(h)).read_text(encoding="utf-8")
    return ast.parse(src)


class TestStaticRatchet:
    def test_no_async_def_touches_the_filesystem_directly(self):
        """A nested sync helper is fine — it runs in the worker it is handed to.
        What is flagged is a filesystem call in the ``async def``'s OWN body,
        which is the shape that runs on the gateway loop.
        """
        violations: list[str] = []

        def scan(node: ast.AsyncFunctionDef) -> None:
            stack = list(ast.iter_child_nodes(node))
            while stack:
                n = stack.pop()
                if isinstance(n, (ast.FunctionDef, ast.Lambda, ast.AsyncFunctionDef)):
                    continue  # body runs off-loop when offloaded
                if isinstance(n, ast.Call):
                    fn = n.func
                    if isinstance(fn, ast.Attribute) and fn.attr in _FS_METHODS:
                        violations.append(f"{node.name}:{n.lineno} calls .{fn.attr}()")
                    elif isinstance(fn, ast.Attribute) and fn.attr in _FS_FUNCS:
                        violations.append(f"{node.name}:{n.lineno} calls {fn.attr}()")
                stack.extend(ast.iter_child_nodes(n))

        for node in ast.walk(_module_tree()):
            if isinstance(node, ast.AsyncFunctionDef):
                scan(node)

        assert not violations, (
            "filesystem call(s) on the event loop (offload via asyncio.to_thread "
            "/ run_in_executor, or move them into a sync helper you hand to "
            "one):\n" + "\n".join(violations)
        )

    def test_the_ratchet_can_actually_fail(self):
        """Guard against a scan that passes because it sees nothing: the same
        walk must flag a known-bad snippet."""
        bad = ast.parse(
            "async def h(p):\n"
            "    if p.exists():\n"
            "        return p.read_text()\n"
            "    return None\n"
        )
        found = [
            n.func.attr
            for node in ast.walk(bad)
            for n in ast.walk(node)
            if isinstance(node, ast.AsyncFunctionDef)
            and isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in _FS_METHODS
        ]
        assert set(found) == {"exists", "read_text"}


# --- 2. behavioural proof ----------------------------------------------------


class TestGrillTreeReadsOffLoop:
    @pytest.mark.asyncio
    async def test_tree_read_runs_off_the_event_loop(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cid = "aaaa0001"
        d = h.RESEARCH_DIR / cid
        d.mkdir(parents=True)
        tree_path = d / "grill_tree.json"
        tree_path.write_text(json.dumps([{"text": "why?"}]))
        reads: list[str] = []
        _spy(monkeypatch, "read_text", tree_path, reads)

        resp = await h._handle_grill_tree(_mk("x/grill-tree", app=_app(), match={"id": cid}))

        assert resp.status == 200
        assert _body(resp)["tree"], "the tree must still be served, not just offloaded"
        assert reads and all(t != "MainThread" for t in reads)

    @pytest.mark.asyncio
    async def test_absent_tree_is_still_an_empty_list(self, _isolate: Path):
        """Control: folding the exists() check into the worker read must not
        change what a campaign with no tree returns."""
        cid = "aaaa0002"
        (h.RESEARCH_DIR / cid).mkdir(parents=True)

        resp = await h._handle_grill_tree(_mk("x/grill-tree", app=_app(), match={"id": cid}))

        assert _body(resp) == {"tree": []}

    @pytest.mark.asyncio
    async def test_malformed_tree_is_still_an_empty_list(self, _isolate: Path):
        """Control: a corrupt file was already treated as no data."""
        cid = "aaaa0003"
        d = h.RESEARCH_DIR / cid
        d.mkdir(parents=True)
        (d / "grill_tree.json").write_text("{not json")

        resp = await h._handle_grill_tree(_mk("x/grill-tree", app=_app(), match={"id": cid}))

        assert _body(resp) == {"tree": []}


class TestKnowledgeExportIoOffLoop:
    def _state(self, existing: dict | None = None) -> MagicMock:
        store = MagicMock()
        store.get_source_by_uri.return_value = existing
        store.add_source.return_value = {"id": 1}
        return MagicMock(knowledge_store=store)

    @pytest.mark.asyncio
    async def test_findings_read_and_sanitized_write_run_off_the_event_loop(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cid = "aaaa0004"
        d = h.RESEARCH_DIR / cid
        d.mkdir(parents=True)
        findings = d / "FINDINGS.md"
        findings.write_text("# findings\n")
        sanitized = d / "findings_for_knowledge.md"
        reads: list[str] = []
        writes: list[str] = []
        _spy(monkeypatch, "read_text", findings, reads)
        _spy(monkeypatch, "write_text", sanitized, writes)
        app = _app(state=self._state(), knowledge_pipeline=MagicMock(ingest=AsyncMock()))

        await h._handle_to_knowledge(_mk("x/to-knowledge", app=app, match={"id": cid}))

        assert sanitized.exists(), "the sanitized copy must still be written"
        assert reads and all(t != "MainThread" for t in reads)
        assert writes and all(t != "MainThread" for t in writes)

    @pytest.mark.asyncio
    async def test_absent_findings_is_still_a_404(self, _isolate: Path):
        """Control: the existence probe runs off-loop ahead of the guards, so a
        campaign with no findings must still 404 rather than raising."""
        cid = "aaaa0005"
        (h.RESEARCH_DIR / cid).mkdir(parents=True)

        resp = await h._handle_to_knowledge(_mk("x/to-knowledge", app=_app(), match={"id": cid}))

        assert resp.status == 404


class TestWriteHelperDeletionRace:
    def test_write_text_does_not_resurrect_a_deleted_campaign_dir(self, tmp_path: Path):
        """A concurrent campaign deletion must keep winning.

        ``_handle_to_knowledge`` and ``_poll_workflow_campaign`` both write into
        an existing campaign directory. If the off-loop write helper created
        missing parents, a DELETE racing the write would have its directory
        silently recreated and the deleted campaign's data resurrected (and
        possibly ingested). The helper must surface the missing directory
        instead, exactly as the pre-offload inline ``write_text`` did.
        """
        gone = tmp_path / "deleted-campaign" / "findings_for_knowledge.md"

        with pytest.raises(FileNotFoundError):
            h._write_text(gone, "stale findings")

        assert not gone.parent.exists(), "the write must not recreate the deleted directory"


class TestGuardOrderBeforeRead:
    """The full findings read must come AFTER the prerequisite guards.

    An eager read would let an unreadable findings file (here: a directory
    squatting on the FINDINGS.md path, so ``read_text`` raises
    ``IsADirectoryError`` while ``exists()`` is still true) surface as a 500
    where the guards' 404/503 must win — the pre-offload order on main.
    """

    @pytest.mark.asyncio
    async def test_to_knowledge_unavailable_service_still_503s_with_unreadable_findings(
        self, _isolate: Path
    ):
        cid = "aaaa0006"
        d = h.RESEARCH_DIR / cid
        (d / "FINDINGS.md").mkdir(parents=True)  # exists, but unreadable as a file

        resp = await h._handle_to_knowledge(_mk("x/to-knowledge", app=_app(), match={"id": cid}))

        assert resp.status == 503, "the service guard must answer before the full read"

    @pytest.mark.asyncio
    async def test_to_artifact_orphaned_row_still_404s_with_unreadable_findings(
        self, _isolate: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(h, "_HAS_ARTIFACTS", True)
        cid = "aaaa0007"
        d = h.RESEARCH_DIR / cid
        (d / "FINDINGS.md").mkdir(parents=True)  # exists, but unreadable as a file
        # No campaigns row for cid: the DB guard must answer 404 before the read.

        resp = await h._handle_to_artifact(_mk("x/to-artifact", app=_app(), match={"id": cid}))

        assert resp.status == 404, "the orphaned-row guard must answer before the full read"


class TestCancellationSettlesWorker:
    @pytest.mark.asyncio
    async def test_cancellation_waits_for_the_worker_to_settle(self):
        """A cancellation delivered while the off-loop write worker runs must
        not propagate (releasing the campaign transition lock) until the
        worker thread has finished — otherwise a lock-serialized DELETE can
        interleave with the still-running writes and have its directory
        resurrected by the worker's ``mkdir``.
        """
        release = threading.Event()
        finished: list[bool] = []

        def worker() -> bool:
            release.wait(timeout=10)
            finished.append(True)
            return True

        task = asyncio.create_task(
            h._settle_before_cancellation(asyncio.create_task(asyncio.to_thread(worker)))
        )
        await asyncio.sleep(0.05)  # let the worker thread start
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "cancellation must not settle before the worker finishes"

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished, "the worker ran to completion before cancellation propagated"

    @pytest.mark.asyncio
    async def test_on_settled_runs_after_settlement_only_when_cancelled(self):
        """``on_settled`` is the seam the watchdog's failure logging rides on:
        it must observe a SETTLED task (so ``task.result()`` cannot raise
        ``InvalidStateError``), fire exactly once on the cancellation path,
        and stay silent on the normal path. A REPEAT cancellation while the
        helper is re-shielding must neither cancel the inner task nor abort
        the settling loop (the ``continue`` branch).
        """
        release = threading.Event()
        observed: list[bool] = []

        def worker() -> NoReturn:
            release.wait(timeout=10)
            raise OSError("worker failed")

        inner = asyncio.create_task(asyncio.to_thread(worker))
        task = asyncio.create_task(
            h._settle_before_cancellation(inner, on_settled=lambda t: observed.append(t.done()))
        )
        await asyncio.sleep(0.05)  # let the worker thread start
        task.cancel()
        await asyncio.sleep(0.05)  # first cancellation lands; helper re-shields
        task.cancel()
        await asyncio.sleep(0.05)  # repeat cancellation hits the continue branch
        assert not task.done(), "a repeat cancellation must not abort the settling loop"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not inner.cancelled(), "no cancellation may reach the settling task"
        assert observed == [True], "on_settled fires once, after the task settled"

        # Normal path: the callback must not run when nothing was cancelled.
        observed.clear()
        result = await h._settle_before_cancellation(
            asyncio.create_task(asyncio.to_thread(lambda: 42)),
            on_settled=lambda t: observed.append(t.done()),
        )
        assert result == 42
        assert observed == [], "on_settled must not fire on the uncancelled path"


class TestCampaignTextIsUtf8NotTheHostCodePage:
    """Research prose is written and read as UTF-8, whatever the host code page.

    The same reasoning this module opens with: ``FINDINGS.md`` is the report an
    LLM research loop produces, so its content is bounded by the model's output
    and not by anything this code controls. A model routinely emits characters
    the host's legacy ANSI code page cannot represent -- an emoji is the common
    one. ``Path.write_text`` with no ``encoding`` encodes with
    ``locale.getpreferredencoding()``, which is UTF-8 on POSIX but the ANSI code
    page on Windows, so on a CJK Windows host that write raises
    ``UnicodeEncodeError`` and the cycle's finding is lost rather than saved.

    Measured on a `cp950` host before the fix, calling these very helpers:

        _write_text             UnicodeEncodeError: 'cp950' codec can't encode
                                character '\\U0001f600'
        _write_new_cycle_files  UnicodeEncodeError: ... same

    The bug is host-conditioned, so a UTF-8 CI runner cannot make it fail. What
    IS checkable everywhere is the property that fixes it: the bytes on disk are
    UTF-8, chosen by this code, rather than whatever the host would have picked.
    That is what these assert, so the guard holds on the runners too.

    The JSON paths in this module are deliberately NOT covered here:
    ``json.dumps`` defaults to ``ensure_ascii=True``, so those files are pure
    ASCII and decode identically under every code page. Pinning them would be
    noise.
    """

    #: A finding shaped like real model output: an emoji (outside every legacy
    #: ANSI code page), an em dash and CJK (inside some, outside others).
    FINDING = "Cycle 1 \U0001f600 adoption — up 12% 已完成"

    def _campaign(self, tmp_path: Path, name: str = "aaaa0100") -> Path:
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_write_text_encodes_utf8(self, tmp_path: Path) -> None:
        target = self._campaign(tmp_path) / "FINDINGS.md"
        h._write_text(target, self.FINDING)
        assert target.read_bytes() == self.FINDING.encode("utf-8")

    def test_new_cycle_files_encode_utf8(self, tmp_path: Path) -> None:
        target = self._campaign(tmp_path) / "cycle-1.md"
        assert h._write_new_cycle_files([(target, self.FINDING)]) is True
        assert target.read_bytes() == self.FINDING.encode("utf-8")

    def test_findings_survive_a_fork_round_trip(self, tmp_path: Path) -> None:
        """The fork path reads a parent's findings and writes them straight back.

        Read and write must agree; a decode-then-encode through two different
        code pages is how a forked campaign silently inherits mojibake.
        """
        src = self._campaign(tmp_path, "aaaa0101") / "FINDINGS.md"
        src.write_bytes(self.FINDING.encode("utf-8"))
        dst = self._campaign(tmp_path, "aaaa0102") / "FINDINGS.md"

        h._copy_parent_findings(src, dst)

        assert dst.read_bytes() == self.FINDING.encode("utf-8")
        assert h._read_text_or_missing(dst) == self.FINDING

    def test_the_guard_can_actually_fail(self) -> None:
        """Guard the guard: the fixture really does defeat a legacy code page.

        Without this, every assertion above would pass just as happily on a
        payload that was ASCII all along, and would be pinning nothing.
        """
        with pytest.raises(UnicodeEncodeError):
            self.FINDING.encode("cp950")
        assert self.FINDING.encode("utf-8").decode("utf-8") == self.FINDING

    def test_a_legacy_code_page_campaign_reads_back_recoverable(self, tmp_path: Path) -> None:
        """A campaign written BEFORE the encoding was pinned must not 500.

        Its `FINDINGS.md` is sitting on disk in the host code page, so a strict
        UTF-8 read would raise and make every report/fork/export route fail for
        good. Nothing on disk records which code page that was, so the read does
        not pick one: it decodes ``latin-1``, which never raises and maps every
        byte to the code point of the same value. The report therefore renders,
        the file is untouched, and the original bytes are recoverable from the
        string -- byte-exact up to the universal-newline translation text mode
        applies, which is stated rather than glossed, and is why the fixture
        carries a ``\r\n``.
        """
        raw = "Old finding - 已完成\r\nsecond line".encode("cp950")
        target = self._campaign(tmp_path) / "FINDINGS.md"
        target.write_bytes(raw)
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")  # guard the guard: the non-UTF-8 branch is reached
        assert b"\r\n" in raw  # ... and the newline fold is genuinely exercised

        out = h._read_text_or_missing(target)

        assert out is not None
        assert out.encode("latin-1") == raw.replace(b"\r\n", b"\n")
        assert target.read_bytes() == raw, "reading a report must not rewrite it"

    def test_bytes_valid_under_both_decodings_resolve_to_the_declared_contract(
        self, tmp_path: Path
    ) -> None:
        """The ambiguous case: bytes that decode successfully under BOTH candidates.

        cp950 ``癒`` is the two bytes ``c2 a1``, and those same bytes are valid
        UTF-8 for ``¡``; 899 cp950 multi-byte characters have that property. A
        read that chose its codec by trying the host code page could not tell
        the two apart, because a decode succeeding is not evidence of what wrote
        the file -- and no marker, version or writer generation is stored
        anywhere in a campaign directory to break the tie.

        So the tie is not broken by inspection. UTF-8 is the declared contract
        for campaign prose, these bytes are read as UTF-8, and that answer is
        the same on every host whatever its code page happens to be.
        """
        raw = "癒".encode("cp950")
        assert raw == b"\xc2\xa1"
        assert raw.decode("utf-8") == "\u00a1"  # guard the guard: genuinely ambiguous
        target = self._campaign(tmp_path) / "FINDINGS.md"
        target.write_bytes(raw)

        assert h._read_text_or_missing(target) == "\u00a1"
        assert target.read_bytes() == raw, "reading a report must not rewrite it"

    def test_the_read_never_consults_the_host_code_page(self) -> None:
        """Ratchet: a code-page probe must not come back into this read path.

        Picking the host codec assumes the READING host wrote the file. It need
        not have: a campaign directory can be copied or restored between
        machines, and a host's code page can change under a campaign that never
        moved. That assumption makes one directory decode differently in two
        places, which is a worse contract than a render that is uniformly
        latin-1 and uniformly reversible.
        """
        assert not hasattr(h, "locale"), "handlers imports a host-code-page probe again"
        source = inspect.getsource(h._read_campaign_text)
        assert "getpreferredencoding" not in source.split('"""')[-1]

    def test_an_undecodable_file_degrades_without_destroying_it(self, tmp_path: Path) -> None:
        """Bytes no candidate decoding claims must not become an HTTP 500.

        They must also survive: ``errors="replace"`` throws away exactly the
        bytes a later correct migration would need, so a decode that dropped
        them would make the damage permanent on the one file that still had the
        original.
        """
        raw = b"\xff\xfe\x00 broken \xf0\x28\x8c\x28"
        target = self._campaign(tmp_path) / "FINDINGS.md"
        target.write_bytes(raw)

        out = h._read_text_or_missing(target)

        assert out is not None and "broken" in out
        assert out.encode("latin-1") == raw
        assert target.read_bytes() == raw, "a lossy decode must not be persisted"

    def test_a_crlf_findings_file_still_comes_back_with_newlines(self, tmp_path: Path) -> None:
        """Text mode's universal-newline translation is load-bearing here.

        Two tests in this tree compare a findings file's contents after a fork,
        so a read that returned ``\r\n`` would shift them. Both the UTF-8 read
        and the legacy fallback stay in text mode, which is what keeps this
        free.
        """
        target = self._campaign(tmp_path) / "FINDINGS.md"
        target.write_bytes(b"line one\r\nline two\r\n")

        assert h._read_text_or_missing(target) == "line one\nline two\n"
