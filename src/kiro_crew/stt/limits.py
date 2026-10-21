"""The speech-to-text limits that configuration can set, and their bounds.

Split out from the modules that enforce them for one reason: this module imports
nothing. ``vad``, ``session`` and ``engine`` all import numpy, and
``config.loader`` needs these numbers at class-definition time to build
``SttConfig``'s field defaults. Reaching them through those modules would put
numpy on the import path of ``kiro_crew.cli``, which ``test_cli_lazy_imports``
correctly refuses: a ``kirocrew --help`` has no business loading an array
library.

So the values live here and the enforcing modules import them, rather than the
other way round. A floor stated in two places drifts, and the shape it drifts
into is the worst one available: the loader stores a value the recogniser then
clamps, so the setting a user reads back is not the setting in force.

Limits with no configuration surface stay in the module that uses them. The test
for whether a number belongs here is whether ``config.loader`` has to know it.
"""

from __future__ import annotations

#: Silence needed to call an utterance finished (``stt.silence_ms``). Long enough
#: to survive the pause between clauses, short enough that finalising feels
#: immediate.
DEFAULT_SILENCE_MS = 700

#: Floor on that window. Below roughly this, the normal pause between two words
#: ends the utterance, so a caller asking for less is asking for a recogniser
#: that cuts them off. Enforced in :mod:`kiro_crew.stt.vad` and clamped to by
#: the config loader, so a stored value is always the effective one.
MIN_SILENCE_MS = 200

#: How often a live partial may be produced (``stt.partial_interval_ms``). Below
#: this the text churns faster than it can be read. The interval starts after
#: inference completes; model and runtime speed determine actual update latency.
DEFAULT_PARTIAL_INTERVAL_MS = 400

#: Floor on the partial cadence, enforced in :mod:`kiro_crew.stt.session`.
MIN_PARTIAL_INTERVAL_MS = 100

#: Ceiling shared by both millisecond knobs. This is where either reads as a hang
#: rather than a setting: five seconds of silence before a phrase commits, or
#: five seconds of speaking with nothing appearing.
MAX_INTERVAL_MS = 5_000

#: How long a loaded model may sit unused before it is released
#: (``stt.idle_evict_secs``). A model is 148 MB resident at the default and
#: 1.6 GB at the largest, which is not something to hold for the life of a
#: gateway that transcribed one voice memo this morning. Long enough that a
#: conversational back-and-forth never reloads.
DEFAULT_IDLE_EVICT_SECS = 600

#: Zero is legal and means "release the model as soon as it goes idle", which is
#: the right choice on a memory-constrained host.
MIN_IDLE_EVICT_SECS = 0

#: A day. Past this the setting is indistinguishable from "never release", and a
#: gateway that has been up that long has had many chances to release.
MAX_IDLE_EVICT_SECS = 86_400

#: Ceiling on how long a caller waits for one decode or one model load
#: (``stt.timeout_secs``). Model size, available acceleration, and a first load's
#: GPU pipeline compilation can make work expensive, so this bounds waiting
#: without assuming that every native runtime decodes in real time. It does NOT bound the
#: first-run model download, which happens before the engine takes its lock.
DEFAULT_TIMEOUT_SECS = 300

#: Native decode cancellation cleanup, shared with the browser's advertised
#: final-result deadline so a cooperative abort can release its context first.
DECODE_ABORT_GRACE_SECS = 5.0

#: Floor on that ceiling. A first-ever model load compiles a GPU pipeline and was
#: measured at 7.4 s, so a smaller value would abort the one operation that is
#: legitimately slow and leave voice input permanently broken on a cold host.
MIN_TIMEOUT_SECS = 10

#: An hour. Past this the setting is indistinguishable from no ceiling at all, and
#: the point of having one is to fail a wedged call rather than hang a caller.
MAX_TIMEOUT_SECS = 3_600

#: ``stt.threads`` value that derives the decode thread count from the host: half
#: the cores the process may run on, capped at :data:`THREAD_CEILING`. Stored as
#: ``0`` so the field stays an integer; ``"auto"`` is accepted as a spelling of it.
THREADS_AUTO = 0

