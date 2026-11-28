"""The bundled evaluator binds a verdict to what it checked.

Three acceptance kinds could pass on something other than the work they accept:
``pr_checks`` judged whatever head the PR had and never said which, ``pr_merged``
passed on ``MERGED`` with no link to the head that passed the checks, and ``file``
passed on any path that existed, an empty stub or an old file included. Pinned here:

* ``pr_checks`` reads the head on both sides of ``gh pr checks``; a pass names it,
  and a head that moved while the checks were read is ``pending``.
* ``pr_merged`` names the final head and merge commit; with ``head`` set, a PR that
  merged at another head fails.
* ``file`` names size and modification time; the optional ``min_bytes`` and
  ``newer_than`` pins hold the file to them, read through ``lstat`` only, so a
  symbolic link swapped in after the path was screened lends no other file's
  size or time.
* Every bar written before these fields evaluates as it did, and the ledger's
  concreteness mirror agrees with the evaluator on each new field.

No network and no real ``gh``: ``subprocess.run`` is replaced. File times are set
with ``os.utime``, never waited for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import work_ledger as wl
from kiro_crew import work_ledger_records as records
from kiro_crew.skills import _BUILTIN_SKILLS_DIR
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

SCRIPT = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "scripts" / "accept_eval.py"

HEAD = "a" * 40
OTHER = "b" * 40
MERGE = "c" * 40
REPO = "owner/name"
HEAD_PROBE = ["gh", "pr", "view", "123", "--repo", REPO, "--json", "headRefOid"]
HEAD_PROBE += ["-q", ".headRefOid"]

#: 2026-01-01T00:00:00Z, the modification time every file test sets.
MTIME = 1767225600


class _Proc:
    """The three ``CompletedProcess`` fields the evaluator reads."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _load():
    return load_skill_script("_accept_eval_binding_under_test", SCRIPT)


def _wire_checks(mod, monkeypatch, heads, checks=(0, "all checks pass", "")):
    """Answer the head probes from ``heads`` in order and ``gh pr checks`` with ``checks``."""
    seen: list[list[str]] = []
    probes = iter(heads)

    def _fake_run(argv, **kwargs):
        seen.append(list(argv))
        if "headRefOid" in argv:
            return _Proc(*next(probes))
        return _Proc(*checks)

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    return seen


def _wire_view(mod, monkeypatch, view):
    """Answer ``gh pr view`` for ``pr_merged`` with ``view`` as JSON."""
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **kwargs: _Proc(0, json.dumps(view)))


# --------------------------------------------------------------------------- #
# pr_checks names the head it judged
# --------------------------------------------------------------------------- #


def test_a_pr_checks_pass_names_the_head_it_judged(monkeypatch):
    mod = _load()
    seen = _wire_checks(mod, monkeypatch, [(0, HEAD + "\n", ""), (0, HEAD, "")])
    verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": 123, "repo": REPO}})
    assert verdict == "pass"
    assert evidence.startswith(f"head {HEAD}: ")
    assert seen == [HEAD_PROBE, ["gh", "pr", "checks", "123", "--repo", REPO], HEAD_PROBE]


def test_a_head_that_moved_while_the_checks_ran_is_pending(monkeypatch):
    mod = _load()
    _wire_checks(mod, monkeypatch, [(0, HEAD, ""), (0, OTHER, "")])
    verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": 123}})
    assert verdict == "pending"
    assert f"moved from {HEAD} to {OTHER}" in evidence


@pytest.mark.parametrize(
    "heads",
    [
        [(1, "", "no pull requests found"), (0, HEAD, "")],
        [(0, HEAD, ""), (0, "null", "")],
        [(0, "--flag", ""), (0, "--flag", "")],
    ],
)
def test_a_head_probe_that_cannot_answer_leaves_the_pass_standing(monkeypatch, heads):
    """One-sided, like the draft probe: an unreadable head never changes the verdict."""
    mod = _load()
    _wire_checks(mod, monkeypatch, heads)
    verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": 123}})
    assert verdict == "pass"
    assert evidence.startswith("head unknown")


def test_a_failing_check_costs_no_second_head_probe(monkeypatch):
    mod = _load()
    seen = _wire_checks(mod, monkeypatch, [(0, HEAD, "")], checks=(1, "2 failing", ""))
    verdict, _ = mod._evaluate({"accept": {"kind": "pr_checks", "pr": 123}})
    assert verdict == "fail"
    assert sum("headRefOid" in argv for argv in seen) == 1


# --------------------------------------------------------------------------- #
# pr_merged names what merged, and a pinned head must be it
# --------------------------------------------------------------------------- #


