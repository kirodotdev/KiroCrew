"""Acquisition and integrity, with no network reached in any test.

Every test either points ``ensure`` at a file this module wrote itself, or calls
it with ``allow_download=False`` so the download branch is unreachable. The URLs
in the fabricated specs are ``.invalid`` hosts (RFC 2606) so a regression that
starts fetching fails loudly instead of silently going online.

The property worth the most here is that verification runs on a **cache hit**.
The failure it catches is not a malicious file — it is a corpus truncated by a
full disk after a successful download, which passes an existence check forever
and turns every later baseline into a comparison against a different dataset.
The second-most-valuable is the ``sha256=None`` sidecar tier: it is the weaker of
the two integrity guarantees and the one a refactor is most likely to drop,
because dropping it breaks nothing on the first run.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kiro_crew.eval.bench import datasets
from kiro_crew.eval.bench.datasets import (
    SPECS,
    CorpusFetchError,
    DatasetSpec,
    cache_dir,
    describe,
    ensure,
    load_json,
)


@pytest.fixture()
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the cache into tmp_path and prove the redirection took."""
    monkeypatch.setenv("KIROCREW_BENCH_CACHE", str(tmp_path))
    assert cache_dir() == tmp_path
    return tmp_path


def _spec(
    filename: str = "fake_corpus.json",
    *,
    sha256: str | None = None,
    key: str = "fake",
    measures_retrieval: bool = True,
) -> DatasetSpec:
    return DatasetSpec(
        key=key,
        dataset="fake",
        variant="v0",
        url=f"https://example.invalid/{filename}",
        filename=filename,
        approx_bytes=277_380_000,
        sha256=sha256,
        measures_retrieval=measures_retrieval,
        note="a fabricated spec; the URL is intentionally unreachable",
    )


def _write(path: Path, body: bytes = b'[{"sample_id": "conv-1"}]') -> str:
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


# ── cache_dir redirection ────────────────────────────────────────────────────


def test_cache_override_wins_over_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hundreds of MB of third-party data must be relocatable without editing code."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("KIROCREW_BENCH_CACHE", str(tmp_path / "explicit"))
    assert cache_dir() == tmp_path / "explicit"


def test_cache_dir_falls_back_to_xdg_then_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never under KIROCREW_HOME: `kirocrew snapshot` must not swallow the corpora."""
    monkeypatch.delenv("KIROCREW_BENCH_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache_dir() == tmp_path / "xdg" / "kirocrew" / "bench-data"
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert cache_dir() == Path.home() / ".cache" / "kirocrew" / "bench-data"


# ── The refusal that keeps a test suite offline ───────────────────────────────


def test_missing_file_with_downloads_disabled_names_the_fetch_command(cache: Path) -> None:
    """The error has to be actionable, or the next person reaches for curl."""
    spec = _spec(key="longmemeval_s")
    with pytest.raises(CorpusFetchError) as exc:
        ensure(spec, allow_download=False)
    msg = str(exc.value)
    assert "kirocrew bench fetch longmemeval_s" in msg
    assert "downloading is disabled" in msg
    # The size is quoted so nobody starts a 277 MB fetch by surprise.
    assert "277 MB" in msg
    assert not (cache / spec.filename).exists()


def test_unknown_string_key_lists_the_known_ones(cache: Path) -> None:
    with pytest.raises(CorpusFetchError) as exc:
        ensure("longmemeval_xl", allow_download=False)
    msg = str(exc.value)
    assert "unknown corpus 'longmemeval_xl'" in msg
    for key in SPECS:
        assert key in msg


def test_ensure_accepts_a_string_key_resolved_through_specs(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both call shapes must reach the same verification code."""
    digest = _write(cache / "fake_corpus.json")
    monkeypatch.setitem(SPECS, "fake", _spec(sha256=digest))
    assert ensure("fake", allow_download=False) == cache / "fake_corpus.json"


# ── Pinned-upstream tier: a hardcoded hash ───────────────────────────────────


def test_matching_hash_returns_the_path(cache: Path) -> None:
    digest = _write(cache / "fake_corpus.json")
    assert ensure(_spec(sha256=digest), allow_download=False) == cache / "fake_corpus.json"


