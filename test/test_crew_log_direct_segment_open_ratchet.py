"""Ratchet: who may open a crew-log segment file directly. The list may only shrink.

The read budget (``crew_log/read_budget.py``) counts what ``CrewLog.iter_from``,
``get``, ``page`` and the raw prefix walks decode. A function that opens a segment
file itself is invisible to it, however much of the log it reads. Every such function
on today's tree is named below with the reason it reads directly, so a new one has to
either go through a counted entry point or be argued into this list in review.

Two checks:

* STATIC. Every module under ``src/kiro_crew`` that can name a segment -- the
  ``crew_log`` package, any module importing a segment-path helper from it, or one
  holding both ``"crew-log"`` and ``"log.jsonl"`` -- is parsed, and every raw file
  open in it (``open``, ``os.open``, ``io.open``, ``Path.open``, ``read_bytes``,
  ``read_text``, ``mmap``) is found by enclosing function. Each must be listed, either
  in :data:`SEGMENT_OPENERS` or in :data:`NOT_A_SEGMENT`. A listed function that no
  longer opens anything fails too, so the list only ever loses entries.
* RUNTIME. A path built by hand, without any helper name, slips past the static
  scope. So the real store is driven through its counted entry points and the tree
  readers with ``open`` / ``os.open`` patched, and every open of a segment file must
  come from a function in :data:`SEGMENT_OPENERS`. It covers only the paths it drives.
"""

from __future__ import annotations

import ast
import builtins
import functools
import io
import os
import re
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store_paths, store_retention
from kiro_crew.crew_log.session_tree import SessionTree

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
SLOT = "chat-directopen"

#: Functions that open a SEGMENT file themselves, per module, with why. Remove an entry
#: when its function moves to a counted entry point; never add one without review.
SEGMENT_OPENERS: "dict[str, dict[str, str]]" = {
    "crew_log/store_segment_reads.py": {
        "read_head": "header and first entry only; bounded",
        "_scan_tail": "the last 72 KiB, for the writer's seq recovery before a handle exists",
        "_iter_entries_located": "the framer under iter_from, get and page; callers pinned below",
        "_read_header_line": "one header line; bounded",
        "_read_first_entry_seq": "first entry, to check a segment's name inside iter_from",
    },
    "crew_log/store_segment_writes.py": {
        "_write_then_sync": "the append itself; a write, not a read",
        "_undo_residue": "rolls back a failed append's bytes; bounded",
        "_rollback_append": "rolls back a failed append; bounded",
        "_truncate_now": "truncates, reads nothing",
    },
    "crew_log/store_handle.py": {
        "raw_prefix_digest": "counted itself (read_budget 'digest')",
        "raw_records_through": "counted itself (read_budget 'records_through')",
    },
    "crew_log/prefix_extent.py": {
        "extent_at": "one record at a stored offset; O(1)",
        "extent_holds": "one record at a stored offset; O(1)",
    },
    "crew_log/store_tail_repair.py": {
        "_orphan_chunk_offset": "UNCOUNTED whole newest segment on open(repair=True); "
        "should resume from a savepoint through iter_from",
        "_open_tail": "UNCOUNTED whole newest segment on open(repair=True); "
        "should resume from a savepoint through iter_from",
    },
    "crew_log/store_retention.py": {
        "find_spawned_entries": "UNCOUNTED whole unit; the session-tree watermark PR "
        "moves it to iter_from past the watermark",
        "_scan_whole_for_types": "UNCOUNTED whole unit; the session-tree watermark PR "
        "moves it to iter_from past the watermark",
    },
    "crew_log/holders.py": {
        "_read_from": "resumes from a cached offset; no product caller builds its scanner",
    },
    "crew_log/read.py": {
        "_unit_id_in": "first line of log.jsonl only; bounded",
    },
    "dashboard/subagent_replay_scope.py": {
        "_tail": "64 bytes before a cached offset; bounded",
        "_contains_needle": "byte search, no decode; delta from a cached offset, "
        "whole unit on a first scan",
    },
}

#: Callers of the uncounted framer (``_iter_entries_located`` and its ``_iter_entries``
#: wrapper) outside the counted entry points. Same rule: shrink only.
FRAMER_CALLERS: "dict[str, dict[str, str]]" = {
    "crew_log/store_handle.py": {
        "_iter_segments_located": "the walk under iter_from, get and page; counted there",
        "_resume_at": "one record at a resume point, inside iter_from; counted there",
    },
    "crew_log/store_segment_reads.py": {
        "_iter_entries": "the plain wrapper; its own callers are pinned here",
        "_anchor_exists": "UNCOUNTED: an append's thread anchor older than the tail "
        "window walks to it; rare",
        "_full_scan_last_seq": "UNCOUNTED: a tail window with no parseable line walks "
        "the segment; rare",
    },
}

