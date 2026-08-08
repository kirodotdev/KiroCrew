"""Round-4 review findings: derived paths, and a hole created by round 2's own fix.

Both blockings this round came from earlier fixes in this PR, which is the useful
signal: round 3 audited which *entry points* get guarded and that held (no new
ungated entry point was found), but it did not consider paths DERIVED from an
already-guarded one, nor the check-to-use window between guarding a name and opening
it. And the measurability filter added in round 2 introduced a new way for the
comparison to be wrong -- a new field with a default is a new default to be wrong
about.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.eval.bench import datasets
from kiro_crew.eval.bench.run import compare_reports
from kiro_crew.eval.bench.safepath import (
    UnsafePathError,
    open_write_nofollow,
    read_text_nofollow,
)


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".aws").mkdir()
    return tmp_path


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("creating a symlink needs privilege on this host")


# ── A guarded name is not a guarded descriptor ───────────────────────────────


def test_writing_through_a_planted_symlink_is_refused(fake_home: Path, tmp_path: Path) -> None:
    """Refused, and the victim untouched. Two layers can stop this; assert the outcome.

    A link whose target is itself protected is caught by `guard_write_path`, because
    `Path.resolve()` follows the link and the resolved target is sensitive. The
    O_NOFOLLOW layer is what covers the case the guard cannot see -- see the next
    test.
    """
    victim = fake_home / ".aws" / "credentials"
    victim.write_text("[default]\nORIGINAL\n")
    link = tmp_path / "corpus.json.part"
    _symlink_or_skip(link, victim)

    with pytest.raises(UnsafePathError):
        open_write_nofollow(link, what="corpus download staging file")
    assert victim.read_text() == "[default]\nORIGINAL\n"


def test_a_link_to_an_unprotected_file_is_also_refused(tmp_path: Path) -> None:
    """The case ONLY O_NOFOLLOW catches, and the reason it is not redundant.

    Here the link's target is an ordinary file, so the path guard has nothing to
    object to -- it resolves to a perfectly safe location. But the caller authorized
    a write to `<corpus>.part`, not to whatever that name happens to point at, and
    silently following the redirect would truncate an unrelated file.
    """
    bystander = tmp_path / "someone-elses-notes.txt"
    bystander.write_text("KEEP ME\n")
    link = tmp_path / "corpus.json.part"
    _symlink_or_skip(link, bystander)

    with pytest.raises(UnsafePathError) as exc:
        open_write_nofollow(link, what="corpus download staging file")
    assert "symbolic link" in str(exc.value)
    assert bystander.read_text() == "KEEP ME\n"


def test_reading_through_a_planted_symlink_is_refused(fake_home: Path, tmp_path: Path) -> None:
    """This is the sharpest case: the bytes would be printed as a checksum."""
    victim = fake_home / ".aws" / "credentials"
    victim.write_text("aws_secret_access_key = MUST-NOT-BE-ECHOED\n")
    link = tmp_path / "corpus.json.sha256"
    _symlink_or_skip(link, victim)

    with pytest.raises(UnsafePathError) as exc:
        read_text_nofollow(link, what="checksum sidecar")
    assert "MUST-NOT-BE-ECHOED" not in str(exc.value)


def test_a_plain_file_still_round_trips(tmp_path: Path) -> None:
    """Guard against a vacuous suite: the normal path must still work."""
    target = tmp_path / "ok.sha256"
    with os.fdopen(open_write_nofollow(target, what="checksum sidecar"), "w") as fh:
        fh.write("deadbeef\n")
    assert read_text_nofollow(target, what="checksum sidecar").strip() == "deadbeef"


def test_the_sidecar_read_in_ensure_goes_through_the_nofollow_path(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: a link at `<corpus>.sha256` must stop `ensure`, not feed it.

    Exercised through `ensure` rather than the helper alone, because the helper being
    correct proves nothing if the call site still uses `read_text`.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("KIROCREW_BENCH_CACHE", str(cache))

    spec = datasets.SPECS["longmemeval_s"]  # sha256 is None -> takes the sidecar path
    corpus = cache / spec.filename
    corpus.write_text("[]")
    victim = fake_home / ".aws" / "credentials"
    victim.write_text("aws_secret_access_key = MUST-NOT-BE-ECHOED\n")
    _symlink_or_skip(corpus.with_suffix(corpus.suffix + ".sha256"), victim)

    with pytest.raises(UnsafePathError):
        datasets.ensure(spec, allow_download=False)


# ── A missing measurement is not a zero ─────────────────────────────────────


def _report(session: dict, measurable: dict, *, embedder: str = "toy-hashed-bow") -> dict:
    return {
        "corpus": {"fingerprint": "abc"},
        "config": {
            "ingest": {"granularity": "turn"},
            "retrieval": {"mmr": True},
            "search_backend": "sqlite_cosine",
            "embedder": embedder,
        },
        "metrics": {"session": session, "session_measurable": measurable},
    }


def test_an_unmeasurable_baseline_does_not_become_a_zero() -> None:
    """The exact shape round 2 created: absent on one side, defaulted to 0.0.

    A cut-off the baseline's window never exposed is omitted from its metric dict.
    Substituting zero turned "could not measure" into "scored nothing" and published
    the candidate's full value as an exact improvement.
    """
    baseline = _report({}, {"10": 0})
    candidate = _report({"recall_all@10": 0.6429}, {"10": 1966})
    out = compare_reports(baseline, candidate, k=10)
    assert "not comparable" in out
    assert "No delta is reported" in out
    # The fabricated improvement must appear nowhere.
    assert "+0.6429" not in out
    assert "0.0000" not in out


def test_differing_populations_are_refused_with_both_counts_named() -> None:
    """746 vs 1977 is the real shape from this harness's own LoCoMo runs."""
    baseline = _report({"recall_all@5": 0.2279}, {"5": 746})
    candidate = _report({"recall_all@5": 0.4901}, {"5": 1977})
    out = compare_reports(baseline, candidate, k=5)
    assert "not comparable" in out
    assert "746" in out and "1977" in out
    assert "different populations" in out
    assert "+0.2622" not in out


def test_a_report_predating_the_measurable_counts_is_refused() -> None:
    """Legacy reports must not silently take the comparable path."""
    legacy = _report({"recall_all@5": 0.5}, {})
    current = _report({"recall_all@5": 0.6}, {"5": 100})
    out = compare_reports(legacy, current, k=5)
    assert "not comparable" in out
    assert "predate" in out


def test_matching_populations_compare_and_show_the_denominator() -> None:
    baseline = _report({"recall_all@5": 0.40, "ndcg@5": 0.30}, {"5": 1977})
    candidate = _report({"recall_all@5": 0.60, "ndcg@5": 0.35}, {"5": 1977})
    out = compare_reports(baseline, candidate, k=5)
    assert "not comparable" not in out
    assert "1977 queries in each arm" in out
    assert "+0.2000" in out
    assert "deterministic" in out


def test_a_metric_present_on_only_one_side_is_named_not_zero_filled() -> None:
    """Per-metric asymmetry inside an otherwise comparable cut-off."""
    baseline = _report({"recall_all@5": 0.40}, {"5": 1977})
    candidate = _report({"recall_all@5": 0.60, "ndcg@5": 0.35}, {"5": 1977})
    out = compare_reports(baseline, candidate, k=5)
    assert "+0.2000" in out  # the shared metric still compares
    assert "Omitted (present in only one report" in out
    assert "ndcg@5" in out
    assert "+0.3500" not in out  # the one-sided metric is not a delta
