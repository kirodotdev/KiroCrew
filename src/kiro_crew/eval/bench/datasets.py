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

from .errors import BenchRefusal
from .safepath import (
    _refuse_hardlink_alias,
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
    """Hash the corpus file, refusing a symlink or a hardlink alias.

    Two distinct refusals, for two distinct reasons:

    * ``O_NOFOLLOW`` -- a swap after the guard would have us verify a different file
      than the one we accepted, and then report the mismatch as upstream drift.
    * ``st_nlink > 1`` -- a hardlink shares its target's inode, so no path check can
      see it. Hashing does not disclose the bytes, but it does publish the target's
      SHA-256 as a corpus checksum, and a digest of a credential file is a leak. This
      is the THIRD site with its own ``os.open``; the write helper and the read helper
      got the same refusal in an earlier round, and routing through the shared helper
      is what keeps a fourth site from being written without it.
    """
    import errno

    h = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        _refuse_hardlink_alias(fd, what="corpus file", name=Path(path).name)
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


class CorpusFetchError(BenchRefusal):
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
    #
    # The pid is in the name because the staging file is created EXCLUSIVELY: a
    # `.part` left behind by a killed download would otherwise make every later run
    # refuse, and "delete this file to fetch a corpus" is a bad answer to a crash.
    # Two concurrent fetches also stop fighting over one name.
    tmp = dest.with_suffix(f"{dest.suffix}.{os.getpid()}.part")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        # Order matters here, and it is the whole fix for a real cross-platform bug.
        # The response is acquired FIRST, and the staging fd only after it. Opening
        # the fd first meant a failing `urlopen` left the raw descriptor unowned --
        # `os.fdopen(fd)` never ran, because evaluating the `with` header is what
        # raised -- so nothing ever closed it. On Windows the surviving handle then
        # made `tmp.unlink()` in the handlers below raise PermissionError [WinError
        # 32], and the CorpusFetchError those handlers exist to raise never arrived.
        #
        # The rule's concern is a dynamic value reaching urlopen with a scheme it
        # did not choose. The https-only check above runs on this exact `url`
        # immediately before the Request is built, so no other scheme can arrive
        # here; `req` cannot smuggle one either, since Request does not rewrite
        # the scheme. Marker must sit on the statement line -- semgrep only reads
        # the finding's own line or the one directly above it.
        with urllib.request.urlopen(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            req, timeout=120
        ) as resp:
            fd = open_write_nofollow(tmp, what="corpus download staging file")
            with os.fdopen(fd, "wb") as out:
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
    # Not `path.open()`: the corpus file is the largest caller-influenced read in
    # this package, and a plain open leaves the check-to-use window that the
    # sidecar and staging-file reads already close. Same helper, same reason.
    return json.loads(read_text_nofollow(path, what="corpus file"))


def load_text(spec: DatasetSpec, *, allow_download: bool = True) -> str:
    """A fetched file's text (CSV, JSONL), through the same verified, no-follow read."""
    path = ensure(spec, allow_download=allow_download)
    return read_text_nofollow(path, what="corpus file")


# ── Provider benchmark suites ────────────────────────────────────────────────
#
# Fetched on demand from immutable upstream revisions, never vendored. A file small
# enough to fetch while this was written carries its SHA-256; the rest pin on first
# fetch. Kept apart from SPECS, which `kirocrew bench fetch` lists.

_BEAM_COMMIT = "b2da22eac88bb0874c64665f13457eb99835774a"
_PRECISIONMEM_COMMIT = "61a712a1754e7d27b6dee42da32a2c214b003ece"
_PERSONAMEM_HF_REVISION = "ed956dea41521fc4499acbc63f966e0fd3c053ba"
_MAB_HF_REVISION = "7ea066982b140a19337e17e60d45d4076e042faf"
_HALUMEM_HF_REVISION = "cb04336aa1b732d4b24f5186c552456b4099806e"

SUITE_SPECS: dict[str, DatasetSpec] = {
    "precisionmem_seed": DatasetSpec(
        key="precisionmem_seed",
        dataset="precisionmembench",
        variant="seed",
        url=(
            "https://raw.githubusercontent.com/tenurehq/precisionmembench/"
            f"{_PRECISIONMEM_COMMIT}/fixtures/beliefs.seed.json"
        ),
        filename="precisionmem_beliefs.seed.json",
        approx_bytes=33_303,
        sha256="35659b3b9a581b644fc6746e1b4e902c3e7b56c0973b546bf7b0816a69e3de98",
        measures_retrieval=True,
        note="35 seed beliefs every case retrieves from. MIT.",
    ),
    "precisionmem_cases": DatasetSpec(
        key="precisionmem_cases",
        dataset="precisionmembench",
        variant="retrieval",
        url=(
            "https://raw.githubusercontent.com/tenurehq/precisionmembench/"
            f"{_PRECISIONMEM_COMMIT}/fixtures/retrieval.cases.json"
        ),
        filename="precisionmem_retrieval.cases.json",
        approx_bytes=35_940,
        sha256="09e99f0852280538abe34b03570f65583fdb10df3c554b7150f0f665d5db709c",
        measures_retrieval=True,
        note="77 must-return and must-not-return cases. MIT.",
    ),
    "personamem_v2_text": DatasetSpec(
        key="personamem_v2_text",
        dataset="personamem_v2",
        variant="text",
        url=(
            "https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2/resolve/"
            f"{_PERSONAMEM_HF_REVISION}/benchmark/text/benchmark.csv"
        ),
        filename="personamem_v2_benchmark_text.csv",
        approx_bytes=42_426_457,
        sha256="95f2a8a324aab7baf2af937feae12731369e2abf7cad5ab3e170594cb25a3e52",
        measures_retrieval=False,
        note="5,000 multiple choice personalization questions. CC BY 4.0.",
    ),
    "mab_conflict_resolution": DatasetSpec(
        key="mab_conflict_resolution",
        dataset="memoryagentbench",
        variant="conflict_resolution",
        url=(
            "https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/resolve/"
            f"{_MAB_HF_REVISION}/data/Conflict_Resolution-00000-of-00001.parquet"
        ),
        filename="mab_conflict_resolution.parquet",
        approx_bytes=0,
        sha256=None,
        measures_retrieval=False,
        note="FactConsolidation single and multi hop. MIT; reading it needs pyarrow.",
    ),
    "mab_accurate_retrieval": DatasetSpec(
        key="mab_accurate_retrieval",
        dataset="memoryagentbench",
        variant="accurate_retrieval",
        url=(
            "https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/resolve/"
            f"{_MAB_HF_REVISION}/data/Accurate_Retrieval-00000-of-00001.parquet"
        ),
        filename="mab_accurate_retrieval.parquet",
        approx_bytes=0,
        sha256=None,
        measures_retrieval=False,
        note="EventQA rows of Accurate Retrieval. MIT; reading it needs pyarrow.",
    ),
    "halumem_medium": DatasetSpec(
        key="halumem_medium",
        dataset="halumem",
        variant="medium",
        url=(
            "https://huggingface.co/datasets/IAAR-Shanghai/HaluMem/resolve/"
            f"{_HALUMEM_HF_REVISION}/HaluMem-Medium.jsonl"
        ),
        filename="halumem_medium.jsonl",
        approx_bytes=0,
        sha256=None,
        measures_retrieval=False,
        note=(
            "CC BY-NC-ND 4.0: converted in memory at run time on this machine, never "
            "written back out in another form."
        ),
    ),
}


def beam_spec(split: str, conversation: int, part: str) -> DatasetSpec:
    """One BEAM conversation file: ``chat`` or ``questions``, pinned on first fetch."""
    if split not in ("100K", "500K") or part not in ("chat", "questions"):
        raise CorpusFetchError(f"unknown BEAM file {split}/{conversation}/{part}")
    path = "chat.json" if part == "chat" else "probing_questions/probing_questions.json"
    return DatasetSpec(
        key=f"beam_{split}_{conversation}_{part}",
        dataset="beam",
        variant=split,
        url=(
            "https://raw.githubusercontent.com/mohammadtavakoli78/BEAM/"
            f"{_BEAM_COMMIT}/chats/{split}/{int(conversation)}/{path}"
        ),
        filename=f"beam_{split}_{int(conversation)}_{part}.json",
        approx_bytes=0,
        sha256=None,
        measures_retrieval=True,
        note="BEAM data CC BY-SA 4.0, code MIT.",
    )


def personamem_history_spec(link: str) -> DatasetSpec:
    """One PersonaMem v2 chat history the benchmark CSV links to (``data/...json``)."""
    if not link.startswith("data/chat_history_32k/") or ".." in link or not link.endswith(".json"):
        raise CorpusFetchError(f"unexpected PersonaMem history path {link!r}")
    name = link.rsplit("/", 1)[-1]
    return DatasetSpec(
        key=f"personamem_v2_{name}",
        dataset="personamem_v2",
        variant="32k",
        url=(
            "https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2/resolve/"
            f"{_PERSONAMEM_HF_REVISION}/{link}"
        ),
        filename=f"personamem_v2_{name}",
        approx_bytes=0,
        sha256=None,
        measures_retrieval=False,
        note="CC BY 4.0.",
    )
