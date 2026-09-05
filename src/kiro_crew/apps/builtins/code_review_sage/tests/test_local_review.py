"""Tests for the local review diff and finding contract."""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

import pytest
from sage_lib import local_review

from kiro_crew import platform_compat

_APP_ROOT = Path(__file__).resolve().parent.parent
_ROUTES = _APP_ROOT / "backend" / "routes.py"
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))


def _retry_readonly_removal(func, path, _exc_info):
    """Let ``rmtree`` remove git's read-only object files on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path):
    """rmtree that tolerates read-only files (Windows loose git objects)."""
    shutil.rmtree(path, onerror=_retry_readonly_removal)


def _load_routes_module():
    """Fresh backend-routes instance (same harness the routes tests use)."""
    spec = importlib.util.spec_from_file_location("sage_routes_local_review", str(_ROUTES))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    # These setup calls read the host system gitconfig while the module under
    # test runs with GIT_CONFIG_NOSYSTEM=1; a host autocrlf would otherwise
    # clean the committed blob's EOLs and desync them from the raw bytes the
    # working-tree diff compares against. Pin conversion off for determinism.
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "example.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "example.py")
    _git(repo, "commit", "-qm", "initial")
    return repo


@pytest.fixture(autouse=True)
def _stub_sandboxed_git(monkeypatch):
    """Keep these parser tests independent of the runner's OS sandbox backend."""
    monkeypatch.setattr(
        local_review,
        "sandboxed_spawn_argv",
        lambda argv, *, env=None, **_: (argv, env or {}, None),
    )


def test_working_tree_diff_anchors_added_lines(tmp_path):
    repo = _repo(tmp_path)
    (repo / "example.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")

    diff = local_review.working_tree_diff(repo)

    assert diff.base_revision
    assert diff.revision.startswith(diff.base_revision + " + working-tree:")
    assert [item.path for item in diff.files] == ["example.py"]
    assert diff.files[0].changed_lines() == {2}
    assert diff.files[0].hunks[0].lines[-1].content == "value += 1"


def test_untracked_file_uses_repository_relative_path(tmp_path):
    repo = _repo(tmp_path)
    (repo / "new file.py").write_text("value = 2\n", encoding="utf-8")

    diff = local_review.working_tree_diff(repo)

    assert [item.path for item in diff.files] == ["new file.py"]
    assert diff.files[0].status == "added"
    assert diff.files[0].changed_lines() == {1}


def test_working_tree_diff_is_byte_capped_not_unbuffered(tmp_path):
    # A diff larger than MAX_DIFF_BYTES must yield the partial-review warning
    # from the CAPTURE-time truncation flag — the parent never buffers more
    # than the cap, so a runaway diff cannot OOM the gateway before the limit
    # applies (GPT review, PR #5274, residual/crash-data-loss-corruption).
    repo = _repo(tmp_path)
    big = "\n".join(f"value_{i} = {i}" for i in range(40000)) + "\n"
    (repo / "example.py").write_text(big, encoding="utf-8")

    diff = local_review.working_tree_diff(repo)

    assert diff.warning == "diff exceeded the review byte limit; review is partial"
    assert diff.files  # the capped head still parses into reviewable findings


