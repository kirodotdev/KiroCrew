"""Regression tests for the review findings on the pipeline fold and its routes.

Each test here exists because a reviewer found the defect in shipped code, so each
one is written to FAIL against the version that had it rather than merely to
describe the fixed behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.auto_triage_pipeline.backend import pipeline_fold as fold
from kiro_crew.apps.builtins.auto_triage_pipeline.backend import routes

# --------------------------------------------------------------------------
# The issue cache is a WRAPPER; the fields live under `detail`.
# --------------------------------------------------------------------------


def test_cached_issue_detail_is_read_not_the_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the wrapper discarded every real field.

    The cache writes ``{owner, repo, number, detail, timeline}`` and title, labels,
    assignees and comments all live inside ``detail``. Returning the outer dict made
    them always absent, so a fully cached issue rendered as "not cached" -- a real
    defect wearing the costume of missing data.
    """
    repo_dir = tmp_path / "repos" / "acme" / "widget"
    repo_dir.mkdir(parents=True)
    monkeypatch.setattr(fold, "_issue_cache_dir", lambda owner, repo: repo_dir)
    (repo_dir / "issue-42.json").write_text(
        json.dumps(
            {
                "owner": "acme",
                "repo": "widget",
                "number": 42,
                "detail": {
                    "title": "a real title",
                    "labels": [{"name": "bug"}, {"name": "area: core"}],
                    "assignees": [{"login": "someone"}],
                    "comments": [{"body": "x"}, {"body": "y"}],
                },
                "timeline": [],
            }
        ),
        encoding="utf-8",
    )

    detail = fold._read_issue_cache(42, "acme", "widget")

    assert detail.get("title") == "a real title"
    assert fold._names(detail.get("labels")) == ["bug", "area: core"]
    assert fold._names(detail.get("assignees")) == ["someone"]


def test_a_flat_cached_record_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A record with no wrapper is used as-is rather than thrown away."""
    repo_dir = tmp_path / "repos" / "acme" / "widget"
    repo_dir.mkdir(parents=True)
    monkeypatch.setattr(fold, "_issue_cache_dir", lambda owner, repo: repo_dir)
    (repo_dir / "issue-7.json").write_text(
        json.dumps({"title": "flat", "labels": [{"name": "enhancement"}]}), encoding="utf-8"
    )

    detail = fold._read_issue_cache(7, "acme", "widget")

    assert detail.get("title") == "flat"


# --------------------------------------------------------------------------
# A file between the two size ceilings must be a refusal, not a crash.
# --------------------------------------------------------------------------


def test_reader_size_refusal_becomes_a_fold_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader enforces its own, lower cap; its refusal must not escape.

    This module's ceiling used to be higher than the reader's, so a file between the
    two passed the local size check and then raised out of the route as an unhandled
    HTTP 500. Both halves are covered: the effective limit is the smaller of the
    two, AND the reader's exception is translated.
    """
    from kiro_crew import hooks

    target = tmp_path / "audit.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    def refuse(_raw: str) -> bytes:
        raise hooks.FileTooLargeError("File exceeds the safety cap")

    monkeypatch.setattr(hooks, "safe_read_file_bytes", refuse)

    with pytest.raises(fold.FoldError) as caught:
        fold._read_text_bounded(target, fold.MAX_LOG_BYTES, "The pipeline event log")

    # The message must not leak an absolute path.
    assert str(tmp_path) not in str(caught.value)


def test_the_effective_ceiling_is_never_above_the_readers(tmp_path: Path) -> None:
    """A caller asking for more than the reader allows gets the reader's limit."""
    from kiro_crew import hooks

    oversized = tmp_path / "big.jsonl"
    oversized.write_bytes(b"x" * (hooks.MAX_FILE_BYTES + 1024))

    with pytest.raises(fold.FoldError):
        fold._read_text_bounded(oversized, fold.MAX_LOG_BYTES, "The pipeline event log")


# --------------------------------------------------------------------------
# Repo params are an allow-list: a deny-list already missed a real escape.
# --------------------------------------------------------------------------


def _query(**params: str) -> web.Request:
    from urllib.parse import urlencode

    from aiohttp.test_utils import make_mocked_request

    return make_mocked_request("GET", f"/x?{urlencode(params)}")


@pytest.mark.parametrize(
    "owner",
    [
        "D:foo",  # Windows drive-relative: resolves off the cache root
        "..",  # parent
        ".hidden",  # leading dot
        "a/b",  # separator
        "a\\b",  # Windows separator
        "with space",
        "semi;colon",
        "nul\x00byte",
        "x" * 101,  # over length
        "",  # empty
    ],
)
def test_hostile_owner_names_are_refused(owner: str) -> None:
    """Every one of these once had to be enumerated by hand; now the rule is an
    allow-list, so a character nobody thought of is refused by default.

    ``D:foo`` is the case that shipped: the deny-list rejected slashes and a leading
    dot, and accepted a drive-relative name that resolves against that drive's
    current directory rather than under the cache root.
    """
    result = routes._repo_params(_query(owner=owner, repo="widget"))
    assert isinstance(result, web.Response)
    assert result.status == 400


def test_a_real_owner_and_repo_are_accepted() -> None:
    result = routes._repo_params(_query(owner="acme-org", repo="widget"))
    assert result == ("acme-org", "widget")


def test_names_with_the_punctuation_github_allows_are_accepted() -> None:
    result = routes._repo_params(_query(owner="some-org", repo="my_repo.js"))
    assert result == ("some-org", "my_repo.js")
