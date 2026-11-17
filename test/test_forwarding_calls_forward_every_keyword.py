"""A hand-spelled forwarding call passes every keyword its caller and callee share.

Two pull requests, each green on its own base, can meet in such a call: one
adds a re-call with its keywords spelled out by hand, the other adds a keyword
to the callee, and the merged re-call drops it. Git merges the two cleanly
because they edit different lines, and no test of either change sees the other.
:mod:`forwarding_call_scan` finds the shape over the whole tree, so the
change that lands second fails here whichever of the two it is.

A row in :data:`_ALLOWED_DROPS` is a drop that is deliberate, one row per
``(file, caller, callee, parameter)`` with its reason, so two changes that each add
one edit different lines. A row that matches nothing fails too: the
table only ever says what the tree does. :data:`_OPEN_BUGS` holds real drops whose
fix is in flight; those rows are not checked for staleness, so the fix and this
gate can land in either order.

``test/`` is not scanned: it holds no hit and would add ~25 s.
"""

from __future__ import annotations

from pathlib import Path

import forwarding_call_scan as scan_mod
import source_corpus

#: Deliberate drops: (file, caller, callee, parameter) -> why the call leaves it out.
_ALLOWED_DROPS: dict[tuple[str, str, str, str], str] = {
    (
        "src/kiro_crew/apps/builtins/issue_radar/backend/store_repos.py",
        "write_repo_settings",
        "provider_root",
        "owner",
    ): "provider_root accepts owner/repo for positional callers and deletes them unread",
    (
        "src/kiro_crew/apps/builtins/issue_radar/backend/store_repos.py",
        "write_repo_settings",
        "provider_root",
        "repo",
    ): "provider_root accepts owner/repo for positional callers and deletes them unread",
    (
        "src/kiro_crew/apps/builtins/issue_radar/backend/store_repos.py",
        "remove_connected_repo",
        "provider_root",
        "owner",
    ): "provider_root accepts owner/repo for positional callers and deletes them unread",
    (
        "src/kiro_crew/apps/builtins/issue_radar/backend/store_repos.py",
        "remove_connected_repo",
        "provider_root",
        "repo",
    ): "provider_root accepts owner/repo for positional callers and deletes them unread",
    (
        "src/kiro_crew/context_assembly/session_start.py",
        "build_session_context",
        "_build_v2_essentials",
        "native_documents",
    ): "the essentials read native_documents only with native_envelope_out, not asked here",
    (
        "src/kiro_crew/platform/governance_parts/evaluator.py",
        "gate_decision",
        "gate_decision",
        "shell_command",
    ): "the as-before re-judge passes the command as a Running: title instead",
}

#: Real drops whose fix is open: key -> the issue. Not stale-checked (see module doc).
_OPEN_BUGS: dict[tuple[str, str, str, str], str] = {}

#: Everything except the test tree (see the module docstring).
_SKIPPED_TOP = ("test/", "semgrep-tests/")


def _tree_files() -> list[tuple[str, Path]]:
    root = source_corpus.repo_root()
    files = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if not rel.startswith(_SKIPPED_TOP) and "/_vendor/" not in rel:
            files.append((rel, path))
    return files


def _tree_sources(files):
    # The scan streams the tree once per pass, so the file list is enumerated once
    # (git is ~2 s) and only the reads repeat.
    def stream():
        for rel, path in files:
            try:
                yield rel, path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # source_corpus's own test fails on an unreadable file

    return stream


def test_no_forwarding_call_drops_a_shared_keyword():
    drops = scan_mod.scan(_tree_sources(_tree_files()))
    found = {drop.key for drop in drops}

    new = [d for d in drops if d.key not in _ALLOWED_DROPS and d.key not in _OPEN_BUGS]
    assert not new, (
        "A call forwards its caller's own parameters by hand but leaves out one the "
        "callee also accepts. A keyword added to the callee later is dropped on this "
        "path unless someone edits the call too. Pass it, or forward with **kwargs, "
        "or add a row to _ALLOWED_DROPS with why the call must not pass it:\n"
        + "\n".join(f"  {d.rel}:{d.line} {d.caller} -> {d.callee}: {d.param}" for d in new)
    )

    stale = sorted(set(_ALLOWED_DROPS) - found)
    assert (
        not stale
    ), "These _ALLOWED_DROPS rows match no call any more; delete them:\n" + "\n".join(
        f"  {row}" for row in stale
    )


