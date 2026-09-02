"""Round-10 findings: the hardlink bypass, and the last raw report values.

The hardlink one is the most concrete security defect in this PR, and it is a
different attack surface from every symlink test here: a hardlink SHARES its target's
inode, so `realpath` yields the alias's own name, `is_symlink()` is False, and
`O_NOFOLLOW` has no link to refuse. Demonstrated before fixing -- a write through the
alias replaced the victim file's contents outright.

The discriminator is `st_nlink` on the OPEN DESCRIPTOR, which is also why it is not a
check-then-use: the fd already refers to the inode being judged.

The second finding is the third recurrence of "a value read from a report reached an
operation that assumes its type" -- `bp[:12]` on a non-string, `float(...)` on a list
-- so it is fixed with the readers that were missing rather than two more guards.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.eval.bench.run import compare_reports
from kiro_crew.eval.bench.safepath import (
    UnsafePathError,
    open_write_nofollow,
    read_text_nofollow,
)


def _hardlink_or_skip(alias: Path, target: Path) -> None:
    try:
        os.link(target, alias)
    except (OSError, NotImplementedError):  # pragma: no cover - fs/platform dependent
        pytest.skip("hardlinks unavailable here (filesystem or platform)")


# ── The write side must not truncate through an alias ────────────────────────


def test_a_write_through_a_hardlink_is_refused(tmp_path: Path) -> None:
    """And the victim's bytes are asserted intact.

    A test that only checked for the exception would pass even if O_TRUNC had already
    destroyed the file -- which is exactly what happened before the fix, because
    truncation used to occur at open time.
    """
    victim = tmp_path / "important.txt"
    victim.write_text("ORIGINAL CONTENTS\n", encoding="utf-8")
    alias = tmp_path / "corpus.json.part"
    _hardlink_or_skip(alias, victim)

    with pytest.raises(UnsafePathError) as excinfo:
        open_write_nofollow(alias, what="corpus download staging file")
    assert "hard link" in str(excinfo.value)
    assert victim.read_text(encoding="utf-8") == "ORIGINAL CONTENTS\n", (
        "the victim was truncated -- O_TRUNC ran before the inode could be judged"
    )


def test_an_existing_name_is_refused_instead_of_truncated(tmp_path: Path) -> None:
    """The writer creates; it does not reopen.

    Truncating an existing name is what made the no-O_NOFOLLOW fallback exploitable:
    the name must be opened for writing before anything about it can be judged, so a
    link planted after the check gets followed. Exclusive creation has no such moment.
    The price is that this primitive can no longer replace a file -- callers that mean
    to do that publish by rename (`write_text_atomic_nofollow`).
    """
    target = tmp_path / "report.json"
    target.write_text("A" * 500, encoding="utf-8")

    with pytest.raises(UnsafePathError, match="already exists"):
        open_write_nofollow(target, what="JSON report")

    assert target.read_text(encoding="utf-8") == "A" * 500, "the old bytes were touched"


def test_a_fresh_write_still_works(tmp_path: Path) -> None:
    fd = open_write_nofollow(tmp_path / "new.json", what="JSON report")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{}")
    assert (tmp_path / "new.json").read_text(encoding="utf-8") == "{}"


# ── The read side must not disclose through an alias ────────────────────────


def test_a_read_through_a_hardlink_is_refused(tmp_path: Path) -> None:
    """The sidecar path is the sharp one: its bytes get echoed as a checksum."""
    secret = tmp_path / "pretend_credentials"
    secret.write_text("MUST-NOT-BE-ECHOED\n", encoding="utf-8")
    alias = tmp_path / "corpus.json.sha256"
    _hardlink_or_skip(alias, secret)

    with pytest.raises(UnsafePathError) as excinfo:
        read_text_nofollow(alias, what="checksum sidecar")
    message = str(excinfo.value)
    assert "hard link" in message
    assert "MUST-NOT-BE-ECHOED" not in message, "the refusal echoed the bytes it refused"


def test_an_ordinary_read_still_works(tmp_path: Path) -> None:
    """The suite must not be able to pass by refusing every read."""
    payload = tmp_path / "corpus.json"
    payload.write_text('{"sample": []}', encoding="utf-8")
    assert read_text_nofollow(payload, what="corpus file") == '{"sample": []}'


def test_a_symlink_to_an_ordinary_file_is_still_readable(tmp_path: Path) -> None:
    """The documented read/write asymmetry survives the rewrite.

    The read helper stopped delegating to `hooks.safe_read_file` in this round, so
    this pins that it still opens the RESOLVED path -- refusing every symlink here
    would break a corpus cache legitimately linked to another disk.
    """
    real = tmp_path / "real.json"
    real.write_text("[]", encoding="utf-8")
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
        pytest.skip("symlink creation requires privileges on this platform")
    assert read_text_nofollow(link, what="corpus file") == "[]"


# ── Malformed digests and metric values refuse rather than crash ────────────


def _report(*, digest: object = "a" * 64, metric: object = 0.5) -> dict:
    return {
        "corpus": {"fingerprint": "f" * 64},
        "config": {
            "ingest": {"granularity": "turn", "timeline": "now"},
            "retrieval": {"limit": 20, "mmr": True},
            "search_backend": "sqlite_cosine",
            "embedder": "qwen3-embedding:0.6b@1024",
            "environment": {"python": "3.12.10"},
        },
        "metrics": {
            "session": {"recall_all@5": metric},
            "session_measurable": {"5": 1977},
            "session_population": {"5": digest},
        },
    }


def _refused(out: str) -> bool:
    return "not comparable" in out.lower()


@pytest.mark.parametrize("digest", [12345, None, [], {"a": 1}, "", True])
def test_a_non_string_population_digest_refuses(digest: object) -> None:
    """`bp[:12]` for the message is a TypeError on anything unsliceable."""
    out = compare_reports(_report(digest=digest), _report(), k=5)
    assert _refused(out)


@pytest.mark.parametrize("metric", ["many", None, [], {"a": 1}, True, float("nan"), float("inf")])
def test_an_unusable_metric_value_refuses(metric: object) -> None:
    """NaN is in this list on purpose: it compares falsely against itself, so it
    would render a delta no reader could act on."""
    out = compare_reports(_report(metric=metric), _report(), k=5)
    assert _refused(out) or "recall_all@5" not in out


def test_a_numeric_string_metric_is_not_silently_accepted() -> None:
    """`float("0.5")` succeeds, which would let a differently-typed report compare
    against a real one. The reader requires an actual number."""
    out = compare_reports(_report(metric="0.5"), _report(), k=5)
    assert _refused(out) or "recall_all@5" not in out


def test_a_well_formed_pair_still_reports_its_delta() -> None:
    out = compare_reports(_report(metric=0.40), _report(metric=0.60), k=5)
    assert not _refused(out)
    assert "recall_all@5" in out
    assert "+0.2000" in out
