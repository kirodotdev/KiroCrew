"""Pins scripts/audit_bypass_merge.py on the September bypass merges."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

import audit_bypass_merge as mod  # noqa: E402

# A merge past pending: readiness last set pending 23:33:57Z, merged 23:46:38Z.
_PENDING = [
    {"context": "PR Readiness", "state": "pending", "created_at": "2026-09-30T23:33:57Z"},
    {"context": "AWS CodeBuild", "state": "success", "created_at": "2026-09-30T23:40:00Z"},
]

# A merge past failure, as GitHub returned it: readiness failure 03:41:22Z,
# merged 03:56:46Z.
_SHA_11594 = "263075d4bb1cb3c2b7c7188170fd2f989a6465fb"
_PR_11594 = {
    "number": 11594,
    "merged_at": "2026-09-18T03:56:46Z",
    "merged_by": {"login": "bolichen97"},
    "merge_commit_sha": _SHA_11594,
    "head": {"sha": "bc25adc29d565f1eb604edb2fdcc4b49f35d4378"},
}
_STATUSES_11594 = [
    {"context": "PR Readiness", "state": "failure", "created_at": "2026-09-18T03:41:22Z"},
    {"context": "PR Readiness", "state": "pending", "created_at": "2026-09-18T03:36:33Z"},
]


def test_a_merge_past_pending_readiness_is_a_bypass() -> None:
    pull = {"merged_at": "2026-09-30T23:46:38Z", "merged_by": {"login": "admin"}}
    code, text = mod.verdict(pull, _PENDING)
    assert code == 1
    assert "pending" in text


def test_a_merge_past_failing_readiness_is_a_bypass() -> None:
    code, text = mod.verdict(_PR_11594, _STATUSES_11594)
    assert code == 1
    assert "failure" in text and "bolichen97" in text


def test_a_success_set_after_the_merge_does_not_excuse_it() -> None:
    late = _PENDING + [
        {"context": "PR Readiness", "state": "success", "created_at": "2026-10-01T00:30:00Z"}
    ]
    assert mod.verdict({"merged_at": "2026-09-30T23:46:38Z"}, late)[0] == 1


def test_a_merge_on_green_readiness_passes() -> None:
    green = _PENDING + [
        {"context": "PR Readiness", "state": "success", "created_at": "2026-09-30T23:45:00Z"}
    ]
    assert mod.verdict({"merged_at": "2026-09-30T23:46:38Z"}, green)[0] == 0


def test_a_merge_with_no_readiness_at_all_is_a_bypass() -> None:
    assert mod.verdict({"merged_at": "2026-09-19T19:42:51Z"}, [])[0] == 1


def test_an_open_pr_is_not_reported() -> None:
    assert mod.verdict({"merged_at": None}, _PENDING)[0] == 0


def test_only_the_pr_whose_merge_commit_is_the_push_is_attributed() -> None:
    containing = {"number": 1, "merge_commit_sha": "0" * 40, "merged_at": "2026-09-18T00:00:00Z"}
    assert mod.pull_for_commit([containing, _PR_11594], _SHA_11594) is _PR_11594
    assert mod.pull_for_commit([containing], _SHA_11594) is None
    assert mod.pull_for_commit([], _SHA_11594) is None


def _fake_gh(monkeypatch: pytest.MonkeyPatch, pulls: list[dict]) -> None:
    def fake_all(path: str) -> list:
        return pulls if path.endswith("/pulls?per_page=100") else _STATUSES_11594

    monkeypatch.setattr(mod, "_gh_all", fake_all)
    # The commits/{sha}/pulls listing omits merged_by; the PR read carries it.
    monkeypatch.setattr(mod, "_gh", lambda path: _PR_11594)


def test_annotate_names_the_bypass_and_never_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = {k: v for k, v in _PR_11594.items() if k != "merged_by"}
    _fake_gh(monkeypatch, [listing])
    assert mod.main(["--commit", _SHA_11594, "--annotate"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("::warning::#11594 ") and "failure" in out
    assert "merged by bolichen97" in out


def test_without_annotate_the_bypass_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(monkeypatch, [_PR_11594])
    assert mod.main(["--commit", _SHA_11594]) == 1


def test_a_direct_push_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_gh(monkeypatch, [])
    assert mod.main(["--commit", _SHA_11594, "--annotate"]) == 0
    assert "::warning::" not in capsys.readouterr().out


def test_an_unreadable_api_warns_under_annotate_and_exits_two_otherwise(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(path: str) -> list:
        raise subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(mod, "_gh_all", boom)
    assert mod.main(["--commit", _SHA_11594, "--annotate"]) == 0
    assert capsys.readouterr().out.startswith("::warning::could not read")
    assert mod.main(["--commit", _SHA_11594]) == 2