def test_validate_finding_rejects_context_and_external_lines(tmp_path):
    repo = _repo(tmp_path)
    (repo / "example.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
    diff = local_review.working_tree_diff(repo)

    with pytest.raises(ValueError, match="changed line"):
        local_review.validate_finding(
            {
                "file": "example.py",
                "line": 1,
                "severity": "warning",
                "title": "bad",
                "message": "not changed",
            },
            diff,
            "session",
        )

    with pytest.raises(ValueError, match="outside"):
        local_review.validate_finding(
            {
                "file": "secret.txt",
                "line": 1,
                "severity": "warning",
                "title": "bad",
                "message": "outside",
            },
            diff,
            "session",
        )


def test_validate_finding_accepts_deleted_line_and_rejects_invalid_old_line(tmp_path):
    repo = _repo(tmp_path)
    (repo / "example.py").write_text("value = 2\n", encoding="utf-8")
    diff = local_review.working_tree_diff(repo)

    finding = local_review.validate_finding(
        {
            "file": "example.py",
            "side": "old",
            "line": 1,
            "severity": "warning",
            "title": "old value",
            "message": "the old value is unsafe",
        },
        diff,
        "session",
    )
    assert finding.side == "old"

    with pytest.raises(ValueError, match="changed line"):
        local_review.validate_finding(
            {
                "file": "example.py",
                "side": "old",
                "line": 999,
                "severity": "warning",
                "title": "bad",
                "message": "not deleted",
            },
            diff,
            "session",
        )


def test_reconcile_preserves_dismissal_and_marks_missing_findings_resolved(tmp_path):
    repo = _repo(tmp_path)
    (repo / "example.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
    diff = local_review.working_tree_diff(repo)
    old = local_review.validate_finding(
        {
            "file": "example.py",
            "line": 2,
            "severity": "warning",
            "category": "correctness",
            "title": "duplicate",
            "message": "same issue",
        },
        diff,
        "session",
    )
    old.status = "dismissed"
    old.user_instruction = "Keep the public API."
    current = local_review.validate_finding(
        {
            "file": "example.py",
            "line": 2,
            "severity": "warning",
            "category": "correctness",
            "title": "changed title",
            "message": "same issue",
        },
        diff,
        "session",
    )

    result = local_review.reconcile_findings([old], [current])
    assert result[0].status == "dismissed"
    assert result[0].user_instruction == "Keep the public API."

    missing = local_review.validate_finding(
        {
            "file": "example.py",
            "line": 2,
            "severity": "warning",
            "category": "correctness",
            "title": "gone",
            "message": "gone issue",
        },
        diff,
        "session",
    )
    result = local_review.reconcile_findings([missing], [])
    assert missing.status == "resolved"
    assert result == [missing]


def test_context_is_bounded(tmp_path):
    repo = _repo(tmp_path)
    (repo / "example.py").write_text("x = 'a' * 100000\n", encoding="utf-8")
    diff = local_review.working_tree_diff(repo)

    assert len(local_review.build_context(diff).encode("utf-8")) <= local_review.MAX_CONTEXT_BYTES


def test_context_bounds_guidance_read_at_disk(tmp_path):
    """An oversized AGENTS.md must be cut without reading the whole file.

    read_text() would load the entire file before the [:4000] cut; build_context
    runs on the gateway event loop, so the read itself has to be bounded.
    """
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("g" * 100_000, encoding="utf-8")
    diff = local_review.working_tree_diff(repo)

    context = local_review.build_context(diff)

    guidance = next(block for block in context.split("\n\n") if block.startswith("GUIDANCE"))
    assert guidance.startswith("GUIDANCE AGENTS.md\n")
    assert len(guidance.removeprefix("GUIDANCE AGENTS.md\n")) == 4000
    assert "gggggggggg" in guidance  # actually the fixture's content, cut cleanly


def test_validate_finding_redacts_category_and_reviewer(tmp_path):
    """category/reviewer are model-written: they cross the dashboard boundary
    like title/message and must be scrubbed BEFORE they can be stored."""
    repo = _repo(tmp_path)
    (repo / "example.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
    diff = local_review.working_tree_diff(repo)
    raw = {
        "file": "example.py",
        "line": 2,
        "severity": "warning",
        "category": "leak AKIAIOSFODNN7EXAMPLE in category",
        "title": "bad",
        "message": "the increment is unsafe",
        "reviewer": "leak AKIAIOSFODNN7EXAMPLE in reviewer",
    }

    finding = local_review.validate_finding(raw, diff, "session")

    assert "AKIAIOSFODNN7EXAMPLE" not in (finding.category or "")
    assert "AKIAIOSFODNN7EXAMPLE" not in (finding.reviewer or "")
    # The fingerprint is derived from the REDACTED category, so dedup keys can
    # never diverge from the stored fields.
    assert str(raw["category"]) not in (finding.category or "")
    assert local_review.store.redact_text(str(raw["category"])) == finding.category


def test_save_session_round_trips(tmp_path):
    session = {
        "id": "local-1",
        "repository": str(_repo(tmp_path)),
        "findings": [{"id": "f1", "status": "open", "title": "off-by-one"}],
    }

    local_review.save_session(session)

    assert local_review.load_session("local-1") == session


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits")
def test_save_session_writes_owner_only(tmp_path):
    """A review session holds the repo's diff, so it must stay private to its owner."""
    session = {"id": "local-1"}

    local_review.save_session(session)

    path = local_review.session_path("local-1")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ── re-review reconciliation scope guard (backend routes) ──────────────────


_RAW_FINDING = {
    "file": "example.py",
    "side": "new",
    "line": 2,
    "severity": "warning",
    "title": "unsafe increment",
    "message": "the increment is unsafe",
}


class _FakeLocalPool:
    """Reviewer turn: re-reports the SAME anchor each re-review."""

    async def begin_batch(self):
        return None

    async def send(self, prompt, timeout=0.0):
        import json as _json

        return _json.dumps({"version": 1, "findings": [dict(_RAW_FINDING)]})

    async def end_batch(self):
        return None


class TestReReviewReconcileScopeGuard(unittest.IsolatedAsyncioTestCase):
    """Re-review reconciles against previous_session_id, but ONLY when the
    previous session reviewed the SAME repository in the SAME mode.

    The UI submits the currently-displayed session id even after the user
    switched the repository or diff scope, and the old code merged by
    fingerprint with zero checks — carrying the old repo's findings in as
    "resolved" and donating dismissed/fixing statuses to unrelated findings.
    """

    def setUp(self):
        self.mod = _load_routes_module()
        self.mod._LOCAL_TASKS.clear()
        self.mod._ACTIVE_FIX_REPOS.clear()
        # A re-review scenario is by definition a dirty tree: the diff the
        # findings anchor to must exist, so give example.py a real edit.
        self.repo = _repo(Path(self.mkdtemp()))
        (self.repo / "example.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
        self.diff = local_review.working_tree_diff(self.repo)

    def mkdtemp(self) -> str:
        import tempfile

        tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, tmp)
        return tmp

    def tearDown(self):
        self.mod._LOCAL_TASKS.clear()
        self.mod._ACTIVE_FIX_REPOS.clear()

    def _session(self, session_id: str, findings: list[dict]) -> dict:
        session = {
            "id": session_id,
            "repository": str(self.repo),
            "mode": "all-working-tree",
            "status": "completed",
            "findings": findings,
        }
        local_review.save_session(session)
        return session

    def _previous_with_statuses(self) -> dict:
        """A completed previous session: one dismissed + one open finding."""
        matching = local_review.validate_finding(dict(_RAW_FINDING), self.diff, "prev-1")
        matching.status = "dismissed"
        matching.user_instruction = "Keep the public API."
        gone = local_review.validate_finding(
            {**_RAW_FINDING, "message": "a since-fixed issue"}, self.diff, "prev-1"
        )
        return self._session("prev-1", [matching.to_dict(), gone.to_dict()])

    async def _review(self, session: dict) -> dict:
        with unittest.mock.patch.object(self.mod.review_pool, "get_pool", lambda: _FakeLocalPool()):
            await self.mod._local_review_bg(session)
        return session

    async def test_cross_repo_previous_session_carries_nothing(self):
        previous = self._previous_with_statuses()
        previous["repository"] = str(self.repo.parent / "other-repo")
        local_review.save_session(previous)

        session = {
            "id": "curr-1",
            "repository": str(self.repo),
            "mode": "all-working-tree",
            "status": "reviewing",
            "findings": [],
            "previous_session_id": "prev-1",
        }

        result = await self._review(session)

        self.assertEqual(result["status"], "completed")
        # Nothing was inherited: no resolved/dismissed clone of either old
        # finding, and the fresh finding kept its default status.
        self.assertEqual(len(result["findings"]), 1)
        fresh = result["findings"][0]
        self.assertEqual(fresh["status"], "open")
        self.assertIsNone(fresh.get("user_instruction"))

    async def test_cross_mode_previous_session_carries_nothing(self):
        previous = self._previous_with_statuses()
        previous["mode"] = "unstaged"
        local_review.save_session(previous)

        session = {
            "id": "curr-1",
            "repository": str(self.repo),
            "mode": "all-working-tree",
            "status": "reviewing",
            "findings": [],
            "previous_session_id": "prev-1",
        }

        result = await self._review(session)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["status"], "open")

    async def test_same_repo_same_mode_still_carries_statuses(self):
        # The guard must not over-block: an identical repo+mode re-review
        # still inherits the dismissed status (and its instruction) onto the
        # colliding fresh finding, and still carries the missing one.
        self._previous_with_statuses()

        session = {
            "id": "curr-1",
            "repository": str(self.repo),
            "mode": "all-working-tree",
            "status": "reviewing",
            "findings": [],
            "previous_session_id": "prev-1",
        }

        result = await self._review(session)

        self.assertEqual(result["status"], "completed")
        by_status: dict = {}
        for item in result["findings"]:
            by_status.setdefault(item["status"], []).append(item)
        # The fresh finding inherited the old disposition...
        self.assertIn("dismissed", by_status)
        inherited = by_status["dismissed"][0]
        self.assertEqual(inherited["user_instruction"], "Keep the public API.")
        # ...and the finding that disappeared was carried as before (a
        # dismissed finding is NOT rewritten to resolved).
        self.assertEqual(len(result["findings"]), 2)