def test_a_merged_pr_names_its_head_and_merge_commit(monkeypatch):
    mod = _load()
    _wire_view(
        mod, monkeypatch, {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE}}
    )
    verdict, evidence = mod._evaluate({"accept": {"kind": "pr_merged", "pr": 7, "repo": REPO}})
    assert verdict == "pass"
    assert f"head {HEAD}" in evidence and f"merge commit {MERGE}" in evidence


@pytest.mark.parametrize("pinned", [HEAD, HEAD[:7]])
def test_a_pr_merged_at_the_checked_head_passes(monkeypatch, pinned):
    mod = _load()
    _wire_view(
        mod, monkeypatch, {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE}}
    )
    spec = {"kind": "pr_merged", "pr": 7, "repo": REPO, "head": pinned}
    assert mod._evaluate({"accept": spec})[0] == "pass"


def test_a_pr_merged_at_another_head_fails(monkeypatch):
    """The push after the green check merged code no check judged."""
    mod = _load()
    _wire_view(
        mod, monkeypatch, {"state": "MERGED", "headRefOid": OTHER, "mergeCommit": {"oid": MERGE}}
    )
    spec = {"kind": "pr_merged", "pr": 7, "repo": REPO, "head": HEAD}
    verdict, evidence = mod._evaluate({"accept": spec})
    assert verdict == "fail"
    assert f"merged at head {OTHER}, not the checked head {HEAD}" in evidence


def test_an_open_pr_that_moved_off_the_pinned_head_says_so(monkeypatch):
    mod = _load()
    _wire_view(mod, monkeypatch, {"state": "OPEN", "headRefOid": OTHER, "mergeCommit": None})
    spec = {"kind": "pr_merged", "pr": 7, "repo": REPO, "head": HEAD}
    verdict, evidence = mod._evaluate({"accept": spec})
    assert verdict == "pending"
    assert f"its head is now {OTHER}" in evidence


def test_a_pinned_head_with_no_head_reported_is_an_error(monkeypatch):
    mod = _load()
    _wire_view(mod, monkeypatch, {"state": "MERGED", "headRefOid": "", "mergeCommit": None})
    spec = {"kind": "pr_merged", "pr": 7, "repo": REPO, "head": HEAD}
    assert mod._evaluate({"accept": spec})[0] == "error"


@pytest.mark.parametrize("bad", ["TBD", "", "ABCDEF1", "abc", "--flag", 7, True, ["a" * 40]])
def test_a_malformed_pinned_head_is_an_error_before_any_subprocess(monkeypatch, bad):
    mod = _load()
    seen: list = []
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **kw: seen.append(argv))
    spec = {"kind": "pr_merged", "pr": 7, "repo": REPO, "head": bad}
    verdict, evidence = mod._evaluate({"accept": spec})
    assert verdict == "error", bad
    assert "hex commit id" in evidence
    assert seen == []
    assert wl.is_acceptance_concrete(spec) is False


# --------------------------------------------------------------------------- #
# file names what it saw, and its pins hold the file to them
# --------------------------------------------------------------------------- #


def _made(tmp_path: Path, body: bytes = b"x" * 100) -> Path:
    path = tmp_path / "out.md"
    path.write_bytes(body)
    os.utime(path, (MTIME, MTIME))
    return path


def test_a_present_file_names_its_size_and_time(tmp_path):
    path = _made(tmp_path)
    verdict, evidence = _load()._evaluate({"accept": {"kind": "file", "path": str(path)}})
    assert verdict == "pass"
    assert "(100 bytes, modified 2026-01-01T00:00:00+00:00)" in evidence


def test_a_directory_still_passes_a_bare_presence_check(tmp_path):
    verdict, evidence = _load()._evaluate({"accept": {"kind": "file", "path": str(tmp_path)}})
    assert verdict == "pass"
    assert "not a regular file" in evidence


@pytest.mark.parametrize(
    ("pins", "verdict"),
    [
        ({"min_bytes": 100}, "pass"),
        ({"min_bytes": 101}, "fail"),
        ({"min_bytes": 0}, "pass"),
        ({"newer_than": "2025-12-31T23:59:59Z"}, "pass"),
        ({"newer_than": "2026-01-01T00:00:00Z"}, "fail"),
        ({"newer_than": "2026-01-01T08:59:59+09:00"}, "pass"),
        ({"min_bytes": 1, "newer_than": "2025-01-01T00:00:00+00:00"}, "pass"),
        ({"min_bytes": None, "newer_than": None}, "pass"),
    ],
)
def test_each_pin_holds_the_file_to_it(tmp_path, pins, verdict):
    path = _made(tmp_path)
    got, evidence = _load()._evaluate({"accept": {"kind": "file", "path": str(path), **pins}})
    assert got == verdict, (pins, evidence)


def test_a_stub_under_min_bytes_fails_and_says_so(tmp_path):
    """The reported case: every output file a 68-byte stub, every item done."""
    path = _made(tmp_path, b"x" * 68)
    verdict, evidence = _load()._evaluate(
        {"accept": {"kind": "file", "path": str(path), "min_bytes": 1024}}
    )
    assert verdict == "fail"
    assert "68 bytes" in evidence and "under min_bytes 1024" in evidence