def test_corrupted_file_raises_and_quotes_both_hashes(cache: Path) -> None:
    """Both hashes, because "checksum mismatch" alone is not a diagnosis.

    The expected value is what a human has to re-derive deliberately if upstream
    really did move; the actual value is what they compare a fresh download
    against. Printing one without the other makes the next step guesswork.
    """
    path = cache / "fake_corpus.json"
    digest = _write(path)
    with path.open("ab") as fh:
        fh.write(b"\n")  # one byte, the shape a truncation or an append takes
    corrupted = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(CorpusFetchError) as exc:
        ensure(_spec(sha256=digest), allow_download=False)
    msg = str(exc.value)
    assert digest in msg and corrupted in msg
    assert "expected" in msg and "actual" in msg
    # And it says what to do, including that overwriting the pin is a decision.
    assert "must be re-derived deliberately" in msg


def test_verification_runs_on_a_cache_hit_not_only_after_download(cache: Path) -> None:
    """The property that catches a file truncated by a full disk.

    First call: the file is present and correct, so it is returned. Then the file
    is damaged in place and the same call is made again with downloading still
    disabled — so nothing but re-verification of the cached bytes can distinguish
    the two outcomes. A verify-after-download-only implementation returns the path
    both times.
    """
    path = cache / "fake_corpus.json"
    digest = _write(path, b'[{"sample_id": "conv-1"}]')
    spec = _spec(sha256=digest)

    assert ensure(spec, allow_download=False) == path

    path.write_bytes(b'[{"sample_id": "co')  # truncated mid-JSON
    with pytest.raises(CorpusFetchError, match="checksum mismatch"):
        ensure(spec, allow_download=False)


# ── Pinned-on-first-fetch tier: the sidecar ──────────────────────────────────


def test_sha256_none_writes_a_sidecar_on_first_ensure(cache: Path) -> None:
    path = cache / "fake_corpus.json"
    digest = _write(path)
    sidecar = cache / "fake_corpus.json.sha256"
    assert not sidecar.exists()

    assert ensure(_spec(sha256=None), allow_download=False) == path
    assert sidecar.read_text(encoding="utf-8").strip() == digest


def test_sidecar_catches_corruption_on_the_second_ensure(cache: Path) -> None:
    """The whole value of the weaker tier: drift is caught from run two onward.

    This is the tier most likely to rot in a refactor, because deleting the
    sidecar write breaks nothing on a first run and nothing in a test that only
    ever calls ensure once.
    """
    path = cache / "fake_corpus.json"
    digest = _write(path)
    spec = _spec(sha256=None)

    ensure(spec, allow_download=False)  # pins
    path.write_bytes(b'[{"sample_id": "conv-2-different-bytes"}]')
    new_digest = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(CorpusFetchError) as exc:
        ensure(spec, allow_download=False)
    msg = str(exc.value)
    assert digest in msg  # the pinned value from the first fetch
    assert new_digest in msg
    assert digest != new_digest


def test_sidecar_is_not_rewritten_once_it_exists(cache: Path) -> None:
    """A re-pin on every call would make the tier unable to detect anything."""
    path = cache / "fake_corpus.json"
    _write(path)
    spec = _spec(sha256=None)
    ensure(spec, allow_download=False)
    first = (cache / "fake_corpus.json.sha256").read_text(encoding="utf-8")
    ensure(spec, allow_download=False)
    assert (cache / "fake_corpus.json.sha256").read_text(encoding="utf-8") == first


def test_integrity_label_reflects_which_tier_a_spec_is_on(cache: Path) -> None:
    assert _spec(sha256="ab" * 32).integrity == "pinned-upstream"
    assert _spec(sha256=None).integrity == "pinned-on-first-fetch"


# ── The real specs, and the guard describe() has to surface ───────────────────


def test_describe_says_the_oracle_variant_does_not_measure_recall(cache: Path) -> None:
    """The evidence-only trap has to be visible in the inventory, not just in code.

    ``longmemeval_oracle`` is the smallest LongMemEval variant and therefore the
    most tempting one to reach for, and its recall is trivially 1.0. If the
    listing a human reads before choosing a corpus does not say so, the guard in
    the runner is the first place they find out.
    """
    text = describe()
    oracle = text.split("longmemeval_oracle", 1)[1].split("longmemeval_s", 1)[0]
    assert "measures recall  NO" in oracle
    assert "evidence-only" in oracle.lower()
    assert "Cannot measure retrieval" in oracle


def test_describe_reports_the_redirected_cache_and_the_not_fetched_state(cache: Path) -> None:
    text = describe()
    assert f"cache: {cache}" in text
    assert text.count("not fetched") == len(SPECS)
    (cache / SPECS["locomo10"].filename).write_bytes(b"{}")
    assert "(cached)" in describe()


