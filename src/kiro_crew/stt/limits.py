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
#: this the text churns faster than it can be read; measured decode cost for a
#: phrase-length buffer is 27-48 ms, so this is a readability bound, not a
#: compute one.
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
#: (``stt.timeout_secs``). Generous on purpose: a warm decode is tens of
#: milliseconds, so anything near this is a wedged native call rather than slow
#: work, and clipping a legitimate first load (which compiles a GPU pipeline the
#: first time) would be worse than waiting. It deliberately does NOT bound the
#: first-run model download, which happens before the engine takes its lock.
DEFAULT_TIMEOUT_SECS = 300

#: Floor on that ceiling. A first-ever model load compiles a GPU pipeline and was
#: measured at 7.4 s, so a smaller value would abort the one operation that is
#: legitimately slow and leave voice input permanently broken on a cold host.
MIN_TIMEOUT_SECS = 10

#: An hour. Past this the setting is indistinguishable from no ceiling at all, and
#: the point of having one is to fail a wedged call rather than hang a caller.
MAX_TIMEOUT_SECS = 3_600