@pytest.mark.parametrize("pins", [{"min_bytes": 1}])
def test_a_content_pin_on_a_directory_fails(tmp_path, pins):
    verdict, evidence = _load()._evaluate(
        {"accept": {"kind": "file", "path": str(tmp_path), **pins}}
    )
    assert verdict == "fail"
    assert "needs a regular file" in evidence


def test_a_link_swapped_in_after_screening_lends_no_size_or_time(tmp_path):
    """The tool resolves a path before the evaluator runs, so a link found here was
    put in place afterwards: its target, whatever it is, is never stat-ed."""
    target = _made(tmp_path, b"x" * 4096)
    link = tmp_path / "out-link.md"
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symbolic link here: {exc}")
    mod = _load()
    verdict, evidence = mod._evaluate({"accept": {"kind": "file", "path": str(link)}})
    assert verdict == "pass"
    assert evidence.endswith("(a symbolic link)")
    assert "4096" not in evidence
    for pins in ({"min_bytes": 1}, {"newer_than": "2025-01-01T00:00:00Z"}):
        verdict, evidence = mod._evaluate({"accept": {"kind": "file", "path": str(link), **pins}})
        assert verdict == "fail", pins
        assert "not a link" in evidence and "4096" not in evidence


def test_the_file_kind_reads_no_content(tmp_path, monkeypatch):
    """Metadata only: nothing in the ``file`` path opens the file."""
    mod = _load()
    path = _made(tmp_path)
    refuse = lambda *a, **k: pytest.fail("file content was read")  # noqa: E731
    monkeypatch.setattr("builtins.open", refuse)
    monkeypatch.setattr("io.open", refuse)  # ``Path.open`` / ``read_bytes`` route here
    spec = {"kind": "file", "path": str(path), "min_bytes": 1, "newer_than": "2025-01-01T00:00:00Z"}
    assert mod._evaluate({"accept": spec})[0] == "pass"


def test_a_pin_on_a_missing_file_fails_as_before(tmp_path):
    spec = {"kind": "file", "path": str(tmp_path / "no"), "min_bytes": 1}
    assert _load()._evaluate({"accept": spec})[0] == "fail"


@pytest.mark.parametrize(
    "pins",
    [
        {"min_bytes": -1},
        {"min_bytes": True},
        {"min_bytes": 1.5},
        {"min_bytes": "TBD"},
        {"newer_than": "2026-01-01T00:00:00"},
        {"newer_than": "yesterday"},
        {"newer_than": "TBD"},
        {"newer_than": 1767225600},
        {"min_bytes": 1, "exists": False},
        {"newer_than": "2026-01-01T00:00:00Z", "exists": False},
    ],
)
def test_a_malformed_pin_is_an_error_and_not_concrete(tmp_path, pins):
    path = _made(tmp_path)
    spec = {"kind": "file", "path": str(path), **pins}
    assert _load()._evaluate({"accept": spec})[0] == "error", pins
    assert wl.is_acceptance_concrete(spec) is False, pins


def test_well_formed_pins_are_concrete():
    assert wl.is_acceptance_concrete(
        {"kind": "file", "path": "/p", "min_bytes": 1, "newer_than": "2026-01-01T00:00:00Z"}
    )
    assert wl.is_acceptance_concrete(
        {"kind": "file", "path": "/p", "exists": False, "min_bytes": None}
    )
    assert wl.is_acceptance_concrete({"kind": "pr_merged", "pr": 7, "repo": REPO, "head": HEAD})
    assert wl.is_acceptance_concrete({"kind": "pr_merged", "pr": 7, "repo": REPO, "head": None})


def test_the_ledger_mirror_uses_the_evaluators_own_patterns():
    mod = _load()
    assert records.ACCEPTANCE_GIT_BASE.pattern == mod._GIT_BASE.pattern


def test_the_pins_need_no_new_command():
    """``file`` stays in-process and the head probes run ``gh``, which ``pr_checks``
    already needed."""
    assert _load()._SELF_BUILT_COMMANDS == {"gh", "kirocrew", "git"}


def test_pinned_file_bars_end_to_end(tmp_path):
    """Through the real script: a stub fails its floor beside a bare bar that passes."""
    path = _made(tmp_path, b"stub")
    items = [
        {"id": "floor", "accept": {"kind": "file", "path": str(path), "min_bytes": 1024}},
        {"id": "bare", "accept": {"kind": "file", "path": str(path)}},
    ]
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"items": items}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    out = {row["id"]: row["verdict"] for row in json.loads(proc.stdout)["results"]}
    assert out == {"floor": "fail", "bare": "pass"}
