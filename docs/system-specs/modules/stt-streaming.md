# Streaming speech-to-text

## Overview

Live speech-to-text for the dashboard composer. The browser streams 16 kHz mono Int16 PCM over a WebSocket and the server relays partial hypotheses, one or more final transcripts, and (when enabled) an auto-submit signal.

All selectable providers implement streaming (`stt_stream._STREAMING_PROVIDERS`): `local` processes audio in this process, `apple` processes it on-device, and `transcribe` sends it to AWS Transcribe Streaming.

| `stt.provider` | Where recognition runs | Cost | Precondition |
|---|---|---|---|
| `local` (default) | this process, whisper.cpp held loaded by [`kiro_crew.stt`](../../../src/kiro_crew/stt/__init__.py) | free | desktop builds include the runtime; select a model and click **Download now** |
| `apple` | the OS, on-device SpeechAnalyzer | free | macOS 26 or later, and a Swift toolchain to build the helper |
| `transcribe` | AWS Transcribe Streaming | billed per audio-second | the `voice` extra, and a recorded AWS consent |

The batch path at `POST /api/stt/transcribe` (`transcribe.transcribe_audio`)
serves whole files instead: a Slack voice memo, a channel voice note, an upload.
Both paths read one provider setting and apply the same redaction, and on `local`
both go through the same resident model, so a voice memo decodes on the weights a
dictation just warmed. `stt.hallucinations.filter_hallucinations` also runs on both
of `local`'s outputs (whisper emits caption boilerplate on near-silence, and an
emptied transcript is reported as nothing heard rather than written into an agent's
notes). It is the recogniser's own artefact, so it is not applied to `apple` or
`transcribe`.

Compressed files are decoded by the FFmpeg executable in the pinned
`imageio-ffmpeg` wheel. It is part of the desktop voice runtime, not a desktop
system prerequisite. The release gate resolves that exact packaged resource and
executes `ffmpeg -version`; a supported artifact cannot publish with only
dependency metadata or an ambient PATH copy satisfying the check. Source
environments do not read their own site-packages, because a project venv is
agent-writable executable storage; they resolve a system FFmpeg from the fixed
platform paths, and failing that the digest-verified decoder store below.

**The store is a third location, not a third rule.** A source or Toolbox install on
a distribution that packages no FFmpeg (Amazon Linux, RHEL without EPEL) previously
had no decoder it could ever reach, so batch voice was permanently broken with a
shell command as the only remedy — and on such a host that command was an `echo`
of the ffmpeg.org URL. `stt.decoder` closes that: it downloads the platform's
pinned `imageio-ffmpeg==0.6.0` **wheel** from PyPI (filename, URL, size and sha256
pinned per platform), verifies the wheel's own digest before opening it, extracts
only `imageio_ffmpeg/binaries/<artifact>` by exact member name through a descriptor
it opened itself (never `ZipFile.extract`, whose destination is attacker-controlled
name data), re-verifies the extracted bytes against the SAME
`transcribe._PACKAGED_FFMPEG_ARTIFACTS` pin, and renames it atomically into
`<data home>/models/ffmpeg/`. Both digest tables are one table, derived in
`stt.decoder`: two copies would fail as a decoder that downloads successfully and
is then refused at exec, with nothing to say which copy was wrong.

Resolution order is bundled site-packages → the trusted system directories → the
store. The store adds no directory to `transcribe._ffmpeg_candidate_dirs`, and that
distinction is the whole point: that list is searched by NAME, so an entry there is
a claim that a path is trustworthy, while the store is reached only as a pinned
FILENAME whose bytes match its pinned digest, re-verified on every open and kept
bound to the descriptor that is spawned. A store file that fails is ignored and
logged. The directory is user-writable and vouches for nothing; it is also inside
the `models/` tree the agent's file and shell gates already write-protect.
Platforms with no pinned artifact (32-bit ARM Linux, Windows on ARM, win32) report
`unsupported` and start no download.

That gate reports **two independent verdicts** and the build treats them
differently (`transcribe.PackagedDecoderProbe`). Whether the resolved bytes
**authenticate** is a property of the artifact, holds on every host, and failing
it stops the build. Whether they then **execute** is a property of the build
machine: an image lacking an OS library the executable load-time imports makes
the loader refuse it before its entry point runs, while the identical bytes run
correctly for a user. Windows Server Core is the case that forced the
distinction — it carries no Video for Windows components, so it cannot load a
Windows ffmpeg that imports `AVICAP32.dll`, and every CodeBuild Windows image is
built on it. So an authenticated payload that will not run **warns and ships**;
only an unauthenticated one fails. Collapsing the two reported a build-host
limitation as a corrupt artifact and withheld a correct release over it.

That executable ships **uncompressed** inside the macOS bundle, and must keep
doing so: the Apple notary service decompresses archive members and scans what is
inside them, so a decoder sealed as a compressed payload is rejected as an
unsigned nested executable and fails the entire macOS release. Shipping it plain
means the app signer rewrites its Mach-O signature, so its released bytes cannot
match the pinned upstream digest. The runtime therefore accepts the macOS decoder
on **either** cryptographic anchor: the pinned upstream digest (a local or
unsigned build, and the pre-signing release gate above) or a valid Developer ID
signature from the release team, evaluated by `codesign` against the exact
private snapshot staged for execution. Neither anchor is a path or a
filesystem-permission claim, and a payload satisfying neither is refused.
`packaging/signing/generate-manifest.py` additionally fails the sign when any
compressed member of the bundle contains a Mach-O, so this class of defect
surfaces at sign time rather than as an opaque notarization `Invalid`.

