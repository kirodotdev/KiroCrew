"""Round-13: the hardlink rule's third site, and absent provenance treated as equal.

Both are sibling sites of rules already established in this PR, which is why the fixes
route through the existing helper rather than adding a fourth bespoke check.

1. `_sha256_file` carries `O_NOFOLLOW` but had no nlink check, so a credential
   hardlinked at the corpus cache path was hashed and its SHA-256 published as the
   corpus checksum. A digest of a secret is still a leak.

2. `compare_reports` compared provenance by INEQUALITY. Absent on one side mismatches a
   present value -- which the code comment already claimed -- but absent on BOTH sides is
   `None != None`, i.e. False, i.e. "compatible". Two reports with no fingerprint
   compared as an exact delta. `_section` returning {} for a malformed report made that
   two-sided case easy to reach, so my own earlier fix widened the path to it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.eval.bench.datasets import _sha256_file
from kiro_crew.eval.bench.run import compare_reports
from kiro_crew.eval.bench.safepath import UnsafePathError


def _hardlink_or_skip(alias: Path, target: Path) -> None:
    try:
        os.link(target, alias)
    except (OSError, NotImplementedError):  # pragma: no cover - fs/platform dependent
        pytest.skip("hardlinks unavailable here (filesystem or platform)")


# ── The hash read must refuse a hardlink alias ───────────────────────────────


def test_hashing_a_hardlinked_corpus_path_is_refused(tmp_path: Path) -> None:
    """The leak is the DIGEST, not the bytes -- and a digest of a secret is still a leak.

    A hardlink shares its target's inode, so `realpath` yields the alias's own name and
    `is_symlink()` is False: only `st_nlink` on the open descriptor can see it.
    """
    secret = tmp_path / "pretend_credentials"
    secret.write_text("aws_secret_access_key = MUST-NOT-BE-DIGESTED\n", encoding="utf-8")
    alias = tmp_path / "locomo10.json"
    _hardlink_or_skip(alias, secret)

    with pytest.raises(UnsafePathError) as excinfo:
        _sha256_file(alias)
    message = str(excinfo.value)
    assert "hard link" in message
    assert "MUST-NOT-BE-DIGESTED" not in message


def test_hashing_an_ordinary_corpus_file_still_works(tmp_path: Path) -> None:
    """The suite must not be able to pass by refusing every hash."""
    payload = tmp_path / "locomo10.json"
    payload.write_text("[]", encoding="utf-8")
    digest = _sha256_file(payload)
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


# ── Absent provenance is a refusal, not a match ─────────────────────────────


def _report(*, corpus: dict | None = None, config: dict | None = None) -> dict:
    return {
        "corpus": {"fingerprint": "f" * 64} if corpus is None else corpus,
        "config": (
            {
                "ingest": {"granularity": "turn", "timeline": "now"},
                "retrieval": {"limit": 20, "mmr": True},
                "search_backend": "sqlite_cosine",
                "embedder": "qwen3-embedding:0.6b@1024",
                "environment": {"python": "3.12.10"},
            }
            if config is None
            else config
        ),
        "metrics": {
            "session": {"recall_all@5": 0.5},
            "session_measurable": {"5": 1977},
            "session_population": {"5": "a" * 64},
        },
    }


def _refused(out: str) -> bool:
    return "not comparable" in out.lower()


def test_two_reports_with_no_fingerprint_do_not_compare() -> None:
    """`None != None` is False, so this used to read as "same corpus"."""
    both_missing = _report(corpus={})
    out = compare_reports(both_missing, _report(corpus={}), k=5)
    assert _refused(out)
    assert "no corpus fingerprint" in out


@pytest.mark.parametrize(
    "missing_key",
    ["ingest", "retrieval", "search_backend", "embedder", "environment"],
)
def test_a_config_field_missing_from_both_reports_does_not_compare(missing_key: str) -> None:
    """Every field in the compatibility set, not just the one that was reported."""
    cfg = _report()["config"]
    del cfg[missing_key]
    out = compare_reports(_report(config=cfg), _report(config=dict(cfg)), k=5)
    assert _refused(out)
    assert missing_key in out


def test_one_sided_absence_still_refuses() -> None:
    """The property the old code did have -- it must survive the change."""
    cfg = _report()["config"]
    del cfg["embedder"]
    out = compare_reports(_report(config=cfg), _report(), k=5)
    assert _refused(out)


def test_a_fully_provenanced_pair_still_compares() -> None:
    """Otherwise the requirement would refuse every legitimate comparison."""
    out = compare_reports(_report(), _report(), k=5)
    assert not _refused(out)
    assert "recall_all@5" in out


def test_differing_fingerprints_still_report_the_difference() -> None:
    """Presence is checked first, but a real mismatch must still be named as one."""
    other = _report(corpus={"fingerprint": "b" * 64})
    out = compare_reports(_report(), other, k=5)
    assert _refused(out)
    assert "fingerprints differ" in out