#: Ceiling on the derived (auto) thread count. The count itself is host-derived;
#: this only bounds EXTRAPOLATION above the widths that were measured. Decode-heavy
#: models stop benefiting early (in-process ``base``, an 11s clip: 8 threads 0.96s,
#: 16 1.13s, 24 1.18s, i.e. flat-to-worse), while encoder-heavy ``large-v3-turbo``
#: keeps gaining to 24 (6.26s / 5.13s / 4.81s). 16 is where both model shapes sit
#: within 7% of their own best, so a 64- or 128-core host gets 16 rather than an
#: untested 32+. An explicit count is NOT capped by this: the ceiling bounds
#: extrapolation on hosts nobody measured, and an operator who sets a count has
#: measured their own host.
#:
#: Lives here rather than in :mod:`kiro_crew.stt.engine` so the resolver below is
#: reachable without importing that module, which pulls in numpy. The dashboard's
#: config read reports the resolved count and must not load an array library on
#: the request path to do it.
THREAD_CEILING = 16

#: Ceiling on a STORED ``stt.threads``. The recogniser clamps a configured count to
#: the cores this process may run on at the moment it loads a model, so this bound
#: only stops a typo (``6400``) from being written; it is not a tuning limit.
MAX_THREADS = 1024

#: Environment variable that outranks ``stt.threads`` for one process, the way
#: ``KIROCREW_EMBED_MODEL_URL`` outranks ``memory.embed_model_url``. Read when a
#: model loads, from the environment of the process that loads it. Today that is
#: always the gateway: dictation, Slack and messaging attachments, and the meetings
#: audio import all decode there.
THREADS_ENV = "KIROCREW_STT_THREADS"

_THREADS_AUTO_SPELLING = "auto"


def parse_threads(value: object) -> int | None:
    """*value* as a ``stt.threads`` setting: :data:`THREADS_AUTO`, a count, or ``None``.

    ``None`` means the value is unusable and the caller decides the fallback: the
    config loader keeps the default, the environment reader ignores the variable,
    and the dashboard and CLI refuse the write. Accepted spellings are an int in
    ``[0, MAX_THREADS]``, the same digits as a string, ``"auto"`` in any case, and
    ``""`` (which reads as auto). ``bool`` is refused because it subclasses ``int``,
    and only ASCII digits are read so ``"١٦"`` cannot slip through ``int()``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == _THREADS_AUTO_SPELLING:
            return THREADS_AUTO
        if not (text.isascii() and text.isdigit()):
            return None
        # Leading zeros are dropped first, then anything longer than MAX_THREADS's
        # own digits is refused before int() runs: past ~4300 digits int() raises
        # ValueError instead of returning, which would crash config loading. int()
        # reads the stripped digits, the same ones the length check measured.
        digits = text.lstrip("0") or "0"
        if len(digits) > len(str(MAX_THREADS)):
            return None
        value = int(digits)
    if not isinstance(value, int):
        return None
    return value if THREADS_AUTO <= value <= MAX_THREADS else None


def resolve_thread_count(configured: int, cpus: int) -> int:
    """The intra-op thread count *configured* resolves to on a host of *cpus* cores.

    A positive *configured* is used as given, clamped to ``[1, cpus]``: an operator
    who names a count has measured their host, so it is not held to
    :data:`THREAD_CEILING`. :data:`THREADS_AUTO` (``0``) derives the count from the
    host as half the cores, bounded by :data:`THREAD_CEILING`.

    Pure arithmetic on two ints, with no I/O and no package imports, so the
    dashboard config read can report the resolved count without importing
    :mod:`kiro_crew.stt.engine` (which loads numpy) onto the request path. The
    engine's own :func:`~kiro_crew.stt.engine.thread_count` adds the
    ``KIROCREW_STT_THREADS`` override and its one-time warning on top of this.
    """
    if configured > 0:
        return max(1, min(configured, cpus))
    return max(1, min(THREAD_CEILING, cpus // 2))