Legacy provider values and the loader behavior for persisted values are in
[Legacy provider values](#legacy-provider-values).

## Architecture

```
mic -> AudioWorklet (16 kHz mono Int16 PCM) -> WebSocket /api/ws/stt
    -> provider session (local | apple | transcribe)
    -> status / partial / final / endpoint frames
    -> composer (partial tail replaced in place)
```

### Components

The microphone selector represents devices with an empty ID through System
default only. Identifiable devices remain separately selectable; anonymous
enumeration results cannot create a duplicate default option.

The capture worklet keeps resampling phase across render blocks and applies a
63-tap low-pass filter before downsampling, preventing high-frequency input from
aliasing into speech. Precomputed fractional-phase kernels preserve non-integer
ratios such as 44.1 kHz to 16 kHz without sample-position jitter. It emits 100 ms
PCM batches. On release, microphone tracks
stop immediately and the hook enters `draining`; the worklet receives `flush`,
drains the filter history and remaining short PCM frame, then acknowledges with
`flushed`. Only after
that acknowledgement (or the bounded flush timeout) does the client send the
WebSocket `stop`, so the tail precedes finalization. Disconnect recovery preserves
the most recent partial alongside committed utterances. Transcript joins avoid
inserting spaces between CJK characters while keeping Latin word separation.
The shared `website/src/lib/dictationText.ts` rules also apply when dictation
replaces a selection, inserts at the caret, or appends to a background draft.
Authored whitespace is preserved and an empty hypothesis leaves the draft intact.
Meeting captions use the same joining rule and count the actual separator in
their 240-character window. A long caption keeps a bounded recent tail without
splitting a surrogate pair or dropping an unspaced CJK prefix just because a
later Latin word contains a space. Durable meeting transcript storage is unchanged.
Manual release and an automatic capture stop share the composer's stop protection:
they freeze the release caret and disarm semantic submission while results drain.
Late final corrections preserve text the user types after capture has stopped.
Every actual capture end, including a fatal server frame, synchronously fires
the composer's once-only capture-stop protection before deferred socket-close
transcript delivery; explicit cancel and unmount remain discard-only and do not fire it.

| Component | File | Role |
|---|---|---|
| WS endpoint | `src/kiro_crew/dashboard/stt_stream.py` | One provider session per connection, plus the caps and the SEL audit pair |
| Local recogniser | `src/kiro_crew/stt/engine.py` | One resident whisper.cpp context, serialised decodes, idle eviction |
| Local session | `src/kiro_crew/stt/session.py` | Turns a PCM stream into partials and a final |
| Endpointing VAD | `src/kiro_crew/stt/vad.py` | Adaptive-RMS speech detection and end-of-utterance |
| Model catalog | `src/kiro_crew/stt/models.py` | The offered models, their sizes, and the sha256-pinned download |
| Apple helper | `src/kiro_crew/apple_speech/` | Swift `AppleTranscribe.swift` plus its Python driver |
| Config fields | `src/kiro_crew/config/sections.py` | `SttConfig`, and the degradation rules for a stored provider or model |
| Worklet | `website/public/pcm-worklet.js` | Float32-to-16 kHz mono Int16 PCM downsampler |
| Streaming hook | `website/src/hooks/useStreamingStt.ts` | Opens the WS, wires the worklet, emits partial and final |
| Voice hook | `website/src/hooks/useVoiceInput.ts` | Chooses streaming or batch, owns mic and device selection |
| Composer wiring | `website/src/chat-core/composer/useComposerVoice.ts` | The `Composer` root's Voice atom: splices the live region into the input box, owns the one-mic mutex and the frozen-prefix snapshot; `ChatPage.tsx` and `ChatPane.tsx` mount the root and supply only host options |
| Recording UI | `website/src/components/VoiceDictationPanel.tsx`, `VoiceStatusBar.tsx` | The animated panel, and the thin bar it falls back to |
| Settings UI | `website/src/pages/settings/SttSettings.tsx` | Enable, provider, model, language, and the streaming knobs |

## WebSocket protocol

Client to server:

- Binary frames: raw 16 kHz mono, little-endian Int16 PCM; `test/test_stt_stream.py` pins the transport format and frame limits.
- Text frame `{"type":"stop"}`: the user released the mic. The server finishes
  the utterance and closes, so trailing finals still arrive.

Server to client, JSON. `stt.session.SttEvent.kind` supplies the local provider's `partial` and `final` frame types; `dashboard.stt_stream` owns the complete wire contract:

- `{"type":"ready"}`: the session is live and the client may send audio. Capture begins before this arrives, so `useStreamingStt` buffers PCM locally and flushes it in order after readiness. Reaching 60 seconds of buffered PCM stops capture and drains the retained audio after readiness instead of discarding the recording's beginning; the worklet's short flushed tail is retained too. Local sessions additionally advertise `final_timeout_ms`, the browser's stop-to-close allowance: `stt.timeout_secs` plus the native abort grace and a wire grace. Readiness keeps its separate 60-second client timeout. For older servers without a valid allowance, the client uses 315 seconds.
- `{"type":"status","stage":...,"downloaded_bytes":N,"total_bytes":N,"code":...}`
  where `stage` is `downloading`, `preparing` or `ready`. A first-ever local session has to
  fetch weights before it can recognise anything, and a silent transfer is
  indistinguishable from a hang, so the transport emits the notice itself *before*
  starting the fetch and `LocalSession.prepare()`'s own copy of it is dropped on
  return: re-sending it with a zero byte count would walk a progress reading
  backwards. Live byte progress is polled from `GET /api/stt/status` rather than
  pushed. `preparing` covers the sibling case where the weights are already on disk
  but not yet resident, so `prepare()` must load them (and re-hash them against the
  pin) before the first `ready`: that load emits no `downloading` status and is
  otherwise silent to the client, so the transport announces it with a single
  `preparing` frame (zero byte counts, empty `code`) before starting the load. A
  session with nothing to report -- neither a download nor a load, because the model
  is already resident -- emits no status frame at all and goes straight to `ready`.
- `{"type":"partial","text":"..."}`: an in-progress hypothesis that replaces the
  previous one.
- `{"type":"final","text":"..."}`: the committed transcript for the utterance. An empty final explicitly retracts the previous partial, including a hypothesis removed by hallucination filtering or redaction. It contributes no text to semantic endpointing. The client clears that live hypothesis so disconnect recovery cannot restore it.
- `{"type":"endpoint","complete":true}`: the semantic endpointer judged the
  utterance a finished request, so the composer may submit without a keypress.
  Only when `stt.endpointing` is on.
- `{"type":"error","message":"...","code":"..."}`: a setup failure, a refusal or
  a cap. The English `message` is advisory and the `code` is the contract, because
  the dashboard renders localised text and cannot key off a sentence. Codes the
  `stt` package already owns travel through unchanged rather than being remapped;
  the transport adds `_CODE_MAX_DURATION` and `_CODE_SESSION_FAILED` for the two
  conditions only it can see. Only the FIRST fatal claimant sends a frame
  (`_claim_fatal`): otherwise the duration cap and a concurrent failure each emit
  one in the window before the other's close lands, and the client shows two
  contradictory errors for a single failure. `useStreamingStt` resolves the code
  through `sttProviders.streamErrorMessage`, which prefers a stream-specific
  catalog key, falls back to the availability vocabulary the settings panel
  already renders, and only then to `message`.

Partials and finals both pass `security.redact_credentials` and
`security.redact_exfiltration_urls` before emit. A partial is ephemeral and never
persisted, but it is written into the browser DOM, which makes it an external
surface: a spoken credential must not flash unredacted.

## Activation

The endpoint answers **503** unless all three hold:

1. `stt.enabled`
2. `stt.streaming`
3. `stt.provider` is in `stt_stream._STREAMING_PROVIDERS`

The third is positive membership in a named tuple, never an inequality or a
negation against one provider. Adding a name to that tuple grants it the
endpointer, the caps and the `stt_stream_*` audit identity in one step, so the
grant has to be an explicit edit to the set rather than a side effect of not
matching some other provider. `handlers/core.py` serves the same tuple to the
settings page as `streaming_providers`, so the UI gates its streaming controls on
that capability instead of on a hardcoded name.

After the three gates, each provider has its own precondition and failure frame:

- **local**: the recogniser must import (`stt.engine.probe`) and the configured
  model must be on disk. Supported desktop releases bundle the recogniser and
  fail their build if it is missing; macOS Intel is the unsupported exception.
  The model remains an explicit one-click download, and first dictation can join
  the same transfer. Source/PyPI installs can still add the `voice` extra without
  a gateway restart. What cannot be fixed by waiting arrives as an `error` frame
  carrying `stt_extra_missing`, `stt_no_wheel_for_platform` or
  `stt_import_failed`.
- **apple**: `apple_speech.availability()` decides, and separates "this macOS
  cannot run it" from "the Swift toolchain is missing", because only the second
  has a fix.
- **transcribe**: `amazon_transcribe` must be importable, and
  `aws_consent.authorize(SERVICE_TRANSCRIBE, profile, region)` must grant.

### The AWS consent gate is an authorization, not a preference

Transcribe bills per second of audio, so the socket is refused before the client
is constructed and before any audio is read, and the refusal is reported over the
same `error` frame as every other setup failure so the audit pair stays balanced.
The grant is recorded per profile, per region and per resolved account in
`aws_service_consent.json` under the data home, which sits on the read and write
keystone floor, so the agent can neither read the record nor grant itself
permission to spend. The authenticated dashboard is the only writer: there is
deliberately no CLI verb, because a terminal command that records a grant on
request is a grant an automated caller can take.

Moving that check later, adding a CLI verb that records a grant, or reporting the
refusal over some other channel each break one of those three properties.

## The local provider's pipeline

whisper.cpp is not a streaming recogniser: it decodes a buffer. Live text is
therefore produced by decoding repeatedly as audio arrives, and the interesting
question is *what* to re-decode.

**Endpointing.** `stt.vad.Endpointer` consumes the same PCM as the recogniser and tracks an adaptive noise floor rather than a fixed dBFS threshold. A frame must clear `SPEECH_MARGIN_DB`, speech must persist for `MIN_SPEECH_FRAMES`, and quiet for `stt.silence_ms` ends an utterance; `DEFAULT_MAX_UTTERANCE_MS` bounds a session that never becomes quiet. `test/test_stt_vad.py` pins the frame, threshold, and endpointing behavior. The floor falls quickly and rises slowly so sustained speech does not raise it enough to terminate the speaker mid-sentence.

**Partials.** The detector that decides when the utterance ended also decides
where to cut it. On a pause too short to end the utterance, the audio so far is
decoded once and its text is *committed*, and the phrase buffer resets. A partial
is then the committed text plus a decode of the current phrase, so its cost
tracks the current phrase rather than the whole recording. Decoding the entire
utterance on every partial makes each update grow with the recording and can fall behind the speaker. Committed text never regresses under the speaker. Cadence is `stt.partial_interval_ms`, pinned by `test/test_stt_session.py`.

A phrase boundary requires `PHRASE_SILENCE_MS` of consecutive quiet; an individual
quiet analysis frame inside a syllable does not discard the model's phrase context.
The partial interval starts when inference completes, so CPU inference taking
longer than that interval cannot trigger another decode immediately on return.

**Backpressure.** The local transport receives PCM and `stop` independently of
inference, in a byte- and frame-bounded inbox. Its limits admit the browser's
entire readiness burst with headroom for live audio during catch-up. While audio is queued, or after
`stop` has been received, `LocalSession.feed(allow_partial=False)` drains every
sample through endpointing and the final buffer without cosmetic partial or
phrase-commit decodes. This prevents a slow model from repeatedly transcribing
stale frames while the speaker's latest audio waits unread. A stop preserves
the entire queued tail for final recognition and signals the session's abort
predicate to stop an in-flight cosmetic decode. Phrase commits are cosmetic too;
the final never honors that predicate. Overflow reports a coded error;
audio is never silently dropped to meet the memory budget. The receiver and
deadline tasks are joined on teardown.

When semantic endpointing is enabled, a second cheap VAD runs synchronously at
receipt, before queued inference can wait. Newly confirmed speech invalidates
pending judgments even when backpressure suppresses partial decodes. Each local
final carries an internal cumulative PCM sample position (never sent on the wire);
classification and COMPLETE delivery require that position to cover all confirmed
speech received so far. A coalesced chunk's older final cannot acknowledge its
next live utterance, and phrase commits, decode padding, or an audio cap cannot
advance that acknowledgement. The VAD tracks actual confirmed speech separately
from its duration ceiling: ongoing silence neither invalidates a judgment nor
keeps auto-submit waiting forever. An empty final acknowledges its audio too;
when it resolves newer speech that invalidated a judgment, the endpointer may
reclassify previously committed text. It never revives the stale verdict or
classifies an empty transcript.

Receipt of `stop` also starts one total `stt.timeout_secs` drain budget. It covers
the current partial, queued PCM, shared-engine contention, and all remaining final
decodes; multiple utterances do not each restart that budget. The existing
timeout's configuration help also describes this post-recording budget.
Expiry cancels the session task and native decode, allows at most
`DECODE_ABORT_GRACE_SECS` for native
cleanup, then sends `stt_decode_failed` and closes while preserving text already
delivered. Local `ready.final_timeout_ms` adds that cleanup grace and
`_LOCAL_FINAL_WIRE_GRACE_SECS` so the browser can receive this outcome before its own
fallback closes. A configured 300-second decode budget therefore advertises
320 seconds. The independent maximum session-duration cap can end the session
earlier. Transport cancellation abandons remaining audio instead of starting
another final decode during cleanup.

**Language.** Local recognition defaults to `auto`, allowing the multilingual
model to detect the spoken language. `SttConfig.language_code` keeps that stored
preference through provider changes and unrelated configuration saves, including
theme changes. Apple and Transcribe require an explicit locale, so their batch and
streaming calls and the STT settings response use `effective_language_code`, which
resolves `auto` to `en-US` only for those providers. Switching back to local restores
automatic detection. Explicit choices such as `en-US` and `zh-CN` remain unchanged
in both storage and recognition; only the local provider offers `auto` in its picker.

**The final.** One decode of the entire buffer, so the text that reaches the
message box has the full context the model would have had if it had never been
streamed, followed by `filter_hallucinations`. Partials are fast and approximate
on purpose; the final is the accurate one.

Both batch and final recognition retry an empty decode only when the resident
model key changed. Silence does not pay for the same inference twice; all-zero
batch recordings skip model loading and inference entirely.

Cancelling a decode sets its native abort callback and holds the decode lock for
at most `_ABORT_GRACE_SECS` while the worker settles. A worker that ignores abort
loses its resident context before the lock is released, so another request cannot
enter the same native state concurrently. This also applies to cancellation during
timeout cleanup. Cancelling an asyncio task alone cannot terminate a native thread.
Until a retired worker finishes, model preparation refuses a retry rather than
allocating a second set of weights alongside the stuck context. Prewarming a model
that has already completed inference also skips the throwaway silence decode;
eviction clears that warm state so the next load can warm again.
The cold throwaway decode is superseding, so a real audio request invalidates
prewarm work already in flight and can proceed as soon as native abort completes.
This priority applies to prewarm only; final transcripts remain non-superseding.

**A failed decode is not silence, and the engine no longer says it is.** whisper.cpp
reports a failure through `whisper_full`'s return code and pywhispercpp discards it:
`Model._transcribe` calls the binding as a bare statement, then reads
`whisper_full_n_segments`, which is 0 after a failed encode — so `transcribe` answers
the empty list for a failure and the empty list for a quiet room, with the difference
visible only in whisper.cpp's own stderr (`whisper_full_with_state: failed to encode`).
`stt.engine` therefore reads that status itself, from the same extension module the
library calls, and `WhisperEngine.decode` RAISES `engine.DecodeFailed` carrying
`stt_decode_failed` for a native failure, an exception out of the call, or a decode
that outran `DEFAULT_TIMEOUT_SECS`. It still returns `""` for the three non-events a
caller already handles — no resident model, a superseded partial, and an `expect`
mismatch — so an empty transcript means nothing was heard and nothing else.

`LocalSession` splits the two failure classes by what the user keeps. A FINAL decode
failure becomes an `error` event carrying that code, which the transport relays as an
`error` frame before closing; a partial or a phrase-commit failure is logged and
skipped, because the next partial is moments away and one bad decode must not end a
session the speaker is still talking into. A final failure must retain its error
code: treating it as an empty transcript would retract the partial and discard
the utterance with nothing on screen to explain the failure.
`transcribe_pcm` reports the same code through the `Availability` it already returns,
so the batch path names the reason rather than reporting a memo it could not hear.

The detector, not the client, normally ends an UTTERANCE: `feed()` returns the
final, drops that utterance's audio and committed text, and installs a fresh
`Endpointer` for the next one.

**The chunk that ends an utterance is split, not filed whole.** A client chunk can contain both the silence that ends one utterance and speech that starts the next. `Endpointer.push` stops at the frame that closed the utterance and returns
everything after it as `VadUpdate.pending`; `feed()` buffers only the head, finalises,
and then seeds the re-armed buffer and detector with that tail. A large frame can
contain multiple endpoints; the session repeats this split until all have been
finalised, preserving every sample in either a final or the live tail. Filing the chunk whole
attributed resumed speech to the utterance that just closed, where it sits behind a
hangover of silence and contributes nothing, and clipped that word's onset off the
utterance it belongs to. `pending` is empty unless `ended`, because otherwise it would
be the sub-frame carry `push` retains internally and a caller re-feeding it would
duplicate audio. It does **not** end the session, and
`LocalSession.ended` is not set — only a client `stop`, a close, or the session
audio ceiling does that. A session spans many utterances here exactly as it does on
`apple` and `transcribe`, and `useStreamingStt` accumulates finals rather than treating the first as the end. Ending the socket on a recognizer utterance would make continuous transcription stop after the first pause.

An utterance finishing is also a different event from the `endpoint` frame (a
judgment about whether the finished text is a complete request). The transport
skips `finish()` entirely on a session with no deliverable transcript, whose client
went away, or whose socket is closed. That is not tidiness: `finish()` is a decode
of the whole tail, real work on the shared model that a live session behind this one
would queue behind. It is gated on `LocalSession.has_pending_audio` rather than on
"a final was already sent", because over a multi-utterance session both are true at
once and reading the latter discarded whatever was said after the last detected
pause. The endpointer is closed AFTER the final, because the final is the one
segment its judgment is about.

**Residency.** The model is loaded once and reused, removing repeated model-load
cost. Decode latency still depends on the chosen model, hardware, competing work,
and the acceleration actually compiled into the installed runtime. Having a GPU
does not establish that the recognizer uses it; a CPU-only runtime can make a large
model much slower than real time even after warming. Streaming cadence is a limit
on cosmetic work, not a real-time performance guarantee. `stt.idle_evict_secs`
releases weights after a quiet spell to bound resident memory. Decodes run on
`executors.stt_executor()` and hold `WhisperEngine._decode_lock`: `whisper_full`
mutates the context, so two concurrent decodes on one context corrupt each other,
and a superseded partial aborts rather than queueing.

**Boot prewarm.** `dashboard.server._stt_startup_prewarm` loads and warms the model
in the background a few seconds after boot, so the first dictation of a gateway's
life does not pay the cold start. Triggering only on the browser's pointer-down is
too late: the digest verification and the native load sit in front of the first
utterance's own decode, so a user who says a short phrase and stops is still waiting
on them after they have finished speaking. It is registered beside the idle sweep and
cancelled with it at shutdown.

It removes the hash and the load, NOT the decode, which happens either way. Measured
on a 32-core aarch64 CPU build (11 s clip, time from "ready to decode" to "transcript
in hand"): `base` 1.36 s -> 0.66 s, `small` 4.39 s -> 2.44 s, `large-v3-turbo`
15.47 s -> 13.59 s. The saving is dominated by the digest check, which scales with
model size and page-cache state -- the same 1.6 GB model hashed in 1.14 s warm and
5.48 s cold -- so several seconds is the upper bound on a cold host. The first
decode's graph allocation is negligible on a CPU build (30-40 ms, the gap between the
first and second decode after a load). On macOS it is not: a reviewer measured
`ggml_metal_library_init` at 6.364 s on a host whose Metal library cache was cold, and
0.018 s on every run after, because the OS caches the compiled library. Boot prewarm
covers that once-per-machine cost, which makes it worth more on a Mac than the aarch64
figures suggest rather than less.

**A fixture is evidence too, and a wrong one is worse than none.** The capture
harness answered `/api/stt/status` with `size_mb` where the payload's field is
`size_bytes`, so `fmtBytes` saw `undefined` and rendered its non-finite answer: every
screenshot showed the model picker as `base (—)`. A reviewer's blind reader read that
as "something failed to fill in" and was right about the picture and wrong about the
product, which is the worst shape a review can take -- a real defect reported against
code that does not have it, and a fixture that could have hidden a real one just as
easily. Fixture keys are payload keys; the field name is the contract.

**The badge has four states and this repository's hosts only produce one.** `cpu` is
what every published bundle links, so `metal`, `cuda`, `coreml` (encoder-only) and
`unknown` are reachable only by mocking the status payload, and the harness takes the
state from an environment variable for exactly that. The accelerated stills also carry
a second fact worth seeing: the slow-model warning under the picker DISAPPEARS on an
accelerated build, because `large-v3-turbo` is not slow there.

**UI evidence is attached, never committed.** `gh pr edit --attach` rewrites a local
path into a permanent `user-attachments` URL, which is what `docs/ci/ci-and-reviews.md`
prescribes and what the review lanes read. Two wrong answers were tried first and are
worth naming: force-adding the files into `temp-screenshots/` puts binaries in this
repository's history forever past a `.gitignore` rule that exists to prevent exactly
that, and hosting them on a side branch leaves the evidence outside the PR with no tie
to its head, which the design lane rejects as unevaluable. The attachment path is the
only one that satisfies both.

Four restraints, each with a test, because a task on every boot has more ways to do
harm than good:

- It NEVER downloads. Only an already-present model is warmed (`is_present` is
  checked first), so a gateway cannot spend a user's bandwidth on 1.6 GB because it
  restarted. The first-run download stays an explicit `POST /api/stt/prepare`.
- It does not run when `stt.enabled` is false or the provider is not `local`, and in
  those cases it does not even IMPORT the recognizer -- that import pulls numpy and
  the native binding, measured at 169 ms.
- It does not block boot and does not run on the event loop: the delay plus
  `asyncio.to_thread` for the import, exactly as the idle sweep does it.
- It does not run when memory is short: `subagent._available_memory_gb` must report at
  least twice the model's size, and a reading that could not be taken is treated as
  "do not speculate". `large-v3-turbo` measured 1861 MB resident on a reviewer's Mac,
  and a desktop install restarts the gateway with the app -- so without this an 8 GB
  machine pays that per launch for `idle_evict_secs` whether or not its owner dictates.
  The pointer-down prewarm still covers the case.

  The reading is cgroup-CLAMPED rather than the host's `MemAvailable`, which is the
  one that matters in a container: `/proc/meminfo` reports the HOST there, so a 1.6 GB
  model clears a host-wide check and is then OOM-killed against the cgroup limit, and
  because this step runs on every boot that is a crash loop no config change escapes.
  `subagent._available_memory_gb` already takes the minimum of the host reading and the
  tightest visible cgroup headroom on every platform, so it answers the question this
  gate is asking; `resource_status` reuses it across modules the same way, which is why
  this does not add a second implementation.
- It cannot fail the gateway. Every reason it gives up -- no model, no recognizer, a
  load that timed out -- is a state the gateway is expected to run in, so its
  done-callback consumes the exception rather than re-raising the way the sweep's
  deliberately does.

It also passes `stt.idle_evict_secs` and `stt.timeout_secs` when it first reaches
`shared_engine`, because that function is a process singleton whose bounds are set by
its first caller; booting without them would leave the module defaults in force.

**The section label is the backend name.** Easy to get backwards, and getting it
backwards inverts the answer on two of the three shipped platforms. Upstream builds
`whisper_print_system_info()` by walking the ggml backend registry and printing each
registry's NAME as a section label, then that registry's own features as `KEY = VALUE`
pairs. So `VITISAI`, `COREML` and `OPENVINO` are the only genuine backend flags; CUDA,
Vulkan, Metal, ROCm, SYCL and BLAS appear only as labels, and CUDA's own flags are
`ARCHS` / `USE_GRAPHS`. There is no `CUDA = 1` token in any build's output.

The first version of `stt/capabilities.py` flattened the labels away as noise, which
made `detect()` report every Mac (`MTL :`) and every CUDA or Vulkan build as CPU-only
-- the exact failure this module exists to prevent, in the unsafe direction, on the
platforms where acceleration matters most. Two reviewers caught it on real hardware,
one measuring `large-v3-turbo` at RTF 0.070 over Metal on a machine the panel was
warning could not keep up with speech. The fixtures were the reason the suite stayed
green: they synthesized `CUDA = 1` and `METAL = 1` tokens inside the `CPU :` section, a
shape no build emits, so they pinned the assumption rather than the format. Every
fixture is now a verbatim capture, and one test asserts those tokens are ABSENT so a
future fixture cannot drift back.

Apple is the reason label matching is a table of accepted spellings: its Metal registry
shortens to `MTL`, and its BLAS appears as the CPU-registry feature `ACCELERATE` rather
than a `BLAS :` section.

A caveat the module cannot fix: this string is COMPILE-TIME information. Which backend a
load actually uses is stated only by the loader's own `using <name> backend` lines,
reachable through `whisper_log_set` during a load. What is reported is therefore "what
was linked", not "what ran".

**Which acceleration is actually present.** `stt.capabilities` answers this by
parsing `whisper_print_system_info()`, which is produced BY the compiled artifact
and lists the backends linked into it. Nothing else is evidence:
`whisper_context_default_params()` returns `use_gpu=True` and `flash_attn=True` on
a CPU-only wheel (measured on the packaged `pywhispercpp` for linux-aarch64), so
those fields report the request rather than the grant. The published wheels are
described upstream as CPU-only builds, with CUDA, Vulkan, CoreML and OpenBLAS each
requiring a different source build, so "has a GPU, uses the GPU" is a common false
inference rather than an edge case. A build that cannot be interrogated reports
`unknown` and `accelerated: false`: the module never guesses, because a guess here
tells a user to stop expecting a speedup they are not getting.

**There is deliberately no `stt.local_backend` key.** One was built and then removed
before merge, and the reason is worth keeping: its only reader was the status echo
that reported whether it had been honoured. No load path consulted it, so naming
`cuda` on a CPU-only build changed nothing except the sentence the panel printed
about the value the user had just set. A config key is honoured forever once it
ships, and a 14-value one whose entire effect is a note about itself is surface with
no function -- the same objection that retired the adaptive partial-cadence budget in
this change. `detect()` and the badge stay, because reporting the backend the build
actually links is the fix; asking for one was never the fix.

Installing acceleration is a packaging problem (the published wheel links none), and
a config key cannot solve it. If backend selection ever becomes real it needs a
loader that acts on it, which is a different change.

**Decode cost is a fixed floor plus a small marginal term.** whisper.cpp pads every
decode into a fixed analysis window, so the cost of a decode is dominated by a
constant rather than by the length of the audio. Measured with `base` on a 32-core
aarch64 CPU build: 0.5 s of audio in 0.82 s, 2 s in 0.86 s, 8 s in 1.29 s, 11 s in
1.65 s -- about 0.78 s fixed plus 0.08 s per audio-second. Two consequences that
matter for anyone tuning this path:

- The real-time factor of one model on one host spans 1.64 to 0.15 depending only
  on how much audio it was handed, so RTF is a reporting figure and cannot be used
  as a multiplier to project a decode's cost. The previous decode's absolute wall
  time is the honest predictor.
- `stt.partial_interval_ms`'s 400 ms default is below that floor, so on a CPU build
  the cadence is bounded by inference rather than by the setting. The interval is
  measured from the END of a decode, which bounds the queue but not the share of the
  machine cosmetic work takes.

**The Voice panel's shape follows from that.** Two duration pickers were retired from
Settings -> Voice as a consequence of the paragraph above, not as a matter of taste:
`stt.partial_interval_ms` asked a user to choose a cadence the recogniser cannot
honour on any CPU build, and `stt.silence_ms` asked them to tell 700 ms from 750 ms by
feel. Both keys are still read from `config.json`, so an operator who has measured
their own pauses loses nothing; only the pickers are gone, because a dial nobody can
aim is worse than no dial. What a user actually reaches for when dictation cuts them
off is `stt.endpointing`, which is the behaviour those milliseconds were tuning.

The panel now keeps six decisions on its surface (enabled, microphone, provider,
model, language, transcript polish) and puts everything else behind a disclosure,
with the key binding behind a second one inside it. The acceleration rides on the
Status row rather than in a section of its own, and the decode-thread count and the
last decode's cost sit in the tip beside it: they answer "why is it slow", which is a
question a user goes looking for, so they do not need permanent space.
`test/SttSettings.surface.test.tsx` pins that shape, because a regression here does
not throw -- it quietly puts a knob back on the surface.

**Where the streaming time actually goes.** Measured streaming an 11 s clip in real
time through the full session on the same host: with `base`, 7 partials costing
4.1 s and 5 phrase commits costing 2.9 s, against a 1.65 s final -- cosmetic and
phrase-commit inference together are roughly 4x the cost of the text the user keeps.
With `large-v3-turbo` the same clip costs 40 s of partials and 68 s of phrase
commits, and the session finishes ~138 s behind real time. Phrase commits, whose
text is RETAINED and which are therefore not cosmetic, are the larger share in both
cases. Any future attempt to reduce streaming cost should start there rather than at
the partial cadence.

`stt.engine`'s docstring carries the two properties that make this safe inside
the gateway process: whisper.cpp releases the GIL for the duration of a decode,
and it writes nothing to stdout with `print_progress=False` and
`print_realtime=False`. The second matters because the MCP servers import this
module and their stdout *is* their protocol. Neither argument may be removed.
stderr is not quiet, so no test may assert it empty.

`redirect_whispercpp_logs_to` stays at its `False` default. Its binding governs stderr rather than the log callback, and `None` redirects process-wide fd 2 during model loading, silencing unrelated threads while leaving stdout behavior unchanged.

## Tidying a finished transcript

`stt.polish` (default **false**) hands a FINISHED transcript to a fast model for
punctuation and capitalisation -- never words, and never where they divide. It is off by default because it is the one part of the local
provider that sends anything off the machine: the TEXT leaves, the audio never does.
The switch is the consent, so `POST /api/stt/polish` refuses with 403
`stt_polish_disabled` when it is off rather than quietly passing the transcript
through -- a setting that is honoured only sometimes is a decoration.

Why it is an endpoint and not a frame on the speech websocket, which is the design
decision worth recording: that socket closes shortly after the final (the server has
a bounded deadline to deliver it and then ends the session), so a correction routed
through it would be cancelled in the most common case of all -- the user stops talking
and the correction is still in flight. An endpoint also serves the MediaRecorder batch
path, which never opens that socket, and it can hand the caller BOTH strings so
reverting is a local swap rather than another round-trip.

It reuses `llm_helpers.run_bg_oneliner` with `model="auto"`, the same seam
`stt.endpointing` already uses, so this adds a second consumer of an existing boundary
rather than a new one. The prompt forbids changing a word at all, and
**translating** specifically -- the last because code-switched speech is the least accurate input
the recogniser has (CER 0.436 on `base`) and therefore the input a model is most
tempted to "fix" by rendering it in one language.

Four properties, each with a test:

- **Never blocks dictation.** The recogniser's own text is in the composer and is
  already sendable before the request goes out. The correction replaces it a moment
  later or does not arrive at all.
- **Never changes a WORD.** The load-bearing guard, and the length band alone was not
  it. A reply is accepted only when its letters and digits, lowercased, are identical
  to the original's; the 0.6-1.8x length band is kept merely to bound the comparison
  on a pathological reply. A length check by itself admits every substitution of a
  similar length, which is every meaning-changing rewrite there is: `deploy to
  staging` comes back as `deploy to production`, passes, and lands in the composer of
  a user who is by definition not watching the text appear.

  The comparison is per CHARACTER rather than per token, because in Chinese the words
  ARE the characters and a token-based check would compare one giant token and notice
  nothing. It is blind to marks and to case, which is exactly the set of changes this
  feature is for, and NOT blind to where the words divide, because that is where the
  corruption hides: `apart` and `a part` share their letters and mean opposite things.

  So every division in the transcript must survive, and a division the transcript did
  not have may only appear where the separator is punctuation a correction pass is
  entitled to insert -- a mark with no whitespace beside it, or a boundary between a
  script that spaces its words and one that does not.
  Both halves of that allowance turn on the same property, and the mark half is the
  narrower one: a mark may create a division ONLY inside a script that runs its words
  together. Chinese writes `部署到测试环境` as one run, so the comma this feature exists
  to insert necessarily creates a division and refusing it would refuse the feature; a
  space between Chinese and Latin text is a typographic fix on the worst-measured
  bucket (code-switched zh/en, CER 0.436).

  A script that SPACES its words gets no mark-created divisions at all, because there
  the division is a word being replaced. `shell continue` returned as `She'll continue`
  keeps every letter and changes who is continuing, and `well` to `we'll`, `wont` to
  `won't` and `were` to `we're` are the same edit -- restoring a contraction apostrophe
  and rewriting a word are indistinguishable from outside, which is the same reason
  this pass does not attempt mis-hearing corrections at all. The cost is real and
  accepted: `dont` stays `dont`. English loses nothing else, because a full stop or a
  comma it legitimately restores goes on a division the transcript already has, and an
  apostrophe the recogniser itself produced is punctuation like any other -- the rule
  forbids CREATING a division, not keeping one.

  The space allowance asks whether each side's script SPACES its words, and not
  whether the two scripts differ. `isascii()` reads like a cheap stand-in for that and
  is wrong in the direction that costs a word: `é` is not ASCII and `i` is, so
  `caféine` returned as `café ine` looks like a boundary between two scripts when it
  is one Latin word cut in half, and an accent beside a plain letter is ordinary input
  in every accented language. Asking about spacing instead also refuses a seam the
  scripts-differ test would have allowed -- Japanese crosses kanji and kana inside one
  word, so `食べる` as `食 べる` has no place for a space either. Unicode has no script
  property in the standard library; the character NAME carries it as its first word
  (`LATIN SMALL LETTER E WITH ACUTE`, `CJK UNIFIED IDEOGRAPH-4E2D`), which is enough
  for a question this coarse. Digits count as spaced, so a space between Chinese text
  and a number is accepted for the same typographic reason a space before Latin text
  is, while `abc123` as `abc 123` is not.

  The content check underneath all of that discards NOTHING, and that is deliberate.
  Every check built on normalising both sides and comparing the remainder has to decide
  what to drop, and whatever it drops becomes a class the model may change unobserved:
  the same guard successively missed symbols (`hello` -> `hello 🚀`), combining marks
  (`कि` -> `क.`) and a symbol RELOCATED rather than added (`$100 fee` -> `100$ fee`,
  same letters, same symbol, same divisions). So the two strings are case-folded and
  ALIGNED instead, and every span that is not common to both must consist purely of
  punctuation or whitespace. That one rule covers words changed, added, removed or
  reordered, symbols and control codes inserted, deleted or moved, and combining marks
  stripped -- the only characters exempt from the comparison are the ones the feature
  exists to change. Whitespace is tested with `str.isspace` rather than by category,
  because a newline and a tab are control characters and are ordinary separators here.
  The division rules still stand on top of it: deleting a space IS deleting
  whitespace, so where the words divide needs its own rule. The 0.6-1.8x length band
  runs first, which bounds the alignment's cost on a pathological reply.

  `GET /api/stt/status` skips the acceleration probe unless the local provider is BOTH
  selected and enabled. Reading it calls `whisper_print_system_info()`, which on macOS
  runs `ggml_metal_device_init` -- +31.8 MB resident held for the process lifetime and
  a one-off 6.4 s library build on a cold cache -- so an operator who turned voice off
  would otherwise pay that for one visit to Settings. The `backend` block is ABSENT
  rather than null in that case, so the panel renders nothing instead of an unknown.

  The PROMPT is narrow for the same reason. Asking a model to fix "words the recogniser
  clearly misheard" cannot be made safe: a correct correction and a meaning change are
  the same operation seen from outside. Punctuation and
  capitalisation are the subset whose result can be VERIFIED
  rather than trusted -- and they are what the panel promised all along ("fixes
  punctuation"), so the broader prompt was asking the user to consent to something the
  interface never mentioned.

  The response reports `changed: false` for a decline, a timeout, an empty reply, a
  length rejection and a word rejection alike: from the caller's side all five mean
  "keep what you have", and distinguishing them would invite a client to treat a safe
  outcome as a failure. The two rejections are logged distinctly, because a word
  rejection means the prompt is not holding and a length one only means the model
  rambled.
- **Redacted both ways, and a redaction that BITES declines the reply.** Credentials
  and exfiltration URLs are stripped from the text before it is sent, and again from
  the model's reply, which is new text the outbound pass says nothing about. The second
  pass runs after the word check, so if it changes anything the text is no longer the
  text that passed: a marker has replaced a span the user dictated, which is the exact
  failure the word check exists to prevent, arriving after it ran. Ordering cannot fix
  this -- redacting first would validate a string the user never dictated either -- so
  the reply is declined and the original returned. Reachable in practice: the words
  `send it to https evil test steal data AKIA...` re-punctuate into a URL whose letters
  are identical, every original division survives as a mark, and the scrubber then
  rewrites the whole span.
- **Belongs to the composer that asked.** The originating `sessionId` is captured when
  the request goes out and re-checked before the replacement is applied. A value check
  alone is not enough and the gap was not theoretical: two slots routinely hold
  byte-identical drafts -- an empty one is the common case -- so a late reply for slot
  A satisfied "the text is unchanged" and rewrote slot B's draft. Identity has to be
  checked as identity.

- **Not applied to a manually-stopped stream, which is a known gap.** After a manual
  stop the partial route (not the delivery route) is what turns the last hypothesis
  into the real transcript, and it keeps firing while the socket drains -- so there is
  no "this one is final" signal to polish against, and polishing a mid-drain
  hypothesis would be overwritten by the next partial. The falling edge of
  `useVoiceInput`'s `streamDraining` is the signal a fix would use; wiring it is a
  change to the streaming path rather than to this feature, so it is recorded here
  rather than guessed at. A manually-stopped stream therefore delivers an unpolished
  transcript even with the setting on.

- **Never overwrites the user.** `useComposerVoice.polishDictation` rewrites only the
  span it wrote itself, and only while the composer value is still byte-identical to
  what the delivery left. If the user typed, sent, or another utterance landed, the
  replacement is dropped rather than merged -- guessing at a merge there deletes text
  the user authored after they stopped talking, which is worse than not polishing.
  It never auto-submits.

## Model download

`stt.models` holds the catalog: name, byte size and a sha256 digest per entry. The
rows are `tiny`, `base`, `small` and `large-v3-turbo`, unchanged by this work.

**A quantized catalog was built here and then withdrawn before merge.** The
measurements stand and are worth keeping, because they are why the idea was
attractive: on a CPU build `base-q8_0` decodes an 11 s clip in 0.40 s against `base`'s
0.62 s for a byte-identical transcript at 45% of the download, and over 60 clips in ten
language buckets `tiny` was dominated outright by it (Portuguese 0.826 against 0.409,
Italian 0.761 against 0.477).

It was withdrawn for two reasons, both of which outrank the measurements:

- **It is a product-shape change, and this was not the change to make it in.** Removing
  `tiny` and full-precision `small` alters what a user can download and run. That needs
  its own decision with its own record; measurements in a pull-request description are a
  proposal, not an accepted one. Making local dictation honest about its speed never
  required changing which models exist.
- **The speed premise is platform-local.** Every figure above was measured on a CPU
  build. On Metal a reviewer measured full-precision `small` decoding 5.5 s of audio in
  195 ms (RTF 0.036), where the 2.6x download `small-q5_1` saves buys nothing while
  `small`'s accuracy advantage over it (Chinese CER 0.349 against 0.413) still applies.
  A cull justified by CPU speed would be wrong for every accelerated user.

A retired row would also have stranded its weights: `ggml-tiny.bin` and
`ggml-small.bin` stay on disk with no catalog row referencing them, so nothing lists
them and nothing reclaims them. Any future cull has to answer that first.

**`large-v3-turbo` is slower than real time on a CPU build** (RTF 1.24, so 11 s of
speech costs 13.6 s) and is kept, because measured per language it is the best model
available by a wide margin for exactly the users who need one: German 0.063 against
`small`'s 0.180, Italian 0.114 against 0.239, Portuguese 0.113 against 0.139, and
code-switched Mandarin-English 0.282, the best reading any model produced on that
bucket. Its cost is made VISIBLE instead, which is this change's actual subject:
`GET /api/stt/status` reports the backend and the measured real-time factor.

`PUT /api/config/stt` resolves the model field through `models.canonical_name` rather
than testing membership of the catalog: a membership test rejects an alias, so
someone whose stored model was retired could not save the panel at all -- a field
they never edited was refused on every write. `canonical_name` is deliberately not
`resolve`: an unrecognised name returns `None` and leaves the stored value alone,
where `resolve` would answer the default and let one junk request replace a model the
user deliberately chose.

Four endpoints expose it, all four refused to an app token by `_deny_app_token`
because they start a download and warm a resident model inside the gateway, which
is operator setup rather than something an app earns by naming a path (the
transcription surfaces are deliberately open to an app token):

- `GET /api/stt/status`: the availability code and prose, the resolved model with
  `model_present` and its size, whether a model is resident right now, and the live
  transfer state. Separate from `GET /api/config/stt`, which serves settings. It also
  carries `backend: {name, accelerated, encoder_only, detail, cpu_features, sections,
  system_info, threads, os, arch, python}` -- read from the build, with
  `accelerated: false` whenever it cannot be interrogated. `sections` is the ordered
  list of ggml registry LABELS, which is where `name` comes from. The whole `backend`
  block is ABSENT rather than null-filled when the provider is not `local`, so
  nothing reports a CPU backend for a cloud recogniser. Then `timings`, the most
  recent load episode split into
  `hash_ms` / `load_ms` / `first_decode_ms` plus recent per-decode costs. `timings`
  holds durations, counts and the model name only: never audio, never a transcript,
  no paths and no host identity, so it is safe to quote in a bug report. Splitting
  the load episode is what makes a cold start legible -- a 1.6 GB model spends
  seconds in its digest verification before the recognizer is asked to do anything,
  and one total cannot tell that apart from a slow model.
  It also carries `ffmpeg: {present, source, auto_fetch, os, arch, download}`.
  `source` is `bundled` | `system` | `store` | `null` and names WHICH decoder the
  transcode path would run, because each one is repaired differently — reinstall
  the app, the host's package manager, or the fetch below. `auto_fetch` is
  `available` | `unsupported` | `bundled`, so the panel offers a button rather than
  a shell command, and `unsupported` is not a failure: no retry can make a pinned
  artifact exist. `os`/`arch` are the GATEWAY's, since a dashboard may be open
  against another machine and the agent hand-off has to name the host that needs
  fixing. `ffmpeg_missing` on `GET /api/config/stt` is kept for compatibility.
- `POST /api/stt/prepare`: starts or joins the transfer and returns its current state. Concurrent callers share one transfer behind the store's lock.
- `POST /api/stt/prewarm`: starts local-provider preparation without waiting for completion. `useVoiceInput` calls it while the user reaches for the microphone so local initialization can overlap capture.
- `POST /api/stt/ffmpeg/download`: starts or joins the decoder fetch, 202 then
  poll, the same contract as `prepare`. Refused 409 on a bundled interpreter
  (`stt_decoder_bundled`) and on a platform with no pinned artifact
  (`decoder_unsupported_platform`) rather than answering 202 for work that can
  never help. A completed whisper-model download also starts this in the
  background when the interpreter is not bundled and no decoder resolves — that is
  the one moment a second transfer reads as part of the same setup, instead of
  surfacing as a hang during a first voice memo.

Desktop installers intentionally contain no speech-model weights. Settings shows
the selected model's exact size and a **Download now** action; that is the only
setup action a desktop user needs because the recogniser and its dependencies are
already in the application. First use can start or join the same download, and
every later session loads the verified model from disk. The digest is the trust
anchor for that fetch: bytes are streamed to a
staging file inside the target directory and renamed into place only after the
computed digest matches, so a tampered mirror, a truncated transfer or a
captive-portal HTML body can only fail verification. The pinned **size** is enforced
as a ceiling during the transfer rather than compared afterwards: nothing about an
HTTPS response bounds its length, `Content-Length` is the server's claim rather than
the pin, and the operator can point `KIROCREW_WHISPER_MODEL_BASE_URL` at any host, so
streaming to EOF first let a hostile or misconfigured mirror fill the disk before
anything was checked. The refusal precedes the write, which caps the overshoot at one
read; the post-loop size comparison is then only reachable for a *short* response,
which is the common failure and keeps its own message. The staging file comes from
`tempfile.mkstemp`, written through the descriptor it returns: the name is
unpredictable and the create is exclusive, so a symlink pre-planted at a guessable
staging path cannot redirect the write.

A file already on disk is verified against the pin too, on every model LOAD. Not once
per session, because `WhisperEngine.ensure_loaded` settles residency before asking the store. It is not memoised against size and mtime because
`os.utime` is available to anything that can write the file.

The digest is the second line of defence, not the first. Verifying and then handing a
PATH to a native loader leaves a window in which the bytes can be swapped, and
re-hashing cannot close it because the loader re-opens by name. What closes it is that
`<data home>/models` is **write-protected from the agent on both gates**
(`security._WRITE_PROTECTED_HOME_PATHS` for the file tools,
`_WRITE_PROTECTED_BASH_LEAVES` for the shell), so the verified bytes are the loaded
bytes. Reads stay allowed — the weights hold no secret and the settings surface reports
what is installed — and Kiro Crew's own downloader writes directly without routing
through those gates, so a first fetch and a re-download after a failed check both work.
The same directory holds the embedding GGUF, which the one entry covers.

`is_present` also checks the file's size, which is what makes an interrupted
download visible: a staging file never occupies the final path, so a wrong size
there means a replaced or truncated file, and reporting it as absent lets the next
download overwrite it. `MODEL_URL_ENV` repoints the base URL for a mirrored or
air-gapped install without weakening the pin, and `SKIP_DOWNLOAD_ENV` is the same
switch the embedding downloader honours, so one setting means "this process must
not pull model weights".

## Caps and limits

Every limit is a named constant in the module that owns it. The values are not
restated here, because a copied constant goes stale silently.

| Constant | Module | Bounds |
|---|---|---|
| `_MAX_CONCURRENT_SESSIONS` | `dashboard/stt_stream.py` | Sessions per gateway process |
| `_MAX_STREAM_DURATION_SECS` | `dashboard/stt_stream.py` | Wall-clock life of one connection |
| `_MAX_WS_MSG_SIZE` | `dashboard/stt_stream.py` | One inbound audio frame |
| `_MAX_TEXT_FRAME_BYTES` | `dashboard/stt_stream.py` | One inbound control frame |
| `_MAX_LOCAL_BUFFER_BYTES`, `_MAX_LOCAL_BUFFER_FRAMES` | `dashboard/stt_stream.py` | PCM awaiting local inference, by bytes and frame count |
| `_MAX_MODEL_PREPARE_SECS` | `dashboard/stt_stream.py` | The one-time model fetch a first-ever `local` session waits on |
| `heartbeat` on `WebSocketResponse` | `dashboard/stt_stream.py` | Idle liveness ping interval |
| `MAX_SESSION_SECS` | `stt/session.py` | Audio one local session buffers |
| `MAX_PHRASE_SECS` | `stt/session.py` | Phrase length before a commit is forced |
| `PHRASE_SILENCE_MS` | `stt/session.py` | Sustained quiet needed to commit a phrase |
| `MIN_DECODE_SECS`, `MIN_COMMIT_SECS` | `stt/session.py` | Floors below which a decode or a commit is not worth doing |
| `THREAD_CEILING` | `stt/engine.py` | Extrapolation ceiling on the derived thread count |
| `DEFAULT_TIMEOUT_SECS` | `stt/engine.py` | One decode or one model load |
| `DEFAULT_MAX_UTTERANCE_MS`, `MIN_SILENCE_MS` | `stt/vad.py` | Utterance backstop, and the floor on `stt.silence_ms` |

The duration and concurrency caps exist for a different reason per provider and
for an unbounded cost in every case. On `transcribe` an abandoned socket bills per
audio-second and counts against the account's concurrent-stream quota; on `apple`
it holds a helper process and an OS recognition session; on `local` it accumulates
buffered audio and keeps queueing decodes onto the one shared model. The
concurrency cap is shared by the free providers because all still consume bounded local capacity.

The model fetch gets its own ceiling rather than borrowing the session's because it transfers a catalog entry rather than a dictation. `stt.models` uses a request timeout, while `_prepare_local_with_progress` also bounds how long a WebSocket waits. On the WebSocket timeout the transfer is shielded and left running:
cancelling it would release the model store's transfer lock while its worker thread
is still writing the staging file, and the next session would start a second write
to the same path. Only the socket gives up; the bytes land for the next attempt.

`test/test_stt_stream.py` pins the transport caps, `test/test_stt_session.py` the
session ones, `test/test_stt_vad.py` the detector's thresholds and
`test/test_stt_engine.py` the thread derivation and the availability codes.

## SEL audit pairing: emit before closing

Every accepted connection logs `stt_stream_start`, and **every** exit path must
log a matching `stt_stream_end` (`error`, `refused`, `timeout` or `ok`) or the
audit trail shows an unmatched start. A rejection before the socket is prepared
logs `stt_stream_rejected` instead.

`stt_stream_end` is emitted **before** `await ws.close()`, never after, on the
early-return paths (via `_close_and_end_audit`) and on the normal cleanup path.
`WebSocketResponse.close()` awaits the *peer's* close acknowledgement under its
own timeout, so a client that has already gone away (an abrupt disconnect, a
closed tab) parks the handler inside `close()`, and with the audit after the close
the end event is withheld for as long as that takes. Emitting first makes the
pairing independent of the peer, which is the property a balanced trail actually
needs. The close still runs, is still awaited immediately after, and still
tolerates a broken transport (logged, not raised).

A claimed fatal cause outranks the read loop's own outcome: the loop can exit
cleanly because the cap or the relay closed the socket under it, and recording
that as `ok` would report a session that died as a session that finished.

Tests asserting on the audit pair must **wait** for the end event: neither
receiving the error frame nor exiting the `TestClient` context orders the
assertion after the server handler's remaining steps, so asserting straight after
either one is a race.

## Frozen-prefix behaviour

`useComposerVoice.ts` (the `Composer` root's Voice atom, mounted by `ChatPage.tsx`
and `ChatPane.tsx`) snapshots the composer's contents and the caret on the first
`partial` of an utterance. Later partials replace only the live region after that
snapshot, so anything the user typed before speaking survives, and the caret does
not jump. The snapshot clears on the final, so the next utterance starts from the
newly committed text.

## Legacy provider values

`_validated_stt_provider` in `config/sections.py` accepts only `local`, `apple`, and `transcribe`. Persisted `whisper`, `mlx`, `parakeet`, or `faster` values degrade to `local` and log the replacement rather than preventing the gateway from loading a voice setting. `stt.models` resolves legacy model aliases to a catalog entry; unknown models fall back through the loader's validation path.

Legacy config fields such as `whisper_path`, `mlx_model`, `parakeet_model`, and `device` are ignored by `KiroCrewConfig.load` because `SttConfig` does not consume them. `config/superseded_defaults.py` records migrated defaults for the config surface.

## Deliberately not built

- **Speaker diarisation and word-level timestamps.** Neither has a consumer in
  the composer, and both change the frame shape every client conforms to.
- **A neural VAD.** It would add a second model download and native dependency; `stt.vad.Endpointer` supplies the current adaptive-RMS decision.
- **Fan-out of one utterance to several agents.** One session drives one
  composer.