def test_real_specs_declare_their_tiers_and_retrieval_capability() -> None:
    """Pins the two facts the runner branches on, so a spec edit is deliberate."""
    assert SPECS["locomo10"].measures_retrieval is True
    assert SPECS["locomo10"].integrity == "pinned-upstream"
    assert SPECS["longmemeval_oracle"].measures_retrieval is False
    assert SPECS["longmemeval_s"].measures_retrieval is True
    # The retrieval-measuring LongMemEval variant is the one too large to have
    # been hashed at authoring time; that is exactly why the sidecar tier exists.
    assert SPECS["longmemeval_s"].sha256 is None
    assert SPECS["longmemeval_s"].integrity == "pinned-on-first-fetch"


def test_urls_are_pinned_to_immutable_revisions_not_a_moving_branch() -> None:
    """A corpus that changes under a stored baseline turns a regression into a mystery.

    Asserted positively — every URL must contain its pinned revision — rather than
    by enumerating the moving-ref names to exclude. The positive form is the
    stronger check (it fails on any unpinned URL, not just the two names someone
    thought to list) and it avoids spelling a non-inclusive default branch name
    that the inclusive-language gate flags on added lines.
    """
    pinned = (datasets._LOCOMO_COMMIT, datasets._LME_HF_REVISION)
    for key, spec in SPECS.items():
        assert any(rev in spec.url for rev in pinned), f"{key} is not revision-pinned"
    assert datasets._LOCOMO_COMMIT in SPECS["locomo10"].url
    assert datasets._LME_HF_REVISION in SPECS["longmemeval_oracle"].url
    assert datasets._LME_HF_REVISION in SPECS["longmemeval_s"].url


# ── load_json rides on the same verification ─────────────────────────────────


def test_load_json_verifies_before_parsing(cache: Path) -> None:
    path = cache / "fake_corpus.json"
    digest = _write(path, b'[{"sample_id": "conv-1"}]')
    assert load_json(_spec(sha256=digest), allow_download=False) == [{"sample_id": "conv-1"}]

    path.write_bytes(b'[{"sample_id": "conv-1"}] ')
    with pytest.raises(CorpusFetchError):
        load_json(_spec(sha256=digest), allow_download=False)


# ── Scheme guard on the download path ────────────────────────────────────────
# urlopen honours file:// and ftp://. SPECS only ever holds https:// literals, but
# ensure() takes a caller-supplied DatasetSpec, so the guard is what stops a spec
# carrying file:///etc/passwd from reading a local file and having it verified,
# cached and parsed as "corpus". Without these tests the guard is just a comment
# that silences a static-analysis finding.


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "http://example.invalid/corpus.json",
        "ftp://example.invalid/corpus.json",
        "HTTP://example.invalid/corpus.json",
    ],
)
def test_non_https_urls_are_refused_before_any_request(
    cache: Path, bad_url: str
) -> None:
    spec = DatasetSpec(
        key="evil",
        dataset="fake",
        variant="v0",
        url=bad_url,
        filename="evil.json",
        approx_bytes=1,
        sha256=None,
        measures_retrieval=True,
    )
    with pytest.raises(CorpusFetchError) as exc:
        datasets.ensure(spec)
    assert "only https://" in str(exc.value)
    # Nothing may be left behind -- not the file, not a .part, not a sidecar.
    assert not (cache / "evil.json").exists()
    assert not (cache / "evil.json.part").exists()
    assert not (cache / "evil.json.sha256").exists()


def test_the_scheme_check_is_case_insensitive_on_the_allowed_scheme(cache: Path) -> None:
    """An uppercase HTTPS:// must not be rejected as if it were another scheme.

    Asserted via the error message rather than by letting a request happen: the
    URL is unreachable, so reaching the network layer at all is proof the guard
    let it through, and the failure that follows is a URLError, not the scheme
    refusal.
    """
    spec = DatasetSpec(
        key="upper",
        dataset="fake",
        variant="v0",
        url="HTTPS://example.invalid/corpus.json",
        filename="upper.json",
        approx_bytes=1,
        sha256=None,
        measures_retrieval=True,
    )
    with pytest.raises(CorpusFetchError) as exc:
        datasets.ensure(spec)
    assert "only https://" not in str(exc.value)
