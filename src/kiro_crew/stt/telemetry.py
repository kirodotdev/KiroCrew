"""Stage timings for local speech recognition, with no audio and no transcript.

**Why this is its own module.** Before it, "voice input is slow" was unanswerable:
the one-line log said a model had loaded and a decode had run, and every question a
user or a maintainer actually asks -- was that the digest check or the native load,
was the decode slower than real time, did the final queue behind a partial nobody
would read -- had no recorded answer. Measuring it by hand needed a checkout, a
debugger and the model already downloaded, which is not available to the person
reporting the problem.

**The privacy rule is the design constraint, not a caveat.** Nothing here holds
audio, a transcript, a file path, a hostname or a user identifier. The fields are
durations, sample counts, and values the user themselves chose in settings (model
name, language code, backend). That is what makes it safe to put on
``GET /api/stt/status`` and into a pasted bug report, and it is why the recording
API takes a duration and a kind rather than the objects a decode works with -- a
function that never receives the text cannot leak it.

**What the first round of these measurements established**, recorded here because it
is a property of whisper.cpp that a future reader will otherwise re-derive: decode
cost is a large constant plus a small term in the audio length, because every decode
is padded into a fixed analysis window. Measured with ``base`` on a 32-core aarch64
CPU build -- 0.5 s of audio in 0.82 s, 2 s in 0.86 s, 11 s in 1.65 s, about 0.78 s
fixed plus 0.08 s per audio-second. So ``rtf`` for one model on one host spans 1.64
to 0.15 depending only on how much audio it was handed. **Report it, never scale
it**: a cost projection built on that rate over-predicts from a short sample and
under-predicts badly from a long one. ``wall_ms`` is the honest predictor. An
adaptive partial-cadence budget that scaled the rate was written, measured against
this, and removed for exactly that reason.

Bounded by construction: :data:`_HISTORY` samples, a plain deque, overwritten in
place. A gateway that dictates all day holds the same few kilobytes as one that
never does.

Thread-safety: :meth:`Recorder.record_decode` is called from the event loop, while
:meth:`Recorder.record_hash` is called from a worker thread (the digest runs in
``asyncio.to_thread``). One lock covers both, and every read takes a snapshot, so a
status request cannot observe a half-written sample.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Deque

#: How many recent decodes are retained. Enough to see a cadence pattern across one
#: utterance (a 15 s phrase at the default 400 ms interval produces well under this)
#: without holding a session's whole history.
_HISTORY = 32

#: The decode kinds, as a contract: these strings reach the status payload and the
#: diagnostic table, so renaming one changes what an operator's saved comparison
#: means. They map one-to-one onto the paths that spend inference.
KIND_PREWARM = "prewarm"
KIND_PARTIAL = "partial"
KIND_PHRASE_COMMIT = "phrase_commit"
KIND_FINAL = "final"
KIND_BATCH = "batch"


@dataclass(frozen=True)
class DecodeSample:
    """One decode's cost. Numbers and enums only -- never content.

    ``rtf`` is the real-time factor: wall time divided by audio duration. Above 1.0
    the recogniser is slower than the speech it is transcribing, which is the single
    number that decides whether live partials can keep up at all.
    """

    kind: str
    audio_ms: float
    wall_ms: float
    rtf: float
    #: How long the request waited for the single-entry decode lock. Separates "the
    #: model is slow" from "something else was using it", which look identical from
    #: the outside and have opposite remedies.
    queue_wait_ms: float = 0.0
    #: True when the decode was abandoned (superseded partial, stop, timeout). An
    #: aborted decode's wall time is spent work with nothing to show, so it is
    #: counted separately rather than averaged into the honest ones.
    aborted: bool = False
    #: Wall-clock stamp, for ordering only.
    at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class LoadSample:
    """One model load episode, split into the phases that have different causes.

    Split because the remedies differ completely. ``hash_ms`` is disk plus SHA-256
    over the whole file and scales with model size (measured on a 32-core aarch64
    host: 0.11 s for the 148 MB ``base``, 5.48 s for the 1.6 GB
    ``large-v3-turbo``); ``load_ms`` is the native context allocation;
    ``first_decode_ms`` is the graph build that the first decode after a load pays,
    and it is recorded ONLY from a prewarm. That is a deliberate narrowing rather than
    an omission: attributing it to whichever decode happened to be first let an
    ordinary utterance's full cost be filed as a load-stage number, which is a much
    worse answer than a zero. A load with no prewarm therefore reports zero here,
    meaning "not measured", and the decode's own cost is in ``decodes`` where it
    belongs.
    Reporting one total made a 5.5 s digest check look like a slow model.
    """

    model: str
    size_bytes: int
    hash_ms: float = 0.0
    load_ms: float = 0.0
    first_decode_ms: float = 0.0
    at: float = field(default_factory=time.time)


_PRIVATE_FIELDS = frozenset({"at"})


def _public(sample: DecodeSample | LoadSample | None) -> dict[str, Any] | None:
    """One sample as a payload, minus the fields that are the recorder's own.

    A denylist rather than an allowlist on purpose: a field added to either sample
    is a measurement, and a measurement that silently failed to reach the status
    endpoint is the failure mode this whole module exists to prevent. Anything
    genuinely internal is named here and covered by the field-set test.
    """
    if sample is None:
        return None
    return {k: v for k, v in asdict(sample).items() if k not in _PRIVATE_FIELDS}


class Recorder:
    """Process-wide store of the most recent speech-stage timings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._decodes: Deque[DecodeSample] = deque(maxlen=_HISTORY)
        self._load: LoadSample | None = None
        self._pending_hash: tuple[str, float] | None = None
        self._loads = 0
        self._hashes = 0

    def record_hash(self, model: str, wall_ms: float) -> None:
        """Note a digest verification. Called from the worker thread that ran it.

        Held as *pending* rather than published immediately because a hash and the
        load it gates are one episode from the reader's point of view, and the load
        has not happened yet. :meth:`record_load` folds it in.

        Keyed BY MODEL, which the first version was not: it took the argument and
        discarded it, so a hash could be folded into a load of a different model. That
        is reachable rather than theoretical -- ``_verified_on_disk`` records the hash
        BEFORE comparing the digest, so a failed verification leaves the slot
        populated with nothing to consume it, and nothing clears it when no load
        follows. Verify ``large-v3-turbo`` (5.48 s) then load ``base`` and the load
        reported 5480 ms of hashing against a real ~110 ms: the one number this module
        exists to make legible, wrong by 50x, blaming the wrong phase.
        """
        with self._lock:
            self._pending_hash = (model, wall_ms)
            self._hashes += 1

    def record_load(self, model: str, size_bytes: int, wall_ms: float) -> None:
        with self._lock:
            pending = self._pending_hash
            # Only this model's hash. A stale slot from another model's verification
            # is dropped rather than misattributed.
            hash_ms = pending[1] if pending is not None and pending[0] == model else 0.0
            self._load = LoadSample(
                model=model,
                size_bytes=size_bytes,
                hash_ms=hash_ms,
                load_ms=wall_ms,
            )
            self._pending_hash = None
            self._loads += 1

    def record_decode(self, sample: DecodeSample) -> None:
        with self._lock:
            self._decodes.append(sample)
            # The first decode after a load pays the graph allocation, and attributing
            # it to the load is what makes a cold start legible: a user waiting 19 s
            # is waiting on hash + load + graph, not on "a slow model".
            #
            # Restricted to the PREWARM decode, which is the one the load actually
            # causes. Without that restriction any decode could fill the field: a
            # prewarm runs with `superseding=True`, so a boot prewarm racing a
            # pointer-down prewarm aborts, the aborted sample is skipped, and the next
            # non-aborted sample -- possibly a 60 s utterance -- is then published as
            # a load phase. The docstring calls this field a graph build and quotes
            # 30-40 ms for it, so a reader takes it as a constant.
            if (
                self._load is not None
                and self._load.first_decode_ms == 0.0
                and not sample.aborted
                and sample.kind == KIND_PREWARM
            ):
                self._load = LoadSample(
                    model=self._load.model,
                    size_bytes=self._load.size_bytes,
                    hash_ms=self._load.hash_ms,
                    load_ms=self._load.load_ms,
                    first_decode_ms=sample.wall_ms,
                    at=self._load.at,
                )

    def reset(self) -> None:
        """Drop every sample. For tests, and for a backend or model change."""
        with self._lock:
            self._decodes.clear()
            self._load = None
            self._pending_hash = None
            self._loads = 0
            self._hashes = 0

    def snapshot(self) -> dict[str, Any]:
        """A JSON-safe view for the status endpoint and the diagnostic.

        Everything here is a number, a kind, or a model name the user chose.

        ``at`` is deliberately NOT carried. It exists so the recorder can order its
        own samples, and the payload already expresses that ordering by listing them
        in order -- so emitting it would put an absolute wall-clock stamp of when
        someone spoke into a body whose stated contract is durations and counts. A
        field-set test pins this.
        """
        with self._lock:
            decodes = list(self._decodes)
            load = self._load
            loads, hashes = self._loads, self._hashes
        finals = [d for d in decodes if d.kind == KIND_FINAL and not d.aborted]
        partials = [d for d in decodes if d.kind == KIND_PARTIAL]
        return {
            "loads": loads,
            "hashes": hashes,
            "last_load": _public(load),
            "last_final": _public(finals[-1] if finals else None),
            "last_decode": _public(decodes[-1] if decodes else None),
            # Counted rather than listed: the number is what says whether cosmetic
            # inference is being thrown away, and the individual samples are already
            # in `decodes`.
            "partials": len(partials),
            "partials_aborted": sum(1 for d in partials if d.aborted),
            "decodes": [_public(d) for d in decodes],
        }


_recorder = Recorder()


def recorder() -> Recorder:
    """The process-wide recorder.

    A singleton for the same reason the engine is one: the thing being measured is
    one resident model shared by every session, so per-session records would each
    hold a fragment of one story.
    """
    return _recorder