def _scan_text(text: str) -> list[tuple[str, str, str]]:
    drops = scan_mod.scan(lambda: [("m.py", text)])
    return [(d.caller, d.callee, d.param) for d in drops]


def test_a_re_call_that_misses_a_new_keyword_is_found():
    # A re-entry that spells every keyword but ``kas_memory_mode``.
    text = """
async def load_session(self, session_file, resume_sid, *, cwd=None, agent=None,
                       channel_id="", kas_memory_mode=None, focus_resume=False, inner=None):
    if inner is None:
        return await load_session(self, session_file, resume_sid, cwd=cwd, agent=agent,
                                  channel_id=channel_id, focus_resume=focus_resume, inner=1)
"""
    assert _scan_text(text) == [("load_session", "load_session", "kas_memory_mode")]


def test_a_kwargs_hand_on_is_seen_through():
    # The callee takes **kwargs and hands them back to the caller.
    text = """
class R:
    async def create(self, cwd=None, agent=None, channel_id="", member_context=None, state=None):
        if state is None:
            return await self._with_state(cwd=cwd, agent=agent, channel_id=channel_id)

    async def _with_state(self, **kwargs):
        return await self.create(**kwargs, state=1)
"""
    assert _scan_text(text) == [("R.create", "_with_state", "member_context")]


def test_a_call_passing_every_shared_keyword_is_clean():
    text = """
def a(x, y, z, w=0):
    return b(x=x, y=y, z=z, w=w)

def b(x, y, z, w=0, extra=1):
    return x
"""
    assert _scan_text(text) == []


def test_a_call_that_forwards_too_few_keywords_is_not_forwarding():
    text = """
def a(x, y, z):
    return b(x=x, y=y)

def b(x, y, z):
    return x
"""
    assert _scan_text(text) == []


def test_a_def_the_indentation_cut_cannot_hold_is_read_from_the_whole_file():
    # A column-0 line inside a string ends the cut early; the scan must still see the
    # call after it, and the class the method belongs to.
    text = '''
class R:
    def a(self, x, y, z, w):
        note = """
column zero
"""
        return self.b(x=x, y=y, z=z)

    def b(self, x, y, z, w):
        return x
'''
    assert _scan_text(text) == [("R.a", "b", "w")]


def test_a_name_with_disagreeing_definitions_is_skipped():
    # Two unrelated ``b`` in other modules: the scan does not guess which one.
    sources = [
        ("m.py", "def a(x, y, z, w):\n    return thing.b(x=x, y=y, z=z)\n"),
        ("n.py", "class N:\n    def b(self, x, y, z, w): ...\n"),
        ("o.py", "class O:\n    def b(self, x, y, z, w, v): ...\n"),
    ]
    assert scan_mod.scan(lambda: sources) == []


def test_a_paren_in_a_callee_default_does_not_hide_it():
    # ``sep=")"`` ends the paren-depth header early; the callee must still be found.
    text = """
def a(x, y, z, w):
    return b(x=x, y=y, z=z)

def b(sep=")", x=0, y=0, z=0, w=0):
    return x
"""
    assert _scan_text(text) == [("a", "b", "w")]


def test_a_generic_def_takes_part():
    # PEP 695: ``def f[T](...)`` is a caller and a callee like any other def.
    text = """
def a[T](x: T, y, z, w):
    return b(x=x, y=y, z=z)

def b[U](x: U, y, z, w):
    return x
"""
    assert _scan_text(text) == [("a", "b", "w")]
