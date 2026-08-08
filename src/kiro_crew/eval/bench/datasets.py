"""Fetch-on-demand acquisition for the external benchmark corpora.

Nothing here is vendored into the repo. The reason is size, not licensing: the
LongMemEval variant that can actually measure retrieval is 277 MB, and its
sibling ``_m`` variant is 2.7 GB. Committing either would dominate the repository
and every clone of it. LoCoMo's 2.7 MB would be tolerable, but a benchmark
harness with one vendored corpus and one fetched corpus has two code paths where
one will do, so both are fetched.

Both sources are ungated and need no token — verified against the live
endpoints. Downloads are pinned to immutable upstream revisions (a git commit for
LoCoMo, a repo revision hash for the HuggingFace dataset) rather than a moving
``main``, because a corpus that changes underneath a stored baseline turns a
regression into a mystery.

Integrity has two tiers, and the distinction is reported rather than blurred:

* **Pinned upstream** — the expected SHA-256 is hardcoded below because the file
  was downloaded and hashed when this module was written. A mismatch is a hard
  error.
* **Pinned on first fetch** — no hardcoded hash (the file is too large to have
  been fetched during development). The first successful download writes a
  sidecar ``.sha256``; every later fetch is checked against it. This catches
  upstream drift from the second run onward, and says so plainly instead of
  claiming an integrity guarantee it does not have.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .safepath import (
    guard_output_dir,
    guard_write_path,
    open_write_nofollow,
    read_text_nofollow,
)

# Immutable upstream revisions, resolved and recorded at authoring time.
_LOCOMO_COMMIT = "3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376"
_LME_HF_REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"

_CHUNK = 1 << 20
_USER_AGENT = "kirocrew-bench/1"


@dataclass(frozen=True)
class DatasetSpec:
    """One downloadable corpus file.

    ``sha256`` is ``None`` for files large enough that they were never fetched
    during development; see the module docstring for what that costs.

    ``measures_retrieval`` is the field that stops a well-meaning default from
    producing a meaningless number. LongMemEval's ``oracle`` variant contains
    *only* the evidence sessions — verified 500/500 instances where the gold
    session set equals the full haystack session set — so its haystack has no
    distractors and retrieval is trivially perfect. It is a reading-comprehension
    corpus, not a memory-retrieval one. The retrieval ruler refuses to run
    against a spec with this flag false unless explicitly forced.
    """

    key: str
    dataset: str
    variant: str
    url: str
    filename: str
    approx_bytes: int
    sha256: str | None
    measures_retrieval: bool
    note: str = ""

    @property
    def integrity(self) -> str:
        return "pinned-upstream" if self.sha256 else "pinned-on-first-fetch"


SPECS: dict[str, DatasetSpec] = {
    "locomo10": DatasetSpec(
        key="locomo10",
        dataset="locomo",
        variant="locomo10",
        url=(
            "https://raw.githubusercontent.com/snap-research/locomo/"
            f"{_LOCOMO_COMMIT}/data/locomo10.json"
        ),
        filename="locomo10.json",
        approx_bytes=2_805_274,
        sha256="79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4",
        measures_retrieval=True,
        note=(
            "10 conversations, 1986 QA pairs, full haystack with distractors. "
            "Official metric is token-F1 per category with no LLM judge, which "
            "makes this the only corpus here that scores end-to-end without an "
            "external API key."
        ),
    ),
    "longmemeval_oracle": DatasetSpec(
        key="longmemeval_oracle",
        dataset="longmemeval",
        variant="oracle",
        url=(
            "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/"
            f"{_LME_HF_REVISION}/longmemeval_oracle.json"
        ),
        filename="longmemeval_oracle.json",
        approx_bytes=15_388_478,
        sha256="821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c",
        measures_retrieval=False,
        note=(
            "Evidence-only haystack: gold sessions == all haystack sessions for "
            "500/500 instances. Cannot measure retrieval. Useful as a fast "
            "smoke corpus for the ingest and QA paths only."
        ),
    ),
    "longmemeval_s": DatasetSpec(
        key="longmemeval_s",
        dataset="longmemeval",
        variant="s_cleaned",
        url=(
            "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/"
            f"{_LME_HF_REVISION}/longmemeval_s_cleaned.json"
        ),
        filename="longmemeval_s_cleaned.json",
        approx_bytes=277_380_000,
        sha256=None,
        measures_retrieval=True,
        note=(
            "The smallest LongMemEval variant with distractors (~40 sessions / "
            "~115k tokens per instance). This is the retrieval-measuring corpus. "
            "Official metric is an LLM judge with five prompts selected by "
            "question_type plus a sixth for abstention."
        ),
    ),
}


def cache_dir() -> Path:
    """Where corpora land. Outside the repo and outside the config dir.

    Kept off ``KIROCREW_HOME`` deliberately: these are hundreds of megabytes of
    reproducible third-party data, and sweeping them into the directory that
    ``kirocrew snapshot`` backs up would bloat every snapshot with bytes that a
    URL already describes.
    """
    override = os.environ.get("KIROCREW_BENCH_CACHE")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "kirocrew" / "bench-data"


def _sha256_file(path: Path) -> str:
    """Hash the corpus file, refusing to follow a final symlink.

    Hashing does not disclose the bytes, so this is the mildest of the four, but a
    swap after the guard would have us verify a different file than the one we
    accepted -- and then treat the mismatch as upstream drift.
    """
    import errno

    h = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise CorpusFetchError(
                f"refusing to hash {path.name!r}: it is a symbolic link."
            ) from exc
        raise
    with os.fdopen(fd, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


class CorpusFetchError(RuntimeError):
    """Raised with an actionable message; never with a bare urllib traceback."""


def _download(url: str, dest: Path) -> None:
    """Stream to a temp sibling, then rename.

    Renaming last means an interrupted download can never be mistaken for a
    complete one on the next run — the alternative (writing ``dest`` directly)
    leaves a short file that passes an existence check and fails a JSON parse
    with a confusing error much later.

    The scheme is validated before the request rather than trusted from the spec.
    ``SPECS`` only ever holds ``https://`` literals, but :func:`ensure` accepts a
    caller-supplied :class:`DatasetSpec`, and ``urlopen`` honours ``file://`` — so
    without this check a spec carrying ``file:///etc/passwd`` would read a local
    file and hand it back as a "corpus". Same guard, same reasoning as
    ``embeddings._resolve_model_url``, which rejects non-https model-URL overrides.
    """
    if not url.lower().startswith("https://"):
        raise CorpusFetchError(
            f"refusing to fetch {url!r}: only https:// is allowed.\n"
            "urlopen also honours file:// and ftp://, so a spec with a non-https "
            "URL could read a local file and present it as benchmark data."
        )
    # Derived from a guarded `dest`, but a separate final component: a link planted
    # at `<name>.part` would redirect this write. Guarded and opened no-follow.
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        # The rule's concern is a dynamic value reaching urlopen with a scheme it
        # did not choose. The https-only check above runs on this exact `url`
        # immediately before the Request is built, so no other scheme can arrive
        # here; `req` cannot smuggle one either, since Request does not rewrite
        # the scheme. Marker must sit on the statement line -- semgrep only reads
        # the finding's own line or the one directly above it.
        fd = open_write_nofollow(tmp, what="corpus download staging file")
        with urllib.request.urlopen(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            req, timeout=120
        ) as resp, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(resp, out, _CHUNK)
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise CorpusFetchError(
            f"HTTP {exc.code} fetching {url}\n"
            "Both corpora are ungated and need no token, so a 401/403 here means "
            "the upstream revision moved or the host is blocking this network, "
            "not that credentials are missing."
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        tmp.unlink(missing_ok=True)
        raise CorpusFetchError(f"network error fetching {url}: {exc}") from exc
    tmp.replace(dest)


def ensure(spec: DatasetSpec | str, *, allow_download: bool = True) -> Path:
    """Return a local path to *spec*'s file, downloading and verifying if needed.

    Verification is the whole point of this function, so it runs on cache hits
    too: a corpus file that was truncated by a full disk after a successful
    download would otherwise be trusted forever.
    """
    if isinstance(spec, str):
        try:
            spec = SPECS[spec]
        except KeyError:
            raise CorpusFetchError(
                f"unknown corpus {spec!r}; known: {', '.join(sorted(SPECS))}"
            ) from None

    # KIROCREW_BENCH_CACHE can point this anywhere, so it gets the same treatment
    # as an argv path: a cache root of ~/.kiro/crew would drop corpus files and
    # sidecars into the governance trust root.
    root = guard_output_dir(cache_dir(), what="corpus cache directory")
    dest = guard_write_path(root / spec.filename, what="corpus file")
    root.mkdir(parents=True, exist_ok=True)

    if not dest.exists():
        if not allow_download:
            raise CorpusFetchError(
                f"{spec.key} is not cached at {dest} and downloading is disabled.\n"
                f"Fetch it with: kirocrew bench fetch {spec.key}   "
                f"(~{spec.approx_bytes / 1e6:.0f} MB)"
            )
        _download(spec.url, dest)

    actual = _sha256_file(dest)
    expected = spec.sha256
    side = _sidecar(dest)

    if expected is None:
        # The sidecar is the sharpest case: its contents become `expected`, which the
        # mismatch message below prints. Read through a planted link and a credential
        # file is echoed to stdout as a "checksum".
        if side.exists():
            expected = read_text_nofollow(side, what="checksum sidecar").strip()
        else:
            with os.fdopen(
                open_write_nofollow(side, what="checksum sidecar"), "w", encoding="utf-8"
            ) as fh:
                fh.write(actual + "\n")
            return dest

    if actual != expected:
        raise CorpusFetchError(
            f"checksum mismatch for {dest}\n"
            f"  expected {expected}\n"
            f"  actual   {actual}\n"
            "Delete the file to re-download. If a fresh download still "
            "mismatches, upstream changed the pinned revision's content and the "
            "hardcoded hash in datasets.py must be re-derived deliberately — "
            "silently accepting the new bytes would invalidate every stored "
            "baseline without anyone noticing."
        )
    return dest


def describe() -> str:
    """Human-readable inventory, including what is already cached."""
    root = cache_dir()
    lines = [f"cache: {root}", ""]
    for spec in SPECS.values():
        path = root / spec.filename
        state = "cached" if path.exists() else "not fetched"
        retr = "yes" if spec.measures_retrieval else "NO — evidence-only haystack"
        lines += [
            f"{spec.key}",
            f"  dataset          {spec.dataset} / {spec.variant}",
            f"  size             ~{spec.approx_bytes / 1e6:.1f} MB   ({state})",
            f"  integrity        {spec.integrity}",
            f"  measures recall  {retr}",
            f"  note             {spec.note}",
            "",
        ]
    return "\n".join(lines)


def load_json(spec: DatasetSpec | str, *, allow_download: bool = True) -> object:
    path = ensure(spec, allow_download=allow_download)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