#: Raw opens in scanned modules whose file is NOT a segment, with what it is. Listed so
#: that a new open there is classified on purpose, not waved through by the scope.
NOT_A_SEGMENT: "dict[str, dict[str, str]]" = {
    "crew_log/lease.py": {"_take": "the unit's lease file"},
    "crew_log/pending_open.py": {"_drain": "pending-open marker files"},
    "crew_log/session_tree_projection_checkpoint.py": {
        "_load_checkpoint": "the session-tree checkpoint JSON"
    },
    "crew_log/store_cuts.py": {
        "_read_cuts_raw": "the .cuts counter",
        "_cuts_dir_forced": "a directory fd for fsync",
        "_retire_cuts_inode": "the .cuts counter",
        "_close_cut": "the .cuts counter",
    },
    "crew_log/store_paths.py": {"_open_lock": "the unit's .lock file"},
    "crew_log/store_removal.py": {"_write_hold": "the trash hold file"},
    "eventlog/service.py": {"_remove_legacy_activity": "a directory fd (O_DIRECTORY)"},
    "session_ledger.py": {"purge_matching": "a session-ledger key file"},
}

#: Names that let a module reach a segment path through the store's helpers.
_SEGMENT_HELPERS = frozenset(
    {
        "segment_paths",
        "segment_first_seqs",
        "crew_log_path",
        "crew_log_dir",
        "crew_log_root",
        "LOG_FILE",
        "_SEGMENT_GLOB",
    }
)
_FRAMERS = frozenset({"_iter_entries_located", "_iter_entries"})
_RAW_ATTRS = frozenset({"open", "read_bytes", "read_text", "FileIO", "mmap"})


def _is_raw_open(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in ("open", "FileIO")
    if isinstance(func, ast.Attribute) and func.attr in _RAW_ATTRS:
        # ``CrewLog.open`` returns a handle; it opens no file itself.
        return not (func.attr == "open" and ast.unparse(func.value).endswith("CrewLog"))
    return False


def _calls_framer(call: ast.Call) -> bool:
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in _FRAMERS


def _in_scope(rel: str, source: str, tree: ast.AST) -> bool:
    if rel.startswith("crew_log/"):
        return True
    if "crew-log" in source and "log.jsonl" in source:
        return True
    return any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("kiro_crew.crew_log")
        and any(alias.name in _SEGMENT_HELPERS for alias in node.names)
        for node in ast.walk(tree)
    )


