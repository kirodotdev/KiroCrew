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
    # Host autocrlf can desync committed blobs from raw diff bytes.
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "example.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "example.py")
    _git(repo, "commit", "-qm", "initial")
    return repo


@pytest.fixture(autouse=True)
def _stub_sandboxed_git(_floor_monkeypatch):
    """Keep these parser tests independent of the runner's OS sandbox backend."""
    _floor_monkeypatch.setattr(
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
    # Capture-time truncation must flag an oversized diff without buffering it.
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


def test_context_skips_hardlinked_guidance_file(tmp_path):
    """A hardlink at AGENTS.md must not smuggle an outside file's bytes in.

    ``is_symlink()`` only refuses a symlink; a hardlink to a file outside the
    repo passes that check and ``path.open("rb")`` would read straight
    through it. ``safe_read_file_bytes_nolink`` refuses ``st_nlink > 1``.
    """
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside secret content", encoding="utf-8")
    repo = _repo(tmp_path)
    (repo / "example.py").write_text("value = 1\n", encoding="utf-8")
    # Keep AGENTS.md out of the ordinary diff to isolate the dedicated read.
    (repo / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
    os.link(outside, repo / "AGENTS.md")
    (repo / "CONTRIBUTING.md").write_text("normal guidance", encoding="utf-8")
    diff = local_review.working_tree_diff(repo)

    context = local_review.build_context(diff)

    assert "GUIDANCE AGENTS.md" not in context
    assert "outside secret content" not in context
    assert "GUIDANCE CONTRIBUTING.md\nnormal guidance" in context


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
    # Dedup keys must use the same redacted category as stored fields.
    assert str(raw["category"]) not in (finding.category or "")
    assert local_review.store.redact_text(str(raw["category"])) == finding.category


def test_validate_finding_truncates_unbounded_reviewer_text(tmp_path):
    """title/message/suggestion are otherwise-unbounded model output; cap them
    at MAX_FINDING_TEXT_CHARS so a runaway reviewer response cannot inflate a
    persisted session without limit."""
    repo = _repo(tmp_path)
    (repo / "example.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
    diff = local_review.working_tree_diff(repo)
    overlong = "x" * (local_review.MAX_FINDING_TEXT_CHARS + 500)
    raw = {
        "file": "example.py",
        "line": 2,
        "severity": "warning",
        "title": overlong,
        "message": overlong,
        "suggestion": overlong,
        "category": overlong,
        "reviewer": overlong,
    }

    finding = local_review.validate_finding(raw, diff, "session")

    assert len(finding.title) == local_review.MAX_FINDING_TEXT_CHARS
    assert len(finding.message) == local_review.MAX_FINDING_TEXT_CHARS
    assert len(finding.suggestion or "") == local_review.MAX_FINDING_TEXT_CHARS
    assert finding.category is not None
    assert finding.reviewer is not None
    assert len(finding.category) == local_review.MAX_FINDING_TEXT_CHARS
    assert len(finding.reviewer) == local_review.MAX_FINDING_TEXT_CHARS


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


def _fresh_local_reviews_root():
    """Return the local-reviews root, resetting whatever an earlier test left
    there (the pinned KIROCREW_HOME is shared across the whole suite)."""
    root = local_review.session_path("probe").parent
    if root.is_symlink():
        root.unlink()
    elif root.exists():
        _rmtree(root)
    root.mkdir(parents=True)
    return root


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="unprivileged symlinks")
def test_save_session_refuses_a_link_planted_at_the_root(tmp_path):
    """A symlinked (or junctioned) local-reviews root would redirect every
    session write to attacker-chosen storage."""
    root = _fresh_local_reviews_root()
    outside = root.parent / "local-reviews-outside"
    if outside.is_symlink() or outside.exists():
        _rmtree(outside) if outside.is_dir() and not outside.is_symlink() else outside.unlink()
    outside.mkdir()
    root.rmdir()
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="local-review"):
        local_review.save_session({"id": "local-link-root"})


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="unprivileged symlinks")
def test_save_session_refuses_a_link_planted_at_the_session_file(tmp_path):
    """The session file itself being a plant must not be dereferenced into a
    write outside the local-reviews directory."""
    root = _fresh_local_reviews_root()
    path = local_review.session_path("local-link-file")
    outside = root / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path.symlink_to(outside)

    with pytest.raises(ValueError, match="local-review"):
        local_review.save_session({"id": "local-link-file"})


