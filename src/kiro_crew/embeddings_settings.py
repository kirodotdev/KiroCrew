"""The embedding runtime's settings.

The model, runtime and download constants, the scheduling priorities with the
per-call work deadline (``EmbeddingWork``), and the ``memory`` config knobs that
set idle eviction, the inference thread counts and the bulk duty cycle.

:mod:`kiro_crew.embeddings` stays the import path and the patch surface for every
name here; that module's comment lists the owners.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from kiro_crew.config.loader import config_path, read_config_text
from kiro_crew.cpu_affinity import affinity_cpu_count

logger = logging.getLogger("kiro_crew.embeddings")


@dataclass
class EmbeddingWork:
    """A caller's monotonic deadline and cancellation, carried into native jobs.

    Queued inference is cancellable. A running native call retains its executor
    worker and admission until completion because native inference cannot stop.
    """

    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)
    priority: int = 2

    def expired(self) -> bool:
        return self.cancelled.is_set() or time.monotonic() >= self.deadline


embedding_work: ContextVar[EmbeddingWork | None] = ContextVar("embedding_work", default=None)
_EMBED_WAIT_SECS = 30.0


# ── Model constants ──

_GGUF_FILENAME = "qwen3-embedding-0.6b.gguf"
# Anything smaller than this is a truncated/placeholder file, not model weights.
_GGUF_MIN_BYTES = 1_000_000
# Stable model identifier: names the vector space, feeds the knowledge
# library's embed_signature staleness detection. Changing the model (or dim)
# invalidates stored vectors → triggers a re-embed/reindex.
_MODEL_ID = "qwen3-embedding:0.6b"
# sha256 of the published Qwen3-Embedding-0.6B Q8_0 GGUF. The trust anchor for
# every download source. Bump in lockstep when the published model is updated.
_GGUF_SHA256 = "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"
_DEFAULT_DIM = 1024  # Qwen3-Embedding-0.6B output dimension
_MODELS_DIR_NAME = "models"
# Stand-in path for a REJECTED custom model. Never created, so it can never
# satisfy model_file_present() and the embedder can never load — which is the
# point: a rejected spec must not carry the real (possibly protected) path.
_REJECTED_MODEL_SENTINEL = ".rejected-custom-model.invalid"

# ── Runtime constants ──

# Qwen3-Embedding requires last-token pooling (LLAMA_POOLING_TYPE_LAST), and it
# stays the pooling of every model whose id names no other (see
# model_pooling_type): the bundled model, and a custom model recorded before its
# file's declared pooling was honoured. The value is always passed explicitly.
_POOLING_TYPE_LAST = 3
# llama.cpp's `enum llama_pooling_type` values for the poolings a model can ask for.
_POOLING_TYPES = {"mean": 1, "cls": 2, "last": _POOLING_TYPE_LAST}
# Architectures the vendored llama.cpp runs WITHOUT a KV cache (the `res =
# nullptr` cases of llama_model::create_memory in src/llama-model.cpp, plus the
# t5encoder encoder-only path). llama_decode() routes them through encode(),
# which aborts unless one physical micro-batch holds every input token, whatever
# the GGUF says about causality. Keep this list a verbatim mirror of that switch
# whenever the vendored runtime is bumped: test/test_encoder_architecture_mirror.py
# requires every name here to be spelled by every vendored libllama binary, so a
# removed or renamed architecture fails the test unless its name is the suffix
# of another listed name (`bert` inside `modern-bert`: GNU ld tail-merges the
# strings, so only the terminating NUL can be required and the longer name still
# satisfies the check), while an ADDED cache-less architecture is invisible to it
# and must be re-mirrored by hand (see the bump procedure in _vendor/README.md).
# Membership decides only the micro-batch shape (see _model_context_policy);
# pooling comes from the model's id (see model_pooling_type).
# Non-causal models that DO keep a KV cache (llama-embed, or any decoder family
# re-tagged bidirectional) are not named here: they declare
# `<architecture>.attention.causal = false` and _model_context_policy() reads
# that key directly.
_ENCODER_ARCHITECTURES = frozenset(
    {
        "bert",
        "dream",
        "eurobert",
        "gemma-embedding",
        "jina-bert-v2",
        "jina-bert-v3",
        "llada",
        "llada-moe",
        "modern-bert",
        "neo-bert",
        "nomic-bert",
        "nomic-bert-moe",
        "rnd1",
        "t5encoder",
        "wavtokenizer-dec",
    }
)
# Context window for the embedding pass. Episodic memories are capped at
# 2000 chars and knowledge chunks are bounded by the chunker (~512 tokens +
# overlap, ≈5.8k chars max), so 2048 tokens covers both. Kept deliberately
# small: KV-cache size scales linearly with n_ctx (~115KB/token for this
# model) and the embedder may load in more than one process (gateway +
# kirocrew-core MCP server — the GGUF weights themselves are mmap'd and
# physically shared, the KV buffers are not). The logical batch still covers
# the complete input for last-token pooling; llama.cpp may split that work into
# smaller physical micro-batches without changing the resulting vector. This is
# the ceiling: a model trained for fewer positions is sized to its own count
# instead (see _model_context_policy).
_N_CTX = 2048
# Physical decode micro-batch. Keeping this below the logical batch bounds the
# compute scratch arena and llama.cpp's per-token output buffer (one row per
# micro-batch token, 610,772 bytes each for the shipped model) without reducing
# the accepted context. On the shipped Qwen3 file and the vendored Linux CPU
# runtime, 256, 512 and 2048 produce byte-identical vectors under last-token
# pooling; 256 holds 317 MiB less anonymous memory than 512 once a long input
# has run, at the same embed latency.
_N_UBATCH = 256
# Safety truncation (chars) before inference, sized under _N_CTX at a
# conservative ~4 chars/token so a clipped input always fits the context
# window. Only pathological un-chunked blobs exceed this; mirrors the
# knowledge embedder's content-budget backstop. An input that still exceeds
# the logical batch after clipping (dense CJK/code, or a model sized below
# _N_CTX) is cut to its first n_batch tokens by the vendored binding's
# create_embedding() -> embed(truncate=True) path.
_MAX_EMBED_CHARS = 6_000
_LLM_LOAD_RETRY_SECS = 300.0  # re-attempt a failed model load after this long
# How long close() waits for the inference thread to finish its current job and
# exit. Bounded so an unload never wedges a shutdown; the thread is a daemon, so
# a straggler cannot hold the interpreter open either.
_INFER_STOP_TIMEOUT_SECS = 30.0
# Idle eviction. The loaded model is the largest single cost of an idle gateway:
# the GGUF mapping (~600 MB resident) plus llama.cpp's context buffers (~400 MB),
# held for the process lifetime once anything embedded. After this many seconds
# with no embed the model is unloaded; the next embed loads it again. 0 keeps it
# resident forever. Overridable as memory.embedding_idle_evict_secs.
_DEFAULT_IDLE_EVICT_SECS = 3600.0
# Longest the evictor sleeps between checks, so a config change is seen promptly.
_EVICT_POLL_MAX_SECS = 60.0
# How long a caller that can wait (an interactive or normal embed, off the event
# loop) blocks for a load it triggered, instead of getting None and falling back
# to keyword search. Bounded again by the caller's own EmbeddingWork deadline.
_COLD_LOAD_WAIT_SECS = 30.0
# llama.cpp sizes its compute pools from the HOST CPU COUNT when the caller does
# not pass them: n_threads = cpu//2 and n_threads_batch = cpu (see the vendored
# llama.py). Embedding is prompt processing, so it runs on the BATCH pool — on a
# 16-core host EVERY embed, even an 8-character one, fanned out across all 16
# cores and measured ~4.5 cores sustained inside the gateway. A 0.6B model over
# short text does not need that, and oversubscribing the box makes the pool both
# suffer and cause contention. Pinned to the established four-thread default
# here, overridable via memory.embedding_threads.
_DEFAULT_EMBED_THREADS = 4
# One shared model and worker serve every store within this queue budget.
_MAX_PENDING_EMBEDS = 8
_INTERACTIVE_QUEUE_RESERVE = 2
_MAX_EMBED_BATCH_TEXTS = 8
# Bulk corpus loops (the post-migration re-embed sweep above all) run for as long
# as the corpus takes: measured 429 ms/row at 4 threads on ~500-character rows,
# so a 3,000-row migrated memory is ~21 minutes at a SUSTAINED 3.7 cores. That is
# indistinguishable from a runaway process to the user — laptop fans spin up and
# stay up — even though every row is legitimate work.
#
# Nobody is waiting on that work. A row with a NULL embedding is still FTS5
# keyword-searchable the moment it lands; the sweep only adds SEMANTIC reach over
# memories the user imported from a previous install. So the sweep is optimized
# for staying invisible, not for finishing early, and the defaults below are
# deliberately slow: one thread at a 20% duty cycle is ~0.2 of a core, which no
# fan reacts to. On the measured host that is ~7 s/row, so a 3,000-row backlog
# takes hours — and that is fine. It is idempotent and resumes across restarts
# (an unfinished row stays NULL and the next boot picks it up), so a machine that
# is never on long enough to finish still converges over several sessions.
#
# Two independent dials for a deployment that wants it faster (a server, or a
# user who wants semantic search over old memories today):
#
#   * ``memory.embedding_bulk_threads`` — threads for BULK jobs only, so raising
#     it never slows a query the user is waiting on. 0 means "inherit
#     ``embedding_threads``".
#   * ``memory.embedding_bulk_duty`` — the fraction of wall time the loop may
#     spend computing. 1.0 runs flat out.
#
# Total CPU work is unchanged either way (measured 1.39–1.57 CPU-seconds per row
# across 1/2/4 threads); what changes is how thinly it is spread. Fans respond to
# sustained load, so spreading it is the whole point. The one cost of spreading
# is that the ~700MB model stays resident while the sweep runs, which is why the
# sweep still probes for pending rows BEFORE loading anything.
_DEFAULT_BULK_THREADS = 1
_DEFAULT_BULK_DUTY = 0.2
# Floor on the duty cycle. A typo of 0.001 would otherwise turn a multi-hour
# sweep into a multi-week one, which is indistinguishable from it never running.
_MIN_BULK_DUTY = 0.05
# Cap on a single pace sleep. This exists ONLY so one pathological row (a
# 6,000-character blob whose inference takes seconds) cannot park the sweep for
# minutes; it must stay well ABOVE the delay an ordinary row produces at the
# default duty (~5.6s at 1.39s/row and 0.2), or it would quietly override the
# configured duty on EVERY row instead of catching the outlier it is named for.
_MAX_BULK_PACE_SLEEP = 30.0
# Only log an embed's queue wait at INFO once it is long enough for a waiting
# caller to notice; below this it stays DEBUG so ordinary memory writes do not
# emit a line each.
_EMBED_WAIT_LOG_MS = 250.0
# Scheduling classes for the shared inference queue. The whole process shares ONE
# model on ONE thread, so a bulk corpus sweep and a user's query compete for the
# same single slot: without ordering, a short interactive embed waits behind
# however much background work happens to be queued. Lower value wins.
PRIORITY_INTERACTIVE = 0  # a human is blocked on this (prompt build, search box)
PRIORITY_NORMAL = 1  # bounded explicit write (one lesson, one preference)
PRIORITY_BULK = 2  # corpus loops: backfill, migration, ingestion, consolidation
# Shutdown outranks everything so close() is not stuck behind a queued sweep.
_PRIORITY_SENTINEL = -1


def _work_for_priority(priority: int) -> EmbeddingWork:
    """Only explicit deadlines expire bulk work waiting on the shared duty cycle."""
    return embedding_work.get() or EmbeddingWork(
        math.inf if priority >= PRIORITY_BULK else time.monotonic() + _EMBED_WAIT_SECS
    )


# ── Download constants ──

_DOWNLOAD_MAX_ATTEMPTS = 6  # background startup task (long backoff, may span hours)
DOWNLOAD_ATTEMPTS_INTERACTIVE = 3  # dashboard Enable/Retry click (fast feedback)
_DOWNLOAD_BACKOFF_BASE_SECS = 60.0
_DOWNLOAD_BACKOFF_CAP_SECS = 1800.0
# Escape hatch for tests/CI: never kick a 610MB model download from a test run.
_SKIP_DOWNLOAD_ENV = "KIROCREW_SKIP_MODEL_DOWNLOAD"
# Distribution: public CloudFront CDN in front of Kiro Crew's OWN model bucket
# (kirocrew-models, reachable only through the distribution's OAC — the bucket
# itself blocks public access). Serving our own copy rather than sharing another
# project's bucket keeps the download signal attributable: a fetch here is a
# Kiro Crew first-install, uncontaminated by another product's traffic.
# Plain HTTPS, no git access and no cloud SDK required. The
# sha256 pin above is the trust anchor for every source, so a tampered CDN
# object can only fail verification. Resolution order: KIROCREW_EMBED_MODEL_URL
# env, then the memory.embed_model_url config knob, then this default. The URL
# basename (qwen3-embedding-0.6b.gguf) intentionally differs from the on-disk
# _GGUF_FILENAME — the sha pin, not the name, is the integrity gate.
_MODEL_URL_ENV = "KIROCREW_EMBED_MODEL_URL"
# Custom-model escape hatch: an absolute path to a user-supplied GGUF. When set
# (env, or the memory.embed_model_path config knob) Kiro Crew runs THAT model and
# never downloads or installs the bundled default — including when the default's
# pinned version changes. No sha256 pin is applied: the pin is the trust anchor
# for a NETWORK download, whereas a local file is trusted by provenance (the
# user put it there). Note a GGUF is parsed by native llama.cpp code, so this
# knob is deliberately config-file-only — it is NOT in the dashboard's
# _EDITABLE_CONFIG allowlist, so no API caller and no agent can point the
# embedder at an arbitrary file.
_MODEL_PATH_ENV = "KIROCREW_EMBED_MODEL_PATH"
_DEFAULT_MODEL_URL = "https://d3j0sthz5doyui.cloudfront.net/models/qwen3-embedding-0.6b.gguf"
_HTTP_TIMEOUT_SECS = 1800  # 610MB at >=340KB/s; slower links retry with backoff
_HTTP_CHUNK_BYTES = 1 << 20
# Reported by the shared transfer engine every ~16MB so the status endpoint can
# report byte-level progress; the dashboard renders a determinate progress bar
# from it.
_PROGRESS_EVERY_BYTES = 16 << 20


def _read_memory_config() -> dict:
    """Read the raw ``memory`` section of ``config.json``. Never raises.

    Read raw rather than through the config loader so the download thread and
    the backend factory never depend on the config dataclass import graph
    (which imports large parts of the package).
    """
    try:
        path = config_path()
        if path.exists():
            data = json.loads(read_config_text(path))
            section = data.get("memory", {})
            if isinstance(section, dict):
                return section
    except Exception:
        logger.debug("Could not read the memory config section", exc_info=True)
    return {}


def embedding_idle_evict_secs() -> float:
    """Seconds without an embed before the model is unloaded; 0 means never.

    Raw ``memory`` section, like the thread counts: read by the evictor thread,
    which must not pull in the config dataclass graph. A missing, negative or
    non-numeric value is the default.
    """
    raw = _read_memory_config().get("embedding_idle_evict_secs")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return float(raw)
    return _DEFAULT_IDLE_EVICT_SECS


def _embed_threads() -> int:
    """Thread count for llama.cpp's embedding compute pools.

    Read from the RAW ``memory`` config section for the same reason the rest of
    this module does: the download thread and the backend factory must not pull
    in the full config dataclass import graph.

    The count comes from :func:`kiro_crew.cpu_affinity.affinity_cpu_count`, not
    ``os.cpu_count``: under a CPU-set restriction (``--cpuset-cpus``, ``taskset``)
    the latter reports the host's cores, so the cap below would compute from 64 on
    a 2-core allowance and answer four threads where two is the whole allowance.

    An operator value OTHER than the declared :data:`_DEFAULT_EMBED_THREADS` is
    honoured up to that count. Default policy caps the default one core BELOW it
    instead, so a 2-vCPU host keeps a core for the event loop rather than handing
    llama.cpp the whole box. It is a ceiling on the default, not a replacement
    for it: a 16-core host still answers 4.

    A raw value EQUAL to the default is default policy, not operator intent.
    ``MemoryConfig.embedding_threads`` is a dataclass field defaulting to 4 and
    ``KiroCrewConfig.save()`` publishes every field, so a fresh install's
    ``config.json`` carries a 4 nobody typed; reading that as a choice would
    hand the whole box to exactly the hosts this cap protects. The cost is that
    4 cannot be pinned where the process may use 4 or fewer CPUs -- any other
    number can.
    """
    raw = _read_memory_config().get("embedding_threads")
    cores = affinity_cpu_count()
    requested = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0
    if requested and requested != _DEFAULT_EMBED_THREADS:
        return max(1, min(requested, cores or _DEFAULT_EMBED_THREADS))
    if cores is None:
        return _DEFAULT_EMBED_THREADS
    return max(1, min(_DEFAULT_EMBED_THREADS, cores - 1))


def bulk_embed_threads() -> int:
    """Thread count for ``PRIORITY_BULK`` inference, from ``memory``.

    Defaults to :data:`_DEFAULT_BULK_THREADS` — one thread, because nothing is
    waiting on bulk work and a single thread is what keeps it off the fans. An
    explicit 0 means "inherit :func:`_embed_threads`", which is how a deployment
    opts back into the interactive pool for its sweeps. A value above the
    interactive count is honoured but still clamped to the CPUs this process
    may run on, the same count :func:`_embed_threads` reads.
    """
    raw = _read_memory_config().get("embedding_bulk_threads")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raw = _DEFAULT_BULK_THREADS
    elif raw == 0:
        return _embed_threads()
    return max(1, min(raw, affinity_cpu_count() or _DEFAULT_EMBED_THREADS))


def bulk_duty_cycle() -> float:
    """Fraction of wall time a bulk corpus loop targets for inference.

    A target rather than a ceiling: :func:`bulk_pace_delay` caps one pause at
    :data:`_MAX_BULK_PACE_SLEEP`, so a row slow enough to ask for more idle than
    that runs at a higher effective duty than configured.

    1.0 disables pacing (the pre-existing behaviour). Anything else is clamped to
    ``[_MIN_BULK_DUTY, 1.0]``, so a typo cannot stretch a sweep to the point
    where it looks stalled. Bools are rejected before ``isinstance(x, int)`` can
    accept ``True`` as 1.

    Non-finite values are rejected explicitly rather than left to the clamp. This
    reads the RAW config section — the loader's ``_safe_float`` guard is NOT in
    the path — and ``json.load`` accepts the ``NaN`` literal, which compares
    false against every bound and would slip through ``max()`` unchanged.

    ``float()`` itself can raise: ``json.load`` yields arbitrary-precision ints,
    and one wider than a double (a 309-digit integer) raises ``OverflowError``.
    That would propagate out through ``bulk_pace_delay`` into the sweep and abort
    it, leaving every pending row NULL — a config typo must degrade to the
    default, never stop the sweep.
    """
    raw = _read_memory_config().get("embedding_bulk_duty")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raw = _DEFAULT_BULK_DUTY
    try:
        duty = float(raw)
    except (OverflowError, ValueError):
        duty = _DEFAULT_BULK_DUTY
    if not math.isfinite(duty):
        duty = _DEFAULT_BULK_DUTY
    if duty >= 1.0:
        return 1.0
    return max(_MIN_BULK_DUTY, duty)


def bulk_pace_delay(elapsed: float) -> float:
    """Seconds a bulk loop should idle after a unit of work taking *elapsed*.

    Duty *d* means work occupies ``d`` of the cycle, so the idle share is
    ``elapsed * (1 - d) / d`` — at ``d = 0.5`` the loop sleeps for exactly as
    long as it worked. Returns 0.0 when pacing is off, and never returns more
    than :data:`_MAX_BULK_PACE_SLEEP`.

    Corpus callers sleep without a model lock. The shared inference worker also
    applies this delay between bulk jobs, with an interruptible wait so an
    interactive query can wake it immediately. Independent stores therefore
    share one duty cycle rather than keeping the worker busy during each other's
    pauses.
    """
    if elapsed <= 0:
        return 0.0
    duty = bulk_duty_cycle()
    if duty >= 1.0:
        return 0.0
    return min(elapsed * (1.0 - duty) / duty, _MAX_BULK_PACE_SLEEP)