def _sites(rel: str, source: str, wanted: Any) -> "set[str]":
    """Enclosing function names of every call in *source* that *wanted* accepts."""
    found: "set[str]" = set()

    def visit(node: ast.AST, enclosing: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            enclosing = node.name
        if isinstance(node, ast.Call) and wanted(node):
            found.add(enclosing)
        for child in ast.iter_child_nodes(node):
            visit(child, enclosing)

    visit(ast.parse(source), "<module>")
    return found


@functools.cache
def _scan() -> "tuple[dict[str, set[str]], dict[str, set[str]]]":
    opens: "dict[str, set[str]]" = {}
    framers: "dict[str, set[str]]" = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if "/_vendor/" in f"/{rel}" or "/tests/" in f"/{rel}":
            continue
        source = path.read_text(encoding="utf-8")
        # A cheap text test first, so only a module that could be in scope is parsed.
        if not (
            rel.startswith("crew_log/")
            or "kiro_crew.crew_log" in source
            or ("crew-log" in source and "log.jsonl" in source)
        ):
            continue
        tree = ast.parse(source)
        if not _in_scope(rel, source, tree):
            continue
        if sites := _sites(rel, source, _is_raw_open):
            opens[rel] = sites
        if any(name in source for name in _FRAMERS):
            if sites := _sites(rel, source, _calls_framer):
                framers[rel] = sites
    return opens, framers


def _pinned(*tables: "dict[str, dict[str, str]]") -> "dict[str, set[str]]":
    merged: "dict[str, set[str]]" = {}
    for table in tables:
        for rel, functions in table.items():
            merged.setdefault(rel, set()).update(functions)
    return merged


def _diff(found: "dict[str, set[str]]", pinned: "dict[str, set[str]]") -> "tuple[dict, dict]":
    new = {rel: sorted(f - pinned.get(rel, set())) for rel, f in found.items()}
    stale = {rel: sorted(f - found.get(rel, set())) for rel, f in pinned.items()}
    return {r: f for r, f in new.items() if f}, {r: f for r, f in stale.items() if f}


def test_every_reason_is_one_line():
    for table in (SEGMENT_OPENERS, FRAMER_CALLERS, NOT_A_SEGMENT):
        for functions in table.values():
            for reason in functions.values():
                assert reason.strip() and "\n" not in reason


def test_raw_opens_near_crew_log_segments_only_go_down():
    found, _framers = _scan()
    new, stale = _diff(found, _pinned(SEGMENT_OPENERS, NOT_A_SEGMENT))
    assert not new, (
        f"new direct file opens where a crew-log segment is in reach: {new}. Read the "
        "segment through CrewLog.iter_from / get / page (counted by read_budget). If the "
        "file is not a segment, list it in NOT_A_SEGMENT with what it is."
    )
    assert not stale, f"these no longer open a file; remove them from the allowlist: {stale}"


def test_callers_of_the_uncounted_framer_only_go_down():
    _opens, found = _scan()
    # The framer's own module defines it; its definition is not a call.
    new, stale = _diff(found, _pinned(FRAMER_CALLERS))
    assert not new, (
        f"new callers of the uncounted framer: {new}. Walk through CrewLog.iter_from, "
        "which the read budget counts."
    )
    assert not stale, f"these no longer call the framer; remove them: {stale}"


def test_the_scan_sees_an_open_it_was_not_told_about():
    """The scanner itself: a direct open in a scoped module is found by function."""
    source = (
        "from kiro_crew.crew_log.store_paths import segment_paths\n"
        "def sneaky(kind, unit):\n"
        "    with open(segment_paths(kind, unit)[0], 'rb') as f:\n"
        "        return f.read()\n"
        "def handle(kind, unit):\n"
        "    return CrewLog.open(kind, unit)\n"
    )
    tree = ast.parse(source)
    assert _in_scope("elsewhere.py", source, tree)
    assert _sites("elsewhere.py", source, _is_raw_open) == {"sneaky"}


# --------------------------------------------------------------------------- #
# Runtime: the counted entry points and the tree readers open segments only
# through listed functions.
# --------------------------------------------------------------------------- #

_SEGMENT_NAME = re.compile(r"^log(\.\d+)?\.jsonl$")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()


class _SegmentOpens:
    """Who opened a segment file, as ``(module, function)`` of the first frame in SRC."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.callers: "set[tuple[str, str]]" = set()
        self._root = store_paths.crew_log_root(lg.KIND_SESSION).parent.resolve()
        real_open = builtins.open
        real_os_open = os.open
        # An ``opener=`` callback runs ``os.open`` inside ``open``: that is the same open,
        # already attributed to whoever called ``open``.
        inside = threading.local()

        def spied_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            self._note(file)
            inside.depth = getattr(inside, "depth", 0) + 1
            try:
                return real_open(file, *args, **kwargs)
            finally:
                inside.depth -= 1

        def spied_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
            if not getattr(inside, "depth", 0):
                self._note(path)
            return real_os_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", spied_open)
        # ``pathlib`` opens through ``io.open``, a separate binding of the same function.
        monkeypatch.setattr(io, "open", spied_open)
        monkeypatch.setattr(os, "open", spied_os_open)

    def _note(self, file: Any) -> None:
        if isinstance(file, int) or not isinstance(file, (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(file))
        if not _SEGMENT_NAME.match(path.name):
            return
        try:
            path.resolve().relative_to(self._root)
        except ValueError:
            return
        frame: "FrameType | None" = sys._getframe(2)
        src = str(SRC)
        while frame is not None and not frame.f_code.co_filename.startswith(src):
            frame = frame.f_back
        if frame is None:
            self.callers.add(("<outside src>", "?"))
            return
        rel = Path(frame.f_code.co_filename).relative_to(SRC).as_posix()
        self.callers.add((rel, frame.f_code.co_name))


def _unit(unit_id: str, records: int) -> CrewLog:
    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="o", agent="a", slot=SLOT)
    handle.append(
        "session/opened",
        {"agent": "a", "slot": SLOT, "model": "m", "cwd": "/w", "owner": "o", "resumed": False},
        src="gateway",
    )
    handle.append_many(
        [
            {"type": "ledger/recorded", "data": {"slot": SLOT, "goal": f"g{i}"}}
            for i in range(records)
        ],
        src="gateway",
    )
    return handle


def test_the_store_and_tree_readers_open_segments_only_through_listed_functions(monkeypatch):
    handle = _unit("acp-directopen", 40)
    directory = store_paths.crew_log_dir(lg.KIND_SESSION, handle.id)
    opens = _SegmentOpens(monkeypatch)

    handle.append("ledger/recorded", {"slot": SLOT, "goal": "late"}, src="gateway")
    assert len(list(handle.iter_from(1))) > 40
    assert handle.get(5) is not None
    assert handle.page(None, 10).entries
    handle.raw_prefix_digest(10)
    handle.raw_records_through(10)
    handle.release_ownership()
    CrewLog.open(lg.KIND_SESSION, handle.id, repair=True).release_ownership()
    crew_log.read_projection(handle.id, "status")
    crew_log.read_slot_projection(SLOT, "ledger")
    SessionTree().reading((handle.id,), with_edges=True)
    store_retention.find_last_tree_edge(directory)
    store_retention.find_spawned_entries(directory, 8)

    assert opens.callers, "the spy saw no segment open; the check is not measuring anything"
    allowed = {(rel, fn) for rel, fns in SEGMENT_OPENERS.items() for fn in fns}
    unlisted = sorted(opens.callers - allowed)
    assert not unlisted, (
        f"segment files opened by functions not in SEGMENT_OPENERS: {unlisted}. Read through "
        "CrewLog.iter_from / get / page, or argue the function into the list."
    )