def test_save_session_works_on_a_fresh_root(tmp_path):
    session = {"id": "local-fresh"}

    local_review.save_session(session)

    assert local_review.load_session("local-fresh") == session


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


def _mkdtemp(case: unittest.TestCase) -> str:
    """tempfile.mkdtemp() with cleanup auto-registered on the test case."""
    import tempfile

    tmp = tempfile.mkdtemp()
    case.addCleanup(_rmtree, tmp)
    return tmp


class _RoutesTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared harness: fresh routes module, cleared local-task registry."""

    def setUp(self):
        self.mod = _load_routes_module()
        self.mod._LOCAL_TASKS.clear()

    def tearDown(self):
        self.mod._LOCAL_TASKS.clear()

    def mkdtemp(self) -> str:
        return _mkdtemp(self)


class TestReReviewReconcileScopeGuard(_RoutesTestCase):
    """Re-review reconciles against previous_session_id, but ONLY when the
    previous session reviewed the SAME repository in the SAME mode.

    The UI submits the currently-displayed session id even after the user
    switched the repository or diff scope, and the old code merged by
    fingerprint with zero checks — carrying the old repo's findings in as
    "resolved" and donating dismissed/fixing statuses to unrelated findings.
    """

    def setUp(self):
        super().setUp()
        # A re-review scenario is by definition a dirty tree: the diff the
        # findings anchor to must exist, so give example.py a real edit.
        self.repo = _repo(Path(self.mkdtemp()))
        (self.repo / "example.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
        self.diff = local_review.working_tree_diff(self.repo)

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

    async def test_cross_repo_or_mode_previous_session_carries_nothing(self):
        # Neither a different repository nor a different diff mode on the
        # previous session may donate a status onto the fresh finding.
        mutations = {
            "repository": str(self.repo.parent / "other-repo"),
            "mode": "unstaged",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                previous = self._previous_with_statuses()
                previous[field] = value
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
                # Nothing was inherited: no resolved/dismissed clone of
                # either old finding, and the fresh finding kept its default
                # status.
                self.assertEqual(len(result["findings"]), 1)
                fresh = result["findings"][0]
                self.assertEqual(fresh["status"], "open")
                self.assertIsNone(fresh.get("user_instruction"))

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


class _ManyFindingsPool:
    """Reviewer turn: reports ``count`` distinct findings on the same anchor."""

    def __init__(self, path: str, count: int):
        self._path = path
        self._count = count

    async def begin_batch(self):
        return None

    async def send(self, prompt, timeout=0.0):
        import json as _json

        findings = [
            {
                "file": self._path,
                "side": "new",
                "line": 2,
                "severity": "warning",
                "title": f"finding {i}",
                "message": f"message body {i}",
            }
            for i in range(self._count)
        ]
        return _json.dumps({"version": 1, "findings": findings})

    async def end_batch(self):
        return None


class TestFindingsTruncationIsCounted(_RoutesTestCase):
    """``parsed["findings"][:MAX_FINDINGS]`` and the post-carry-forward
    ``valid[:MAX_FINDINGS]`` both silently dropped overflow findings; both
    drop counts must be surfaced on the session so the UI (or an operator)
    can tell a completed review dropped results instead of reading it as a
    small, complete one."""

    def setUp(self):
        super().setUp()
        self.repo = _repo(Path(self.mkdtemp()))
        (self.repo / "example.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")

    async def _review_with(self, session: dict, count: int) -> dict:
        pool = _ManyFindingsPool("example.py", count)
        with unittest.mock.patch.object(self.mod.review_pool, "get_pool", lambda: pool):
            await self.mod._local_review_bg(session)
        return session

    async def test_reviewer_turn_overflow_is_counted(self):
        # (reported count, findings kept, findings_truncated) -- covers both
        # the overflow and no-overflow paths through the same cap.
        cases = [
            (local_review.MAX_FINDINGS + 7, local_review.MAX_FINDINGS, 7),
            (3, 3, 0),
        ]
        for count, expected_len, expected_truncated in cases:
            with self.subTest(count=count):
                session = {
                    "id": "curr-1",
                    "repository": str(self.repo),
                    "mode": "all-working-tree",
                    "status": "reviewing",
                    "findings": [],
                }

                result = await self._review_with(session, count)

                self.assertEqual(len(result["findings"]), expected_len)
                self.assertEqual(result["findings_truncated"], expected_truncated)

    async def test_carry_forward_overflow_is_also_counted(self):
        """Carry-forward (resolved findings appended from the previous
        session) can push ``valid`` past MAX_FINDINGS even when the fresh
        review turn alone did not."""
        # A previous session with MAX_FINDINGS distinct resolved-eligible
        # findings, none of which the (single) fresh finding below matches by
        # fingerprint -- every one of them gets carried forward as "resolved".
        diff = local_review.working_tree_diff(self.repo)
        previous_findings = [
            local_review.validate_finding(
                {
                    "file": "example.py",
                    "side": "new",
                    "line": 2,
                    "severity": "warning",
                    "title": f"stale finding {i}",
                    "message": f"stale message body {i}",
                },
                diff,
                "prev-1",
            ).to_dict()
            for i in range(local_review.MAX_FINDINGS)
        ]
        local_review.save_session(
            {
                "id": "prev-1",
                "repository": str(self.repo),
                "mode": "all-working-tree",
                "status": "completed",
                "findings": previous_findings,
            }
        )

        session = {
            "id": "curr-1",
            "repository": str(self.repo),
            "mode": "all-working-tree",
            "status": "reviewing",
            "findings": [],
            "previous_session_id": "prev-1",
        }

        result = await self._review_with(session, 1)

        self.assertEqual(len(result["findings"]), local_review.MAX_FINDINGS)
        # 1 fresh + MAX_FINDINGS carried-forward = MAX_FINDINGS + 1, so exactly
        # one is dropped by the post-merge truncation (the fresh-turn slice
        # itself dropped nothing, since only 1 finding was reported).
        self.assertEqual(result["findings_truncated"], 1)


class _Req:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


class TestLocalSessionsListingOrder(unittest.IsolatedAsyncioTestCase):
    """Session filenames are random uuids (``session_path`` hashes/sanitizes
    the id, not a timestamp), so sorting by NAME sorts by nothing meaningful.
    The endpoint must sort by each session file's mtime instead, newest
    first, and slice to 25 BEFORE loading any session."""

    def setUp(self):
        self.mod = _load_routes_module()
        self.mod.is_app_enabled = lambda name: True

    async def test_sessions_are_returned_newest_mtime_first(self):
        import json as _json
        import time

        # Deliberately picked so alphabetical filename order is the OPPOSITE
        # of the intended mtime-newest-first order below -- otherwise a
        # regression back to sorting by filename would coincidentally still
        # pass this test.
        ids = ["zzz-oldest", "mmm-middle", "aaa-newest"]
        for sid in ids:
            local_review.save_session({"id": sid, "findings": []})

        # Force a deterministic, strictly-increasing mtime per file,
        # independent of filesystem timestamp resolution and independent of
        # the session id each file is named after.
        now = time.time()
        for offset, sid in enumerate(ids):
            path = local_review.session_path(sid)
            stamp = now + offset
            os.utime(path, (stamp, stamp))

        response = await self.mod._handle_local_sessions(_Req())
        body = _json.loads(response.body)
        returned_ids = [item["id"] for item in body["sessions"]]

        self.assertEqual(returned_ids, ["aaa-newest", "mmm-middle", "zzz-oldest"])
