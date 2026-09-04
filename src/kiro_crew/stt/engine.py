"""A resident whisper.cpp recogniser, loaded once and reused.

The cost that made local speech-to-text feel broken was never the decode. It was
that every utterance paid for a fresh process, a fresh interpreter and a fresh
model load before any audio was looked at. Holding one loaded model in the
gateway removes all three: measured on a 4.2 s clip, a warm decode is 30-48 ms
against a real-time factor of ~0.01, and a 0.9 s push-to-talk utterance is 27 ms.

Three properties make it safe to do this in the gateway process rather than in a
worker of its own:

- **whisper.cpp releases the GIL for the duration of a decode.** A spinner thread
  measured 44.2M iterations/s during a decode against 61.0M/s idle, so the event
  loop keeps running while inference is in flight, so a worker thread is enough.
  Decodes run on ``executors.stt_executor()`` rather than the default pool: a
  started ``run_in_executor`` future cannot be cancelled, so a wedged model load
  would otherwise hold a general-purpose worker for the life of the process. What
  is NOT needed is the dedicated inference thread and priority queue
  ``embeddings.py`` carries, because every request here is interactive: there is
  no bulk class that could starve one.
- **It writes nothing to stdout.** ``print_progress=False`` and
  ``print_realtime=False`` suppress the progress writers; with both set, a load and
  a decode were measured to emit zero bytes on stdout. That matters because this
  module is imported by the MCP servers, whose stdout IS their protocol, and the
  vendored llama.cpp runtime has the opposite behaviour (it ``dup2``s over fd 1
  during model load).

  ``redirect_whispercpp_logs_to`` is deliberately left at its ``False`` default and
  must NOT be set to ``None``, which is the opposite of what the name suggests. It
  is not a log-callback switch and has nothing to do with stdout: passing ``None``
  makes the binding ``os.dup2`` ``/dev/null`` over **fd 2** for the duration of the
  model load, which is process-wide and therefore silences every other thread too.
  Measured across one load: 693 of 862 concurrent stderr writes destroyed, against
  zero at the default, with stdout clean either way. Losing the gateway's logs for
  the 7.4 s of a cold load costs more than the handful of whisper.cpp info lines
  the default lets through -- and those go to stderr, where diagnostics belong.
- **The context is single-entry.** ``whisper_full`` mutates state on the context,
  so two concurrent decodes on one context corrupt each other. Every decode holds
  ``_decode_lock``; a partial that is superseded aborts instead of queueing.

The recogniser package is an optional extra, imported lazily, so a gateway
without it starts normally and reports voice as unavailable with a reason.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import logging
import os
import platform
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from kiro_crew import extras
from kiro_crew.executors import stt_executor
from kiro_crew.stt import models
from kiro_crew.stt.limits import DEFAULT_IDLE_EVICT_SECS, DEFAULT_TIMEOUT_SECS
from kiro_crew.stt.vad import SAMPLE_RATE_HZ

logger = logging.getLogger(__name__)

#: Ceiling on the derived thread count. The count itself is host-derived; this
#: only bounds EXTRAPOLATION above the widths that were measured. Decode-heavy
#: models stop benefiting early (in-process ``base``, an 11s clip: 8 threads
#: 0.96s, 16 1.13s, 24 1.18s, i.e. flat-to-worse), while encoder-heavy
#: ``large-v3-turbo`` keeps gaining to 24 (6.26s / 5.13s / 4.81s). 16 is where
#: both model shapes sit within 7% of their own best, so a 64- or 128-core host
#: gets 16 rather than an untested 32+.
THREAD_CEILING = 16

#: Audio handed to a prewarm decode. whisper.cpp refuses a buffer shorter than
#: 1000 ms, and the point of prewarming is to pay the graph allocation (measured
#: at 154-528 ms on the first decode after a load) before a user is waiting on
#: it. Silence is fine: the cost is in building the graph, not in the content.
_PREWARM_SECS = 1.0

#: How long a timed-out decode is given to honour its abort callback before the
#: decode lock is released anyway. whisper.cpp polls the callback inside its decode
#: loop, so a cooperating call returns in well under a second; this only bounds the
#: pathological case where it does not, since holding the lock forever would wedge
#: every later decode instead of just this one.
_ABORT_GRACE_SECS = 5.0

#: How long a timed-out LOAD is waited on before its lock is released. Longer than the
#: decode grace because a load has no abort callback at all: nothing can ask it to stop,
#: so this is purely "give the allocation a chance to finish while still holding the
#: lock", and finishing is the good outcome (the context is adopted).
_LOAD_GRACE_SECS = 30.0


def available_cpus() -> int:
    """Return the core count this process may actually run on.

    ``os.sched_getaffinity`` rather than ``os.cpu_count``: under a CPU-set
    restriction (containers, cgroups, ``taskset``) the latter reports the whole
    machine, which is exactly the environment that over-threads worst. Falls back
    to ``os.cpu_count`` where affinity is unavailable (macOS, Windows).
    """
    if hasattr(os, "sched_getaffinity"):
        try:
            return len(os.sched_getaffinity(0)) or 1
        except OSError:
            pass
    return os.cpu_count() or 1


def thread_count() -> int:
    """Derive the recogniser's intra-op thread count from the host.

    Half the available cores, bounded by :data:`THREAD_CEILING`.

    Whisper decoding is autoregressive: thousands of tiny parallel regions, each
    ending in a barrier that completes only when its slowest worker arrives. Wide
    pools therefore cost latency per step rather than buying throughput, and on a
    host with other work (a Kiro Crew host runs the gateway and agent sessions
    alongside) the workers are time-sliced, so a barrier waits on threads the
    scheduler has not run yet.

    Half the cores is measured, not assumed, at two widths on a 32-core
    Graviton3 host: at 32 visible cores 16 threads beat 31 (base 4.9s vs 7.3s,
    turbo 20.8s vs 26.9s), and under ``taskset`` to 16 visible cores 8 threads
    beat 16 (5s vs 7s). Taking every core also destabilises the runtime far more
    than it slows it: 8 threads measured 4.9-5.0s across repeats while 32 threads
    ranged 8.1-68.4s depending on background load. The headroom buys
    predictability first and mean latency second.
    """
    return max(1, min(THREAD_CEILING, available_cpus() // 2))


# ── Availability ──

#: Machine-readable availability reasons. These travel to the browser in JSON
#: error bodies and select the message the dashboard renders, so they are a
#: contract: rename one and the UI silently falls back to a generic string.
CODE_OK = ""
CODE_EXTRA_MISSING = "stt_extra_missing"
CODE_NO_WHEEL = "stt_no_wheel_for_platform"
CODE_IMPORT_FAILED = "stt_import_failed"
CODE_MODEL_MISSING = "stt_model_missing"
#: A decode that RAN AND FAILED, as opposed to one that heard nothing. Its own code
#: because collapsing the two is what made the failure invisible: a failed decode
#: answered ``""``, the session read that as silence, the transport dropped the empty
#: final, and the user's dictation disappeared with nothing on screen to say why.
CODE_DECODE_FAILED = "stt_decode_failed"


class DecodeFailed(RuntimeError):
    """A decode did not finish, so its silence carries no information.

    Raised rather than returned because an empty transcript is a legitimate
    outcome of this API -- a muted microphone, a pause, a hallucination filtered
    away -- and a caller cannot tell that apart from a failure by looking at the
    string. Whoever holds the user's surface decides what to do with it: the
    session turns a FINAL failure into an ``error`` event, and drops a partial's.

    ``code`` travels to the browser as the machine-readable half of an ``error``
    frame; ``detail`` is advisory English for the log and for a client with no
    catalog entry for the code.
    """

    def __init__(self, detail: str, *, code: str = CODE_DECODE_FAILED) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


#: ``whisper_full``'s success status. Every other value is a failure, and which one
#: it is does not change the answer for a caller, so it is reported rather than
#: mapped.
_WHISPER_OK = 0


def _whisper_binding() -> Any | None:
    """The recogniser's low-level extension module, or ``None`` if it is unusable.

    ``None`` is a real answer rather than an error: :func:`_decode_segments` uses
    the binding only to read a status the library discards, so a host where it
    cannot be reached still decodes -- it just cannot distinguish a failed encode
    from silence.
    """
    try:
        import _pywhispercpp as binding
    except Exception:  # pragma: no cover (host-specific loader failures)
        return None
    return binding


def _decode_segments(
    model: Any,
    pcm: np.ndarray,
    should_abort: Callable[[], bool],
) -> list[Any]:
    """Run one whisper.cpp decode, raising :class:`DecodeFailed` on a native failure.

    Blocking; runs in a worker thread.

    **pywhispercpp throws the failure away.** ``Model.transcribe`` forwards to
    ``Model._transcribe``, which calls ``whisper_full`` as a bare statement and never
    looks at its return value; it then reads ``whisper_full_n_segments``, which is 0
    after a failed encode, and hands back the empty list ``_get_segments`` built from
    it. So ``transcribe`` answers ``[]`` for a failure and ``[]`` for a silent
    recording, byte for byte, and the only trace of the difference is a line
    whisper.cpp writes to its own stderr (``whisper_full_with_state: failed to
    encode``, the ``-6`` path, which is how this was seen in production losing a
    whole utterance). Nothing in the public API exposes the code afterwards, because
    nothing retains it. The status is therefore read here, from the same extension
    module the library itself calls.

    A non-zero status does NOT by itself mean something went wrong: an abort granted
    through ``abort_callback`` unwinds the encoder, which reports the same ``-6``. So
    the predicate that would have asked for the abort is what separates the two, and
    an aborted decode returns ``[]`` like the non-event it is.

    Falls back to ``Model.transcribe`` when the private surface is not shaped as
    expected -- a renamed member in a later release, or an unreachable extension
    module. That path loses only the status; the failures :meth:`WhisperEngine.decode`
    raises on regardless (an exception out of the call, and a timeout) are unaffected.
    """
    ctx = getattr(model, "_ctx", None)
    params = getattr(model, "_params", None)
    get_segments = getattr(type(model), "_get_segments", None)
    binding = _whisper_binding()
    if ctx is None or params is None or get_segments is None or binding is None:
        return list(model.transcribe(pcm, abort_callback=should_abort))
    # Both callbacks are assigned exactly as `Model.transcribe` assigns them, and
    # they live on the params object rather than on the call, so a stale
    # new-segment callback from some other caller would otherwise still fire.
    binding.assign_new_segment_callback(params, None)
    binding.assign_abort_callback(params, should_abort)
    status = binding.whisper_full(ctx, params, pcm, pcm.size)
    if status != _WHISPER_OK:
        if should_abort():
            return []
        raise DecodeFailed(f"whisper.cpp decode returned {status}")
    return list(get_segments(ctx, 0, binding.whisper_full_n_segments(ctx)))


@dataclass(frozen=True)
class Availability:
    """Whether local recognition can run, and if not, precisely why.

    Distinguishing the reasons is the point. "Install an extra" and "your
    platform has no prebuilt wheel" lead to completely different actions, and
    collapsing them into one boolean is what makes a feature feel broken instead
    of unconfigured.
    """

    ok: bool
    code: str = CODE_OK
    detail: str = ""


#: CPU architectures the recogniser publishes a wheel for, per OS, keyed by
#: ``platform.system()`` and holding lower-cased ``platform.machine()`` spellings
#: (which differ per OS: Windows reports ``AMD64`` where Linux reports ``x86_64``).
#:
#: Transcribed from the published wheel matrix rather than inferred, because the
#: gaps are not where a reader would guess: macOS is arm64-ONLY (there is no
#: ``macosx_*_x86_64`` wheel at any Python version), Linux is x86_64 and aarch64
#: only (no armv7l, i686, ppc64le or s390x), and Windows has no ``win_arm64``.
#: Anything outside this attempts a source build needing a C++ toolchain and CMake.
_WHEEL_ARCHS: dict[str, frozenset[str]] = {
    "Darwin": frozenset({"arm64", "aarch64"}),
    "Linux": frozenset({"x86_64", "amd64", "aarch64", "arm64"}),
    "Windows": frozenset({"amd64", "x86_64", "x86", "i386", "i686"}),
}


def _has_prebuilt_wheel() -> bool:
    """Whether this platform is one the recogniser publishes a wheel for.

    Fails CLOSED on anything unlisted, which is the correction that matters: the
    previous form special-cased Intel macOS and returned True everywhere else, so
    Windows ARM64, 32-bit ARM Linux and every other unlisted architecture were told
    to "install the voice extra" -- advice that cannot work, since the install they
    were being sent back to is the one that found no wheel. Naming the platform
    instead turns an unfixable loop into a statement of what is missing.

    A glibc older than 2.27 is a fourth way to have no usable wheel and is NOT
    modelled here: the version is awkward to read portably and the failure surfaces
    as an import error carrying the real loader message, which is more use to the
    reader than a guess would be.
    """
    allowed = _WHEEL_ARCHS.get(platform.system())
    if allowed is None:
        return False
    return platform.machine().lower() in allowed


def probe() -> Availability:
    """Report whether the recogniser is importable on this host.

    Import is attempted, not inferred from a package listing: a wheel can be
    installed and still fail to load (a missing system library, an incompatible
    CPU baseline), and only an import distinguishes the two.
    """
    # Whether the package is INSTALLED is a separate question from whether it
    # imports, and conflating them mislabels the common failure. An ImportError
    # raised from inside an installed pywhispercpp -- a missing system library, a
    # too-old glibc, a numpy ABI mismatch -- is indistinguishable at the `except`
    # from the package being absent, so every one of those was reported as "install
    # the voice extra" to a user who already had. Ask the finder first: it answers
    # "is it on the path" without executing anything.
    try:
        installed = importlib.util.find_spec("pywhispercpp") is not None
    except Exception as exc:
        # A finder can RAISE rather than answer: a broken loader on `sys.meta_path`,
        # a partially-installed package whose spec cannot be built, a native
        # extension whose `dlopen` fails during discovery. That is the
        # installed-but-unusable state, and the finder's own message names the cause.
        return Availability(False, CODE_IMPORT_FAILED, f"recogniser failed to load: {exc}")
    if not installed:
        if not _has_prebuilt_wheel():
            return Availability(
                False,
                CODE_NO_WHEEL,
                f"no prebuilt speech recogniser for {platform.system()} "
                f"{platform.machine()}; install a C++ toolchain and CMake, "
                f"then reinstall it with {extras.install_hint('voice')}",
            )
        return Availability(
            False,
            CODE_EXTRA_MISSING,
            f"speech recognition needs its voice dependencies: {extras.install_hint('voice')}",
        )
    try:
        import pywhispercpp.model  # noqa: F401
    except Exception as exc:  # pragma: no cover (host-specific loader failures)
        # Installed but unusable. The loader's own message is the useful part: it
        # names the missing library or the ABI that did not match.
        return Availability(False, CODE_IMPORT_FAILED, f"recogniser failed to load: {exc}")
    return Availability(True)


@dataclass(frozen=True)
class LoadedKey:
    """What a loaded context is specific to. A change here forces a reload.

    Public because it crosses the engine's boundary: a caller snapshots the key
    its :meth:`WhisperEngine.ensure_loaded` installed and hands it back to
    :meth:`WhisperEngine.decode`, which is what proves the decode ran on the model
    that was prepared for it rather than on one a concurrent session swapped in.
    """

    model_path: str
    language: str
    n_threads: int


class WhisperEngine:
    """Holds one loaded whisper.cpp context and serialises decodes onto it.

    A single instance is shared process-wide (see :func:`engine`). The state it
    holds (which model is loaded) is caller-independent, so it is safe in the
    MCP servers that serve many sessions from one process.
    """

    def __init__(
        self,
        idle_evict_secs: int = DEFAULT_IDLE_EVICT_SECS,
        timeout_secs: int = DEFAULT_TIMEOUT_SECS,
    ) -> None:
        self._idle_evict_secs = idle_evict_secs
        self._timeout_secs = timeout_secs
        self._model: Any | None = None
        self._key: LoadedKey | None = None
        self._last_used = 0.0
        # Both created lazily inside the running loop: an asyncio.Lock built at
        # import time belongs to whichever loop first awaits it, which breaks a
        # test suite that runs a fresh loop per test.
        self._load_lock: asyncio.Lock | None = None
        self._decode_lock: asyncio.Lock | None = None
        # Incremented by every decode request. An in-flight partial whose
        # generation is stale aborts rather than finishing work nobody will read.
        self._generation = 0
        #: The executor future of a load still in flight, kept so a load that outlived
        #: its caller's timeout cannot be joined by a second one. A native context is
        #: 148 MB at the default and 1.6 GB at the largest, so two at once is what
        #: exhausts a host sized for one.
        self._load_future: "asyncio.Future[Any] | None" = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def set_bounds(self, idle_evict_secs: int, timeout_secs: int) -> None:
        """Update the idle window and the per-call ceiling from current config.

        Both are read at the point of use, so a change takes effect on the next
        decode without reloading the model.
        """
        self._idle_evict_secs = idle_evict_secs
        self._timeout_secs = timeout_secs

    def _locks(self) -> tuple[asyncio.Lock, asyncio.Lock]:
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        if self._decode_lock is None:
            self._decode_lock = asyncio.Lock()
        return self._load_lock, self._decode_lock

    async def ensure_loaded(self, model_name: str, language: str) -> Availability:
        """Load the configured model if it is not already resident.

        Returns an :class:`Availability` rather than raising so a websocket
        handler can turn a missing extra or an undownloaded model into a status
        frame instead of a 500.
        """
        # Off the loop: `probe` imports the binding, which dlopens a native library.
        # Measured at 209 ms cold, and this runs on the websocket handler's task, so
        # doing it inline stalled the whole gateway on the first voice session of a
        # boot. `to_thread` rather than the STT pool deliberately: the pool's two
        # workers are for decodes, and a probe must not queue behind one.
        probed = await asyncio.to_thread(probe)
        if not probed.ok:
            return probed

        model = models.resolve(model_name)
        # The key is built from the path the model WOULD have, so residency can be
        # decided before the store is asked for it. That ordering is what makes the
        # store's digest check affordable: `ensure` verifies the file against its pin
        # on every call, so calling it per session would hash 148 MB (1.6 GB at the
        # largest) on the path this feature exists to make fast. Asked only when a
        # load is actually about to happen, the cost lands once per load -- which is
        # also exactly when an unverified file would take effect.
        key = LoadedKey(str(models.model_path(model)), language, thread_count())
        load_lock, decode_lock = self._locks()
        # The LOAD lock spans the whole episode; the DECODE lock is taken only around
        # the context swap, and that narrower scope is load-bearing. `ensure` below
        # downloads up to 1.6 GB and re-hashes a file already on disk, and neither
        # touches the resident context -- so holding the decode lock across it stalled
        # every other session for the length of a download. A live stream that cannot
        # decode is not merely slow: it keeps buffering until it hits the duration cap
        # and then discards the speech.
        #
        # Lock order is load-then-decode here, in `maybe_evict`, and nowhere the
        # reverse -- `decode` takes only the decode lock -- so the pair cannot deadlock.
        async with load_lock:
            if self._key == key and self._model is not None:
                # Read WITHOUT the decode lock, deliberately. `decode` can retire the
                # context holding only that lock, but this condition is a single
                # expression with no `await` in it, so nothing interleaves between the
                # two reads. The answer is advisory either way: the caller hands
                # `expect=key` to `decode`, which re-checks under the decode lock and
                # refuses if the context changed underneath. Keeping a hit off the
                # decode lock is what lets `prewarm` return while the previous
                # utterance is still decoding, which is the whole point of prewarming.
                self._last_used = time.monotonic()
                return Availability(True)
            path = await models.store().ensure(model)
            if path is None:
                return Availability(
                    False,
                    CODE_MODEL_MISSING,
                    f"speech model {model.name} is not downloaded",
                )
            if self._load_future is not None and not self._load_future.done():
                # A previous load timed out, released this lock and is STILL building
                # its context. Starting a second one is how a host sized for one model
                # runs out of memory, so refuse instead: the load in flight will land,
                # and the next request finds it resident.
                logger.warning("A whisper model load is already in flight; not starting another")
                return Availability(False, CODE_IMPORT_FAILED, "a model load is already running")
            # BOTH locks from here, for the same reason `maybe_evict` takes both:
            # replacing the resident context while a decode is running on it leaves
            # the old one alive (the decode holds its own reference to the model
            # object) while the replacement allocates, so a host sized for
            # one model briefly has two.
            async with decode_lock:
                if self._model is not None:
                    # A model change means the resident context is for the wrong
                    # weights; drop it before loading, so peak memory is one model
                    # rather than two.
                    self._unload_locked()
                loop = asyncio.get_running_loop()
                # `asyncio.wait` on a kept reference, not `wait_for`: a load has no abort
                # hook, so a timeout cannot stop the native allocation. `wait_for` would
                # cancel the wrapper and let this lock go while a 1.6 GB context was still
                # being built, and the caller's retry would start a SECOND one.
                future = loop.run_in_executor(stt_executor(), self._build_model, key)
                self._load_future = future
                done, _pending = await asyncio.wait({future}, timeout=self._timeout_secs)
                if not done:
                    # Hold the lock a while longer first: most slow loads finish just after
                    # the caller gave up, and waiting here keeps the whole episode inside
                    # one lock hold instead of leaking a pending load past it.
                    settled, _still = await asyncio.wait({future}, timeout=_LOAD_GRACE_SECS)
                    if settled:
                        # It finished late. ADOPT it rather than discarding it: the memory
                        # is already allocated, and dropping it would have the next request
                        # build a second context for the same weights.
                        self._load_future = None
                        try:
                            self._model = future.result()
                            self._key = key
                            self._last_used = time.monotonic()
                        except Exception:
                            # It failed rather than finished. Logged rather than suppressed:
                            # the caller already has its timeout, so this is the only place
                            # the real reason is ever visible.
                            logger.warning(
                                "The whisper model load that outlived its timeout then failed",
                                exc_info=True,
                            )
                    logger.error(
                        "Loading whisper model %s exceeded %ds. The caller is released; the "
                        "load itself cannot be cancelled, so it is %s.",
                        model.name,
                        self._timeout_secs,
                        "resident now that it finished" if settled else "still running",
                    )
                    return Availability(False, CODE_IMPORT_FAILED, "model load timed out")
                self._load_future = None
                try:
                    self._model = future.result()
                except Exception as exc:
                    logger.warning("Failed to load whisper model from %s: %s", path, exc)
                    return Availability(False, CODE_IMPORT_FAILED, f"model load failed: {exc}")
                self._key = key
                self._last_used = time.monotonic()
                logger.info(
                    "Whisper model loaded: %s (language=%s, threads=%d)",
                    model.name,
                    language or "auto",
                    key.n_threads,
                )
        return Availability(True)

    @staticmethod
    def _build_model(key: LoadedKey) -> Any:
        """Construct the recogniser. Blocking; runs in a worker thread.

        Passing an absolute path as ``model`` keeps the library's own resolver
        from treating the value as a catalog name and downloading anything: every
        fetch in Kiro Crew goes through the sha256-pinned store instead.
        """
        from pywhispercpp.model import Model

        return Model(
            model=key.model_path,
            # Load-bearing for stdout hygiene, which the MCP servers depend on: see
            # the module docstring. Do not remove either.
            print_progress=False,
            print_realtime=False,
            # `redirect_whispercpp_logs_to` is NOT passed on purpose. Its `None`
            # value dup2s /dev/null over fd 2 process-wide during the load and
            # destroys other threads' stderr; the module docstring has the
            # measurement. The default (False) redirects nothing.
            n_threads=key.n_threads,
            language=key.language or "auto",
            # Each utterance is independent, so carrying decoder context across
            # calls would let one dictation seed the next one's prompt.
            no_context=True,
            no_timestamps=True,
            # Suppresses the model's non-speech token class at decode time. The
            # transcript filter is still applied by the caller: this reduces the
            # artifacts, it does not eliminate them.
            suppress_nst=True,
        )

    @property
    def loaded_key(self) -> LoadedKey | None:
        """What the resident context was loaded for, or ``None`` when unloaded.

        A caller snapshots this after :meth:`ensure_loaded` and hands it back to
        :meth:`decode`, which is how a decode proves it ran on the model that was
        prepared for it.
        """
        return self._key

    async def decode(
        self,
        pcm: np.ndarray,
        *,
        superseding: bool = False,
        expect: LoadedKey | None = None,
    ) -> str:
        """Transcribe mono float32 16 kHz audio, returning cleaned text.

        ``superseding`` marks a request whose result is only useful if it is the
        newest one, i.e. a live partial. Such a request aborts as soon as another
        request arrives, so a slow partial can never delay the final decode
        behind it.

        ``expect`` is the key the caller prepared, and passing it is how a caller
        avoids silently transcribing with someone else's model. This engine is a
        process singleton, so a second session whose configuration names a
        different model or language replaces the resident context, and a decode
        that did not check would return text from the wrong weights or the wrong
        language with nothing to show it had happened. On a mismatch the decode
        refuses and says so, which the caller answers by preparing again.

        Raises :class:`DecodeFailed` when the decode FAILED: the native call raised,
        the native call reported a non-zero status, or the wait expired and the work
        was aborted. Returns ``""`` only for the three protocol non-events a caller
        already handles -- no resident model, a superseded partial, and an ``expect``
        mismatch -- so an empty string means "nothing was heard" and nothing else.
        """
        if self._model is None:
            return ""
        _, decode_lock = self._locks()
        self._generation += 1
        my_generation = self._generation
        # Set when the caller's wait expires. whisper.cpp polls the abort callback
        # during decoding, so unlike a bare executor timeout this genuinely unwinds
        # the work and frees the worker rather than leaving it running unseen.
        expired = False

        def should_abort() -> bool:
            return expired or (superseding and self._generation != my_generation)

        async with decode_lock:
            if should_abort():
                return ""
            model = self._model
            if model is None:
                return ""
            if expect is not None and self._key != expect:
                logger.warning(
                    "Refusing a decode prepared for %s while %s is resident; "
                    "the caller must prepare again",
                    expect.model_path,
                    self._key.model_path if self._key else "nothing",
                )
                return ""
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                stt_executor(),
                functools.partial(_decode_segments, model, pcm, should_abort),
            )
            # `asyncio.wait` rather than `wait_for`, because a timeout here must NOT
            # abandon the future: `wait_for` cancels the wrapper and leaves the
            # executor thread inside `whisper_full`, and the `async with` below then
            # releases the decode lock under it. The context is single-entry, so the
            # next decode would enter `whisper_full` on a context this one is still
            # executing on -- the exact corruption the lock exists to prevent.
            done, _pending = await asyncio.wait({future}, timeout=self._timeout_secs)
            if not done:
                expired = True
                logger.error(
                    "Whisper decode of %.1fs of audio exceeded %ds; aborting it",
                    pcm.size / SAMPLE_RATE_HZ,
                    self._timeout_secs,
                )
                # whisper.cpp polls the abort callback inside the decode loop, so the
                # native call unwinds once `expired` is set -- promptly, but not
                # atomically. Keep holding the lock until it has actually returned.
                settled, _still_running = await asyncio.wait({future}, timeout=_ABORT_GRACE_SECS)
                if not settled:
                    # The call is not unwinding, and holding the lock forever would
                    # wedge every later decode instead of just this one. So release it
                    # -- but RETIRE the context first, so the next decode cannot enter
                    # a context this call is still executing on. The next request
                    # finds no model and its re-prepare loads a fresh one.
                    #
                    # Dropping the reference here does not free the context under the
                    # running call: the worker holds its own reference through the
                    # `model` it was handed, so `whisper_free` runs only once that
                    # returns.
                    logger.error(
                        "Whisper decode did not honour the abort within %.0fs; retiring "
                        "the context so no later decode shares it, and reloading on the "
                        "next request",
                        _ABORT_GRACE_SECS,
                    )
                    self._model = None
                    self._key = None
                    raise DecodeFailed(f"decode exceeded {self._timeout_secs}s and did not abort")
                # A timeout is a failure even when the abort landed cleanly. The audio
                # was heard and no transcript exists for it, so answering "" here is
                # what let a whole utterance disappear behind an empty final.
                raise DecodeFailed(f"decode exceeded {self._timeout_secs}s")
            try:
                segments = future.result()
            except DecodeFailed:
                # Already carries its own reason (a non-zero native status), and
                # re-wrapping it would bury that behind this frame's generic text.
                logger.warning("Whisper decode failed", exc_info=True)
                raise
            except Exception as exc:
                logger.warning("Whisper decode failed", exc_info=True)
                raise DecodeFailed(f"decode failed: {exc}") from exc
            self._last_used = time.monotonic()

        parts = [str(getattr(seg, "text", "")).strip() for seg in segments]
        return " ".join(p for p in parts if p).strip()

    async def prewarm(self, model_name: str, language: str) -> Availability:
        """Load the model and run one throwaway decode.

        Called when the user reaches for the microphone, not when they release
        it, so the one-off costs (a 7.4 s first-ever load while the GPU pipeline
        is compiled, and 154-528 ms of graph allocation on the first decode) are
        paid while they are still speaking.
        """
        result = await self.ensure_loaded(model_name, language)
        if not result.ok:
            return result
        silence = np.zeros(int(_PREWARM_SECS * SAMPLE_RATE_HZ), dtype=np.float32)
        try:
            await self.decode(silence)
        except DecodeFailed:
            # The throwaway decode is an optimisation, and the model IS loaded, which
            # is what this function reports on. Failing the prewarm would refuse a
            # session the user could still dictate into, and the real decode that
            # follows raises on its own behalf if the fault persists.
            logger.warning("Whisper prewarm decode failed", exc_info=True)
        return result

    async def maybe_evict(self) -> bool:
        """Release the model when it has been idle past the configured window.

        Zero is a meaningful setting, not a disable: `limits.MIN_IDLE_EVICT_SECS`
        documents it as "release the model as soon as it goes idle", which is the
        right choice on a memory-constrained host. Treating ``<= 0`` as "never"
        inverted that -- the one value a user picks to bound memory hardest was the
        one that held the model forever.

        Called both from the paths that finish a decode and from
        :func:`idle_sweep_loop`. The former alone cannot work: see that function.

        Holds BOTH locks, and the decode lock is the one that matters. Unloading on the
        load lock alone could retire the context while a decode was running on it: the
        decode survives (it holds its own reference to the model object), but the
        engine then looks unloaded, so a concurrent
        request builds a SECOND context and a host sized for one model has two. Taking
        the decode lock makes eviction and decoding mutually exclusive instead.

        Lock order is load-then-decode here and nowhere the reverse -- `decode` takes
        only the decode lock -- so the pair cannot deadlock.
        """
        if self._model is None or self._idle_evict_secs < 0:
            return False
        if time.monotonic() - self._last_used < self._idle_evict_secs:
            return False
        load_lock, decode_lock = self._locks()
        async with load_lock, decode_lock:
            # Re-checked under the locks: waiting for a decode to finish is exactly the
            # window in which the model stops being idle, and evicting the weights a
            # request is actively using would make the next decode reload them.
            if self._model is None:
                return False
            if time.monotonic() - self._last_used < self._idle_evict_secs:
                return False
            self._unload_locked()
        logger.info("Released idle whisper model after %ds", self._idle_evict_secs)
        return True

    async def close(self) -> None:
        """Drop the resident model. Idempotent."""
        load_lock, _ = self._locks()
        async with load_lock:
            self._unload_locked()

    def _unload_locked(self) -> None:
        """Release the context. Caller holds the load lock.

        The binding exposes no explicit free: its ``__del__`` calls
        ``whisper_free``, so dropping the last reference IS the release, and
        under CPython's refcounting that happens here rather than at some later
        collection. Which is why the reference is cleared and nothing else is
        held onto: keeping the object alive in a worker thread to "free it
        properly" would do the opposite.
        """
        self._model = None
        self._key = None


_engine: WhisperEngine | None = None


def shared_engine(
    idle_evict_secs: int | None = None,
    timeout_secs: int | None = None,
) -> WhisperEngine:
    """The process-wide recogniser.

    One loaded model per process, shared by the dashboard's voice sessions, the
    batch endpoint and any channel that receives a voice memo. Holding it once is
    the entire point of this module: the alternative is the per-utterance model
    load this replaced.

    Both bounds default to ``None`` meaning "leave whatever is set", NOT to the
    module defaults. They used to default to the constants, which made every
    zero-argument caller silently a WRITER: :func:`prewarm` and :func:`close` ask
    for the engine without opinions about its configuration, and each call reset
    an operator's configured window back to 600 s.
    """
    global _engine
    if _engine is None:
        _engine = WhisperEngine(
            idle_evict_secs=(
                DEFAULT_IDLE_EVICT_SECS if idle_evict_secs is None else idle_evict_secs
            ),
            timeout_secs=DEFAULT_TIMEOUT_SECS if timeout_secs is None else timeout_secs,
        )
    elif idle_evict_secs is not None and timeout_secs is not None:
        # The window comes from config, which is read live. Without this the value
        # the FIRST caller happened to pass would stick for the life of the
        # process, so editing the setting would appear to do nothing until a
        # restart. Reassigning it is safe: it is only read when deciding whether
        # an already-idle model has aged out.
        _engine.set_bounds(idle_evict_secs, timeout_secs)
    return _engine


#: How often the idle sweep re-checks the resident model. Coarse on purpose: the
#: idle window is a ceiling on how long the memory stays held, not a deadline, and
#: a check that finds nothing costs one monotonic subtraction.
_IDLE_SWEEP_INTERVAL_SECS = 60.0


async def idle_sweep_loop() -> None:
    """Release the resident model once it has been idle past its window.

    `WhisperEngine.maybe_evict` is also called from the paths that finish a decode,
    and that call can never fire on its own: it runs microseconds after
    ``_last_used`` was stamped, so the elapsed time is always inside the window.
    Idleness is by definition a stretch in which none of those paths run, so
    noticing it requires something that runs anyway.

    Which is why this exists despite being one more task to cancel at shutdown --
    the alternative was a documented saving that never happened, leaving 148 MB
    (1.6 GB at the largest model) resident for the life of a gateway that
    transcribed one voice memo in the morning.

    Deliberately does not CREATE the engine: a host that never uses voice should
    not get an instance out of a sweep, and there is nothing to evict until some
    caller has loaded a model.
    """
    while True:
        await asyncio.sleep(_IDLE_SWEEP_INTERVAL_SECS)
        engine = _engine
        if engine is None:
            continue
        try:
            await engine.maybe_evict()
        except Exception:
            logger.debug("Idle whisper eviction sweep failed", exc_info=True)


def pcm_from_int16(raw: bytes) -> np.ndarray:
    """Convert little-endian int16 PCM bytes to the float32 the recogniser wants.

    The dashboard's audio worklet already emits 16 kHz mono int16, so this is the
    whole adaptation: a scale into ``[-1, 1)``. An odd trailing byte is dropped
    rather than raising, because a socket read can split a sample across frames.
    """
    usable = len(raw) - (len(raw) % 2)
    if usable <= 0:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(raw, dtype="<i2", count=usable // 2).astype(np.float32) / 32768.0
