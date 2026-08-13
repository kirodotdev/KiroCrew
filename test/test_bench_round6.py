"""Round-6 review findings: two blocking defects, both real.

Both are the SAME incomplete-sweep pattern that produced round 5's fifth finding:
a refusal was added at one site and the sibling site was left alone.

* the report writer guarded its composed paths but still used `Path.write_text`,
  and the guard returns the RESOLVED path -- so a link planted at `<stem>.md` had
  already been followed by the time the write happened. `--stem` was also free to
  be absolute or traversing, which composes to a file outside the `--out-dir`
  that was the thing actually checked;
* ingest refuses a NULL DOCUMENT embedding (round 5) but the QUERY embedding was
  passed inline to `search_episodic`, and a NULL there is worse than a NULL row:
  it switches that entire question to FTS5 keyword ranking, which the report then
  presents as vector recall.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.eval.bench.corpus import CAT_SINGLE_HOP, BenchQuery
from kiro_crew.eval.bench.ingest import IngestError
from kiro_crew.eval.bench.retrieval import RetrievalConfig, retrieve_for_query
from kiro_crew.eval.bench.run import write_report
from kiro_crew.eval.bench.safepath import UnsafePathError


def _link_or_skip(link: Path, target: Path) -> None:
    """Create a symlink, or skip if the platform will not allow it.

    Deliberately NOT a platform skip. The write path refuses a pre-planted link on
    every platform now -- `open_write_nofollow` falls back to an explicit
    `is_symlink()` check where `O_NOFOLLOW` does not exist -- so skipping on Windows
    would hide real coverage. The only platform-dependent part left is whether an
    unprivileged process may create the link in the first place.
    """
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
        pytest.skip("symlink creation requires privileges on this platform")


class _FakeOutcome:
    """Minimal stand-in: these tests are about path handling, not report content."""

    corpus_name = "locomo"

    @staticmethod
    def to_json() -> dict:
        return {"metrics": {}}


@pytest.fixture(autouse=True)
def _plain_report(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.eval.bench.run.format_report", lambda outcome: "# report\n"
    )


# ── Finding 1: the report stem must not compose outside --out-dir ────────────


@pytest.mark.parametrize(
    "stem",
    [
        "../escape",
        "../../etc/passwd",
        "/tmp/absolute",
        "sub/dir",
        "..",
    ],
)
def test_a_traversing_or_absolute_stem_is_refused(tmp_path: Path, stem: str) -> None:
    """`guard_write_path` answers "is this protected", not "is this inside out-dir".

    So a stem that composes outside the checked directory has to be refused on its
    own terms rather than resolved and allowed.
    """
    with pytest.raises(UnsafePathError) as excinfo:
        write_report(_FakeOutcome(), out_dir=tmp_path, stem=stem)
    assert "bare filename" in str(excinfo.value)


def test_an_ordinary_stem_still_writes_both_artifacts(tmp_path: Path) -> None:
    """The suite must not be able to pass by refusing every stem."""
    md, js = write_report(_FakeOutcome(), out_dir=tmp_path, stem="real_now")
    assert md.name == "real_now.md"
    assert js.name == "real_now.json"
    assert (tmp_path / "real_now.md").read_text(encoding="utf-8") == "# report\n"
    assert json.loads((tmp_path / "real_now.json").read_text(encoding="utf-8")) == {
        "metrics": {}
    }


def test_a_not_yet_existing_out_dir_is_created(tmp_path: Path) -> None:
    """The guard runs before `mkdir`, so a fresh directory must still work.

    Pins the ordering: gating before mkdir is deliberate (creating a tree under a
    protected root is already the damage), and it must not cost the ordinary case.
    """
    out = tmp_path / "nested" / "reports"
    md, js = write_report(_FakeOutcome(), out_dir=out, stem="fresh")
    assert md.exists() and js.exists()


def test_a_symlink_at_the_report_name_is_not_followed(tmp_path: Path) -> None:
    """A link to an ORDINARY file, so only the nofollow layer can catch it.

    The victim's contents are asserted untouched: a test that only checks for an
    exception would pass even if the write had already landed.
    """
    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite me", encoding="utf-8")
    out = tmp_path / "reports"
    out.mkdir()
    _link_or_skip(out / "planted.md", victim)

    with pytest.raises(UnsafePathError) as excinfo:
        write_report(_FakeOutcome(), out_dir=out, stem="planted")
    assert "symbolic link" in str(excinfo.value)
    assert victim.read_text(encoding="utf-8") == "do not overwrite me"


# ── Finding 2: a NULL query embedding must refuse ────────────────────────────


class _Store:
    """Records whether search was reached, so a leak-through is visible."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search_episodic(self, **kw: object) -> list[dict]:
        self.calls.append(kw)
        return []


class _Loaded:
    def __init__(self) -> None:
        self.store = _Store()
        self.text_to_turn: dict[str, str] = {}
        self.turn_attribution = True


def _query() -> BenchQuery:
    return BenchQuery(
        query_id="q1",
        question="where did they book the lessons?",
        category=CAT_SINGLE_HOP,
        raw_category="1",
        gold_session_ids=("s1",),
        gold_turn_ids=("t1",),
    )


def test_a_null_query_embedding_refuses_before_searching() -> None:
    loaded = _Loaded()
    with pytest.raises(IngestError) as excinfo:
        retrieve_for_query(
            loaded, _query(), embed_fn=lambda _t: None, config=RetrievalConfig()
        )
    msg = str(excinfo.value)
    assert "q1" in msg
    assert "keyword" in msg
    # The refusal must happen BEFORE the store is consulted -- reaching
    # search_episodic with a NULL vector is the defect, not a step on the way to it.
    assert loaded.store.calls == []


def test_a_healthy_query_reaches_the_store_with_its_vector() -> None:
    """Otherwise the suite could pass by refusing every query."""
    loaded = _Loaded()
    retrieve_for_query(
        loaded, _query(), embed_fn=lambda _t: [0.1, 0.2], config=RetrievalConfig()
    )
    assert len(loaded.store.calls) == 1
    call = loaded.store.calls[0]
    assert call["query_embedding"] == [0.1, 0.2]
    # query_text is still passed: production sends both, and MMR's diversity term
    # plus the FTS5 path read the text.
    assert call["query_text"] == "where did they book the lessons?"
